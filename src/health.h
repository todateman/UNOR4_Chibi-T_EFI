//-----------------------------------------------------------------------------
// health.h : ハング時の自動復旧（ウォッチドッグ）と、リセット原因の記録
//
// - Routine（AGT割込み）とstatusTaskの両方が動いているときだけWDTを更新する。
//   どちらかが止まれば約1.4秒でリセットし、スタータ操作を受け付ける状態に戻る。
// - HardFault・スタックあふれは原因（PC・割込み番号・タスク名）を記録してから
//   リセットする。記録はVBATTバックアップレジスタに置き、USBの DIAG で読む。
//   RAMの .noinit はブートローダが先頭RAMを使うため、リセット後に残る保証がない。
// - 自動リセット（WDT・自前のソフトリセット）のときだけ走行時間・距離・燃料を
//   引き継ぐ。リセットボタン・電源投入では従来どおり0から始める。
//-----------------------------------------------------------------------------
#ifndef HEALTH_H
#define HEALTH_H

#include <Arduino.h>

// 直前のリセットの種類（RSTSR0/1/2から判定）
enum HealthReset : uint8_t {
  HEALTH_RST_POR = 0,   // 電源投入
  HEALTH_RST_LVD,       // 電圧低下
  HEALTH_RST_PIN,       // リセットボタン（RESピン）
  HEALTH_RST_WDT,       // ウォッチドッグ（ハングからの自動復旧）
  HEALTH_RST_IWDT,      // 独立ウォッチドッグ
  HEALTH_RST_SW,        // ソフトウェアリセット（HardFault・スタックあふれの記録後）
  HEALTH_RST_UPLOAD,    // USBからの書き込み（ブートローダ経由。WDT・SWのフラグが立つが自動リセットではない）
};

// 最後に記録した異常の種類
enum HealthFault : uint8_t {
  HEALTH_FAULT_NONE = 0,
  HEALTH_FAULT_HARD,    // HardFault（MemManage/BusFault/UsageFaultの昇格を含む）
  HEALTH_FAULT_STACK,   // FreeRTOSのスタックあふれ検出
};

// 走行中に積算する値（自動リセットをまたいで引き継ぐ）
struct HealthCarry {
  bool          launch;
  unsigned long elapsedMs;   // millis() - starttime
  unsigned long distancemm;
  float         gasml;
};

// setup()の先頭で呼ぶ。リセット原因を読み取ってフラグをクリアする。
void healthBootInit();

// 自動リセットで、直前に走行中（Launch済み）だった場合だけ true と値を返す。
bool healthRestoreCarry(HealthCarry &out);

// statusTaskの10Hz経路から呼ぶ（CRC付きでバックアップレジスタへ保存）
void healthSaveCarry(const HealthCarry &c);

// WDTを開始する（AGTimer.start()の後、vTaskStartScheduler()の前に呼ぶ）
void healthWatchdogStart();

// statusTaskから100msごとに呼ぶ。routineBeatが前回から進んでいるときだけ更新する。
void healthKick(uint32_t routineBeat);

// 直前のリセット種別と自動リセットの累計（USB表示用）
HealthReset healthLastReset();
const char* healthResetText(HealthReset r);
uint16_t    healthAutoResetCount();
uint32_t    healthWatchdogTimeoutMs();

// DIAG 応答の本体（呼び出し側でUSBロックを取得済みであること）
void healthPrintDiag();
void healthClearDiag();

#endif  // HEALTH_H
