//-----------------------------------------------------------------------------
// map_console.h : USBシリアル経由のMAP書き換えコンソール
//
// ビルドせずにMAPを差し替えるための行指向コマンドインタフェース。
// 既存の microSD/RPM_*.CSV と同じ書式のCSVをそのまま流し込める。
//
//   MAP?                     現在のMAPをCSVで出力
//   MAP INFO                 出所・行数・CRC・EEPROM状態を表示
//   MAP BEGIN                転送セッション開始（USBテレメトリを一時停止）
//   <rpm>,<inj>,<ign>        セッション中の1行（ヘッダ行は自動スキップ）
//   MAP END                  検証して原子的に反映
//   MAP ABORT                セッション破棄
//   MAP SET <rpm> <inj> <ign>  1行だけライブ変更
//   MAP SAVE                 EEPROMへ保存（エンジン停止時のみ）
//   MAP LOAD                 EEPROMから読み直し
//   MAP DEFAULT              内蔵デフォルトMAPへ戻す
//   HELP                     コマンド一覧
//
// 応答は必ず1行の "OK ..." または "ERR ..."。PC側スクリプトはこれをACKとして
// 同期するため、1行送信ごとに応答を待てばRX FIFOが溢れない。
//-----------------------------------------------------------------------------
#ifndef MAP_CONSOLE_H
#define MAP_CONSOLE_H

#include <Arduino.h>
#include <Arduino_FreeRTOS.h>

// setup()から呼ぶ（USB出力ロックの生成）。xTaskCreateより前に呼ぶこと。
void mapConsoleInit();

// FreeRTOSタスク本体
void mapConsoleTask(void *pvParameters);

// 転送セッション中はUSBテレメトリを止める（Serial1のロガー出力は止めない）
bool mapConsoleTelemetryMuted();

// USB(Serial)出力の排他。statusTask側の出力ブロックからも使う。
bool mapConsoleUsbLock(TickType_t timeout);
void mapConsoleUsbUnlock();

#endif  // MAP_CONSOLE_H
