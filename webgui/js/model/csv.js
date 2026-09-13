// CSVの読み書き。microSD/RPM_*.CSV と互換。
//
// 読み取り規則は tools/send_map.py / src/map_store.cpp と揃える:
//   空行・"#"/";" 始まり・数字で始まらない行（ヘッダ）は無視する。

export const CSV_HEADER = 'RPM,  INJ(0.1msec), IGN(CA)';

export function parseCsv(text) {
  const rows = [];
  const warnings = [];
  const lines = text.replace(/^﻿/, '').split(/\r?\n/);   // BOM除去
  lines.forEach((raw, i) => {
    const line = raw.trim();
    if (!line || line[0] === '#' || line[0] === ';') return;
    if (!/^\d/.test(line)) return;   // ヘッダ行
    const parts = line.split(',').map((s) => s.trim());
    if (parts.length < 3) {
      warnings.push(`${i + 1}行目: 列が足りません: ${line}`);
      return;
    }
    const v = parts.slice(0, 3).map(Number);
    if (v.some((n) => Number.isNaN(n))) {
      warnings.push(`${i + 1}行目: 数値ではありません: ${line}`);
      return;
    }
    rows.push({ rpm: v[0], inj: v[1], ign: v[2] });
  });
  if (!rows.length) warnings.push('有効な行がありません');
  return { rows, warnings };
}

export function formatCsv(rows) {
  return [CSV_HEADER, ...rows.map((r) => `${r.rpm},${r.inj},${r.ign}`)].join('\n') + '\n';
}

/**
 * ファイルとして保存する。
 * 発行元がページ自身なので、artifact 等の閲覧環境ではブロックされうる。
 * ローカルサーバ / GitHub Pages の通常のブラウザでは動作する。
 */
export function download(filename, text, mime = 'text/csv') {
  const blob = new Blob([text], { type: `${mime};charset=utf-8` });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

/** テレメトリのログをCSVにする。単位は人が読める形へ換算しておく。 */
export function telemetryCsv(samples) {
  const head = 'seq,ms,rpm,inj_ms,ign_ca,speed_kmh,ne_deg,map_row,eng_on,out_of_range';
  const body = samples.map((s) => [
    s.seq, s.ms, s.rpm, (s.inj01 / 10).toFixed(1), s.ign,
    (s.spd01 / 10).toFixed(1), s.ne, s.row,
    (s.flags & 0x01) ? 1 : 0,
    (s.flags & 0x08) ? 1 : 0,
  ].join(','));
  return [head, ...body].join('\n') + '\n';
}

export function parseTelemetryCsv(text) {
  const out = [];
  const lines = text.replace(/^﻿/, '').split(/\r?\n/);
  for (const raw of lines) {
    const line = raw.trim();
    if (!line || !/^\d/.test(line)) continue;
    const p = line.split(',').map(Number);
    if (p.length < 8 || p.some((n) => Number.isNaN(n))) continue;
    out.push({
      seq: p[0], ms: p[1], rpm: p[2], inj01: Math.round(p[3] * 10), ign: p[4],
      spd01: Math.round(p[5] * 10), ne: p[6], row: p[7],
      flags: (p[8] ? 0x01 : 0) | (p[9] ? 0x08 : 0),
      legacy: false,
    });
  }
  return out;
}
