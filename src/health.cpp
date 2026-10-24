//-----------------------------------------------------------------------------
// health.cpp : ハング時の自動復旧（ウォッチドッグ）と、リセット原因の記録（実装）
//-----------------------------------------------------------------------------
#include "health.h"
#include <WDT.h>
#include <Arduino_FreeRTOS.h>

// VBATTバックアップレジスタ（512バイト）の使い方。[0..3] はブートローダの
// ダブルタップ判定（BOOT_DOUBLE_TAP_DATA）が使うので避ける。
#define HEALTH_BKR_DIAG    64     // 診断レコード
#define HEALTH_BKR_CARRY0  128    // 積算値レコード（2面を交互に書き、書き込み途中のリセットに備える）
#define HEALTH_BKR_CARRY1  160

#define HEALTH_DIAG_MAGIC   0x47414944UL  // "DIAG"
#define HEALTH_CARRY_MAGIC  0x59524143UL  // "CARY"
#define HEALTH_TASK_NAME    12

#define PRCR_KEY        0xA500U
#define PRCR_PRC1       0x0002U   // VBTBKR の書き込み許可

// SRAM範囲（Fault時に積まれたスタックフレームを読む前の確認用）
#define HEALTH_RAM_START 0x20000000UL
#define HEALTH_RAM_END   0x20008000UL

// WDT: PCLKB/2048 を 16384 カウント。PCLKB=24MHz で約1.4秒。
// Routine・statusTaskが止まってからリセットまでの最大時間で、始動を受け付けるまでの待ちにもなる。
#define HEALTH_WDT_DIV_CODE  WDT_CLOCK_DIVISION_2048
#define HEALTH_WDT_DIV       2048UL
#define HEALTH_WDT_COUNTS    16384UL

struct DiagRec {
  uint32_t magic;
  uint16_t count;        // 自動リセット（WDT・ソフトリセット）の累計
  uint8_t  fault;        // HealthFault
  uint8_t  pending;      // 自前のソフトリセット直前に1（RSTSRがブートローダに消された場合の保険）
  uint32_t pc;           // 異常時のPC（積まれたスタックフレームから）
  uint32_t lr;
  uint32_t cfsr;
  uint32_t hfsr;
  uint16_t exc;          // 異常時の例外番号（0=タスク, 16+n=IRQn）
  uint8_t  lastReset;    // HealthReset
  uint8_t  rstsr0;
  uint16_t rstsr1;
  uint8_t  rstsr2;
  uint8_t  flagsCleared; // 起動時にRSTSRのフラグを消せたか（消せないと次回の判定を誤る）
  char     task[HEALTH_TASK_NAME];
  uint32_t sum;
};

struct CarryRec {
  uint32_t magic;
  uint32_t seq;
  uint32_t launch;
  uint32_t elapsedMs;
  uint32_t distancemm;
  float    gasml;
  uint32_t sum;
};

static_assert(HEALTH_BKR_DIAG + sizeof(DiagRec) <= HEALTH_BKR_CARRY0, "DiagRec overlaps carry");
static_assert(HEALTH_BKR_CARRY1 - HEALTH_BKR_CARRY0 >= sizeof(CarryRec), "CarryRec overlaps");
static_assert(HEALTH_BKR_CARRY1 + sizeof(CarryRec) <= 512, "VBTBKR overflow");

static HealthReset lastReset   = HEALTH_RST_POR;
static bool        autoReset   = false;
static uint32_t    carrySeq    = 0;
static bool        wdtStarted  = false;
static bool        carryRestored = false;  // 積算値を引き継いで起動したか（DIAG表示用）

// アップロード時の1200bpsタッチで true になる（SerialUSB.cpp）。USB無しのコアでは未定義なのでweak。
extern bool is_watchdog_reset_in_progress_for_upload __attribute__((weak));

//-----------------------------------------------------------------------------
// バックアップレジスタ入出力
//-----------------------------------------------------------------------------
static uint32_t fnv1a(const void *p, size_t n) {
  const uint8_t *b = (const uint8_t *)p;
  uint32_t h = 2166136261UL;
  for (size_t i = 0; i < n; i++) {
    h ^= b[i];
    h *= 16777619UL;
  }
  return h;
}

static void bkrRead(uint16_t off, void *dst, size_t n) {
  uint8_t *d = (uint8_t *)dst;
  for (size_t i = 0; i < n; i++) d[i] = R_SYSTEM->VBTBKR[off + i];
}

static void bkrWrite(uint16_t off, const void *src, size_t n) {
  const uint8_t *s = (const uint8_t *)src;
  // 他の箇所（ブートローダ移行処理）と競合しても元の許可状態に戻す
  const uint16_t prev = R_SYSTEM->PRCR & 0x000FU;
  R_SYSTEM->PRCR = PRCR_KEY | prev | PRCR_PRC1;
  for (size_t i = 0; i < n; i++) R_SYSTEM->VBTBKR[off + i] = s[i];
  R_SYSTEM->PRCR = PRCR_KEY | prev;
}

static bool diagLoad(DiagRec &d) {
  bkrRead(HEALTH_BKR_DIAG, &d, sizeof(d));
  return d.magic == HEALTH_DIAG_MAGIC && d.sum == fnv1a(&d, offsetof(DiagRec, sum));
}

static void diagLoadOrInit(DiagRec &d) {
  if (!diagLoad(d)) {
    memset(&d, 0, sizeof(d));
    d.magic = HEALTH_DIAG_MAGIC;
  }
}

static void diagStore(DiagRec &d) {
  d.sum = fnv1a(&d, offsetof(DiagRec, sum));
  bkrWrite(HEALTH_BKR_DIAG, &d, sizeof(d));
}

static bool carryLoad(uint16_t off, CarryRec &c) {
  bkrRead(off, &c, sizeof(c));
  return c.magic == HEALTH_CARRY_MAGIC && c.sum == fnv1a(&c, offsetof(CarryRec, sum));
}

static void carryInvalidate() {
  const uint32_t zero = 0;
  bkrWrite(HEALTH_BKR_CARRY0, &zero, sizeof(zero));
  bkrWrite(HEALTH_BKR_CARRY1, &zero, sizeof(zero));
}

static void copyTaskName(char *dst, const char *src) {
  size_t i = 0;
  if (src != NULL) {
    for (; i < HEALTH_TASK_NAME - 1 && src[i] >= 0x20 && src[i] < 0x7F; i++) dst[i] = src[i];
  }
  dst[i] = '\0';
}

//-----------------------------------------------------------------------------
// 起動時のリセット原因判定
//-----------------------------------------------------------------------------
void healthBootInit() {
  const uint8_t  r0 = R_SYSTEM->RSTSR0;
  const uint16_t r1 = R_SYSTEM->RSTSR1;
  const uint8_t  r2 = R_SYSTEM->RSTSR2;

  DiagRec d;
  diagLoadOrInit(d);

  if      (r0 & 0x01U)      lastReset = HEALTH_RST_POR;    // PORF
  else if (r0 & 0x0EU)      lastReset = HEALTH_RST_LVD;    // LVD0RF〜LVD2RF
  else if (r1 & 0x0002U)    lastReset = HEALTH_RST_WDT;    // WDTRF
  else if (r1 & 0x0001U)    lastReset = HEALTH_RST_IWDT;   // IWDTRF
  else if (r1 & 0x0004U)    lastReset = HEALTH_RST_SW;     // SWRF
  else if (d.pending)       lastReset = HEALTH_RST_SW;     // フラグが消されていても自前のリセットは分かる
  else if (!(r2 & 0x01U))   lastReset = HEALTH_RST_POR;    // CWSF=0: コールドスタート
  else                      lastReset = HEALTH_RST_PIN;

  // 検出フラグは「1を確認して0を書く」と消える。CWSFは1を書いてウォームスタート扱いにする。
  R_SYSTEM->RSTSR0 = 0;
  R_SYSTEM->RSTSR1 = 0;
  R_SYSTEM->RSTSR2 = 0x01U;

  autoReset = (lastReset == HEALTH_RST_WDT || lastReset == HEALTH_RST_IWDT || lastReset == HEALTH_RST_SW);

  if (lastReset == HEALTH_RST_POR) {
    // 電源投入時のバックアップレジスタは不定。累計も含めて初期化する。
    memset(&d, 0, sizeof(d));
    d.magic = HEALTH_DIAG_MAGIC;
  }
  if (autoReset && d.count < 0xFFFFU) d.count++;
  d.pending      = 0;
  d.lastReset    = (uint8_t)lastReset;
  d.rstsr0       = r0;
  d.rstsr1       = r1;
  d.rstsr2       = r2;
  d.flagsCleared = (R_SYSTEM->RSTSR0 == 0 && R_SYSTEM->RSTSR1 == 0) ? 1 : 0;
  diagStore(d);

  // 手動リセット・電源投入では従来どおり積算値を0から始める
  if (!autoReset) carryInvalidate();
}

bool healthRestoreCarry(HealthCarry &out) {
  if (!autoReset) return false;
  CarryRec a, b;
  const bool va = carryLoad(HEALTH_BKR_CARRY0, a);
  const bool vb = carryLoad(HEALTH_BKR_CARRY1, b);
  if (!va && !vb) return false;
  const CarryRec &c = (va && vb) ? ((int32_t)(a.seq - b.seq) >= 0 ? a : b) : (va ? a : b);
  carrySeq = c.seq;
  if (!c.launch) return false;
  out.launch     = true;
  out.elapsedMs  = c.elapsedMs;
  out.distancemm = c.distancemm;
  out.gasml      = c.gasml;
  carryRestored  = true;
  return true;
}

void healthSaveCarry(const HealthCarry &c) {
  CarryRec r;
  r.magic      = HEALTH_CARRY_MAGIC;
  r.seq        = ++carrySeq;
  r.launch     = c.launch ? 1 : 0;
  r.elapsedMs  = c.elapsedMs;
  r.distancemm = c.distancemm;
  r.gasml      = c.gasml;
  r.sum        = fnv1a(&r, offsetof(CarryRec, sum));
  bkrWrite((r.seq & 1U) ? HEALTH_BKR_CARRY1 : HEALTH_BKR_CARRY0, &r, sizeof(r));
}

//-----------------------------------------------------------------------------
// ウォッチドッグ
//-----------------------------------------------------------------------------
void healthWatchdogStart() {
  wdt_cfg_t cfg;
  cfg.timeout        = WDT_TIMEOUT_16384;
  cfg.clock_division = HEALTH_WDT_DIV_CODE;
  cfg.window_start   = WDT_WINDOW_START_100;   // いつ更新してもよい（窓なし）
  cfg.window_end     = WDT_WINDOW_END_0;
  cfg.reset_control  = WDT_RESET_CONTROL_RESET;
  // アイドル中はFreeRTOSがスリープする。スリープ中も数えないと、全タスクが待ちのまま
  // 割込みも止まった状態を検出できない。
  cfg.stop_control   = WDT_STOP_CONTROL_DISABLE;
  cfg.p_callback     = NULL;
  cfg.p_context      = NULL;
  cfg.p_extend       = NULL;
  wdtStarted = WDT.begin(cfg) != 0;
}

void healthKick(uint32_t routineBeat) {
  static uint32_t lastBeat = 0;
  // アップロード要求（1200bpsタッチ）後は更新をやめ、ブートローダへのリセットを妨げない
  if (&is_watchdog_reset_in_progress_for_upload != NULL && is_watchdog_reset_in_progress_for_upload) return;
  if (routineBeat == lastBeat) return;   // Routine（AGT割込み）が止まっている
  lastBeat = routineBeat;
  WDT.refresh();
}

uint32_t healthWatchdogTimeoutMs() {
  if (!wdtStarted) return 0;
  const uint32_t pclkb = R_FSP_SystemClockHzGet(FSP_PRIV_CLOCK_PCLKB);
  return (uint32_t)((uint64_t)HEALTH_WDT_COUNTS * HEALTH_WDT_DIV * 1000ULL / pclkb);
}

//-----------------------------------------------------------------------------
// 異常時の記録とリセット
//-----------------------------------------------------------------------------
static void recordAndReset(HealthFault kind, const uint32_t *frame, const char *taskName) {
  DiagRec d;
  diagLoadOrInit(d);
  d.fault = (uint8_t)kind;
  d.cfsr  = SCB->CFSR;
  d.hfsr  = SCB->HFSR;
  if (frame != NULL) {
    d.lr  = frame[5];
    d.pc  = frame[6];
    d.exc = (uint16_t)(frame[7] & 0x1FFU);   // 積まれたxPSRの例外番号
  } else {
    d.lr = d.pc = 0;
    d.exc = (uint16_t)(__get_IPSR() & 0x1FFU);
  }
  copyTaskName(d.task, taskName);
  d.pending = 1;
  diagStore(d);
  NVIC_SystemReset();
}

extern "C" void healthHardFaultC(uint32_t *frame) {
  // スタックが壊れていると読みに行くだけで再度Faultしてロックアップするため範囲を確かめる
  const uint32_t a = (uint32_t)frame;
  if (a < HEALTH_RAM_START || a + 32U > HEALTH_RAM_END || (a & 3U)) frame = NULL;

  const char *name = NULL;
  if (frame != NULL && (frame[7] & 0x1FFU) == 0) {
    TaskHandle_t h = xTaskGetCurrentTaskHandle();
    const uint32_t ha = (uint32_t)h;
    if (ha >= HEALTH_RAM_START && ha < HEALTH_RAM_END) name = pcTaskGetName(h);
  } else {
    name = "ISR";
  }
  recordAndReset(HEALTH_FAULT_HARD, frame, name);
}

// FSPの既定（weak）は無限ループで、ECUが丸ごと止まる。記録してリセットする版に差し替える。
extern "C" __attribute__((naked)) void HardFault_Handler(void) {
  __asm volatile(
    "tst   lr, #4          \n"
    "ite   eq              \n"
    "mrseq r0, msp         \n"
    "mrsne r0, psp         \n"
    "b     healthHardFaultC\n");
}

extern "C" void vApplicationStackOverflowHook(TaskHandle_t xTask, char *pcTaskName) {
  (void)xTask;
  recordAndReset(HEALTH_FAULT_STACK, NULL, pcTaskName);
}

//-----------------------------------------------------------------------------
// 表示
//-----------------------------------------------------------------------------
HealthReset healthLastReset() {
  return lastReset;
}

const char* healthResetText(HealthReset r) {
  switch (r) {
    case HEALTH_RST_POR:  return "POR";
    case HEALTH_RST_LVD:  return "LVD";
    case HEALTH_RST_PIN:  return "PIN";
    case HEALTH_RST_WDT:  return "WDT";
    case HEALTH_RST_IWDT: return "IWDT";
    case HEALTH_RST_SW:   return "SW";
    default:              return "?";
  }
}

uint16_t healthAutoResetCount() {
  DiagRec d;
  return diagLoad(d) ? d.count : 0;
}

static void printHex(uint32_t v) {
  Serial.print(F("0x"));
  for (int8_t s = 28; s >= 0; s -= 4) Serial.print((v >> s) & 0xFU, HEX);
}

void healthPrintDiag() {
  DiagRec d;
  const bool valid = diagLoad(d);
  if (!valid) memset(&d, 0, sizeof(d));
  static const char *const faultText[] = {"NONE", "HARD", "STACK"};

  Serial.print(F("OK DIAG rst="));
  Serial.print(healthResetText(lastReset));
  Serial.print(F(" count="));
  Serial.print(d.count);
  Serial.print(F(" fault="));
  Serial.print(d.fault < 3 ? faultText[d.fault] : "?");
  Serial.print(F(" pc="));
  printHex(d.pc);
  Serial.print(F(" lr="));
  printHex(d.lr);
  Serial.print(F(" cfsr="));
  printHex(d.cfsr);
  Serial.print(F(" hfsr="));
  printHex(d.hfsr);
  Serial.print(F(" exc="));
  Serial.print(d.exc);
  Serial.print(F(" task="));
  Serial.print(d.task[0] ? d.task : "-");
  Serial.print(F(" rstsr="));
  printHex(((uint32_t)d.rstsr0 << 24) | ((uint32_t)d.rstsr1 << 8) | d.rstsr2);
  Serial.print(F(" clr="));
  Serial.print(d.flagsCleared);
  Serial.print(F(" wdt_ms="));
  Serial.print(healthWatchdogTimeoutMs());
  Serial.print(F(" carry="));
  Serial.println(carryRestored ? 1 : 0);
}

void healthClearDiag() {
  DiagRec d;
  diagLoadOrInit(d);
  const uint8_t keepReset = d.lastReset;
  memset(&d, 0, sizeof(d));
  d.magic     = HEALTH_DIAG_MAGIC;
  d.lastReset = keepReset;
  d.flagsCleared = 1;
  diagStore(d);
}
