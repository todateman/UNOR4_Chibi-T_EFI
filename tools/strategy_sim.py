#!/usr/bin/env python3
"""もてぎオーバルの加速・惰行パターンを試算する走行シミュレータ。

    python3 tools/strategy_sim.py fit-coast LOG0369.CSV LOG0503.CSV   # 実走の惰行から走行抵抗
    python3 tools/strategy_sim.py fit-grade LOG0369.CSV LOG0503.CSV --alt-csv alt.csv --out wp.csv
                                                                      # 惰行と標高の測定から周回の標高
    python3 tools/strategy_sim.py fit-burn LOG0645.CSV                # ドラムから加速度・燃料流量
    python3 tools/strategy_sim.py optimize --target 38:30             # 加速・惰行パターンの探索
    python3 tools/strategy_sim.py --params tools/strategy/practice_motegi_2026.json plan   # JSON に書いたプランの確認
    python3 tools/strategy_sim.py speed-rule                          # 速度だけで判断する規則（比較用）
    python3 tools/strategy_sim.py replay LOG0503.CSV                  # 惰行モデルを実走ログで検証
    python3 tools/strategy_sim.py map --marks 400 800 1400 --v-off 32.5 --out map.svg   # コース図

標準ライブラリのみで動く。パラメータは tools/strategy/motegi_2026.json にあり、
練習走行後は fit-coast / fit-burn の出力でそこを書き換えて optimize を回し直す。

モデルは単位質量あたりの加速度だけで閉じている:

    惰行: dv/dt = -g*(Crr + 勾配) - k*v^2            k = 0.5*rho*CdA/m
    加速: dv/dt = a_drum(v) + beta*d_drum - 惰行抵抗   (ドラム慣性 = 実車質量と仮定)

beta（ドラムの惰行損失をどこまで駆動力に上乗せするか）は実走で確かめるまで決められないので、
optimize は 0 / 0.5 / 1 の感度を必ず併記する。燃料は ECU 積算値と同じ式なので、
絶対燃費ではなくパターン同士の比較に使うこと。

パラメータ JSON に "phases" を書くと、周ごとにパターンを替えるプランになる（練習走行用）:

    "phases": [{"from_lap": 1, "v_off": 35.0, "marks_m": [350, 600, 1100]},
               {"from_lap": 4, "v_off": 32.5, "marks_m": [400, 800, 1400]}]

from_lap 周目からその停止速度・加速開始位置で走る。plan サブコマンドは探索をせず、このプランを評価する。

ロガー（Chibi-T_Furoshiki_Logger）の tools/make_strategy_json.py が、このファイルとパラメータ JSON を
読み込んで走行支援の設定ファイルを作る。形式が変わったことを向こうで検知できるよう、版数を 2 つ持つ:

    パラメータ JSON の "format_version"   項目を足したり、名前・単位・意味を変えたりしたら上げる
    下の INTERFACE_VERSION                上げる条件は定数のコメントを参照

どちらもロガー側のスクリプトが対応版数と照合し、違えば設定ファイルを作らずに止まる。
"""

from __future__ import annotations

import argparse
import bisect
import csv
import datetime as dt
import json
import math
import os
import statistics
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_PARAMS = os.path.join(HERE, "strategy", "motegi_2026.json")
G = 9.80665

# ロガー側の tools/make_strategy_json.py が照合する版数。次のどれかを変えたら上げる:
#   - 推奨パターンの選び方（cmd_optimize。ロガー側は同じ手順を写して持っている）
#   - load_params / course_from_params / plan_from_params / simulate / evaluate / pick / place_marks
#     の引数・戻り値
#   - JITTER / HOME_MARGIN_S の意味
INTERFACE_VERSION = 2


# ---------------------------------------------------------------------------
# 共通
# ---------------------------------------------------------------------------

def mmss(sec: float) -> str:
    sec = round(sec, 1)                     # 59.96 秒が "60.0" にならないよう先に丸める
    return "%d:%04.1f" % (sec // 60, sec % 60)


def parse_mmss(text: str) -> float:
    m, _, s = text.partition(":")
    return float(m) * 60 + float(s) if s else float(m)


def interp(x: float, xs: list, ys: list) -> float:
    if x <= xs[0]:
        return ys[0]
    if x >= xs[-1]:
        return ys[-1]
    i = bisect.bisect_right(xs, x) - 1
    return ys[i] + (x - xs[i]) / (xs[i + 1] - xs[i]) * (ys[i + 1] - ys[i])


def inj_ml(inj_ms: float) -> float:
    """1 噴射あたりの燃料量。src/main.cpp の gasml 積算式と同じ。"""
    return (inj_ms * 1000.0 * 0.0000007 + 0.0015) / 1.5073


def load_params(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        p = json.load(f)
    p["_dir"] = os.path.dirname(os.path.abspath(path))
    return p


# ---------------------------------------------------------------------------
# コース
# ---------------------------------------------------------------------------

class Course:
    """ウェイポイント CSV（ID,lat,lng,alt,distance）から作る周回コース。

    distance は公式の 1 周距離へ比例で伸縮する（ウェイポイントの経路長は走行ラインと
    一致しないため）。標高は GPS 由来でばらつくので周回の移動平均を掛ける。
    """

    def __init__(self, path: str, lap_m: float, smooth: int = 9):
        with open(path, encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))
        self.lat = [float(r["lat"]) for r in rows]
        self.lng = [float(r["lng"]) for r in rows]
        raw_alt = [float(r["alt"]) for r in rows]
        dist = [float(r["distance"]) for r in rows]
        n = len(rows)
        h = smooth // 2
        self.alt = [sum(raw_alt[(i + j) % n] for j in range(-h, h + 1)) / smooth
                    for i in range(n)]
        self._coslat = math.cos(math.radians(self.lat[0]))
        closing = self._ground_m(self.lat[-1], self.lng[-1], self.lat[0], self.lng[0])
        scale = lap_m / (dist[-1] + closing)
        self.s = [d * scale for d in dist]
        self.lap_m = lap_m
        # 積分ループから毎ステップ引くので 1 m 刻みの表にしておく
        self._slope = [self._slope_at(i + 0.5) for i in range(int(math.ceil(lap_m)))]

    def _ground_m(self, lat1, lng1, lat2, lng2) -> float:
        return math.hypot((lat2 - lat1) * 111320.0,
                          (lng2 - lng1) * 111320.0 * self._coslat)

    def _seg(self, s: float):
        s %= self.lap_m
        i = max(bisect.bisect_right(self.s, s) - 1, 0)
        if i >= len(self.s) - 1:           # 最終点 → 先頭点の閉じ区間
            return s, self.s[i], self.lap_m, self.alt[i], self.alt[0]
        return s, self.s[i], self.s[i + 1], self.alt[i], self.alt[i + 1]

    def _slope_at(self, s: float) -> float:
        _, s0, s1, a0, a1 = self._seg(s)
        return (a1 - a0) / max(s1 - s0, 1e-6)

    def slope(self, s: float) -> float:
        return self._slope[min(int(s % self.lap_m), len(self._slope) - 1)]

    def alt_at(self, s: float) -> float:
        s, s0, s1, a0, a1 = self._seg(s)
        return a0 + (s - s0) / max(s1 - s0, 1e-6) * (a1 - a0)

    def latlng_at(self, s: float):
        s %= self.lap_m
        i = max(bisect.bisect_right(self.s, s) - 1, 0)
        j = (i + 1) % len(self.s)
        s1 = self.s[j] if j else self.lap_m
        f = (s - self.s[i]) / max(s1 - self.s[i], 1e-6)
        return (self.lat[i] + f * (self.lat[j] - self.lat[i]),
                self.lng[i] + f * (self.lng[j] - self.lng[i]))

    def nearest_id(self, s: float) -> int:
        """コース距離に最も近いウェイポイントの ID（1 始まり）。"""
        s %= self.lap_m
        return min(range(len(self.s)), key=lambda i: abs(self.s[i] - s)) + 1

    def nearest(self, lat: float, lng: float):
        """(コース距離 m, 標高 m, ウェイポイントからの離れ m)"""
        best_i, best_d = 0, float("inf")
        for i in range(len(self.s)):
            d = self._ground_m(lat, lng, self.lat[i], self.lng[i])
            if d < best_d:
                best_i, best_d = i, d
        return self.s[best_i], self.alt[best_i], best_d


def course_from_params(p: dict, waypoints: str | None = None) -> Course:
    c = p["course"]
    path = waypoints or os.path.join(p["_dir"], c["waypoints"])
    return Course(path, c["total_m"] / c["laps"])


def coast_step(course: Course, s: float, v: float, crr: float, k: float, dt_s: float):
    a = -(G * (crr + course.slope(s)) + k * v * v)
    v = max(v + a * dt_s, 0.0)
    return s + v * dt_s, v


# ---------------------------------------------------------------------------
# ログ読み込み
# ---------------------------------------------------------------------------

# 年度でヘッダが違う（11 列 / 16 列 / 22 列）ので、見出しの部分一致で列を引く
_COLS = {
    "v": ("速度",), "lap": ("ラップ",), "wt": ("走行時間",), "rpm": ("回転数",),
    "inj": ("噴射時間",), "dist": ("走行距離",), "fuel": ("積算燃料",),
    "lat": ("lat",), "lng": ("lng", "lon"), "fp": ("燃圧",),
}
_REQUIRED = ("v", "rpm", "dist")


def load_log(path: str) -> list:
    rows = []
    with open(path, encoding="utf-8-sig", errors="replace") as f:
        reader = csv.reader(f)
        header = [h.strip() for h in next(reader)]
        idx = {}
        for key, names in _COLS.items():
            idx[key] = next((i for i, h in enumerate(header)
                             if any(nm in h for nm in names)), None)
        missing = [k for k in _REQUIRED if idx[k] is None]
        if missing:
            raise ValueError("%s: 列が見つかりません %s" % (path, missing))
        for x in reader:
            try:
                t = dt.datetime.strptime(x[0].strip(), "%Y/%m/%d %H:%M:%S.%f").timestamp()
            except (ValueError, IndexError):
                continue                    # GPS 未捕捉などで日時が空の行
            row = {"t": t}
            try:
                for key, i in idx.items():
                    row[key] = float(x[i]) if i is not None else None
            except (ValueError, IndexError):
                continue
            rows.append(row)
    return rows


def _split(rows: list, pred, gap_s: float, tkey: str = "t") -> list:
    """pred を満たす連続区間に分ける。時刻が gap_s 以上飛んだところでも切る。"""
    segs, cur = [], []
    for r in rows:
        if pred(r) and (not cur or r[tkey] - cur[-1][tkey] <= gap_s):
            cur.append(r)
        else:
            if cur:
                segs.append(cur)
            cur = [r] if pred(r) else []
    if cur:
        segs.append(cur)
    return segs


# ---------------------------------------------------------------------------
# fit-coast: 実走の惰行から Crr と k
# ---------------------------------------------------------------------------

def coast_windows(rows: list, course: Course, win_m: float = 250.0,
                  skip_s: float = 3.0, min_kmh: float = 14.0) -> list:
    """惰行区間を win_m ごとに区切ったエネルギー収支 (g*ds, ∫v^2 ds, 失ったエネルギー)。"""
    return [(G * ds, iv, dke - G * (hb - ha))
            for _, _, ds, iv, dke, ha, hb in coast_spans(rows, course, win_m, skip_s, min_kmh)]


def coast_spans(rows: list, course: Course, win_m: float = 250.0,
                skip_s: float = 3.0, min_kmh: float = 14.0) -> list:
    """惰行区間を win_m ごとに区切った (始点 s, 終点 s, ds, ∫v^2 ds, -Δ(v^2/2), 始点の標高, 終点の標高)。"""
    out = []
    for seg in _split(rows, lambda r: r["rpm"] == 0 and r["v"] >= min_kmh, gap_s=5.0):
        # エンジン停止直後はクラッチ・エンジンの引きずりが残る
        seg = [r for r in seg if r["t"] - seg[0]["t"] >= skip_s]
        i = 0
        while i < len(seg) - 1:
            j = i
            while j < len(seg) - 1 and seg[j]["dist"] - seg[i]["dist"] < win_m:
                j += 1
            ds = seg[j]["dist"] - seg[i]["dist"]
            if ds < 0.8 * win_m:
                break
            a, b = seg[i], seg[j]
            sa, ha, off_a = course.nearest(a["lat"], a["lng"])
            sb, hb, off_b = course.nearest(b["lat"], b["lng"])
            if max(off_a, off_b) <= 30.0:   # コース外（ピットなど）は使わない
                iv = 0.0
                for q in range(i, j):
                    vm = (seg[q]["v"] + seg[q + 1]["v"]) / 7.2
                    iv += vm * vm * (seg[q + 1]["dist"] - seg[q]["dist"])
                va, vb = a["v"] / 3.6, b["v"] / 3.6
                out.append((sa, sb, ds, iv, -(vb * vb - va * va) / 2, ha, hb))
            i = j
    return out


def solve_coast(windows: list):
    """y = Crr*x1 + k*x2 の最小二乗。(Crr, k, 残差RMS J/kg)"""
    s11 = sum(w[0] * w[0] for w in windows)
    s12 = sum(w[0] * w[1] for w in windows)
    s22 = sum(w[1] * w[1] for w in windows)
    b1 = sum(w[0] * w[2] for w in windows)
    b2 = sum(w[1] * w[2] for w in windows)
    det = s11 * s22 - s12 * s12
    crr = (b1 * s22 - b2 * s12) / det
    k = (s11 * b2 - s12 * b1) / det
    rms = math.sqrt(sum((w[2] - crr * w[0] - k * w[1]) ** 2 for w in windows) / len(windows))
    return crr, k, rms


def cmd_fit_coast(args) -> int:
    p = load_params(args.params)
    course = course_from_params(p, args.waypoints)
    allw = []
    for path in args.logs:
        w = coast_windows(load_log(path), course)
        allw += w
        if len(w) >= 3:
            crr, k, rms = solve_coast(w)
            print("%s: 区間 %d  Crr=%.5f  k=%.6f /m  残差 %.2f J/kg"
                  % (os.path.basename(path), len(w), crr, k, rms))
        else:
            print("%s: 惰行区間が足りません (%d)" % (os.path.basename(path), len(w)))
    if len(allw) < 3:
        return 1
    crr, k, rms = solve_coast(allw)
    print("全体: 区間 %d  Crr=%.5f  k=%.6f /m  残差 %.2f J/kg" % (len(allw), crr, k, rms))
    for kmh in (20, 25, 30, 35):
        v = kmh / 3.6
        print("  %d km/h の減速度(平坦) %.4f m/s2" % (kmh, G * crr + k * v * v))
    print('JSON: "vehicle": {"crr": %.5f, "k_aero": %.6f}' % (crr, k))
    return 0


# ---------------------------------------------------------------------------
# fit-grade: 惰行の減速度と標高の測定値から周回の標高を推定し直す
# ---------------------------------------------------------------------------

def _solve(a: list, b: list) -> list:
    """連立一次方程式 a x = b（部分ピボット付きガウス消去）。"""
    n = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for c in range(n):
        p = max(range(c, n), key=lambda r: abs(m[r][c]))
        m[c], m[p] = m[p], m[c]
        if abs(m[c][c]) < 1e-12:
            raise ValueError("解が定まりません（惰行区間か標高の測定値が足りません）")
        for r in range(c + 1, n):
            f = m[r][c] / m[c][c]
            if f:
                for q in range(c, n + 1):
                    m[r][q] -= f * m[c][q]
    x = [0.0] * n
    for c in range(n - 1, -1, -1):
        x[c] = (m[c][n] - sum(m[c][q] * x[q] for q in range(c + 1, n))) / m[c][c]
    return x


class _Lsq:
    """1 本ずつ式を足していく重み付き最小二乗（正規方程式）。"""

    def __init__(self, n: int):
        self.ata = [[0.0] * n for _ in range(n)]
        self.atb = [0.0] * n

    def add(self, coefs: dict, rhs: float, w: float = 1.0):
        items = [(j, c * w) for j, c in coefs.items() if c]
        for j, cj in items:
            self.atb[j] += cj * rhs * w
            row = self.ata[j]
            for q, cq in items:
                row[q] += cj * cq

    def solve(self) -> list:
        return _solve(self.ata, self.atb)


class GradeFit:
    """周回の標高を node_m 間隔の節点で表し、次の 3 種類の式をまとめて最小二乗で解く。

    - 惰行の各窓: g*Crr*ds + k*∫v^2 ds + g*(h(終点) - h(始点)) = -Δ(v^2/2)
      （Crr と k は車両ごと。窓の重みは車両ごとの残差 RMS で決め直す）
    - 標高の測定値（ウェイポイント・別の GPS の周平均など）: h(s) - 測定ごとのオフセット = 測定値
    - 隣り合う節点の 2 階差分 = 0（平滑化）

    周で閉じているので一定の勾配は Crr と区別でき、標高の平均はウェイポイントの平均に合わせる。
    """

    def __init__(self, course: Course, node_m: float = 50.0):
        self.course = course
        self.n = max(int(round(course.lap_m / node_m)), 8)
        self.d = course.lap_m / self.n

    def weights(self, s: float) -> dict:
        u = (s % self.course.lap_m) / self.d
        j = int(u) % self.n
        f = u - int(u)
        return {j: 1.0 - f, (j + 1) % self.n: f}

    def solve(self, span_sets: list, priors: list, smooth_m: float = 0.05,
              iters: int = 4, reject: float = 3.0) -> dict:
        """span_sets: 車両ごとの coast_spans()。priors: [(名前, [(s, 標高)], σ m), ...]"""
        nv, npr = len(span_sets), len(priors)
        nx = self.n + 2 * nv + npr
        sig = [1.0] * nv                    # 窓の残差の目安 J/kg
        use = [[True] * len(sp) for sp in span_sets]
        mean_alt = statistics.fmean(self.course.alt)
        for _ in range(iters):
            ls = _Lsq(nx)
            for v, spans in enumerate(span_sets):
                ic, ik = self.n + 2 * v, self.n + 2 * v + 1
                for ok, (sa, sb, ds, iv, dke, _, _) in zip(use[v], spans):
                    if not ok:
                        continue
                    co = {ic: G * ds, ik: iv}
                    for j, w in self.weights(sb).items():
                        co[j] = co.get(j, 0.0) + G * w
                    for j, w in self.weights(sa).items():
                        co[j] = co.get(j, 0.0) - G * w
                    ls.add(co, dke, 1.0 / sig[v])
            for q, (_, pts, sigma) in enumerate(priors):
                io = self.n + 2 * nv + q
                for s, h in pts:
                    co = dict(self.weights(s))
                    co[io] = -1.0
                    ls.add(co, h, 1.0 / sigma)
            for j in range(self.n):
                ls.add({(j - 1) % self.n: 1.0, j: -2.0, (j + 1) % self.n: 1.0}, 0.0, 1.0 / smooth_m)
            ls.add({j: 1.0 / self.n for j in range(self.n)}, mean_alt, 1e4)
            x = ls.solve()
            self.h = x[:self.n]
            vehicles = []
            for v, spans in enumerate(span_sets):
                crr, k = x[self.n + 2 * v], x[self.n + 2 * v + 1]
                res = [dke - (G * ds * crr + iv * k + G * (self.alt_at(sb) - self.alt_at(sa)))
                       for sa, sb, ds, iv, dke, _, _ in spans]
                kept = [r for r, ok in zip(res, use[v]) if ok]
                rms = math.sqrt(statistics.fmean(r * r for r in kept)) if kept else float("nan")
                sig[v] = max(rms, 0.05)
                use[v] = [abs(r) <= reject * sig[v] for r in res]
                vehicles.append({"crr": crr, "k": k, "rms": rms, "n": len(kept), "all": len(spans)})
        return {"vehicles": vehicles,
                "offsets": [x[self.n + 2 * nv + q] for q in range(npr)]}

    def alt_at(self, s: float) -> float:
        return sum(self.h[j] * w for j, w in self.weights(s).items())


def read_alt_csv(path: str) -> list:
    """(s, 標高) の CSV（列名 s, alt）。"""
    with open(path, encoding="utf-8-sig") as f:
        return [(float(r["s"]), float(r["alt"])) for r in csv.DictReader(f)]


def write_waypoints(src: str, dst: str, course: Course, alt_at) -> None:
    """src の alt 列だけを alt_at(コース距離) に置き換えて dst に書く。BOM・改行・他の列はそのまま。"""
    with open(src, "rb") as f:
        raw = f.read()
    bom = raw.startswith(b"\xef\xbb\xbf")
    text = raw.decode("utf-8-sig")
    nl = "\r\n" if "\r\n" in text else "\n"
    lines = text.split(nl)
    header = lines[0].split(",")
    ia = header.index("alt")
    out, i = [lines[0]], 0
    for line in lines[1:]:
        if not line.strip():
            out.append(line)
            continue
        cols = line.split(",")
        cols[ia] = "%.2f" % alt_at(course.s[i])
        out.append(",".join(cols))
        i += 1
    with open(dst, "wb") as f:
        f.write((b"\xef\xbb\xbf" if bom else b"") + nl.join(out).encode("utf-8"))


def cmd_fit_grade(args) -> int:
    p = load_params(args.params)
    course = course_from_params(p, args.waypoints)
    groups = args.groups or list(range(1, len(args.logs) + 1))
    if len(groups) != len(args.logs):
        print("--groups の数をログの数に合わせてください")
        return 1
    order = sorted(set(groups), key=groups.index)
    span_sets = [[] for _ in order]
    names = [[] for _ in order]
    for path, g in zip(args.logs, groups):
        span_sets[order.index(g)] += coast_spans(load_log(path), course)
        names[order.index(g)].append(os.path.basename(path))
    priors = [("ウェイポイント", list(zip(course.s, course.alt)), args.sigma_wp)]
    for path in args.alt_csv or ():
        priors.append((os.path.basename(path), read_alt_csv(path), args.sigma_alt))
    fit = GradeFit(course, args.node_m)
    try:
        r = fit.solve(span_sets, priors, smooth_m=args.smooth)
    except ValueError as e:
        print(e)
        return 1
    print("節点 %d 個（%.1f m 間隔）  標高の重み: ウェイポイント σ %.2f m、別の測定 σ %.2f m、平滑 %.2f m"
          % (fit.n, fit.d, args.sigma_wp, args.sigma_alt, args.smooth))
    for nm, v in zip(names, r["vehicles"]):
        print("  %s: 区間 %d/%d  Crr=%.5f  k=%.6f /m  残差 %.2f J/kg"
              % (" + ".join(nm), v["n"], v["all"], v["crr"], v["k"], v["rms"]))

    def prior_at(pts, s):
        xs = [q[0] for q in pts]
        i = bisect.bisect_left(xs, s)
        return pts[min(i, len(pts) - 1)][1]

    cols = [(nm, pts, off) for (nm, pts, _), off in zip(priors, r["offsets"])]
    print("\n  距離 m  推定 m  " + "  ".join("%s との差" % nm for nm, _, _ in cols))
    for s in range(0, int(course.lap_m), args.every):
        h = fit.alt_at(s)
        diffs = ["%+6.2f" % (h - (prior_at(pts, s) + off)) for _, pts, off in cols]
        print("  %5d  %6.2f  %s" % (s, h, "  ".join(diffs)))
    hs = [fit.alt_at(s) for s in course.s]
    print("\n  高低差 推定 %.2f m / ウェイポイント %.2f m" % (max(hs) - min(hs), max(course.alt) - min(course.alt)))
    for nm, pts, off in cols:
        rms = math.sqrt(statistics.fmean((fit.alt_at(s) - (h + off)) ** 2 for s, h in pts))
        print("  %s との差 RMS %.2f m" % (nm, rms))
    if args.out:
        write_waypoints(args.waypoints or os.path.join(p["_dir"], p["course"]["waypoints"]),
                        args.out, course, fit.alt_at)
        print("\nalt を差し替えたウェイポイント: %s" % args.out)
    return 0


# ---------------------------------------------------------------------------
# fit-burn: ドラムログから加速度・燃料流量
# ---------------------------------------------------------------------------

def fit_burn(rows: list, bin_kmh: int = 4, win_s: float = 1.0, min_n: int = 5) -> dict:
    # ロガーの記録日時は 0.1 s 単位で揺れるので、ECU の走行時間が 0.1 s 分解能ならそちらを使う
    tkey = "wt" if any(r["wt"] is not None and r["wt"] % 1 for r in rows) else "t"
    acc, fuel, fp, dec = {}, {}, {}, []
    for seg in _split(rows, lambda r: True, gap_s=0.35, tkey=tkey):
        j = 0
        for i in range(len(seg)):
            while j < len(seg) - 1 and seg[j][tkey] - seg[i][tkey] < win_s - 1e-6:
                j += 1
            span = seg[j][tkey] - seg[i][tkey]
            if span < win_s - 1e-6:
                break
            w = seg[i:j + 1]
            a = (w[-1]["v"] - w[0]["v"]) / 3.6 / span
            vm = (w[-1]["v"] + w[0]["v"]) / 2
            if vm < 6:
                continue
            if min(r["inj"] for r in w) > 0 and min(r["rpm"] for r in w) > 1800:
                b = int(vm // bin_kmh) * bin_kmh
                acc.setdefault(b, []).append(a)
                fuel.setdefault(b, []).append(
                    statistics.fmean(inj_ml(r["inj"]) * r["rpm"] / 120.0 for r in w))
                if w[0]["fp"] is not None:
                    fp.setdefault(b, []).append(min(r["fp"] for r in w))
            elif max(r["inj"] for r in w) == 0 and max(r["rpm"] for r in w) < 1000:
                dec.append(-a)
    bins = sorted(b for b in acc if len(acc[b]) >= min_n)
    return {
        "speed_kmh": [b + bin_kmh / 2 for b in bins],
        "accel_drum": [round(statistics.median(acc[b]), 3) for b in bins],
        "fuel_ml_s": [round(statistics.fmean(fuel[b]), 4) for b in bins],
        "fuel_press_min": [round(min(fp[b]), 3) if b in fp else None for b in bins],
        "n": [len(acc[b]) for b in bins],
        "drum_decel": round(statistics.median(dec), 3) if dec else None,
        "drum_decel_n": len(dec),
    }


def cmd_fit_burn(args) -> int:
    rows = load_log(args.log)
    if rows[0]["inj"] is None:
        print("噴射時間の列が無いログです（22 列形式が必要）")
        return 1
    r = fit_burn(rows)
    print("速度帯 km/h | ドラム加速度 m/s2 | 燃料 ml/s | 車輪仕事 kJ/ml (beta=0 / 1) | 最低燃圧 MPa | n")
    d = r["drum_decel"] or 0.0
    for v, a, f, fpm, n in zip(r["speed_kmh"], r["accel_drum"], r["fuel_ml_s"],
                               r["fuel_press_min"], r["n"]):
        # 単位質量あたりの仕事率 a*v [W/kg] に慣性質量を掛けて燃料流量で割る
        e0 = args.inertia_kg * a * (v / 3.6) / f / 1000.0
        e1 = args.inertia_kg * (a + d) * (v / 3.6) / f / 1000.0
        print("  %4.0f      |  %.3f           | %.4f    |  %5.1f / %5.1f              | %s  | %d"
              % (v, a, f, e0, e1, "%.3f" % fpm if fpm is not None else "  -  ", n))
    print("ドラム惰行減速度: %s m/s2 (n=%d)" % (r["drum_decel"], r["drum_decel_n"]))
    print("JSON:")
    print(json.dumps({"speed_kmh": r["speed_kmh"], "accel_drum": r["accel_drum"],
                      "fuel_ml_s": r["fuel_ml_s"], "drum_decel": r["drum_decel"]}))
    return 0


# ---------------------------------------------------------------------------
# 走行シミュレーション
# ---------------------------------------------------------------------------

SKIP_KMH = 2.0      # 目印を通過しても、停止速度までこれ未満しか残っていなければ加速しない
HOME_MARGIN_S = 15.0  # 最終周を惰行で終えてよいのは、規定時間までこれだけ残る場合だけ


def pace_bump(delay_s: float) -> float:
    """周回通過時の遅れ（+ が遅れ）から、次の周の停止速度の上げ幅 km/h を決める。

    停止速度 +1 km/h で 1 周あたり約 12 秒縮むので、遅れは 1 周で取り戻せる。
    """
    if delay_s > 35:
        return 3.0
    if delay_s > 20:
        return 2.0
    if delay_s > 8:
        return 1.0
    if delay_s < -30:
        return -0.5
    return 0.0


def simulate(p: dict, course: Course, v_off: float, *, marks=(), v_on: float | None = None,
             zone=None, laps: int | None = None, beta: float | None = None,
             crr_scale: float = 1.0, k_scale: float = 1.0, fail_restarts: int = 0,
             pace: list | None = None, switches=(), dt_s: float = 0.1, trace: list | None = None,
             trace_every: int = 10) -> dict:
    """規定周回を走る。加速は次のどちらかで始め、v_off km/h に達したら止める。

    - marks: スタートラインからの距離 m。毎周そこを通過したときに始める（位置同期）
    - v_on : この速度まで落ちたら始める。zone=(開始 m, 終了 m) の中では v_on-4 km/h まで待つ

    最終盤は、惰行だけでゴールに届き、かつ規定時間まで HOME_MARGIN_S 以上残ると分かった
    時点で以降加速しない。
    pace に各周通過の目標累積時間を渡すと、通過のたびに pace_bump() で停止速度を補正する。
    switches に [(周, 停止速度 km/h, marks), ...] を渡すと、その周に入るときに停止速度と
    加速開始位置を替える（周は 1 始まり）。
    """
    veh, burn, rst, c = p["vehicle"], p["burn"], p["restart"], p["course"]
    crr = veh["crr"] * crr_scale
    k = veh["k_aero"] * k_scale
    if beta is None:
        beta = burn["drum_loss_fraction"]
    # ドラム慣性と実車質量が違えば加速度は質量比で変わる
    mratio = burn.get("inertia_kg", 1.0) / veh.get("mass_kg", burn.get("inertia_kg", 1.0))
    extra = beta * burn["drum_decel"]
    xs, acc, frate = burn["speed_kmh"], burn["accel_drum"], burn["fuel_ml_s"]
    lap = course.lap_m
    total = lap * (laps if laps is not None else c["laps"])
    vfin = c.get("finish_min_kmh", 12.0) / 3.6
    # 失敗させる再始動（発進を除く通し番号）。序盤・中盤・終盤に散らす
    fail_at = {2 + 7 * i for i in range(fail_restarts)}
    marks = sorted(m % lap for m in marks)
    mi, mlap = 0, 0
    switches = sorted(switches, key=lambda sw: sw[0])

    t = s = v = 0.0
    fuel = rst["fuel_ml"]
    state, dead, burns, restarts, burn_time = "burn", 0.0, 1, 0, 0.0
    home = launched = False
    crossings, starts = [], [(1, 0.0, 0.0)]
    vmin = float("inf")
    bump = 0.0
    step = 0
    while s < total:
        res = G * (crr + course.slope(s)) + k * v * v
        kmh = v * 3.6
        v_cut = v_off + bump
        hit = False
        while marks and s >= mlap * lap + marks[mi]:
            hit = True
            mi += 1
            if mi == len(marks):
                mi, mlap = 0, mlap + 1
        if state == "burn":
            a = (interp(kmh, xs, acc) + extra) * mratio - res
            fuel += interp(kmh, xs, frate) * dt_s
            burn_time += dt_s
            if kmh >= v_cut:
                state, launched = "coast", True
        else:
            a = -res
            if state == "crank":
                dead -= dt_s
                if dead <= 0:
                    state = "burn"
            elif not home:
                want = hit and kmh < v_cut - SKIP_KMH
                if not want and v_on is not None and kmh <= v_on:
                    pos = s % lap
                    want = not (zone and zone[0] <= pos <= zone[1] and kmh > v_on - 4)
                if want:
                    tc = _coast_home_s(course, s, v, crr, k, total, vfin) if total - s < 1500 else None
                    if tc is not None and t + tc <= c["limit_s"] - HOME_MARGIN_S:
                        home = True
                    else:
                        restarts += 1
                        burns += 1
                        fuel += rst["fuel_ml"]
                        dead = rst["delay_s"] + (rst["fail_dead_s"] if restarts in fail_at else 0.0)
                        state = "crank"
                        starts.append((int(s // lap) + 1, s % lap, kmh))
        if launched:
            vmin = min(vmin, kmh)
        v = max(v + a * dt_s, 0.3)
        s2 = s + v * dt_s
        if int(s2 // lap) > int(s // lap):
            crossings.append((t + dt_s, fuel, burns))
            if pace is not None and len(crossings) <= len(pace):
                bump = pace_bump(t + dt_s - pace[len(crossings) - 1])
            while switches and switches[0][0] <= len(crossings) + 1:
                _, v_off, new_marks = switches.pop(0)
                marks, mi, mlap = sorted(m % lap for m in new_marks), 0, len(crossings)
        s = s2
        t += dt_s
        step += 1
        if trace is not None and step % trace_every == 0:
            trace.append((round(t, 1), round(s, 1), int(s // lap) + 1, round(s % lap, 1),
                          round(v * 3.6, 2), state, round(fuel, 3)))
    return {"time_s": t, "fuel_ml": fuel, "burns": burns, "burn_time_s": burn_time,
            "crossings": crossings, "starts": starts, "finish_kmh": v * 3.6, "min_kmh": vmin}


def _coast_home_s(course, s, v, crr, k, total, vfin):
    """惰行だけでゴールまでかかる秒数。途中で vfin を割るなら None。"""
    t = 0.0
    while s < total:
        s, v = coast_step(course, s, v, crr, k, 0.5)
        t += 0.5
        if v < vfin:
            return None
    return t


# ドライバーは狙った位置・速度ちょうどでは加速を始められない。開始位置 ±30 m（速度で始める
# 規則なら ±0.5 km/h）ずらした 3 通りの最悪時間・平均燃料で評価する。
# 停止速度のずれは平均速度そのものを動かす（1 km/h で合計約 90 秒）ので、ここには含めず
# ペース感度として別に示し、周回タイムによる補正（pace）で吸収する。
JITTER = ((0.0, 0.0), (-30.0, -0.5), (30.0, 0.5))


def jittered(pol: dict):
    """pol を JITTER の 3 通りにずらし、simulate() へ渡す (v_off, キーワード引数) を順に返す。"""
    v_on = pol.get("v_on")
    for dm, dv in JITTER:
        yield pol["v_off"], {
            "marks": [m + dm for m in pol.get("marks", ())],
            "v_on": None if v_on is None else v_on + dv,
            "zone": pol.get("zone"),
            "switches": [(lap, v, [m + dm for m in ms]) for lap, v, ms in pol.get("switches", ())],
        }


def plan_from_params(p: dict) -> dict | None:
    """パラメータの "phases"（周ごとのパターン）を pol にして返す。phases が無ければ None。

    先頭の段階が 1 周目から走るパターンで、2 つ目以降は pol["switches"] に入る。
    """
    phases = p.get("phases")
    if not phases:
        return None
    laps = p["course"]["laps"]
    firsts = [ph["from_lap"] for ph in phases]
    if firsts[0] != 1:
        raise ValueError("phases の先頭は from_lap 1 にしてください（%s）" % firsts[0])
    if any(b <= a for a, b in zip(firsts, firsts[1:])) or firsts[-1] > laps:
        raise ValueError("phases の from_lap は昇順で %d 周以内にしてください（%s）" % (laps, firsts))
    if any(not ph["marks_m"] for ph in phases):
        raise ValueError("phases の marks_m が空です")
    pats = [(ph["from_lap"], float(ph["v_off"]), sorted(float(m) for m in ph["marks_m"])) for ph in phases]
    return {"v_off": pats[0][1], "marks": pats[0][2], "v_on": p["course"].get("guard_kmh", 16.0),
            "switches": pats[1:]}


def evaluate(p: dict, course: Course, pol: dict, **kw) -> dict:
    """pol = {"v_off":, "marks":, "v_on":, "zone":, "switches":} を JITTER の 3 通りで走らせてまとめる。"""
    runs = [simulate(p, course, v_off, **jit, **kw) for v_off, jit in jittered(pol)]
    r = dict(runs[0])
    r.update(pol=pol,
             time_s=max(q["time_s"] for q in runs),
             time_min_s=min(q["time_s"] for q in runs),
             fuel_ml=statistics.fmean(q["fuel_ml"] for q in runs),
             burns=max(q["burns"] for q in runs),
             min_kmh=min(q["min_kmh"] for q in runs))
    return r


def pick(results: list, target_s: float, fuel_slack: float = 0.05):
    """(燃料最小, 推奨)。推奨は燃料最小から fuel_slack 以内で加速回数が最も少ない組。

    再始動のたびに始動失敗のリスクを負うので、数%の燃料差なら回数の少なさを取る。
    """
    ok = [r for r in results if r["time_s"] <= target_s]
    if not ok:
        return None, None
    best = min(ok, key=lambda r: r["fuel_ml"])
    near = [r for r in ok if r["fuel_ml"] <= best["fuel_ml"] * (1 + fuel_slack)]
    rec = min(near, key=lambda r: (r["burns"], r["fuel_ml"]))
    return best, rec


# --- 位置同期パターンの探索 -------------------------------------------------

def steady_lap(p: dict, course: Course, marks, v_off: float, dt_s: float = 0.2):
    """定常周回の (時間 s, 燃料 ml, 加速回数, 最低速度 km/h)。

    発進周と最終周を除くため 4 周走って 2〜3 周目を測る。保険の加速（v_on）は入れない。
    """
    r = simulate(p, course, v_off, marks=marks, laps=4, dt_s=dt_s)
    (t1, f1, b1), (t3, f3, b3) = r["crossings"][0], r["crossings"][2]
    return (t3 - t1) / 2, (f3 - f1) / 2, (b3 - b1) / 2, r["min_kmh"]


def fit_v_off(p, course, marks, lap_target_s, floor_kmh, lo=26.0, hi=40.0, iters=6):
    """定常周回が lap_target_s に収まる最小の停止速度。(v_off, 燃料 ml/周) / 成り立たなければ None

    停止速度を上げるほど周回は速くなるので二分法で足りる。最低速度が floor_kmh を割る
    （＝本番では保険の加速が入ってしまう）配置と、目印を飛ばしてしまう配置は採らない。
    """
    def ok(v_off):
        t, f, n, vmin = steady_lap(p, course, marks, v_off)
        return (t <= lap_target_s and vmin >= floor_kmh), f, n

    good, f, n = ok(hi)
    if not good:
        return None
    for _ in range(iters):
        mid = (lo + hi) / 2
        good, fm, nm = ok(mid)
        if good:
            hi, f, n = mid, fm, nm
        else:
            lo = mid
    return (hi, f) if abs(n - len(marks)) < 0.01 else None


def _gap_ok(marks, lap, min_gap) -> bool:
    m = sorted(marks)
    return all((m[(i + 1) % len(m)] - m[i]) % lap >= min_gap for i in range(len(m))) or len(m) == 1


def place_marks(p, course, n, lap_target_s, floor_kmh, grid=100, min_gap=200, sweeps=3):
    """1 周 n 回の加速開始位置を、周回時間を揃えたうえで燃料が最小になるよう座標降下で決める。"""
    lap = course.lap_m
    marks = sorted(float(int((i * lap / n + 100) // grid * grid)) for i in range(n))
    best = fit_v_off(p, course, marks, lap_target_s, floor_kmh)
    for _ in range(sweeps):
        improved = False
        for i in range(n):
            for cand in range(0, int(lap), grid):
                trial = sorted(marks[:i] + [float(cand)] + marks[i + 1:])
                if trial == marks or not _gap_ok(trial, lap, min_gap):
                    continue
                r = fit_v_off(p, course, trial, lap_target_s, floor_kmh)
                if r is not None and (best is None or r[1] < best[1] - 1e-4):
                    marks, best, improved = trial, r, True
        if not improved:
            break
    return (marks, best[0]) if best is not None else None


def _marks_text(marks, v_off: float) -> str:
    return "位置 [%s] m で開始 → %.1f km/h で停止" % (", ".join("%.0f" % m for m in marks), v_off)


def _pol_text(pol: dict) -> str:
    if pol.get("marks"):
        return " ／ ".join([_marks_text(pol["marks"], pol["v_off"])]
                          + ["%d 周目から %s" % (lap, _marks_text(ms, v))
                             for lap, v, ms in pol.get("switches", ())])
    return "ON %.0f / OFF %.0f km/h  禁止 %d-%d m" % (
        pol["v_on"], pol["v_off"], pol["zone"][0], pol["zone"][1])


def _line(r: dict, total_m: float) -> str:
    return ("%s  %s〜%s (平均 %.2f km/h 以上)  燃料 %.2f ml  加速 %d 回"
            % (_pol_text(r["pol"]), mmss(r["time_min_s"]), mmss(r["time_s"]),
               total_m / r["time_s"] * 3.6, r["fuel_ml"], r["burns"]))


def _shift_v_off(pol: dict, dv: float) -> dict:
    """停止速度を全周で dv km/h ずらした pol。"""
    return dict(pol, v_off=pol["v_off"] + dv,
                switches=[(lap, v + dv, ms) for lap, v, ms in pol.get("switches", ())])


def _report(p, course, rec, args, limit, what: str = "推奨パターン") -> float:
    pol = rec["pol"]
    trace = [] if args.trace else None
    r = simulate(p, course, pol["v_off"], marks=pol.get("marks", ()), v_on=pol.get("v_on"),
                 zone=pol.get("zone"), switches=pol.get("switches", ()), trace=trace)
    cum = [tl for tl, _, _ in r["crossings"]]
    print("\n%sをずらさずに走った場合（スタートラインからの距離 m @ 加速開始時の速度 km/h）:" % what)
    prev = 0.0
    for n, tl in enumerate(cum, 1):
        st = ["%4.0f@%2.0f" % (pos, kmh) for lp, pos, kmh in r["starts"] if lp == n]
        print("  %d 周目  %s  累積 %s  加速開始 %s" % (n, mmss(tl - prev), mmss(tl), "  ".join(st)))
        prev = tl
    print("  合計 %s  燃料 %.2f ml  ゴール通過 %.1f km/h  発進後の最低速度 %.1f km/h"
          % (mmss(r["time_s"]), r["fuel_ml"], r["finish_kmh"], r["min_kmh"]))

    print("\n停止速度が系統的にずれたときの合計時間（補正なし）:")
    for dv in (-1.0, -0.5, 0.5, 1.0):
        q = evaluate(p, course, _shift_v_off(pol, dv))
        print("  %+.1f km/h → %s (%+.0f s)  燃料 %.2f ml"
              % (dv, mmss(q["time_s"]), q["time_s"] - rec["time_s"], q["fuel_ml"]))

    print("\n感度（補正なし → 周回通過タイムで停止速度を補正。時間は 3 通りの最長）:")
    slow = {"crr_scale": 1.2, "k_scale": 1.2}
    cases = [("基準", {}),
             ("ドラム損失 上乗せ 0", {"beta": 0.0}),
             ("ドラム損失 上乗せ 1", {"beta": 1.0}),
             ("走行抵抗 +20%", slow),
             ("走行抵抗 -20%", {"crr_scale": 0.8, "k_scale": 0.8}),
             ("再始動失敗 %d 回" % args.fails, {"fail_restarts": args.fails}),
             ("抵抗 +20%% かつ失敗 %d 回" % args.fails, dict(slow, fail_restarts=args.fails)),
             ("抵抗 +20% かつ上乗せ 0", dict(slow, beta=0.0)),
             ("停止速度 -1 km/h", {"_dv": -1.0}),
             ("停止速度 -1 km/h かつ抵抗 +20%", dict(slow, _dv=-1.0))]
    worst = 0.0
    for name, kw in cases:
        kw = dict(kw)
        q_pol = _shift_v_off(pol, kw.pop("_dv", 0.0))
        o = evaluate(p, course, q_pol, **kw)
        q = evaluate(p, course, q_pol, pace=cum, **kw)
        worst = max(worst, q["time_s"])
        print("  %-26s %s → %s  規定まで %+4.0f s  燃料 %5.2f ml  加速 %d 回  最低 %.1f km/h"
              % (name, mmss(o["time_s"]), mmss(q["time_s"]), limit - q["time_s"],
                 q["fuel_ml"], q["burns"], q["min_kmh"]))
    if args.trace:
        with open(args.trace, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["t_s", "dist_m", "lap", "pos_m", "speed_kmh", "state", "fuel_ml"])
            w.writerows(trace)
        print("\n速度トレース: %s" % args.trace)
    return worst


def cmd_plan(args) -> int:
    """パラメータの phases に書いたプラン（周ごとのパターン）を評価する。探索はしない。"""
    p = load_params(args.params)
    course = course_from_params(p, args.waypoints)
    c = p["course"]
    try:
        pol = plan_from_params(p)
    except ValueError as e:
        print(e)
        return 1
    if pol is None:
        print("パラメータに phases がありません: %s" % args.params)
        return 1
    limit = c["limit_s"]
    rec = evaluate(p, course, pol)
    print("規定 %s / %d 周 %.2f m / 保険の加速開始 %.0f km/h"
          % (mmss(limit), c["laps"], c["total_m"], pol["v_on"]))
    print("（時間は開始位置を ±30 m ずらした 3 通りの最短〜最長、燃料は平均、加速回数は最大）")
    print("  %s  最低 %.1f km/h" % (_line(rec, c["total_m"]), rec["min_kmh"]))
    worst = _report(p, course, rec, args, limit, "プラン")
    return 0 if worst <= limit else 2


def cmd_optimize(args) -> int:
    p = load_params(args.params)
    course = course_from_params(p, args.waypoints)
    c = p["course"]
    target = parse_mmss(args.target) if args.target else p["target_s"]
    limit, guard = c["limit_s"], c.get("guard_kmh", 16.0)
    # 発進周と惰行で終える最終周は定常周回より遅いので、定常周回はその分だけ詰めておく
    lap_target = target / c["laps"] * 0.97
    results = []
    for n in range(args.n_min, args.n_max + 1):
        placed = place_marks(p, course, n, lap_target, guard + 1.0)
        if placed is None:
            print("1 周 %d 回: 最低速度 %.0f km/h 以上を保てる配置がありません" % (n, guard + 1.0))
            continue
        marks, v_off = placed
        v_off = math.ceil(v_off * 2) / 2 - 1.0  # 0.5 km/h 刻み。少し下から上げて目標に合わせる
        for _ in range(12):
            r = evaluate(p, course, {"v_off": v_off, "marks": marks, "v_on": guard})
            if r["time_s"] <= target:
                break
            v_off += 0.5
        results.append(r)
    best, rec = pick(results, target, args.fuel_slack)
    print("目標 %s / 規定 %s / %d 周 %.2f m / 保険の加速開始 %.0f km/h"
          % (mmss(target), mmss(limit), c["laps"], c["total_m"], guard))
    print("（時間は開始位置を ±30 m ずらした 3 通りの最短〜最長、燃料は平均、加速回数は最大）")
    for r in results:
        print("  1 周 %d 回: %s  最低 %.1f km/h"
              % (len(r["pol"]["marks"]), _line(r, c["total_m"]), r["min_kmh"]))
    if rec is None:
        print("目標 %s に収まる組がありません" % mmss(target))
        return 1
    print("燃料最小: 1 周 %d 回" % len(best["pol"]["marks"]))
    print("推奨    : 1 周 %d 回（燃料最小から %.0f%% 以内で加速回数が最少）"
          % (len(rec["pol"]["marks"]), args.fuel_slack * 100))
    worst = _report(p, course, rec, args, limit)
    return 0 if worst <= limit else 2


def cmd_speed_rule(args) -> int:
    """位置を使わず速度だけで判断する規則の格子探索（位置同期との比較用）。"""
    p = load_params(args.params)
    course = course_from_params(p, args.waypoints)
    c = p["course"]
    target = parse_mmss(args.target) if args.target else p["target_s"]
    results = []
    for zone in (tuple(z) for z in c["no_burn_zones_m"]):
        for v_on in range(args.on_min, args.on_max + 1):
            for v_off in range(args.off_min, args.off_max + 1):
                if v_off - v_on >= 6:
                    results.append(evaluate(p, course, {"v_off": float(v_off), "v_on": float(v_on),
                                                        "zone": zone}))
    best, rec = pick(results, target, args.fuel_slack)
    print("目標 %s / 規定 %s" % (mmss(target), mmss(c["limit_s"])))
    print("（時間は開始速度を ±0.5 km/h ずらした 3 通りの最短〜最長、燃料は平均、加速回数は最大）")
    if rec is None:
        print("目標に収まる組がありません")
        return 1
    for r in sorted((r for r in results if r["time_s"] <= target), key=lambda r: r["fuel_ml"])[:args.top]:
        print("  " + _line(r, c["total_m"]))
    spread = statistics.fmean(r["time_s"] - r["time_min_s"] for r in results)
    print("ずらしたときの合計時間の振れ（全組の平均）: %.0f s" % spread)
    return 0


# ---------------------------------------------------------------------------
# replay: 惰行モデルを実走ログで検証
# ---------------------------------------------------------------------------

def replay(rows: list, course: Course, crr: float, k: float, min_s: float = 20.0) -> list:
    """各惰行区間を実測の初速から同じ距離だけ惰行させ、所要時間と終速を実測と比べる。"""
    out = []
    for seg in _split(rows, lambda r: r["rpm"] == 0 and r["v"] >= 14.0, gap_s=5.0):
        seg = [r for r in seg if r["t"] - seg[0]["t"] >= 3.0]
        if len(seg) < 2 or seg[-1]["t"] - seg[0]["t"] < min_s:
            continue
        a, b = seg[0], seg[-1]
        s0, _, off = course.nearest(a["lat"], a["lng"])
        if off > 30.0:
            continue
        dist = b["dist"] - a["dist"]
        s, v, t = s0, a["v"] / 3.6, 0.0
        while s - s0 < dist and v > 0.5:
            s, v = coast_step(course, s, v, crr, k, 0.1)
            t += 0.1
        out.append({"pos": s0, "dist": dist, "v0": a["v"], "t_meas": b["t"] - a["t"],
                    "t_sim": t, "v_meas": b["v"], "v_sim": v * 3.6})
    return out


def cmd_replay(args) -> int:
    p = load_params(args.params)
    course = course_from_params(p, args.waypoints)
    crr = args.crr if args.crr is not None else p["vehicle"]["crr"]
    k = args.k if args.k is not None else p["vehicle"]["k_aero"]
    rc = 0
    for path in args.logs:
        res = replay(load_log(path), course, crr, k)
        if not res:
            print("%s: 惰行区間なし" % os.path.basename(path))
            rc = 1
            continue
        print("%s (Crr=%.5f k=%.6f)" % (os.path.basename(path), crr, k))
        print("  位置 m  距離 m  初速  時間 実測/計算 s   終速 実測/計算 km/h")
        for r in res:
            print("  %5.0f  %5.0f  %4.1f  %6.1f / %6.1f     %4.1f / %4.1f"
                  % (r["pos"], r["dist"], r["v0"], r["t_meas"], r["t_sim"], r["v_meas"], r["v_sim"]))
        tm = sum(r["t_meas"] for r in res)
        ts = sum(r["t_sim"] for r in res)
        ev = math.sqrt(statistics.fmean((r["v_sim"] - r["v_meas"]) ** 2 for r in res))
        print("  惰行 %d 区間  合計時間 実測 %.0f s / 計算 %.0f s (%+.1f%%)  終速の RMS 誤差 %.1f km/h"
              % (len(res), tm, ts, (ts - tm) / tm * 100, ev))
    return rc


# ---------------------------------------------------------------------------
# map: 加速開始位置のコース図（SVG）
# ---------------------------------------------------------------------------

def burn_spans(p: dict, course: Course, marks, v_off: float) -> list:
    """定常周回（3 周目）で各加速が続く区間 [(開始 m, 終了 m, 開始速度 km/h)]。"""
    trace = []
    simulate(p, course, v_off, marks=marks, laps=4, trace=trace, trace_every=1)
    spans, cur = [], None
    for _, _, lap, pos, kmh, state, _ in trace:
        if lap != 3:
            continue
        if state == "burn":
            cur = [pos, pos, kmh] if cur is None else [cur[0], pos, cur[2]]
        elif cur is not None:
            spans.append(tuple(cur))
            cur = None
    return spans


_SLOPE_STYLE = (("up", "#d97706", "登り"), ("flat", "#9ca3af", "平坦"), ("down", "#2563eb", "下り（加速しない）"))
_BURN_COLOR = "#dc2626"


def course_svg(course: Course, marks, spans, v_off: float, width: int = 900) -> str:
    """北を上にしたコース図。勾配で色分けし、加速開始位置と加速区間を重ねる。"""
    pad_x, top, bottom = 200, 80, 130
    lat0, lng0 = min(course.lat), min(course.lng)
    mx = [(g - lng0) * 111320.0 * course._coslat for g in course.lng]
    my = [(a - lat0) * 111320.0 for a in course.lat]
    scale = (width - 2 * pad_x) / max(mx)
    height = int(max(my) * scale + top + bottom)

    def px(lat, lng):
        return (pad_x + (lng - lng0) * 111320.0 * course._coslat * scale,
                top + (max(my) - (lat - lat0) * 111320.0) * scale)

    def at(s):
        return px(*course.latlng_at(s))

    def outward(s, dist):
        # 左回りなので進行方向の右手がコースの外側（画面座標は y が下向き）
        (x0, y0), (x1, y1), (x, y) = at(s - 6), at(s + 6), at(s)
        d = math.hypot(x1 - x0, y1 - y0)
        return x - (y1 - y0) / d * dist, y + (x1 - x0) / d * dist

    def label(s, dist, text, size=15, weight="normal", fill="#111827"):
        x, y = outward(s, dist)
        ox = (x - at(s)[0]) / abs(dist)
        anchor = "start" if ox > 0.35 else "end" if ox < -0.35 else "middle"
        if dist < 0:
            anchor = {"start": "end", "end": "start"}.get(anchor, anchor)
        return ('<text x="%.1f" y="%.1f" text-anchor="%s" dominant-baseline="middle" font-size="%d" '
                'font-weight="%s" fill="%s" stroke="#ffffff" stroke-width="4" paint-order="stroke">%s</text>'
                % (x, y, anchor, size, weight, fill, text))

    def slope_class(s):
        # 標高は GPS 由来で細かく波打つので、前後 150 m の平均で見る
        m = statistics.fmean(course.slope(s + d) for d in range(-150, 151, 10))
        return "up" if m > 0.0015 else "down" if m < -0.0015 else "flat"

    def poly(s0, s1, color, w):
        n = max(int((s1 - s0) / 5), 1)
        pts = " ".join("%.1f,%.1f" % at(s0 + (s1 - s0) * i / n) for i in range(n + 1))
        return ('<polyline points="%s" fill="none" stroke="%s" stroke-width="%s" '
                'stroke-linecap="round" stroke-linejoin="round"/>' % (pts, color, w))

    color = {k: c for k, c, _ in _SLOPE_STYLE}
    out = ['<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 %d %d" width="%d" height="%d" '
           'font-family="sans-serif">' % (width, height, width, height),
           '<rect width="100%" height="100%" fill="#ffffff"/>',
           '<text x="%d" y="34" text-anchor="middle" font-size="20" font-weight="bold" fill="#111827">'
           'もてぎオーバル 加速開始位置（左回り・北が上）</text>' % (width / 2)]

    # 勾配で色分けしたコース
    runs, start, cls = [], 0, slope_class(0)
    for s_m in range(5, int(course.lap_m) + 5, 5):
        c = slope_class(s_m) if s_m < course.lap_m else None
        if c != cls:
            runs.append((start, min(s_m, course.lap_m), cls))
            start, cls = s_m, c
    for s0, s1, c in runs:
        out.append(poly(s0, s1, color[c], 9))
    # 登りは開始位置のラベルと重ならないよう内側、下りは外側へ置く
    for c, text, dist in (("up", "登り", -34), ("down", "下り（加速しない）", 30)):
        s0, s1, _ = max((r for r in runs if r[2] == c), key=lambda r: r[1] - r[0])
        out.append(label((s0 + s1) / 2, dist, text, size=14, fill=color[c], weight="bold"))

    # 進行方向
    for s_m in (130.0, 1250.0):
        (x0, y0), (x1, y1) = at(s_m), at(s_m + 12)
        ang = math.degrees(math.atan2(y1 - y0, x1 - x0))
        out.append('<polygon points="9,0 -7,-7 -7,7" fill="#111827" transform="translate(%.1f,%.1f) rotate(%.1f)"/>'
                   % (x0, y0, ang))

    # スタート / ゴールライン
    (ax, ay), (bx, by) = outward(0, -16), outward(0, 16)
    out.append('<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" stroke="#111827" stroke-width="4"/>' % (ax, ay, bx, by))
    out.append(label(0, 30, "スタート / ゴール（0 m）", weight="bold"))

    # 加速区間と加速開始位置
    for s0, s1, _ in spans:
        out.append(poly(s0, s1, "#ffffff", 15))
        out.append(poly(s0, s1, _BURN_COLOR, 9))
    for n, m in enumerate(sorted(marks), 1):
        x, y = at(m)
        out.append('<circle cx="%.1f" cy="%.1f" r="11" fill="%s" stroke="#ffffff" stroke-width="3"/>' % (x, y, _BURN_COLOR))
        out.append('<text x="%.1f" y="%.1f" text-anchor="middle" dominant-baseline="central" font-size="13" '
                   'font-weight="bold" fill="#ffffff">%d</text>' % (x, y, n))
        span = next((sp for sp in spans if abs(sp[0] - m) < 60), None)
        out.append(label(m, 34, "%d: %.0f m（ID %d）" % (n, m, course.nearest_id(m)), weight="bold"))
        if span:
            lx, ly = outward(m, 34)
            ox = (lx - x) / 34
            anchor = "start" if ox > 0.35 else "end" if ox < -0.35 else "middle"
            out.append('<text x="%.1f" y="%.1f" text-anchor="%s" dominant-baseline="middle" font-size="13" '
                       'fill="#374151" stroke="#ffffff" stroke-width="4" paint-order="stroke">'
                       '約 %.0f → %.1f km/h、%.0f m</text>' % (lx, ly + 19, anchor, span[2], v_off, span[1] - span[0]))

    # 方位・縮尺・凡例
    nx, ny = width - 50, top + 10
    out.append('<polygon points="%d,%d %d,%d %d,%d" fill="#111827"/>' % (nx, ny - 22, nx - 8, ny, nx + 8, ny))
    out.append('<text x="%d" y="%d" text-anchor="middle" font-size="14" font-weight="bold" fill="#111827">N</text>' % (nx, ny + 17))
    bx0, by0 = 40, height - 34
    out.append('<line x1="%d" y1="%d" x2="%.1f" y2="%d" stroke="#111827" stroke-width="3"/>' % (bx0, by0, bx0 + 200 * scale, by0))
    for x in (bx0, bx0 + 200 * scale):
        out.append('<line x1="%.1f" y1="%d" x2="%.1f" y2="%d" stroke="#111827" stroke-width="3"/>' % (x, by0 - 6, x, by0 + 6))
    out.append('<text x="%.1f" y="%d" text-anchor="middle" font-size="13" fill="#111827">200 m</text>' % (bx0 + 100 * scale, by0 - 10))
    lx, ly = width - 250, height - 96
    items = [(c, t, 9) for _, c, t in _SLOPE_STYLE] + [(_BURN_COLOR, "加速区間（丸印が開始位置）", 9)]
    for i, (c, t, w) in enumerate(items):
        y = ly + i * 22
        out.append('<line x1="%d" y1="%d" x2="%d" y2="%d" stroke="%s" stroke-width="%d" stroke-linecap="round"/>' % (lx, y, lx + 34, y, c, w))
        out.append('<text x="%d" y="%d" dominant-baseline="middle" font-size="13" fill="#111827">%s</text>' % (lx + 46, y, t))
    out.append("</svg>")
    return "\n".join(out) + "\n"


def cmd_map(args) -> int:
    p = load_params(args.params)
    course = course_from_params(p, args.waypoints)
    spans = burn_spans(p, course, args.marks, args.v_off)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(course_svg(course, args.marks, spans, args.v_off))
    print("コース図: %s" % args.out)
    print("番号 | 位置 m | ID | 緯度, 経度 | 加速区間 m")
    for n, m in enumerate(sorted(args.marks), 1):
        lat, lng = course.latlng_at(m)
        span = next((sp for sp in spans if abs(sp[0] - m) < 60), None)
        print("  %d | %4.0f | %3d | %.6f, %.6f | %s"
              % (n, m, course.nearest_id(m), lat, lng,
                 "%.0f〜%.0f（%.0f km/h から）" % span if span else "-"))
    return 0


# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="もてぎオーバル 加速・惰行シミュレータ")
    ap.add_argument("--params", default=DEFAULT_PARAMS, help="パラメータ JSON")
    ap.add_argument("--waypoints", help="ウェイポイント CSV（既定は JSON の指定）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("fit-coast", help="実走ログの惰行から Crr と k を推定")
    s.add_argument("logs", nargs="+")
    s.set_defaults(func=cmd_fit_coast)

    s = sub.add_parser("fit-grade", help="惰行の減速度と標高の測定値から周回の標高を推定")
    s.add_argument("logs", nargs="+", help="惰行を含むログ（rpm=0 の行を惰行とみなす）")
    s.add_argument("--groups", type=int, nargs="+",
                   help="ログごとの車両番号（同じ番号は Crr・k を共有）。既定はログごとに別")
    s.add_argument("--alt-csv", nargs="+", help="別の測定の周平均標高（列 s, alt）")
    s.add_argument("--sigma-wp", type=float, default=0.5, help="ウェイポイント標高の誤差の目安 m")
    s.add_argument("--sigma-alt", type=float, default=0.3, help="--alt-csv の誤差の目安 m")
    s.add_argument("--smooth", type=float, default=0.05, help="節点の 2 階差分の許容 m")
    s.add_argument("--node-m", type=float, default=50.0, help="節点の間隔 m")
    s.add_argument("--every", type=int, default=100, help="表の刻み m")
    s.add_argument("--out", help="alt を差し替えたウェイポイント CSV の出力先")
    s.set_defaults(func=cmd_fit_grade)

    s = sub.add_parser("fit-burn", help="ドラムログから加速度・燃料流量を集計")
    s.add_argument("log")
    s.add_argument("--inertia-kg", type=float, default=105.0, help="ドラムの等価慣性質量")
    s.set_defaults(func=cmd_fit_burn)

    s = sub.add_parser("optimize", help="位置同期の加速・惰行パターンを探索")
    s.add_argument("--target", help="目標時間 MM:SS（既定は JSON の target_s）")
    s.add_argument("--n-min", type=int, default=2, help="1 周あたり加速回数の下限")
    s.add_argument("--n-max", type=int, default=5, help="1 周あたり加速回数の上限")
    s.add_argument("--fuel-slack", type=float, default=0.05,
                   help="推奨を選ぶとき、燃料最小からこの割合までは加速回数の少なさを優先する")
    s.add_argument("--fails", type=int, default=3, help="感度に入れる再始動失敗の回数")
    s.add_argument("--trace", help="推奨パターンの速度トレース CSV の出力先")
    s.set_defaults(func=cmd_optimize)

    s = sub.add_parser("plan", help="JSON の phases に書いたプラン（周ごとのパターン）を評価")
    s.add_argument("--fails", type=int, default=3, help="感度に入れる再始動失敗の回数")
    s.add_argument("--trace", help="プランの速度トレース CSV の出力先")
    s.set_defaults(func=cmd_plan)

    s = sub.add_parser("speed-rule", help="速度だけで判断する規則の格子探索（比較用）")
    s.add_argument("--target", help="目標時間 MM:SS（既定は JSON の target_s）")
    s.add_argument("--on-min", type=int, default=16)
    s.add_argument("--on-max", type=int, default=24)
    s.add_argument("--off-min", type=int, default=28)
    s.add_argument("--off-max", type=int, default=38)
    s.add_argument("--fuel-slack", type=float, default=0.05)
    s.add_argument("--top", type=int, default=10)
    s.set_defaults(func=cmd_speed_rule)

    s = sub.add_parser("map", help="加速開始位置のコース図（SVG）を出力")
    s.add_argument("--marks", type=float, nargs="+", required=True, help="加速開始位置 m")
    s.add_argument("--v-off", type=float, required=True, help="停止速度 km/h")
    s.add_argument("--out", required=True, help="出力先 .svg")
    s.set_defaults(func=cmd_map)

    s = sub.add_parser("replay", help="惰行モデルを実走ログで検証")
    s.add_argument("logs", nargs="+")
    s.add_argument("--crr", type=float)
    s.add_argument("--k", type=float)
    s.set_defaults(func=cmd_replay)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
