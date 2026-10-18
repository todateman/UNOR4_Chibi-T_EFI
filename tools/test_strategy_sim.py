#!/usr/bin/env python3
"""strategy_sim のテスト。

    python3 tools/test_strategy_sim.py

実走ログは不要（合成データと tools/strategy/ の同梱パラメータだけで走る）。
"""

from __future__ import annotations

import math
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import strategy_sim as ss

LAT0, LNG0 = 36.53, 140.23
LAP = 2400.0
N_WP = 300


def circle_latlng(s: float):
    r = LAP / (2 * math.pi)
    th = 2 * math.pi * s / LAP
    return (LAT0 + r * math.sin(th) / 111320.0,
            LNG0 + r * math.cos(th) / (111320.0 * math.cos(math.radians(LAT0))))


def write_course(dirname: str, amp: float) -> str:
    """周長 2400 m の円形コース。標高は 1 周で 1 波の正弦（振幅 amp m）。"""
    path = os.path.join(dirname, "wp.csv")
    with open(path, "w", encoding="utf-8") as f:
        f.write("ID,lat,lng,alt,distance\n")
        for i in range(N_WP):
            s = i * LAP / N_WP
            lat, lng = circle_latlng(s)
            f.write("%d,%.7f,%.7f,%.3f,%.1f\n"
                    % (i + 1, lat, lng, 100 + amp * math.sin(2 * math.pi * s / LAP), s))
    return path


class TestPureFunctions(unittest.TestCase):
    def test_inj_ml_matches_firmware(self):
        # src/main.cpp: gasml += ((dt_us * 0.0000007) + 0.0015) / 1.5073
        self.assertAlmostEqual(ss.inj_ml(8.0), ((8000 * 0.0000007) + 0.0015) / 1.5073)
        self.assertAlmostEqual(ss.inj_ml(0.0), 0.0015 / 1.5073)

    def test_interp_clamps(self):
        xs, ys = [10, 20, 30], [1.0, 3.0, 2.0]
        self.assertEqual(ss.interp(5, xs, ys), 1.0)
        self.assertEqual(ss.interp(35, xs, ys), 2.0)
        self.assertAlmostEqual(ss.interp(15, xs, ys), 2.0)
        self.assertAlmostEqual(ss.interp(25, xs, ys), 2.5)

    def test_mmss(self):
        self.assertAlmostEqual(ss.parse_mmss("38:30"), 2310.0)
        self.assertAlmostEqual(ss.parse_mmss("39:20.11"), 2360.11)
        self.assertEqual(ss.mmss(2310.0), "38:30.0")
        self.assertEqual(ss.mmss(1619.96), "27:00.0")

    def test_pace_bump(self):
        self.assertEqual(ss.pace_bump(0), 0.0)
        self.assertEqual(ss.pace_bump(9), 1.0)
        self.assertEqual(ss.pace_bump(21), 2.0)
        self.assertEqual(ss.pace_bump(60), 3.0)
        self.assertEqual(ss.pace_bump(-31), -0.5)

    def test_pick_prefers_fewer_burns_within_slack(self):
        rs = [{"time_s": 2300, "fuel_ml": 7.00, "burns": 34},
              {"time_s": 2300, "fuel_ml": 7.20, "burns": 21},
              {"time_s": 2300, "fuel_ml": 7.60, "burns": 14},
              {"time_s": 2400, "fuel_ml": 6.00, "burns": 10}]   # 目標超過
        best, rec = ss.pick(rs, 2310, fuel_slack=0.05)
        self.assertEqual(best["burns"], 34)
        self.assertEqual(rec["burns"], 21)
        self.assertEqual(ss.pick(rs, 2000), (None, None))


class TestCourse(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.hilly = ss.Course(write_course(cls.tmp.name, 1.5), LAP)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_lap_is_scaled_to_official_length(self):
        short = ss.Course(write_course(self.tmp.name, 1.5), 2341.38)
        self.assertAlmostEqual(short.s[-1] / self.hilly.s[-1], 2341.38 / LAP, places=3)
        self.assertLess(short.s[-1], 2341.38)

    def test_slope_closes_over_a_lap(self):
        # 1 周して元の標高に戻る（閉じ区間の勾配も含めて）
        self.assertAlmostEqual(sum(self.hilly.slope(i + 0.5) for i in range(int(LAP))), 0.0, places=2)

    def test_nearest(self):
        s, alt, off = self.hilly.nearest(*circle_latlng(600.0))
        self.assertAlmostEqual(s, 600.0, delta=5.0)
        self.assertAlmostEqual(alt, 101.5, delta=0.05)
        self.assertLess(off, 1.0)

    def test_coast_conserves_energy_without_resistance(self):
        s, v = 0.0, 8.0
        e0 = v * v / 2 + ss.G * self.hilly.alt_at(s)
        while s < LAP:
            s, v = ss.coast_step(self.hilly, s, v, 0.0, 0.0, 0.05)
        self.assertAlmostEqual(v * v / 2 + ss.G * self.hilly.alt_at(s), e0, delta=0.05)


class TestFitCoast(unittest.TestCase):
    def test_recovers_known_coefficients(self):
        crr, k = 0.0020, 0.00040
        with tempfile.TemporaryDirectory() as d:
            course = ss.Course(write_course(d, 1.5), LAP)
        rows, t, odo = [], 0.0, 0.0
        for start in (0.0, 500.0, 1100.0, 1700.0):
            # 加速区間（回転数あり）を挟んで惰行を 4 本
            rows.append({"t": t, "v": 36.0, "rpm": 3000.0, "dist": round(odo), "lat": 0, "lng": 0})
            t += 1.0
            s, v = start, 36.0 / 3.6
            while v * 3.6 > 15.0:
                lat, lng = circle_latlng(s % LAP)
                rows.append({"t": t, "v": round(v * 3.6, 1), "rpm": 0.0,
                             "dist": round(odo), "lat": lat, "lng": lng})
                for _ in range(4):
                    s2, v = ss.coast_step(course, s, v, crr, k, 0.05)
                    odo += s2 - s
                    s = s2
                t += 0.2
        win = ss.coast_windows(rows, course)
        self.assertGreater(len(win), 8)
        got_crr, got_k, rms = ss.solve_coast(win)
        self.assertAlmostEqual(got_crr, crr, delta=crr * 0.10)
        self.assertAlmostEqual(got_k, k, delta=k * 0.10)
        self.assertLess(rms, 1.0)


def coast_rows(course, crr, k, starts, v0=36.0, v1=15.0):
    """starts の各位置から v0 → v1 km/h まで惰行したログ行（0.2 s ごと、速度は 0.1 km/h に丸め）。"""
    rows, t, odo = [], 0.0, 0.0
    for start in starts:
        rows.append({"t": t, "v": v0, "rpm": 3000.0, "dist": round(odo), "lat": 0, "lng": 0})
        t += 1.0
        s, v = start, v0 / 3.6
        while v * 3.6 > v1:
            lat, lng = circle_latlng(s % LAP)
            rows.append({"t": t, "v": round(v * 3.6, 1), "rpm": 0.0,
                         "dist": round(odo, 1), "lat": lat, "lng": lng})
            for _ in range(4):
                s2, v = ss.coast_step(course, s, v, crr, k, 0.05)
                odo += s2 - s
                s = s2
            t += 0.2
    return rows


class TestFitGrade(unittest.TestCase):
    def test_recovers_altitude_from_coasting(self):
        with tempfile.TemporaryDirectory() as d:
            truth = ss.Course(write_course(d, 1.5), LAP)
            flat = ss.Course(write_course(d, 0.0), LAP)   # ウェイポイントの標高は平ら（間違い）
        starts = [i * 150.0 for i in range(16)]
        sets = [ss.coast_spans(coast_rows(truth, 0.0020, 0.00040, starts), flat),
                ss.coast_spans(coast_rows(truth, 0.0030, 0.00030, starts[::2]), flat)]
        fit = ss.GradeFit(flat, 50.0)
        r = fit.solve(sets, [("wp", list(zip(flat.s, flat.alt)), 5.0)])
        for s in range(0, int(LAP), 100):
            self.assertAlmostEqual(fit.alt_at(s), truth.alt_at(s), delta=0.1, msg=s)
        (v1, v2) = r["vehicles"]
        self.assertAlmostEqual(v1["crr"], 0.0020, delta=0.0002)
        self.assertAlmostEqual(v2["crr"], 0.0030, delta=0.0003)
        self.assertAlmostEqual(v1["k"], 0.00040, delta=0.00004)

    def test_altitude_prior_alone_is_followed(self):
        # 惰行が無ければ、標高の測定値（オフセットは自由）に沿う
        with tempfile.TemporaryDirectory() as d:
            flat = ss.Course(write_course(d, 0.0), LAP)
        meas = [(s, 7.0 + 1.0 * math.sin(2 * math.pi * s / LAP)) for s in range(0, int(LAP), 25)]
        fit = ss.GradeFit(flat, 50.0)
        fit.solve([], [("wp", list(zip(flat.s, flat.alt)), 10.0), ("gps", meas, 0.1)])
        for s in (0, 600, 1200, 1800):
            self.assertAlmostEqual(fit.alt_at(s), 100.0 + math.sin(2 * math.pi * s / LAP), delta=0.05)

    def test_write_waypoints_replaces_only_alt(self):
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "src.csv")
            with open(src, "wb") as f:
                f.write("﻿ID,lat,lng,alt,distance\r\n".encode("utf-8"))
                for i in range(N_WP):
                    s = i * LAP / N_WP
                    lat, lng = circle_latlng(s)
                    f.write(("%d,%.6f,%.6f,100.00,%.0f\r\n" % (i + 1, lat, lng, s)).encode())
            course = ss.Course(src, LAP)
            dst = os.path.join(d, "dst.csv")
            ss.write_waypoints(src, dst, course, lambda s: 100.0 + s / 1000.0)
            a = open(src, "rb").read().split(b"\r\n")
            b = open(dst, "rb").read().split(b"\r\n")
        self.assertTrue(b[0].startswith(b"\xef\xbb\xbf"))
        self.assertEqual(len(a), len(b))
        for x, y in zip(a[1:], b[1:]):
            if not x:
                self.assertEqual(x, y)
                continue
            xs, ys = x.split(b","), y.split(b",")
            self.assertEqual(xs[:3] + xs[4:], ys[:3] + ys[4:])
        self.assertEqual(b[101].split(b",")[3], b"100.80")


HEADERS = {
    # 2024 年（11 列）
    11: "記録日時,速度(km/h),ラップ数(周目),走行時間(秒),回転数(rpm),走行距離積算(m),"
        "積算燃料消費量(ml),燃費(km/l),lat(緯度),lng(経度),温度(C)",
    # 2026 年 鈴鹿（16 列。lon / alt / loc）
    16: "記録日時,速度(km/h),ラップ数,走行時間,回転数,走行距離,積算燃料,燃費,lat,lon,alt,loc,"
        "温度,気圧(kPa),気温(C),湿度(%)",
    # 2026 年 現行（22 列。噴射時間・燃圧あり）
    22: "記録日時,速度(km/h),ラップ数(周目),走行時間(秒),回転数(rpm),燃料噴射時間(ms),点火進角角度(CA),"
        "燃料噴射終了角度(CA),走行距離積算(m),積算燃料消費量(ml),燃費(km/l),lat(緯度),lng(経度),標高(m),"
        "場所,温度(C),気圧(kPa),気温(C),湿度(%),1次空気圧(MPa),2次空気圧(MPa),燃圧(MPa)",
}
ROWS = {
    11: "2024/10/13 13:18:44.00,18,0,460,2100,291,2.8,103.9,36.5327530,140.2263250,80.00",
    16: "2026/06/20 11:18:56.0,17.5,0,538,2400,840,0.3,2800,34.8440,136.5398,45.1,suzuka,80,100.1,28,60",
    22: "2026/09/26 14:43:14.1,29.3,0,6125.5,3469,3,34,650,2552,10.8,236.6,35.0820373,137.1603928,"
        "41.9,to,79.5,100.823,35.3,40.5,0.477,0.352,0.32",
}


class TestLoadLog(unittest.TestCase):
    def _load(self, ncol: int, extra: str = ""):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "log.csv")
            with open(path, "w", encoding="utf-8-sig") as f:
                f.write(HEADERS[ncol] + "\n" + extra + ROWS[ncol] + "\n")
            return ss.load_log(path)

    def test_11_columns(self):
        (r,) = self._load(11)
        self.assertEqual((r["v"], r["rpm"], r["dist"]), (18.0, 2100.0, 291.0))
        self.assertAlmostEqual(r["lng"], 140.226325)
        self.assertIsNone(r["inj"])
        self.assertIsNone(r["fp"])

    def test_16_columns_uses_lon(self):
        (r,) = self._load(16)
        self.assertEqual((r["v"], r["rpm"], r["dist"]), (17.5, 2400.0, 840.0))
        self.assertAlmostEqual(r["lng"], 136.5398)

    def test_22_columns(self):
        (r,) = self._load(22)
        self.assertEqual((r["v"], r["rpm"], r["dist"], r["inj"]), (29.3, 3469.0, 2552.0, 3.0))
        self.assertEqual((r["wt"], r["fp"]), (6125.5, 0.32))

    def test_rows_without_timestamp_are_skipped(self):
        # GPS 未捕捉の行は記録日時が空
        rows = self._load(11, extra=",0,0,0,0,0,0.0,nan,35.0820780,137.1603580,0.00\n")
        self.assertEqual(len(rows), 1)

    def test_missing_required_column(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "log.csv")
            with open(path, "w", encoding="utf-8") as f:
                f.write("記録日時,速度(km/h),lat,lng\n")
            with self.assertRaises(ValueError):
                ss.load_log(path)


class TestFitBurn(unittest.TestCase):
    def test_recovers_accel_decel_and_fuel(self):
        rows, wt, v = [], 100.0, 10.0
        while v < 40.0:                      # 加速 0.5 m/s2
            rows.append({"t": wt, "wt": round(wt, 1), "v": v, "rpm": 3000.0, "inj": 3.0, "fp": 0.32})
            v += 0.5 * 3.6 * 0.1
            wt += 0.1
        while v > 20.0:                      # 惰行 0.4 m/s2
            rows.append({"t": wt, "wt": round(wt, 1), "v": v, "rpm": 0.0, "inj": 0.0, "fp": 0.25})
            v -= 0.4 * 3.6 * 0.1
            wt += 0.1
        r = ss.fit_burn(rows)
        self.assertEqual(r["speed_kmh"][0], 10.0)
        for a in r["accel_drum"]:
            self.assertAlmostEqual(a, 0.5, delta=0.01)
        for f in r["fuel_ml_s"]:
            self.assertAlmostEqual(f, ss.inj_ml(3.0) * 3000 / 120, places=4)
        self.assertAlmostEqual(r["drum_decel"], 0.4, delta=0.01)


class TestSimulate(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.p = ss.load_params(ss.DEFAULT_PARAMS)
        cls.course = ss.course_from_params(cls.p)
        cls.marks = [400.0, 800.0, 1400.0]
        cls.base = ss.simulate(cls.p, cls.course, 32.5, marks=cls.marks, v_on=16.0)

    def test_bundled_course_matches_regulation(self):
        c = self.p["course"]
        self.assertAlmostEqual(self.course.lap_m * c["laps"], 16389.68, places=2)
        self.assertAlmostEqual(c["total_m"] / c["limit_s"] * 3.6, 25.0, places=3)

    def test_finishes_all_laps_within_limit(self):
        r = self.base
        self.assertEqual(len(r["crossings"]), self.p["course"]["laps"])
        self.assertLess(r["time_s"], self.p["course"]["limit_s"])
        self.assertGreater(r["finish_kmh"], self.p["course"]["finish_min_kmh"])

    def test_burns_start_at_marks_only(self):
        # 発進を除き、加速は目印の通過直後に始まる（保険の加速は入らない）
        for lap, pos, kmh in self.base["starts"][1:]:
            self.assertLess(min(abs(pos - m) for m in self.marks), 5.0, (lap, pos))
            self.assertGreater(kmh, 16.0)

    def test_last_lap_coasts_home(self):
        last = [pos for lap, pos, _ in self.base["starts"] if lap == self.p["course"]["laps"]]
        self.assertLess(len(last), len(self.marks))

    def test_higher_cut_speed_is_faster_and_thirstier(self):
        fast = ss.simulate(self.p, self.course, 33.5, marks=self.marks, v_on=16.0)
        self.assertLess(fast["time_s"], self.base["time_s"])
        self.assertGreater(fast["fuel_ml"], self.base["fuel_ml"])

    def test_pace_feedback_recovers_extra_drag(self):
        cum = [t for t, _, _ in self.base["crossings"]]
        kw = dict(marks=self.marks, v_on=16.0, crr_scale=1.2, k_scale=1.2)
        open_loop = ss.simulate(self.p, self.course, 32.5, **kw)
        closed = ss.simulate(self.p, self.course, 32.5, pace=cum, **kw)
        self.assertGreater(open_loop["time_s"], self.base["time_s"] + 30)
        self.assertLess(closed["time_s"], open_loop["time_s"] - 15)
        self.assertLess(closed["time_s"], self.p["course"]["limit_s"] - 30)

    def test_late_car_does_not_coast_home(self):
        # 遅れているときは最終周も最後の目印で加速する（惰行で終えると規定時間に近づきすぎる）
        r = ss.simulate(self.p, self.course, 32.5, marks=self.marks, v_on=16.0,
                        crr_scale=1.2, k_scale=1.2)
        last = [pos for lap, pos, _ in r["starts"] if lap == self.p["course"]["laps"]]
        # 最後の目印（1400 m）か、その手前で最低限界速度に落ちたところで 3 回目を加速する
        self.assertTrue(any(1300.0 < pos < 1405.0 for pos in last), last)
        self.assertLess(r["time_s"], self.p["course"]["limit_s"])

    def test_failed_restarts_cost_little_time(self):
        r = ss.simulate(self.p, self.course, 32.5, marks=self.marks, v_on=16.0, fail_restarts=3)
        self.assertEqual(r["burns"], self.base["burns"])
        self.assertLess(abs(r["time_s"] - self.base["time_s"]), 20.0)

    def test_speed_rule_respects_no_burn_zone(self):
        zone = (1500, 2150)
        r = ss.simulate(self.p, self.course, 32.0, v_on=20.0, zone=zone)
        for _, pos, kmh in r["starts"][1:]:
            if zone[0] <= pos <= zone[1]:
                self.assertLessEqual(kmh, 16.1)
        self.assertEqual(len(r["crossings"]), self.p["course"]["laps"])

    def test_place_marks(self):
        lap_target = ss.steady_lap_target(self.p["course"], self.course, self.p["target_s"])
        self.assertGreater(lap_target, self.p["target_s"] / self.p["course"]["laps"] * 0.97)
        placed = ss.place_marks(self.p, self.course, 3, lap_target, 17.0, grid=300, sweeps=1)
        self.assertIsNotNone(placed)
        marks, v_off = placed
        self.assertEqual(len(marks), 3)
        t, _, n, vmin = ss.steady_lap(self.p, self.course, marks, v_off)
        self.assertLessEqual(t, lap_target)
        self.assertAlmostEqual(n, 3.0)
        self.assertGreaterEqual(vmin, 17.0)

    def test_switch_to_same_pattern_changes_nothing(self):
        r = ss.simulate(self.p, self.course, 32.5, marks=self.marks, v_on=16.0,
                        switches=[(4, 32.5, self.marks)])
        self.assertEqual(r["time_s"], self.base["time_s"])
        self.assertEqual(r["starts"], self.base["starts"])

    def test_switch_changes_marks_and_cut_speed_from_that_lap(self):
        first = [350.0, 600.0, 1100.0]
        trace = []
        r = ss.simulate(self.p, self.course, 35.0, marks=first, v_on=16.0,
                        switches=[(4, 32.5, self.marks)], trace=trace, trace_every=1)
        for lap, pos, kmh in r["starts"][1:]:
            if kmh <= 16.5:
                continue                     # 保険の加速（最低限界速度）は目印と関係なく入る
            marks = first if lap < 4 else self.marks
            self.assertLess(min(abs(pos - m) for m in marks), 5.0, (lap, pos))
        top = {n: max(kmh for _, _, lap, _, kmh, _, _ in trace if lap == n) for n in (3, 4)}
        # 停止速度を 1 ステップ（0.1 s）ぶん行き過ぎる
        self.assertAlmostEqual(top[3], 35.0, delta=0.5)
        self.assertAlmostEqual(top[4], 32.5, delta=0.5)

    def test_pace_feedback_survives_switch(self):
        # 切り替えたあとも、通過タイムの遅れによる停止速度の補正が効く
        kw = dict(marks=[350.0, 600.0, 1100.0], v_on=16.0, switches=[(4, 32.5, self.marks)])
        cum = [t for t, _, _ in ss.simulate(self.p, self.course, 35.0, **kw)["crossings"]]
        slow = dict(kw, crr_scale=1.2, k_scale=1.2)
        open_loop = ss.simulate(self.p, self.course, 35.0, **slow)
        closed = ss.simulate(self.p, self.course, 35.0, pace=cum, **slow)
        # 合計ではなく最終周に入る通過で比べる（間に合っていれば最終周は惰行で終えるため）
        self.assertLess(closed["crossings"][-2][0], open_loop["crossings"][-2][0] - 15)

    def test_start_and_finish_off_the_control_line(self):
        c = self.p["course"]
        lap = self.course.lap_m
        self.assertEqual(ss.race_span(c, lap), (160.0, 6 * lap + 2224.0, False))
        trace = []
        r = ss.simulate(self.p, self.course, 32.5, marks=[100.0, 800.0, 1400.0], v_on=16.0,
                        trace=trace, trace_every=1)
        # コントロールラインは 6 回通過し、最後の要素がゴール
        self.assertEqual(len(r["crossings"]), c["laps"])
        self.assertEqual(r["crossings"][-1][0], r["time_s"])
        self.assertLess(r["crossings"][-2][0], r["time_s"] - 200)
        self.assertEqual(r["starts"][0], (1, 160.0, 0.0))
        self.assertAlmostEqual(trace[-1][1], 6 * lap + 2224.0, delta=2.0)
        # 発進位置より手前の目印（100 m）は 1 周目には来ず、2 周目から使う
        firsts = [(lap_no, round(pos)) for lap_no, pos, kmh in r["starts"][1:] if kmh > 16.5]
        self.assertNotIn(1, [n for n, pos in firsts if pos < 160])
        self.assertIn((2, 100), [(n, round(pos, -1)) for n, pos in firsts])

    def test_without_start_and_finish_runs_whole_laps(self):
        c = {k: v for k, v in self.p["course"].items() if k not in ("start_m", "finish_m")}
        p = dict(self.p, course=c)
        lap = self.course.lap_m
        self.assertEqual(ss.race_span(c, lap), (0.0, 7 * lap, True))
        r = ss.simulate(p, self.course, 32.5, marks=self.marks, v_on=16.0)
        self.assertEqual(len(r["crossings"]), 7)
        self.assertGreater(r["time_s"], self.base["time_s"] + 20)    # 約 270 m 長い

    def test_plan_from_params(self):
        self.assertIsNone(ss.plan_from_params(self.p))
        phases = [{"from_lap": 1, "v_off": 35, "marks_m": [1100, 350]},
                  {"from_lap": 4, "v_off": 32.5, "marks_m": [400, 800, 1400]}]
        pol = ss.plan_from_params(dict(self.p, phases=phases))
        self.assertEqual((pol["v_off"], pol["marks"], pol["v_on"]), (35.0, [350.0, 1100.0], 16.0))
        self.assertEqual(pol["switches"], [(4, 32.5, [400.0, 800.0, 1400.0])])
        for bad in ([dict(phases[0], from_lap=2)],                      # 1 周目から始まらない
                    [phases[0], dict(phases[1], from_lap=1)],          # 昇順でない
                    [phases[0], dict(phases[1], from_lap=8)],          # 規定周回を超える
                    [dict(phases[0], marks_m=[])]):                    # 加速開始位置が無い
            with self.assertRaises(ValueError):
                ss.plan_from_params(dict(self.p, phases=bad))

    def test_bundled_practice_plan_covers_the_measurements(self):
        p = ss.load_params(os.path.join(os.path.dirname(ss.DEFAULT_PARAMS), "practice_motegi_2026.json"))
        for key in ("course", "vehicle", "burn", "restart"):
            self.assertEqual(p[key], self.p[key], key)      # 本番用と同じモデルで計算する
        pol = ss.plan_from_params(p)
        r = ss.evaluate(p, self.course, pol)
        self.assertLess(r["time_s"], p["course"]["limit_s"] - 60)
        # 1〜3 周目: バックストレート（平坦、1020〜1590 m）で 21 km/h 以下から加速を始める
        back = [(lap, pos, kmh) for lap, pos, kmh in r["starts"] if lap <= 3 and 1020 <= pos <= 1500]
        self.assertEqual([lap for lap, _, _ in back], [1, 2, 3])
        self.assertTrue(all(kmh <= 21.0 for _, _, kmh in back), back)
        # 惰行で 18.5 km/h 以下まで落ちてから加速する回が 2 本以上
        self.assertGreaterEqual(sum(1 for _, _, kmh in r["starts"][1:] if kmh <= 18.5), 2)
        # 4〜6 周目は推奨パターンの位置で 3 回ずつ加速する
        for lap in (4, 5, 6):
            self.assertEqual(sum(1 for n, _, _ in r["starts"] if n == lap), 3, lap)


class TestMap(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.p = ss.load_params(ss.DEFAULT_PARAMS)
        cls.course = ss.course_from_params(cls.p)

    def test_latlng_at_interpolates_and_wraps(self):
        c = self.course
        self.assertEqual(c.latlng_at(0.0), (c.lat[0], c.lng[0]))
        lat, lng = c.latlng_at((c.s[10] + c.s[11]) / 2)
        self.assertAlmostEqual(lat, (c.lat[10] + c.lat[11]) / 2, places=7)
        self.assertAlmostEqual(lng, (c.lng[10] + c.lng[11]) / 2, places=7)
        # 最終点と先頭点のあいだ（閉じ区間）も補間できる
        lat, _ = c.latlng_at(c.lap_m - 0.01)
        self.assertAlmostEqual(lat, c.lat[0], places=5)
        self.assertEqual(c.nearest_id(400.0), 52)

    def test_burn_spans_follow_marks(self):
        marks = [400.0, 800.0, 1400.0]
        spans = ss.burn_spans(self.p, self.course, marks, 32.5)
        self.assertEqual(len(spans), 3)
        for m, (s0, s1, kmh) in zip(marks, spans):
            self.assertTrue(m <= s0 < m + 15, (m, s0))     # クランキングの分だけ遅れて始まる
            self.assertTrue(20 < s1 - s0 < 60, (s0, s1))
            self.assertLess(kmh, 30.5)

    def test_svg_is_well_formed_and_marks_every_start(self):
        import xml.etree.ElementTree as ET
        marks = [400.0, 800.0, 1400.0]
        svg = ss.course_svg(self.course, marks, ss.burn_spans(self.p, self.course, marks, 32.5), 32.5)
        root = ET.fromstring(svg)
        ns = "{http://www.w3.org/2000/svg}"
        self.assertEqual(len(root.findall(ns + "circle")), len(marks))
        texts = ["".join(t.itertext()) for t in root.findall(ns + "text")]
        for n, m in enumerate(marks, 1):
            self.assertTrue(any(t.startswith("%d: %.0f m" % (n, m)) for t in texts), texts)


if __name__ == "__main__":
    unittest.main(verbosity=2)
