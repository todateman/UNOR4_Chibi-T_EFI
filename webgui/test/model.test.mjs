// node --test webgui/test/
//
// ブラウザ不要の純関数テスト。検証規則とCRCがファーム（src/map_store.cpp）および
// Python側（tools/map_protocol.py）と一致していることを固定する。

import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

import {
  crc16, validate, cellErrors, diff, lint, rowForRpm,
  trimRatio, trimDelta, interpolate, smooth, insertRow, removeRows,
  MAP_MAX_ENTRIES, TACHO_RPM_MAX,
} from '../js/model/maptable.js';
import { parseCsv, formatCsv, telemetryCsv, parseTelemetryCsv } from '../js/model/csv.js';
import { History } from '../js/model/history.js';
import { TelemetryRing, DwellTracker } from '../js/model/ring.js';
import {
  classifyLine, parseTelemetry, parseInfo, parseVersion, parseDump,
  rejectReason, timeoutFor, isRunning, KIND,
} from '../js/protocol/lines.js';

const REPO = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const readMap = (name) => parseCsv(readFileSync(join(REPO, 'microSD', name), 'utf8')).rows;

const DEFAULT_MAP = readMap('RPM_2026SUZUKA.CSV');

// -----------------------------------------------------------------------------
test('CRCが実機の既知値と一致する', () => {
  // 実機の MAP INFO が defaultMap（4列）に対して返す値
  assert.equal(crc16(DEFAULT_MAP), 0x7418);
});

test('CRCはMapEntryの8バイト（パディング込み）で計算される', () => {
  // 詰め方を誤ると別の値になる。順序を入れ替えれば当然変わる。
  assert.notEqual(crc16([{
    rpm: 400, inj: 40, ign: 15, end: 680,
  }]), crc16([{
    rpm: 400, inj: 15, ign: 40, end: 680,
  }]));
  assert.notEqual(crc16([{
    rpm: 400, inj: 40, ign: 15, end: 680,
  }]), crc16([{
    rpm: 400, inj: 40, ign: 15, end: 681,
  }]));
});

test('検証がファームと同じ判定を返す', () => {
  assert.equal(validate(DEFAULT_MAP), null);
  assert.equal(validate([]), 'NO_ROWS');
  assert.equal(validate([{
    rpm: 0, inj: 40, ign: 15, end: 680,
  }]), 'RPM_OUT_OF_RANGE');
  assert.equal(validate([{
    rpm: 20001, inj: 40, ign: 15, end: 680,
  }]), 'RPM_OUT_OF_RANGE');
  assert.equal(validate([{
    rpm: 400, inj: 256, ign: 15, end: 680,
  }]), 'INJ_OUT_OF_RANGE');
  assert.equal(validate([{
    rpm: 400, inj: 40, ign: 91, end: 680,
  }]), 'IGN_CA_OUT_OF_RANGE');
  assert.equal(validate([{
    rpm: 400, inj: 40, ign: 15, end: 721,
  }]), 'INJ_END_CA_OUT_OF_RANGE');
  assert.equal(validate([
    {
      rpm: 400, inj: 40, ign: 15, end: 680,
    },
    {
      rpm: 400, inj: 40, ign: 15, end: 680,
    },
  ]), 'RPM_NOT_ASCENDING');
  const many = Array.from({ length: 25 }, (_, i) => ({
    rpm: (i + 1) * 10, inj: 40, ign: 15, end: 680,
  }));
  assert.equal(validate(many), 'TOO_MANY_ROWS');
});

test('ign_ca>90 の既存3ファイルが弾かれる（3列CSVはparseCsvがエラーにするのでスキップ）', () => {
  for (const name of ['RPM_2025SUZUKA.CSV', 'RPM_2025MOTEGI.csv', 'RPM_2024MOTEGI.CSV']) {
    const { warnings } = parseCsv(readFileSync(join(REPO, 'microSD', name), 'utf8'));
    assert.ok(warnings.length > 0, `${name}: 3列CSVは4列目なしでエラーになるはず`);
  }
  for (const name of ['RPM.CSV', 'RPM_2026SUZUKA.CSV']) {
    assert.equal(validate(readMap(name)), null, name);
  }
});

test('cellErrors が違反セルを特定する', () => {
  const rows = [
    {
      rpm: 400, inj: 40, ign: 15, end: 680,
    },
    {
      rpm: 300, inj: 40, ign: 200, end: 680,
    },
  ];
  const bad = cellErrors(rows);
  assert.ok(bad['1:rpm']);
  assert.ok(bad['1:ign']);
  assert.equal(bad['0:rpm'], undefined);
});

// -----------------------------------------------------------------------------
test('rowForRpm がファームの階段状参照と一致する', () => {
  // 最初に rpm < e.rpm となる行を採用する
  assert.equal(rowForRpm(DEFAULT_MAP, 0), 0);        // < 400
  assert.equal(rowForRpm(DEFAULT_MAP, 399), 0);
  assert.equal(rowForRpm(DEFAULT_MAP, 400), 1);      // 400 は 400 未満ではない
  assert.equal(rowForRpm(DEFAULT_MAP, 5999), 14);
  assert.equal(rowForRpm(DEFAULT_MAP, 6000), -1);    // MAP範囲外＝噴射・点火停止
});

test('diff がブレークポイント一致を判定する', () => {
  const edit = DEFAULT_MAP.map((r, i) => (i === 4 ? { ...r, inj: r.inj + 2 } : { ...r }));
  const d = diff(DEFAULT_MAP, edit);
  assert.equal(d.breakpointsMatch, true);
  assert.deepEqual(d.changedRows, [4]);
  assert.equal(d.cells['4:inj'], 2);

  // RPMが変わると差分転送(MAP SET)は使えない
  const moved = DEFAULT_MAP.map((r, i) => (i === 4 ? { ...r, rpm: r.rpm + 1 } : { ...r }));
  assert.equal(diff(DEFAULT_MAP, moved).breakpointsMatch, false);
});

// -----------------------------------------------------------------------------
test('トリムがクランプされる', () => {
  const rows = [{
    rpm: 400, inj: 250, ign: 88, end: 700,
  }];
  assert.equal(trimRatio(rows, [0], 'inj', 0.5)[0].inj, 255);     // 上限255
  assert.equal(trimDelta(rows, [0], 'ign', 10)[0].ign, 90);       // 上限90
  assert.equal(trimDelta(rows, [0], 'inj', -300)[0].inj, 0);      // 下限0
  assert.equal(trimDelta(rows, [0], 'end', 100)[0].end, 720);     // 上限720
});

test('トリムは選択行だけを変える', () => {
  const rows = [
    {
      rpm: 400, inj: 40, ign: 15, end: 680,
    },
    {
      rpm: 800, inj: 40, ign: 15, end: 680,
    },
  ];
  const out = trimDelta(rows, [1], 'inj', 5);
  assert.equal(out[0].inj, 40);
  assert.equal(out[1].inj, 45);
});

test('補間が両端を固定して間を埋める', () => {
  const rows = [
    {
      rpm: 1000, inj: 10, ign: 0, end: 680,
    },
    {
      rpm: 2000, inj: 99, ign: 0, end: 680,
    },
    {
      rpm: 3000, inj: 30, ign: 0, end: 680,
    },
  ];
  const out = interpolate(rows, [0, 1, 2], 'inj');
  assert.equal(out[0].inj, 10);
  assert.equal(out[2].inj, 30);
  assert.equal(out[1].inj, 20);   // 中点
});

test('平滑化が選択範囲の内側だけを動かす', () => {
  const rows = [
    {
      rpm: 1000, inj: 40, ign: 0, end: 680,
    },
    {
      rpm: 2000, inj: 80, ign: 0, end: 680,
    },
    {
      rpm: 3000, inj: 40, ign: 0, end: 680,
    },
  ];
  const out = smooth(rows, [0, 1, 2], 'inj');
  assert.equal(out[0].inj, 40);
  assert.equal(out[2].inj, 40);
  assert.equal(out[1].inj, 60);   // (40 + 80*2 + 40)/4
});

test('行の追加と削除', () => {
  const added = insertRow(DEFAULT_MAP, 2200, 50, 22, 680);
  assert.equal(added.length, 16);
  assert.equal(added[5].rpm, 2200);
  assert.deepEqual(added.map((r) => r.rpm), [...added.map((r) => r.rpm)].sort((a, b) => a - b));

  // 重複RPMは追加しない
  assert.equal(insertRow(DEFAULT_MAP, 2000, 50, 22, 680).length, DEFAULT_MAP.length);

  // 上限を超えて追加しない
  const full = Array.from({ length: MAP_MAX_ENTRIES }, (_, i) => ({
    rpm: (i + 1) * 100, inj: 40, ign: 15, end: 680,
  }));
  assert.equal(insertRow(full, 9999, 40, 15, 680).length, MAP_MAX_ENTRIES);

  assert.equal(removeRows(DEFAULT_MAP, [0, 1]).length, 13);
  // 0行にはしない
  assert.equal(removeRows([{
    rpm: 400, inj: 40, ign: 15, end: 680,
  }], [0]).length, 1);
});

// -----------------------------------------------------------------------------
test('リンターがレブリミット手前の燃料カットを検出する', () => {
  const short = DEFAULT_MAP.slice(0, 10);   // 最終行 4000rpm < 6000rpm
  const issues = lint(short);
  const hit = issues.find((i) => i.text.includes('レブリミット'));
  assert.ok(hit, 'レブリミット手前の警告が出るはず');
  assert.equal(hit.level, 'error');

  // 最終行がレブリミット以上なら出ない
  assert.equal(lint(DEFAULT_MAP).filter((i) => i.text.includes('レブリミット')).length, 0);
  assert.equal(DEFAULT_MAP[DEFAULT_MAP.length - 1].rpm, TACHO_RPM_MAX);
});

test('リンターが中間行の燃料カットを検出する', () => {
  const rows = DEFAULT_MAP.map((r, i) => (i === 5 ? { ...r, inj: 0 } : { ...r }));
  assert.ok(lint(rows).some((i) => i.text.includes('燃料カット')));
});

test('リンターが跳びを警告する', () => {
  const rows = [
    {
      rpm: 1000, inj: 40, ign: 10, end: 680,
    },
    {
      rpm: 2000, inj: 100, ign: 30, end: 680,
    },
  ];
  const issues = lint(rows);
  assert.ok(issues.some((i) => i.text.includes('噴射時間が前行から')));
  assert.ok(issues.some((i) => i.text.includes('進角が前行から')));
});

// -----------------------------------------------------------------------------
test('CSVの往復で内容が保たれる', () => {
  const { rows, warnings } = parseCsv(readFileSync(join(REPO, 'microSD', 'RPM_2026SUZUKA.CSV'), 'utf8'));
  assert.equal(warnings.length, 0);
  assert.equal(rows.length, 15);
  assert.deepEqual(rows[0], {
    rpm: 400, inj: 0, ign: 0, end: 0,
  });
  assert.deepEqual(parseCsv(formatCsv(rows)).rows, rows);
});

test('3列（旧書式）CSVは4列目が無いのでエラーになる', () => {
  const { rows, warnings } = parseCsv('RPM, INJ, IGN\n400,40,15\n');
  assert.equal(rows.length, 0);
  assert.ok(warnings.length > 0);
});

test('CSVがBOM・コメント・ヘッダを無視する', () => {
  const { rows } = parseCsv('﻿RPM, INJ, IGN, INJ_END\n# comment\n;other\n\n400,40,15,680\n');
  assert.deepEqual(rows, [{
    rpm: 400, inj: 40, ign: 15, end: 680,
  }]);
});

test('テレメトリログCSVの往復', () => {
  const samples = [
    { seq: 1, ms: 100, rpm: 2480, inj01: 44, ign: 20, spd01: 183, ne: 213, row: 5, flags: 0x09 },
  ];
  const back = parseTelemetryCsv(telemetryCsv(samples));
  assert.equal(back.length, 1);
  assert.equal(back[0].rpm, 2480);
  assert.equal(back[0].inj01, 44);
  assert.equal(back[0].flags, 0x09);
});

// -----------------------------------------------------------------------------
test('行の分類', () => {
  assert.equal(classifyLine('T\t1\t2\t3\t4\t5\t6\t7\t8\t9'), KIND.TELEMETRY);
  assert.equal(classifyLine('1560\t4.0\t15\t0.0\t0\t0.0\t0.0\t12\t213'), KIND.TELEMETRY);
  assert.equal(classifyLine('OK APPLIED 15'), KIND.ACK_OK);
  assert.equal(classifyLine('ERR BAD_CSV_LINE'), KIND.ACK_ERR);
  assert.equal(classifyLine('INFO src=EEPROM rows=15'), KIND.BODY);
  assert.equal(classifyLine('400,  40, 15'), KIND.BODY);
  assert.equal(classifyLine('MAP SOURCE: EEPROM (15 rows)'), KIND.BODY);
});

test('テレメトリのパース（新書式）', () => {
  const s = parseTelemetry('T\t12\t34567\t2480\t44\t20\t183\t213\t5\t9');
  assert.deepEqual(
    { seq: s.seq, rpm: s.rpm, inj01: s.inj01, ign: s.ign, spd01: s.spd01, row: s.row },
    { seq: 12, rpm: 2480, inj01: 44, ign: 20, spd01: 183, row: 5 },
  );
  assert.equal(isRunning(s), true);
  assert.equal(s.flags & 0x08, 0x08);   // mapOutOfRange
});

test('row=255 は「MAPを参照していない」を意味する', () => {
  // 実機で見つけた不具合の固定。回転信号が1.2秒無いとファームは噴射・点火を止める。
  // そのとき row=255 が来るので、回転数から行を推測し直してはいけない
  // （rpm=0 から推測すると先頭行を指してしまい、止まっているのにハイライトが残る）。
  const s = parseTelemetry('T\t1\t100\t0\t0\t0\t0\t213\t255\t0');
  assert.equal(s.row, 255);
  assert.equal(s.legacy, false);
  // 旧書式は row を持たないので、そのときだけ推測にフォールバックしてよい
  const legacy = parseTelemetry('0\t0.0\t0\t0.0\t0\t0.0\t0.0\t0\t213');
  assert.equal(legacy.legacy, true);
  assert.equal(legacy.row, 255);
});

test('テレメトリのパース（旧2Hz書式）', () => {
  const s = parseTelemetry('1560\t4.0\t15\t18.4\t0\t0.0\t0.0\t12\t213');
  assert.equal(s.rpm, 1560);
  assert.equal(s.inj01, 40);
  assert.equal(s.spd01, 184);
  assert.equal(s.row, 255);     // 旧書式は行番号を持たない
  assert.equal(s.legacy, true);
});

test('壊れたテレメトリ行はnullになる', () => {
  assert.equal(parseTelemetry('T\t1\t2'), null);
  assert.equal(parseTelemetry('a\tb\tc\td\te\tf\tg\th\ti'), null);
});

test('INFO行のパース（CRCはゼロ埋め無しの大文字HEX）', () => {
  const i = parseInfo(['INFO src=EEPROM rows=15 crc=0x52F4 eeprom=VALID rpm=0 eng=OFF']);
  assert.equal(i.src, 'EEPROM');
  assert.equal(i.rows, 15);
  assert.equal(i.crc, 0x52f4);
  assert.equal(i.eng, 'OFF');
  // 桁数が短いケース
  assert.equal(parseInfo(['INFO crc=0xA3']).crc, 0xa3);
});

test('VER行のパース', () => {
  assert.deepEqual(parseVersion('OK VER 1.1.0 proto=2'), { fw: '1.1.0', proto: 2 });
  assert.equal(parseVersion('OK PONG').proto, 1);   // 旧ファーム扱い
});

test('MAP?の本文からCSV行を取り出す', () => {
  const rows = parseDump(['RPM,  INJ(0.1msec), IGN(CA), INJ_END(CA)', '400,40,15,680', '800,40,15,680']);
  assert.deepEqual(rows, [{
    rpm: 400, inj: 40, ign: 15, end: 680,
  }, {
    rpm: 800, inj: 40, ign: 15, end: 680,
  }]);
});

test('送ってはいけない行を弾く', () => {
  assert.ok(rejectReason(''));               // 空行は応答が返らない
  assert.ok(rejectReason('   '));
  assert.ok(rejectReason('X'.repeat(100)));  // 96バイト上限
  assert.ok(rejectReason('MAP SET 2000 40 20 680 　'));   // 非ASCII
  assert.equal(rejectReason('MAP INFO'), null);
});

test('データフラッシュ操作には長いタイムアウトを使う', () => {
  assert.equal(timeoutFor('MAP SAVE'), 10000);
  assert.equal(timeoutFor('map load'), 10000);
  assert.equal(timeoutFor('MAP SET 2000 40 20'), 2000);
});

// -----------------------------------------------------------------------------
test('Undo/Redoの往復', () => {
  const h = new History();
  const a = [{ rpm: 400, inj: 40, ign: 15 }];
  const b = [{ rpm: 400, inj: 50, ign: 15 }];
  assert.equal(h.canUndo, false);
  h.commit(a);
  assert.equal(h.canUndo, true);
  assert.deepEqual(h.undo(b), a);
  assert.equal(h.canRedo, true);
  assert.deepEqual(h.redo(a), b);
});

test('Undoスタックが上限を超えない', () => {
  const h = new History(3);
  for (let i = 0; i < 10; i += 1) h.commit([{ rpm: i + 1, inj: 40, ign: 15 }]);
  assert.equal(h.undoStack.length, 3);
});

// -----------------------------------------------------------------------------
test('リングバッファが古いサンプルを捨てる', () => {
  const ring = new TelemetryRing(4);
  for (let i = 1; i <= 6; i += 1) {
    ring.push({ seq: i, ms: i * 100, rpm: i * 10, inj01: 0, ign: 0, spd01: 0, ne: 0, row: 0, flags: 0 });
  }
  assert.equal(ring.n, 4);
  assert.equal(ring.at(0).seq, 3);          // 最も古い
  assert.equal(ring.at(3).seq, 6);          // 最新
  assert.deepEqual(ring.tail(2).map((s) => s.seq), [5, 6]);
});

test('滞在時間が取りこぼし区間を加算しない', () => {
  const d = new DwellTracker();
  const s = (seq, ms, row) => ({ seq, ms, rpm: 0, inj01: 0, ign: 0, spd01: 0, ne: 0, row, flags: 0 });
  d.add(s(1, 0, 2));
  d.add(s(2, 100, 2));      // +100ms
  assert.equal(d.dwell[2], 100);

  d.add(s(5, 400, 2));      // seqが飛んでいる → 加算しない
  assert.equal(d.dwell[2], 100);

  d.add(s(6, 500, 2));      // 連続に戻った → +100ms
  assert.equal(d.dwell[2], 200);

  d.add(s(7, 5000, 2));     // 間隔が大きすぎる（ミュート区間） → 加算しない
  assert.equal(d.dwell[2], 200);

  d.add(s(8, 5100, 255));   // MAP未使用 → 加算しない
  assert.equal(d.dwell.reduce((a, b) => a + b, 0), 200);
});
