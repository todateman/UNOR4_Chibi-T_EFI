#!/usr/bin/env python3
"""インジェクタ流量試験のログCSVから、流量と無効噴射時間（実効オフセット）を求める。

  python tools/inj_analyze.py document/injector_test_log_20260822.csv
  python tools/inj_analyze.py log.csv --pw-min 1 --pw-max 6 --plot fit.png --json fit.json

モデル（1バーストの総質量 m[mg]、発数 N、パルス幅 PW[ms]）:
    m = a·N·(PW − t0) + C  =  a·(N·PW) + b·N + C        （t0 = −b/a）
  a  : 流量 [mg/ms]
  t0 : 実効オフセット [ms]。正なら通常の無効噴射時間。
       負なら「閉弁遅れ > 開弁遅れ」（指令より長く噴いている）で、ECUでは符号付きで扱う。
  C  : バーストごとの固定量 [mg]（捨て打ち後の残り、垂れ、計量時の付着など PW・N によらない分）。
       N が全行で同じログでは b と分離できないので C=0 として扱い、その旨を表示する。

第1版の手書きログ（injector_test_log_20260822.csv）と、inj_bench.py が出す第2版ログの両方を読める。
第2版では指令幅ではなく実測幅（実測幅_平均_us）と実発数を使う。numpy が必要（--plot は matplotlib も）。
"""
import argparse
import csv
import json
import sys

import numpy as np


def fnum(s):
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def load(paths, exclude_notes):
    rows = []
    for path in paths:
        with open(path, encoding="utf-8-sig", newline="") as f:
            for r in csv.DictReader(f):
                mass = fnum(r.get("噴射質量_g"))
                if mass is None:
                    continue
                if (r.get("中断") or "none") != "none":
                    continue
                note = r.get("備考") or ""
                if any(x and x in note for x in exclude_notes):
                    continue
                w_us = fnum(r.get("実測幅_平均_us"))
                pw_cmd = fnum(r.get("パルス幅_ms"))
                n = fnum(r.get("実発数")) or fnum(r.get("パルス数_N"))
                if pw_cmd is None or not n:
                    continue
                rows.append({
                    "no": r.get("No", ""), "phase": r.get("フェーズ") or "",
                    "pw_cmd": pw_cmd, "pw": w_us / 1000.0 if w_us else pw_cmd,
                    "n": n, "mg": mass * 1000.0,
                    "p": fnum(r.get("燃圧平均_MPa")) or fnum(r.get("燃圧_MPa")),
                    "v": fnum(r.get("オシロ_電源電圧_V")) or fnum(r.get("バッテリー電圧_V")),
                })
    return rows


def fit(rows, with_c):
    """最小二乗で (a, b, C) を求める。戻り値: 係数, 共分散, 残差標準偏差[mg]。"""
    n = np.array([r["n"] for r in rows])
    pw = np.array([r["pw"] for r in rows])
    y = np.array([r["mg"] for r in rows])
    cols = [n * pw, n] + ([np.ones_like(n)] if with_c else [])
    x = np.column_stack(cols)
    coef, *_ = np.linalg.lstsq(x, y, rcond=None)
    res = y - x @ coef
    dof = max(1, len(y) - x.shape[1])
    s2 = float(res @ res) / dof
    cov = s2 * np.linalg.inv(x.T @ x)
    if not with_c:
        coef = np.append(coef, 0.0)
        cov = np.pad(cov, ((0, 1), (0, 1)))
    return coef, cov, s2 ** 0.5


def bootstrap_t0(rows, with_c, iters=2000, seed=1):
    rng = np.random.default_rng(seed)
    vals = []
    for _ in range(iters):
        pick = [rows[i] for i in rng.integers(0, len(rows), len(rows))]
        if len({r["pw_cmd"] for r in pick}) < 2:
            continue
        try:
            c, _, _ = fit(pick, with_c)
        except np.linalg.LinAlgError:
            continue
        if c[0] > 0:
            vals.append(-c[1] / c[0])
    if len(vals) < iters // 2:
        return None
    return float(np.percentile(vals, 2.5)), float(np.percentile(vals, 97.5))


def group_mean(values, key, tol):
    """key の値が tol 以内のものをまとめ、(代表値, [行...]) のリストを返す。"""
    groups = []
    for r in sorted(values, key=lambda r: r[key]):
        if groups and abs(r[key] - groups[-1][0]) <= tol:
            groups[-1][1].append(r)
        else:
            groups.append([r[key], [r]])
    return [(float(np.mean([r[key] for r in g])), g) for _, g in groups]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv", nargs="+", help="ログCSV（複数可）")
    ap.add_argument("--pw-min", type=float, default=1.0, help="直線回帰に使うパルス幅の下限 ms（既定 1.0）")
    ap.add_argument("--pw-max", type=float, default=6.0, help="直線回帰に使うパルス幅の上限 ms（既定 6.0）")
    ap.add_argument("--base-phases", default="P1,P2,P5",
                    help="回帰に使うフェーズ（前方一致、カンマ区切り）。フェーズ列が空の行は常に使う")
    ap.add_argument("--no-burst-offset", action="store_true", help="バースト固定量 C を 0 に固定する")
    ap.add_argument("--pnorm", type=float, default=None, metavar="MPa",
                    help="燃圧をこの値に換算してから回帰する（質量に √(Pref/P) を掛ける）")
    ap.add_argument("--density", type=float, default=0.754, help="燃料密度 g/ml（ml換算用）")
    ap.add_argument("--exclude-note", action="append", default=["容積オーバー"],
                    help="備考にこの文字列を含む行を除外（複数指定可。既定: 容積オーバー）")
    ap.add_argument("--vtol", type=float, default=0.25, help="同じ電圧水準とみなす幅 V")
    ap.add_argument("--plot", default=None, help="回帰と残差のグラフをPNGで保存")
    ap.add_argument("--json", default=None, help="結果をJSONで保存")
    a = ap.parse_args()

    rows = load(a.csv, a.exclude_note)
    if not rows:
        sys.exit("有効な行がありません（噴射質量_g が空、または中断・除外のみ）")
    if a.pnorm:
        for r in rows:
            if r["p"]:
                r["mg"] *= (a.pnorm / r["p"]) ** 0.5

    bases = [p.strip() for p in a.base_phases.split(",") if p.strip()]
    is_base = lambda r: (not r["phase"]) or any(r["phase"].startswith(p) for p in bases)
    base = [r for r in rows if is_base(r)]
    lin = [r for r in base if a.pw_min - 1e-9 <= r["pw_cmd"] <= a.pw_max + 1e-9]
    if len({r["pw_cmd"] for r in lin}) < 2:
        sys.exit(f"直線回帰に使える水準が足りません（{a.pw_min}〜{a.pw_max}ms に2水準以上必要）")

    # C（バースト固定量）を分離できるのは、N と N·PW が独立に振られているときだけ
    n_levels = len({r["n"] for r in lin})
    with_c = (not a.no_burst_offset) and n_levels >= 2 and len(lin) >= 6
    if with_c:
        x = np.column_stack([[r["n"] * r["pw"] for r in lin], [r["n"] for r in lin], np.ones(len(lin))])
        if np.linalg.cond(x / np.abs(x).max(axis=0)) > 1e4:
            with_c = False
    (ka, kb, kc), cov, rms = fit(lin, with_c)
    t0 = -kb / ka
    # デルタ法による t0 の標準誤差
    g = np.array([kb / ka ** 2, -1.0 / ka, 0.0])
    t0_se = float(np.sqrt(g @ cov @ g))
    ci = bootstrap_t0(lin, with_c)

    out = {
        "n_rows_fit": len(lin), "pw_range_ms": [a.pw_min, a.pw_max], "burst_offset_fitted": with_c,
        "flow_mg_per_ms": round(float(ka), 5), "flow_g_per_min": round(float(ka) * 60, 3),
        "flow_ml_per_min": round(float(ka) * 60 / a.density, 3),
        "t0_ms": round(float(t0), 4), "t0_se_ms": round(t0_se, 4),
        "t0_ci95_ms": [round(ci[0], 4), round(ci[1], 4)] if ci else None,
        "burst_offset_mg": round(float(kc), 2) if with_c else None,
        "burst_offset_se_mg": round(float(np.sqrt(cov[2, 2])), 2) if with_c else None,
        "rms_mg_per_burst": round(float(rms), 2),
    }

    print(f"== 直線回帰（{a.pw_min:g}〜{a.pw_max:g}ms, {len(lin)}行"
          + (f", 燃圧 {a.pnorm}MPa 換算" if a.pnorm else "") + "）==")
    print(f"流量 a          : {ka:.4f} mg/ms = {ka * 60:.2f} g/min = {ka * 60 / a.density:.2f} ml/min")
    print(f"実効オフセット t0: {t0:+.3f} ms（標準誤差 {t0_se:.3f}"
          + (f", 95%区間 {ci[0]:+.3f}〜{ci[1]:+.3f}" if ci else "") + "）")
    if with_c:
        print(f"バースト固定量 C : {kc:+.1f} mg（標準誤差 {np.sqrt(cov[2, 2]):.1f}）")
    else:
        why = "指定により0固定" if a.no_burst_offset else "Nの水準が1つ、または条件が悪く分離できないため0として計算"
        print(f"バースト固定量 C : 未推定（{why}）")
        if not a.no_burst_offset:
            print("  → 固定量があると t0 に混ざります。同じPWでNを振った行（P2）を加えると分離できます")
    print(f"残差の標準偏差   : {rms:.1f} mg/バースト")
    if t0 < 0:
        print("※ t0 が負: 指令幅より長く噴いている形です（閉弁遅れ > 開弁遅れ、またはドライバのOFF遅れ）。")
        print("  オシロの フライバック継続・電流減衰・OFF遅れ の列と突き合わせてください。")

    # --- 水準別の表（短パルスの補正量を含む）---
    print("\n== パルス幅ごとの結果（回帰に使うフェーズの全行）==")
    print(" PW指令  実測PW   行数  1発あたり[mg]  ばらつき[%]  直線予測[mg]  等価PW補正[ms]  回帰")
    levels = []
    for pw_cmd, grp in group_mean(base, "pw_cmd", 1e-6):
        per = np.array([(r["mg"] - kc) / r["n"] for r in grp])
        pw_act = float(np.mean([r["pw"] for r in grp]))
        pred = ka * (pw_act - t0)
        adder = float(per.mean() / ka - (pw_act - t0))   # 直線モデルに足すべき等価パルス幅
        cv = float(per.std(ddof=1) / per.mean() * 100) if len(per) > 1 else float("nan")
        used = a.pw_min - 1e-9 <= pw_cmd <= a.pw_max + 1e-9
        print(f"{pw_cmd:7.3f} {pw_act:7.3f} {len(grp):5d} {per.mean():13.3f} {cv:11.1f} {pred:12.3f} "
              f"{adder:+14.3f}  {'○' if used else '－'}")
        levels.append({"pw_cmd_ms": pw_cmd, "pw_ms": round(pw_act, 4), "rows": len(grp),
                       "mg_per_shot": round(float(per.mean()), 4), "cv_pct": round(cv, 2),
                       "pw_adder_ms": round(adder, 4), "in_fit": used})
    out["levels"] = levels
    print("  等価PW補正: 回帰範囲外（短パルス・長パルス）で、直線モデルからのずれをパルス幅に換算した値。")

    # --- 電圧別（a を共通として t0 を水準ごとに計算）---
    volt_rows = [r for r in rows if r["v"] and r["phase"].startswith("P3")] or \
                [r for r in rows if r["v"]]
    vgroups = group_mean(volt_rows, "v", a.vtol) if volt_rows else []
    if len(vgroups) >= 2:
        print("\n== 電圧ごとの実効オフセット（流量 a は共通として計算）==")
        print("  電圧[V]  行数   t0[ms]   ばらつき[ms]")
        table = []
        for v, grp in vgroups:
            t = np.array([r["pw"] - (r["mg"] - kc) / (r["n"] * ka) for r in grp])
            sd = float(t.std(ddof=1)) if len(t) > 1 else float("nan")
            print(f"{v:8.2f} {len(grp):5d} {t.mean():+8.3f} {sd:10.3f}")
            table.append({"v": round(v, 2), "rows": len(grp), "t0_ms": round(float(t.mean()), 4)})
        out["t0_by_voltage"] = table

    # --- 燃圧別（t0 を共通として流量を水準ごとに計算）---
    pres_rows = [r for r in rows if r["p"] and r["phase"].startswith("P4")]
    pgroups = group_mean(pres_rows, "p", 0.012) if pres_rows else []
    if len(pgroups) >= 2 and not a.pnorm:
        print("\n== 燃圧ごとの流量（t0 は共通として計算）==")
        print("  燃圧[MPa] 行数  流量[mg/ms]  √P則の予測[mg/ms]")
        pref = float(np.mean([r["p"] for r in lin if r["p"]])) if any(r["p"] for r in lin) else None
        table = []
        for p, grp in pgroups:
            q = np.array([(r["mg"] - kc) / (r["n"] * (r["pw"] - t0)) for r in grp])
            pred = ka * (p / pref) ** 0.5 if pref else float("nan")
            print(f"{p:9.3f} {len(grp):5d} {q.mean():11.4f} {pred:16.4f}")
            table.append({"p_mpa": round(p, 4), "rows": len(grp), "flow_mg_per_ms": round(float(q.mean()), 5)})
        out["flow_by_pressure"] = table

    print("\n== ECU（gasml計算）に入れる値 ==")
    print(f"  flow_ml_per_us = {ka / a.density / 1e6:.4e}   // {ka:.4f} mg/ms ÷ 密度{a.density} ÷ 1e6")
    print(f"  t_offset_us    = {t0 * 1000:+.0f}            // 符号付き。噴射量 = flow × (PW − t_offset)")
    if not with_c or t0 < 0:
        print("  ※ まだ暫定値です。固定量 C の分離と、負のオフセットの原因確認が済んでから反映してください。")

    if a.json:
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=1)
    if a.plot:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(7, 7), sharex=True, height_ratios=[3, 1.4])
        pw = np.array([r["pw"] for r in base])
        per = np.array([(r["mg"] - kc) / r["n"] for r in base])
        used = np.array([a.pw_min - 1e-9 <= r["pw_cmd"] <= a.pw_max + 1e-9 for r in base])
        ax1.plot(pw[used], per[used], "o", ms=5, label="used in fit")
        ax1.plot(pw[~used], per[~used], "o", ms=5, mfc="none", label="outside fit range")
        xs = np.linspace(min(0.0, t0), pw.max() * 1.05, 50)
        ax1.plot(xs, ka * (xs - t0), "-", lw=1, label=f"{ka:.3f} mg/ms x (PW - ({t0:+.3f} ms))")
        ax1.axhline(0, color="0.6", lw=0.8)
        ax1.set_ylabel("mass per shot [mg]")
        ax1.legend(loc="upper left", frameon=False)
        ax1.set_title("Injector flow: mass per shot vs pulse width")
        ax2.plot(pw, per - ka * (pw - t0), "o", ms=4)
        ax2.axhline(0, color="0.6", lw=0.8)
        ax2.set_xlabel("pulse width [ms]")
        ax2.set_ylabel("residual [mg]")
        fig.tight_layout()
        fig.savefig(a.plot, dpi=130)
        print(f"\nグラフ: {a.plot}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
