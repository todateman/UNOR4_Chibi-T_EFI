// テレメトリのリングバッファと滞在時間（dwell）の集計。
//
// 10Hz で長時間受け続けるので、サンプルごとにオブジェクトを作るとGC圧が効いてくる。
// 型付き配列を並べた SoA で持つ。

import { MAP_MAX_ENTRIES } from './maptable.js';

export class TelemetryRing {
  constructor(capacity = 18000) {     // 10Hz で 30分
    this.cap = capacity;
    this.n = 0;
    this.w = 0;
    this.seq = new Uint16Array(capacity);
    this.ms = new Uint32Array(capacity);
    this.rpm = new Uint16Array(capacity);
    this.inj01 = new Uint8Array(capacity);
    this.ign = new Int16Array(capacity);
    this.spd01 = new Uint16Array(capacity);
    this.ne = new Int16Array(capacity);
    this.row = new Uint8Array(capacity);
    this.flags = new Uint8Array(capacity);
  }

  push(s) {
    const i = this.w;
    this.seq[i] = s.seq;
    this.ms[i] = s.ms;
    this.rpm[i] = s.rpm;
    this.inj01[i] = s.inj01;
    this.ign[i] = s.ign;
    this.spd01[i] = s.spd01;
    this.ne[i] = s.ne;
    this.row[i] = s.row;
    this.flags[i] = s.flags;
    this.w = (i + 1) % this.cap;
    if (this.n < this.cap) this.n += 1;
  }

  /** 論理インデックス（0 = 最も古い）から物理インデックスへ。 */
  _phys(k) {
    const start = this.n < this.cap ? 0 : this.w;
    return (start + k) % this.cap;
  }

  at(k) {
    const i = this._phys(k);
    return {
      seq: this.seq[i], ms: this.ms[i], rpm: this.rpm[i], inj01: this.inj01[i],
      ign: this.ign[i], spd01: this.spd01[i], ne: this.ne[i],
      row: this.row[i], flags: this.flags[i],
    };
  }

  /** 直近 count 件を古い順に配列で返す（グラフ描画用）。 */
  tail(count) {
    const n = Math.min(count, this.n);
    const out = new Array(n);
    for (let k = 0; k < n; k += 1) out[k] = this.at(this.n - n + k);
    return out;
  }

  toArray() { return this.tail(this.n); }

  clear() { this.n = 0; this.w = 0; }
}

/**
 * MAP行ごとの滞在時間（ms）。
 *
 * サンプル間隔をそのまま足すが、以下は加算しない:
 *   - row が 255（MAP未使用: 始動時・範囲外）
 *   - seq が飛んでいる区間（取りこぼし）
 *   - 間隔が負または大きすぎる区間（MAP BEGIN中のミュート・切断・millisの巻き戻り）
 * これをやらないと、止まっていた時間が特定の行に丸ごと積まれて嘘のヒートマップになる。
 */
export class DwellTracker {
  constructor(maxGapMs = 500) {
    this.maxGapMs = maxGapMs;
    this.dwell = new Float64Array(MAP_MAX_ENTRIES);
    this.prev = null;
  }

  add(s) {
    const p = this.prev;
    this.prev = s;
    if (!p) return;
    if (s.seq !== 0 && p.seq !== 0 && ((p.seq + 1) & 0xffff) !== s.seq) return;
    if (s.row >= MAP_MAX_ENTRIES) return;
    const dt = s.ms - p.ms;
    if (dt <= 0 || dt > this.maxGapMs) return;
    this.dwell[s.row] += dt;
  }

  get max() {
    let m = 0;
    for (const v of this.dwell) if (v > m) m = v;
    return m;
  }

  /** 滞在時間を 0..1 へ正規化する。偏りが極端なので対数で潰す。 */
  normalized() {
    const m = this.max;
    if (m <= 0) return new Float64Array(MAP_MAX_ENTRIES);
    const out = new Float64Array(MAP_MAX_ENTRIES);
    const denom = Math.log1p(m);
    for (let i = 0; i < MAP_MAX_ENTRIES; i += 1) out[i] = Math.log1p(this.dwell[i]) / denom;
    return out;
  }

  reset() {
    this.dwell.fill(0);
    this.prev = null;
  }
}
