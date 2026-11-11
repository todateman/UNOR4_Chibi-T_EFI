# インジェクタ流量試験ツール

`src/injector_test.cpp`（第2版）と組み合わせて、無効噴射時間・流量の測定を1バースト1コマンドで行うためのPC側スクリプト。
測定の考え方と過去データの考察は [document/Investigation_of_Injector_Characteristics.md](../document/Investigation_of_Injector_Characteristics.md) を参照。

| ファイル | 役割 |
| --- | --- |
| `inj_bench.py` | 測定の実行。設定送信 → 捨て打ち → 重量入力 → 噴射 → 燃圧記録 → 波形取得 → CSV追記 |
| `inj_scope.py` | RIGOL DHO804 の操作と、1発分の波形の解析（電源電圧・実パルス幅・フライバック・電流） |
| `inj_analyze.py` | ログCSVの回帰解析（流量・実効オフセット・バースト固定量・短パルス補正） |

## 準備

```sh
pip install -r tools/requirements.txt          # pyserial / numpy / pyvisa / matplotlib
pio run -e uno_r4_minima_inj_test -t upload    # 試験ファーム（UNO R4）
```

燃圧は [Chibi-T_Furoshiki_AutoAirAdjust](https://github.com/todateman/Chibi-T_Furoshiki_AutoAirAdjust/tree/feature/sensing-only) の
`m5stack-core2-sensing-only` ビルドがUSBシリアルへ出す `[BLE TX] ... FUEL=0.35012 ...` の行を読む。
この値は約200〜250msごとの更新で4点移動平均がかかっているため、バースト中の平均と垂れは分かるが、1発ごとの脈動は見えない。
脈動を見たい場合は燃圧センサの出力をオシロのCH4へ入れる。

燃料の加圧は [XIAO_PulseCreate](https://github.com/todateman/XIAO_PulseCreate) がタイマーで Hi → PWM → Lo を繰り返している
（既定 0.2秒 → 7.8秒 → 0.5秒）。Lo の間は加圧が抜けてダイヤフラムが燃料を吸い直すので、噴射と無関係に回っていると、
バーストの途中に燃圧の谷が1〜数回入る。`--pump-port` に XIAO のUSBシリアル（`DEBUG 1` のビルドが出す
`countmode, hiTime, pwmTime, loTime, Duty / 4096` の行）を指定すると、ポンプが Hi に入って `--pump-settle-s`（既定1.0秒）
経ってから噴き始め、Lo の `--pump-margin-s`（既定0.5秒）前までに噴き終えるよう、バーストを自動で分割して同期する。
XIAO側のファームは変更不要。TriggerPin が LOW（ポンプ停止）のときは、その旨を表示して待つ。

オシロの接続（既定。`--ch-*` で変更、0で未使用）:

| チャネル | 接続先 | 分かること |
| --- | --- | --- |
| CH1 | インジェクタ−端子（ローサイド側）。10:1プローブ | 電源電圧（通電前の値）、実パルス幅、フライバック電圧と継続時間 |
| CH2 | コイル電流（電流プローブ。`--cur-a-per-v` で感度指定） | 電流ピーク、OFF後の減衰時間、開弁キンク（参考） |
| CH3 | INJ_OUT（A0）の指令信号 | 指令幅、ドライバ段のON/OFF遅れ |
| CH4 | 燃圧センサ出力（0.5V=0MPa, 4.5V=1.0MPa） | 通電前後の燃圧 |

オシロは LAN でつなぐ（試験は macOS で行う）。既定の接続先は `TCPIP::192.168.1.2::INSTR`（Mac 側の USB-LAN アダプタを 192.168.1.1 にして直結）で、
違う場合は `--scope-resource` で指定する。VISA は pyvisa-py を使う（`requirements.txt` に含む）。
USB 直結（`USB0::6833::1101::DHO8A253701207::0::INSTR`）は、設定・測定値の問い合わせは通るが、
pyvisa-py では波形の読み出し（`:WAVeform:DATA?`）が返ってこないので使わない（2026-11-11 確認）。

## 測定（inj_bench.py）

```sh
# 機材なしで流れを確認（擬似ECU・擬似燃圧・擬似はかり）
python tools/inj_bench.py plan document/injector_test_log_template.csv --phase P2 --simulate --out sim.csv

# 1バーストだけ（4ms x 750発）
python tools/inj_bench.py run --ecu-port /dev/cu.usbmodem101 --fuel-port /dev/cu.usbserial-XXXX --pw-ms 4 --n 750 \
    --out document/injector_test_log_20261115.csv

# 計画CSVを順に実行。P2（N振り）を先に、オシロは先頭/終わり際を行ごとに交互に撮る
python tools/inj_bench.py plan document/injector_test_log_template.csv --phase P2,P1 \
    --ecu-port /dev/cu.usbmodem101 --fuel-port /dev/cu.usbserial-XXXX --pump-port /dev/cu.usbmodemYYYY --scope alt --cur-a-per-v 10 \
    --operator yabuki --fuel-temp 25 --out document/injector_test_log_20261115.csv
```

- `plan` は `--out` に有効な結果がある No を飛ばすので、中断しても同じコマンドで続きから再開できる。
- 重量は、はかりの表示を見てキーボードから入力する。捨て打ち（既定20発）の後に開始重量を読むので、容器は載せたままでよい。
- 1回に連続で噴く量が `--max-burst-ml`（既定1.5ml。ダイヤフラムポンプ2ccに対する余裕）を超える場合は自動で分割する。
  分割の間は `--resume` で続行方法を選ぶ: `enter`（再加圧してEnter）/ `pressure`（燃圧が戻ったら自動）/ `time`（`--pause-s` 秒後）。
  `--pump-port` を指定した場合は `--resume` によらずポンプ周期に同期し、1回分は「加圧が安定している区間に収まる発数」
  （既定設定・周期50msなら124発）と上記の量の上限の小さい方になる。
- 周期は `--period-ms`（既定50ms）で固定。第1版の「OFF区間50ms」と違い、パルス幅によらず1発ごとの間隔が同じになる。
- オシロを使わない場合は、バーストごとに電源電圧の入力を求める（Enterで前回値）。`--vbat` で固定値も指定できる。
- Ctrl+C やエラーで止まったときは、試験ファームへ停止 `S` を送ってから終了する。

出力CSVは `injector_test_log_template.csv` の列をそのまま持ち、その後ろに実発数・実測パルス幅・燃圧の統計・ポンプ設定・オシロの解析値を追加している。

## 解析（inj_analyze.py）

```sh
python tools/inj_analyze.py document/injector_test_log_20261115.csv --plot fit.png
python tools/inj_analyze.py document/injector_test_log_20260822.csv      # 第1版の手書きログも読める
```

1バーストの総質量を `m = a·N·(PW − t0) + C` で回帰する。

- `a`: 流量 [mg/ms]
- `t0`: 実効オフセット [ms]。正なら通常の無効噴射時間。負なら指令より長く噴いている（閉弁遅れ > 開弁遅れ、またはドライバのOFF遅れ）。
- `C`: バーストごとの固定量 [mg]。同じパルス幅で N を振った行（P2）があると `t0` から分離できる。N が全行同じログでは分離できないので 0 として計算し、その旨を表示する。

回帰に使うのは `--pw-min`〜`--pw-max`（既定1〜6ms）で、範囲外の水準は「直線モデルからのずれをパルス幅に換算した値」を表に出す（短パルス補正表の元データ）。
電圧スイープ（P3）は流量を共通として電圧ごとの `t0` を、燃圧スイープ（P4）は `t0` を共通として燃圧ごとの流量を出す。

## オシロ単体（inj_scope.py）

```sh
python tools/inj_scope.py arm --pw-ms 4     # 単発トリガーをアーム（この後に1発噴く）
python tools/inj_scope.py wait
python tools/inj_scope.py read shot.npz --cur-a-per-v 10
python tools/inj_scope.py analyze shot.npz --cur-a-per-v 10   # 保存済み波形を再解析
```

「フライバック最大」が電源電圧+1V程度で止まっていれば還流ダイオード型で、OFF後も電流が数ms流れ続けて閉弁が遅れる。
回帰の `t0` が負になる原因の切り分けには、この値と「OFF遅れ」「電流減衰」を見る。

## 試験ファームのコマンド

115200bps、改行終端。1行に空白区切りで複数書ける（例: `U4000 N750 T50 B300 H0`）。

| コマンド | 内容 |
| --- | --- |
| `U<us>` / `W<ms>` | パルス幅（50〜30000us） |
| `N<発数>` | 総発数 |
| `T<ms>` | 周期（ONからON。固定周期） |
| `I<ms>` | OFF区間（周期 = パルス幅 + I。第1版互換） |
| `B<発数>` | 分割1回分の発数（0 = 分割なし） |
| `H<ms>` | 分割の間の待ち時間（0 = `C` が来るまで待つ） |
| `Q<発数>` | 捨て打ちを今すぐ実行 |
| `G` / `C` / `S` | 開始 / 分割待ちから続行 / 停止 |
| `?` | 設定と状態を表示 |

開始（`G`・`Q`・スタータボタン）にはキルスイッチOFF（導通）が必要で、噴射中・分割待ち中にキルスイッチを戻すと即中断する。
パルスは `main.cpp` と同じ AGTimer 24us 周期の割り込みで生成しているので、実パルス幅は指令より0〜24us長くなる（実車と同じ）。
完了時に出る `#INJ,DONE,...` の `w_mean_us` が、`micros()` で測った実パルス幅の平均。
