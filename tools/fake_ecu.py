#!/usr/bin/env python3
"""実機なしで GUI と CLI を動かすためのモック ECU。

src/map_console.cpp / src/map_store.cpp の挙動を再現する。pty や com0com を
使わず pyserial と同じインタフェースのオブジェクトを提供するだけなので、
Mac でも Windows でも動く。

再現している「引っかかりどころ」:
- 空行には応答を返さない
- コマンドは大文字小文字を無視する
- 行が96バイトを超えると ERR LINE_TOO_LONG でセッションも破棄される
- ヘッダ行("RPM"始まり)は OK SKIP、それ以外の非数字始まりは ERR BAD_CSV_LINE
- 検証NGなら全体を破棄し、現行MAPは無傷
- 応答の終端は CRLF
- MAP SAVE はエンジン停止時のみ受理
"""

from __future__ import annotations

import math
import threading
import time
from typing import List, Optional

from map_protocol import (
    CSV_HEADER,
    MAP_IGN_CA_MAX,
    MAP_INJ_END_CA_MAX,
    MAP_MAX_ENTRIES,
    MAP_RPM_MAX,
    crc16,
)

FW_VERSION = "1.2.0"
PROTO_VERSION = 3
LINE_MAX = 96
TELEM_BASE_MS = 100

# src/map_store.cpp の defaultMap[]（rpm, inj_time, ign_ca, inj_end_ca）
DEFAULT_MAP = [
    (400, 0, 0, 0),        # 400RPM以下はアイドリング不能なのでエンジン停止
    (800, 80, 0, 20),      # 800RPM以下は始動状態
    (1200, 80, 0, 20),     # 1200RPM以下は始動状態
    (1600, 40, 15, 680),   # 以降は通常走行域
    (2000, 44, 20, 680), (2400, 44, 20, 680), (2800, 44, 25, 680),
    (3200, 42, 25, 680), (3600, 40, 25, 680), (4000, 40, 30, 680),
    (4400, 40, 30, 680), (4800, 40, 30, 680), (5200, 40, 30, 680),
    (5600, 40, 30, 680), (6000, 40, 30, 680),
]

TACHO_RPM_MAX = 6000   # main.cpp のレブリミット


def _validate_table(rows) -> Optional[str]:
    """src/map_store.cpp の validateTable()。inj_time は検査しない点まで同じ。"""
    if not rows:
        return "NO_ROWS"
    if len(rows) > MAP_MAX_ENTRIES:
        return "TOO_MANY_ROWS"
    prev = None
    for rpm, _inj, ign, inj_end_ca in rows:
        if rpm == 0 or rpm > MAP_RPM_MAX:
            return "RPM_OUT_OF_RANGE"
        if ign > MAP_IGN_CA_MAX:
            return "IGN_CA_OUT_OF_RANGE"
        if inj_end_ca > MAP_INJ_END_CA_MAX:
            return "INJ_END_CA_OUT_OF_RANGE"
        if prev is not None and rpm <= prev:
            return "RPM_NOT_ASCENDING"
        prev = rpm
    return None


def _parse_csv_line(line: str):
    """src/map_store.cpp の mapParseCsvLine()。戻り値は (ok, skip, row)。"""
    p = line.lstrip(" \t")
    if p == "" or p[0] in "#;":
        return True, True, None
    if p[:3].upper() == "RPM":
        return True, True, None
    if not p[0].isdigit():
        return False, False, None

    vals = []
    i = 0
    for col in range(4):
        while i < len(p) and p[i] in " \t":
            i += 1
        if i >= len(p) or not p[i].isdigit():
            return False, False, None
        v = 0
        while i < len(p) and p[i].isdigit():
            v = v * 10 + int(p[i])
            if v > 999999:
                return False, False, None
            i += 1
        vals.append(v)
        while i < len(p) and p[i] in " \t":
            i += 1
        if col < 3:
            if i >= len(p) or p[i] != ",":
                return False, False, None
            i += 1

    while i < len(p) and p[i] in " \t,":
        i += 1
    if i != len(p):
        return False, False, None
    if vals[0] > 65535 or vals[1] > 255 or vals[2] > 65535 or vals[3] > 65535:
        return False, False, None
    return True, False, tuple(vals)


class FakeEcu:
    """MAPコンソールの応答とテレメトリを生成する。スレッドセーフ。"""

    def __init__(self, rows=None, eeprom=None, engine_running: bool = False,
                 rpm_profile: bool = True):
        self._lock = threading.RLock()
        self.rows: List[tuple] = list(rows or DEFAULT_MAP)
        self.eeprom: Optional[List[tuple]] = list(eeprom) if eeprom else None
        self.source = "EEPROM" if eeprom else "DEFAULT"
        self.engine_running = engine_running
        self._rpm_profile = rpm_profile

        self._staging: List[tuple] = []
        self._session = False
        self._session_last = 0.0
        self._telem_on = False
        self._telem_div = 1
        self._telem_drop = 0
        self._seq = 0

        self._in = ""
        self._overflow = False
        self._out = bytearray()
        self._t0 = time.monotonic()
        self._next_telem = 0.0

    # -- テレメトリ ---------------------------------------------------------
    def _rpm_now(self) -> int:
        """アイドル→加速→巡航→減速 を繰り返す回転数プロファイル。"""
        if not self._rpm_profile:
            return 0
        if not self.engine_running:
            return 0
        t = (time.monotonic() - self._t0) % 40.0
        if t < 8:
            return int(1200 + 80 * math.sin(t * 3))          # アイドル
        if t < 18:
            return int(1200 + (5200 - 1200) * (t - 8) / 10)  # 加速
        if t < 30:
            return int(5200 + 200 * math.sin((t - 18) * 2))  # 巡航
        return int(5200 - (5200 - 1200) * (t - 30) / 10)     # 減速

    def _active_row(self, rpm: int) -> int:
        for i, (r, _i, _g, _e) in enumerate(self.rows):
            if rpm < r:
                return i
        return 255

    def _emit_telemetry(self) -> None:
        rpm = self._rpm_now()
        row = self._active_row(rpm)
        out_of_range = row == 255 and self.engine_running
        inj, ign = (self.rows[row][1], self.rows[row][2]) if row != 255 else (0, 0)
        flags = (0x01 if self.engine_running else 0) | (0x08 if out_of_range else 0)
        self._seq = (self._seq + 1) & 0xFFFF
        ms = int((time.monotonic() - self._t0) * 1000)
        ne = int((time.monotonic() * 360) % 720)
        self._write(f"T\t{self._seq}\t{ms}\t{rpm}\t{inj}\t{ign}\t0\t{ne}\t{row}\t{flags}\n")

    def pump(self) -> None:
        """時間経過に応じてテレメトリを生成する。read() から呼ばれる。"""
        with self._lock:
            now = time.monotonic()
            if self._session and now - self._session_last > 5.0:
                self._staging.clear()
                self._session = False
                self._write("ERR SESSION_TIMEOUT\r\n")
            if not self._telem_on or self._session:
                return
            period = self._telem_div * TELEM_BASE_MS / 1000.0
            if self._next_telem == 0.0:
                self._next_telem = now
            while now >= self._next_telem:
                self._emit_telemetry()
                self._next_telem += period

    # -- 入出力 -------------------------------------------------------------
    def _write(self, text: str) -> None:
        self._out.extend(text.encode("ascii"))

    def _ok(self, msg: str) -> None:
        self._write(f"OK {msg}\r\n")

    def _err(self, msg: str) -> None:
        self._write(f"ERR {msg}\r\n")

    def feed(self, data: bytes) -> None:
        with self._lock:
            for ch in data.decode("ascii", errors="replace"):
                if ch in "\r\n":
                    if self._overflow:
                        self._in = ""
                        self._overflow = False
                        self._staging.clear()
                        self._session = False
                        self._err("LINE_TOO_LONG")
                        continue
                    line, self._in = self._in, ""
                    if line:                       # 空行には応答を返さない
                        self._process(line)
                        if self._session:
                            self._session_last = time.monotonic()
                    continue
                if len(self._in) < LINE_MAX - 1:
                    self._in += ch
                else:
                    self._overflow = True

    def take_output(self, n: int = -1) -> bytes:
        with self._lock:
            if n < 0 or n >= len(self._out):
                data, self._out = bytes(self._out), bytearray()
            else:
                data, self._out = bytes(self._out[:n]), self._out[n:]
            return data

    @property
    def pending(self) -> int:
        with self._lock:
            return len(self._out)

    # -- コマンド -----------------------------------------------------------
    def _process(self, line: str) -> None:
        p = line.lstrip(" \t")
        if not p:
            return
        up = p.upper()

        if up == "MAP" or up == "MAP?":
            return self._cmd_dump()
        if up.startswith("MAP ") or up.startswith("MAP\t"):
            return self._cmd_map(p[3:].lstrip(" \t"))
        if up == "HELP":
            return self._cmd_help()
        if up == "TELEM" or up == "TELEM?":
            return self._cmd_telem_query()
        if up.startswith("TELEM ") or up.startswith("TELEM\t"):
            return self._cmd_telem(p[5:].lstrip(" \t"))
        if up == "VER":
            return self._write(f"OK VER {FW_VERSION} proto={PROTO_VERSION}\r\n")
        if up == "PING":
            return self._ok("PONG")

        if self._session:
            return self._csv_row(p)
        self._err("UNKNOWN_COMMAND")

    def _cmd_map(self, arg: str) -> None:
        up = arg.upper()
        if up == "BEGIN":
            self._staging.clear()
            self._session = True
            self._session_last = time.monotonic()
            return self._ok("BEGIN")
        if up == "END":
            return self._cmd_end()
        if up == "ABORT":
            self._staging.clear()
            self._session = False
            return self._ok("ABORTED")
        if up == "INFO":
            return self._cmd_info()
        if up == "SAVE":
            return self._cmd_save()
        if up == "LOAD":
            if self.eeprom is None:
                return self._err("EEPROM_EMPTY")
            self.rows = list(self.eeprom)
            self.source = "EEPROM"
            return self._ok("LOADED")
        if up == "DEFAULT":
            self.rows = list(DEFAULT_MAP)
            self.source = "DEFAULT"
            return self._ok("DEFAULT")
        if up.startswith("SET"):
            return self._cmd_set(arg[3:])
        self._err("UNKNOWN_MAP_SUBCOMMAND")

    def _cmd_dump(self) -> None:
        self._write(CSV_HEADER + "\r\n")
        for rpm, inj, ign, inj_end_ca in self.rows:
            self._write(f"{rpm},{inj},{ign},{inj_end_ca}\r\n")
        self._ok(f"ROWS {len(self.rows)}")

    def _cmd_info(self) -> None:
        rpm = self._rpm_now()
        self._write(
            f"INFO src={self.source} rows={len(self.rows)} "
            f"crc=0x{crc16(self.rows):X} "
            f"eeprom={'VALID' if self.eeprom else 'EMPTY'} "
            f"rpm={rpm} eng={'ON' if self.engine_running else 'OFF'}\r\n")
        self._ok("INFO")

    def _cmd_save(self) -> None:
        if self.engine_running or self._rpm_now() != 0:
            return self._err("ENGINE_RUNNING")
        if self.eeprom == self.rows:
            return self._ok("UNCHANGED")
        self.eeprom = list(self.rows)
        self.source = "EEPROM"
        self._ok("SAVED")

    def _cmd_set(self, args: str) -> None:
        parts = args.split()
        if len(parts) < 4 or not all(x.isdigit() for x in parts[:4]):
            return self._err("USAGE_MAP_SET_RPM_INJ_IGN_ENDCA")
        rpm, inj, ign, inj_end_ca = (int(x) for x in parts[:4])
        if rpm > 65535 or inj > 255 or ign > 65535 or inj_end_ca > 65535:
            return self._err("VALUE_OUT_OF_RANGE")
        if (rpm == 0 or rpm > MAP_RPM_MAX or ign > MAP_IGN_CA_MAX
                or inj_end_ca > MAP_INJ_END_CA_MAX):
            return self._err("SET_REJECTED")
        for i, (r, _i, _g, _e) in enumerate(self.rows):
            if r == rpm:
                self.rows[i] = (rpm, inj, ign, inj_end_ca)
                self.source = "SERIAL"
                return self._ok("SET")
        if len(self.rows) >= MAP_MAX_ENTRIES:
            return self._err("SET_REJECTED")
        # 該当RPMが無ければ昇順を保つ位置へ挿入する（稼働中は行数が変わる点に注意）
        pos = next((i for i, r in enumerate(self.rows) if r[0] > rpm), len(self.rows))
        self.rows.insert(pos, (rpm, inj, ign, inj_end_ca))
        self.source = "SERIAL"
        self._ok("SET")

    def _csv_row(self, line: str) -> None:
        ok, skip, row = _parse_csv_line(line)
        if not ok:
            self._staging.clear()
            self._session = False
            return self._err("BAD_CSV_LINE")
        if skip:
            return self._ok("SKIP")
        if len(self._staging) >= MAP_MAX_ENTRIES:
            self._staging.clear()
            self._session = False
            return self._err("TOO_MANY_ROWS")
        self._staging.append(row)
        self._ok(f"ROW {len(self._staging)}")

    def _cmd_end(self) -> None:
        err = _validate_table(self._staging)
        if err:
            self._session = False
            return self._err(err)
        self.rows = list(self._staging)
        self.source = "SERIAL"
        self._staging = []
        self._session = False
        self._ok(f"APPLIED {len(self.rows)}")

    def _cmd_telem(self, arg: str) -> None:
        parts = arg.split()
        if parts and parts[0].upper() == "OFF":
            self._telem_on = False
            return self._ok("TELEM OFF")
        if parts and parts[0].upper() == "ON":
            if len(parts) > 1 and parts[1].isdigit():
                d = round(int(parts[1]) / TELEM_BASE_MS)
                self._telem_div = max(1, min(20, d))
            self._telem_drop = 0
            self._telem_on = True
            self._next_telem = 0.0
            return self._write(f"OK TELEM ON {self._telem_div * TELEM_BASE_MS}\r\n")
        self._err("UNKNOWN_TELEM_SUBCOMMAND")

    def _cmd_telem_query(self) -> None:
        self._write(f"TELEM on={1 if self._telem_on else 0} "
                    f"ms={self._telem_div * TELEM_BASE_MS} drop={self._telem_drop}\r\n")
        self._ok("TELEM")

    def _cmd_help(self) -> None:
        for line in (
            "MAP?                  dump current map as CSV",
            "MAP INFO              source / rows / crc / eeprom state",
            "MAP BEGIN             start CSV transfer session",
            "  <rpm>,<inj>,<ign>,<inj_end_ca>   one CSV row (header line is skipped)",
            "MAP END               validate and apply atomically",
            "MAP ABORT             discard the session",
            "MAP SET r i g e       change one row live (rpm inj ign inj_end_ca)",
            "MAP SAVE              store to EEPROM (stopped only, skip if same)",
            "MAP LOAD              reload from EEPROM",
            "MAP DEFAULT           restore built-in default map",
            "TELEM ON [ms]         start machine-readable telemetry",
            "TELEM OFF             stop it (back to the 2Hz human line)",
            "TELEM?                telemetry state",
            "VER                   firmware / protocol version",
            "PING                  connectivity check",
        ):
            self._write(line + "\r\n")
        self._ok("HELP")


class FakeSerial:
    """pyserial の serial.Serial と同じ使い方ができる FakeEcu のラッパ。

    MapConsole はこれを実機と区別せずに扱えるので、GUI も send_map.py も
    実機なしで通しで動かせる。
    """

    def __init__(self, ecu: Optional[FakeEcu] = None, **kwargs):
        self.ecu = ecu or FakeEcu(**kwargs)
        self.is_open = True
        self.port = "fake"

    @property
    def in_waiting(self) -> int:
        self.ecu.pump()
        return self.ecu.pending

    def read(self, size: int = 1) -> bytes:
        self.ecu.pump()
        data = self.ecu.take_output(size)
        if not data:
            time.sleep(0.005)   # 実機の read timeout に相当するブロック
        return data

    def write(self, data: bytes) -> int:
        self.ecu.feed(data)
        return len(data)

    def flush(self) -> None:
        pass

    def reset_input_buffer(self) -> None:
        self.ecu.take_output()

    def close(self) -> None:
        self.is_open = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
