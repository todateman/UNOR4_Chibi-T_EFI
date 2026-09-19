// MAPの階段グラフ（Canvas2D）。外部ライブラリは使わない。
//
// 描くのは最大24点の1次元階段が3枚だけなので、チャートライブラリを持ち込むより
// 自前で描く方が、点ドラッグ編集・滞在ヒートマップ・実機MAPの重ね描きといった
// このアプリ固有の要件に対して素直になる。ピットではネットが無いのでCDNも使えない。
//
// 階段の形はファームの参照ロジックに合わせる:
//   「最初に tachoRpm < e[i].rpm となる行 i を採用」
// つまり行 i の値は [rows[i-1].rpm, rows[i].rpm) の区間で有効。
//
// 縦軸の上限・単位・表示換算・目盛数は model の FIELD_META から引く。以前は
// `field === 'inj' ? A : B` の二値前提が各所に散っていて、列を1本増やせなかった。

import { FIELD_META, clampField, TACHO_RPM_MAX } from '../model/maptable.js';

const PAD = { left: 52, right: 14, top: 14, bottom: 26 };
const HIT_RADIUS = 12;

const css = (el, name, fallback) => {
  const v = getComputedStyle(el).getPropertyValue(name).trim();
  return v || fallback;
};

export class MapChart {
  /**
   * @param {HTMLCanvasElement} canvas
   * @param {'inj'|'ign'|'end'} field
   * @param {{onDragStart:Function, onDrag:Function, onDragEnd:Function,
   *          onSelect:Function}} handlers
   */
  constructor(canvas, field, handlers = {}) {
    this.canvas = canvas;
    this.field = field;
    this.meta = FIELD_META[field];
    this.axis = this.meta.chart;
    this.h = handlers;
    this.state = {
      rows: [], deviceRows: [], selection: new Set(),
      dwell: null, liveRpm: 0, activeRow: -1, showDevice: true, showDwell: true,
    };
    this._drag = null;
    this._hover = -1;
    this._bind();
  }

  get max() { return this.meta.max; }

  /** 表示単位へ換算する（噴射は x0.1ms → ms）。 */
  toDisplay(v) { return this.meta.toDisplay(v); }

  get unit() { return this.meta.unit; }

  set(state) {
    Object.assign(this.state, state);
    this.render();
  }

  // -- 座標変換 -----------------------------------------------------------
  get _rect() {
    const { width, height } = this.canvas.getBoundingClientRect();
    return {
      w: width, h: height,
      x0: PAD.left, x1: width - PAD.right,
      y0: PAD.top, y1: height - PAD.bottom,
    };
  }

  get _rpmMax() {
    const rows = this.state.rows;
    const last = rows.length ? rows[rows.length - 1].rpm : TACHO_RPM_MAX;
    return Math.max(last, TACHO_RPM_MAX) * 1.06;
  }

  _sx(rpm) {
    const r = this._rect;
    return r.x0 + (rpm / this._rpmMax) * (r.x1 - r.x0);
  }

  _sy(v) {
    const r = this._rect;
    return r.y1 - (v / this.max) * (r.y1 - r.y0);
  }

  _invY(py) {
    const r = this._rect;
    return ((r.y1 - py) / (r.y1 - r.y0)) * this.max;
  }

  // -- 入力 ---------------------------------------------------------------
  _bind() {
    const c = this.canvas;
    c.addEventListener('pointerdown', (e) => this._down(e));
    c.addEventListener('pointermove', (e) => this._move(e));
    c.addEventListener('pointerup', (e) => this._up(e));
    c.addEventListener('pointercancel', (e) => this._up(e));
    c.addEventListener('pointerleave', () => {
      if (!this._drag) { this._hover = -1; this.render(); }
    });
  }

  _pos(e) {
    const b = this.canvas.getBoundingClientRect();
    return { x: e.clientX - b.left, y: e.clientY - b.top };
  }

  _nearest(p) {
    let best = -1;
    let bestD = HIT_RADIUS;
    this.state.rows.forEach((r, i) => {
      const d = Math.hypot(this._sx(r.rpm) - p.x, this._sy(r[this.field]) - p.y);
      if (d < bestD) { bestD = d; best = i; }
    });
    return best;
  }

  _down(e) {
    const p = this._pos(e);
    const i = this._nearest(p);
    if (i < 0) {
      // 点以外をクリックしたら、そのRPM位置に最も近い行を選択する
      const rpm = ((p.x - this._rect.x0) / (this._rect.x1 - this._rect.x0)) * this._rpmMax;
      let near = -1;
      let nd = Infinity;
      this.state.rows.forEach((r, k) => {
        const d = Math.abs(r.rpm - rpm);
        if (d < nd) { nd = d; near = k; }
      });
      if (near >= 0 && this.h.onSelect) this.h.onSelect(near, e);
      return;
    }
    this.canvas.setPointerCapture(e.pointerId);
    // ドラッグ開始時に1回だけスナップショットを積む。移動中に積むと
    // 1ドラッグで数百エントリになってUndoが使い物にならない。
    this._drag = { index: i, moved: false };
    if (this.h.onDragStart) this.h.onDragStart(i, e);
  }

  _move(e) {
    const p = this._pos(e);
    if (!this._drag) {
      const i = this._nearest(p);
      if (i !== this._hover) { this._hover = i; this.render(); }
      this.canvas.style.cursor = i >= 0 ? 'ns-resize' : 'crosshair';
      return;
    }
    this._drag.moved = true;
    const value = clampField(this.field, this._invY(p.y));
    if (this.h.onDrag) this.h.onDrag(this._drag.index, this.field, value);
  }

  _up(e) {
    if (!this._drag) return;
    try { this.canvas.releasePointerCapture(e.pointerId); } catch { /* noop */ }
    const d = this._drag;
    this._drag = null;
    if (this.h.onDragEnd) this.h.onDragEnd(d.index, d.moved);
  }

  // -- 描画 ---------------------------------------------------------------
  render() {
    const c = this.canvas;
    const dpr = window.devicePixelRatio || 1;
    const r = this._rect;
    if (r.w <= 0 || r.h <= 0) return;
    if (c.width !== Math.round(r.w * dpr) || c.height !== Math.round(r.h * dpr)) {
      c.width = Math.round(r.w * dpr);
      c.height = Math.round(r.h * dpr);
    }
    const g = c.getContext('2d');
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    g.clearRect(0, 0, r.w, r.h);

    const col = {
      grid: css(c, '--grid', '#2a3038'),
      text: css(c, '--muted', '#8b97a6'),
      edit: css(c, '--accent', '#4da3ff'),
      device: css(c, '--ghost', '#5c6773'),
      danger: css(c, '--danger', '#ff5c5c'),
      warn: css(c, '--warn', '#ffb84d'),
      live: css(c, '--live', '#ffd24d'),
      heat: css(c, '--heat', '#4da3ff'),
      sel: css(c, '--select', '#ffffff'),
    };

    this._drawDanger(g, r, col);
    if (this.state.showDwell) this._drawDwell(g, r, col);
    this._drawGrid(g, r, col);
    this._drawGuides(g, r, col);
    if (this.state.showDevice && this.state.deviceRows.length) {
      this._drawSteps(g, this.state.deviceRows, col.device, 1.5, true);
    }
    this._drawSteps(g, this.state.rows, col.edit, 2, false);
    this._drawLive(g, r, col);
    this._drawPoints(g, col);
  }

  /** MAP最終行より右＝噴射・点火が止まる領域。レブリミットも重ねて描く。 */
  _drawDanger(g, r, col) {
    const rows = this.state.rows;
    if (!rows.length) return;
    const lastRpm = rows[rows.length - 1].rpm;
    const xCut = this._sx(lastRpm);
    if (xCut < r.x1) {
      g.fillStyle = col.danger;
      g.globalAlpha = 0.13;
      g.fillRect(xCut, r.y0, r.x1 - xCut, r.y1 - r.y0);
      g.globalAlpha = 1;
      g.fillStyle = col.danger;
      g.font = '10px ui-monospace, monospace';
      g.textAlign = 'left';
      // グラフを横に並べると帯が狭くなる。入らないなら書かない（赤帯だけで伝わる）
      const label = '噴射・点火停止';
      if (g.measureText(label).width + 8 <= r.x1 - xCut) g.fillText(label, xCut + 4, r.y0 + 11);
    }
    const xRev = this._sx(TACHO_RPM_MAX);
    if (xRev >= r.x0 && xRev <= r.x1) {
      g.strokeStyle = col.danger;
      g.setLineDash([4, 3]);
      g.lineWidth = 1;
      g.beginPath();
      g.moveTo(xRev, r.y0);
      g.lineTo(xRev, r.y1);
      g.stroke();
      g.setLineDash([]);
    }
  }

  /**
   * 縦軸上の意味のある高さに横線を引く。
   * 噴射終了角の360CAだけが今のところ該当する（ファームがそこで噴射を強制終了する）。
   */
  _drawGuides(g, r, col) {
    const guides = this.axis.guides;
    if (!guides) return;
    g.font = '10px ui-monospace, monospace';
    g.textAlign = 'right';
    g.textBaseline = 'bottom';
    for (const gd of guides) {
      const y = this._sy(gd.v);
      if (y < r.y0 || y > r.y1) continue;
      g.strokeStyle = col.warn;
      g.globalAlpha = 0.7;
      g.lineWidth = 1;
      g.setLineDash([6, 4]);
      g.beginPath();
      g.moveTo(r.x0, y);
      g.lineTo(r.x1, y);
      g.stroke();
      g.setLineDash([]);
      g.fillStyle = col.warn;
      g.fillText(gd.text, r.x1 - 2, y - 2);
      g.globalAlpha = 1;
    }
  }

  /** 行ごとの滞在時間を、その行が受け持つRPM区間の背景の濃さで示す。 */
  _drawDwell(g, r, col) {
    const d = this.state.dwell;
    const rows = this.state.rows;
    if (!d || !rows.length) return;
    g.fillStyle = col.heat;
    rows.forEach((row, i) => {
      const v = d[i] || 0;
      if (v <= 0.001) return;
      const xa = this._sx(i === 0 ? 0 : rows[i - 1].rpm);
      const xb = this._sx(row.rpm);
      g.globalAlpha = 0.06 + v * 0.22;
      g.fillRect(xa, r.y0, xb - xa, r.y1 - r.y0);
    });
    g.globalAlpha = 1;
  }

  _drawGrid(g, r, col) {
    g.strokeStyle = col.grid;
    g.fillStyle = col.text;
    g.lineWidth = 1;
    g.font = '10px ui-monospace, monospace';

    const yTicks = this.axis.yTicks;
    g.textAlign = 'right';
    g.textBaseline = 'middle';
    for (let i = 0; i <= yTicks; i += 1) {
      const v = (this.max / yTicks) * i;
      const y = this._sy(v);
      g.beginPath();
      g.moveTo(r.x0, y);
      g.lineTo(r.x1, y);
      g.stroke();
      g.fillText(this.toDisplay(v).toFixed(this.meta.digits), r.x0 - 6, y);
    }

    g.textAlign = 'center';
    g.textBaseline = 'top';
    const step = 1000;
    for (let rpm = 0; rpm <= this._rpmMax; rpm += step) {
      const x = this._sx(rpm);
      if (x > r.x1) break;
      g.beginPath();
      g.moveTo(x, r.y0);
      g.lineTo(x, r.y1);
      g.stroke();
      g.fillText(String(rpm), x, r.y1 + 5);
    }
    g.textAlign = 'left';
    g.fillText(this.unit, 4, r.y0 - 2);
  }

  /** ファームの階段状参照をそのまま描く。 */
  _drawSteps(g, rows, color, width, dashed) {
    if (!rows.length) return;
    g.strokeStyle = color;
    g.lineWidth = width;
    if (dashed) g.setLineDash([5, 4]);
    g.beginPath();
    rows.forEach((row, i) => {
      const y = this._sy(row[this.field]);
      const xa = this._sx(i === 0 ? 0 : rows[i - 1].rpm);
      const xb = this._sx(row.rpm);
      if (i === 0) g.moveTo(xa, y); else g.lineTo(xa, y);
      g.lineTo(xb, y);
    });
    g.stroke();
    g.setLineDash([]);
  }

  _drawLive(g, r, col) {
    const { liveRpm } = this.state;
    if (!liveRpm) return;
    const x = this._sx(liveRpm);
    if (x < r.x0 || x > r.x1) return;
    g.strokeStyle = col.live;
    g.lineWidth = 1.5;
    g.beginPath();
    g.moveTo(x, r.y0);
    g.lineTo(x, r.y1);
    g.stroke();
    g.fillStyle = col.live;
    g.beginPath();                       // 上端に三角を置いて現在位置を示す
    g.moveTo(x, r.y0);
    g.lineTo(x - 4, r.y0 - 6);
    g.lineTo(x + 4, r.y0 - 6);
    g.closePath();
    g.fill();
  }

  _drawPoints(g, col) {
    const { rows, selection, activeRow } = this.state;
    rows.forEach((row, i) => {
      const x = this._sx(row.rpm);
      const y = this._sy(row[this.field]);
      const selected = selection.has(i);
      const active = i === activeRow;
      g.beginPath();
      g.arc(x, y, active ? 5.5 : (selected || i === this._hover) ? 4.5 : 3, 0, Math.PI * 2);
      g.fillStyle = active ? col.live : col.edit;
      g.fill();
      if (selected) {
        g.strokeStyle = col.sel;
        g.lineWidth = 1.5;
        g.stroke();
      }
    });
  }
}
