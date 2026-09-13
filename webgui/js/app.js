// MAP調整 Web GUI 本体。
//
// 状態は1箇所（state）に集め、UIは render() で再描画する。フレームワークは使わない。
// 安全ガードは safety() に集約し、各所で個別に if を書かない（漏れが致命的になる）。

import { WebSerialTransport } from './transport/webserial.js';
import { HttpBridgeTransport } from './transport/httpbridge.js';
import {
  parseInfo, parseVersion, parseDump, isRunning, FLAG,
} from './protocol/lines.js';
import * as M from './model/maptable.js';
import * as CSV from './model/csv.js';
import { History } from './model/history.js';
import { TelemetryRing, DwellTracker } from './model/ring.js';
import { MapChart } from './ui/chart.js';
import { MapTableView } from './ui/table.js';

const $ = (sel) => document.querySelector(sel);

// ライブ適用の安全装置。1操作あたりの変化量の上限と、自動解除までの無操作時間。
const LIVE_ARM_MS = 60000;
const LIVE_MAX_INJ_STEP = 10;   // x0.1ms = 1.0ms
const LIVE_MAX_IGN_STEP = 5;    // CA

const state = {
  transport: null,
  connected: false,
  portName: '',
  version: { fw: 'unknown', proto: 1 },
  info: {},

  deviceRows: [],       // 最後に実機から読み出したMAP
  rows: [],             // 編集中
  selection: new Set(),
  history: new History(),

  live: null,           // 最新のテレメトリ
  ring: new TelemetryRing(18000),
  dwell: new DwellTracker(),
  recording: false,
  recorded: [],
  replay: null,         // { samples, index } 再生中

  liveApply: false,
  liveArmedUntil: 0,
  busy: false,
  log: [],
};

let chartInj = null;
let chartIgn = null;
let table = null;

// -----------------------------------------------------------------------------
// ログ表示
// -----------------------------------------------------------------------------
function say(text, level = 'info') {
  state.log.unshift({ text, level, at: new Date() });
  if (state.log.length > 200) state.log.pop();
  renderLog();
}

function renderLog() {
  $('#log').innerHTML = state.log.slice(0, 60).map((l) => {
    const t = l.at.toTimeString().slice(0, 8);
    return `<div class="line ${l.level}"><span class="t">${t}</span>${escapeHtml(l.text)}</div>`;
  }).join('');
}

const escapeHtml = (s) => String(s).replace(/[&<>"]/g, (c) => (
  { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

// -----------------------------------------------------------------------------
// 安全ガード（判定をここに集約する）
// -----------------------------------------------------------------------------
function safety() {
  const t = state.live;
  const running = isRunning(t);
  const engStopped = !running && (state.info.eng === 'OFF' || !state.info.eng);
  const validationError = M.validate(state.rows);
  const d = M.diff(state.deviceRows, state.rows);
  return {
    running,
    // MAP SAVE はファームも ENG_ON || tachoRpm!=0 で拒否するが、押せること自体を無くす
    canSave: state.connected && !state.busy && engStopped,
    // 全転送はセッション中テレメトリが止まる。稼働中は確認を挟む
    canUpload: state.connected && !state.busy && !validationError,
    // RPMブレークポイントが一致していれば MAP SET の差分転送が使える
    canPushDelta: state.connected && !state.busy && !validationError
      && d.breakpointsMatch && d.changedRows.length > 0,
    // 稼働中は行が増えると危険なのでRPM列を固定する（MAP SET は行を挿入する）
    rpmLocked: running,
    validationError,
    diff: d,
  };
}

// -----------------------------------------------------------------------------
// 接続
// -----------------------------------------------------------------------------
function attachTransport(tp) {
  state.transport = tp;
  tp.onTelemetry((s) => onTelemetry(s));
  tp.onStatus((s) => {
    state.connected = !!s.connected;
    state.portName = s.port || '';
    if (!s.connected) state.live = null;
    render();
  });
  tp.onNotice((text) => say(`受信: ${text}`, 'dim'));
}

async function connectWebSerial() {
  const tp = new WebSerialTransport();
  attachTransport(tp);
  await tp.connect();
  await afterConnect();
}

async function connectBridge(opts) {
  const tp = new HttpBridgeTransport();
  attachTransport(tp);
  await tp.connect(opts);
  await afterConnect();
}

async function afterConnect() {
  state.connected = true;
  state.portName = state.transport.name;
  try {
    const ver = await state.transport.command('VER');
    state.version = parseVersion(ver.ack);
  } catch {
    state.version = { fw: 'unknown', proto: 1 };
  }
  say(`接続しました: ${state.portName} (fw=${state.version.fw} proto=${state.version.proto})`, 'ok');

  if (state.version.proto >= 2) {
    try {
      const r = await state.transport.command('TELEM ON 100');
      say(`テレメトリ開始: ${r.code}`, 'dim');
    } catch (e) { say(`テレメトリを開始できません: ${e.message}`, 'warn'); }
  } else {
    say('このファームは機械可読テレメトリ(TELEM)に未対応です。'
      + '2Hzの旧形式で表示します（行番号と状態フラグは取得できません）。', 'warn');
  }

  await readFromDevice();
}

async function disconnect() {
  try { await state.transport?.disconnect(); } catch { /* noop */ }
  state.connected = false;
  state.live = null;
  render();
  say('切断しました');
}

// -----------------------------------------------------------------------------
// テレメトリ
// -----------------------------------------------------------------------------
let lastSeq = -1;
function onTelemetry(s) {
  if (state.replay) return;    // 再生中はライブを表示しない
  if (lastSeq >= 0 && s.seq !== 0 && s.seq !== ((lastSeq + 1) & 0xffff)) {
    // 取りこぼしはdwell側でも除外される。ここでは黙って進める
  }
  lastSeq = s.seq;
  state.live = s;
  state.ring.push(s);
  state.dwell.add(s);
  if (state.recording) state.recorded.push(s);
  scheduleRender();
}

// -----------------------------------------------------------------------------
// 実機とのやりとり
// -----------------------------------------------------------------------------
async function readFromDevice() {
  if (!state.connected) return;
  state.busy = true;
  render();
  try {
    const dump = await state.transport.command('MAP?');
    state.deviceRows = parseDump(dump.body);
    const inf = await state.transport.command('MAP INFO');
    state.info = parseInfo(inf.body);

    const localCrc = M.crc16(state.deviceRows);
    if (state.info.crc !== undefined && state.info.crc !== localCrc) {
      say(`実機のCRC(${M.hex4(state.info.crc)})と読み出した内容(${M.hex4(localCrc)})が一致しません。`
        + '通信を確認してください。', 'error');
    }

    if (!state.rows.length) {
      state.rows = M.clone(state.deviceRows);
      state.history.clear();
    }
    say(`実機から読み出しました: ${state.deviceRows.length} 行 `
      + `src=${state.info.src} crc=${M.hex4(localCrc)}`, 'ok');
  } catch (e) {
    say(`読み出しに失敗しました: ${e.message}`, 'error');
  } finally {
    state.busy = false;
    render();
  }
}

async function uploadFull() {
  const s = safety();
  if (s.validationError) {
    say(`転送できません: ${M.ERROR_TEXT[s.validationError] || s.validationError}`, 'error');
    return;
  }
  const issues = M.lint(state.rows).filter((i) => i.level === 'error');
  const warn = issues.length
    ? `\n\n警告:\n${issues.map((i) => `・${i.text}`).join('\n')}` : '';
  const runningNote = s.running
    ? '\n\nエンジンが回っています。転送中の約1秒間はテレメトリが止まります。' : '';
  if (!window.confirm(`${state.rows.length} 行を実機へ転送します。${runningNote}${warn}`)) return;

  state.busy = true;
  render();
  const tp = state.transport;
  try {
    await tp.command('MAP BEGIN');
    try {
      for (const r of state.rows) {
        // eslint-disable-next-line no-await-in-loop
        await tp.command(`${r.rpm},${r.inj},${r.ign}`);
      }
      const end = await tp.command('MAP END');
      say(`転送しました: ${end.code}`, 'ok');
    } catch (e) {
      try { await tp.command('MAP ABORT'); } catch { /* noop */ }
      throw e;
    }
    await verifyCrc();
  } catch (e) {
    say(`転送に失敗しました（実機のMAPは変更されていません）: ${e.message}`, 'error');
  } finally {
    state.busy = false;
    await readFromDevice();
  }
}

/** 変更行だけを MAP SET で送る。セッション不要なのでテレメトリが途切れない。 */
async function pushDelta() {
  const s = safety();
  if (!s.canPushDelta) return;
  state.busy = true;
  render();
  try {
    for (const i of s.diff.changedRows) {
      const r = state.rows[i];
      // eslint-disable-next-line no-await-in-loop
      await state.transport.command(`MAP SET ${r.rpm} ${r.inj} ${r.ign}`);
    }
    say(`${s.diff.changedRows.length} 行をライブ反映しました`, 'ok');
    await verifyCrc();
  } catch (e) {
    say(`ライブ反映に失敗しました: ${e.message}`, 'error');
  } finally {
    state.busy = false;
    await readFromDevice();
  }
}

/** 転送後の検証。全行読み戻すより速く、実機の内部表現と直接比較できる。 */
async function verifyCrc() {
  try {
    const inf = await state.transport.command('MAP INFO');
    const info = parseInfo(inf.body);
    const expect = M.crc16(state.rows);
    if (info.crc !== expect) {
      say(`CRC不一致: 期待 ${M.hex4(expect)} / 実機 ${M.hex4(info.crc)}`, 'error');
      return false;
    }
    say(`CRC検証OK: ${M.hex4(expect)}`, 'ok');
    return true;
  } catch (e) {
    say(`CRC検証に失敗しました: ${e.message}`, 'warn');
    return false;
  }
}

async function saveEeprom() {
  if (!safety().canSave) return;
  if (!window.confirm('現在のMAPをEEPROMへ保存します。エンジンが停止していることを確認してください。')) return;
  state.busy = true;
  render();
  // ベンチではノイズで tachoRpm が一瞬非ゼロになり ERR ENGINE_RUNNING が返ることがある
  for (let attempt = 1; attempt <= 3; attempt += 1) {
    try {
      // eslint-disable-next-line no-await-in-loop
      const r = await state.transport.command('MAP SAVE');
      say(r.code.includes('UNCHANGED')
        ? 'EEPROMの内容は同一のため書き込みませんでした'
        : 'EEPROMへ保存しました', 'ok');
      break;
    } catch (e) {
      if (!String(e.message).includes('ENGINE_RUNNING') || attempt === 3) {
        say(`EEPROM保存に失敗しました: ${e.message}`, 'error');
        break;
      }
      say(`ENGINE_RUNNING が返りました。再試行します (${attempt}/3)`, 'warn');
      // eslint-disable-next-line no-await-in-loop
      await new Promise((r) => setTimeout(r, 500));
    }
  }
  state.busy = false;
  await readFromDevice();
}

async function simpleCommand(line, label) {
  if (!state.connected || state.busy) return;
  if (!window.confirm(`${label} を実行します。よろしいですか？`)) return;
  state.busy = true;
  render();
  try {
    const r = await state.transport.command(line);
    say(`${label}: ${r.code}`, 'ok');
  } catch (e) {
    say(`${label} に失敗しました: ${e.message}`, 'error');
  } finally {
    state.busy = false;
    await readFromDevice();
  }
}

// -----------------------------------------------------------------------------
// 編集
// -----------------------------------------------------------------------------
function commitBefore() {
  state.history.commit(state.rows);
}

function applyRows(rows, { live = false } = {}) {
  state.rows = rows;
  render();
  if (live) maybeLiveApply();
}

/** ライブ適用モードが有効なら、変更行を即座に MAP SET で送る。 */
async function maybeLiveApply() {
  if (!state.liveApply || !state.connected || state.busy) return;
  if (Date.now() > state.liveArmedUntil) {
    state.liveApply = false;
    say('ライブ適用が60秒無操作で自動解除されました', 'warn');
    render();
    return;
  }
  state.liveArmedUntil = Date.now() + LIVE_ARM_MS;

  const d = M.diff(state.deviceRows, state.rows);
  if (!d.breakpointsMatch) {
    say('RPMブレークポイントが実機と違うため、ライブ適用できません（全転送が必要です）', 'warn');
    return;
  }
  for (const i of d.changedRows) {
    const r = state.rows[i];
    const dev = state.deviceRows[i];
    if (Math.abs(r.inj - dev.inj) > LIVE_MAX_INJ_STEP
        || Math.abs(r.ign - dev.ign) > LIVE_MAX_IGN_STEP) {
      say(`変化量が大きすぎます（噴射 ±${LIVE_MAX_INJ_STEP / 10}ms / 進角 ±${LIVE_MAX_IGN_STEP}CA まで）。`
        + '「変更を送信」から明示的に反映してください。', 'warn');
      return;
    }
  }
  await pushDelta();
}

function selectedIndices() {
  return state.selection.size
    ? [...state.selection].sort((a, b) => a - b)
    : state.rows.map((_, i) => i);
}

function editOp(fn) {
  commitBefore();
  applyRows(fn(state.rows, selectedIndices()), { live: true });
}

// -----------------------------------------------------------------------------
// ロギングと再生
// -----------------------------------------------------------------------------
function toggleRecording() {
  state.recording = !state.recording;
  if (state.recording) {
    state.recorded = [];
    say('記録を開始しました');
  } else {
    say(`記録を停止しました（${state.recorded.length} サンプル）`);
  }
  render();
}

function saveLog() {
  if (!state.recorded.length) { say('記録がありません', 'warn'); return; }
  const name = `telemetry_${new Date().toISOString().slice(0, 19).replace(/[:T]/g, '')}.csv`;
  CSV.download(name, CSV.telemetryCsv(state.recorded));
  say(`${name} を保存しました`, 'ok');
}

let replayTimer = null;
function startReplay(samples) {
  stopReplay();
  if (!samples.length) return;
  state.replay = { samples, index: 0 };
  state.dwell.reset();
  say(`再生中: ${samples.length} サンプル`);
  replayTimer = setInterval(() => {
    const r = state.replay;
    if (!r || r.index >= r.samples.length) { stopReplay(); return; }
    const s = r.samples[r.index];
    r.index += 1;
    state.live = s;
    state.dwell.add(s);
    scheduleRender();
  }, 50);   // 実時間の2倍速
  render();
}

function stopReplay() {
  if (replayTimer) { clearInterval(replayTimer); replayTimer = null; }
  if (state.replay) say('再生を停止しました');
  state.replay = null;
  render();
}

// -----------------------------------------------------------------------------
// 描画
// -----------------------------------------------------------------------------
let renderQueued = false;
function scheduleRender() {
  if (renderQueued) return;
  renderQueued = true;
  requestAnimationFrame(() => { renderQueued = false; render(); });
}

function render() {
  const s = safety();
  const t = state.live;
  const dwellNorm = state.dwell.normalized();
  const activeRow = t && t.row < M.MAP_MAX_ENTRIES ? t.row
    : (t ? M.rowForRpm(state.rows, t.rpm) : -1);

  // ヘッダ
  $('#conn-state').textContent = state.connected ? `接続中: ${state.portName}` : '未接続';
  $('#conn-state').className = state.connected ? 'pill ok' : 'pill';
  $('#fw-state').textContent = state.connected
    ? `fw=${state.version.fw} proto=${state.version.proto}` : '';
  $('#map-state').textContent = state.connected
    ? `src=${state.info.src ?? '-'} rows=${state.info.rows ?? '-'} `
      + `crc=${state.info.crc !== undefined ? M.hex4(state.info.crc) : '-'} `
      + `eeprom=${state.info.eeprom ?? '-'}`
    : '';

  // ライブバー
  const badge = $('#run-badge');
  badge.textContent = s.running ? 'エンジン稼働中' : '停止';
  badge.className = `badge ${s.running ? 'run' : 'stop'}`;
  $('#v-rpm').textContent = t ? t.rpm : '—';
  $('#v-inj').textContent = t ? (t.inj01 / 10).toFixed(1) : '—';
  $('#v-ign').textContent = t ? t.ign : '—';
  $('#v-spd').textContent = t ? (t.spd01 / 10).toFixed(1) : '—';
  $('#v-row').textContent = t ? (t.row === 255 ? 'MAP外' : `#${t.row + 1}`) : '—';
  $('#cut-badge').hidden = !(t && (t.flags & FLAG.OUT_OF_RANGE));

  // グラフ
  const chartState = {
    rows: state.rows,
    deviceRows: state.deviceRows,
    selection: state.selection,
    dwell: dwellNorm,
    liveRpm: t ? t.rpm : 0,
    activeRow,
    showDevice: $('#opt-device').checked,
    showDwell: $('#opt-dwell').checked,
  };
  chartInj.set(chartState);
  chartIgn.set(chartState);

  // テーブル
  table.set({
    rows: state.rows,
    deviceRows: state.deviceRows,
    diff: s.diff,
    selection: state.selection,
    dwell: dwellNorm,
    activeRow,
    rpmLocked: s.rpmLocked,
  });

  // ボタンの活性
  const dis = (id, off, title) => {
    const el = $(id);
    el.disabled = off;
    if (title) el.title = title;
  };
  dis('#btn-read', !state.connected || state.busy);
  dis('#btn-upload', !s.canUpload, s.validationError
    ? M.ERROR_TEXT[s.validationError] : '全行を MAP BEGIN/END で転送します');
  dis('#btn-push', !s.canPushDelta, s.diff.breakpointsMatch
    ? '変更行だけを MAP SET で送ります（テレメトリは止まりません）'
    : 'RPMが実機と違うため差分転送できません。全転送してください');
  dis('#btn-save', !s.canSave, s.canSave
    ? '現在のMAPをEEPROMへ保存します'
    : 'エンジン停止中(eng=OFF かつ rpm=0)のみ保存できます');
  dis('#btn-load', !state.connected || state.busy);
  dis('#btn-default', !state.connected || state.busy);
  dis('#btn-undo', !state.history.canUndo);
  dis('#btn-redo', !state.history.canRedo);
  dis('#btn-revert', !state.deviceRows.length);
  dis('#btn-savelog', !state.recorded.length);

  $('#btn-record').textContent = state.recording ? '■ 記録停止' : '● 記録開始';
  $('#rec-count').textContent = state.recorded.length
    ? `${state.recorded.length} サンプル` : '';
  $('#btn-live').className = state.liveApply ? 'toggle on' : 'toggle';
  $('#btn-live').textContent = state.liveApply ? '🔓 ライブ適用 有効' : '🔒 ライブ適用';
  $('#diff-count').textContent = s.diff.breakpointsMatch
    ? (s.diff.changedRows.length ? `差分 ${s.diff.changedRows.length} 行` : '差分なし')
    : 'RPM構成が実機と異なります';

  // 検証とリンター。MAPをまだ読み込んでいない状態では何も出さない。
  if (!state.rows.length) {
    $('#issues').innerHTML = '<div class="issue ok">MAPを読み込むと、検証結果とリンターの警告がここに出ます</div>';
  } else {
    const issues = M.lint(state.rows);
    const verr = s.validationError
      ? [{ level: 'error', text: `転送できません: ${M.ERROR_TEXT[s.validationError] || s.validationError}` }]
      : [];
    const all = [...verr, ...issues];
    $('#issues').innerHTML = all.length
      ? all.map((i) => `<div class="issue ${i.level}">${escapeHtml(i.text)}</div>`).join('')
      : '<div class="issue ok">問題は見つかりませんでした</div>';
  }
}

// -----------------------------------------------------------------------------
// 起動
// -----------------------------------------------------------------------------
function wire() {
  chartInj = new MapChart($('#chart-inj'), 'inj', chartHandlers('inj'));
  chartIgn = new MapChart($('#chart-ign'), 'ign', chartHandlers('ign'));
  table = new MapTableView($('#table'), {
    onCommitStart: commitBefore,
    onEdit: (i, field, value) => {
      const rows = state.rows.map((r, k) => (k === i ? { ...r, [field]: value } : r));
      applyRows(rows, { live: true });
    },
    onSelect: (sel) => { state.selection = sel; render(); },
  });

  // 接続
  $('#btn-webserial').addEventListener('click', () => guard(connectWebSerial));
  $('#btn-bridge').addEventListener('click', () => guard(() => connectBridge({})));
  $('#btn-fake').addEventListener('click', () => guard(() => connectBridge({ fake: true })));
  $('#btn-disconnect').addEventListener('click', () => guard(disconnect));

  // 実機
  $('#btn-read').addEventListener('click', () => guard(readFromDevice));
  $('#btn-upload').addEventListener('click', () => guard(uploadFull));
  $('#btn-push').addEventListener('click', () => guard(pushDelta));
  $('#btn-save').addEventListener('click', () => guard(saveEeprom));
  $('#btn-load').addEventListener('click', () => guard(() => simpleCommand('MAP LOAD', 'EEPROMから読み直し')));
  $('#btn-default').addEventListener('click', () => guard(() => simpleCommand('MAP DEFAULT', '内蔵デフォルトMAPへ戻す')));

  // ファイル
  $('#file-csv').addEventListener('change', async (e) => {
    const file = e.target.files[0];
    if (!file) return;
    const { rows, warnings } = CSV.parseCsv(await file.text());
    warnings.forEach((w) => say(w, 'warn'));
    if (!rows.length) return;
    commitBefore();
    state.rows = rows;
    state.selection.clear();
    const err = M.validate(rows);
    if (err) {
      say(`${file.name} は現行ファームへ転送できません: ${M.ERROR_TEXT[err] || err}。`
        + '該当セルを修正してください。', 'error');
    } else {
      say(`${file.name} を読み込みました（${rows.length} 行 crc=${M.hex4(M.crc16(rows))}）`, 'ok');
    }
    render();
    e.target.value = '';
  });
  $('#btn-export').addEventListener('click', () => {
    CSV.download('RPM_EDIT.CSV', CSV.formatCsv(state.rows));
    say('CSVを書き出しました', 'ok');
  });
  $('#btn-revert').addEventListener('click', () => {
    commitBefore();
    state.rows = M.clone(state.deviceRows);
    render();
    say('実機の値へ戻しました');
  });

  // 編集
  $('#btn-undo').addEventListener('click', () => {
    const prev = state.history.undo(state.rows);
    if (prev) { state.rows = prev; render(); }
  });
  $('#btn-redo').addEventListener('click', () => {
    const next = state.history.redo(state.rows);
    if (next) { state.rows = next; render(); }
  });
  document.addEventListener('keydown', (e) => {
    if (!(e.metaKey || e.ctrlKey)) return;
    if (e.key === 'z' && !e.shiftKey) { e.preventDefault(); $('#btn-undo').click(); }
    if (e.key === 'z' && e.shiftKey) { e.preventDefault(); $('#btn-redo').click(); }
    if (e.key === 'y') { e.preventDefault(); $('#btn-redo').click(); }
  });

  const field = () => ($('#field-ign').checked ? 'ign' : 'inj');
  $('#btn-plus5').addEventListener('click', () => editOp((r, i) => M.trimRatio(r, i, field(), 0.05)));
  $('#btn-minus5').addEventListener('click', () => editOp((r, i) => M.trimRatio(r, i, field(), -0.05)));
  $('#btn-plus1').addEventListener('click', () => editOp((r, i) => M.trimDelta(r, i, field(), 1)));
  $('#btn-minus1').addEventListener('click', () => editOp((r, i) => M.trimDelta(r, i, field(), -1)));
  $('#btn-interp').addEventListener('click', () => editOp((r, i) => M.interpolate(r, i, field())));
  $('#btn-smooth').addEventListener('click', () => editOp((r, i) => M.smooth(r, i, field())));
  $('#btn-selall').addEventListener('click', () => {
    state.selection = new Set(state.rows.map((_, i) => i));
    render();
  });
  $('#btn-selnone').addEventListener('click', () => { state.selection.clear(); render(); });

  $('#btn-live').addEventListener('click', () => {
    state.liveApply = !state.liveApply;
    state.liveArmedUntil = Date.now() + LIVE_ARM_MS;
    say(state.liveApply
      ? 'ライブ適用を有効にしました（60秒無操作で自動解除）。編集が即座に MAP SET で送られます。'
      : 'ライブ適用を無効にしました', state.liveApply ? 'warn' : 'info');
    render();
  });

  // ロギング
  $('#btn-record').addEventListener('click', toggleRecording);
  $('#btn-savelog').addEventListener('click', saveLog);
  $('#btn-stopreplay').addEventListener('click', stopReplay);
  $('#file-log').addEventListener('change', async (e) => {
    const file = e.target.files[0];
    if (!file) return;
    startReplay(CSV.parseTelemetryCsv(await file.text()));
    e.target.value = '';
  });

  $('#opt-device').addEventListener('change', render);
  $('#opt-dwell').addEventListener('change', render);
  $('#btn-resetdwell').addEventListener('click', () => {
    state.dwell.reset();
    render();
    say('滞在時間をリセットしました');
  });

  window.addEventListener('resize', scheduleRender);
  // タブを閉じるときにセッションを残さない。ファーム側にも5秒のウォッチドッグがある。
  window.addEventListener('beforeunload', () => {
    try { state.transport?.command('MAP ABORT'); } catch { /* noop */ }
  });

  if (!WebSerialTransport.supported) {
    $('#btn-webserial').disabled = true;
    $('#btn-webserial').title = 'このブラウザは Web Serial に未対応です（Chrome / Edge が必要）';
  }
  if (!HttpBridgeTransport.available || location.protocol === 'file:') {
    $('#btn-bridge').disabled = true;
    $('#btn-fake').disabled = true;
  }
}

/** ドラッグ編集。pointerdown で1回スナップショット、移動中は積まない。 */
function chartHandlers(f) {
  return {
    onDragStart: (i) => {
      commitBefore();
      if (!state.selection.has(i)) { state.selection = new Set([i]); }
    },
    onDrag: (i, fieldName, value) => {
      const rows = state.rows.map((r, k) => (k === i ? { ...r, [fieldName]: value } : r));
      state.rows = rows;
      scheduleRender();
    },
    onDragEnd: (i, moved) => {
      if (!moved) {
        // 動かしていないなら履歴を1つ戻す（空のUndoを残さない）
        state.history.undoStack.pop();
      } else {
        maybeLiveApply();
      }
      render();
    },
    onSelect: (i, e) => {
      if (e.shiftKey || e.metaKey || e.ctrlKey) state.selection.add(i);
      else state.selection = new Set([i]);
      render();
    },
  };
}

async function guard(fn) {
  try {
    await fn();
  } catch (e) {
    say(e.message || String(e), 'error');
  }
}

/**
 * ローカルサーバ（tools/map_gui.py）が既に実機を握っていれば、開き直さずに追従する。
 * サーバは起動時に自分で接続するので、これが無いとページを開くたびに
 * 接続し直すことになり、テレメトリが一瞬途切れる。
 */
async function attachIfServerConnected() {
  if (!HttpBridgeTransport.available) return false;
  try {
    const tp = new HttpBridgeTransport();
    const st = await tp.status();
    if (!st.connected) return false;
    attachTransport(tp);
    tp.attach(st);
    await afterConnect();
    return true;
  } catch {
    return false;   // サーバが居ない（GitHub Pages 版）
  }
}

async function boot() {
  wire();
  state.rows = [];
  state.deviceRows = [];
  render();
  say('MAP調整GUI を起動しました。上の「接続」から実機を選んでください。');

  if (await attachIfServerConnected()) return;
  if (!WebSerialTransport.supported && !HttpBridgeTransport.available) {
    say('このブラウザでは実機に接続できません。Chrome / Edge を使うか、'
      + 'python3 tools/map_gui.py でローカルサーバを起動してください。', 'warn');
  }
  if ('serviceWorker' in navigator && location.protocol === 'https:') {
    navigator.serviceWorker.register('./sw.js').catch(() => {});
  }
}

document.addEventListener('DOMContentLoaded', boot);
