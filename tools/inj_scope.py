#!/usr/bin/env python3
"""RIGOL DHO804 でインジェクタ1発分の波形を取り、電源電圧・実パルス幅・フライバック・電流を算出する。

inj_bench.py から import して使うほか、単体でも実行できる:
  python tools/inj_scope.py arm --pw-ms 4          # 単発トリガーを設定してアーム
  python tools/inj_scope.py wait                   # トリガー成立まで待つ
  python tools/inj_scope.py read shot.npz          # 波形を保存し、解析結果を表示
  python tools/inj_scope.py analyze shot.npz       # 保存済み波形を再解析（オシロ不要）

チャネル割り当て（既定。--ch-* で変更、0 で未使用）:
  CH1 = ドライバがスイッチしている側のインジェクタ端子。10:1プローブ推奨
        ハイサイド駆動（Chibi-T の実機。--drive high、既定）: インジェクタ+端子。OFF時0V、ON時は電源電圧、
          OFF後は還流ダイオードなどで負にクランプされる
        ローサイド駆動（--drive low）: インジェクタ−端子。OFF時は電源電圧、ON時ほぼ0V、OFF後は正のフライバック
  CH2 = コイル電流（電流プローブ。感度は --cur-a-per-v で指定。例: 100mV/A のプローブを倍率1xで使うなら 10）
  CH3 = INJ_OUT（A0）の指令信号。LOW=ON
  CH4 = 燃圧センサのアナログ出力（0.5V=0MPa, 4.5V=1.0MPa）
トリガーは CH1 の通電開始のエッジ（ハイサイドは立ち上がり、ローサイドは立ち下がり）。時間軸はトリガー点を0とする。
解析は駆動方式を波形から自動で判別する（トリガー前とトリガー直後の電圧を比べる）。

SCPIコマンドは Chibi-T_Furoshiki_AutoAirAdjust の tools/scope_capture.py で実機確認済みのものを使っている。
pyvisa はオシロを操作するときだけ必要（analyze は numpy のみ）。
"""
import argparse
import json
import sys
import time

try:
    import numpy as np
except ImportError:      # 引数定義だけ使う場合（inj_bench.py でオシロなし）は numpy 不要
    np = None

# LAN 直結（Mac 側 192.168.1.1）。macOS + pyvisa-py の USB では波形の読み出しが返ってこないため LAN を既定にする
DEFAULT_RESOURCE = "TCPIP::192.168.1.2::INSTR"
CHUNK = 125000
ROLES = ("inj", "cur", "cmd", "fuel")


def nice_timebase(pw_ms):
    """トリガーが画面中央の前提で、通電後 (pw + 6ms) が収まる s/div を 1-2-5 系列から選ぶ。"""
    need = (pw_ms + 6.0) / 5.0 / 1e3
    for tb in (2e-4, 5e-4, 1e-3, 2e-3, 5e-3, 1e-2, 2e-2):
        if tb >= need:
            return tb
    return 5e-2


class Scope:
    def __init__(self, resource=DEFAULT_RESOURCE, channels=None, trig_level=6.0, drive="high"):
        if drive not in ("high", "low"):
            raise ValueError("drive は high か low")
        self.drive = drive
        if np is None:
            raise RuntimeError("オシロを使うには numpy が必要です（pip install -r tools/requirements.txt）")
        import pyvisa  # オシロを使うときだけ必要
        self.channels = channels or {"inj": 1, "cur": 2, "cmd": 3, "fuel": 4}
        if not self.channels.get("inj"):
            raise ValueError("inj チャネル（インジェクタ−端子）は必須です")
        self.trig_level = trig_level
        rm = pyvisa.ResourceManager()
        self.inst = rm.open_resource(resource)
        self.inst.timeout = 60000
        self.inst.write_termination = "\n"
        self.inst.read_termination = "\n"

    def q(self, cmd):
        return self.inst.query(cmd).strip()

    def idn(self):
        return self.q("*IDN?")

    def arm(self, pw_ms, tb=None):
        tb = tb or nice_timebase(pw_ms)
        w = self.inst.write
        w(f":TIMebase:MAIN:SCALe {tb}")
        w(":TIMebase:MAIN:OFFSet 0")      # 解析はトリガー点が画面中央の前提。手で動かした位置を戻す
        w(":TRIGger:MODE EDGE")
        w(f":TRIGger:EDGE:SOURce CHAN{self.channels['inj']}")
        # 通電開始のエッジ: ハイサイドは 0V→電源電圧、ローサイドは 電源電圧→0V
        w(":TRIGger:EDGE:SLOPe " + ("POSitive" if self.drive == "high" else "NEGative"))
        w(f":TRIGger:EDGE:LEVel {self.trig_level}")
        w(":SINGle")
        time.sleep(0.3)
        return self.q(":TRIGger:STATus?")

    def wait(self, timeout=10.0):
        t0 = time.time()
        while time.time() - t0 < timeout:
            if self.q(":TRIGger:STATus?") == "STOP":
                return True
            time.sleep(0.2)
        return False

    def _depth(self):
        """メモリ長（点数）。読めなければ 1M 点とみなす。"""
        try:
            return int(float(self.q(":ACQuire:MDEPth?")))
        except (ValueError, TypeError):
            return 1000000

    def _fetch(self, ch):
        w = self.inst.write
        depth = self._depth()
        w(f":WAVeform:SOURce CHANnel{ch}")
        w(":WAVeform:MODE RAW")
        w(":WAVeform:FORMat WORD")
        # 前回のSTARt/STOPが残っているとプリアンブルの点数が切れるため、毎回全範囲に戻す。
        # STOP を 1M 固定にすると、メモリ長 10M 以上のとき先頭の 1M 点しか読めない（実機で確認）
        w(":WAVeform:STARt 1")
        w(f":WAVeform:STOP {depth}")
        pre = self.q(":WAVeform:PREamble?").split(",")
        n = int(pre[2])
        vals = []
        s = 1
        while s <= n:
            e = min(s + CHUNK - 1, n)
            w(f":WAVeform:STARt {s}")
            w(f":WAVeform:STOP {e}")
            vals += self.inst.query_binary_values(
                ":WAVeform:DATA?", datatype="H", is_big_endian=False, container=list)
            s = e + 1
        w(":WAVeform:STARt 1")
        w(f":WAVeform:STOP {depth}")
        xinc, xorg = float(pre[4]), float(pre[5])
        yinc, yorg, yref = float(pre[7]), float(pre[8]), float(pre[9])
        volts = (np.asarray(vals, dtype=np.float64) - yorg - yref) * yinc
        return xinc, xorg, volts.astype(np.float32)

    def read(self):
        """使用中の全チャネルを読み、{"dt","t0", 役割名: 電圧配列, "meta": {...}} を返す。"""
        wave = {}
        for role in ROLES:
            ch = self.channels.get(role)
            if not ch:
                continue
            dt, t0, v = self._fetch(ch)
            wave[role] = v
            wave["dt"], wave["t0"] = dt, t0
        wave["meta"] = {
            "idn": self.idn(),
            "tb_scale": self.q(":TIMebase:MAIN:SCALe?"),
            "channels": dict(self.channels),
            "drive": self.drive,
            "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        return wave

    def close(self):
        try:
            self.inst.close()
        except Exception:
            pass


def save_wave(path, wave):
    arrays = {r: wave[r] for r in ROLES if r in wave}
    np.savez_compressed(path, dt=wave["dt"], t0=wave["t0"],
                        meta=json.dumps(wave.get("meta", {}), ensure_ascii=False), **arrays)


def load_wave(path):
    z = np.load(path, allow_pickle=False)
    wave = {"dt": float(z["dt"]), "t0": float(z["t0"]), "meta": json.loads(str(z["meta"]))}
    for r in ROLES:
        if r in z.files:
            wave[r] = z[r]
    return wave


def _smooth(x, n):
    n = max(1, int(n))
    if n == 1:
        return np.asarray(x, dtype=np.float64)
    c = np.cumsum(np.insert(np.asarray(x, dtype=np.float64), 0, 0.0))
    h = n // 2
    idx = np.arange(len(x))
    lo, hi = np.clip(idx - h, 0, len(x)), np.clip(idx + h + 1, 0, len(x))
    return (c[hi] - c[lo]) / (hi - lo)


def _first_cross(x, th, start, below):
    """start 以降で、x が th を下回る(below=True)/上回る 最初のインデックス。なければ None。"""
    seg = x[start:]
    hit = np.nonzero(seg < th if below else seg > th)[0]
    return int(start + hit[0]) if len(hit) else None


def analyze(wave, cur_a_per_v=1.0, fuel_v0=0.5, fuel_v_full=4.5, fuel_mpa_full=1.0, drive="auto"):
    """1発分の波形から特性値を出す。時刻はトリガー点を0とする ms。得られなかった値は None。

    drive: "high"（ハイサイド駆動。CH1 は OFF時0V・ON時電源電圧）/ "low"（ローサイド駆動。CH1 は OFF時電源電圧・ON時0V）
    / "auto"（トリガー前とトリガー直後の電圧を比べて判別）。
    """
    dt, t0 = wave["dt"], wave["t0"]
    n = len(wave["inj"])
    t_ms = (t0 + np.arange(n) * dt) * 1e3
    res = {"window_ms": [round(float(t_ms[0]), 3), round(float(t_ms[-1]), 3)],
           "sample_us": round(dt * 1e6, 3)}
    w20us = max(3, int(20e-6 / dt))

    # --- ドライバがスイッチしている側のインジェクタ端子: 電源電圧・通電区間・フライバック ---
    raw = np.asarray(wave["inj"], dtype=np.float64)
    v = _smooth(raw, w20us)
    pre = t_ms < -0.2
    if pre.sum() < 50:
        res["error"] = "トリガー前の区間が短すぎます（タイムベース・トリガー位置を確認）"
        return res
    idle = float(np.median(v[pre]))
    early = (t_ms > 0.01) & (t_ms < 0.04)        # トリガー（通電開始）直後。最短パルス 0.05ms でも通電中
    if early.sum() < 3:
        res["error"] = "サンプル間隔が粗すぎます（タイムベースを確認）"
        return res
    v_early = float(np.median(raw[early]))
    if drive == "auto":
        drive = "high" if v_early > idle else "low"
    res["drive"] = drive
    # u: 待機時を0、通電中を正にそろえた電圧。フライバックはどちらの駆動でも負になる
    sign = 1.0 if drive == "high" else -1.0
    u = sign * (v - idle)
    amp = sign * (v_early - idle)
    if amp < 1.0:
        res["error"] = (f"通電中の電圧が待機時と区別できません（待機 {idle:.2f}V, 通電直後 {v_early:.2f}V）。"
                        "CH1 の接続と --drive を確認")
        return res
    th = amp / 2.0
    i_on = _first_cross(u, th, int(np.argmax(t_ms >= -0.1)), below=False)
    i_off = _first_cross(u, th, i_on + w20us, below=True) if i_on is not None else None
    if i_on is None or i_off is None:
        res["error"] = "通電開始/終了のエッジが見つかりません（トリガーレベル・窓幅を確認）"
        return res
    t_on, t_off = float(t_ms[i_on]), float(t_ms[i_off])
    res["t_on_ms"], res["t_off_ms"] = round(t_on, 4), round(t_off, 4)
    res["term_width_ms"] = round(t_off - t_on, 4)
    if drive == "high":
        # 電源電圧は通電直後（電流が小さく配線の電圧降下が小さい）の値。通電終了直前の値も残す
        seg = v[i_on + w20us:min(i_off, i_on + max(w20us + 3, int(0.1e-3 / dt)))]
        res["vbat_v"] = round(float(np.median(seg)), 2) if len(seg) else round(v_early, 2)
        res["v_idle_v"] = round(idle, 2)
        tail = v[max(i_on + w20us, i_off - int(0.2e-3 / dt)):i_off - w20us]
        res["v_load_v"] = round(float(np.median(tail)), 2) if len(tail) else None
        res["v_on_v"] = None
    else:
        res["vbat_v"] = round(idle, 2)
        res["v_on_v"] = round(float(np.median(v[i_on + w20us:i_off - w20us])), 2) \
            if i_off - i_on > 4 * w20us else None

    raw_after = raw[i_off:]
    if len(raw_after):
        fly_peak = float(raw_after.max() if drive == "low" else raw_after.min())
    else:
        fly_peak = idle
    res["flyback_peak_v"] = round(fly_peak, 2 if drive == "high" else 1)
    # フライバック継続: 待機時の電圧から外れている時間（ハイサイドは 0.2V、ローサイドは 0.4V 以上）
    beyond = np.nonzero(u[i_off:] < -(0.2 if drive == "high" else 0.4))[0]
    res["flyback_ms"] = round(float(len(beyond) * dt * 1e3), 3) if len(beyond) else 0.0
    # 待機時から数Vで止まるなら還流ダイオード（電流の減衰が遅く、閉弁が遅れる）
    res["clamp"] = "diode" if abs(fly_peak - idle) < 3.0 else "zener"

    # --- 指令信号（LOW=ON）: ドライバ段の遅れ ---
    if "cmd" in wave:
        c = _smooth(wave["cmd"], max(1, int(5e-6 / dt)))
        hi = float(np.median(c[pre]))
        cth = hi / 2.0
        ic_fall = _first_cross(c, cth, 0, below=True)
        ic_rise = _first_cross(c, cth, ic_fall + 2, below=False) if ic_fall is not None else None
        if ic_fall is not None and ic_rise is not None and hi > 1.0:
            tc_fall, tc_rise = float(t_ms[ic_fall]), float(t_ms[ic_rise])
            res["cmd_width_ms"] = round(tc_rise - tc_fall, 4)
            res["on_delay_us"] = round((t_on - tc_fall) * 1e3, 1)
            res["off_delay_us"] = round((t_off - tc_rise) * 1e3, 1)
            res["width_stretch_us"] = round((res["term_width_ms"] - res["cmd_width_ms"]) * 1e3, 1)
        else:
            res["cmd_width_ms"] = None

    # --- コイル電流: ピーク・OFF後の減衰・開弁キンク（参考値）---
    if "cur" in wave:
        cur = (np.asarray(wave["cur"], dtype=np.float64) - float(np.mean(wave["cur"][pre]))) * cur_a_per_v
        s = _smooth(cur, max(3, int(50e-6 / dt)))
        seg = s[i_on:i_off + 1]
        res["i_peak_a"] = round(float(seg.max()), 3)
        res["i_at_off_a"] = round(float(s[i_off]), 3)
        cur_off = float(s[i_off])
        if cur_off > 0.02:
            below = np.nonzero(s[i_off:] < 0.05 * cur_off)[0]
            res["i_decay_ms"] = round(float(below[0] * dt * 1e3), 3) if len(below) else None
        else:
            res["i_decay_ms"] = None
        res["open_kink_ms"] = _open_kink(s, dt, i_on, i_off)
        # コイル抵抗（温度の目安。銅線なら約0.39%/K）: 電流が飽和した通電終了前 0.5ms の 電圧÷電流。
        # 電流が飽和しない短いパルス（2ms未満）では出さない
        res["coil_r_ohm"] = None
        if t_off - t_on >= 2.0:
            win = slice(max(i_on, i_off - int(0.5e-3 / dt)), i_off - w20us)
            i_pl = float(np.mean(s[win]))
            v_coil = float(np.mean(v[win])) - idle if drive == "high" else idle - float(np.mean(v[win]))
            if i_pl > 0.1:
                res["coil_r_ohm"] = round(v_coil / i_pl, 3)

    # --- 燃圧センサ ---
    if "fuel" in wave:
        f = np.asarray(wave["fuel"], dtype=np.float64)
        to_mpa = lambda x: (x - fuel_v0) / (fuel_v_full - fuel_v0) * fuel_mpa_full
        res["fuel_pre_mpa"] = round(float(to_mpa(np.mean(f[pre]))), 4)
        fs = _smooth(f, max(3, int(200e-6 / dt)))
        res["fuel_min_mpa"] = round(float(to_mpa(fs.min())), 4)
        res["fuel_end_mpa"] = round(float(to_mpa(np.mean(f[-max(10, int(1e-3 / dt)):]))), 4)
    return res


def _open_kink(s, dt, i_on, i_off):
    """電流の立ち上がり中に出る「いったん傾きが落ちて、また戻る」点（電機子が動いた跡）を探す。

    di/dt が落ち込んだあと有意に回復する箇所を探し、そのくぼみの底の時刻（通電開始から）を返す。
    波形によっては出ないので、見つからなければ None。値は参考（噴射量の回帰値が正）。
    """
    w = max(3, int(100e-6 / dt))
    lo = i_on + 2 * w
    hi = min(i_off - 2 * w, i_on + int(4e-3 / dt))
    if hi - lo < 6 * w:
        return None
    # 傾きは ±100us の差分で求める（1サンプル差分ではノイズに埋もれる）
    d = (s[lo + w:hi + w] - s[lo - w:hi - w]) / (2 * w * dt)   # A/s
    dmax = float(d.max())
    if dmax <= 0:
        return None
    # 各点について「この先0.5ms以内に傾きがどれだけ回復するか」を求める
    look = max(w, int(500e-6 / dt))
    rec = np.array([d[k:k + look].max() - d[k] for k in range(0, len(d), max(1, w // 10))])
    cand = np.nonzero(rec >= 0.10 * dmax)[0]
    if len(cand) == 0:
        return None
    # 最初にまとまって現れる候補区間の中で、傾きが最小の点をキンクとする
    end = cand[0]
    while end + 1 < len(rec) and rec[end + 1] >= 0.10 * dmax:
        end += 1
    step = max(1, w // 10)
    seg = d[cand[0] * step:(end + 1) * step]
    k = cand[0] * step + int(np.argmin(seg))
    # 傾きが最小の点（落ち込み始め）と回復後の最大点の中間を、電流のくぼみの底＝キンク時刻とする
    k2 = k + int(np.argmax(d[k:k + look]))
    mid = 0.5 * (d[k] + d[k2])
    up = np.nonzero(d[k:k2 + 1] >= mid)[0]
    kc = k + (int(up[0]) if len(up) else 0)
    return round(float((lo + kc - i_on) * dt * 1e3), 3)


def describe(res):
    """解析結果を日本語の短い行にする。"""
    if "error" in res:
        return ["波形解析エラー: " + res["error"]]
    drive = res.get("drive", "low")
    vb = f"電源電圧 {res['vbat_v']}V"
    if drive == "high" and res.get("v_load_v") is not None:
        vb += f"（通電終了前 {res['v_load_v']}V）"
    out = [f"{vb} / 端子パルス幅 {res['term_width_ms']}ms（{'ハイサイド' if drive == 'high' else 'ローサイド'}駆動）"]
    if res.get("cmd_width_ms") is not None:
        out.append(f"指令幅 {res['cmd_width_ms']}ms（ON遅れ {res['on_delay_us']}us, OFF遅れ {res['off_delay_us']}us, "
                   f"差 {res['width_stretch_us']:+}us）")
    clamp = "還流ダイオード型（閉弁が遅れやすい）" if res["clamp"] == "diode" else "ツェナー/アバランシェ型"
    fly = "フライバック最小" if drive == "high" else "フライバック最大"
    out.append(f"{fly} {res['flyback_peak_v']}V, 継続 {res['flyback_ms']}ms → {clamp}")
    if "i_peak_a" in res:
        decay = f"{res['i_decay_ms']}ms" if res["i_decay_ms"] is not None else "窓内で減衰せず"
        kink = f"{res['open_kink_ms']}ms（参考）" if res["open_kink_ms"] is not None else "検出なし"
        coil = f", コイル抵抗 {res['coil_r_ohm']}Ω" if res.get("coil_r_ohm") is not None else ""
        out.append(f"電流ピーク {res['i_peak_a']}A, OFF後5%まで {decay}, 開弁キンク {kink}{coil}")
    if "fuel_pre_mpa" in res:
        out.append(f"燃圧 通電前 {res['fuel_pre_mpa']}MPa / 最小 {res['fuel_min_mpa']}MPa")
    return out


def add_scope_args(ap):
    ap.add_argument("--scope-resource", default=DEFAULT_RESOURCE, help="VISAリソース文字列")
    ap.add_argument("--ch-inj", type=int, default=1, help="ドライバがスイッチしている側のインジェクタ端子のチャネル")
    ap.add_argument("--ch-cur", type=int, default=2, help="コイル電流のチャネル（0=未使用）")
    ap.add_argument("--ch-cmd", type=int, default=3, help="指令信号(A0)のチャネル（0=未使用）")
    ap.add_argument("--ch-fuel", type=int, default=4, help="燃圧センサのチャネル（0=未使用）")
    ap.add_argument("--cur-a-per-v", type=float, default=1.0, help="電流プローブの換算 A/V（100mV/Aなら10）")
    ap.add_argument("--trig-level", type=float, default=6.0, help="トリガーレベル V（電源電圧の半分より下）")
    ap.add_argument("--drive", choices=("high", "low"), default="high",
                    help="インジェクタの駆動方式。トリガーの向きを決める（high=立ち上がり、low=立ち下がり。既定 high）")


def channels_from_args(a):
    return {"inj": a.ch_inj, "cur": a.ch_cur, "cmd": a.ch_cmd, "fuel": a.ch_fuel}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("arm", help="単発トリガー（インジェクタ端子の立ち下がり）を設定してアーム")
    p.add_argument("--pw-ms", type=float, default=4.0, help="想定パルス幅 ms（タイムベースの自動選択に使う）")
    p.add_argument("--tb", type=float, default=None, help="タイムベース s/div を直接指定")
    add_scope_args(p)
    p = sub.add_parser("wait", help="トリガー成立(STOP)まで待つ")
    p.add_argument("--timeout", type=float, default=10.0)
    add_scope_args(p)
    p = sub.add_parser("read", help="波形を .npz に保存して解析結果を表示")
    p.add_argument("out")
    add_scope_args(p)
    p = sub.add_parser("analyze", help="保存済み .npz を再解析")
    p.add_argument("path")
    p.add_argument("--cur-a-per-v", type=float, default=1.0)
    p.add_argument("--drive", choices=("auto", "high", "low"), default="auto", help="駆動方式（既定: 波形から判別）")
    a = ap.parse_args()

    if a.cmd == "analyze":
        res = analyze(load_wave(a.path), cur_a_per_v=a.cur_a_per_v, drive=a.drive)
        print(json.dumps(res, ensure_ascii=False, indent=1))
        print("\n".join(describe(res)))
        return 0

    sc = Scope(a.scope_resource, channels_from_args(a), a.trig_level, a.drive)
    try:
        if a.cmd == "arm":
            print("armed status:", sc.arm(a.pw_ms, a.tb))
        elif a.cmd == "wait":
            ok = sc.wait(a.timeout)
            print("STOP" if ok else "TIMEOUT")
            return 0 if ok else 1
        elif a.cmd == "read":
            wave = sc.read()
            save_wave(a.out, wave)
            res = analyze(wave, cur_a_per_v=a.cur_a_per_v)
            print("saved", a.out)
            print("\n".join(describe(res)))
    finally:
        sc.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
