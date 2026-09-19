//-----------------------------------------------------------------------------
// map_console.cpp : USBシリアル経由のMAP書き換えコンソール（実装）
//-----------------------------------------------------------------------------
#include "map_console.h"
#include "map_store.h"

#define MAP_CONSOLE_LINE_MAX   96    // 1行の最大長（CSV1行は十分に収まる）
#define MAP_CONSOLE_PERIOD_MS  5     // ポーリング周期

// セッションを開いたままホストが消えた場合の救済。これが無いと
// mapConsoleTelemetryMuted()がtrueのまま残り、テレメトリが永久に止まる。
#define MAP_SESSION_TIMEOUT_MS 5000

// テレメトリ分周比の範囲（statusTaskの基本周期100msに対して 100ms〜2000ms）
#define TELEM_DIV_MIN          1
#define TELEM_DIV_MAX          20
#define TELEM_BASE_MS          100   // = main.cpp の STATUS_TASK_DELAY_MS

#define FW_VERSION             "1.3.0"
// v3: MAP列を4列化（rpm,inj,ign,inj_end_ca）
// v4: テレメトリT行の末尾に inj_end を追加（先頭10個だけ読む旧パーサとは互換）
// v5: テレメトリT行の inj_end を ign の直後へ移動（列数は同じ11なので旧パーサとは非互換）
#define PROTO_VERSION          5

// main.cpp のエンジン状態（EEPROM書き込みの安全ガードに使用）
extern bool ENG_ON;
extern volatile uint16_t tachoRpm;

//-----------------------------------------------------------------------------
// 内部状態
//-----------------------------------------------------------------------------
static SemaphoreHandle_t usbLock = NULL;
static volatile bool     sessionActive = false;   // MAP BEGIN 〜 END/ABORT
static uint32_t          sessionLastMs = 0;       // セッション中の最終受信時刻
static char              lineBuf[MAP_CONSOLE_LINE_MAX];
static uint8_t           lineLen  = 0;
static bool              lineOverflow = false;

static volatile bool     telemOn   = false;       // TELEM ON/OFF（既定OFF）
static volatile uint8_t  telemDiv  = 1;           // 分周比（1 = 100ms = 10Hz）
static volatile uint16_t telemDrop = 0;           // 出力を落としたサンプル数

//-----------------------------------------------------------------------------
// USB出力の排他
//-----------------------------------------------------------------------------
void mapConsoleInit() {
  // configUSE_MUTEXES に依存しないバイナリセマフォを使う。
  // 保持区間は数行のprintのみで、優先度継承が無くても実害はない。
  usbLock = xSemaphoreCreateBinary();
  if (usbLock != NULL) xSemaphoreGive(usbLock);
}

bool mapConsoleUsbLock(TickType_t timeout) {
  if (usbLock == NULL) return true;   // 生成失敗時は排他なしで動作継続
  return xSemaphoreTake(usbLock, timeout) == pdTRUE;
}

void mapConsoleUsbUnlock() {
  if (usbLock != NULL) xSemaphoreGive(usbLock);
}

bool mapConsoleTelemetryMuted() {
  return sessionActive;
}

//-----------------------------------------------------------------------------
// テレメトリ設定（statusTaskから参照）
//-----------------------------------------------------------------------------
bool mapConsoleTelemetryStream() {
  return telemOn;
}

uint8_t mapConsoleTelemetryDivisor() {
  return telemDiv;
}

void mapConsoleTelemetryDropped() {
  telemDrop++;
}

//-----------------------------------------------------------------------------
// 応答ヘルパ（呼び出し側でUSBロックを取得済みであること）
//-----------------------------------------------------------------------------
static void replyOk(const char* msg) {
  Serial.print(F("OK "));
  Serial.println(msg);
}

static void replyErr(const char* msg) {
  Serial.print(F("ERR "));
  Serial.println(msg);
}

//-----------------------------------------------------------------------------
// トークン比較・切り出し
//-----------------------------------------------------------------------------
// s が prefix で始まるか（大文字小文字を無視）。一致したら prefix の直後を返す。
static const char* matchToken(const char* s, const char* prefix) {
  while (*prefix) {
    if (toupper((unsigned char)*s) != toupper((unsigned char)*prefix)) return NULL;
    s++;
    prefix++;
  }
  return s;
}

static const char* skipSpace(const char* s) {
  while (*s == ' ' || *s == '\t') s++;
  return s;
}

// 空白区切りの符号なし10進数を1つ読む
static bool parseUInt(const char*& s, unsigned long& out) {
  s = skipSpace(s);
  if (!isdigit((unsigned char)*s)) return false;
  unsigned long v = 0;
  while (isdigit((unsigned char)*s)) {
    v = v * 10 + (unsigned long)(*s - '0');
    if (v > 999999UL) return false;
    s++;
  }
  out = v;
  return true;
}

//-----------------------------------------------------------------------------
// 各コマンド
//-----------------------------------------------------------------------------
static void cmdDump() {
  const MapTable& t = mapGetActive();
  Serial.println(F("RPM,  INJ(0.1msec), IGN(CA), INJ_END(CA)"));
  for (uint8_t i = 0; i < t.count; i++) {
    Serial.print(t.e[i].rpm);
    Serial.print(',');
    Serial.print(t.e[i].inj_time);
    Serial.print(',');
    Serial.print(t.e[i].ign_ca);
    Serial.print(',');
    Serial.println(t.e[i].inj_end_ca);
  }
  Serial.print(F("OK ROWS "));
  Serial.println(t.count);
}

static void cmdInfo() {
  const MapTable& t = mapGetActive();
  Serial.print(F("INFO src="));
  switch (mapGetSource()) {
    case MAP_SRC_EEPROM:  Serial.print(F("EEPROM"));  break;
    case MAP_SRC_SERIAL:  Serial.print(F("SERIAL"));  break;
    default:              Serial.print(F("DEFAULT")); break;
  }
  Serial.print(F(" rows="));
  Serial.print(t.count);
  Serial.print(F(" crc=0x"));
  Serial.print(mapGetActiveCrc(), HEX);
  Serial.print(F(" eeprom="));
  Serial.print(mapEepromValid() ? F("VALID") : F("EMPTY"));
  Serial.print(F(" rpm="));
  Serial.print(tachoRpm);
  Serial.print(F(" eng="));
  Serial.println(ENG_ON ? F("ON") : F("OFF"));
  replyOk("INFO");
}

static void cmdHelp() {
  Serial.println(F("MAP?                  dump current map as CSV"));
  Serial.println(F("MAP INFO              source / rows / crc / eeprom state"));
  Serial.println(F("MAP BEGIN             start CSV transfer session"));
  Serial.println(F("  <rpm>,<inj>,<ign>,<inj_end_ca>   one CSV row (header line is skipped)"));
  Serial.println(F("MAP END               validate and apply atomically"));
  Serial.println(F("MAP ABORT             discard the session"));
  Serial.println(F("MAP SET r i g e       change one row live (rpm inj ign inj_end_ca)"));
  Serial.println(F("MAP SAVE              store to EEPROM (stopped only, skip if same)"));
  Serial.println(F("MAP LOAD              reload from EEPROM"));
  Serial.println(F("MAP DEFAULT           restore built-in default map"));
  Serial.println(F("TELEM ON [ms]         start machine-readable telemetry"));
  Serial.println(F("TELEM OFF             stop it (back to the 2Hz human line)"));
  Serial.println(F("TELEM?                telemetry state"));
  Serial.println(F("VER                   firmware / protocol version"));
  Serial.println(F("PING                  connectivity check"));
  replyOk("HELP");
}

//-----------------------------------------------------------------------------
// TELEM: 機械可読テレメトリの制御
//
// statusTaskの基本周期100msに対する分周で刻む。要求msは100msへ丸めて
// 1..20（100ms〜2000ms）にクランプし、実際に採用した周期を応答で返す。
//-----------------------------------------------------------------------------
static void cmdTelemOn(const char* args) {
  const char* p = args;
  unsigned long ms;
  if (parseUInt(p, ms)) {
    unsigned long d = (ms + TELEM_BASE_MS / 2) / TELEM_BASE_MS;   // 四捨五入
    if (d < TELEM_DIV_MIN) d = TELEM_DIV_MIN;
    if (d > TELEM_DIV_MAX) d = TELEM_DIV_MAX;
    telemDiv = (uint8_t)d;
  }
  // ms省略時は現在の分周比を保つ（初回は1 = 100ms）
  telemDrop = 0;
  telemOn   = true;
  Serial.print(F("OK TELEM ON "));
  Serial.println((unsigned)telemDiv * TELEM_BASE_MS);
}

static void cmdTelemQuery() {
  Serial.print(F("TELEM on="));
  Serial.print(telemOn ? 1 : 0);
  Serial.print(F(" ms="));
  Serial.print((unsigned)telemDiv * TELEM_BASE_MS);
  Serial.print(F(" drop="));
  Serial.println(telemDrop);
  replyOk("TELEM");
}

static void cmdVer() {
  Serial.print(F("OK VER "));
  Serial.print(F(FW_VERSION));
  Serial.print(F(" proto="));
  Serial.println(PROTO_VERSION);
}

static void cmdSave() {
  if (ENG_ON || tachoRpm != 0) {
    // データフラッシュの消去・書き込みは数msブロックするため稼働中は拒否する
    replyErr("ENGINE_RUNNING");
    return;
  }
  switch (mapSaveToEEPROM()) {
    case MAP_SAVE_WRITTEN:   replyOk("SAVED");     break;
    case MAP_SAVE_UNCHANGED: replyOk("UNCHANGED"); break;  // 内容が同一で未書き込み
    default:                 replyErr("EEPROM_WRITE_FAILED"); break;
  }
}

static void cmdSet(const char* args) {
  unsigned long rpm, inj, ign, endCa;
  const char* p = args;
  if (!parseUInt(p, rpm) || !parseUInt(p, inj) || !parseUInt(p, ign) || !parseUInt(p, endCa)) {
    replyErr("USAGE_MAP_SET_RPM_INJ_IGN_ENDCA");
    return;
  }
  if (rpm > 65535UL || inj > 255UL || ign > 65535UL || endCa > 65535UL) {
    replyErr("VALUE_OUT_OF_RANGE");
    return;
  }
  if (mapSetEntry((uint16_t)rpm, (uint8_t)inj, (uint16_t)ign, (uint16_t)endCa)) {
    replyOk("SET");
  } else {
    replyErr("SET_REJECTED");
  }
}

static void cmdEnd() {
  MapValidation v = mapStagingValidate();
  if (v != MAP_OK) {
    sessionActive = false;
    replyErr(mapValidationText(v));
    return;
  }
  uint8_t rows = mapStagingCount();
  mapStagingApply();
  sessionActive = false;
  Serial.print(F("OK APPLIED "));
  Serial.println(rows);
}

// セッション中のCSV行を処理
static void handleCsvRow(const char* line) {
  uint16_t rpm, ign, endCa;
  uint8_t  inj;
  bool     skip;

  if (!mapParseCsvLine(line, rpm, inj, ign, endCa, skip)) {
    mapStagingClear();
    sessionActive = false;
    replyErr("BAD_CSV_LINE");
    return;
  }
  if (skip) {
    replyOk("SKIP");
    return;
  }
  if (!mapStagingAppend(rpm, inj, ign, endCa)) {
    mapStagingClear();
    sessionActive = false;
    replyErr("TOO_MANY_ROWS");
    return;
  }
  Serial.print(F("OK ROW "));
  Serial.println(mapStagingCount());
}

//-----------------------------------------------------------------------------
// 1行の処理
//-----------------------------------------------------------------------------
static void processLine(const char* line) {
  const char* p = skipSpace(line);
  if (*p == '\0') return;   // 空行は無反応（改行連打で応答が乱れないように）

  const char* rest = matchToken(p, "MAP");
  if (rest != NULL) {
    // "MAP?" / "MAP <SUBCOMMAND>"
    if (*rest == '?') {
      cmdDump();
      return;
    }
    if (*rest == ' ' || *rest == '\t') {
      const char* sub = skipSpace(rest);
      const char* a;
      if ((a = matchToken(sub, "BEGIN")) != NULL && *skipSpace(a) == '\0') {
        mapStagingClear();
        sessionActive = true;
        replyOk("BEGIN");
        return;
      }
      if ((a = matchToken(sub, "END")) != NULL && *skipSpace(a) == '\0') {
        cmdEnd();
        return;
      }
      if ((a = matchToken(sub, "ABORT")) != NULL && *skipSpace(a) == '\0') {
        mapStagingClear();
        sessionActive = false;
        replyOk("ABORTED");
        return;
      }
      if ((a = matchToken(sub, "INFO")) != NULL && *skipSpace(a) == '\0') {
        cmdInfo();
        return;
      }
      if ((a = matchToken(sub, "SAVE")) != NULL && *skipSpace(a) == '\0') {
        cmdSave();
        return;
      }
      if ((a = matchToken(sub, "LOAD")) != NULL && *skipSpace(a) == '\0') {
        if (mapLoadFromEEPROM()) replyOk("LOADED");
        else                     replyErr("EEPROM_EMPTY");
        return;
      }
      if ((a = matchToken(sub, "DEFAULT")) != NULL && *skipSpace(a) == '\0') {
        mapApplyDefault();
        replyOk("DEFAULT");
        return;
      }
      if ((a = matchToken(sub, "SET")) != NULL) {
        cmdSet(a);
        return;
      }
      replyErr("UNKNOWN_MAP_SUBCOMMAND");
      return;
    }
    // "MAP" 単体、または "MAPxxx" は下の分岐へ落とさずエラーにする
    if (*rest == '\0') {
      cmdDump();
      return;
    }
  }

  const char* h = matchToken(p, "HELP");
  if (h != NULL && *skipSpace(h) == '\0') {
    cmdHelp();
    return;
  }

  // TELEM / VER / PING はセッション判定より前に置く。
  // 後ろに置くと転送セッション中にCSV行として食われてしまう。
  const char* t = matchToken(p, "TELEM");
  if (t != NULL) {
    if (*t == '?' && *skipSpace(t + 1) == '\0') {
      cmdTelemQuery();
      return;
    }
    if (*t == ' ' || *t == '\t') {
      const char* sub = skipSpace(t);
      const char* a;
      if ((a = matchToken(sub, "OFF")) != NULL && *skipSpace(a) == '\0') {
        telemOn = false;
        replyOk("TELEM OFF");
        return;
      }
      if ((a = matchToken(sub, "ON")) != NULL) {
        cmdTelemOn(a);
        return;
      }
      replyErr("UNKNOWN_TELEM_SUBCOMMAND");
      return;
    }
    if (*t == '\0') {
      cmdTelemQuery();
      return;
    }
  }

  const char* v = matchToken(p, "VER");
  if (v != NULL && *skipSpace(v) == '\0') {
    cmdVer();
    return;
  }

  const char* g = matchToken(p, "PING");
  if (g != NULL && *skipSpace(g) == '\0') {
    replyOk("PONG");
    return;
  }

  // セッション中ならCSV行として解釈する
  if (sessionActive) {
    handleCsvRow(p);
    return;
  }

  replyErr("UNKNOWN_COMMAND");
}

//-----------------------------------------------------------------------------
// タスク本体
//-----------------------------------------------------------------------------
void mapConsoleTask(void *pvParameters) {
  (void)pvParameters;
  TickType_t xLastWakeTime = xTaskGetTickCount();

  for (;;) {
    while (Serial.available() > 0) {
      int c = Serial.read();
      if (c < 0) break;

      if (c == '\n' || c == '\r') {
        if (lineOverflow) {
          lineLen = 0;
          lineOverflow = false;
          mapStagingClear();
          sessionActive = false;
          if (mapConsoleUsbLock(pdMS_TO_TICKS(50))) {
            replyErr("LINE_TOO_LONG");
            mapConsoleUsbUnlock();
          }
          continue;
        }
        lineBuf[lineLen] = '\0';
        if (lineLen > 0) {
          if (mapConsoleUsbLock(pdMS_TO_TICKS(50))) {
            processLine(lineBuf);
            mapConsoleUsbUnlock();
          }
          // MAP BEGIN もここを通るので、セッション開始時刻の初期化も兼ねる
          if (sessionActive) sessionLastMs = millis();
        }
        lineLen = 0;
        continue;
      }

      if (lineLen < MAP_CONSOLE_LINE_MAX - 1) {
        lineBuf[lineLen++] = (char)c;
      } else {
        lineOverflow = true;   // 改行が来るまで読み捨てる
      }
    }

    // セッションを開いたままホストが消えた場合の救済。
    // これが無いとテレメトリがミュートされたまま復帰しない。
    if (sessionActive &&
        (uint32_t)(millis() - sessionLastMs) > MAP_SESSION_TIMEOUT_MS) {
      mapStagingClear();
      sessionActive = false;
      if (mapConsoleUsbLock(pdMS_TO_TICKS(50))) {
        replyErr("SESSION_TIMEOUT");
        mapConsoleUsbUnlock();
      }
    }

    vTaskDelayUntil(&xLastWakeTime, pdMS_TO_TICKS(MAP_CONSOLE_PERIOD_MS));
  }
}
