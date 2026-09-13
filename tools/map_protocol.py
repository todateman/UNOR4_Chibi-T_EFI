#!/usr/bin/env python3
"""MAPコンソール（src/map_console.cpp）のプロトコル層。

tools/send_map.py（CLI）と tools/map_gui.py（Web GUIのブリッジ）で共有する。
ここにはシリアルの行プロトコルだけを置き、UIやHTTPの都合は持ち込まない。

ファーム側の仕様で、PC側の実装に効いてくる点:

- 応答は必ず1行の "OK ..." / "ERR ..."。1行送るごとに応答を待てばRX FIFOが溢れない。
- 空行には応答が返らない。CRLFを送ると2行目が空行になりACK待ちが固まるため、
  送信は必ずLFのみにする。
- 応答は Serial.println なので終端はCRLF。受信側で strip する。
- テレメトリ行とコマンド応答が同じストリームに混在する。テレメトリは
  「タブを含む行」で判別する（新書式は "T\\t" 始まり、旧書式は9フィールドのタブ区切り）。
- 起動バナー "MAP SOURCE: ..." がACK待ちのbodyに混入しうる。
- MAP SAVE はデータフラッシュの消去・書き込みを含むため応答が遅い（最大10秒）。
  またベンチではノイズで tachoRpm が一瞬非ゼロになり ERR ENGINE_RUNNING が返ることが
  あるので、リトライする。
"""

from __future__ import annotations

import struct
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional, Sequence

BAUD = 115200

# 応答待ちタイムアウト（秒）。MAP SAVE/LOAD/DEFAULT はデータフラッシュ操作を含む。
ACK_TIMEOUT = 3.0
SLOW_TIMEOUT = 10.0
SLOW_COMMANDS = ("MAP SAVE", "MAP LOAD", "MAP DEFAULT")

# ENG_ON / tachoRpm のノイズで MAP SAVE が弾かれたときのリトライ。
# 実測（UNO R4 Minima 単体、Ardu-Stim 未接続）では tachoRpm が 36% の確率で
# 15rpm を拾い、3回では 5% 程度取りこぼす。5回なら 99% 以上通る。
SAVE_RETRIES = 5
SAVE_RETRY_WAIT = 0.4

# Arduino UNO R4 Minima (Renesas RA4M1) の VID
ARDUINO_VIDS = (0x2341, 0x2A03)

# MAP検証レンジ（src/map_store.h と一致させること）
MAP_MAX_ENTRIES = 24
MAP_RPM_MAX = 20000
MAP_INJ_MAX = 255
MAP_IGN_CA_MAX = 90

CSV_HEADER = "RPM,  INJ(0.1msec), IGN(CA)"


class MapConsoleError(RuntimeError):
    """コマンドがERRで返った、またはプロトコル上の異常。"""


class MapConsoleTimeout(MapConsoleError):
    """応答が返らなかった。"""


# -----------------------------------------------------------------------------
# 行の分類
# -----------------------------------------------------------------------------
def classify_line(text: str) -> str:
    """受信した1行を 'telemetry' / 'ack_ok' / 'ack_err' / 'body' に分類する。

    テレメトリ判定をタブの有無で行うのは、新書式("T\\t...")と旧書式(9フィールドの
    タブ区切り)の両方を1つの規則で拾うため。コマンド応答とCSVダンプ行はタブを
    含まないので衝突しない。
    """
    if not text:
        return "body"
    if "\t" in text:
        return "telemetry"
    if text.startswith("OK"):
        return "ack_ok"
    if text.startswith("ERR"):
        return "ack_err"
    return "body"


@dataclass
class Telemetry:
    """機械可読テレメトリ1サンプル。単位はファームの生の値に揃えてある。"""

    seq: int = 0
    ms: int = 0
    rpm: int = 0
    inj01: int = 0          # x0.1ms
    ign: int = 0            # CA
    spd01: int = 0          # x0.1km/h
    ne: int = 0             # CA
    row: int = 255          # 採用中のMAP行。255 = MAP未使用（始動時・範囲外）
    flags: int = 0
    legacy: bool = False    # 旧2Hz書式から復元したサンプル

    FLAG_ENG_ON = 0x01
    FLAG_LAUNCH = 0x02
    FLAG_CRANKING = 0x04
    FLAG_OUT_OF_RANGE = 0x08

    @property
    def eng_on(self) -> bool:
        return bool(self.flags & self.FLAG_ENG_ON)

    @property
    def out_of_range(self) -> bool:
        return bool(self.flags & self.FLAG_OUT_OF_RANGE)

    @property
    def running(self) -> bool:
        """稼働中の判定。MAP INFO のスナップショットではなくこちらを真とする。"""
        return self.eng_on or self.rpm > 0


def parse_telemetry(text: str) -> Optional[Telemetry]:
    """テレメトリ行をパースする。新書式・旧書式の両方に対応。

    新: T\\t<seq>\\t<ms>\\t<rpm>\\t<inj01>\\t<ign>\\t<spd01>\\t<ne>\\t<row>\\t<flags>
    旧: <rpm>\\t<inj_ms>\\t<ign>\\t<speed>\\t<dist>\\t<gas>\\t<fuel>\\t<work>\\t<ne>
    """
    parts = text.split("\t")
    try:
        if parts[0] == "T":
            if len(parts) < 10:
                return None
            v = [int(x) for x in parts[1:10]]
            return Telemetry(seq=v[0], ms=v[1], rpm=v[2], inj01=v[3], ign=v[4],
                             spd01=v[5], ne=v[6], row=v[7], flags=v[8])
        if len(parts) == 9:
            # 旧書式は小数を含み、seq/row/flags を持たない
            return Telemetry(
                rpm=int(parts[0]),
                inj01=int(round(float(parts[1]) * 10)),
                ign=int(parts[2]),
                spd01=int(round(float(parts[3]) * 10)),
                ne=int(parts[8]),
                legacy=True,
            )
    except (ValueError, IndexError):
        return None
    return None


# -----------------------------------------------------------------------------
# MAPテーブル
# -----------------------------------------------------------------------------
Row = tuple  # (rpm, inj, ign)


def validate(rows: Sequence[Row]) -> Optional[str]:
    """src/map_store.cpp の validateTable() と同じ規則で検証する。

    問題が無ければ None、あればファームと同じエラー名を返す。
    """
    if not rows:
        return "NO_ROWS"
    if len(rows) > MAP_MAX_ENTRIES:
        return "TOO_MANY_ROWS"
    prev = None
    for rpm, inj, ign in rows:
        if rpm < 1 or rpm > MAP_RPM_MAX:
            return "RPM_OUT_OF_RANGE"
        if not 0 <= inj <= MAP_INJ_MAX:
            return "INJ_OUT_OF_RANGE"
        if not 0 <= ign <= MAP_IGN_CA_MAX:
            return "IGN_CA_OUT_OF_RANGE"
        if prev is not None and rpm <= prev:
            return "RPM_NOT_ASCENDING"
        prev = rpm
    return None


def crc16(rows: Sequence[Row]) -> int:
    """MAPのCRC。MAP INFO の crc= と突き合わせて転送を検証するために使う。

    CRC-16/CCITT-FALSE を MapEntry の生バイト列に対して計算する。
    MapEntry は { uint16 rpm; uint8 inj; (padding 1); uint16 ign; } の6バイトで、
    ファーム側は sizeof(MapEntry)*count バイトを対象にするため、
    パディングの1バイト(常に0)も含めて詰める必要がある。
    """
    data = b"".join(struct.pack("<HBxH", rpm, inj, ign) for rpm, inj, ign in rows)
    crc = 0xFFFF
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def read_csv_rows(path: str) -> list:
    """CSVファイルから (rpm, inj, ign) のリストを読む。ヘッダ・空行・#行は無視。"""
    rows = []
    with open(path, "r", encoding="utf-8-sig") as f:
        for lineno, raw in enumerate(f, 1):
            line = raw.strip()
            if not line or line[0] in "#;":
                continue
            if not line[0].isdigit():
                continue  # ヘッダ行
            parts = [c.strip() for c in line.split(",")]
            if len(parts) < 3:
                raise MapConsoleError(f"{path}:{lineno} 列が足りません: {line}")
            try:
                rows.append((int(parts[0]), int(parts[1]), int(parts[2])))
            except ValueError:
                raise MapConsoleError(f"{path}:{lineno} 数値ではありません: {line}")
    if not rows:
        raise MapConsoleError(f"{path} に有効な行がありません")
    return rows


def format_csv(rows: Sequence[Row]) -> str:
    """microSD/RPM_*.CSV と同じ書式で書き出す。"""
    lines = [CSV_HEADER]
    lines += [f"{rpm},{inj},{ign}" for rpm, inj, ign in rows]
    return "\n".join(lines) + "\n"


# -----------------------------------------------------------------------------
# ポート探索
# -----------------------------------------------------------------------------
def list_ports() -> list:
    """ECUの候補になるシリアルポートを列挙する（GUIのポート選択用）。"""
    from serial.tools import list_ports as _lp

    out = []
    for p in _lp.comports():
        likely = p.vid in ARDUINO_VIDS or "usbmodem" in p.device or "ttyACM" in p.device
        out.append({
            "device": p.device,
            "description": p.description or "",
            "vid": p.vid,
            "pid": p.pid,
            "likely": likely,
        })
    out.sort(key=lambda d: (not d["likely"], d["device"]))
    return out


def find_port() -> str:
    """候補が1つに絞れればそのデバイス名を返す。絞れなければ例外。"""
    candidates = [p["device"] for p in list_ports() if p["likely"]]
    if not candidates:
        raise MapConsoleError("シリアルポートが見つかりません。--port で明示してください。")
    if len(candidates) > 1:
        raise MapConsoleError(
            f"候補が複数あります ({', '.join(candidates)})。--port で明示してください。")
    return candidates[0]


# -----------------------------------------------------------------------------
# コンソール本体
# -----------------------------------------------------------------------------
@dataclass
class Response:
    ack: str
    body: list = field(default_factory=list)

    @property
    def code(self) -> str:
        """"OK APPLIED 15" なら "APPLIED 15"。"""
        return self.ack.split(" ", 1)[1].strip() if " " in self.ack else ""


class _Pending:
    __slots__ = ("line", "event", "ack", "ok", "body")

    def __init__(self, line: str):
        self.line = line
        self.event = threading.Event()
        self.ack: str = ""
        self.ok: bool = False
        self.body: list = []


class MapConsole:
    """1本のシリアル接続に対する行プロトコル。

    受信を1つのスレッドで回し続け、テレメトリはコールバックへ、コマンド応答は
    待っている command() へ振り分ける。send_map.py の旧実装のように送信前に
    reset_input_buffer() はしない（テレメトリが流れ続けるので取りこぼす）。

    ファームは「非空行1つにつきOK/ERRを1つだけ返す」ので、同時に1コマンドだけ
    走らせれば順序一致だけで正しくマッチする。IDタグは不要。
    """

    def __init__(self, ser, on_telemetry: Optional[Callable[[Telemetry], None]] = None,
                 on_unsolicited: Optional[Callable[[str], None]] = None):
        self._ser = ser
        self._on_telemetry = on_telemetry
        self._on_unsolicited = on_unsolicited
        self._send_lock = threading.Lock()   # command() を直列化する
        self._state_lock = threading.Lock()
        self._pending: Optional[_Pending] = None
        self._buf = ""
        self._stop = threading.Event()
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    # -- 受信 ---------------------------------------------------------------
    def _read_loop(self) -> None:
        while not self._stop.is_set():
            try:
                n = getattr(self._ser, "in_waiting", 0) or 1
                chunk = self._ser.read(n)
            except Exception:
                break
            if not chunk:
                continue
            self._buf += chunk.decode("ascii", errors="replace")
            # ファームは '\n' / '\r' のどちらでも行終端として扱う
            while True:
                idx = min((i for i in (self._buf.find("\n"), self._buf.find("\r")) if i >= 0),
                          default=-1)
                if idx < 0:
                    break
                line, self._buf = self._buf[:idx], self._buf[idx + 1:]
                line = line.strip()
                if line:
                    self._dispatch(line)
            if len(self._buf) > 4096:
                self._buf = ""   # 暴走ガード

    def _dispatch(self, text: str) -> None:
        kind = classify_line(text)
        if kind == "telemetry":
            if self._on_telemetry:
                sample = parse_telemetry(text)
                if sample is not None:
                    self._on_telemetry(sample)
            return

        with self._state_lock:
            pending = self._pending
        if pending is None:
            # 起動バナーなど、コマンドを待っていないときに届いた行
            if self._on_unsolicited:
                self._on_unsolicited(text)
            return

        if kind == "ack_ok":
            pending.ack, pending.ok = text, True
            pending.event.set()
        elif kind == "ack_err":
            pending.ack, pending.ok = text, False
            pending.event.set()
        else:
            if len(pending.body) < 128:
                pending.body.append(text)

    # -- 送信 ---------------------------------------------------------------
    def command(self, line: str, timeout: Optional[float] = None) -> Response:
        """1行送ってOK/ERRが返るまで待つ。ERRなら MapConsoleError を投げる。"""
        if not line or not line.strip():
            raise MapConsoleError("空行は送信できません（ファームが応答を返しません）")
        if len(line) >= 95:
            raise MapConsoleError("1行は95バイト未満にしてください（ERR LINE_TOO_LONG になります）")
        if not line.isascii():
            # ファームはASCII前提。そのまま送ると化けた1行として解釈され、
            # セッション中なら ERR BAD_CSV_LINE でセッションごと破棄される。
            raise MapConsoleError(f"ASCII以外の文字は送信できません: {line!r}")
        if timeout is None:
            timeout = SLOW_TIMEOUT if line.upper().startswith(SLOW_COMMANDS) else ACK_TIMEOUT

        with self._send_lock:
            pending = _Pending(line)
            with self._state_lock:
                self._pending = pending
            try:
                self._ser.write((line + "\n").encode("ascii"))   # 終端はLFのみ
                self._ser.flush()
                if not pending.event.wait(timeout):
                    raise MapConsoleTimeout(f"{line!r} への応答がありません（タイムアウト）")
            finally:
                with self._state_lock:
                    self._pending = None

        if not pending.ok:
            raise MapConsoleError(f"{line!r} -> {pending.ack}")
        return Response(ack=pending.ack, body=pending.body)

    def close(self) -> None:
        self._stop.set()
        self._reader.join(timeout=1.0)

    # -- 高レベル操作 --------------------------------------------------------
    def ping(self) -> float:
        """往復レイテンシを秒で返す。"""
        t0 = time.monotonic()
        self.command("PING")
        return time.monotonic() - t0

    def version(self) -> dict:
        """ファーム版数とプロトコル版数。旧ファームでは proto=1 を返す。"""
        try:
            res = self.command("VER")
        except MapConsoleError:
            return {"fw": "unknown", "proto": 1}
        # "OK VER 1.1.0 proto=2"
        parts = res.code.split()
        out = {"fw": parts[1] if len(parts) > 1 else "unknown", "proto": 1}
        for p in parts:
            if p.startswith("proto="):
                out["proto"] = int(p.split("=", 1)[1])
        return out

    def info(self) -> dict:
        """MAP INFO の内容を dict で返す。"""
        res = self.command("MAP INFO")
        out: dict = {}
        for line in res.body:
            if not line.startswith("INFO "):
                continue
            for token in line[5:].split():
                if "=" not in token:
                    continue
                k, v = token.split("=", 1)
                out[k] = v
        if "rows" in out:
            out["rows"] = int(out["rows"])
        if "rpm" in out:
            out["rpm"] = int(out["rpm"])
        if "crc" in out:
            out["crc"] = int(out["crc"], 16)   # ゼロ埋め無しの大文字HEX
        return out

    def dump_map(self) -> list:
        """現在のMAPを (rpm, inj, ign) のリストとして取得する。"""
        res = self.command("MAP?")
        rows = []
        for line in res.body:
            if not line or not line[0].isdigit():
                continue   # ヘッダ行
            parts = [c.strip() for c in line.split(",")]
            if len(parts) < 3:
                continue
            rows.append((int(parts[0]), int(parts[1]), int(parts[2])))
        return rows

    def set_entry(self, rpm: int, inj: int, ign: int) -> Response:
        """1行だけライブ変更する。セッション不要なのでテレメトリが途切れない。

        注意: 該当RPMの行が無い場合は昇順を保つ位置へ「挿入」される。
        稼働中に意図せず行が増えないよう、呼び出し側でRPMの一致を確認すること。
        """
        return self.command(f"MAP SET {rpm} {inj} {ign}")

    def telemetry(self, on: bool, period_ms: int = 100) -> Response:
        return self.command(f"TELEM ON {period_ms}" if on else "TELEM OFF")

    def save(self) -> Response:
        """EEPROMへ保存する。エンジン停止時のみ受理される。

        ベンチではノイズで tachoRpm が一瞬非ゼロになり ERR ENGINE_RUNNING が
        返ることがあるため、数回リトライする。
        """
        last: Optional[MapConsoleError] = None
        for attempt in range(SAVE_RETRIES):
            try:
                return self.command("MAP SAVE")
            except MapConsoleError as e:
                if "ENGINE_RUNNING" not in str(e):
                    raise
                last = e
                if attempt < SAVE_RETRIES - 1:
                    time.sleep(SAVE_RETRY_WAIT)
        raise last   # type: ignore[misc]

    def transfer(self, rows: Sequence[Row], verify: bool = True) -> None:
        """MAP BEGIN 〜 END で一括転送する。失敗時は必ず ABORT して現行MAPを残す。

        転送中はファーム側がテレメトリをミュートする点に注意（数百ms〜1秒）。
        RPMブレークポイントが変わらないなら set_entry() による差分転送の方が、
        テレメトリが途切れずエンジンを止める必要も無い。
        """
        err = validate(rows)
        if err:
            raise MapConsoleError(f"転送前の検証に失敗しました: {err}")

        self.command("MAP BEGIN")
        try:
            for rpm, inj, ign in rows:
                self.command(f"{rpm},{inj},{ign}")
            self.command("MAP END")
        except MapConsoleError:
            try:
                self.command("MAP ABORT")
            except MapConsoleError:
                pass
            raise

        if verify:
            # CRCで突き合わせる。全行読み戻すより速く、実機の内部表現と直接比較できる。
            expect = crc16(list(rows))
            actual = self.info().get("crc")
            if actual != expect:
                raise MapConsoleError(
                    f"転送後の検証に失敗しました（CRC不一致: 期待 0x{expect:04X} / "
                    f"実機 0x{actual:04X}）" if actual is not None else
                    "転送後の検証に失敗しました（実機のCRCを取得できません）")


def open_console(port: Optional[str] = None, **kwargs) -> "tuple":
    """シリアルを開いて MapConsole を返す。戻り値は (console, serial)。"""
    import serial

    device = port or find_port()
    ser = serial.Serial(device, BAUD, timeout=0.2)
    time.sleep(0.3)          # CDCの立ち上がり待ち
    ser.reset_input_buffer()
    return MapConsole(ser, **kwargs), ser
