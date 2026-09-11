# EEPROM/USBシリアル MAP書き換え機能 検証レポート（Ardu-Stim代替）

**日付**: 2026-09-12
**対象ブランチ**: `feature/eeprom-map-console`
**目的**: [README.md の実機確認チェックリスト](../README.md#実機確認チェックリスト)（項目1〜7）を、実車の代わりに Ardu-Stim（回転信号エミュレータ）+ RIGOL DHO804（オシロスコープ）のベンチ環境で代替検証する。  

Ardu-Stim・キルスイッチ・スタータスイッチの操作はすべて手動。  
オシロスコープは [visa-mcp](https://github.com/todateman/visa-mcp) 経由でSCPIコマンドを送って自動計測した。

---

## 1. 検証環境

| 項目 | 内容 |
| ---- | ---- |
| ECU | Arduino UNO R4 Minima、`/dev/cu.usbmodem1401` |
| Serial1（メーター・ロガー）受信側 | `/dev/cu.usbmodem558E1157501` |
| 回転信号発生器 | Ardu-Stim（`develop/furoshiki` ブランチ `furoshiki_2025` パターン） |
| Ardu-Stim配線 | NE_A_IN(D2) / NE_B_IN(D8) / NE_Z_IN(D9) / G_IN(D5) すべて接続 |
| オシロスコープ | RIGOL DHO804（`USB0::6833::1101::DHO8A253701207::0::INSTR`、visa-mcp経由） |
| オシロ チャンネル割当 | CH1=D9(NE_Z_IN) / CH2=D2(NE_A_IN) / CH3=A0(INJ_OUT) / CH4=A1(IGN_OUT) |

**重要な配線上の注意**: 今回のベンチでは、キルスイッチが ECU の `ENGOFF_IN`(D7) と
Ardu-Stimの回転信号出力（NE_A/NE_Z/NE_B/G_IN）を同一スイッチで兼ねている。  
物理的な「押していない（レスト）」位置が `ENGOFF_IN=LOW`（運転許可）かつArdu-Stim信号が流れる状態、「押した（ON）」位置が `ENGOFF_IN=HIGH`（強制 `ENG_ON=false`）かつ信号停止、という一般的なキルスイッチの論理と一致する。  
検証序盤はこの対応関係を勘違いし、「回転中×ENG_ON=true」が配線上不可能と誤認したが、実際には両立可能だった。

---

## 2. 総合結果サマリー

| # | 項目 | 判定 |
| - | ---- | ---- |
| 1 | 起動時フォールバック | ✅ OK |
| 2 | CSV転送→RAM反映 | ✅ OK |
| 3 | EEPROM永続化 | ✅ OK |
| 4 | 安全ガード（クランキング/始動） | ✅ OK |
| 5 | 不正入力耐性 | ✅ OK |
| 6 | リアルタイム性の非退行 | ✅ OK（`Ne_deg`カウント抜けの1項目のみ未検証） |
| 7 | 実走前の最終確認 | 🟡 一部OK（実走行そのものは未検証。レブリミット点火バグを発見・修正） |

---

## 3. 項目別詳細

### 3-1. 起動時フォールバック

- `MAP INFO` → `INFO src=DEFAULT rows=15 crc=0x52F4 eeprom=EMPTY rpm=0 eng=OFF`  
  （EEPROM未初期化状態、`defaultMap[]`と一致）
- `MAP?` の15行が [`src/map_store.cpp`](../src/map_store.cpp) の `defaultMap[]` と完全一致
- `HELP` で全コマンド一覧が出力されることを確認

### 3-2. CSV転送→RAM反映

- `microSD/RPM.CSV`・`microSD/RPM_2026SUZUKA.CSV`・自作24行CSVいずれも `RAMへ反映しました（読み戻し検証OK）`、終了コード0
- USBテレメトリのミュート/再開を専用スクリプトで実測:
  - `MAP BEGIN`〜`ABORT`の3秒間: テレメトリ0件
  - 前後3秒: 500ms周期で6件ずつ受信（正常）
- Serial1側 (`/dev/cu.usbmodem558E1157501`) を転送中も並行ロギングし、500ms周期のCSV出力が一切途切れないことを確認（ギャップなし）
- 24行CSV（`MAP_MAX_ENTRIES`上限）でもFIFO溢れ・ACKタイムアウトなく完了、読み戻し検証もOK

### 3-3. EEPROM永続化

- `RPM_2026SUZUKA.CSV --save` → `EEPROMへ保存しました`
- USB抜き差し後 `MAP INFO` → `src=EEPROM rows=15 crc=0x52F4 eeprom=VALID`
- `RPM_2026SUZUKA.CSV` の内容が偶然 `defaultMap[]` と同一だったため、内容差のある `RPM_2024SUZUKA.csv` でも再保存・再起動して確認  
  （`crc=0x52F4` → `crc=0x8137` に変化、電源断後も保持）
- `MAP DEFAULT` → `MAP?`（既定値）→ `MAP LOAD` → `MAP?`（EEPROM内容）の出所切り替えも確認

### 3-4. 安全ガード（クランキング／エンジン始動）

- Ardu-Stimで1500〜6500RPM相当の回転を与えた状態で `MAP SAVE` → `ERR ENGINE_RUNNING`  
  （`tachoRpm != 0` によるガード。回転を与えただけで `ENG_ON` は無関係に拒否される）
- キルスイッチをレスト位置（運転許可）のままスタータスイッチを一瞬ON→OFFし、 `ENG_ON=true` を達成（`MAP INFO` の `eng=ON` で確認）
- 回転中・`ENG_ON=true` の状態で:
  - `MAP SAVE` → `ERR ENGINE_RUNNING`（EEPROM内容は無傷）
  - `MAP SET 3200 45 26` → `OK SET`、`MAP?` に即反映
  - `MAP BEGIN`〜CSV〜`MAP END` の一括転送も正常に反映
- オシロ実測（詳細は3-6節）で、`MAP SET` 前後にINJ_OUTの周波数・パルス幅が分断・二重化なく安定して新設定値に追従することを確認

### 3-5. 不正入力での耐性

| 入力 | 結果 |
| ---- | ---- |
| RPM非昇順（`3000,40,20` → `1000,40,20`） | `ERR RPM_NOT_ASCENDING` |
| 26行CSV（上限24行超過） | 25行目で `ERR TOO_MANY_ROWS` |
| `ign_ca=95` の自作CSV | `ERR IGN_CA_OUT_OF_RANGE` |
| 実データ `microSD/RPM_2025MOTEGI.csv`（`ign_ca`最大185） | `ERR IGN_CA_OUT_OF_RANGE` |

- 上記いずれの失敗後も `MAP?` は転送前の内容から変化なし
- `MAP BEGIN` 後、`MAP END`/`ABORT` を送らずに接続を切る（USB切断模擬）テストを実施:  
  再接続後も既存MAPは無傷、新しい `MAP BEGIN` セッションも正常に開始できることを確認  
  （`MAP BEGIN` が無条件に `mapStagingClear()` するため、`sessionActive` が残っていても実害がない実装になっている）

**副次的な発見**: `microSD/RPM_2025SUZUKA.CSV` / `RPM_2024MOTEGI.CSV` / `RPM_2025MOTEGI.csv` は `ign_ca` が現行の上限（`MAP_IGN_CA_MAX` = 90、[map_store.h](../src/map_store.h)）を超える値（最大185）を含んでおり、現行のMAPコンソール経由では読み込めない。  
`document/debug_report_20260330.md` が指摘した「旧ファームウェアのISR遅延補正込みの値」の名残と思われる。  
過去のレースデータを再利用する場合は値の見直しが必要。

### 3-6. リアルタイム性の非退行（オシロ実測）

1560RPM相当・`ENG_ON=true` の状態で計測（visa-mcp経由、DHO804）:

| チャンネル | 実測値 | 期待値 | 判定 |
| ---- | ---- | ---- | ---- |
| CH1周波数（クランク） | 26.03Hz | 1561rpm÷60=26.02Hz | ✅ 一致 |
| CH3周波数（噴射サイクル） | 13.01〜13.02Hz | CH1の1/2（720°=2回転に1回噴射） | ✅ 一致 |
| CH4周波数（点火サイクル） | 13.01Hz | CH3と同期 | ✅ 一致 |
| CH4 Lowパルス幅（ドゥエル） | 4.88ms | `Dwell_Time_US`=5000us | ✅ ほぼ一致 |
| CH3 Lowパルス幅（噴射時間） | 4.02ms | `MAP SET 1600 40 15` 直後の値=4.0ms | ✅ 完全一致 |

`MAP SET 1600 40 15` 実行前後でCH3を継続計測:

| | FREQ | Highパルス幅(PWID) | Lowパルス幅(NWID) |
| - | - | - | - |
| 実行前 | 13.014Hz | 72.84ms | 4.88ms（旧設定） |
| 実行後 | 13.017Hz | 72.84ms（不変） | 4.02ms（新設定値に追従） |

周波数・Highパルス幅がほぼ不変のまま、Lowパルス幅だけが新しい設定値に正確に追従しており、
バンク切替時の噴射パルスの分断・二重化がないことを実測で確認した。  

Serial1側の2Hz（500ms周期、`STATUS_TASK_DELAY_MS`、[main.cpp](../src/main.cpp)）ロギングは別途ログして途切れがないことを確認済み（3-2節）。  

**表記の食い違い**: README/チェックリストの「USBシリアルの10Hzロギング」という記述に対応する周期定数はコード中に存在しない。  
実装は `statusTask()` の500ms周期＝2Hzであり、実際にはこの周期を前提に継続性を確認した。

**未検証**: `MAP SAVE`（停止時）中の `Ne_deg` カウント抜け。  
`MAP SAVE` は`tachoRpm==0`（停止状態）でしか受理されない仕様のため、「回転中に保存が走る」状況自体をArdu-Stim上でも作れず、未検証のまま残っている。

### 3-7. 実走前の最終確認

- `RPM_2026SUZUKA.CSV`（`defaultMap[]`と同一内容）を `--save`  
  → 再起動後`MAP INFO` の `crc=0x52F4` を記録
- `python tools/send_map.py --dump` の出力が保存したCSV内容と一致することを確認
- レブリミット確認（詳細は次節）: 6711RPM（MAP最終行6000RPM超過）でUSBテレメトリの `INJ_timems=0.0, IGN_CA=0` を確認  
  → **ただしこの時点でオシロ実測により点火が実際には止まっていないバグを発見（4節）**
- 実走行そのもの（車速センサ `WH_IN`・実負荷・実燃料噴射量）はArdu-Stimでは代替不可のため未検証

---

## 4. 発見した問題と対応

### 4-1.【重要・修正済み】レブリミット超過時、燃料噴射は止まるが点火が止まらない

**現象**: MAP最終行（6000RPM）を超える6711RPMで、USBテレメトリ上は `calculatedINJ_time=0` / `calculatedIGN_CA=0` になっていたが、オシロで実測すると:

| チャンネル | 実測パルス幅（Low区間） | 判定 |
| ---- | ---- | ---- |
| CH3 (INJ_OUT) | 20µs | ほぼゼロ幅。制御ループ1周分(約24µs)の残骸で、インジェクタは物理的に反応できず実質停止 |
| CH4 (IGN_OUT) | 5.04ms | `Dwell_Time_US`(5000us)とほぼ完全一致。<BR>**フルの点火パルスが毎サイクル出続けている** |

**原因**: 点火ON判定（修正前の [main.cpp](../src/main.cpp) 点火制御部）は

```cpp
if (ENG_ON && IGN_Status == 1 && !IGN_His) {
  if (Ne_deg >= (360 - calculatedIGN_CA - Dwell_Time_CA)) { ... }
}
```

で行われており、`calculatedIGN_CA=0` でも閾値が「360°に極めて近い位置」にずれるだけで、判定自体は毎サイクル必ず成立してしまう。  
`Dwell_Time_CA` は実回転数のみから独立に算出されるため、`calculatedIGN_CA=0` は点火を止める効果を持たなかった。  

**修正内容**: `mapOutOfRange` フラグを追加し、MAPの最終行を超えて `calculatedINJ_time=0; calculatedIGN_CA=0;` にフォールバックした場合にのみ立てるようにした。（[updateEngineMap()](../src/main.cpp)）  
既存の噴射・点火ON判定条件に `&& !mapOutOfRange` を追加し、MAP範囲外では新規トリガを一切発生させないようにした。  
（進行中のON_HOLD状態は途中終了させず正常にタイマーで完了させる、既存設計を踏襲）

```cpp
// 噴射
if (ENG_ON && INJ_Status == 1 && !INJ_His && !mapOutOfRange) { ... }
// 点火
if (ENG_ON && IGN_Status == 1 && !IGN_His && !mapOutOfRange) { ... }
```

**再検証結果**: 修正版を書き込み直し、Ardu-Stim 6587RPM・`ENG_ON=true` の状態で再計測。

- CH3 (INJ_OUT): `VMAX=4.83V / VMIN=4.55V`（振幅0.27V程度のノイズのみ、Lowパルスなし）
- CH4 (IGN_OUT): オシロ画面を目視確認し、高電位に張り付いたまま（パルスなし）であることを確認

修正前後で燃料噴射側の挙動は変わらず（もともと実質停止）、点火側が新たに正しく停止するようになった。

### 4-2. 過去のレースCSVがign_ca上限を超過（3-5節参照、詳細は再掲不要）

### 4-3. README「10Hzロギング」表記とコード実装(2Hz)の食い違い（3-6節参照）

### 4-4. Ardu-Stim停止中のノイズによるtachoRpm誤検出

検証中、Ardu-Stimが信号を出力していない間、`NE_A_IN`/`NE_Z_IN` がフローティング状態でノイズを拾い、`tachoRpm` が瞬間的に非ゼロ値（例: 15rpm）になる現象を複数回確認した。  

`MAP SAVE` はこのノイズにより一時的に `ERR ENGINE_RUNNING` を返すことがあったが、1〜2秒待って `tachoRpm` が0に落ち着けば正常に保存できた。  
ファームウェア側の問題ではなく、ベンチ配線（未駆動時の入力オープン）に起因すると考えられる。

---

## 5. 未検証のまま残る項目

- `MAP SAVE`（停止時）中の `Ne_deg` カウント抜け  
  （回転中の保存という状況自体を再現できないため）
- 実走行そのもの（車速センサ `WH_IN`・実負荷・実燃料噴射量）
- `visa-mcp` 再起動前に取得しようとした一部計測（再起動後は正常動作を確認）

---

## 6. 関連ファイル

- [README.md 実機確認チェックリスト](../README.md#実機確認チェックリスト)
- [document/debug_report_20260330.md](debug_report_20260330.md)（今回のオシロ配置・信号名対応の元ネタ）
- [src/main.cpp](../src/main.cpp)（`mapOutOfRange` 修正箇所）
