//-----------------------------------------------------------------------------
// map_store.cpp : エンジンMAPの保持と永続化（実装）
//-----------------------------------------------------------------------------
#include "map_store.h"
#include <EEPROM.h>

//-----------------------------------------------------------------------------
// 内蔵デフォルトMAP（EEPROM未初期化・破損時のフォールバック）
//-----------------------------------------------------------------------------
const MapEntry defaultMap[] = {
  {400,  0,  0,  0},    // 400RPM以下はアイドリング不能なのでエンジン停止
  {800,  80, 0,  20},   // 800RPM以下は始動状態
  {1200, 80, 0,  20},   // 1200RPM以下は始動状態
  {1600, 40, 15, 680},  // 以降は通常走行域
  {2000, 44, 20, 680},
  {2400, 44, 20, 680},
  {2800, 44, 25, 680},
  {3200, 42, 25, 680},
  {3600, 40, 25, 680},
  {4000, 40, 30, 680},
  {4400, 40, 30, 680},
  {4800, 40, 30, 680},
  {5200, 40, 30, 680},
  {5600, 40, 30, 680},
  {6000, 40, 30, 680}
};
const uint8_t defaultMapSize = sizeof(defaultMap) / sizeof(defaultMap[0]);

//-----------------------------------------------------------------------------
// ランタイム状態
//-----------------------------------------------------------------------------
MapTable         mapBank[2];        // ダブルバンク（読み手はISR）
volatile uint8_t mapActiveBank = 0; // 1バイトのvolatileストア＝Cortex-M4でアトミック

static MapTable  mapStaging;        // シリアル転送の受け皿
static MapSource mapSource = MAP_SRC_DEFAULT;

//-----------------------------------------------------------------------------
// EEPROM格納レイアウト（先頭1消去ブロック=1KB内に収める）
//-----------------------------------------------------------------------------
struct MapStore {
  uint32_t magic;
  uint8_t  version;
  uint8_t  count;
  uint16_t crc;                       // entries[0..count) に対するCRC
  MapEntry entries[MAP_MAX_ENTRIES];
};

//-----------------------------------------------------------------------------
// CRC-16/CCITT-FALSE
//-----------------------------------------------------------------------------
static uint16_t mapCrc16(const MapEntry* e, uint8_t count) {
  uint16_t crc = 0xFFFF;
  const uint8_t* p = (const uint8_t*)e;
  const uint16_t len = (uint16_t)count * sizeof(MapEntry);
  for (uint16_t i = 0; i < len; i++) {
    crc ^= (uint16_t)p[i] << 8;
    for (uint8_t b = 0; b < 8; b++) {
      crc = (crc & 0x8000) ? (uint16_t)((crc << 1) ^ 0x1021) : (uint16_t)(crc << 1);
    }
  }
  return crc;
}

//-----------------------------------------------------------------------------
// 内部ヘルパ
//-----------------------------------------------------------------------------
// 非アクティブバンクへの参照
static inline MapTable& inactiveBank() {
  return mapBank[mapActiveBank ^ 1];
}

// 非アクティブバンクをアクティブへ昇格（ここだけが切替点）
static inline void commitBank() {
  mapActiveBank = mapActiveBank ^ 1;
}

// テーブルの内容を検証
static MapValidation validateTable(const MapEntry* e, uint8_t count) {
  if (count == 0)               return MAP_ERR_EMPTY;
  if (count > MAP_MAX_ENTRIES)  return MAP_ERR_TOO_MANY;
  for (uint8_t i = 0; i < count; i++) {
    if (e[i].rpm == 0 || e[i].rpm > MAP_RPM_MAX)       return MAP_ERR_RPM_RANGE;
    if (e[i].ign_ca > MAP_IGN_CA_MAX)                  return MAP_ERR_IGN_RANGE;
    if (e[i].inj_end_ca > MAP_INJ_END_CA_MAX)          return MAP_ERR_INJ_END_CA_RANGE;
    if (i > 0 && e[i].rpm <= e[i - 1].rpm)             return MAP_ERR_RPM_ORDER;
  }
  return MAP_OK;
}

//-----------------------------------------------------------------------------
// 初期化・参照
//-----------------------------------------------------------------------------
void mapStoreInit() {
  mapActiveBank = 0;
  if (!mapLoadFromEEPROM()) {
    mapApplyDefault();
  }
  mapStaging.count = 0;
}

MapSource mapGetSource() {
  return mapSource;
}

uint16_t mapGetActiveCrc() {
  const MapTable& t = mapGetActive();
  return mapCrc16(t.e, t.count);
}

//-----------------------------------------------------------------------------
// 書き換え
//-----------------------------------------------------------------------------
void mapStagingClear() {
  mapStaging.count = 0;
}

bool mapStagingAppend(uint16_t rpm, uint8_t inj_time, uint16_t ign_ca, uint16_t inj_end_ca) {
  if (mapStaging.count >= MAP_MAX_ENTRIES) return false;
  mapStaging.e[mapStaging.count].rpm         = rpm;
  mapStaging.e[mapStaging.count].inj_time    = inj_time;
  mapStaging.e[mapStaging.count].ign_ca      = ign_ca;
  mapStaging.e[mapStaging.count].inj_end_ca  = inj_end_ca;
  mapStaging.count++;
  return true;
}

uint8_t mapStagingCount() {
  return mapStaging.count;
}

MapValidation mapStagingValidate() {
  return validateTable(mapStaging.e, mapStaging.count);
}

void mapStagingApply() {
  MapTable& dst = inactiveBank();
  dst.count = mapStaging.count;
  memcpy(dst.e, mapStaging.e, (size_t)mapStaging.count * sizeof(MapEntry));
  commitBank();
  mapSource = MAP_SRC_SERIAL;
}

void mapApplyDefault() {
  MapTable& dst = inactiveBank();
  dst.count = defaultMapSize;
  memcpy(dst.e, defaultMap, (size_t)defaultMapSize * sizeof(MapEntry));
  commitBank();
  mapSource = MAP_SRC_DEFAULT;
}

bool mapSetEntry(uint16_t rpm, uint8_t inj_time, uint16_t ign_ca, uint16_t inj_end_ca) {
  if (rpm == 0 || rpm > MAP_RPM_MAX)          return false;
  if (ign_ca > MAP_IGN_CA_MAX)                return false;
  if (inj_end_ca > MAP_INJ_END_CA_MAX)        return false;

  const MapTable& src = mapGetActive();
  MapTable&       dst = inactiveBank();

  // 挿入位置を探しつつコピー（RPM昇順を維持）
  uint8_t i = 0;
  while (i < src.count && src.e[i].rpm < rpm) {
    dst.e[i] = src.e[i];
    i++;
  }

  if (i < src.count && src.e[i].rpm == rpm) {
    // 既存行を更新
    dst.count = src.count;
    dst.e[i].rpm         = rpm;
    dst.e[i].inj_time    = inj_time;
    dst.e[i].ign_ca      = ign_ca;
    dst.e[i].inj_end_ca  = inj_end_ca;
    for (uint8_t j = i + 1; j < src.count; j++) dst.e[j] = src.e[j];
  } else {
    // 新規行として挿入
    if (src.count >= MAP_MAX_ENTRIES) return false;
    dst.count = src.count + 1;
    dst.e[i].rpm         = rpm;
    dst.e[i].inj_time    = inj_time;
    dst.e[i].ign_ca      = ign_ca;
    dst.e[i].inj_end_ca  = inj_end_ca;
    for (uint8_t j = i; j < src.count; j++) dst.e[j + 1] = src.e[j];
  }

  commitBank();
  mapSource = MAP_SRC_SERIAL;
  return true;
}

//-----------------------------------------------------------------------------
// EEPROM入出力
//-----------------------------------------------------------------------------
// EEPROMを読み出して検証まで行う。有効ならtrueとともにstoreを埋める。
static bool readValidStore(MapStore& store) {
  EEPROM.get(MAP_EEPROM_ADDR, store);
  if (store.magic != MAP_MAGIC)          return false;
  if (store.version != MAP_VERSION)      return false;
  if (validateTable(store.entries, store.count) != MAP_OK) return false;
  if (store.crc != mapCrc16(store.entries, store.count))   return false;
  return true;
}

// 保存済みstoreの内容がテーブルsrc（CRCはcrc）と一致するか。
// CRCは16bitで衝突しうるため、エントリ本体まで突き合わせる。
static bool storeMatches(const MapStore& store, const MapTable& src, uint16_t crc) {
  return store.count == src.count &&
         store.crc   == crc &&
         memcmp(store.entries, src.e, (size_t)src.count * sizeof(MapEntry)) == 0;
}

bool mapEepromValid() {
  MapStore store;
  return readValidStore(store);
}

bool mapLoadFromEEPROM() {
  MapStore store;
  if (!readValidStore(store)) return false;

  MapTable& dst = inactiveBank();
  dst.count = store.count;
  memcpy(dst.e, store.entries, (size_t)store.count * sizeof(MapEntry));
  commitBank();
  mapSource = MAP_SRC_EEPROM;
  return true;
}

MapSaveResult mapSaveToEEPROM() {
  const MapTable& src = mapGetActive();
  if (validateTable(src.e, src.count) != MAP_OK) return MAP_SAVE_FAILED;

  const uint16_t crc = mapCrc16(src.e, src.count);

  // 既に同じ内容が保存されているならデータフラッシュの摩耗を避けるため書き込まない。
  // readValidStore() は magic/version/検証/CRC を全て見るので、未初期化・破損・
  // バージョン違いの場合はここを素通りして通常の書き込み経路へ進む。
  {
    MapStore current;
    if (readValidStore(current) && storeMatches(current, src, crc)) {
      mapSource = MAP_SRC_EEPROM;
      return MAP_SAVE_UNCHANGED;
    }
  }

  {
    MapStore store;
    memset(&store, 0, sizeof(store));
    store.magic   = MAP_MAGIC;
    store.version = MAP_VERSION;
    store.count   = src.count;
    store.crc     = crc;
    memcpy(store.entries, src.e, (size_t)src.count * sizeof(MapEntry));

    EEPROM.put(MAP_EEPROM_ADDR, store);
  }

  // 書き戻しを読み返して検証
  MapStore verify;
  if (!readValidStore(verify))                return MAP_SAVE_FAILED;
  if (!storeMatches(verify, src, crc))        return MAP_SAVE_FAILED;

  mapSource = MAP_SRC_EEPROM;
  return MAP_SAVE_WRITTEN;
}

//-----------------------------------------------------------------------------
// CSV行パース
//-----------------------------------------------------------------------------
bool mapParseCsvLine(const char* line, uint16_t& rpm, uint8_t& inj_time,
                     uint16_t& ign_ca, uint16_t& inj_end_ca, bool& skip) {
  skip = false;

  // 先頭の空白を飛ばす
  const char* p = line;
  while (*p == ' ' || *p == '\t') p++;

  // 空行・コメント行はスキップ
  if (*p == '\0' || *p == '#' || *p == ';') { skip = true; return true; }

  // ヘッダ行（"RPM,..." で始まる行）だけをスキップする。
  // それ以外の非数値行は書式エラーとして弾き、転送の取りこぼしを黙認しない。
  if (toupper((unsigned char)p[0]) == 'R' &&
      toupper((unsigned char)p[1]) == 'P' &&
      toupper((unsigned char)p[2]) == 'M') {
    skip = true;
    return true;
  }
  if (!isdigit((unsigned char)*p)) return false;

  long v[4];
  for (uint8_t col = 0; col < 4; col++) {
    while (*p == ' ' || *p == '\t') p++;
    if (!isdigit((unsigned char)*p)) return false;  // 桁が足りない/数値でない

    long val = 0;
    while (isdigit((unsigned char)*p)) {
      val = val * 10 + (*p - '0');
      if (val > 999999L) return false;  // 桁あふれ防止
      p++;
    }
    v[col] = val;

    while (*p == ' ' || *p == '\t') p++;
    if (col < 3) {
      if (*p != ',') return false;      // 区切り文字が無い
      p++;
    }
  }

  // 末尾に余分な非空白があれば書式エラー
  while (*p == ' ' || *p == '\t' || *p == ',') p++;
  if (*p != '\0') return false;

  if (v[0] > 65535L || v[1] > 255L || v[2] > 65535L || v[3] > 65535L) return false;

  rpm        = (uint16_t)v[0];
  inj_time   = (uint8_t)v[1];
  ign_ca     = (uint16_t)v[2];
  inj_end_ca = (uint16_t)v[3];
  return true;
}

const char* mapValidationText(MapValidation v) {
  switch (v) {
    case MAP_OK:                    return "OK";
    case MAP_ERR_EMPTY:             return "NO_ROWS";
    case MAP_ERR_TOO_MANY:          return "TOO_MANY_ROWS";
    case MAP_ERR_RPM_ORDER:         return "RPM_NOT_ASCENDING";
    case MAP_ERR_RPM_RANGE:         return "RPM_OUT_OF_RANGE";
    case MAP_ERR_IGN_RANGE:         return "IGN_CA_OUT_OF_RANGE";
    case MAP_ERR_INJ_END_CA_RANGE:  return "INJ_END_CA_OUT_OF_RANGE";
    default:                        return "UNKNOWN";
  }
}
