//-----------------------------------------------------------------------------
// map_store.h : エンジンMAP（燃料噴射時間・点火進角）の保持と永続化
//
// - MAPはRAM上のダブルバンクで保持し、書き換えはバンク切替で原子的に行う。
//   読み手は AGTimer 割り込み（Routine → cycleReset → updateEngineMap）なので、
//   ロックを取らずに一貫したテーブルを参照できるようにするためのもの。
// - 永続化先は UNO R4 (RA4M1) のデータフラッシュ 8KB（Arduino EEPROMライブラリ）。
//   消去単位が1KBのため、MapStore全体を先頭1ブロック内に収めている。
//-----------------------------------------------------------------------------
#ifndef MAP_STORE_H
#define MAP_STORE_H

#include <Arduino.h>

#define MAP_MAX_ENTRIES  24        // MAP最大行数
#define MAP_EEPROM_ADDR  0         // EEPROM上の格納オフセット
#define MAP_MAGIC        0x3150414DUL  // "MAP1"
#define MAP_VERSION      1

// MAP検証レンジ
#define MAP_RPM_MAX      20000
#define MAP_IGN_CA_MAX   90

// MAP 1行分（RPMしきい値・燃料噴射時間・点火進角）
struct MapEntry {
  uint16_t rpm;       // RPMしきい値（この値未満なら当該行を採用）
  uint8_t  inj_time;  // 燃料噴射時間（x0.1ms）
  uint16_t ign_ca;    // 点火進角角度（CA）
};

// MAPテーブル1面分
struct MapTable {
  uint8_t  count;
  MapEntry e[MAP_MAX_ENTRIES];
};

// MAPの出所（MAP INFO表示用）
enum MapSource : uint8_t {
  MAP_SRC_DEFAULT = 0,  // 内蔵defaultMap
  MAP_SRC_EEPROM  = 1,  // EEPROMから読み込み
  MAP_SRC_SERIAL  = 2   // シリアル転送で書き換え（未保存）
};

// 検証結果
enum MapValidation : uint8_t {
  MAP_OK = 0,
  MAP_ERR_EMPTY,        // 0行
  MAP_ERR_TOO_MANY,     // MAP_MAX_ENTRIES超過
  MAP_ERR_RPM_ORDER,    // RPMが厳密昇順でない
  MAP_ERR_RPM_RANGE,    // RPMが範囲外
  MAP_ERR_IGN_RANGE     // 点火進角が範囲外
};

// 内蔵デフォルトMAP（EEPROM破損時のフォールバック）
extern const MapEntry defaultMap[];
extern const uint8_t  defaultMapSize;

//-----------------------------------------------------------------------------
// 初期化・参照
//-----------------------------------------------------------------------------
// setup()から呼ぶ。EEPROMが有効ならそれを、無効なら内蔵defaultMapを採用する。
void mapStoreInit();

// 現在アクティブなMAPテーブルを返す（割り込みコンテキストから呼んで良い）。
// 返る参照は呼び出し時点のバンクを指し、切替中に破壊されることはない。
static inline const MapTable& mapGetActive();

// 現在のMAPの出所
MapSource mapGetSource();

// 現在のMAPのCRC（MAP INFO表示用）
uint16_t mapGetActiveCrc();

//-----------------------------------------------------------------------------
// 書き換え（ステージング → 検証 → 原子的反映）
//-----------------------------------------------------------------------------
// ステージングバッファをクリアして転送セッションを開始
void mapStagingClear();

// ステージングバッファに1行追加。満杯ならfalse。
bool mapStagingAppend(uint16_t rpm, uint8_t inj_time, uint16_t ign_ca);

// ステージングバッファの現在行数
uint8_t mapStagingCount();

// ステージングバッファを検証。MAP_OK以外なら反映してはいけない。
MapValidation mapStagingValidate();

// ステージングバッファを非アクティブバンクへ展開し、バンクを切り替える。
// 事前に mapStagingValidate() == MAP_OK であること。
void mapStagingApply();

// 内蔵defaultMapをRAMへ反映（EEPROMは変更しない）
void mapApplyDefault();

// 1行だけライブ変更。rpmが既存行に一致すればその行を更新、
// 一致しなければ昇順を保つ位置へ挿入する。falseなら行数超過または範囲外。
bool mapSetEntry(uint16_t rpm, uint8_t inj_time, uint16_t ign_ca);

//-----------------------------------------------------------------------------
// EEPROM入出力
//-----------------------------------------------------------------------------
// EEPROMに有効なMAPが保存されているか
bool mapEepromValid();

// EEPROMから読み直してRAMへ反映。EEPROMが無効ならfalse（RAMは無傷）。
bool mapLoadFromEEPROM();

// 現在のRAM MAPをEEPROMへ保存。
// 注意: 数msのブロッキングが発生するため、エンジン停止時のみ呼ぶこと。
bool mapSaveToEEPROM();

//-----------------------------------------------------------------------------
// CSV行パース（シリアル転送とSD読み込みで共用）
//-----------------------------------------------------------------------------
// "rpm,inj,ign" 形式の1行をパースする。
// 空行・コメント行(# ;)・ヘッダ行("RPM"で始まる行)は skip=true で返り、
// 値は書き込まれない。それ以外で数値として解釈できない場合は false を返す。
bool mapParseCsvLine(const char* line, uint16_t& rpm, uint8_t& inj_time,
                     uint16_t& ign_ca, bool& skip);

// 検証エラーコードを人が読める文字列にする
const char* mapValidationText(MapValidation v);

//-----------------------------------------------------------------------------
// inline実装（ISRから呼ばれるため定義をヘッダに置く）
//-----------------------------------------------------------------------------
extern MapTable         mapBank[2];
extern volatile uint8_t mapActiveBank;

static inline const MapTable& mapGetActive() {
  return mapBank[mapActiveBank];
}

#endif  // MAP_STORE_H
