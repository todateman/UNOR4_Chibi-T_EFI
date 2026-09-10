#!/usr/bin/env python3
"""ビルドせずにエンジンMAPを書き換えるための転送スクリプト。

USBシリアル(CDC)経由で ECU の MAP コンソールに CSV を流し込む。
CSV は microSD/RPM_*.CSV と同じ書式（rpm, inj_time(x0.1msec), ign_ca）。

必要なもの:
    pip install pyserial

使い方:
    # 転送のみ（RAMへ反映。電源を切ると元に戻る）
    python tools/send_map.py microSD/RPM_2026SUZUKA.CSV

    # 転送 + EEPROMへ保存（エンジン停止時のみ）
    python tools/send_map.py microSD/RPM_2026SUZUKA.CSV --save

    # 現在のMAPを吸い出す
    python tools/send_map.py --dump > current_map.csv

    # ポートを明示する
    python tools/send_map.py map.csv --port /dev/cu.usbmodem1101
"""

import argparse
import sys
import time

try:
    import serial
    from serial.tools import list_ports
except ImportError:
    sys.exit("pyserial が必要です:  pip install pyserial")

BAUD = 115200
# 応答待ちタイムアウト。MAP SAVE はデータフラッシュの消去+書き込みを含むので長めに取る。
ACK_TIMEOUT = 3.0
SAVE_TIMEOUT = 10.0

# Arduino UNO R4 Minima (Renesas RA4M1) の VID
ARDUINO_VIDS = (0x2341, 0x2A03)


class MapConsoleError(RuntimeError):
    pass


def find_port() -> str:
    candidates = [p for p in list_ports.comports() if p.vid in ARDUINO_VIDS]
    if not candidates:
        # VIDで見つからない場合は usbmodem / ttyACM を拾う
        candidates = [
            p for p in list_ports.comports()
            if "usbmodem" in p.device or "ttyACM" in p.device
        ]
    if not candidates:
        raise MapConsoleError(
            "シリアルポートが見つかりません。--port で明示してください。")
    if len(candidates) > 1:
        names = ", ".join(p.device for p in candidates)
        raise MapConsoleError(
            f"候補が複数あります ({names})。--port で明示してください。")
    return candidates[0].device


def send_line(ser: serial.Serial, line: str, timeout: float = ACK_TIMEOUT):
    """1行送って OK/ERR の応答行が返るまで待つ。

    応答前に出てくる行（MAP? のCSV本体や INFO 行）は body として返す。
    テレメトリ行はタブ区切りなので、それらは読み飛ばす。
    """
    ser.reset_input_buffer()
    ser.write((line + "\n").encode("ascii"))
    ser.flush()

    body = []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        raw = ser.readline()
        if not raw:
            continue
        text = raw.decode("ascii", errors="replace").strip()
        if not text:
            continue
        if text.startswith("OK"):
            return text, body
        if text.startswith("ERR"):
            raise MapConsoleError(f"{line!r} -> {text}")
        if "\t" in text:
            continue  # 500ms周期のテレメトリ行
        body.append(text)
    raise MapConsoleError(f"{line!r} への応答がありません（タイムアウト）")


def read_csv_rows(path: str):
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
                rpm, inj, ign = (int(parts[0]), int(parts[1]), int(parts[2]))
            except ValueError:
                raise MapConsoleError(f"{path}:{lineno} 数値ではありません: {line}")
            rows.append((rpm, inj, ign))
    if not rows:
        raise MapConsoleError(f"{path} に有効な行がありません")
    return rows


def dump_map(ser: serial.Serial):
    """現在のMAPを (rpm, inj, ign) のリストとして取得する。"""
    ack, body = send_line(ser, "MAP?")
    rows = []
    for line in body:
        if not line or not line[0].isdigit():
            continue
        parts = [c.strip() for c in line.split(",")]
        if len(parts) < 3:
            continue
        rows.append((int(parts[0]), int(parts[1]), int(parts[2])))
    return rows


def transfer(ser: serial.Serial, rows, save: bool):
    print(f"転送中: {len(rows)} 行")
    send_line(ser, "MAP BEGIN")
    try:
        for rpm, inj, ign in rows:
            send_line(ser, f"{rpm},{inj},{ign}")
        send_line(ser, "MAP END")
    except MapConsoleError:
        try:
            send_line(ser, "MAP ABORT")
        except MapConsoleError:
            pass
        raise

    written = dump_map(ser)
    if written != rows:
        raise MapConsoleError(
            "読み戻し検証に失敗しました。\n"
            f"  送信: {rows}\n  実機: {written}")
    print("RAMへ反映しました（読み戻し検証OK）")

    if save:
        ack, _ = send_line(ser, "MAP SAVE", timeout=SAVE_TIMEOUT)
        if "UNCHANGED" in ack:
            # 保存済み内容と同一。データフラッシュの摩耗を避けるため書き込まれていない。
            print("EEPROMの内容は同一のため書き込みませんでした")
        else:
            print("EEPROMへ保存しました")
    else:
        print("EEPROMには保存していません（--save で永続化）")


def main() -> int:
    ap = argparse.ArgumentParser(
        description="USBシリアル経由でエンジンMAPを転送・取得する")
    ap.add_argument("csv", nargs="?", help="転送するCSVファイル")
    ap.add_argument("--port", help="シリアルポート（省略時は自動検出）")
    ap.add_argument("--save", action="store_true",
                    help="転送後にEEPROMへ保存する（エンジン停止時のみ）")
    ap.add_argument("--dump", action="store_true",
                    help="現在のMAPをCSVとして標準出力に書き出す")
    ap.add_argument("--info", action="store_true",
                    help="MAPの出所・行数・CRC・EEPROM状態を表示する")
    args = ap.parse_args()

    if not args.dump and not args.info and not args.csv:
        ap.error("CSVファイルを指定するか --dump / --info を使ってください")

    try:
        port = args.port or find_port()
        rows = read_csv_rows(args.csv) if args.csv else None

        with serial.Serial(port, BAUD, timeout=0.3) as ser:
            time.sleep(0.3)          # CDCの立ち上がり待ち
            ser.reset_input_buffer()

            if args.info:
                ack, body = send_line(ser, "MAP INFO")
                for line in body:
                    print(line)

            if args.dump:
                print("RPM,  INJ(0.1msec), IGN(CA)")
                for rpm, inj, ign in dump_map(ser):
                    print(f"{rpm},{inj},{ign}")

            if rows is not None:
                transfer(ser, rows, args.save)

    except MapConsoleError as e:
        print(f"エラー: {e}", file=sys.stderr)
        return 1
    except serial.SerialException as e:
        print(f"シリアルポートエラー: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
