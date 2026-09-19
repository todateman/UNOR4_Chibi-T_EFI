// MAP調整 Web GUI 本体。
//
// 状態は1箇所（state）に集め、UIは render() で再描画する。フレームワークは使わない。
// 安全ガードは safety() に集約し、各所で個別に if を書かない（漏れが致命的になる）。

import { WebSerialTransport } from './transport/webserial.js';
import { HttpBridgeTransport } from './transport/httpbridge.js';
import {
  parseInfo, parseVersion, parseDump, isRunning, FLAG, TELEMETRY_MIN_PROTO,
} from './protocol/lines.js';
import * as M from './model/maptable.js';
import * as CSV from './model/csv.js';
import { History } from './model/history.js';
import { TelemetryRing, DwellTracker } from './model/ring.js';
import { MapChart } from './ui/chart.js';
import { MapTableView } from './ui/table.js';

const $ = (sel) => document.querySelector(sel);

// ライブ適用の自動解除までの無操作時間。1操作あたりの変化量の上限は
// model 側の FIELD_META[*].liveMaxStep にある（テストで固定できるように移した）。
const LIVE_ARM_MS = 60000;

/** 「噴射 ±1.0ms / 進角 ±5CA / 噴射終了 ±30CA」のような上限の一覧を作る。 */
const liveStepLimits = () => M.EDIT_FIELDS.map((f) => {
  const m = M.FIELD_META[f];
  return `${m.short} ±${m.toDisplay(m.liveMaxStep).toFixed(m.digits)}${m.unit}`;
}).join(' / ');

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

let charts = [];       // { field, chart } を EDIT_FIELDS ぶん
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

  // T行の列順は proto=5 で変わった。版数が合わないファームに TELEM ON すると値が黙ってずれる。
  if (state.version.proto >= TELEMETRY_MIN_PROTO) {
    try {
      const r = await state.transport.command('TELEM ON 100');
      say(`テレメトリ開始: ${r.code}`, 'dim');
    } catch (e) { say(`テレメトリを開始できません: ${e.message}`, 'warn'); }
  } else {
    say(`このファームは機械可読テレメトリ(TELEM)の版が合いません(proto=${state.version.proto}, 必要: ${TELEMETRY_MIN_PROTO}以上)。`
      + '2Hzの旧形式で表示します（行番号・状態フラグ・噴射終了角は取得できません）。', 'warn');
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
/**
 * 実機のMAPと状態を読み直す。
 *
 * syncEdit=true なら編集中のテーブルも実機の内容で置き換える。
 * 「実機読出」「EEPROM読直し」「既定へ戻す」は実機の値を画面に反映させるための
 * 操作なので、編集済みでも必ず追従させること（さもないと実機だけ変わって
 * 画面が変わらず、操作が効いていないように見える）。
 * 上書きの前に履歴へ積むので、編集内容は Undo で取り戻せる。
 */
async function readFromDevice({ syncEdit = false } = {}) {
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
    } else if (syncEdit) {
      const changed = M.diff(state.deviceRows, state.rows);
      const dirty = !changed.breakpointsMatch || changed.changedRows.length > 0;
      if (dirty) {
        commitBefore();          // 編集内容を Undo で戻せるようにしてから上書きする
        state.selection.clear();
        say('編集中の内容を実機の値で置き換えました（元に戻すには ↶）', 'warn');
      }
      state.rows = M.clone(state.deviceRows);
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
        await tp.command(`${r.rpm},${r.inj},${r.ign},${r.end}`);
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
    // 編集内容は上書きしない。成功していれば実機＝編集中なので差分表示は自然に消え、
    // 失敗したときは残った差分がそのまま「まだ入っていない分」を示す。
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
      await state.transport.command(`MAP SET ${r.rpm} ${r.inj} ${r.ign} ${r.end}`);
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

/**
 * 転送後の検証。全行読み戻すより速く、実機の内部表現と直接比較できる。
 * quiet=true なら一致時に何も言わない（ライブ適用でログが埋まらないように）。
 */
async function verifyCrc({ quiet = false } = {}) {
  try {
    const inf = await state.transport.command('MAP INFO');
    const info = parseInfo(inf.body);
    state.info = info;                    // ヘッダのCRC表示もここで更新される
    const expect = M.crc16(state.rows);
    if (info.crc !== expect) {
      say(`CRC不一致: 期待 ${M.hex4(expect)} / 実機 ${M.hex4(info.crc)}`, 'error');
      return false;
    }
    if (!quiet) say(`CRC検証OK: ${M.hex4(expect)}`, 'ok');
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
  // ベンチではノイズで tachoRpm が一瞬非ゼロになり ERR ENGINE_RUNNING が返る。
  // 実測では 36% の確率で拾うので、3回だと 5% ほど取りこぼす。
  const SAVE_RETRIES = 5;
  for (let attempt = 1; attempt <= SAVE_RETRIES; attempt += 1) {
    try {
      // eslint-disable-next-line no-await-in-loop
      const r = await state.transport.command('MAP SAVE');
      say(r.code.includes('UNCHANGED')
        ? 'EEPROMの内容は同一のため書き込みませんでした'
        : 'EEPROMへ保存しました', 'ok');
      break;
    } catch (e) {
      if (!String(e.message).includes('ENGINE_RUNNING') || attempt === SAVE_RETRIES) {
        say(`EEPROM保存に失敗しました: ${e.message}`, 'error');
        break;
      }
      say(`ENGINE_RUNNING が返りました。再試行します (${attempt}/${SAVE_RETRIES})`, 'warn');
      // eslint-disable-next-line no-await-in-loop
      await new Promise((r) => setTimeout(r, 400));
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
    // MAP LOAD / MAP DEFAULT は実機のMAPを差し替える操作なので、画面も追従させる
    await readFromDevice({ syncEdit: true });
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

let liveBusy = false;
let livePending = false;

/**
 * ライブ適用。編集を MAP SET で即座に実機へ送る。
 *
 * 送信中に届いた編集は捨てずに保留し、完了後にまとめて送る。以前は state.busy を
 * 見て黙って return していたため、矢印キーの連打やドラッグ中の編集が
 * 取りこぼされ、「ライブ適用が有効なのに反映されない」状態になっていた。
 *
 * 1打鍵ごとに MAP? を読み直すと往復が重いので、OK SET が返った行だけ
 * deviceRows をローカルで更新し、連打が収まってから CRC で一括検証する。
 */
async function maybeLiveApply() {
  if (!state.liveApply || !state.connected) return;
  livePending = true;
  if (liveBusy) return;          // 進行中。この編集も完了後に送られる
  liveBusy = true;
  try {
    while (livePending) {
      livePending = false;

      if (Date.now() > state.liveArmedUntil) {
        state.liveApply = false;
        say('ライブ適用が60秒無操作で自動解除されました', 'warn');
        return;
      }

      const d = M.diff(state.deviceRows, state.rows);
      if (!d.breakpointsMatch) {
        say('RPMブレークポイントが実機と違うため、ライブ適用できません（全転送が必要です）', 'warn');
        return;
      }
      if (!d.changedRows.length) continue;   // 送るものがない
      state.liveArmedUntil = Date.now() + LIVE_ARM_MS;

      const tooBig = M.liveStepViolation(state.deviceRows, state.rows, d.changedRows);
      if (tooBig) {
        say(`${state.rows[tooBig.index].rpm} rpm の変化量が大きすぎます`
          + `（1回のライブ反映は ${liveStepLimits()} まで）。`
          + '「変更を送信」から明示的に反映してください。', 'warn');
        return;
      }

      for (const i of d.changedRows) {
        const r = state.rows[i];
        try {
          // eslint-disable-next-line no-await-in-loop
          await state.transport.command(`MAP SET ${r.rpm} ${r.inj} ${r.ign} ${r.end}`);
          state.deviceRows[i] = { ...r };    // OK SET が返った行だけ反映済みとする
        } catch (e) {
          say(`ライブ反映に失敗しました（${r.rpm} rpm）: ${e.message}`, 'error');
          return;
        }
      }
      render();
    }
    // 連打が収まったところで実機と突き合わせる（一致していれば何も言わない）
    await verifyCrc({ quiet: true });
  } finally {
    liveBusy = false;
    render();
  }
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
  // 採用行はファームの報告を真とする。row=255 は「MAPを参照していない」の意味なので
  // ハイライトを消す（回転数から推測し直すと、噴射・点火が止まっている状態でも
  // どこかの行を指してしまう）。回転数からの推測は、row を持たない旧ファーム専用。
  const activeRow = !t ? -1
    : t.legacy ? M.rowForRpm(state.rows, t.rpm)
      : t.row < M.MAP_MAX_ENTRIES ? t.row : -1;

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
  // 噴射終了角は機械可読テレメトリ(proto>=5)だけが載せてくる。MAP外(row=255)のときは
  // ファームが前回値を持ち回っているだけなので出さない。
  $('#v-end').textContent = (t && t.end !== null && t.row !== 255) ? t.end : '—';
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
  // hidden を外すのは set() より先。rect が 0 のままだと MapChart.render() が
  // 早期 return して、再表示した直後に真っ白な canvas が残る。
  $('#panel-end').hidden = !$('#opt-end').checked;
  charts.forEach(({ chart }) => chart.set(chartState));

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
  charts = M.EDIT_FIELDS.map((field) => ({
    field,
    chart: new MapChart($(`#chart-${field}`), field, chartHandlers(field)),
  }));
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
  $('#btn-read').addEventListener('click', () => guard(() => readFromDevice({ syncEdit: true })));
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

  // ラジオを value 駆動にして、列が増えても二択の三項演算子を書かずに済むようにする
  const field = () => (document.querySelector('input[name="field"]:checked')?.value ?? 'inj');
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
  $('#opt-end').addEventListener('change', render);
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
  // ブリッジ用のボタンは既定で無効にし、サーバの応答を確かめてから有効にする。
  // GitHub Pages にはローカルサーバが居ないので、押せてしまうと404になるだけ。
  setBridgeButtons(false);
}

function setBridgeButtons(enabled) {
  const note = enabled ? ''
    : 'ローカルサーバが見つかりません。python3 tools/map_gui.py を起動して、'
      + 'そこで開いたページから使ってください。';
  for (const id of ['#btn-bridge', '#btn-fake']) {
    $(id).disabled = !enabled;
    $(id).title = note;
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
async function attachIfServerConnected(status) {
  if (!status || !status.connected) return false;
  try {
    const tp = new HttpBridgeTransport();
    attachTransport(tp);
    tp.attach(status);
    await afterConnect();
    return true;
  } catch (e) {
    say(`ローカルサーバへの追従に失敗しました: ${e.message}`, 'warn');
    return false;
  }
}

async function boot() {
  wire();
  state.rows = [];
  state.deviceRows = [];
  render();
  say('MAP調整GUI を起動しました。上の「接続」から実機を選んでください。');

  // Service Worker は静的配信されているときだけ。ローカルサーバ経由では
  // /api/ を挟むので登録しない（sw.js側でも /api/ は素通しにしている）。
  if ('serviceWorker' in navigator && location.protocol === 'https:') {
    navigator.serviceWorker.register('./sw.js').catch(() => {});
  }

  // ローカルサーバが居るかを一度だけ確かめ、その結果でボタンの有効／無効を決める
  const status = await HttpBridgeTransport.probe();
  setBridgeButtons(status !== null);

  if (status === null) {
    if (!WebSerialTransport.supported) {
      say('このブラウザでは実機に接続できません。Chrome / Edge の Web Serial を使うか、'
        + 'python3 tools/map_gui.py でローカルサーバを起動してください。', 'warn');
    } else {
      say('ローカルサーバは見つかりませんでした。「Web Serial で接続」から実機を選んでください。', 'dim');
    }
    return;
  }
  if (await attachIfServerConnected(status)) return;
  say('ローカルサーバが動いています。「ローカルサーバ経由」から実機を選んでください。', 'dim');
}

document.addEventListener('DOMContentLoaded', boot);
