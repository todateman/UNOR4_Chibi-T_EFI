#include <Arduino.h>
#include <SPI.h>
#include "SD.h"
#include "fastestdigitalRW.hpp"  :contentReference[oaicite:0]{index=0}
#include "AGTimerR4.h"
#include <Arduino_FreeRTOS.h>
#include "map_store.h"
#include "map_console.h"

// IRAM_ATTR が未定義の場合は空定義を追加（ESP32等でなければ不要）
#ifndef IRAM_ATTR
#define IRAM_ATTR
#endif

//-----------------------------------------------------------------------------
// 前方宣言（setup()で使う割り込み関数の宣言）
//-----------------------------------------------------------------------------
void IRAM_ATTR WH_PULSE_ISR();
void IRAM_ATTR ReadNe_ISR();
void IRAM_ATTR G_PULSE_ISR();

//-----------------------------------------------------------------------------
// 定数・設定
//-----------------------------------------------------------------------------

#define ROUTINE_CYCLE_US     24
#define STATUS_TASK_DELAY_MS 100      // タスク基本周期: 100ms (10Hz)
#define SERIAL_USB_DIVISOR   5        // USB Serial 分周比: 5回に1回 = 500ms (2Hz)
#define TELEM_LINE_MAX       96       // 機械可読テレメトリ1行のバッファ長

#define PERIMETER_MM         1548UL   // [mm]
#define TACHO_RPM_MAX        6000     // レブリミット（RPM）※これを超えると燃料噴射・点火停止
#define Dwell_Time_US        5000     // ドゥエル時間（IGコイルへの充電時間）[us]

const uint8_t NE_A_IN      = 2;   // クランク角エンコーダAパルス(360°で360パルス)
const uint8_t NE_B_IN      = 8;   // クランク角エンコーダBパルス(360°で360パルス)
const uint8_t NE_Z_IN      = 9;   // クランク角エンコーダBパルス(360°で1パルス)
const uint8_t WH_IN        = 3;   // 車軸パルス（タイヤ1周で1パルス）
const uint8_t G_IN         = 5;   // カム角センサ（クランク2周で1パルス）
const uint8_t STR_IN       = 6;   // スタートスイッチ（エンジン始動）
const uint8_t ENGOFF_IN    = 7;   // エンジンキルスイッチ（エンジン停止）
const uint8_t MA735_CS     = 10;  // MA735 SPIセンサのCSピン（SPI通信に使用）
const uint8_t INJ_OUT      = A0;  // 燃料噴射出力（ON:LOW, OFF:HIGH）
const uint8_t IGN_OUT      = A1;  // 点火出力（ON:LOW, OFF:HIGH）
const uint8_t STR_OUT      = A2;  // スタータ出力（ON:LOW, OFF:HIGH）
const uint8_t DISRESET_OUT = A3;  // リセットランプ出力（ON:LOW, OFF:HIGH）

bool EncoderEnabled   = true;     // クランク角エンコーダ使用フラグ
bool MA735SPIEnabled  = false;    // MA735 SPIセンサ使用フラグ
bool AFREnabled       = false;    // A/Fセンサ使用フラグ
bool Increase_Fuel    = false;    // A/F補正フラグ（未実装）
bool SDMapEnabled     = false;    // SDカードMAP使用フラグ
bool SerialUSBEnabled = true;     // USBシリアル出力フラグ
bool Serial1Enabled   = true;     // Serial1(toメーター・ロガー)出力フラグ

SPISettings ma735Settings(12000000, MSBFIRST, SPI_MODE0);

//-----------------------------------------------------------------------------
// グローバル変数（volatile指定）
//-----------------------------------------------------------------------------
volatile unsigned long tachoBefore  = 0;  // NE_Z_INの立ち上がりで更新
volatile unsigned long tachoAfter   = 0;  // NE_Z_INの立ち下がりで更新
volatile unsigned long tachoWidth   = 0;  // NE_Z_INのパルス幅（us）
volatile uint16_t      tachoRpm     = 0;  // NE_Z_INの回転数（RPM）
volatile int16_t       Ne_deg       = 0;  // クランク角度（CA）
volatile int16_t       Ne_rev       = 0;  // クランク回転数（回転数）

volatile unsigned long speedBefore  = 0;  // WH_INの立ち上がりで更新
volatile unsigned long speedAfter   = 0;  // WH_INの立ち下がりで更新
volatile unsigned long speedWidth   = 0;  // WH_INのパルス幅
volatile unsigned long distancemm   = 0;  // WH_INの走行距離（mm）
volatile uint16_t      distance     = 0;  // WH_INの走行距離（km）
volatile unsigned long speed        = 0;  // WH_INの速度（0.1 km/h 単位）0.0～99.9km/h→内部0～999
// 注意: パルスが止まると最後の1回転周期で算出した速度が保持されるため下限値(例:1.4km/h)から0へ落ちない。
// `statusTask` 内で最終パルスからの経過時間を使った減衰再計算を行い停車時に 0.0 へ近づける。

bool ENG_ON                          = false; // エンジンONフラグ（キルスイッチに連動）
volatile uint8_t  calculatedINJ_time = 0; // 燃料噴射時間（x0.1ms）
volatile int16_t  calculatedIGN_CA   = 0; // 点火進角角度（CA）
volatile int16_t  calculatedINJ_END_CA = 0; // 燃料噴射終了タイミング角度（CA）
// MAP最終行を超える回転数（レブリミット相当）で true。
// calculatedIGN_CA==0 は「0の値が入ったMAP行」と区別できないため、
// 点火・噴射の新規トリガ可否はこのフラグで判定する（進行中のON_HOLDは止めない）。
volatile bool     mapOutOfRange      = false;
// 現在採用中のMAP行インデックス（255 = MAP未使用。始動時・MAP範囲外）。
// GUIのライブトレースが「実機が実際に採用した行」を推測せずに表示できるようにする。
// ISRから書くが、uint8_tの単一ストアはtearingしないためロック不要。
volatile uint8_t  activeMapRow       = 255;
volatile int16_t  Dwell_Time_CA      = 0; // ドゥエル時間（IGコイルへの充電時間）をクランク角度（CA）へ変換
volatile int16_t  INJ_STR_CA         = 0; // 燃料噴射開始タイミング角度（CA）※INJ_END_CAと噴射時間から逆算
volatile uint8_t  INJ_Status         = 1; // 燃料噴射状態（0:OFF, 1:ON, 2:ON_HOLD）
volatile uint8_t  IGN_Status         = 1; // 点火状態（0:OFF, 1:ON, 2:ON_HOLD）

volatile unsigned long timeNow_INJ_ON  = 0; // 燃料噴射ON時間（us）
volatile unsigned long timeNow_INJ_OFF = 0; // 燃料噴射OFF時間（us）
volatile unsigned long timeNow_IGN_ON  = 0; // 点火ON時間（us）
volatile unsigned long timeNow_IGN_OFF = 0; // 点火OFF時間（us）

volatile bool INJ_His = false;            // 燃料噴射履歴（ON/OFF）
volatile bool IGN_His = false;            // 点火履歴（ON/OFF）
volatile bool inj360Reset = false;        // 360CA安全リセットフラグ

volatile bool G_Pulse      = false;       // G_INのパルス状態（立ち上がり）
volatile bool G_Pulse_Flag = false;       // G_INのパルスフラグ（立ち上がり）
volatile bool CycleReset   = false;       // サイクルリセットフラグ（立ち上がり）

bool Launch = false;                      // スタートフラグ（エンジン始動）
bool startState = HIGH;                   // スタートスイッチ状態（OFF=HIGH, ON=LOW）
bool lastStartState = HIGH;               // スタートスイッチの前回状態   
bool STR_IN_state = false;                // エンジン始動状態

// スタータ状態マシン（Issue #11: スタータ制御の自動化）
// 始動用MAP切替フラグ（useStartMap相当）は不要。MAPは低rpm行を自動的に採用するため。
enum StarterState : uint8_t {
  STR_IDLE = 0, STR_CRANKING = 1, STR_STARTED = 2, STR_FAILED = 3
};
StarterState  starterState    = STR_IDLE; // 現在のスタータ状態（IDLE:待機, CRANKING:クランキング中, STARTED:始動成功, FAILED:始動失敗）
uint16_t      start_RPM       = 1500;     // 始動成功判定の回転数（RPM）
unsigned long strCrankStartMs = 0;        // クランキング開始時刻（ms）
unsigned long starterFirstMs  = 0;        // start_RPM到達判定の開始時刻（ms）
bool          starterActive   = false;    // start_RPM維持判定中フラグ
unsigned long STARTER_TIMEOUT_MS = 1000;  // スタータタイムアウト（ms）

volatile float gasml       = 0.0;         // 燃料消費量（ml）
volatile float INJ_timems  = 0.0;         // 燃料噴射時間（ms）
volatile float dispergas   = 0.0;         // 燃費（km/L）
unsigned long starttime    = 0;           // エンジン始動時間（ms）
volatile uint32_t worktime = 0;           // エンジン稼働時間（0.1秒単位）

volatile float usecperdig = 1.0;          // NE_A_INの1パルスあたりの時間（us）

//-----------------------------------------------------------------------------
// MAPテーブル
//
// 実体は map_store.cpp のダブルバンク（mapGetActive()で参照）。
// 内蔵デフォルトMAPは defaultMap[]、EEPROMに有効なMAPがあればそちらが優先される。
// 書き換えは USBシリアル経由（map_console.cpp / tools/send_map.py）で行い、
// ビルドし直さずに調整できる。
//-----------------------------------------------------------------------------

//-----------------------------------------------------------------------------
// 関数宣言（詳細実装は下部）
//-----------------------------------------------------------------------------
void updateEngineMap();
void cycleReset();
void Routine();
int16_t readMA735SPI();
void updateAFR();  // AFR関連は必要に応じて実装
void parseCSV();   // SDカード用

//-----------------------------------------------------------------------------
// スタブ実装（リンクエラー解消用）
//-----------------------------------------------------------------------------
// updateAFR()：AFRセンサ処理（未使用の場合は空実装）
void updateAFR() {
  // AFRセンサ未実装の場合は何もしない
}

// parseCSV()：SDカードからMAP読み込み（未使用の場合は空実装）
void parseCSV() {
  // SDカードMAP読み込み未実装の場合は何もしない
}

//-----------------------------------------------------------------------------
// 割り込みルーチン
//-----------------------------------------------------------------------------

// WH_PULSE_ISR：車軸パルス割込み（速度・走行距離の更新）
void IRAM_ATTR WH_PULSE_ISR() {
  unsigned long now = micros();
  speedWidth = now - speedBefore;
  // 速度計算: km/h = (3600 * 周長(mm)) / パルス間隔(µs) / 1000(mm→m) / 1000(m→km)
  // 0.1km/h分解能にするため10倍 → (36000 * PERIMETER_MM) / dt
  unsigned long dt = (speedWidth ? speedWidth : 1);
  unsigned long calc = (36000UL * PERIMETER_MM) / dt; // 0.1km/h単位
  if (calc > 999) calc = 999; // 上限 99.9km/h
  speed = calc;
  speedBefore = now;
  distancemm += PERIMETER_MM;
  distance = distancemm / 1000;
}

// ReadNe_ISR：クランク角更新割込み（NE_B_INによる処理）
void IRAM_ATTR ReadNe_ISR() {
  if (!fastestdigitalRead(NE_B_IN))
    Ne_deg += 1;
  else
    Ne_deg -= 1;

  if (fastestdigitalRead(NE_Z_IN)) {
    unsigned long now = micros();
    tachoWidth = now - tachoBefore;
    tachoBefore = now;
    // 整数演算で RPM 計算（浮動小数点除算を避け ISR 実行時間を短縮 信号出力時の処理遅れ～1°）
    uint16_t _tachoRpm = (tachoWidth > 0) ? (uint16_t)(60000000UL / tachoWidth) : 0;
    if (_tachoRpm < 10000) {  // 10000 RPMを超える異常値は無視
      tachoRpm = _tachoRpm;
      // ドゥエル時間を回転数に応じたクランク角度に変換
      Dwell_Time_CA = (int16_t)(Dwell_Time_US * 360.0 / tachoWidth);
    }
    if (Ne_deg > 360 && G_Pulse_Flag) {
      Ne_deg = 0;
      G_Pulse_Flag = false;
      CycleReset = true;
    }
  }
}

// G_PULSE_ISR：カム角センサ割込み（G_IN）
void IRAM_ATTR G_PULSE_ISR() {
  if (!G_Pulse) {
    G_Pulse = true;
    G_Pulse_Flag = true;
  }
  // リリース状態は Routine 内で処理
}

//-----------------------------------------------------------------------------
// MA735 SPIによる角度取得
//-----------------------------------------------------------------------------
int16_t readMA735SPI() {
  static uint16_t last_rd = 0;
  static unsigned long last_rd_time = 0;
  SPI.beginTransaction(ma735Settings);
  fastestdigitalWrite(MA735_CS, LOW);
  uint16_t rd = SPI.transfer16(0);
  fastestdigitalWrite(MA735_CS, HIGH);
  SPI.endTransaction();

  long diff = (long)rd - (long)last_rd;
  if (diff < -32767) {
    unsigned long now = micros();
    Ne_rev++;
    if (diff < 262)
      tachoRpm = (uint16_t)(60000000.0 / (now - last_rd_time));
    last_rd_time = now;
    if (G_Pulse_Flag) {
      Ne_rev = 0;
      G_Pulse_Flag = false;
      CycleReset = true;
    }
  } else if (diff > 32767) {
    Ne_rev--;
  }
  last_rd = rd;
  int16_t angle = (rd / 65535) * 360 + 360 * Ne_rev;
  return angle;
}

//-----------------------------------------------------------------------------
// エンジンMAP更新
//-----------------------------------------------------------------------------
void updateEngineMap() {
  // アクティブバンクを1回だけ取得する。バンク切替は非アクティブ側を完成させてから
  // 1バイトのストアで行われるため、参照中にテーブルが壊れることはない。
  const MapTable &map = mapGetActive();
  for (uint8_t i = 0; i < map.count; i++) {
    if (tachoRpm < map.e[i].rpm) {
      calculatedINJ_time = map.e[i].inj_time;
      calculatedIGN_CA   = map.e[i].ign_ca;
      calculatedINJ_END_CA = map.e[i].inj_end_ca;
      mapOutOfRange = false;
      activeMapRow  = i;
      return;
    }
  }
  // MAP上限を超える回転数では燃料噴射・点火を止める
  calculatedINJ_time = 0;
  calculatedIGN_CA   = 0;
  mapOutOfRange = true;
  activeMapRow  = 255;
  return;
}

//-----------------------------------------------------------------------------
// サイクルリセット
//-----------------------------------------------------------------------------
void cycleReset() {
  updateEngineMap();
  // 燃料噴射開始タイミング角度を逆算（終了角度 - 噴射時間相当のCA）
  if (tachoWidth > 0) {
    INJ_STR_CA = calculatedINJ_END_CA - (int16_t)((uint32_t)calculatedINJ_time * 100UL * 360UL / tachoWidth);
    if (INJ_STR_CA < 0)
      INJ_STR_CA += 720;  // 0CA跨ぎ: サイクル後半の開始角度（0〜720CA）に変換
  } else {
    INJ_STR_CA = calculatedINJ_END_CA;
  }
  if (tachoRpm > TACHO_RPM_MAX) {
    timeNow_INJ_ON  = 0;
    timeNow_INJ_OFF = 0;
  }
  // 噴射中(INJ_Status==2)の場合はリセットしない（0CA跨ぎ噴射の継続を保護）
  if (INJ_Status != 2) {
    INJ_Status = 1;
    INJ_His = false;
  }
  IGN_Status = 1;
  IGN_His = false;
  G_Pulse = false;
  G_Pulse_Flag = false;
  inj360Reset = false;  // 新サイクル開始時に360CAリセットフラグをクリア
}

//-----------------------------------------------------------------------------
// Routine(): AGTimerより周期実行されるリアルタイム処理
//-----------------------------------------------------------------------------
void Routine() {
  // スタートスイッチの状態を更新(OFF=HIGH, ON=LOW)
  startState = fastestdigitalRead(STR_IN);

  // A/Fセンサの更新
  if (AFREnabled) {
    updateAFR();
  }
  
  // MA735 SPIセンサの更新
  if (MA735SPIEnabled) {
    Ne_deg = readMA735SPI();
  }
  
  // クランク角センサを使用しない場合、回転数からクランク角を概算
  if (!EncoderEnabled && !MA735SPIEnabled) {
    if (usecperdig > 1e-3)
      Ne_deg += (int16_t)(ROUTINE_CYCLE_US / usecperdig);
  }
  
  // カム角センサのパルス処理
  if (fastestdigitalRead(G_IN) == LOW) {
    if (!G_Pulse) {
      G_Pulse = true;
      G_Pulse_Flag = true;
    }
  } else {
    if (G_Pulse)
      G_Pulse = false;
  }
  // サイクル同期が取れない場合のタイムアウト／再リセット機構(始動不良対策)
  static unsigned long lastZMicros = 0;
  if (G_Pulse_Flag) {
    lastZMicros = micros();
  }
  // カムパルスが来ずに一定時間経過したら、自動的に cycleReset() を繰り返し呼び出す
  if (micros() - lastZMicros > 50000UL) {
    cycleReset();
    lastZMicros = micros();
  }
  
  // エンジン燃焼サイクルのリセット
  if (CycleReset) {
    cycleReset();
    CycleReset = false;
  }

  // スタートスイッチ立上りエッジ検出（OFF→ON）
  if (lastStartState == HIGH && startState == LOW) {
    if (fastestdigitalRead(STR_IN) == LOW) {          // スタートスイッチON
      if (fastestdigitalRead(ENGOFF_IN) == LOW) {     // キルスイッチON（運転許可）
        if (starterState == STR_IDLE || starterState == STR_FAILED) {
          starterState    = STR_CRANKING;
          strCrankStartMs = millis();
          starterActive   = false;
          starterFirstMs  = 0;
          ENG_ON          = true;
          Launch          = true;
          if (starttime == 0) starttime = millis();
        }
      }
      cycleReset();   // キルスイッチ状態に関わらず従来どおり実行
    }
  }
  lastStartState = startState;

  // 360CA安全リセット: 意図しない燃料噴射の継続を防止
  if (Ne_deg >= 360 && !inj360Reset) {
    if (INJ_Status == 2) {  // 噴射継続中なら強制OFF
      timeNow_INJ_OFF = micros();
      fastestdigitalWrite(INJ_OUT, HIGH);
      INJ_Status = 1;
      gasml += (((timeNow_INJ_OFF - timeNow_INJ_ON) * 0.0000007) + 0.0015) / 1.5073;
    }
    inj360Reset = true;
  }

  // 燃料噴射制御
  if (ENG_ON && INJ_Status == 1 && !INJ_His && !mapOutOfRange) {
    // 燃料噴射タイミングに達したらON
    if (Ne_deg >= INJ_STR_CA) {
      timeNow_INJ_ON = micros();
      INJ_His = true;
      fastestdigitalWrite(INJ_OUT, LOW);
      INJ_Status = 2;     // ON_HOLD状態へ
    }
  }
  
  // 燃料噴射ON_HOLD状態の処理
  if (INJ_Status == 2) {
    unsigned long injDuration = calculatedINJ_time * 100UL;
    if (Increase_Fuel) {
      // AFRによる補正（実装必要なら）
    }
    // 燃料噴射時間が経過したらOFF
    if (micros() - timeNow_INJ_ON >= injDuration) {
      timeNow_INJ_OFF = micros();
      fastestdigitalWrite(INJ_OUT, HIGH);
      INJ_Status = 1;   // 次の噴射に備えてON状態へ
      gasml += (((timeNow_INJ_OFF - timeNow_INJ_ON) * 0.0000007) + 0.0015) / 1.5073;
    }
  }
  
  // 点火制御
  if (ENG_ON && IGN_Status == 1 && !IGN_His && !mapOutOfRange) {
    // 点火タイミングに達したらON
    if (Ne_deg >= (360 - calculatedIGN_CA - Dwell_Time_CA)) {
      timeNow_IGN_ON = micros();
      IGN_His = true;
      fastestdigitalWrite(IGN_OUT, LOW);
      IGN_Status = 2;
    }
  }

  // 点火ON_HOLD状態の処理
  if (IGN_Status == 2) {
    // 進角角度に達したら or ドゥエル時間が経過したらOFF
    if (Ne_deg >= 360 - calculatedIGN_CA || micros() - timeNow_IGN_ON >= Dwell_Time_US) {
      timeNow_IGN_OFF = micros();
      fastestdigitalWrite(IGN_OUT, HIGH);
      IGN_Status = 1;
    }
  }
  
  // キルスイッチ確認 + スタータ状態マシン
  if (fastestdigitalRead(ENGOFF_IN) == LOW) {       // キルスイッチON（運転許可）
    switch (starterState) {

      case STR_IDLE:                                  // 待機状態
        fastestdigitalWrite(STR_OUT, HIGH);             // スタータOFF
        STR_IN_state = false;                           // スタータ入力状態OFF
        break;

      case STR_CRANKING: {                            // クランキング中
        fastestdigitalWrite(STR_OUT, LOW);              // スタータON
        STR_IN_state = true;                            // スタータ入力状態ON
        ENG_ON = true;                                  // エンジンONフラグON
        unsigned long nowMs = millis();                 // 現在時刻(ms)

        // 始動成功判定 0.1秒維持チェック（タイムアウトより優先）
        if (tachoRpm >= start_RPM) {
          if (!starterActive) {                           // 始動成功判定開始
            starterActive  = true;                          // 始動成功判定中フラグON
            starterFirstMs = nowMs;                         // 始動成功判定開始時刻(ms)を記録
          } else if (nowMs - starterFirstMs >= 100UL) {
            // 始動成功
            starterState = STR_STARTED;                     // 始動成功状態へ
            fastestdigitalWrite(STR_OUT, HIGH);             // スタータOFF
            STR_IN_state = false;                           // スタータ入力状態OFF
            break;  // タイムアウト確認をスキップ
          }
        // 始動成功判定が途切れた場合はフラグをリセット
        } else {
          starterActive  = false;                         // 始動成功判定中フラグOFF
          starterFirstMs = 0;                             // 始動成功判定開始時刻リセット
        }

        // スタータタイムアウト時間を経過した場合は、始動失敗とみなす
        if (nowMs - strCrankStartMs >= STARTER_TIMEOUT_MS) {
          starterState = STR_FAILED;                      // 始動失敗状態へ
          fastestdigitalWrite(STR_OUT, HIGH);             // スタータOFF
          STR_IN_state = false;                           // スタータ入力状態OFF
          ENG_ON       = false;                           // エンジンONフラグOFF
        }
        break;
      }

      case STR_STARTED:                               // 始動成功状態
        fastestdigitalWrite(STR_OUT, HIGH);             // スタータOFF
        STR_IN_state = false;                           // スタータ入力状態OFF
        ENG_ON = true;                                  // エンジンONフラグON
        break;

      case STR_FAILED:                                // 始動失敗状態（再押し待ち）
        fastestdigitalWrite(STR_OUT, HIGH);             // スタータOFF
        STR_IN_state = false;                           // スタータ入力状態OFF
        ENG_ON = false;                                 // エンジンONフラグOFF
        break;
    }
  } else {                                            // キルスイッチOFF（全停止）
    ENG_ON       = false;                               // エンジンONフラグOFF
    starterState = STR_IDLE;                            // スタータ状態IDLE
    fastestdigitalWrite(STR_OUT, HIGH);                 // スタータOFF（安全のため強制的にOFFへ）
    STR_IN_state = false;                               // スタータ入力状態OFF
    // Launch はここでリセットしない（走行距離/燃費/稼働時間は競技中を通して積算し続ける仕様のため）
  }
}

//-----------------------------------------------------------------------------
// statusTask(): Serial1を10Hz、SerialUSBを2Hzで出力
//-----------------------------------------------------------------------------
void statusTask(void *pvParameters) {
  (void)pvParameters;
  TickType_t xLastWakeTime = xTaskGetTickCount();
  uint8_t div_cnt = 0;
  uint8_t tel_cnt = 0;
  uint16_t telSeq = 0;
  // configCHECK_FOR_STACK_OVERFLOW=0 でスタック破壊が静かに起きるため、
  // 送信用バッファはスタックではなくstaticに置く。
  static char s1buf[80];
  static char telBuf[TELEM_LINE_MAX];

  for (;;) {
    // ── 高速パス（10Hz）──────────────────────────────────────────────────
    INJ_timems = calculatedINJ_time * 0.1f;
    worktime = Launch ? (millis() - starttime) / 100 : 0;

    if (Serial1Enabled) {
      // newlib-nano の snprintf は %.1f 非対応のため整数演算で小数1桁を表現
      // キルスイッチ等でエンジン停止中(ENG_ON=false)は、MAP由来の3値を0で出力する。
      // calculated*はMAP参照値のままで実際の噴射・点火状態を表さないため、
      // ロガー側で「停止中」と「MAP行の値が0」を区別できるようにする。
      const bool engRun = ENG_ON;
      unsigned inj10 = engRun ? (unsigned)(INJ_timems * 10.0f + 0.5f) : 0;
      unsigned gas10 = (unsigned)(gasml * 10.0f + 0.5f);
      unsigned dis10 = (unsigned)(dispergas * 10.0f + 0.5f);
      unsigned wt10  = (unsigned)worktime;
      int len = snprintf(s1buf, sizeof(s1buf),
        "%u,%u.%u,%d,%d,%lu.%lu,%u,%u.%u,%u.%u,%u.%u",
        (unsigned)tachoRpm,           // RPM
        inj10 / 10, inj10 % 10,       // INJ_timems
        engRun ? (int)calculatedIGN_CA : 0,       // calculatedIGN_CA
        engRun ? (int)calculatedINJ_END_CA : 0,   // 噴射終了角 [CA]（MAP外では前回値が残る）
        speed / 10, speed % 10,       // speed
        (unsigned)distance,           // distance
        gas10 / 10, gas10 % 10,       // gasml
        dis10 / 10, dis10 % 10,       // dispergas
        wt10 / 10, wt10 % 10);        // worktime（秒）
      // 行末に "*XX\n"（XX = 先頭から'*'直前までのXOR）を付ける。ロガーはRXバッファ溢れ等で
      // 欠けた行をこれで検出して捨てる（欠けた行が有効な値として読まれるのを防ぐ）。
      if (len > 0 && len < (int)sizeof(s1buf) - 5) {
        uint8_t cs = 0;
        for (int i = 0; i < len; i++) cs ^= (uint8_t)s1buf[i];
        len += snprintf(s1buf + len, sizeof(s1buf) - (size_t)len, "*%02X\n", (unsigned)cs);
        Serial1.write((uint8_t*)s1buf, (size_t)len);
      }
    }

    // ── 機械可読テレメトリ（TELEM ON のときだけ。既定OFF）────────────────
    // 書式: T\t<seq>\t<ms>\t<rpm>\t<inj01>\t<ign>\t<inj_end>\t<spd01>\t<ne>\t<row>\t<flags>
    // タブ区切りを保つのは、tools/send_map.py が「タブを含む行=テレメトリ」として
    // 読み飛ばす実装になっているため（CLIを無改造のまま使える）。
    // MAP由来の3値（inj/ign/inj_end）を隣り合わせにするため、inj_end は ign の直後（proto=5）。
    // 列数はproto=4と同じ11なので、旧ツールとは互換がない（row/flagsが黙ってずれる）。
    // ツールは VER の proto を見てから TELEM ON すること。
    if (SerialUSBEnabled && mapConsoleTelemetryStream() && !mapConsoleTelemetryMuted()) {
      if (++tel_cnt >= mapConsoleTelemetryDivisor()) {
        tel_cnt = 0;
        // USB CDCのwrite()は、ホストが接続したまま読まない状態だとFIFOが空くまで
        // 無限にスピンする（SerialUSB.cpp）。usbLockを握ったままそうなると
        // コンソールが永久に応答しなくなるので、空きを確認してから書く。
        if (Serial.availableForWrite() >= TELEM_LINE_MAX) {
          uint8_t flags = (ENG_ON ? 0x01 : 0)
                        | (Launch ? 0x02 : 0)
                        | (startState == LOW ? 0x04 : 0)
                        | (mapOutOfRange ? 0x08 : 0);
          int len = snprintf(telBuf, sizeof(telBuf),
            "T\t%u\t%lu\t%u\t%u\t%d\t%d\t%lu\t%d\t%u\t%u\n",
            (unsigned)(++telSeq),          // 連番（取りこぼし検出）
            (unsigned long)millis(),       // 時刻 [ms]
            (unsigned)tachoRpm,            // 回転数 [rpm]
            (unsigned)calculatedINJ_time,  // 噴射時間 [x0.1ms]
            (int)calculatedIGN_CA,         // 点火進角 [CA]
            (int)calculatedINJ_END_CA,     // 噴射終了角 [CA]（MAP外では前回値が残る）
            speed,                         // 車速 [x0.1km/h]
            (int)Ne_deg,                   // クランク角 [CA]
            (unsigned)activeMapRow,        // 採用中のMAP行（255=MAP未使用）
            (unsigned)flags);
          // テレメトリよりコマンド応答性を優先する。取れなければ捨てる
          // （落ちた分はseqの飛びでGUI側が検出できる）。
          if (len > 0 && mapConsoleUsbLock(pdMS_TO_TICKS(5))) {
            Serial.write((const uint8_t*)telBuf, (size_t)len);
            mapConsoleUsbUnlock();
          } else {
            mapConsoleTelemetryDropped();
          }
        } else {
          mapConsoleTelemetryDropped();
        }
      }
    }

    // ── 低速パス（2Hz: 5回に1回）─────────────────────────────────────────
    if (++div_cnt >= SERIAL_USB_DIVISOR) {
      div_cnt = 0;

      if (Launch) {
        fastestdigitalWrite(DISRESET_OUT, LOW);
      } else {
        fastestdigitalWrite(DISRESET_OUT, HIGH);
        starttime = 0;
        distancemm = 0;
        distance = 0;
        gasml = 0.0;
      }
      dispergas = (gasml > 0.0f) ? (float)distance / gasml : 0.0f;

      // 停止減衰: 最後のパルスから時間が経つほど再計算速度を小さくする
      unsigned long age = micros() - speedBefore;
      if (age > speedWidth && speedWidth > 0) {
        unsigned long decay = (36000UL * PERIMETER_MM) / age;
        if (decay < speed) {
          if (decay > 999) decay = 999;
          speed = decay < 1 ? 0 : decay;
        }
      }

      // タイムアウトゼロ化
      if (micros() - tachoBefore >= 1200000UL) {
        tachoRpm = 0;
        usecperdig = 1.0;
        calculatedINJ_time = 0;
        calculatedIGN_CA = 0;
        // 噴射・点火を止めた以上、MAPは参照されていない。
        // ここで255にしないと、GUIが最後に採用した行をハイライトし続ける。
        activeMapRow = 255;
      }
      if (micros() - speedBefore > 8000000UL) {
        speed = 0;
      }

      // MAP転送セッション中はUSBテレメトリを止め、コンソール応答だけを流す。
      // Serial1(メーター・ロガー)側は常に出力し続ける。
      // TELEM ON のときは上の機械可読行に一本化し、2つの書式が混ざらないようにする。
      if (SerialUSBEnabled && !mapConsoleTelemetryMuted() && !mapConsoleTelemetryStream()) {
        if (mapConsoleUsbLock(pdMS_TO_TICKS(50))) {
          Serial.print(tachoRpm);
          Serial.print("\t");
          Serial.print(INJ_timems, 1);
          Serial.print("\t");
          Serial.print(calculatedIGN_CA);
          Serial.print("\t");
          Serial.print(speed / 10.0f, 1);
          Serial.print("\t");
          Serial.print(distance);
          Serial.print("\t");
          Serial.print(gasml, 1);
          Serial.print("\t");
          Serial.print(dispergas, 1);
          Serial.print("\t");
          Serial.print(worktime / 10.0f, 1);
          Serial.print("\t");
          Serial.print(Ne_deg);
          Serial.println();
          mapConsoleUsbUnlock();
        }
      }
    }

    vTaskDelayUntil(&xLastWakeTime, pdMS_TO_TICKS(STATUS_TASK_DELAY_MS));
  }
}

//-----------------------------------------------------------------------------
// setup()
//-----------------------------------------------------------------------------
void setup() {
  Serial.begin(115200);
  Serial1.begin(115200);

  // MAPをEEPROMから復元（無効なら内蔵defaultMapへフォールバック）
  mapStoreInit();
  mapConsoleInit();
  Serial.print(F("MAP SOURCE: "));
  Serial.print(mapGetSource() == MAP_SRC_EEPROM ? F("EEPROM") : F("DEFAULT"));
  Serial.print(F(" ("));
  Serial.print(mapGetActive().count);
  Serial.println(F(" rows)"));

  pinMode(WH_IN, INPUT_PULLUP);
  pinMode(G_IN, INPUT_PULLUP);
  pinMode(STR_IN, INPUT_PULLUP);
  pinMode(ENGOFF_IN, INPUT_PULLUP);
  pinMode(NE_A_IN, INPUT_PULLUP);
  pinMode(NE_B_IN, INPUT_PULLUP);
  pinMode(NE_Z_IN, INPUT_PULLUP);
  pinMode(INJ_OUT, OUTPUT);
  pinMode(IGN_OUT, OUTPUT);
  pinMode(STR_OUT, OUTPUT);
  pinMode(DISRESET_OUT, OUTPUT);
  
  digitalWrite(INJ_OUT, HIGH);
  digitalWrite(IGN_OUT, HIGH);
  digitalWrite(STR_OUT, HIGH);
  digitalWrite(DISRESET_OUT, HIGH);
  
  digitalWrite(DISRESET_OUT, LOW);
  delay(100);
  digitalWrite(DISRESET_OUT, HIGH);
  
#ifdef rmc_ra4m1_20
  if (SD.begin(2000000UL, CS)) {
    delay(100);
    digitalWrite(DISRESET_OUT, HIGH);
    Serial.println(F("SD card initialized."));
    parseCSV();
  } else {
    delay(100);
    digitalWrite(DISRESET_OUT, HIGH);
    Serial.println(F("SD card not used."));
  }
#endif
  
  if (MA735SPIEnabled) {
    SPI.begin();
    pinMode(MA735_CS, OUTPUT);
    digitalWrite(MA735_CS, HIGH);
    Ne_deg = readMA735SPI();
  }
  
  if (AFREnabled) {
    // AFR初期化
  }
  
  if (EncoderEnabled) {
    attachInterrupt(digitalPinToInterrupt(NE_A_IN), ReadNe_ISR, RISING);
  }
  attachInterrupt(digitalPinToInterrupt(WH_IN), WH_PULSE_ISR, FALLING);
  attachInterrupt(digitalPinToInterrupt(G_IN), G_PULSE_ISR, CHANGE);
  
  AGTimer.init(ROUTINE_CYCLE_US, Routine);
  AGTimer.start();
  
  xTaskCreate(statusTask, "StatusTask", 256, NULL, 2, NULL);
  // MAP書き換えコンソール（statusTaskより低優先度）
  xTaskCreate(mapConsoleTask, "MapConsole", 256, NULL, 1, NULL);

  vTaskStartScheduler();
}

void loop() {
  delay(1);
}