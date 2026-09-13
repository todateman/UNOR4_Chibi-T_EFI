#!/usr/bin/env python3
"""ビルドせずにエンジンMAPを書き換えるための転送スクリプト。

USBシリアル(CDC)経由で ECU の MAP コンソールに CSV を流し込む。
CSV は microSD/RPM_*.CSV と同じ書式（rpm, inj_time(x0.1msec), ign_ca）。

プロトコルの実装は tools/map_protocol.py にあり、Web GUI（tools/map_gui.py）と
共有している。

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

    # 実機なしで動作確認する（モックECU）
    python tools/send_map.py microSD/RPM_2026SUZUKA.CSV --fake
"""

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import map_protocol as mp
from map_protocol import MapConsole, MapConsoleError


def open_link(args):
    """(console, closer) を返す。--fake なら実機の代わりにモックECUへ繋ぐ。"""
    if args.fake:
        from fake_ecu import FakeSerial
        ser = FakeSerial()
    else:
        try:
            import serial
        except ImportError:
            sys.exit("pyserial が必要です:  pip install pyserial")
        port = args.port or mp.find_port()
        ser = serial.Serial(port, mp.BAUD, timeout=0.2)
        time.sleep(0.3)          # CDCの立ち上がり待ち
        ser.reset_input_buffer()

    console = MapConsole(ser)

    def closer():
        console.close()
        ser.close()

    return console, closer


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
    ap.add_argument("--fake", action="store_true",
                    help="実機の代わりにモックECUへ繋ぐ（動作確認用）")
    args = ap.parse_args()

    if not args.dump and not args.info and not args.csv:
        ap.error("CSVファイルを指定するか --dump / --info を使ってください")

    closer = None
    try:
        rows = mp.read_csv_rows(args.csv) if args.csv else None
        if rows is not None:
            # ファームと同じ規則で先に検証しておき、途中で弾かれるのを避ける
            err = mp.validate(rows)
            if err:
                raise MapConsoleError(f"{args.csv} は転送できません: {err}")

        console, closer = open_link(args)

        if args.info:
            for line in console.command("MAP INFO").body:
                print(line)

        if args.dump:
            print(mp.format_csv(console.dump_map()), end="")

        if rows is not None:
            print(f"転送中: {len(rows)} 行")
            console.transfer(rows)
            print(f"RAMへ反映しました（CRC検証OK: 0x{mp.crc16(rows):04X}）")

            if args.save:
                ack = console.save()
                if "UNCHANGED" in ack.code:
                    # 保存済み内容と同一。データフラッシュの摩耗を避けるため未書き込み。
                    print("EEPROMの内容は同一のため書き込みませんでした")
                else:
                    print("EEPROMへ保存しました")
            else:
                print("EEPROMには保存していません（--save で永続化）")

    except MapConsoleError as e:
        print(f"エラー: {e}", file=sys.stderr)
        return 1
    except Exception as e:                      # serial.SerialException など
        if e.__class__.__name__ == "SerialException":
            print(f"シリアルポートエラー: {e}", file=sys.stderr)
            return 1
        raise
    finally:
        if closer:
            closer()
    return 0


if __name__ == "__main__":
    sys.exit(main())
