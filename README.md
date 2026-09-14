﻿# UNOR4_Chibi-T_EFI

Arduino UNO R4（RA4M1）/ 互換環境上で動作するエコラン車両用ECUプログラム

## 概要

- メイン周期処理: [`Routine`](src/main.cpp)
  - (AGTimerライブラリ [`AGTimer.init`](lib/AGTimer_R4_Library/src/AGTimerR4.h) による24usec毎処理)
- 割り込み:
  - 車速入力: [`WH_PULSE_ISR`](src/main.cpp)
  - クランク角 A: Arduino attachInterrupt + `ReadNe_ISR`（パルスでクランク角更新）
  - カム角: [`G_PULSE_ISR`](src/main.cpp)
- 高速 GPIO: [`fastestdigitalWrite` / `fastestdigitalRead`](src/fastestdigitalRW.hpp)
- フリータスク: FreeRTOS
  - [`statusTask`](src/main.cpp) : Serial1 出力 100ms 周期 (10Hz) / SerialUSB 出力 500ms 周期 (2Hz)
  - 5ms 周期 [`mapConsoleTask`](src/map_console.cpp) : MAP 書き換えコンソール

## ディレクトリ構成（抜粋）

- [src/main.cpp](src/main.cpp) : コアロジック
- [src/map_store.cpp](src/map_store.cpp) : MAP のダブルバンク保持と EEPROM 永続化
- [src/map_console.cpp](src/map_console.cpp) : USB シリアル経由の MAP 書き換えコンソール
- [src/fastestdigitalRW.hpp](src/fastestdigitalRW.hpp) : ボード別最速 GPIO
- [tools/send_map.py](tools/send_map.py) : PC 側の MAP 転送スクリプト
- [lib/AGTimer_R4_Library](lib/AGTimer_R4_Library) : 周期タイマ
- [platformio.ini](platformio.ini) : ビルド環境定義
- microSD/ : MAP 用 CSV（`tools/send_map.py` でそのまま転送できる）
- log/ : 記録例
- [document/arduino-workflow.puml](document/arduino-workflow.puml) : PlantUML ワークフロー図（詳細版）

## 対応ボード / ビルド

PlatformIO 環境: [platformio.ini](platformio.ini)

| Env | ボード | 備考 |
| --- | ------ | ---- |
| `uno_r4_minima` | Arduino UNO R4 Minima | デフォルト |
| `rmc_ra4m1_20` | カスタム RA4M1 (`-D rmc_ra4m1_20`) | SD動作分岐あり |

ビルド例:

```sh
pio run -e uno_r4_minima
pio run -t upload
pio device monitor -b 115200
```

## 主要定数 / パラメータ

| 項目 | 定義 | 説明 |
| ---- | --- | ---- |
| ROUTINE_CYCLE_US | 24 | メイン周期 (µs) |
| PERIMETER_MM | 1548 | タイヤ周長(mm) |
| TACHO_RPM_MAX | 6000 | レブリミット <BR> （回転数上限保護） |
| Dwell_Time_US | 5000 | ドゥエル時間（us） <BR> IGコイルへの充電時間 |
| start_RPM | 1500 | スタータ自動停止の判定回転数（RPM） <BR> MAP切替用の専用定数ではない |

始動時・通常時で切り替わる専用定数（`start_INJ_time` 等）は無い。  
燃料噴射量・点火進角・燃料噴射終了タイミングは全て単一の MAP テーブルから rpm に応じて読み出され、ビルドし直さずに USB シリアルから調整できる。  
（[MAP](#map) 節参照）

## ピン割り当て

- WH_IN / G_IN / STR_IN / ENGOFF_IN は `74HC14` によるシュミットトリガ回路でチャタリング防止・反転入力
- INJ_OUT / IGN_OUT / STR_OUT / DISRESET_OUT は `Nch MOSFET` による LOW アクティブ。  
- 詳細は [src/main.cpp](src/main.cpp) 参照。

| 信号 | 物理ピン | 説明 |
| ---- | -------- | ---- |
| NE_A_IN | 2 | クランク角 A (1deg/パルス) |
| NE_B_IN | 8 | クランク角 B (位相判定) |
| NE_Z_IN | 9 | クランク角0deg基準 |
| WH_IN | 3 | 車軸パルス入力 <BR> 1回転で1パルスIN |
| G_IN | 5 | カムパルス入力 <BR> クランク角720°毎に1パルス入力 |
| STR_IN | 6 | エンジンスタートスイッチ<BR>（LOW=ON） |
| ENGOFF_IN | 7 | キルスイッチ<BR>（LOW=ON=運転状態, HIGH=OFF=停止状態） |
| INJ_OUT | A0 | 燃料噴射<BR>（LOW=ON） |
| IGN_OUT | A1 | 点火<BR>（LOW=ON） |
| STR_OUT | A2 | スタータリレー<BR>（LOW=ON） |
| DISRESET_OUT | A3 | リセットランプ<BR>（LOW=点灯, HIGH=消灯） |
| MA735_CS | 10 | MA735 SPI CS |

LOW アクティブ出力注意 (INJ/IGN/STR/DISRESET)。

## 信号イメージ

![Engine cycle diagram showing crankshaft and camshaft pulse timing](document/engine_cycle.png)

## 処理フロー概要

1. 割り込みで角度/速度更新  
   - `ReadNe_ISR`: NE_A パルスで `Ne_deg` ±1 更新（NE_Bの位相参照）  
   - `G_PULSE_ISR`: カムパルス同期フラグ設定  

   - 角度モデルと usecperdig
     - `usecperdig` は非エンコーダ・非MA735モード時にのみ補間に使用。  
       `Ne_deg += ROUTINE_CYCLE_US / usecperdig` (エンコーダ無効かつ MA735無効時)。
     - 通常は `EncoderEnabled = true` のため使用されない（初期値 `1.0`、無信号タイムアウト時にリセット）。

2. 周期関数 [`Routine`](src/main.cpp):
   - スタート/キル状態評価
   - **スタータステートマシン** (`StarterState`): キルスイッチON時にスタートボタン押下（エッジ）でクランキング開始。  
     `start_RPM` 以上を 0.1秒 維持で始動成功（スタータ自動停止）、2秒タイムアウトで始動失敗（再押しで再試行可）。  
     低rpm帯のMAP行（400/800/1200rpm、`inj_end_ca`=20）が始動用パラメータとして自動的に採用される  
     （始動専用のMAP切替フラグは無い。[MAP](#map) 節参照）。

     ```txt
     STR_IDLE → [ボタン押下(エッジ)+キルスイッチON] → STR_CRANKING
     STR_CRANKING → [start_RPM以上を0.1秒維持] → STR_STARTED（スタータOFF）
     STR_CRANKING → [2秒タイムアウト] → STR_FAILED（スタータOFF・ENG_ON OFF）
     STR_FAILED → [ボタン再押し(エッジ)] → STR_CRANKING
     キルスイッチOFF → 常に STR_IDLE へ強制リセット（`Launch` はリセットしない）
     ```

   - カム同期タイムアウト→`cycleReset`
   - マップ更新: [`updateEngineMap`](src/main.cpp)
   - 噴射開始条件 (角度 >= `INJ_STR_CA`、MAP行の `inj_end_ca` と噴射時間から逆算、0CA跨ぎ対応)
   - 360CA通過時に噴射継続中なら強制OFF（安全リセット）
   - 噴射時間経過で OFF & 燃料量積算
   - 点火進角計算 & 保持時間後 OFF

3. タスク [`statusTask`](src/main.cpp):
   - 高速パス (10Hz/100ms): Serial1 CSV出力 (`snprintf` + 一括 `write`)
   - 低速パス (2Hz/500ms): 状態計算 (燃費, 稼働時間, 速度減衰), SerialUSB 出力

## 燃料噴射計算

噴射終了角度: `calculatedINJ_END_CA`（MAPの該当行の `inj_end_ca` 列をそのまま採用。  
始動時・通常時で切り替える専用ロジックは無く、rpmに応じたMAP行選択（[MAP](#map) 節）に一本化されている）  

噴射開始角度: `INJ_STR_CA`（毎サイクルリセット時に逆算）  

```text
INJ_STR_CA = calculatedINJ_END_CA - (calculatedINJ_time * 100[µs] * 360[deg]) / tachoWidth[µs]
// INJ_STR_CA < 0 の場合（0CA跨ぎ）: INJ_STR_CA += 720
```

**0CA跨ぎ対応**: 噴射開始が720CA付近・終了が次サイクル序盤（例: 716CA→20CA）の場合、
`INJ_STR_CA` が負値になるため `+720` で正規化（0〜720CAの範囲に収める）。  
`cycleReset()` 呼び出し時に噴射継続中（`INJ_Status == 2`）であれば INJ状態をリセットせず、
タイマーで正常終了させる。  

**360CA安全リセット**: 1サイクル内で360CAを通過した時点でも噴射中の場合は強制OFFし、
意図しない噴射継続を防止（`inj360Reset` フラグで1サイクルに1回限り実行）。  

噴射時間: `calculatedINJ_time` (0.1ms単位) → 実際 µs: `injDuration = calculatedINJ_time * 100`  
燃料量近似:

```text
gasml += ( (Δt * 0.0000007) + 0.0015 ) / 1.5073
```

係数は実測燃費から逆算したインジェクタ流量補正。

## 点火制御

- 進角: `calculatedIGN_CA`  
- ドゥエル時間（µs）: `Dwell_Time_US`
  - ドゥエル時間をクランク角に変換: `Dwell_Time_CA = Dwell_Time_US * 360 / tachoWidth`
- 条件: `Ne_deg >= (360 - calculatedIGN_CA - Dwell_Time_CA)` で点火 LOW  
- 保持: クランク角が `360 - calculatedIGN_CA` に達する or `Dwell_Time_US` 経過で HIGH 戻し  
- 点火: HIGH 戻しの瞬間にスパークプラグから放電

## MAP

MAP は RAM 上のダブルバンク（[src/map_store.cpp](src/map_store.cpp)）で保持し、**ビルドし直さずに USB シリアル経由で書き換えられる**。

- 参照ロジック: RPM 昇順テーブルの、最初に `tachoRpm < rpm` となるエントリを採用（階段状）。
- 行数は可変（最大 24 行）。
- 列順（CSV / SD 読込も同一。**4列必須**）:

  ```text
  rpm,inj_time(x0.1msec),ign_ca(deg),inj_end_ca(deg)
  ```

  - `inj_end_ca`（噴射終了タイミング角度、0〜720CA）は、スタートボタン押下直後を含む全rpm帯で共通のMAP行から読み出される。  
    始動時専用のハードコード定数は無い。
  - 3列（旧書式）のCSVはエラーになる。  
    `tools/send_map.py --legacy-inj-end-ca <値>` で噴射終了角度を明示的に補って送ること（無断補完はしない）。
- 内蔵 `defaultMap`（[src/map_store.cpp](src/map_store.cpp)）は、400/800/1200/1600rpm をブレークポイントに以下のように構成されている:

  | RPM未満 | inj_time | ign_ca | inj_end_ca | 意味 |
  | --- | --- | --- | --- | --- |
  | 400 | 0 | 0 | 0 | 400rpm以下はアイドリング不能なのでエンジン停止相当 |
  | 800 | 80 | 0 | 20 | 800rpm以下は始動状態 |
  | 1200 | 80 | 0 | 20 | 1200rpm以下は始動状態 |
  | 1600〜 | 可変 | 可変 | 680 | 通常走行域 |

- 起動時の優先順位: **EEPROM に有効な MAP があればそれを採用**、無ければ内蔵 `defaultMap` へフォールバック。  
  起動時に `MAP SOURCE: EEPROM (15 rows)` のようにどちらを使ったか出力する。
- 書き換えは AGTimer 割り込みから参照されるバンクを非アクティブ側で組み立ててから 1 バイトのストアで切り替えるため、**エンジン稼働中でも安全に反映できる**。

### MAP 調整 Web GUI

回転数を見ながら GUI で MAP を詰めるためのWebアプリ（[webgui/](webgui/)）。  
Windows / macOS / Ubuntu 共通。  
接続方法は 2 通りあり、**どちらも同じフロントエンド**が動く。

| 方法 | 必要なもの | 使えるブラウザ |
| --- | --- | --- |
| GitHub Pages（Web Serial） | なし（ブラウザだけ） | Chrome / Edge |
| ローカルサーバ | Python 3 + `pyserial` | すべて |

```sh
# ローカルサーバ（追加の依存なし。標準ライブラリ + pyserial だけ）
python3 tools/map_gui.py                     # ポート自動検出、ブラウザを開く
python3 tools/map_gui.py --port /dev/cu.usbmodem1101
python3 tools/map_gui.py --fake              # 実機なしでGUIを試す（モックECU）
```

GitHub Pages 版は `https://todateman.github.io/UNOR4_Chibi-T_EFI/` で、
Chrome / Edge の「Web Serial で接続」から直接 USB に繋がる。  
外部 CDN を一切使っておらず Service Worker を積んであるので、**一度開けばピットでオフラインでも動く**。  
Pages 版にはローカルサーバが居ないため、「ローカルサーバ経由」「モック ECU」のボタンは
自動的に無効になる（起動時に `/api/status` の応答を確かめている）。

公開するには初回だけ次の 2 つが必要。

1. リポジトリの **Settings → Pages → Source** を `GitHub Actions` にする
2. `webgui/` を含む変更を **`main` へマージ**する  
   （[.github/workflows/pages.yml](.github/workflows/pages.yml) は `main` への push でのみ動く。  
   手動実行したい場合は Actions タブの `Deploy Web GUI to Pages` → `Run workflow`）

デプロイ前にテスト（`node --test` と `test_map_protocol.py`）が走り、失敗すると公開されない。

できること:

- CSV の読み込み / 書き出し（`microSD/RPM_*.CSV` と同じ書式）
- グラフの点をドラッグして編集。選択範囲に ±5% / ±1 段 / 線形補間 / 平滑化
- Undo / Redo（Ctrl+Z / Ctrl+Shift+Z）と、実機 MAP との差分表示
- **ライブトレース**: 現在の回転数と、ファームが実際に採用している行をハイライト。  
  行ごとの滞在時間をヒートマップで表示
- **ライブ適用**: RPM ブレークポイントが実機と一致していれば、変更行だけを `MAP SET` で送る。  
  セッションを使わないのでテレメトリが途切れず、エンジンを止める必要もない
- テレメトリの CSV 記録と再生
- 転送後は **CRC で検証**する（全行読み戻すより速く、実機の内部表現と直接比較できる）

安全のために GUI 側で次を強制している。

- `MAP SAVE` は `eng=OFF` かつ `rpm=0` のときだけ押せる。  
  ベンチではノイズで `ERR ENGINE_RUNNING` が返ることがあるので最大 5 回リトライする
- **稼働中は RPM 列を編集できない**。`MAP SET` は該当 RPM が無いと行を挿入する仕様なので、走行中に意図せずテーブル構造が変わるのを防ぐ
- ライブ適用は明示的に有効化したときだけ動き、**60 秒無操作で自動解除**。  
  1 操作の変化量が噴射 ±1.0ms / 進角 ±5CA を超える場合は送らない
- 送信前にファームと同じ規則で検証し、違反セルを赤表示して転送を止める
- **MAP 最終行の RPM がレブリミット（`TACHO_RPM_MAX` = 6000）より手前だと警告する**。  
  最終行を超えると噴射・点火が止まる（`mapOutOfRange`）ため

テストは実機なしで走る。  

```sh
node --test "webgui/test/*.test.mjs"   # 検証規則・CRC・編集操作・行分類
python3 tools/test_map_protocol.py     # プロトコル往復（モックECU）
```

### MAP の書き換え（PC 側スクリプト）

`microSD/RPM_*.CSV` と同じ書式の CSV をそのまま送れる。`pyserial` が必要。  
プロトコルの実装は [tools/map_protocol.py](tools/map_protocol.py) にあり、Web GUI と共有している。

```sh
pip install pyserial

# 転送のみ（RAM へ反映。電源を切ると元に戻る）
python tools/send_map.py microSD/RPM_2026SUZUKA.CSV

# 転送 + EEPROM へ保存（エンジン停止時のみ）
python tools/send_map.py microSD/RPM_2026SUZUKA.CSV --save

# 現在の MAP を吸い出す
python tools/send_map.py --dump > current_map.csv

# 状態確認 / ポート明示
python tools/send_map.py --info
python tools/send_map.py map.csv --port /dev/cu.usbmodem1101

# 実機なしで動作確認（モックECU）
python tools/send_map.py microSD/RPM_2026SUZUKA.CSV --fake

# 3列（旧書式）CSVを送る場合は噴射終了角度を明示的に補う
python tools/send_map.py microSD/RPM_2024MOTEGI.CSV --legacy-inj-end-ca 680
```

送信前にファームと同じ規則で検証し、通らない CSV は実機に触れる前に弾く。  
転送後は `MAP INFO` の CRC と自前の計算値を突き合わせて検証し、不一致なら非ゼロ終了する。

### MAP コンソールコマンド

シリアルモニタから手打ちでも操作できる（[src/map_console.cpp](src/map_console.cpp)）。  
応答は必ず 1 行の `OK ...` / `ERR ...`。

| コマンド | 動作 | 稼働中 |
| --- | --- | --- |
| `MAP?` | 現在の MAP を CSV で出力（4列） | 可 |
| `MAP INFO` | 出所 / 行数 / CRC / EEPROM 状態 / RPM / ENG を表示 | 可 |
| `MAP BEGIN` | 転送セッション開始<BR>（USB テレメトリを一時停止） | 可 |
| `<rpm>,<inj>,<ign>,<inj_end_ca>` | セッション中の 1 行<BR>（`RPM` で始まるヘッダ行は自動スキップ。4列必須） | 可 |
| `MAP END` | 検証して原子的に反映。失敗時は破棄され現在の MAP は無傷 | 可 |
| `MAP ABORT` | セッション破棄 | 可 |
| `MAP SET <rpm> <inj> <ign> <inj_end_ca>` | 1 行だけライブ変更<BR>（該当 RPM が無ければ昇順を保って挿入） | 可 |
| `MAP SAVE` | EEPROM へ保存<BR>（既存内容と同一なら書き込まず `OK UNCHANGED`） | **不可** |
| `MAP LOAD` | EEPROM から読み直して RAM へ反映 | 可 |
| `MAP DEFAULT` | 内蔵 `defaultMap` へ戻す<BR>（EEPROM は変更しない） | 可 |
| `TELEM ON [ms]` | 機械可読テレメトリを開始<BR>（既定 OFF、100〜2000ms、100ms 単位） | 可 |
| `TELEM OFF` | 停止して従来の 2Hz 人間向け出力へ戻す | 可 |
| `TELEM?` | `on=` / `ms=` / `drop=`（取りこぼし数）を表示 | 可 |
| `VER` | `OK VER <fw> proto=<n>` | 可 |
| `PING` | `OK PONG`<BR>（副作用のない疎通確認・レイテンシ計測） | 可 |
| `HELP` | コマンド一覧 | 可 |

セッションを開いたままホストが消えた場合に備え、**最終受信から 5 秒で** `ERR SESSION_TIMEOUT` を返してセッションを破棄する。  
（テレメトリがミュートされたまま復帰しなくなるのを防ぐ）

### 機械可読テレメトリ（`TELEM`）

GUI のライブトレース用に、`statusTask` の高速パス（100ms 周期）から出力する。  
**既定は OFF** なので、`tools/send_map.py` やシリアルモニタの見え方は従来どおり変わらない。  
`TELEM ON` の間は 2Hz の人間向けタブ行を止め、2 つの書式が混ざらないようにする。

```text
T\t<seq>\t<ms>\t<rpm>\t<inj01>\t<ign>\t<spd01>\t<ne>\t<row>\t<flags>
```

| 欄 | 内容 | 単位 |
| --- | --- | --- |
| `seq` | 連番（uint16 wrap）。取りこぼし検出に使う | — |
| `ms` | `millis()` | ms |
| `rpm` | `tachoRpm` | RPM |
| `inj01` | `calculatedINJ_time` | ×0.1ms |
| `ign` | `calculatedIGN_CA` | CA |
| `spd01` | `speed` | ×0.1km/h |
| `ne` | `Ne_deg` | CA |
| `row` | 採用中の MAP 行 index（255 = MAP 未使用。始動時・範囲外） | — |
| `flags` | bit0 `ENG_ON` / bit1 `Launch` / bit2 クランキング / bit3 `mapOutOfRange` | — |

- **タブ区切りを保っている**のは、`send_map.py` が「タブを含む行＝テレメトリ」として
  読み飛ばす実装だから。  
  この書式なら CLI を 1 行も変えずに共存できる。
- **整数のみ**。newlib-nano の `snprintf` が `%.1f` 非対応なのに合わせてある。  
  単位換算は PC 側の責務。
- `row` をファームから出しているのは、GUI が「実機が実際に採用した行」を推測せずに
  表示できるようにするため。  
  クランキング中（`startState == LOW`）も rpm に応じた同じ MAP テーブルを参照するが、始動直後は rpm の変動が大きく PC 側の推測は容易にズレるため、ファーム自身が報告する `row` を信用する設計にしてある。
- USB CDC の `write()` はホストが接続したまま読まないと FIFO が空くまで無限にスピンする。  
  `usbLock` を握ったままそうなるとコンソールが応答しなくなるため、 `Serial.availableForWrite()` で空きを確認してから書き、足りなければそのサンプルを捨てる。  
  （落ちた分は `seq` の飛びで GUI が検出できる）

### 検証ルール

`MAP END` / `MAP SAVE` 時に以下を検査し、1 つでも違反したら**全体を破棄**する（部分適用しない）。

- 行数 1〜24
- RPM が厳密昇順、かつ 1〜20000
- `inj_time` 0〜255（x0.1ms → 最大 25.5ms）
- `ign_ca` 0〜90（CA）
- `inj_end_ca` 0〜720（CA。クランク角のフルサイクル、0CA跨ぎ対応と整合）

### EEPROM（データフラッシュ）

- UNO R4 Minima の RA4M1 データフラッシュ 8KB（消去単位 1KB）を Arduino `EEPROM` ライブラリ経由で使用。
- 先頭 1 ブロック内に magic / version / 行数 / CRC-16 / エントリを格納（約 152 バイト）。  
  CRC 不一致・magic 不一致なら内蔵 `defaultMap` へ自動フォールバックする。
- `MAP SAVE` は書き込み前に EEPROM 上の内容と行数 / CRC / エントリ本体を突き合わせ、**同一なら書き込みを行わず** `OK UNCHANGED` を返す（データフラッシュの摩耗対策）。  
  なお構造体一括の書き込みには `EEPROM.put` を使うこと。  
  `EEPROM.update` はバイト単位 API で、差分 1 バイトごとに 1KB ページの消去＋書き込みが走るため摩耗対策として逆効果になる。
- **`MAP SAVE` はエンジン停止時（`ENG_ON == false` かつ `tachoRpm == 0`）のみ受理**し、それ以外は `ERR ENGINE_RUNNING` を返す。  
  データフラッシュの消去・書き込みは数 ms ブロックするため、 24µs 周期の点火・噴射制御中に実行してはならない。

### その他

- SD カード MAP: 現状 SD は未使用。  
  `SDMapEnabled` / `parseCSV()` のスタブは残してあり、実装時は `mapParseCsvLine()` / `mapStagingAppend()` / `mapStagingApply()` をそのまま再利用できる。
- AFR 補正: A/Fセンサによる燃料噴射量補正のため、`Increase_Fuel` 分岐位置あり（未実装）

### 実機確認チェックリスト

CSV パース・検証ルール・バンク切替・`send_map.py` のプロトコルはホスト側のテストで確認済み。  
以下は**実機でしか確認できない項目**なので、車両に載せる前に順に潰すこと。  
`1` → `8` の順（無負荷 → クランキング → 実走）で進めると手戻りが少ない。

検証結果（Ardu-Stim + オシロによる代替検証、発見した不具合と修正内容を含む）:

- [document/eeprom_map_console_verification_20260912.md](document/eeprom_map_console_verification_20260912.md)

#### 1. 起動時フォールバック（エンジン停止・USB のみ）

```sh
pio run -t upload
pio device monitor -b 115200
```

- [x] 起動直後に `MAP SOURCE: DEFAULT (15 rows)` が出る  
  ※ EEPROM 未初期化のボードのみ。既に `MAP SAVE` 済みなら `MAP SOURCE: EEPROM (n rows)` が正しい
- [x] 起動メッセージを取り逃した場合は `MAP INFO` で `src=` / `eeprom=` を確認できる  
  （USB CDC が未接続の間の出力は捨てられるため、電源投入がモニタ起動より先だと起動行は流れない）
- [x] `MAP?` の出力が `src/map_store.cpp` の `defaultMap[]` と一致する
- [x] `HELP` でコマンド一覧が出る

#### 2. CSV 転送 → RAM 反映

```sh
python tools/send_map.py microSD/RPM.CSV
```

- [x] `RAMへ反映しました（読み戻し検証OK）` が出て終了コード 0
- [x] `MAP?` が `RPM.CSV` の内容（INJ=110 系）になっている
- [x] 転送中に USB テレメトリ（タブ区切り行）が止まり、転送後に再開する
- [x] 転送中も `Serial1`（メーター・ロガー）側の CSV 出力は途切れない
- [x] 24 行の CSV でも RX FIFO 溢れなく通る（1 行ごとの ACK 同期が効いているか）

#### 3. EEPROM 永続化（電源断をまたぐ）

```sh
python tools/send_map.py microSD/RPM_2026SUZUKA.CSV --save
```

- [x] `EEPROMへ保存しました` が出る
- [x] **USB を抜き差しして再起動** → `MAP SOURCE: EEPROM (15 rows)`
- [x] `MAP?` が 2026SUZUKA の内容
- [x] `MAP DEFAULT` → `MAP?` が既定値 → `MAP LOAD` → `MAP?` が EEPROM の内容に戻る

#### 4. 安全ガード（クランキング／エンジン始動）

Ardu-Stim で回転信号を与えるか、実際にクランキングした状態で:

- [x] `MAP SAVE` → `ERR ENGINE_RUNNING` が返る
- [x] 拒否後に電源を落として再起動しても、EEPROM の内容が変わっていない
- [x] `MAP SET 3200 45 26 680` → `OK SET` が返り、`MAP?` に即反映される
- [x] `MAP BEGIN` 〜 `MAP END` の一括転送が稼働中でも通り、反映される
- [x] `MAP SET` / `MAP END` の実行前後で**エンジンが失火・ストールしない** ← 最重要

#### 5. 不正入力で既存 MAP が壊れないこと

```sh
printf 'RPM,INJ,IGN\n3000,40,20\n1000,40,20\n' > /tmp/bad_order.csv
python tools/send_map.py /tmp/bad_order.csv     # ERR RPM_NOT_ASCENDING で非ゼロ終了
```

- [x] RPM 非昇順 / 25 行以上 / `ign_ca` 91 以上 のいずれでも `MAP END` が `ERR` を返す
- [x] そのいずれの後でも `MAP?` の内容が**転送前から変化していない**
- [x] 転送の途中で USB ケーブルを抜いても、次回起動時の MAP が壊れていない

#### 6. リアルタイム性の非退行（オシロ）

「デバッグ」節の Ardu-Stim + DHO804 の構成で、変更前のファームと比較する:

- [x] 転送・保存を行わない通常運転で、`INJ_OUT`(A0) / `IGN_OUT`(A1) のタイミングが変更前と一致
- [x] USB シリアルのロギングが従来どおり途切れない
- [x] `MAP SET` 実行の瞬間に噴射パルスが1サイクル内で分断・二重化しない  
  （バンク切替が原子的に効いているかの実測確認）
- [ ] `MAP SAVE`（停止時）中に `Ne_deg` のカウント抜けが無い（未検証、理由は検証結果ドキュメント参照）

#### 7. 実走前の最終確認

- [x] 使用する MAP を `--save` で EEPROM に焼き、再起動して `MAP INFO` の `crc=` を記録
- [x] `python tools/send_map.py --dump` の出力が意図した CSV と一致
- [x] レブリミット（`TACHO_RPM_MAX` = 6000）付近で MAP 最終行を超えたとき、
      `calculatedINJ_time` / `calculatedIGN_CA` が 0 になり燃料噴射・点火が止まる
- [ ] 実走行（車速センサ `WH_IN`・実負荷・実燃料噴射量）は Ardu-Stim では代替不可のため未検証。実車で確認すること。

#### 8. スタータ自動制御（Issue #11）

Ardu-Stim で回転信号を与えつつ、STR_IN/ENGOFF_IN 相当を操作する:

- [ ] rpm を 0→`start_RPM`(1500) 以上へスイープしながら STR_IN を ON → STR_OUT が LOW になり、
      `start_RPM` 到達から約100ms 後に STR_OUT が HIGH に戻る（`STR_STARTED`）
- [ ] rpm を `start_RPM` 未満に維持したまま STR_IN を ON → STR_OUT が LOW になってから
      2000ms 後に STR_OUT が HIGH に戻る（`STR_FAILED`）
- [ ] `STR_FAILED` 状態で STR_IN を離さず維持 → 再クランキングしない（エッジ検出のみで再始動する設計の確認）
- [ ] `STR_FAILED` 後に STR_IN を一度離してから再度 ON → `STR_CRANKING` へ再遷移し STR_OUT が再度 LOW になる
- [ ] クランキング中に ENGOFF_IN を OFF → 即座に STR_OUT が HIGH に戻り `STR_IDLE` に戻る
- [ ] 上記のいずれの遷移でも、走行距離・燃費・稼働時間（`Launch` 連動の積算）が途切れない
      （キルスイッチOFFで `Launch` はリセットされない仕様の確認）

> EEPROM を完全な未書き込み状態へ戻すコマンドは用意していない。  
> 既定値へ戻したい場合は `MAP DEFAULT` → `MAP SAVE`（出所は `EEPROM` のままになる）。

## 高速GPIO

[`fastestdigitalRW.hpp`](src/fastestdigitalRW.hpp):

- AVR: `sbi/cbi` 直接制御
- UNO R4 (RA4M1): レジスタ `R_PORTx->PODR_b`
- その他: フォールバック `digitalWrite/digitalRead`

クリティカル区間 (割り込み内) の遅延最小化に寄与。

## タイマ

AGTimer: [`AGTimer.init(period_us, callback)`](lib/AGTimer_R4_Library/src/AGTimerR4.h)  
本プロジェクトでは 24µs 周期で [`Routine`](src/main.cpp) 呼び出し。  
周波数変更は `ROUTINE_CYCLE_US` を調整。

## FreeRTOS

- 監視タスク: `statusTask` (Serial1: 100ms(10Hz) / SerialUSB: 500ms(2Hz), 優先度 2, スタック 256 words)  
- MAP コンソール: `mapConsoleTask` (5ms, 優先度 1, スタック 256 words)  
- USB(`Serial`) 出力は両タスクで共有するため、`mapConsoleUsbLock()` / `mapConsoleUsbUnlock()` で排他する。  
  MAP 転送セッション中は `statusTask` の USB テレメトリを停止する（`Serial1` 側は常時出力）。
- `statusTask` のスタック 256 words は `snprintf` 浮動小数点フォーマット分を含む。追加タスクは `xTaskCreate` で拡張可能。スタック追加時は増量推奨。

## ログ / 出力

| ポート | 形式 | 周期 | フィールド |
| --- | --- | --- | --- |
| `Serial1` (HW UART) | CSV | **10Hz (100ms)** | RPM, INJ(ms), IGN_CA, speed, distance, fuel(ml), km/L, worktime |
| `Serial` (USB CDC) | タブ区切り | 2Hz (500ms) | 上記 + Ne_deg |

speed は 0.1km/h 分解能。停止時は最終パルス経過時間で減衰し、約8秒後に 0.0 へ。

## PlantUML 図の参照

- 図ファイル: `document/Arduino_ECU_Workflow.puml`  
- VS Code でのプレビュー: PlantUML 拡張(例: `jebbs.plantuml`)を使用して開く。  
- コマンド例 (Windows PowerShell):

```powershell
java -jar "$env:USERPROFILE\.vscode\extensions\jebbs.plantuml-2.18.1\plantuml.jar" -tsvg document/Arduino_ECU_Workflow.puml
```

- 図に含まれる主な数値注記:
  - ROUTINE_CYCLE_US = 24µs
  - PERIMETER_MM = 1548mm
  - 速度上限 99.9km/h (内部999)
  - カム同期タイムアウト ≈ 50ms
  - RPMゼロ化 ≈ 1.2s無信号
  - 速度強制ゼロ ≈ 8s無信号
  - 点火保持 5ms

## 拡張アイデア (TODO)

- [x] MAP内パラメータ選択・燃料噴射・点火処理高速化  
  (現状では処理遅れに起因すると思われる過大な進角角度を設定している)
- [x] ビルド不要の MAP 書き換え (USB シリアル CSV 転送 + EEPROM 永続化)
- [x] スタータ制御の自動化（Issue #11）  
  (`start_RPM`×0.1秒維持で自動停止、2秒タイムアウトで失敗検出・再試行可。始動専用MAPフラグは廃止しMAP方式に一本化)
- [ ] SD から MAP 読込実装 (`parseCSV`) ※ SD が使えるようになったら `mapParseCsvLine()` を再利用
- [ ] AFR センサ補正ロジック (`updateAFR`)
- [ ] クランク角推定のドリフト補正（非エンコーダ時）
- [ ] 例外検出 (センサ断線・異常 RPM)
- [ ] フラッシュ書き込みによる学習補正保存（MAP と同じ EEPROM 領域を拡張）
- [ ] 単位/係数の物理モデル化（燃料密度, 噴射流量）

## ビルドオプションフラグ

| フラグ | 影響 |
| ------ | --- |
| `uno_r4_minima` | 自動 (PlatformIO env) |
| `rmc_ra4m1_20` | SD 初期化ブロック有効 |

## デバッグ

[Ardu-Stim](https://github.com/todateman/Ardu-Stim.git)の`develop/furoshiki`ブランチにある`furoshiki_2025`のパターンと、[visa-mcp](https://github.com/todateman/visa-mcp)によるオシロスコープの制御を組み合わせて、ECUプログラムのデバッグを行う。

- VISAリソース(RIGOL DHO804): `USB0::6833::1101::DHO8A253701207::0::INSTR`
- CH1: D9
- CH2: D2
- CH3: A0
- CH4: A1
- 回転数は外部から可変変化させるので、GPIOピン出力を確認して現在のMAPに対して乖離がないか確認する  
  （現在のMAPは `MAP?` で吸い出せる。EEPROMに保存済みなら `defaultMap` ではなくそちらが使われている点に注意）
- 一度スタータボタンONの信号を入力しなければ`INJ_OUT`と`IGN_OUT`の信号出力を開始しないので、手動で実行する。
- MAP書き換え機能の実機確認は [実機確認チェックリスト](#実機確認チェックリスト) を参照。

評価結果:

- [document/10HzLogging_debug_report_20260912.md](document/10HzLogging_debug_report_20260912.md)

## ライセンス

- 本体: リポジトリ LICENSE (MIT)
- AGTimer ライブラリ: 同梱 MIT (作者表記参照)

## 安全上の注意

実車/燃焼系制御へ適用する際は下記を検討:

- ウォッチドッグ / フェールセーフ / 過回転保護など追加必須
- 電源ノイズ対策 (車載 12V → 5V/3.3V 安定化)
- I/O レベルと駆動回路(インジェクタ / イグナイタ)の絶縁
