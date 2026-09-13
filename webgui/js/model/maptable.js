// MAPテーブルの検証・CRC・編集操作。
//
// 行は { rpm, inj, ign } で、inj はファームと同じ x0.1ms の整数のまま保持する
// （表示のときだけ /10 する）。丸め誤差の混入源を1箇所に閉じ込めるため。
//
// 検証規則は src/map_store.cpp の validateTable() と一致させること。

export const MAP_MAX_ENTRIES = 24;
export const MAP_RPM_MAX = 20000;
export const MAP_INJ_MAX = 255;
export const MAP_IGN_CA_MAX = 90;

// src/main.cpp のレブリミット。MAP最終行がこれを下回ると、
// レブリミットより手前で噴射・点火が止まる（mapOutOfRange）。
export const TACHO_RPM_MAX = 6000;

export const ERROR_TEXT = {
  NO_ROWS: '行がありません',
  TOO_MANY_ROWS: `行数が上限(${MAP_MAX_ENTRIES})を超えています`,
  RPM_NOT_ASCENDING: 'RPMが厳密昇順になっていません',
  RPM_OUT_OF_RANGE: `RPMが範囲外です(1〜${MAP_RPM_MAX})`,
  IGN_CA_OUT_OF_RANGE: `点火進角が範囲外です(0〜${MAP_IGN_CA_MAX} CA)`,
  INJ_OUT_OF_RANGE: `噴射時間が範囲外です(0〜${MAP_INJ_MAX} = 0〜25.5ms)`,
};

export const clone = (rows) => rows.map((r) => ({ ...r }));

/** ファームと同じ規則で検証する。問題なければ null、あればエラー名を返す。 */
export function validate(rows) {
  if (!rows || rows.length === 0) return 'NO_ROWS';
  if (rows.length > MAP_MAX_ENTRIES) return 'TOO_MANY_ROWS';
  let prev = null;
  for (const r of rows) {
    if (!(r.rpm >= 1 && r.rpm <= MAP_RPM_MAX)) return 'RPM_OUT_OF_RANGE';
    if (!(r.inj >= 0 && r.inj <= MAP_INJ_MAX)) return 'INJ_OUT_OF_RANGE';
    if (!(r.ign >= 0 && r.ign <= MAP_IGN_CA_MAX)) return 'IGN_CA_OUT_OF_RANGE';
    if (prev !== null && r.rpm <= prev) return 'RPM_NOT_ASCENDING';
    prev = r.rpm;
  }
  return null;
}

/** 違反しているセルを { '<index>:<field>': 理由 } で返す（赤表示用）。 */
export function cellErrors(rows) {
  const bad = {};
  let prev = null;
  rows.forEach((r, i) => {
    if (!(r.rpm >= 1 && r.rpm <= MAP_RPM_MAX)) bad[`${i}:rpm`] = ERROR_TEXT.RPM_OUT_OF_RANGE;
    else if (prev !== null && r.rpm <= prev) bad[`${i}:rpm`] = ERROR_TEXT.RPM_NOT_ASCENDING;
    if (!(r.inj >= 0 && r.inj <= MAP_INJ_MAX)) bad[`${i}:inj`] = ERROR_TEXT.INJ_OUT_OF_RANGE;
    if (!(r.ign >= 0 && r.ign <= MAP_IGN_CA_MAX)) bad[`${i}:ign`] = ERROR_TEXT.IGN_CA_OUT_OF_RANGE;
    prev = r.rpm;
  });
  return bad;
}

/**
 * MAPのCRC。MAP INFO の crc= と突き合わせて転送を検証する。
 *
 * CRC-16/CCITT-FALSE を MapEntry の生バイト列に対して計算する。
 * MapEntry は { uint16 rpm; uint8 inj; (padding 1); uint16 ign; } の6バイトで、
 * ファームは sizeof(MapEntry)*count バイトを対象にするため、
 * パディングの1バイト(常に0)も含めて詰める必要がある。
 */
export function crc16(rows) {
  const buf = new Uint8Array(rows.length * 6);
  const view = new DataView(buf.buffer);
  rows.forEach((r, i) => {
    view.setUint16(i * 6, r.rpm, true);      // little-endian
    view.setUint8(i * 6 + 2, r.inj);
    // i*6+3 はパディング（0のまま）
    view.setUint16(i * 6 + 4, r.ign, true);
  });
  let crc = 0xffff;
  for (const byte of buf) {
    crc ^= byte << 8;
    for (let b = 0; b < 8; b += 1) {
      crc = (crc & 0x8000) ? ((crc << 1) ^ 0x1021) & 0xffff : (crc << 1) & 0xffff;
    }
  }
  return crc;
}

export const hex4 = (n) => `0x${n.toString(16).toUpperCase().padStart(4, '0')}`;

/** 現在のRPMに対してファームが採用する行（最初に rpm < e.rpm となる行）。 */
export function rowForRpm(rows, rpm) {
  for (let i = 0; i < rows.length; i += 1) if (rpm < rows[i].rpm) return i;
  return -1;   // MAP範囲外（噴射・点火が止まる）
}

// -----------------------------------------------------------------------------
// 差分
// -----------------------------------------------------------------------------
/**
 * 実機MAPと編集中MAPの差分。
 * breakpointsMatch が true なら MAP SET による差分転送ができる
 * （セッション不要＝テレメトリが途切れず、エンジンを止めなくてよい）。
 */
export function diff(deviceRows, editRows) {
  const cells = {};
  const changedRows = [];
  const breakpointsMatch = deviceRows.length === editRows.length
    && deviceRows.every((r, i) => r.rpm === editRows[i].rpm);

  if (breakpointsMatch) {
    editRows.forEach((r, i) => {
      const d = deviceRows[i];
      let dirty = false;
      if (d.inj !== r.inj) { cells[`${i}:inj`] = r.inj - d.inj; dirty = true; }
      if (d.ign !== r.ign) { cells[`${i}:ign`] = r.ign - d.ign; dirty = true; }
      if (dirty) changedRows.push(i);
    });
  }
  return {
    breakpointsMatch,
    cells,
    changedRows,
    count: breakpointsMatch ? changedRows.length : Math.max(deviceRows.length, editRows.length),
  };
}

// -----------------------------------------------------------------------------
// 編集操作（すべて新しい配列を返す。呼び出し側で history に積む）
// -----------------------------------------------------------------------------
const clampInj = (v) => Math.max(0, Math.min(MAP_INJ_MAX, Math.round(v)));
const clampIgn = (v) => Math.max(0, Math.min(MAP_IGN_CA_MAX, Math.round(v)));
const clampRpm = (v) => Math.max(1, Math.min(MAP_RPM_MAX, Math.round(v)));

export const clampField = (field, v) => (
  field === 'inj' ? clampInj(v) : field === 'ign' ? clampIgn(v) : clampRpm(v)
);

/** 選択行の値を割合で増減する（例: +5% なら ratio=0.05）。 */
export function trimRatio(rows, indices, field, ratio) {
  const set = new Set(indices);
  return rows.map((r, i) => (set.has(i)
    ? { ...r, [field]: clampField(field, r[field] * (1 + ratio)) }
    : r));
}

/** 選択行の値を絶対量で増減する（例: 噴射 +1 = +0.1ms、進角 +1 = +1CA）。 */
export function trimDelta(rows, indices, field, delta) {
  const set = new Set(indices);
  return rows.map((r, i) => (set.has(i)
    ? { ...r, [field]: clampField(field, r[field] + delta) }
    : r));
}

/** 選択範囲の両端を固定して、間を線形補間する。 */
export function interpolate(rows, indices, field) {
  const idx = [...indices].sort((a, b) => a - b);
  if (idx.length < 3) return rows;
  const first = idx[0];
  const last = idx[idx.length - 1];
  const x0 = rows[first].rpm;
  const x1 = rows[last].rpm;
  if (x1 === x0) return rows;
  const y0 = rows[first][field];
  const y1 = rows[last][field];
  return rows.map((r, i) => {
    if (i <= first || i >= last || !indices.includes(i)) return r;
    const t = (r.rpm - x0) / (x1 - x0);
    return { ...r, [field]: clampField(field, y0 + (y1 - y0) * t) };
  });
}

/** 選択範囲を3点移動平均で平滑化する（両端は据え置き）。 */
export function smooth(rows, indices, field) {
  const set = new Set(indices);
  const src = rows.map((r) => r[field]);
  return rows.map((r, i) => {
    if (!set.has(i) || i === 0 || i === rows.length - 1) return r;
    if (!set.has(i - 1) || !set.has(i + 1)) return r;
    const v = (src[i - 1] + src[i] * 2 + src[i + 1]) / 4;
    return { ...r, [field]: clampField(field, v) };
  });
}

/** 行を追加する。RPMの昇順を保つ位置へ挿入する。 */
export function insertRow(rows, rpm, inj, ign) {
  if (rows.length >= MAP_MAX_ENTRIES) return rows;
  if (rows.some((r) => r.rpm === rpm)) return rows;
  const next = [...rows, { rpm: clampRpm(rpm), inj: clampInj(inj), ign: clampIgn(ign) }];
  next.sort((a, b) => a.rpm - b.rpm);
  return next;
}

export function removeRows(rows, indices) {
  const set = new Set(indices);
  const next = rows.filter((_, i) => !set.has(i));
  return next.length ? next : rows;   // 0行にはしない
}

// -----------------------------------------------------------------------------
// リンター（ファームの検証は構造だけ。こちらは「もっともらしさ」を見る）
// -----------------------------------------------------------------------------
export function lint(rows) {
  const out = [];
  if (!rows.length) return out;

  const last = rows[rows.length - 1];
  if (last.rpm < TACHO_RPM_MAX) {
    out.push({
      level: 'error',
      row: rows.length - 1,
      text: `MAP最終行が ${last.rpm} rpm で、レブリミット ${TACHO_RPM_MAX} rpm より手前です。`
        + 'この回転数を超えると噴射・点火が止まります。',
    });
  }

  rows.forEach((r, i) => {
    if (r.inj === 0 && i < rows.length - 1) {
      out.push({ level: 'error', row: i, text: `${r.rpm} rpm の噴射時間が 0 です（燃料カット）。` });
    }
    if (i === 0) return;
    const p = rows[i - 1];
    if (p.inj > 0 && Math.abs(r.inj - p.inj) / p.inj > 0.3) {
      out.push({
        level: 'warn',
        row: i,
        text: `${r.rpm} rpm で噴射時間が前行から ${((r.inj - p.inj) / p.inj * 100).toFixed(0)}% 跳んでいます。`,
      });
    }
    if (Math.abs(r.ign - p.ign) > 8) {
      out.push({
        level: 'warn',
        row: i,
        text: `${r.rpm} rpm で進角が前行から ${r.ign - p.ign > 0 ? '+' : ''}${r.ign - p.ign} CA 跳んでいます。`,
      });
    }
  });

  if (rows.length >= MAP_MAX_ENTRIES) {
    out.push({ level: 'warn', row: -1, text: `行数が上限 ${MAP_MAX_ENTRIES} です。これ以上行を追加できません。` });
  }
  return out;
}
