#!/usr/bin/env python3
"""インジェクタ流量試験を1バースト1コマンドで実行し、結果をCSVに追記する。

試験ファーム（src/injector_test.cpp 第2版）へ設定を送り、噴射中の燃圧を
Chibi-T_Furoshiki_AutoAirAdjust（sensing-only ビルド）のUSBシリアルから記録し、
必要ならRIGOL DHO804で1発分の波形を撮る。重量は目視の値をキーボードから入力する。

  # 1バーストだけ実行（4ms x 750発、周期50ms）
  python tools/inj_bench.py run --ecu-port COM5 --fuel-port COM7 --pw-ms 4 --n 750 \
      --out document/injector_test_log_20261115.csv

  # 計画CSV（document/injector_test_log_template.csv）を上から順に実行。P2を先に
  python tools/inj_bench.py plan document/injector_test_log_template.csv --phase P2,P1 \
      --ecu-port COM5 --fuel-port COM7 --scope alt --out document/injector_test_log_20261115.csv

  # 機材なしで流れを確認（擬似ECU・擬似燃圧・擬似はかり）
  python tools/inj_bench.py plan document/injector_test_log_template.csv --phase P2 --simulate --out sim.csv

流れ: 設定送信 → 捨て打ち → 開始重量の入力 → （オシロをアーム）→ 開始 → 燃圧を記録
      → 分割バーストの再加圧待ち → 完了 → 終了重量の入力 → 波形取得・解析 → CSVへ1行追記

燃料の加圧は XIAO_PulseCreate がタイマーで Hi → PWM → Lo を繰り返している（Lo の間は加圧が抜けて
ダイヤフラムが燃料を吸い直す）。--pump-port にそのUSBシリアルを指定すると、加圧が安定している区間だけに
噴射を収めるよう、バーストを自動で分割してポンプ周期に同期させる。

pyserial が必要。オシロを使う場合は pyvisa と numpy も必要（tools/requirements.txt）。
"""
import argparse
import collections
import csv
import math
import os
import random
import re
import sys
import threading
import time

LOG_COLUMNS = [
    "No", "フェーズ", "測定日", "測定者", "燃圧_MPa", "燃料温度_C", "バッテリー電圧_V",
    "パルス幅_ms", "パルス数_N", "パルス間隔_ms", "反復回数",
    "バースト前燃圧_MPa", "バースト後燃圧_MPa", "開始重量_g", "終了重量_g", "噴射質量_g",
    "密度_g/ml", "噴射体積_ml", "備考", "Serialコマンド",
    # ここから第2版で追加した列
    "周期_ms", "分割発数", "実発数", "実測幅_平均_us", "実測幅_最小_us", "実測幅_最大_us",
    "周期_最小_us", "周期_最大_us", "所要時間_s", "待ち時間_s", "中断",
    "燃圧平均_MPa", "燃圧最小_MPa", "燃圧最大_MPa", "燃圧サンプル数",
    "ポンプ同期", "ポンプ_Hi_ms", "ポンプ_PWM_ms", "ポンプ_Lo_ms", "ポンプ_Duty_%",
    "オシロ_取得位置", "オシロ_電源電圧_V", "オシロ_端子パルス幅_ms", "オシロ_指令幅_ms",
    "オシロ_ON遅れ_us", "オシロ_OFF遅れ_us", "オシロ_フライバック最大_V", "オシロ_フライバック継続_ms",
    "オシロ_クランプ種別", "オシロ_電流ピーク_A", "オシロ_電流減衰_ms", "オシロ_開弁キンク_ms",
    "波形ファイル",
]

# 分割発数の目安に使う1発あたり噴射量の概算式（2026-08-22ログ PW=1〜6ms の回帰）
EST_MG_PER_MS = 0.551
EST_MG_OFFSET = 1.21


# ---------------------------------------------------------------------------
# 試験ファームとの通信
# ---------------------------------------------------------------------------
class Ecu:
    """'#INJ,<種別>,key=value,...' の行をイベントとして読む。"""

    def __init__(self, ser, echo=False):
        self.ser = ser
        self.echo = echo
        self._buf = b""

    def send(self, line):
        if self.echo:
            print(f"    > {line}")
        self.ser.write((line + "\n").encode())

    def read_event(self, timeout):
        """timeout 秒以内に来た次のイベント (種別, dict) を返す。来なければ None。"""
        t_end = time.monotonic() + timeout
        while True:
            nl = self._buf.find(b"\n")
            if nl >= 0:
                raw, self._buf = self._buf[:nl], self._buf[nl + 1:]
                line = raw.decode(errors="replace").strip()
                if line.startswith("#INJ,"):
                    parts = line.split(",")
                    kv = dict(p.split("=", 1) for p in parts[2:] if "=" in p)
                    return parts[1], kv
                if self.echo and line:
                    print(f"    < {line}")
                continue
            if time.monotonic() >= t_end:
                return None
            chunk = self.ser.read(256)
            if chunk:
                self._buf += chunk

    def wait_for(self, kinds, timeout):
        """kinds のいずれかのイベントが来るまで読む。ERR が来たら例外。"""
        t_end = time.monotonic() + timeout
        while time.monotonic() < t_end:
            ev = self.read_event(min(0.5, max(0.01, t_end - time.monotonic())))
            if ev is None:
                continue
            if ev[0] == "ERR":
                raise RuntimeError(f"試験ファームがエラーを返しました: {ev[1].get('msg')}")
            if ev[0] in kinds:
                return ev
        raise TimeoutError(f"試験ファームから {'/'.join(kinds)} が返りません")

    def drain(self, seconds=0.3):
        t_end = time.monotonic() + seconds
        while time.monotonic() < t_end:
            self.read_event(0.05)

    def configure(self, pw_us, n, period_ms, sub, pause_ms):
        self.drain(0.2)
        cmd = f"U{pw_us} N{n} T{period_ms} B{sub} H{pause_ms}"
        self.send(cmd)
        _, cfg = self.wait_for({"CFG"}, 3.0)
        want = {"pw_us": pw_us, "n": n, "period_us": period_ms * 1000, "sub": sub, "pause_ms": pause_ms}
        for k, v in want.items():
            if int(cfg.get(k, -1)) != v:
                raise RuntimeError(f"設定が反映されていません: {k}={cfg.get(k)}（指令 {v}）")
        return cmd, cfg.get("armed") == "1"


class FuelMonitor(threading.Thread):
    """AutoAirAdjust の '[BLE TX] ... FUEL=0.35012 ...' 行から燃圧を時刻付きで溜める。"""

    PAT = re.compile(rb"FUEL=(-?\d+\.\d+)")

    def __init__(self, ser):
        super().__init__(daemon=True)
        self.ser = ser
        self.samples = collections.deque(maxlen=200000)   # (monotonic秒, MPa)
        self._halt = threading.Event()

    def run(self):
        while not self._halt.is_set():
            try:
                raw = self.ser.readline()
            except Exception:
                break
            m = self.PAT.search(raw or b"")
            if m:
                self.samples.append((time.monotonic(), float(m.group(1))))

    def stop(self):
        self._halt.set()

    def wait_valid(self, timeout=25.0):
        t_end = time.monotonic() + timeout
        while time.monotonic() < t_end:
            if self.samples:
                return True
            time.sleep(0.1)
        return False

    def window(self, t0, t1):
        return [v for t, v in list(self.samples) if t0 <= t <= t1]

    def recent_mean(self, seconds=0.6):
        now = time.monotonic()
        w = self.window(now - seconds, now)
        return sum(w) / len(w) if w else None


class PumpMonitor(threading.Thread):
    """XIAO_PulseCreate のデバッグ出力 'countmode, hiTime, pwmTime, loTime, Duty / 4096' を読む。

    countmode: 0=停止 1=Hi(100%) 2=PWM(保持) 3=Lo(加圧が抜けて吸い直し)。
    Hi に入った時刻を周期の起点として覚え、「加圧が安定している残り時間」を答える。
    scale は擬似デバイス用（ポンプ側の時間が実時間の何倍で進むか）。実機では 1。
    """

    PAT = re.compile(rb"^\s*([0-3])\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*/")

    def __init__(self, ser, scale=1.0):
        super().__init__(daemon=True)
        self.ser = ser
        self.scale = scale
        self._halt = threading.Event()
        self.mode = None            # 直近の countmode
        self.hi = self.pwm = self.lo = self.duty = None
        self.t_line = None          # 直近の行を受けた時刻
        self.t_cycle = None         # 直近に Hi へ入ったのを見た時刻（周期の起点）

    def run(self):
        while not self._halt.is_set():
            try:
                raw = self.ser.readline()
            except Exception:
                break
            m = self.PAT.match(raw or b"")
            if not m:
                continue
            now = time.monotonic()
            mode = int(m.group(1))
            self.hi, self.pwm, self.lo, self.duty = (int(m.group(i)) for i in (2, 3, 4, 5))
            if mode in (0, 3):
                self.t_cycle = None
            elif self.mode in (0, 3):
                # 停止/Lo から加圧に入った。Hi の行を取りこぼして PWM から見えた場合は、Hi の分だけ前倒しする
                self.t_cycle = now - (self.hi / 1000.0 / self.scale if mode == 2 else 0.0)
            self.mode, self.t_line = mode, now

    def stop(self):
        self._halt.set()

    def alive(self):
        return self.t_line is not None and time.monotonic() - self.t_line < 2.0

    def window_s(self, settle, margin):
        """1周期のうち噴射に使える長さ [s]（ポンプ時間）。"""
        return (self.hi + self.pwm) / 1000.0 - settle - margin

    def remaining_s(self, settle, margin):
        """今すぐ噴き始めた場合に使える残り時間 [s]。使えない状態なら 0 以下、周期未確認なら None。"""
        if self.mode not in (1, 2) or self.t_cycle is None:
            return None if self.mode in (1, 2) else 0.0
        elapsed = (time.monotonic() - self.t_cycle) * self.scale
        if elapsed < settle:
            return 0.0
        return (self.hi + self.pwm) / 1000.0 - margin - elapsed

    def wait_window(self, need_s, settle, margin, timeout_s=60.0):
        """加圧が安定していて、need_s 秒ぶん噴ける区間に入るまで待つ。入れたら True。"""
        t_end = time.monotonic() + timeout_s / self.scale
        t_stopped = None
        while time.monotonic() < t_end:
            if not self.alive():
                return False
            if self.mode == 0:              # 停止が3秒続いたら待つのをやめて呼び出し側に知らせる
                t_stopped = t_stopped or time.monotonic()
                if time.monotonic() - t_stopped > 3.0:
                    return False
            else:
                t_stopped = None
            rem = self.remaining_s(settle, margin)
            if rem is not None and rem >= need_s:
                return True
            time.sleep(0.01 if self.scale > 1 else 0.02)
        return False


# ---------------------------------------------------------------------------
# 機材なしで流れを確認するための擬似デバイス
# ---------------------------------------------------------------------------
class FakeEcuSerial:
    """試験ファームの応答をまねる（タイミングは実時間の speed 倍で進む）。"""

    def __init__(self, speed=20.0):
        self.speed = speed
        self.cfg = {"pw_us": 4000, "n": 300, "period_ms": 50, "sub": 0, "pause_ms": 0}
        self.out = collections.deque()      # (出す時刻, bytes)
        self.run = 0
        self.busy_until = 0.0
        self.paused = None                  # 分割待ち中の残り情報

    def _emit(self, line, at=None):
        self.out.append((at if at is not None else time.monotonic(), (line + "\n").encode()))

    def _cfg_line(self):
        c = self.cfg
        return (f"#INJ,CFG,pw_us={c['pw_us']},n={c['n']},period_us={c['period_ms'] * 1000},mode=T,"
                f"sub={c['sub']},pause_ms={c['pause_ms']},armed=1")

    def _schedule(self, fired, n, kind, t0, elapsed0=0.0, pause_total=0.0):
        c = self.cfg
        per = c["period_ms"] / 1000.0 / self.speed
        sub = c["sub"] if kind == "burst" else 0
        stop_at = min(n, (fired // sub + 1) * sub) if sub else n
        t_end = t0 + (stop_at - fired) * per
        if kind == "burst":
            t = t0 + 0.25
            while t < t_end:
                self._emit(f"#INJ,PROG,run={self.run},fired={fired + int((t - t0) / per)}", t)
                t += 0.25
        self.busy_until = t_end
        elapsed = elapsed0 + (stop_at - fired) * c["period_ms"] / 1000.0
        if stop_at < n:
            self._emit(f"#INJ,PAUSE,run={self.run},fired={stop_at}", t_end)
            self.paused = (stop_at, n, kind, elapsed, pause_total, t_end)
            if c["pause_ms"] > 0:
                wait = c["pause_ms"] / 1000.0 / self.speed
                self._resume(t_end + wait)
        else:
            w = c["pw_us"] + 12
            self._emit(
                f"#INJ,DONE,run={self.run},kind={kind},pw_us={c['pw_us']},n_cmd={n},n_fired={n},"
                f"period_us={c['period_ms'] * 1000},w_min_us={w - 11},w_mean_us={w:.2f},w_max_us={w + 12},"
                f"per_min_us={c['period_ms'] * 1000 - 20},per_max_us={c['period_ms'] * 1000 + 20},"
                f"elapsed_ms={int((elapsed + pause_total) * 1000)},pause_ms_total={int(pause_total * 1000)},"
                f"abort=none,partial=0", t_end)
            self.paused = None

    def _resume(self, at):
        fired, n, kind, elapsed, pause_total, t_pause = self.paused
        pause_total += (at - t_pause) * self.speed
        self._emit(f"#INJ,RESUME,run={self.run},fired={fired}", at)
        self._schedule(fired, n, kind, at, elapsed, pause_total)

    def write(self, data):
        now = time.monotonic()
        for tok in data.decode().split():
            c, arg = tok[0].upper(), tok[1:]
            if c == "G" or c == "Q":
                n = self.cfg["n"] if c == "G" else int(arg)
                kind = "burst" if c == "G" else "prime"
                self.run += 1
                self._emit(f"#INJ,START,run={self.run},kind={kind},pw_us={self.cfg['pw_us']},n={n},"
                           f"period_us={self.cfg['period_ms'] * 1000},sub={self.cfg['sub'] if c == 'G' else 0}", now)
                self._schedule(0, n, kind, now)
                return
            if c == "C" and self.paused:
                self._resume(now)
                return
            key = {"U": "pw_us", "N": "n", "T": "period_ms", "B": "sub", "H": "pause_ms"}.get(c)
            if key:
                self.cfg[key] = int(arg)
        self._emit(self._cfg_line(), now)

    def read(self, _n=1):
        now = time.monotonic()
        if self.out and self.out[0][0] <= now:
            self.out = collections.deque(sorted(self.out, key=lambda x: x[0]))
            return self.out.popleft()[1]
        time.sleep(0.01)
        return b""

    def close(self):
        pass


class FakeFuelSerial:
    """0.35MPa付近でゆっくり揺れる燃圧を10Hzで出す。"""

    def __init__(self):
        self.t0 = time.monotonic()

    def readline(self):
        time.sleep(0.1)
        t = time.monotonic() - self.t0
        v = 0.350 - 0.012 * abs(math.sin(t / 1.7)) + random.gauss(0, 0.001)
        return f"[BLE TX] PRI=0.60012 SEC=0.35120 FUEL={v:.5f} (connected=no)\n".encode()

    def close(self):
        pass


class FakePumpSerial:
    """XIAO_PulseCreate のデバッグ出力をまねる（Hi 0.2s → PWM 7.8s → Lo 0.5s を speed 倍速で繰り返す）。"""

    def __init__(self, speed=20.0, hi=200, pwm=7800, lo=500, duty=25):
        self.speed, self.hi, self.pwm, self.lo, self.duty = speed, hi, pwm, lo, duty
        self.t0 = time.monotonic()

    def readline(self):
        time.sleep(0.1 / self.speed)
        t = ((time.monotonic() - self.t0) * self.speed * 1000.0) % (self.hi + self.pwm + self.lo)
        mode = 1 if t < self.hi else (2 if t < self.hi + self.pwm else 3)
        return f"{mode}, {self.hi}, {self.pwm}, {self.lo}, {self.duty} / 4096\n".encode()

    def close(self):
        pass


def fake_scale(pw_us, n, noise=0.012):
    """擬似はかり。総質量[g] = N×0.55mg/ms×(PW−0.60ms) + 0.05g（バースト固定分）+ 読み取りばらつき。"""
    mg = n * 0.55 * (pw_us / 1000.0 - 0.60) + 50.0
    return max(0.0, round(mg / 1000.0 + random.gauss(0, noise), 2))


# ---------------------------------------------------------------------------
# 1バーストの実行
# ---------------------------------------------------------------------------
def ask_float(prompt, default=None, allow_skip=False):
    while True:
        s = input(prompt).strip()
        if s == "" and default is not None:
            return default
        if allow_skip and s.lower() in ("x", "skip"):
            return None
        try:
            return float(s)
        except ValueError:
            print("  数値を入力してください" + ("（無効にするなら x）" if allow_skip else ""))


def pick_sub_count(pw_us, n, max_ml, density):
    """ポンプ容量を超えないよう、1回分の発数を決める（分割不要なら 0）。"""
    if not max_ml or max_ml <= 0:
        return 0
    mg = EST_MG_PER_MS * pw_us / 1000.0 + EST_MG_OFFSET
    per_shot_ml = mg / 1000.0 / density
    sub = int(max_ml / per_shot_ml)
    if sub >= n:
        return 0
    return max(sub, 10)


def fmt(v, nd=3):
    if v is None or v == "":
        return ""
    return f"{v:.{nd}f}" if isinstance(v, float) else str(v)


def do_burst(ctx, job):
    """job: dict(no, phase, rep, pw_us, n, target_mpa, target_v, note)。CSVの1行(dict)を返す。"""
    a, ecu, fuel, scope, pump = ctx["args"], ctx["ecu"], ctx["fuel"], ctx["scope"], ctx.get("pump")
    pw_us, n = job["pw_us"], job["n"]
    sub = a.sub if a.sub is not None else pick_sub_count(pw_us, n, a.max_burst_ml, a.density)
    resume = "pump" if pump else a.resume
    if pump:
        # ポンプ1周期の「加圧が安定している区間」に収まる発数まで、分割をさらに細かくする
        win = pump.window_s(a.pump_settle_s, a.pump_margin_s)
        fit = int((win - 0.3) / (a.period_ms / 1000.0))   # 0.3秒は開始タイミングのずれに対する余裕
        if fit < 5:
            raise SystemExit(f"ポンプの加圧区間（{win:.1f}秒）が短すぎます。--pump-settle-s / --pump-margin-s か "
                             "XIAO_PulseCreate の PWM 時間を見直してください")
        if n > fit or (sub and sub > fit):
            sub = min(sub, fit) if sub else fit
    pause_ms = int(a.pause_s * 1000) if resume == "time" else 0
    if sub and resume == "time" and pause_ms == 0:
        raise SystemExit("--resume time には --pause-s が必要です")
    chunk_s = (min(sub, n) if sub else n) * a.period_ms / 1000.0

    def wait_pump(what):
        """ポンプ同期: 加圧が安定していて、次の1回分が収まる区間に入るまで待つ。"""
        if not pump:
            return
        while not pump.wait_window(chunk_s, a.pump_settle_s, a.pump_margin_s):
            state = {0: "停止中（TriggerPin が LOW）", None: "応答なし"}.get(
                pump.mode if pump.alive() else None, "周期を確認できません")
            if a.simulate:
                raise RuntimeError(f"ポンプ同期に失敗しました: {state}")
            input(f"  ポンプが {state}。{what}できません。ポンプを動かしてから Enter: ")

    cmd, armed = ecu.configure(pw_us, n, a.period_ms, sub, pause_ms)
    est_s = n * a.period_ms / 1000.0
    print(f"  設定: {pw_us}us x {n}発, 周期{a.period_ms}ms（約{est_s:.0f}秒）"
          + (f", {sub}発ごとに分割" if sub else "")
          + ("（ポンプ周期に同期）" if pump else ""))
    if not armed:
        input("  キルスイッチがOFF(導通)になっていません。操作してから Enter: ")

    # 捨て打ち（ノズルを濡らす。計量前に行うので噴いた分は開始重量に含まれる）
    if a.prime > 0:
        ecu.send(f"Q{a.prime}")
        ecu.wait_for({"START"}, 3.0)
        ecu.wait_for({"DONE"}, a.prime * a.period_ms / 1000.0 + 5.0)
        print(f"  捨て打ち {a.prime}発 完了")

    if a.simulate:
        w_start = 0.0
    else:
        w_start = ask_float("  開始重量[g]（Enterで0.00）: ", default=0.0)

    # オシロ: first は開始前にアーム、last は終わり際にアーム
    shot = job.get("scope_shot", "none")
    late_from = None
    if scope and shot == "last":
        late_from = n - max(3, math.ceil(1.5 / (a.period_ms / 1000.0)))
        if late_from < n // 2:
            shot = "first"     # バーストが短すぎるときは先頭を撮る
    if scope and shot == "first":
        st = scope.arm(pw_us / 1000.0)
        print(f"  オシロをアーム（先頭の1発, status={st}）")

    wait_pump("開始")
    p_before = fuel.recent_mean(1.0) if fuel else None
    ecu.drain(0.1)
    t_start = time.monotonic()
    ecu.send("G")
    ecu.wait_for({"START"}, 3.0)
    print("  噴射中...", end="", flush=True)

    done = None
    armed_late = False
    deadline = t_start + est_s * 1.5 + 30.0
    while done is None:
        ev = ecu.read_event(0.5)
        if ev is None:
            if time.monotonic() > deadline and not sub:
                raise TimeoutError("バーストが終わりません（#INJ,DONE が来ない）")
            continue
        kind, kv = ev
        if kind == "ERR":
            raise RuntimeError(f"試験ファームがエラーを返しました: {kv.get('msg')}")
        if kind == "PROG":
            fired = int(kv.get("fired", 0))
            print(f"\r  噴射中... {fired}/{n}", end="", flush=True)
            if late_from is not None and not armed_late and shot == "last" and fired >= late_from:
                scope.arm(pw_us / 1000.0)
                armed_late = True
        elif kind == "PAUSE":
            fired = int(kv.get("fired", 0))
            print(f"\r  分割待ち {fired}/{n}      ")
            if resume == "pump":
                wait_pump("続行")
                ecu.send("C")
            elif resume == "pressure" and fuel:
                target = a.resume_mpa if a.resume_mpa else (job.get("target_mpa") or 0.35) - 0.01
                t_end = time.monotonic() + a.resume_timeout
                ok = False
                while time.monotonic() < t_end:
                    p = fuel.recent_mean(0.6)
                    if p is not None and p >= target:
                        ok = True
                        break
                    time.sleep(0.2)
                if not ok:
                    input(f"  燃圧が {target:.3f}MPa まで戻りません。再加圧してから Enter: ")
                time.sleep(a.settle_s)
                ecu.send("C")
            elif resume == "time":
                pass                      # ファーム側が H<ms> 後に自動で続行する
            elif a.simulate:
                time.sleep(0.2)
                ecu.send("C")
            else:
                p = fuel.recent_mean(0.6) if fuel else None
                input("  再加圧してから Enter" + (f"（現在の燃圧 {p:.3f}MPa）" if p is not None else "") + ": ")
                ecu.send("C")
        elif kind == "DONE" and kv.get("kind") == "burst":
            done = kv
    t_done = time.monotonic()
    print(f"\r  完了 {done['n_fired']}/{n}発              ")

    n_fired = int(done["n_fired"])
    aborted = done.get("abort", "none")
    if aborted != "none":
        print(f"  !! 中断されました（{aborted}）。この行は解析から除外されます")

    if a.simulate:
        w_end = fake_scale(float(done.get("w_mean_us", pw_us)), n_fired)
        print(f"  終了重量[g]（擬似）: {w_end:.2f}")
    else:
        w_end = ask_float("  終了重量[g]（無効にするなら x）: ", allow_skip=True)

    time.sleep(0.8 if fuel else 0)       # バースト後の燃圧を1点取る
    p_after = fuel.recent_mean(0.8) if fuel else None
    pw = fuel.window(t_start, t_done) if fuel else []

    row = {c: "" for c in LOG_COLUMNS}
    mass = None if w_end is None else round(w_end - w_start, 3)
    row.update({
        "No": job["no"], "フェーズ": job.get("phase", ""), "測定日": time.strftime("%Y/%m/%d"),
        "測定者": a.operator, "燃圧_MPa": fmt(job.get("target_mpa"), 2), "燃料温度_C": fmt(a.fuel_temp, 1),
        "パルス幅_ms": fmt(pw_us / 1000.0, 3), "パルス数_N": n,
        "パルス間隔_ms": fmt(a.period_ms - pw_us / 1000.0, 3), "反復回数": job.get("rep", ""),
        "バースト前燃圧_MPa": fmt(p_before, 4), "バースト後燃圧_MPa": fmt(p_after, 4),
        "開始重量_g": fmt(w_start, 2), "終了重量_g": fmt(w_end, 2), "噴射質量_g": fmt(mass, 2),
        "密度_g/ml": fmt(a.density, 4),
        "噴射体積_ml": fmt(mass / a.density, 3) if mass is not None else "",
        "備考": job.get("note", ""), "Serialコマンド": cmd,
        "周期_ms": a.period_ms, "分割発数": sub, "実発数": n_fired,
        "実測幅_平均_us": done.get("w_mean_us", ""), "実測幅_最小_us": done.get("w_min_us", ""),
        "実測幅_最大_us": done.get("w_max_us", ""), "周期_最小_us": done.get("per_min_us", ""),
        "周期_最大_us": done.get("per_max_us", ""),
        "所要時間_s": fmt(int(done.get("elapsed_ms", 0)) / 1000.0, 1),
        "待ち時間_s": fmt(int(done.get("pause_ms_total", 0)) / 1000.0, 1),
        "中断": aborted + ("+partial" if done.get("partial") == "1" else ""),
    })
    if pw:
        row.update({"燃圧平均_MPa": fmt(sum(pw) / len(pw), 4), "燃圧最小_MPa": fmt(min(pw), 4),
                    "燃圧最大_MPa": fmt(max(pw), 4), "燃圧サンプル数": len(pw)})
        print(f"  燃圧: 前 {fmt(p_before, 3)} / 平均 {row['燃圧平均_MPa']} / 最小 {row['燃圧最小_MPa']} / "
              f"後 {fmt(p_after, 3)} MPa（{len(pw)}点）")

    if pump:
        row.update({"ポンプ同期": "pump", "ポンプ_Hi_ms": pump.hi, "ポンプ_PWM_ms": pump.pwm,
                    "ポンプ_Lo_ms": pump.lo, "ポンプ_Duty_%": pump.duty})
    else:
        row["ポンプ同期"] = "none"

    vbat = a.vbat
    if vbat is None and not scope and not a.simulate:
        # オシロなしの場合は電源の表示値を入力する（Enterで前回値、なければ計画の目標値）
        default = job.get("target_v") or ctx.get("last_vbat")
        hint = f"Enterで{default:g}" if default is not None else "Enterで空欄"
        s = input(f"  電源電圧[V]（{hint}）: ").strip()
        try:
            vbat = float(s) if s else default
        except ValueError:
            vbat = default
        ctx["last_vbat"] = vbat
    elif vbat is None:
        vbat = job.get("target_v")
    if scope and shot in ("first", "last") and (shot == "first" or armed_late):
        import inj_scope
        if scope.wait(5.0):
            wave = scope.read()
            os.makedirs(a.wave_dir, exist_ok=True)
            path = os.path.join(a.wave_dir, f"{time.strftime('%Y%m%d_%H%M%S')}_no{job['no']}_"
                                            f"{pw_us}us_{shot}.npz")
            inj_scope.save_wave(path, wave)
            res = inj_scope.analyze(wave, cur_a_per_v=a.cur_a_per_v)
            for line in inj_scope.describe(res):
                print("  " + line)
            if "error" not in res:
                vbat = res.get("vbat_v", vbat)
                row.update({
                    "オシロ_取得位置": shot, "オシロ_電源電圧_V": fmt(res.get("vbat_v")),
                    "オシロ_端子パルス幅_ms": fmt(res.get("term_width_ms"), 4),
                    "オシロ_指令幅_ms": fmt(res.get("cmd_width_ms"), 4),
                    "オシロ_ON遅れ_us": fmt(res.get("on_delay_us"), 1),
                    "オシロ_OFF遅れ_us": fmt(res.get("off_delay_us"), 1),
                    "オシロ_フライバック最大_V": fmt(res.get("flyback_peak_v"), 1),
                    "オシロ_フライバック継続_ms": fmt(res.get("flyback_ms"), 3),
                    "オシロ_クランプ種別": res.get("clamp", ""),
                    "オシロ_電流ピーク_A": fmt(res.get("i_peak_a"), 3),
                    "オシロ_電流減衰_ms": fmt(res.get("i_decay_ms"), 3),
                    "オシロ_開弁キンク_ms": fmt(res.get("open_kink_ms"), 3),
                })
            row["波形ファイル"] = path
        else:
            print("  オシロがトリガーされませんでした（トリガーレベル・接続を確認）")
    row["バッテリー電圧_V"] = fmt(vbat, 2) if vbat is not None else ""

    if mass is not None and n_fired:
        print(f"  噴射質量 {mass:.2f}g → 1発あたり {mass / n_fired * 1000:.3f}mg")
    return row


def safe_burst(ctx, job):
    """do_burst の途中で Ctrl+C やエラーが起きたら、まず噴射を止める。"""
    try:
        return do_burst(ctx, job)
    except BaseException:
        try:
            ctx["ecu"].send("S")
        except Exception:
            pass
        raise


def append_row(path, row):
    new = not os.path.exists(path) or os.path.getsize(path) == 0
    if not new:
        with open(path, encoding="utf-8-sig", newline="") as f:
            header = next(csv.reader(f), [])
        if header != LOG_COLUMNS:
            raise SystemExit(f"{path} の列が第2版の形式と違います。新しいファイル名を --out に指定してください")
    with open(path, "a", encoding="utf-8-sig" if new else "utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=LOG_COLUMNS)
        if new:
            w.writeheader()
        w.writerow(row)


def done_numbers(path):
    """出力CSVに有効な結果（中断なし・質量あり）が既にあるNoの集合。計画を途中から再開するのに使う。"""
    if not os.path.exists(path):
        return set()
    with open(path, encoding="utf-8-sig", newline="") as f:
        return {r["No"] for r in csv.DictReader(f)
                if r.get("噴射質量_g") and r.get("中断", "none") == "none"}


def load_plan(path, phases):
    with open(path, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    jobs = []
    for r in rows:
        if not r.get("パルス幅_ms") or not r.get("パルス数_N"):
            continue

        def num(key):
            try:
                return float(r.get(key) or "")
            except ValueError:
                return None
        jobs.append({
            "no": r.get("No", ""), "phase": r.get("フェーズ", ""), "rep": r.get("反復回数", ""),
            "pw_us": int(round(float(r["パルス幅_ms"]) * 1000)), "n": int(float(r["パルス数_N"])),
            "target_mpa": num("燃圧_MPa"), "target_v": num("バッテリー電圧_V"), "note": r.get("備考", ""),
        })
    if phases:
        order = [p.strip() for p in phases.split(",") if p.strip()]
        picked = []
        for p in order:
            picked += [j for j in jobs if j["phase"].startswith(p)]
        jobs = picked
    return jobs


def open_devices(a):
    ctx = {"args": a, "ecu": None, "fuel": None, "scope": None, "pump": None, "_close": []}
    pser, pump_scale = None, 1.0
    if a.simulate:
        ser = FakeEcuSerial()
        fser = FakeFuelSerial()
        if a.pump_port:
            pser, pump_scale = FakePumpSerial(speed=ser.speed), ser.speed
    else:
        import serial
        if not a.ecu_port:
            raise SystemExit("--ecu-port を指定してください（機材なしで試すなら --simulate）")
        ser = serial.Serial(a.ecu_port, 115200, timeout=0.05)
        fser = None
        if a.fuel_port:
            fser = serial.Serial()
            fser.port, fser.baudrate, fser.timeout = a.fuel_port, 115200, 0.3
            fser.dtr = False
            fser.rts = False
            fser.open()
        if a.pump_port:
            pser = serial.Serial(a.pump_port, 115200, timeout=0.5)
    ctx["_close"].append(ser)
    ctx["ecu"] = Ecu(ser, echo=a.verbose)
    time.sleep(0.3)
    ctx["ecu"].send("?")
    try:
        ctx["ecu"].wait_for({"CFG"}, 3.0)
    except TimeoutError:
        raise SystemExit("試験ファームが応答しません。uno_r4_minima_inj_test を書き込んだか、ポートを確認してください")

    if fser is not None:
        ctx["_close"].append(fser)
        mon = FuelMonitor(fser)
        mon.start()
        print("燃圧の受信を待っています（ポートを開くとM5Stackがリセットされることがあります）...")
        if not mon.wait_valid(25.0):
            raise SystemExit("燃圧（[BLE TX] ... FUEL=）を受信できません。sensing-only ビルドとポートを確認してください")
        print(f"燃圧 {mon.recent_mean(1.5):.3f}MPa")
        ctx["fuel"] = mon
    else:
        print("燃圧ポートの指定なし: 燃圧は記録しません")

    if pser is not None:
        ctx["_close"].append(pser)
        pm = PumpMonitor(pser, scale=pump_scale)
        pm.start()
        t_end = time.monotonic() + 3.0
        while pm.mode is None and time.monotonic() < t_end:
            time.sleep(0.05)
        if pm.mode is None:
            raise SystemExit("XIAO_PulseCreate のデバッグ出力を受信できません。ポートと DEBUG 1 のビルドを確認してください")
        print(f"ポンプ: Hi {pm.hi}ms / PWM {pm.pwm}ms / Lo {pm.lo}ms / Duty {pm.duty}%"
              f"（1周期のうち噴射に使うのは {pm.window_s(a.pump_settle_s, a.pump_margin_s):.1f}秒）")
        if pm.mode == 0:
            print("  ※ ポンプは停止中（TriggerPin が LOW）。キルスイッチの状態を確認してください")
        ctx["pump"] = pm
    else:
        print("ポンプポートの指定なし: ポンプ周期には同期しません（加圧が抜ける Lo 区間にも噴きます）")

    if a.scope != "none" and not a.simulate:
        import inj_scope
        ctx["scope"] = inj_scope.Scope(a.scope_resource, inj_scope.channels_from_args(a), a.trig_level)
        print("オシロ:", ctx["scope"].idn())
    return ctx


def close_devices(ctx):
    if ctx.get("fuel"):
        ctx["fuel"].stop()
    if ctx.get("pump"):
        ctx["pump"].stop()
    if ctx.get("scope"):
        ctx["scope"].close()
    for s in ctx["_close"]:
        try:
            s.close()
        except Exception:
            pass


def cmd_run(a):
    ctx = open_devices(a)
    try:
        job = {"no": a.no, "phase": a.phase or "", "rep": a.rep, "pw_us": int(round(a.pw_ms * 1000)),
               "n": a.n, "target_mpa": a.target_mpa, "target_v": None, "note": a.note,
               "scope_shot": "first" if a.scope == "alt" else a.scope}
        row = safe_burst(ctx, job)
        append_row(a.out, row)
        print(f"→ {a.out} に追記しました")
    finally:
        close_devices(ctx)


def cmd_plan(a):
    jobs = load_plan(a.plan, a.phase)
    finished = done_numbers(a.out)
    todo = [j for j in jobs if j["no"] not in finished]
    print(f"計画 {len(jobs)}行のうち、未実施 {len(todo)}行")
    if not todo:
        return
    ctx = open_devices(a)
    try:
        for i, job in enumerate(todo):
            job["scope_shot"] = ("first" if i % 2 == 0 else "last") if a.scope == "alt" else a.scope
            tv = f", 目標電圧 {job['target_v']}V" if job["target_v"] else ""
            tp = f", 目標燃圧 {job['target_mpa']}MPa" if job["target_mpa"] else ""
            print(f"\n[{i + 1}/{len(todo)}] No.{job['no']} {job['phase']} 反復{job['rep']}: "
                  f"{job['pw_us'] / 1000:g}ms x {job['n']}発{tp}{tv}")
            if not a.simulate:
                s = input("  条件を合わせて Enter（s=スキップ / q=終了）: ").strip().lower()
                if s == "q":
                    break
                if s == "s":
                    continue
            row = safe_burst(ctx, job)
            append_row(a.out, row)
    finally:
        close_devices(ctx)
    print(f"\n→ {a.out}")


def add_common(p):
    p.add_argument("--ecu-port", help="試験ファーム（UNO R4）のシリアルポート")
    p.add_argument("--fuel-port", help="AutoAirAdjust（sensing-only）のシリアルポート。省略時は燃圧を記録しない")
    p.add_argument("--pump-port", help="XIAO_PulseCreate のシリアルポート。指定するとポンプ周期に同期して噴く"
                                       "（--simulate では任意の文字列で擬似ポンプが動く）")
    p.add_argument("--pump-settle-s", type=float, default=1.0,
                   help="ポンプが Hi に入ってから噴き始めるまでの待ち秒数（再加圧後の整定。既定 1.0）")
    p.add_argument("--pump-margin-s", type=float, default=0.5,
                   help="ポンプが Lo に入る何秒前までに噴き終えるか（既定 0.5）")
    p.add_argument("--out", required=True, help="結果を追記するCSV")
    p.add_argument("--period-ms", type=int, default=50, help="周期 ms（ONからON。既定 50）")
    p.add_argument("--prime", type=int, default=20, help="計量前の捨て打ち発数（0で無効。既定 20）")
    p.add_argument("--max-burst-ml", type=float, default=1.5,
                   help="1回に連続で噴く量の上限 ml（ポンプ容量2ccに対する余裕。これを超える分は分割。既定 1.5）")
    p.add_argument("--sub", type=int, default=None, help="分割1回分の発数を直接指定（0で分割なし）")
    p.add_argument("--resume", choices=("enter", "pressure", "time"), default="enter",
                   help="分割待ちからの続行方法: enter=Enterキー / pressure=燃圧が戻ったら自動 / time=一定時間後")
    p.add_argument("--resume-mpa", type=float, default=None, help="pressure のしきい値 MPa（既定: 目標燃圧−0.01）")
    p.add_argument("--resume-timeout", type=float, default=60.0, help="pressure で待つ最大秒数")
    p.add_argument("--settle-s", type=float, default=0.5, help="pressure で燃圧が戻ってから続行までの待ち秒数")
    p.add_argument("--pause-s", type=float, default=0.0, help="time の待ち秒数")
    p.add_argument("--scope", choices=("none", "first", "last", "alt"), default="none",
                   help="DHO804で撮る1発: first=先頭 / last=終わり際 / alt=行ごとに交互")
    p.add_argument("--wave-dir", default="waves", help="波形(.npz)の保存先")
    p.add_argument("--vbat", type=float, default=None, help="電源電圧 V を手入力で記録（オシロを使わない場合）")
    p.add_argument("--operator", default="", help="測定者")
    p.add_argument("--fuel-temp", type=float, default=None, help="燃料温度 ℃")
    p.add_argument("--density", type=float, default=0.754, help="燃料密度 g/ml（体積換算と分割発数の計算に使う）")
    p.add_argument("--simulate", action="store_true", help="機材なしで流れを確認する（擬似ECU・燃圧・はかり）")
    p.add_argument("--verbose", action="store_true", help="試験ファームとの送受信を表示")
    import inj_scope
    inj_scope.add_scope_args(p)


def main():
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="1バーストを実行")
    r.add_argument("--pw-ms", type=float, required=True, help="パルス幅 ms")
    r.add_argument("--n", type=int, required=True, help="総発数")
    r.add_argument("--no", default="", help="通し番号")
    r.add_argument("--phase", default="", help="フェーズ名")
    r.add_argument("--rep", default="", help="反復回数")
    r.add_argument("--target-mpa", type=float, default=None, help="目標燃圧 MPa")
    r.add_argument("--note", default="", help="備考")
    add_common(r)
    p = sub.add_parser("plan", help="計画CSVを順に実行（--out に結果がある行は飛ばす）")
    p.add_argument("plan", help="計画CSV（injector_test_log_template.csv の形式）")
    p.add_argument("--phase", default="", help="実行するフェーズと順序（前方一致、カンマ区切り。例: P2,P1）")
    add_common(p)
    a = ap.parse_args()
    try:
        {"run": cmd_run, "plan": cmd_plan}[a.cmd](a)
    except KeyboardInterrupt:
        print("\n中断しました（試験ファームへ停止 S を送信済み）。念のためキルスイッチを戻してください")
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
