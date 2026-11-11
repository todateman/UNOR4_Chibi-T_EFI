// http://akiracing.com/2018/01/12/arduino_tachometer/
// ECU Shield は入出力がHIGH/LOWが反転しているので注意
#include <Arduino.h>
#include "AGTimerR4.h"
#include "fastestdigitalRW.hpp"

// =====================================================================
// インジェクタ単体特性試験用スケッチ（INJ_testブランチ 第2版）
//
// 第1版（delayMicroseconds方式）からの変更点:
//  1) パルス生成を main.cpp と同じ方式に変更
//     （AGTimer 24us周期の割り込み内で micros() を見てON/OFF）。
//     実車ファームと同じ量子化（指令 +0〜24us）で噴くので、校正値をそのまま持ち込める。
//  2) パルス幅は整数us指定（U<us>）。W<ms> も引き続き使える。
//  3) 周期（ONからONまで）を固定する T<ms> を追加。
//     従来の I<ms>（OFF区間指定）も互換のため残している。
//  4) シリアルからの開始 G / 停止 S を追加（キルスイッチは安全条件として必須のまま）。
//  5) 分割バースト B<発数> と再加圧待ち H<ms> / C（続行）を追加。
//     ダイヤフラムポンプ（2cc）の容量を超えないよう、1バーストを分けて打てる。
//  6) 捨て打ち Q<発数>（ノズルを濡らす。計量対象外）を追加。
//  7) 1発ごとの実測パルス幅（micros()差）の最小・平均・最大と、周期の最小・最大を集計。
//  8) PC側スクリプト（tools/inj_bench.py）が読む機械可読行 "#INJ,..." を出力。
//  9) 1行に複数コマンドを空白区切りで書ける（例: "U4000 N750 T50 B300 H0"）。
//
// platformio.ini の `uno_r4_minima_inj_test` 環境でこのファイルのみをビルドする。
//   ビルド/書き込み: pio run -e uno_r4_minima_inj_test -t upload
//
// コマンド一覧（115200bps、改行終端）:
//   U<us>   パルス幅 [us]（50〜30000）
//   W<ms>   パルス幅 [ms]（小数可。内部でusに変換）
//   N<発数> 1バーストの総発数（1〜100000）
//   T<ms>   周期 [ms]（ONからON。固定周期モードになる）
//   I<ms>   OFF区間 [ms]（周期 = パルス幅 + I。第1版互換モードになる）
//   B<発数> 分割バーストの1回分の発数（0 = 分割しない）
//   H<ms>   分割の間の待ち時間 [ms]（0 = C コマンドが来るまで待つ）
//   Q<発数> 捨て打ちを今すぐ実行（キルスイッチOFF(導通)が必要）
//   G       バースト開始（キルスイッチOFF(導通)が必要）
//   C       分割待ちから続行
//   S       停止（噴射中でも即OFF）
//   ?       現在の設定と状態を表示
//
// 機械可読行（PC側スクリプト用。key=value をカンマ区切り）:
//   #INJ,CFG,pw_us=..,n=..,period_us=..,mode=T|I,sub=..,pause_ms=..,armed=0|1
//   #INJ,START,run=..,kind=burst|prime,pw_us=..,n=..,period_us=..,sub=..
//   #INJ,PROG,run=..,fired=..
//   #INJ,PAUSE,run=..,fired=..
//   #INJ,RESUME,run=..,fired=..
//   #INJ,DONE,run=..,kind=..,pw_us=..,n_cmd=..,n_fired=..,period_us=..,
//        w_min_us=..,w_mean_us=..,w_max_us=..,per_min_us=..,per_max_us=..,
//        elapsed_ms=..,pause_ms_total=..,abort=none|kill|stop,partial=0|1
//   #INJ,ERR,msg=..
// =====================================================================

#define ROUTINE_CYCLE_US 24            // main.cpp と同じ割り込み周期 [us]

static const uint32_t PW_US_MIN      = 50;
static const uint32_t PW_US_MAX      = 30000;
static const uint32_t N_MAX          = 100000;
static const uint32_t OFF_US_MIN     = 2000;   // 最低OFF時間（閉弁完了とコイル放電の余裕）
static const uint16_t KILL_DEBOUNCE  = 40;     // キルスイッチ復帰の確定に必要な連続回数（約1ms）
static const uint32_t PROG_INTERVAL_MS = 250;
static const uint32_t STARTER_RELEASE_MS = 100; // スタータボタンを「離した」と見なすのに必要な連続HIGH時間
static const uint32_t SERIAL_WAIT_MS   = 3000;  // 起動時にPCのポート接続を待つ最大時間

const uint8_t STR_IN       = 6;   // スタータボタン入力
const uint8_t ENGOFF_IN    = 7;   // キルスイッチ入力
const uint8_t INJ_OUT      = A0;  // インジェクタ出力（ON:LOW, OFF:HIGH）
const uint8_t IGN_OUT      = A1;  // イグニッション出力
const uint8_t STR_OUT      = A2;  // スタータ出力
const uint8_t DISRESET_OUT = A3;  // リセット状態出力

// ---- 設定値（loop側で変更。バースト開始時にISR用変数へコピーする）----
uint32_t cfgPulseUs   = 4000;   // パルス幅 [us]
uint32_t cfgCount     = 300;    // 総発数
uint32_t cfgPeriodMs  = 50;     // 周期 [ms]（mode T）
uint32_t cfgIntervalMs = 50;    // OFF区間 [ms]（mode I）
bool     cfgPeriodFixed = true; // true: T（固定周期） / false: I（OFF区間指定）
uint32_t cfgSubCount  = 0;      // 分割1回分の発数（0=分割なし）
uint32_t cfgPauseMs   = 0;      // 分割の間の待ち [ms]（0=C待ち）

// ---- ISRと共有する実行時変数 ----
enum RunState : uint8_t { ST_IDLE = 0, ST_OFF, ST_ON, ST_PAUSED, ST_DONE };
enum AbortReason : uint8_t { AB_NONE = 0, AB_KILL, AB_STOP };

volatile RunState    runState   = ST_IDLE;
volatile AbortReason abortReason = AB_NONE;
volatile bool        stopRequest = false;
volatile bool        partialShot = false;

volatile uint32_t rPulseUs   = 0;
volatile uint32_t rPeriodUs  = 0;
volatile uint32_t rTarget    = 0;
volatile uint32_t rSubCount  = 0;
volatile uint32_t rFired     = 0;
volatile uint32_t rNextOnUs  = 0;   // 次にONする時刻（micros）
volatile uint32_t rOnUs      = 0;   // 今回ONした時刻
volatile uint32_t rLastOnUs  = 0;   // 前回ONした時刻（周期の実測用）
volatile bool     rHaveLastOn = false;
volatile uint32_t rWMin = 0, rWMax = 0;
volatile uint64_t rWSum = 0;
volatile uint32_t rPerMin = 0, rPerMax = 0;
volatile uint16_t killCount = 0;

// ---- loop側だけが使う変数 ----
uint32_t runId          = 0;
bool     runIsPrime     = false;
uint32_t runStartMs     = 0;
uint32_t pauseStartMs   = 0;
uint32_t pauseTotalMs   = 0;
uint32_t lastProgMs     = 0;
bool     pauseReported  = false;
bool     starterReleased = false;  // 起動時に押されたままでも、一度離すまで開始しない
uint32_t starterLastLowMs = 0;     // スタータボタンが最後に押されていた（LOWだった）時刻
bool     cfgPrinted      = false;  // この行の処理中に #INJ,CFG を出したか

String rxLine;

//-----------------------------------------------------------------------------
// Routine(): AGTimerより24us周期で実行。main.cpp の噴射ON/OFF処理と同じ順序で書いている
// （ONは micros()取得→出力LOW、OFFは micros()取得→出力HIGH）。
//-----------------------------------------------------------------------------
void Routine() {
  RunState st = runState;
  if (st != ST_OFF && st != ST_ON) return;

  // 安全策: キルスイッチが戻されたら（HIGHが約1ms続いたら）即中断
  if (fastestdigitalRead(ENGOFF_IN) == HIGH) {
    if (++killCount >= KILL_DEBOUNCE) {
      fastestdigitalWrite(INJ_OUT, HIGH);
      if (st == ST_ON) partialShot = true;
      abortReason = AB_KILL;
      runState = ST_DONE;
      return;
    }
  } else {
    killCount = 0;
  }

  if (stopRequest) {
    fastestdigitalWrite(INJ_OUT, HIGH);
    if (st == ST_ON) partialShot = true;
    abortReason = AB_STOP;
    runState = ST_DONE;
    return;
  }

  if (st == ST_OFF) {
    // 噴射タイミングに達したらON
    if ((int32_t)(micros() - rNextOnUs) >= 0) {
      uint32_t tOn = micros();
      fastestdigitalWrite(INJ_OUT, LOW);
      rOnUs = tOn;
      if (rHaveLastOn) {
        uint32_t per = tOn - rLastOnUs;
        if (per < rPerMin) rPerMin = per;
        if (per > rPerMax) rPerMax = per;
      }
      rLastOnUs = tOn;
      rHaveLastOn = true;
      runState = ST_ON;
    }
    return;
  }

  // st == ST_ON: 噴射時間が経過したらOFF
  if (micros() - rOnUs >= rPulseUs) {
    uint32_t tOff = micros();
    fastestdigitalWrite(INJ_OUT, HIGH);
    uint32_t w = tOff - rOnUs;
    if (w < rWMin) rWMin = w;
    if (w > rWMax) rWMax = w;
    rWSum += w;
    uint32_t fired = rFired + 1;
    rFired = fired;
    rNextOnUs += rPeriodUs;
    if (fired >= rTarget) {
      runState = ST_DONE;
    } else if (rSubCount > 0 && (fired % rSubCount) == 0) {
      rHaveLastOn = false;   // 待ちをまたぐ周期は統計に入れない
      runState = ST_PAUSED;
    } else {
      runState = ST_OFF;
    }
  }
}

//-----------------------------------------------------------------------------
bool isArmed() {
  return digitalRead(ENGOFF_IN) == LOW;   // キルスイッチOFF(導通)=LOW
}

uint32_t currentPeriodUs() {
  if (cfgPeriodFixed) return cfgPeriodMs * 1000UL;
  return cfgPulseUs + cfgIntervalMs * 1000UL;
}

void printCfg() {
  Serial.print(F("#INJ,CFG,pw_us="));  Serial.print(cfgPulseUs);
  Serial.print(F(",n="));              Serial.print(cfgCount);
  Serial.print(F(",period_us="));      Serial.print(currentPeriodUs());
  Serial.print(F(",mode="));           Serial.print(cfgPeriodFixed ? 'T' : 'I');
  Serial.print(F(",sub="));            Serial.print(cfgSubCount);
  Serial.print(F(",pause_ms="));       Serial.print(cfgPauseMs);
  Serial.print(F(",armed="));          Serial.println(isArmed() ? 1 : 0);
}

void printErr(const __FlashStringHelper *msg) {
  Serial.print(F("#INJ,ERR,msg="));
  Serial.println(msg);
}

void printHelp() {
  Serial.println(F("---- 現在の設定 ----"));
  Serial.print(F("パルス幅[us] : ")); Serial.println(cfgPulseUs);
  Serial.print(F("総発数       : ")); Serial.println(cfgCount);
  Serial.print(F("周期[us]     : ")); Serial.print(currentPeriodUs());
  Serial.println(cfgPeriodFixed ? F(" (T: 固定周期)") : F(" (I: OFF区間指定)"));
  Serial.print(F("分割発数     : ")); Serial.println(cfgSubCount);
  Serial.print(F("分割待ち[ms] : ")); Serial.print(cfgPauseMs);
  Serial.println(cfgPauseMs == 0 ? F(" (C コマンド待ち)") : F(""));
  Serial.println(F("設定: U<us> W<ms> N<発数> T<ms> I<ms> B<発数> H<ms>  例: U4000 N750 T50"));
  Serial.println(F("実行: G=開始 / Q<発数>=捨て打ち / C=続行 / S=停止 / ?=表示"));
  Serial.println(F("開始にはキルスイッチOFF(導通)が必要。スタータボタンでも開始できる。"));
  printCfg();
  cfgPrinted = true;
}

//-----------------------------------------------------------------------------
// バースト開始。count発を現在の設定で打つ。prime=true のときは分割しない。
//-----------------------------------------------------------------------------
bool startBurst(uint32_t count, bool prime) {
  if (runState != ST_IDLE) {
    printErr(F("busy"));
    return false;
  }
  if (!isArmed()) {
    printErr(F("not_armed(kill_switch)"));
    return false;
  }
  uint32_t period = currentPeriodUs();
  if (period < cfgPulseUs + OFF_US_MIN) {
    printErr(F("period_too_short"));
    return false;
  }
  if (count == 0) {
    printErr(F("count_zero"));
    return false;
  }

  runId++;
  runIsPrime = prime;

  noInterrupts();
  rPulseUs   = cfgPulseUs;
  rPeriodUs  = period;
  rTarget    = count;
  rSubCount  = prime ? 0 : cfgSubCount;
  rFired     = 0;
  rWMin      = 0xFFFFFFFFUL;
  rWMax      = 0;
  rWSum      = 0;
  rPerMin    = 0xFFFFFFFFUL;
  rPerMax    = 0;
  rHaveLastOn = false;
  killCount  = 0;
  stopRequest = false;
  partialShot = false;
  abortReason = AB_NONE;
  rNextOnUs  = micros() + 2000UL;   // 2ms後に1発目
  runState   = ST_OFF;
  interrupts();

  runStartMs    = millis();
  lastProgMs    = runStartMs;
  pauseTotalMs  = 0;
  pauseReported = false;

  Serial.print(F("#INJ,START,run="));  Serial.print(runId);
  Serial.print(F(",kind="));           Serial.print(prime ? F("prime") : F("burst"));
  Serial.print(F(",pw_us="));          Serial.print(cfgPulseUs);
  Serial.print(F(",n="));              Serial.print(count);
  Serial.print(F(",period_us="));      Serial.print(period);
  Serial.print(F(",sub="));            Serial.println(prime ? 0 : cfgSubCount);
  return true;
}

void resumeBurst() {
  if (runState != ST_PAUSED) {
    printErr(F("not_paused"));
    return;
  }
  if (!isArmed()) {
    // キルスイッチが戻されていたら続行せず中断扱いで終了
    abortReason = AB_KILL;
    runState = ST_DONE;
    return;
  }
  pauseTotalMs += millis() - pauseStartMs;
  pauseReported = false;
  Serial.print(F("#INJ,RESUME,run=")); Serial.print(runId);
  Serial.print(F(",fired="));          Serial.println(rFired);
  noInterrupts();
  killCount = 0;
  rNextOnUs = micros() + 2000UL;
  runState  = ST_OFF;
  interrupts();
}

void finishBurst() {
  // runState == ST_DONE のときだけ呼ぶ。ISRはDONEでは何も触らないので、そのまま読める。
  fastestdigitalWrite(INJ_OUT, HIGH);
  uint32_t fired = rFired;
  uint32_t elapsed = millis() - runStartMs;
  uint32_t wMin = (fired > 0) ? rWMin : 0;
  uint32_t wMax = (fired > 0) ? rWMax : 0;
  float    wMean = (fired > 0) ? (float)((double)rWSum / (double)fired) : 0.0f;
  bool     havePer = (rPerMax > 0);

  Serial.print(F("#INJ,DONE,run="));   Serial.print(runId);
  Serial.print(F(",kind="));           Serial.print(runIsPrime ? F("prime") : F("burst"));
  Serial.print(F(",pw_us="));          Serial.print(rPulseUs);
  Serial.print(F(",n_cmd="));          Serial.print(rTarget);
  Serial.print(F(",n_fired="));        Serial.print(fired);
  Serial.print(F(",period_us="));      Serial.print(rPeriodUs);
  Serial.print(F(",w_min_us="));       Serial.print(wMin);
  Serial.print(F(",w_mean_us="));      Serial.print(wMean, 2);
  Serial.print(F(",w_max_us="));       Serial.print(wMax);
  Serial.print(F(",per_min_us="));     Serial.print(havePer ? rPerMin : 0);
  Serial.print(F(",per_max_us="));     Serial.print(havePer ? rPerMax : 0);
  Serial.print(F(",elapsed_ms="));     Serial.print(elapsed);
  Serial.print(F(",pause_ms_total=")); Serial.print(pauseTotalMs);
  Serial.print(F(",abort="));
  switch (abortReason) {
    case AB_KILL: Serial.print(F("kill")); break;
    case AB_STOP: Serial.print(F("stop")); break;
    default:      Serial.print(F("none")); break;
  }
  Serial.print(F(",partial="));        Serial.println(partialShot ? 1 : 0);

  if (abortReason == AB_KILL) Serial.println(F("!! キルスイッチ復帰のため中断 !!"));
  if (abortReason == AB_STOP) Serial.println(F("!! 停止コマンドのため中断 !!"));
  if (!runIsPrime) {
    Serial.print(F("=== 完了: "));
    Serial.print(rPulseUs); Serial.print(F(" us x "));
    Serial.print(fired);    Serial.println(F(" 発。重量を記録してください ==="));
  }
  runState = ST_IDLE;
}

//-----------------------------------------------------------------------------
// 1トークン（例 "U4000"）を処理する
//-----------------------------------------------------------------------------
void handleToken(const String &tok) {
  if (tok.length() == 0) return;
  char cmd = toupper(tok.charAt(0));
  String arg = tok.substring(1);
  bool busy = (runState != ST_IDLE);

  // 実行系（引数なし）
  if (tok.length() == 1) {
    switch (cmd) {
      case 'G': startBurst(cfgCount, false); return;
      case 'C': resumeBurst(); return;
      case 'S':
        if (runState == ST_PAUSED) {
          abortReason = AB_STOP;
          runState = ST_DONE;
        } else if (busy) {
          stopRequest = true;
        }
        return;
      case '?': printHelp(); return;
      default:
        printErr(F("unknown_command"));
        return;
    }
  }

  // 設定系は実行中には変更しない
  if (busy) {
    printErr(F("busy"));
    return;
  }

  float fval = arg.toFloat();
  long  ival = arg.toInt();
  switch (cmd) {
    case 'U':
      if (ival < (long)PW_US_MIN || ival > (long)PW_US_MAX) { printErr(F("pw_out_of_range")); return; }
      cfgPulseUs = (uint32_t)ival;
      break;
    case 'W': {
      long us = lroundf(fval * 1000.0f);
      if (us < (long)PW_US_MIN || us > (long)PW_US_MAX) { printErr(F("pw_out_of_range")); return; }
      cfgPulseUs = (uint32_t)us;
      break;
    }
    case 'N':
      if (ival < 1 || ival > (long)N_MAX) { printErr(F("n_out_of_range")); return; }
      cfgCount = (uint32_t)ival;
      break;
    case 'T':
      if (ival < 3 || ival > 10000) { printErr(F("period_out_of_range")); return; }
      cfgPeriodMs = (uint32_t)ival;
      cfgPeriodFixed = true;
      break;
    case 'I':
      if (ival < 2 || ival > 10000) { printErr(F("interval_out_of_range")); return; }
      cfgIntervalMs = (uint32_t)ival;
      cfgPeriodFixed = false;
      break;
    case 'B':
      if (ival < 0 || ival > (long)N_MAX) { printErr(F("sub_out_of_range")); return; }
      cfgSubCount = (uint32_t)ival;
      break;
    case 'H':
      if (ival < 0 || ival > 600000) { printErr(F("pause_out_of_range")); return; }
      cfgPauseMs = (uint32_t)ival;
      break;
    case 'Q':
      if (ival < 1 || ival > 1000) { printErr(F("prime_out_of_range")); return; }
      startBurst((uint32_t)ival, true);
      return;
    default:
      printErr(F("unknown_command"));
      return;
  }
}

void handleLine(String &line) {
  line.trim();
  if (line.length() == 0) return;
  bool wasIdle = (runState == ST_IDLE);
  cfgPrinted = false;
  int start = 0;
  while (start < (int)line.length()) {
    int sp = line.indexOf(' ', start);
    if (sp < 0) sp = line.length();
    if (sp > start) handleToken(line.substring(start, sp));
    start = sp + 1;
  }
  // 設定だけの行なら、反映後の設定を1行返す（PC側はこれで受理を確認する）
  if (wasIdle && runState == ST_IDLE && !cfgPrinted) printCfg();
}

void handleSerial() {
  while (Serial.available()) {
    char c = (char)Serial.read();
    if (c == '\r') continue;
    if (c == '\n') {
      handleLine(rxLine);
      rxLine = "";
    } else if (rxLine.length() < 80) {
      rxLine += c;
    }
  }
}

void setup() {
  Serial.begin(115200);

  digitalWrite(INJ_OUT, HIGH);
  digitalWrite(IGN_OUT, HIGH);
  digitalWrite(STR_OUT, HIGH);
  digitalWrite(DISRESET_OUT, HIGH);

  pinMode(STR_IN, INPUT_PULLUP);
  pinMode(ENGOFF_IN, INPUT_PULLUP);
  pinMode(INJ_OUT, OUTPUT);
  pinMode(IGN_OUT, OUTPUT);
  pinMode(STR_OUT, OUTPUT);
  pinMode(DISRESET_OUT, OUTPUT);

  digitalWrite(INJ_OUT, HIGH);
  digitalWrite(IGN_OUT, HIGH);
  digitalWrite(STR_OUT, HIGH);
  digitalWrite(DISRESET_OUT, HIGH);

  rxLine.reserve(96);

  AGTimer.init(ROUTINE_CYCLE_US, Routine);
  AGTimer.start();

  // USBシリアルはPCがポートを開くまで送信が捨てられるので、起動表示が消えないよう接続を待つ。
  // PCがつながっていなくても SERIAL_WAIT_MS で先へ進む（試験の開始は ? で設定を読み直せばよい）。
  uint32_t t0 = millis();
  while (!Serial && millis() - t0 < SERIAL_WAIT_MS) {}
  delay(100);
  Serial.println(F("=== インジェクタ特性試験モード（第2版） ==="));
  printHelp();
}

void loop() {
  handleSerial();

  RunState st = runState;

  uint32_t nowMs = millis();

  // 従来どおり、キルスイッチOFF(導通) + スタータボタン押下(導通)でも開始できる。
  // 押した瞬間のチャタリングで一瞬HIGHになっただけでは「離した」と見なさない
  // （見なすと、押したままでもバースト終了のたびに再開始してしまう）。
  bool starter = (digitalRead(STR_IN) == LOW);
  if (starter) {
    starterLastLowMs = nowMs;
  } else if (nowMs - starterLastLowMs >= STARTER_RELEASE_MS) {
    starterReleased = true;
  }
  if (st == ST_IDLE && starter && starterReleased && isArmed()) {
    starterReleased = false;   // 離されるまで再トリガしない
    startBurst(cfgCount, false);
    st = runState;
    nowMs = millis();          // startBurst が lastProgMs を更新するので、それより前の時刻を使わない
  }

  if (st == ST_OFF || st == ST_ON) {
    if (!runIsPrime && nowMs - lastProgMs >= PROG_INTERVAL_MS) {
      lastProgMs = nowMs;
      Serial.print(F("#INJ,PROG,run=")); Serial.print(runId);
      Serial.print(F(",fired="));        Serial.println(rFired);
    }
  } else if (st == ST_PAUSED) {
    if (!pauseReported) {
      pauseReported = true;
      pauseStartMs = nowMs;
      Serial.print(F("#INJ,PAUSE,run=")); Serial.print(runId);
      Serial.print(F(",fired="));         Serial.println(rFired);
      if (cfgPauseMs == 0) Serial.println(F("再加圧後、C で続行 / S で中止"));
    } else if (!isArmed()) {
      // 待ち中にキルスイッチが戻されたら中断
      abortReason = AB_KILL;
      runState = ST_DONE;
    } else if (cfgPauseMs > 0 && nowMs - pauseStartMs >= cfgPauseMs) {
      resumeBurst();
    }
  } else if (st == ST_DONE) {
    if (pauseReported) {
      // 待ち中にSまたはキルスイッチで終了した場合、最後の待ち時間も合計に入れる
      pauseTotalMs += nowMs - pauseStartMs;
      pauseReported = false;
    }
    finishBurst();
  }
}
