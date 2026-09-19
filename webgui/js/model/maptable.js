// MAPテーブルの検証・CRC・編集操作。
//
// 行は { rpm, inj, ign, end } で、inj はファームと同じ x0.1ms の整数のまま保持する
// （表示のときだけ /10 する）。丸め誤差の混入源を1箇所に閉じ込めるため。
// end は噴射終了角度(inj_end_ca、CA)。
//
// 検証規則は src/map_store.cpp の validateTable() と一致させること。

export const MAP_MAX_ENTRIES = 24;
export const MAP_RPM_MAX = 20000;
export const MAP_INJ_MAX = 255;
export const MAP_IGN_CA_MAX = 90;
export const MAP_INJ_END_CA_MAX = 720;

// src/main.cpp のレブリミット。MAP最終行がこれを下回ると、
// レブリミットより手前で噴射・点火が止まる（mapOutOfRange）。
export const TACHO_RPM_MAX = 6000;

// クランク2回転。end（噴射終了角）はこの周期で巡る角度なので、素の引き算で
// 比較すると 680CA と 20CA が「660離れている」ことになってしまう（実際は60）。
export const CYCLE_CA = 720;

// 前行からの噴射終了角の跳びを警告する閾値。同梱MAP（既定 / RPM.CSV /
// RPM_2026SUZUKA.CSV）の実測最大が周回距離で60CAなので、1.5倍の余裕を取る。
export const END_JUMP_WARN_CA = 90;

export const ERROR_TEXT = {
  NO_ROWS: '行がありません',
  TOO_MANY_ROWS: `行数が上限(${MAP_MAX_ENTRIES})を超えています`,
  RPM_NOT_ASCENDING: 'RPMが厳密昇順になっていません',
  RPM_OUT_OF_RANGE: `RPMが範囲外です(1〜${MAP_RPM_MAX})`,
  IGN_CA_OUT_OF_RANGE: `点火進角が範囲外です(0〜${MAP_IGN_CA_MAX} CA)`,
  INJ_OUT_OF_RANGE: `噴射時間が範囲外です(0〜${MAP_INJ_MAX} = 0〜25.5ms)`,
  INJ_END_CA_OUT_OF_RANGE: `噴射終了角度が範囲外です(0〜${MAP_INJ_END_CA_MAX} CA)`,
};

/**
 * 列ごとのメタ知識を1箇所に集める。
 *
 * 以前は同じ知識が chart.js の `field === 'inj' ? A : B`、table.js の列見出しと
 * `(r.inj/10).toFixed(1)`、app.js の LIVE_MAX_* に散っていて、列を1本増やすたびに
 * 3ファイルを直す必要があった。
 *
 *   min/max        検証とクランプの範囲（src/map_store.h と一致させること）
 *   toDisplay      内部値 → 表示値。injだけ x0.1ms → ms
 *   fromDisplay    表示値 → 内部値（toDisplayの逆）
 *   digits         表示の小数桁
 *   cyclic         周回する角度なら周期[CA]。補間・平滑化・跳び判定がこれを見る
 *   liveMaxStep    ライブ適用1回で許す変化量（内部値）
 *   chart          グラフの見出し・目盛数・ガイド線
 */
export const FIELD_META = {
  rpm: {
    header: 'RPM', short: 'RPM', unit: 'rpm', min: 1, max: MAP_RPM_MAX,
    toDisplay: (v) => v, fromDisplay: (v) => Math.round(v), digits: 0,
  },
  inj: {
    header: '噴射 ms', short: '噴射', unit: 'ms', min: 0, max: MAP_INJ_MAX,
    toDisplay: (v) => v / 10, fromDisplay: (v) => Math.round(v * 10), digits: 1,
    stepLabel: '+0.1ms', errorCode: 'INJ_OUT_OF_RANGE', liveMaxStep: 10,
    chart: { title: '噴射時間 (ms)', yTicks: 5 },
  },
  ign: {
    header: '進角 CA', short: '進角', unit: 'CA', min: 0, max: MAP_IGN_CA_MAX,
    toDisplay: (v) => v, fromDisplay: (v) => Math.round(v), digits: 0,
    stepLabel: '+1CA', errorCode: 'IGN_CA_OUT_OF_RANGE', liveMaxStep: 5,
    chart: { title: '点火進角 (CA)', yTicks: 6 },
  },
  end: {
    header: '噴射終了 CA', short: '終了', unit: 'CA', min: 0, max: MAP_INJ_END_CA_MAX,
    toDisplay: (v) => v, fromDisplay: (v) => Math.round(v), digits: 0,
    stepLabel: '+1CA', errorCode: 'INJ_END_CA_OUT_OF_RANGE', liveMaxStep: 30,
    cyclic: CYCLE_CA,
    chart: {
      title: '噴射終了角 (CA)',
      yTicks: 4,                                      // 0 / 180 / 360 / 540 / 720
      // ファームは Ne_deg>=360 で噴射中なら強制OFFする（src/main.cpp の360CA安全リセット）。
      // どこを跨ぐと噴射が切られるかが目で見えるように線を引く。
      guides: [{ v: 360, text: '360CAで噴射を強制終了' }],
    },
  },
};

/** 値を編集できる列。rpm は行の構造（ブレークポイント）を変えるので別扱い。 */
export const EDIT_FIELDS = ['inj', 'ign', 'end'];

export const clone = (rows) => rows.map((r) => ({ ...r }));

/**
 * b から a への差を -360..+360 に畳む。
 * 噴射終了角は720CAで巡るので、680CAと20CAはクランク角では60CAしか離れていない。
 */
export function angleDelta(a, b) {
  let d = (a - b) % CYCLE_CA;
  if (d > CYCLE_CA / 2) d -= CYCLE_CA;
  if (d < -CYCLE_CA / 2) d += CYCLE_CA;
  return d;
}

/** フィールドの差。周回する列なら近い方の回りで測る。 */
export const fieldDelta = (field, a, b) => (
  FIELD_META[field].cyclic ? angleDelta(a, b) : a - b);

/**
 * 行の噴射区間を、ファームと同じ式で角度に直す（src/main.cpp の cycleReset）。
 *
 *   tachoWidth は1回転(360CA)の周期[us] = 60000000 / rpm
 *   INJ_STR_CA = inj_end_ca - (inj_time * 100[us] * 360 / tachoWidth)
 *
 * 区間長は回転が速いほど角度として長くなるので、その行が受け持つ最大rpm
 * （ファームは rpm < row.rpm の間この行を使う）で最悪値を見る。
 */
export function injectionWindow(row) {
  const durationCa = (row.inj * 100 * 360 * row.rpm) / 60000000;
  let start = row.end - durationCa;
  if (start < 0) start += CYCLE_CA;     // ファームと同じ 0CA跨ぎの扱い
  return { start, durationCa, end: row.end };
}

/**
 * ライブ適用の変化量チェック。上限を超えた最初の (行, 列) を返す。
 *
 * 判定を app.js から model へ移したのは、安全ガードをテストで固定できるようにするため。
 * 周回する列は近い方の回りで測る（680→20 は 660 ではなく 60 の変更）。
 */
export function liveStepViolation(deviceRows, editRows, changedRows) {
  for (const i of changedRows) {
    for (const field of EDIT_FIELDS) {
      const delta = fieldDelta(field, editRows[i][field], deviceRows[i][field]);
      if (Math.abs(delta) > FIELD_META[field].liveMaxStep) return { index: i, field, delta };
    }
  }
  return null;
}

const inRange = (field, v) => {
  const m = FIELD_META[field];
  return v >= m.min && v <= m.max;
};

/** ファームと同じ規則で検証する。問題なければ null、あればエラー名を返す。 */
export function validate(rows) {
  if (!rows || rows.length === 0) return 'NO_ROWS';
  if (rows.length > MAP_MAX_ENTRIES) return 'TOO_MANY_ROWS';
  let prev = null;
  for (const r of rows) {
    // 判定順は rpm → inj → ign → end → 昇順。ファームの validateTable() と揃える
    if (!inRange('rpm', r.rpm)) return 'RPM_OUT_OF_RANGE';
    for (const f of EDIT_FIELDS) {
      if (!inRange(f, r[f])) return FIELD_META[f].errorCode;
    }
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
    if (!inRange('rpm', r.rpm)) bad[`${i}:rpm`] = ERROR_TEXT.RPM_OUT_OF_RANGE;
    else if (prev !== null && r.rpm <= prev) bad[`${i}:rpm`] = ERROR_TEXT.RPM_NOT_ASCENDING;
    for (const f of EDIT_FIELDS) {
      if (!inRange(f, r[f])) bad[`${i}:${f}`] = ERROR_TEXT[FIELD_META[f].errorCode];
    }
    prev = r.rpm;
  });
  return bad;
}

/**
 * MAPのCRC。MAP INFO の crc= と突き合わせて転送を検証する。
 *
 * CRC-16/CCITT-FALSE を MapEntry の生バイト列に対して計算する。
 * MapEntry は { uint16 rpm; uint8 inj; (padding 1); uint16 ign; uint16 end; }
 * の8バイトで、ファームは sizeof(MapEntry)*count バイトを対象にするため、
 * パディングの1バイト(常に0)も含めて詰める必要がある。
 */
export function crc16(rows) {
  const buf = new Uint8Array(rows.length * 8);
  const view = new DataView(buf.buffer);
  rows.forEach((r, i) => {
    view.setUint16(i * 8, r.rpm, true);      // little-endian
    view.setUint8(i * 8 + 2, r.inj);
    // i*8+3 はパディング（0のまま）
    view.setUint16(i * 8 + 4, r.ign, true);
    view.setUint16(i * 8 + 6, r.end, true);
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
      for (const f of EDIT_FIELDS) {
        if (d[f] === r[f]) continue;
        // 周回する列は近い方の回りで表示する。20→717 は +697 ではなく -23CA の変更で、
        // ライブ適用の変化量判定（liveStepViolation）とも表示が一致する。
        cells[`${i}:${f}`] = fieldDelta(f, r[f], d[f]);
        dirty = true;
      }
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
/**
 * 範囲外はクランプする。周回する列でも wrap はしない。
 * セルに 800 と打ち間違えたとき、wrap すると黙って 80CA（まったく別の噴射位相）に
 * なってしまう。720で止まれば赤いセルとして目に見えるし、ファームの
 * validateTable() も >720 を弾く側なのでそちらとも一致する。
 */
export const clampField = (field, v) => {
  const m = FIELD_META[field] || FIELD_META.rpm;   // 未知の列は従来どおり rpm 扱い
  return Math.max(m.min, Math.min(m.max, Math.round(v)));
};

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

/** 周回する列なら 0..周期 に畳む。クランプの前に通す。 */
const wrapField = (field, v) => {
  const c = FIELD_META[field].cyclic;
  return c ? ((v % c) + c) % c : v;
};

/**
 * 選択範囲の両端を固定して、間を線形補間する。
 *
 * 噴射終了角は周回するので、素の (y1-y0) で補間すると 680→20 の間を「中点350」で
 * 結んでしまう（クランク角では60CAしか離れていないので、正しくは710）。
 * 既定MAPは 1600rpm→2000rpm でまさにこの境界を跨ぐため、必ず踏む。
 */
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
    return { ...r, [field]: clampField(field, wrapField(field, y0 + fieldDelta(field, y1, y0) * t)) };
  });
}

/**
 * 選択範囲を3点移動平均で平滑化する（両端は据え置き）。
 *
 * (a + 2b + c)/4 は b + (a-b)/4 + (c-b)/4 と恒等なので、差分の形で書いておくと
 * 周回する列だけ近い方の回りで平均でき、inj/ign の結果は1ビットも変わらない。
 */
export function smooth(rows, indices, field) {
  const set = new Set(indices);
  const src = rows.map((r) => r[field]);
  return rows.map((r, i) => {
    if (!set.has(i) || i === 0 || i === rows.length - 1) return r;
    if (!set.has(i - 1) || !set.has(i + 1)) return r;
    const v = src[i]
      + (fieldDelta(field, src[i - 1], src[i]) + fieldDelta(field, src[i + 1], src[i])) / 4;
    return { ...r, [field]: clampField(field, wrapField(field, v)) };
  });
}

/** 行を追加する。RPMの昇順を保つ位置へ挿入する。 */
export function insertRow(rows, rpm, inj, ign, end) {
  if (rows.length >= MAP_MAX_ENTRIES) return rows;
  if (rows.some((r) => r.rpm === rpm)) return rows;
  const next = [...rows, {
    rpm: clampField('rpm', rpm),
    inj: clampField('inj', inj),
    ign: clampField('ign', ign),
    end: clampField('end', end),
  }];
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

    // 噴射終了角は inj と rpm を合わせて初めて実害が見える。
    // ファームは Ne_deg>=360 で噴射中なら強制OFFするので、そこを跨ぐ行は
    // MAPの指示より噴射が短くなる（＝リーン）。end 単独では絶対に見えない。
    const w = injectionWindow(r);
    if (w.durationCa >= CYCLE_CA) {
      out.push({
        level: 'error',
        row: i,
        text: `${r.rpm} rpm で噴射時間が1サイクル(${CYCLE_CA}CA)より長く、噴射が閉じません。`,
      });
    } else if (r.inj > 0 && w.start < 360 && w.start + w.durationCa > 360) {
      out.push({
        level: 'warn',
        row: i,
        text: `${r.rpm} rpm で噴射が360CAを跨ぎます`
          + `（開始 ${Math.round(w.start)}CA → 終了 ${r.end}CA）。`
          + 'ファームは360CAで噴射を強制終了するので、指示より短くなります。',
      });
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
    // 噴射終了角は720CAで周回する。既定MAPは 0→20→680 と跳ぶが、680CAと20CAは
    // クランク角では60CAしか離れていない。素の差で見ると全行が警告になるので、
    // 近い方の回りで測る。
    const dEnd = angleDelta(r.end, p.end);
    if (Math.abs(dEnd) > END_JUMP_WARN_CA) {
      out.push({
        level: 'warn',
        row: i,
        text: `${r.rpm} rpm で噴射終了角が前行から ${dEnd > 0 ? '+' : ''}${Math.round(dEnd)} CA `
          + '跳んでいます（720CA周回で比較）。回転数の段差で噴射位相が急変します。',
      });
    }
  });

  if (rows.length >= MAP_MAX_ENTRIES) {
    out.push({ level: 'warn', row: -1, text: `行数が上限 ${MAP_MAX_ENTRIES} です。これ以上行を追加できません。` });
  }
  return out;
}
