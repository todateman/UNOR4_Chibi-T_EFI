#!/usr/bin/env python3
"""map_protocol / fake_ecu のテスト。

    python3 tools/test_map_protocol.py

実機は不要。pyserial も不要（FakeSerial を使うため）。
"""

from __future__ import annotations

import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import map_protocol as mp
from fake_ecu import DEFAULT_MAP, FakeEcu, FakeSerial

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class TestPureFunctions(unittest.TestCase):
    def test_crc_matches_device(self):
        # 実機の MAP INFO が defaultMap に対して返す既知の値
        self.assertEqual(mp.crc16(DEFAULT_MAP), 0x52F4)

    def test_crc_includes_struct_padding(self):
        # MapEntry は 6 バイト（パディング1込み）。5バイトで詰めると値がズレる。
        self.assertEqual(mp.crc16([(400, 40, 15)]), mp.crc16([(400, 40, 15)]))
        self.assertNotEqual(mp.crc16([(400, 40, 15)]), mp.crc16([(400, 15, 40)]))

    def test_validate_accepts_default(self):
        self.assertIsNone(mp.validate(DEFAULT_MAP))

    def test_validate_rejects(self):
        self.assertEqual(mp.validate([]), "NO_ROWS")
        self.assertEqual(mp.validate([(0, 40, 15)]), "RPM_OUT_OF_RANGE")
        self.assertEqual(mp.validate([(20001, 40, 15)]), "RPM_OUT_OF_RANGE")
        self.assertEqual(mp.validate([(400, 40, 91)]), "IGN_CA_OUT_OF_RANGE")
        self.assertEqual(mp.validate([(400, 256, 15)]), "INJ_OUT_OF_RANGE")
        self.assertEqual(mp.validate([(400, 40, 15), (400, 40, 15)]), "RPM_NOT_ASCENDING")
        self.assertEqual(mp.validate([(400, 40, 15), (300, 40, 15)]), "RPM_NOT_ASCENDING")
        self.assertEqual(mp.validate([(i * 10 + 10, 40, 15) for i in range(25)]),
                         "TOO_MANY_ROWS")

    def test_classify_line(self):
        cases = {
            "T\t1\t2\t3\t4\t5\t6\t7\t8\t9": "telemetry",
            "1560\t4.0\t15\t0.0\t0\t0.0\t0.0\t12\t213": "telemetry",
            "OK APPLIED 15": "ack_ok",
            "OK": "ack_ok",
            "ERR BAD_CSV_LINE": "ack_err",
            "INFO src=EEPROM rows=15 crc=0x52F4": "body",
            "400,  40, 15": "body",
            "MAP SOURCE: EEPROM (15 rows)": "body",   # 起動バナーはbody扱い
            "RPM,  INJ(0.1msec), IGN(CA)": "body",
        }
        for text, want in cases.items():
            self.assertEqual(mp.classify_line(text), want, text)

    def test_parse_telemetry_new(self):
        s = mp.parse_telemetry("T\t12\t34567\t2480\t44\t20\t183\t213\t5\t9")
        self.assertIsNotNone(s)
        self.assertEqual((s.seq, s.ms, s.rpm, s.inj01, s.ign), (12, 34567, 2480, 44, 20))
        self.assertEqual((s.spd01, s.ne, s.row), (183, 213, 5))
        self.assertTrue(s.eng_on)          # bit0
        self.assertTrue(s.out_of_range)    # bit3
        self.assertFalse(s.legacy)

    def test_parse_telemetry_legacy(self):
        s = mp.parse_telemetry("1560\t4.0\t15\t18.4\t0\t0.0\t0.0\t12\t213")
        self.assertIsNotNone(s)
        self.assertEqual((s.rpm, s.inj01, s.ign, s.spd01, s.ne), (1560, 40, 15, 184, 213))
        self.assertTrue(s.legacy)
        self.assertEqual(s.row, 255)       # 旧書式は行番号を持たない

    def test_parse_telemetry_garbage(self):
        self.assertIsNone(mp.parse_telemetry("T\t1\t2"))
        self.assertIsNone(mp.parse_telemetry("a\tb\tc\td\te\tf\tg\th\ti"))

    def test_csv_roundtrip(self):
        path = os.path.join(REPO, "microSD", "RPM_2026SUZUKA.CSV")
        rows = mp.read_csv_rows(path)
        self.assertEqual(len(rows), 15)
        self.assertEqual(rows[0], (400, 40, 15))
        self.assertEqual(rows, DEFAULT_MAP)
        # 書き出して読み直しても同じ行になる
        tmp = os.path.join(REPO, ".pio", "test_roundtrip.csv")
        os.makedirs(os.path.dirname(tmp), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(mp.format_csv(rows))
        self.assertEqual(mp.read_csv_rows(tmp), rows)
        os.remove(tmp)

    def test_existing_maps_over_ign_limit(self):
        """ign_ca > 90 の既存3ファイルが検証で弾かれることを固定する。"""
        bad = ["RPM_2025SUZUKA.CSV", "RPM_2025MOTEGI.csv", "RPM_2024MOTEGI.CSV"]
        for name in bad:
            rows = mp.read_csv_rows(os.path.join(REPO, "microSD", name))
            self.assertEqual(mp.validate(rows), "IGN_CA_OUT_OF_RANGE", name)
        for name in ["RPM.CSV", "RPM_2026SUZUKA.CSV", "RPM_2024SUZUKA.csv"]:
            rows = mp.read_csv_rows(os.path.join(REPO, "microSD", name))
            self.assertIsNone(mp.validate(rows), name)


class ConsoleTestCase(unittest.TestCase):
    def setUp(self):
        self.ecu = FakeEcu(rpm_profile=False)
        self.ser = FakeSerial(self.ecu)
        self.telemetry = []
        self.console = mp.MapConsole(self.ser, on_telemetry=self.telemetry.append)

    def tearDown(self):
        self.console.close()


class TestConsole(ConsoleTestCase):
    def test_ping_and_version(self):
        self.assertLess(self.console.ping(), 1.0)
        v = self.console.version()
        self.assertEqual(v["proto"], 2)
        self.assertEqual(v["fw"], "1.1.0")

    def test_dump_matches_default(self):
        self.assertEqual(self.console.dump_map(), list(DEFAULT_MAP))

    def test_info(self):
        info = self.console.info()
        self.assertEqual(info["rows"], 15)
        self.assertEqual(info["crc"], 0x52F4)
        self.assertEqual(info["src"], "DEFAULT")
        self.assertEqual(info["eng"], "OFF")

    def test_transfer_and_verify(self):
        rows = [(r, i + 1, g) for r, i, g in DEFAULT_MAP]
        self.console.transfer(rows)
        self.assertEqual(self.console.dump_map(), rows)
        self.assertEqual(self.console.info()["crc"], mp.crc16(rows))

    def test_transfer_rejects_invalid_before_sending(self):
        bad = [(400, 40, 200)]
        with self.assertRaises(mp.MapConsoleError):
            self.console.transfer(bad)
        # 現行MAPは無傷
        self.assertEqual(self.console.dump_map(), list(DEFAULT_MAP))

    def test_failed_transfer_leaves_map_intact(self):
        """検証NGならファーム側でも全体が破棄され、現行MAPは無傷であること。"""
        self.console.command("MAP BEGIN")
        self.console.command("400,40,15")
        self.console.command("300,40,15")          # 昇順違反だが受理される
        with self.assertRaises(mp.MapConsoleError) as cm:
            self.console.command("MAP END")
        self.assertIn("RPM_NOT_ASCENDING", str(cm.exception))
        self.assertEqual(self.console.dump_map(), list(DEFAULT_MAP))

    def test_header_line_is_skipped(self):
        self.console.command("MAP BEGIN")
        res = self.console.command("RPM,  INJ(0.1msec), IGN(CA)")
        self.assertEqual(res.code, "SKIP")
        self.console.command("MAP ABORT")

    def test_bad_csv_line_aborts_session(self):
        self.console.command("MAP BEGIN")
        with self.assertRaises(mp.MapConsoleError):
            self.console.command("not a number")
        # セッションが落ちているので次の行は UNKNOWN_COMMAND になる
        with self.assertRaises(mp.MapConsoleError) as cm:
            self.console.command("400,40,15")
        self.assertIn("UNKNOWN_COMMAND", str(cm.exception))

    def test_empty_line_is_refused_by_client(self):
        """空行はファームが応答を返さないので、送る前にクライアント側で弾く。"""
        with self.assertRaises(mp.MapConsoleError):
            self.console.command("")
        with self.assertRaises(mp.MapConsoleError):
            self.console.command("   ")

    def test_long_line_is_refused_by_client(self):
        with self.assertRaises(mp.MapConsoleError):
            self.console.command("MAP SET " + "9" * 100)

    def test_non_ascii_is_refused_by_client(self):
        """非ASCIIはファームで化けた1行になるので、送る前に弾く。"""
        with self.assertRaises(mp.MapConsoleError) as cm:
            self.console.command("MAP SET 2000 40 20 　")
        self.assertIn("ASCII", str(cm.exception))

    def test_set_entry_updates_existing_row(self):
        self.console.set_entry(2000, 50, 22)
        rows = self.console.dump_map()
        self.assertEqual(len(rows), 15)               # 行数は変わらない
        self.assertEqual(rows[4], (2000, 50, 22))

    def test_set_entry_inserts_when_rpm_absent(self):
        """該当RPMが無いと行が挿入される。GUIが稼働中にRPM列を固定する根拠。"""
        self.console.set_entry(2200, 50, 22)
        rows = self.console.dump_map()
        self.assertEqual(len(rows), 16)
        self.assertEqual(rows[5], (2200, 50, 22))
        self.assertEqual([r[0] for r in rows], sorted(r[0] for r in rows))

    def test_set_entry_rejects_out_of_range(self):
        with self.assertRaises(mp.MapConsoleError) as cm:
            self.console.set_entry(2000, 40, 91)
        self.assertIn("SET_REJECTED", str(cm.exception))

    def test_save_requires_engine_stopped(self):
        self.ecu.engine_running = True
        with self.assertRaises(mp.MapConsoleError) as cm:
            self.console.save()
        self.assertIn("ENGINE_RUNNING", str(cm.exception))

    def test_save_retries_then_succeeds(self):
        """ノイズで一瞬 ENGINE_RUNNING が返ってもリトライで通ること。"""
        self.ecu.engine_running = True
        state = {"n": 0}
        original = self.ecu._cmd_save

        def flaky():
            state["n"] += 1
            if state["n"] == 1:
                return original()          # 1回目は ENGINE_RUNNING
            self.ecu.engine_running = False
            return original()

        self.ecu._cmd_save = flaky
        res = self.console.save()
        self.assertEqual(res.code, "SAVED")
        self.assertEqual(state["n"], 2)

    def test_save_unchanged_skips_write(self):
        self.assertEqual(self.console.save().code, "SAVED")
        self.assertEqual(self.console.save().code, "UNCHANGED")

    def test_load_from_empty_eeprom(self):
        with self.assertRaises(mp.MapConsoleError) as cm:
            self.console.command("MAP LOAD")
        self.assertIn("EEPROM_EMPTY", str(cm.exception))

    def test_unknown_commands(self):
        for line, want in [("FOO", "UNKNOWN_COMMAND"),
                           ("MAP FOO", "UNKNOWN_MAP_SUBCOMMAND"),
                           ("TELEM FOO", "UNKNOWN_TELEM_SUBCOMMAND")]:
            with self.assertRaises(mp.MapConsoleError) as cm:
                self.console.command(line)
            self.assertIn(want, str(cm.exception), line)

    def test_case_insensitive(self):
        self.assertEqual(self.console.command("map info").code, "INFO")
        self.assertEqual(self.console.command("ping").code, "PONG")


class TestTelemetryStream(ConsoleTestCase):
    def test_telemetry_flows_and_is_separated_from_acks(self):
        """テレメトリが流れている最中でもコマンド応答が正しくマッチすること。"""
        self.ecu.engine_running = True
        self.ecu._rpm_profile = True
        self.assertEqual(self.console.telemetry(True, 100).code, "TELEM ON 100")

        # テレメトリが流れる中で何度もコマンドを往復させる
        for _ in range(5):
            time.sleep(0.12)
            self.assertEqual(self.console.info()["rows"], 15)
            self.assertEqual(self.console.command("PING").code, "PONG")

        self.assertGreater(len(self.telemetry), 0)
        self.assertTrue(all(s.seq > 0 for s in self.telemetry))
        # seq が単調増加している = 応答とテレメトリが取り違えられていない
        seqs = [s.seq for s in self.telemetry]
        self.assertEqual(seqs, sorted(seqs))

        self.assertEqual(self.console.telemetry(False).code, "TELEM OFF")

    def test_telemetry_muted_during_session(self):
        self.ecu.engine_running = True
        self.console.telemetry(True, 100)
        time.sleep(0.15)
        self.console.command("MAP BEGIN")
        before = len(self.telemetry)
        time.sleep(0.35)
        self.assertEqual(len(self.telemetry), before, "セッション中はミュートされるはず")
        self.console.command("MAP ABORT")
        time.sleep(0.25)
        self.assertGreater(len(self.telemetry), before, "END/ABORT後は再開するはず")

    def test_telemetry_rate_is_clamped(self):
        self.assertEqual(self.console.command("TELEM ON 10").code, "TELEM ON 100")
        self.assertEqual(self.console.command("TELEM ON 99999").code, "TELEM ON 2000")
        self.assertEqual(self.console.command("TELEM ON 500").code, "TELEM ON 500")

    def test_telemetry_query(self):
        self.console.telemetry(True, 200)
        res = self.console.command("TELEM?")
        self.assertTrue(any("on=1" in b and "ms=200" in b for b in res.body), res.body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
