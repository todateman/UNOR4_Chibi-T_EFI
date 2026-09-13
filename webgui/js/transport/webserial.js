// Web Serial API による接続（GitHub Pages 版）。
//
// コマンドキューをブラウザ側に持つ。ファームは「非空行1つにつきOK/ERRを1つだけ
// 返す」ので、同時に1コマンドだけ走らせれば順序一致だけで正しくマッチする。
// IDタグは不要。
//
// send_map.py の旧実装と違い、送信前に入力バッファを捨てない。テレメトリが
// 流れ続けるので、捨てるのではなくテレメトリバスへ振り分ける。

import {
  KIND, classifyLine, parseTelemetry, rejectReason, timeoutFor,
} from '../protocol/lines.js';

const BAUD = 115200;
const ARDUINO_VIDS = [0x2341, 0x2a03];

export class WebSerialTransport {
  static get supported() {
    return typeof navigator !== 'undefined' && 'serial' in navigator;
  }

  constructor() {
    this.kind = 'webserial';
    this.port = null;
    this.name = '';
    this._writer = null;
    this._readLoop = null;
    this._buf = '';
    this._pending = null;      // 同時に1つだけ
    this._queue = [];
    this._telemetryCbs = new Set();
    this._statusCbs = new Set();
    this._noticeCbs = new Set();
  }

  onTelemetry(cb) { this._telemetryCbs.add(cb); return () => this._telemetryCbs.delete(cb); }
  onStatus(cb) { this._statusCbs.add(cb); return () => this._statusCbs.delete(cb); }
  onNotice(cb) { this._noticeCbs.add(cb); return () => this._noticeCbs.delete(cb); }

  _emitStatus(s) { for (const cb of this._statusCbs) cb(s); }

  get connected() { return this.port !== null; }

  async connect() {
    if (!WebSerialTransport.supported) {
      throw new Error('このブラウザは Web Serial に対応していません（Chrome / Edge を使うか、'
        + 'ローカルサーバ経由で接続してください）');
    }
    const port = await navigator.serial.requestPort({
      filters: ARDUINO_VIDS.map((usbVendorId) => ({ usbVendorId })),
    });
    await port.open({ baudRate: BAUD, bufferSize: 4096 });
    this.port = port;
    const info = port.getInfo ? port.getInfo() : {};
    this.name = info.usbVendorId
      ? `USB ${info.usbVendorId.toString(16)}:${(info.usbProductId || 0).toString(16)}`
      : 'シリアルポート';
    this._writer = port.writable.getWriter();
    this._readLoop = this._read(port);
    this._emitStatus({ connected: true, port: this.name, kind: this.kind });
  }

  async disconnect() {
    const port = this.port;
    this.port = null;
    this._failAll(new Error('切断されました'));
    try { if (this._writer) { this._writer.releaseLock(); this._writer = null; } } catch { /* noop */ }
    try { if (port) await port.close(); } catch { /* noop */ }
    this._emitStatus({ connected: false, port: '', kind: this.kind });
  }

  async _read(port) {
    const decoder = new TextDecoder();
    const reader = port.readable.getReader();
    try {
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        this._buf += decoder.decode(value, { stream: true });
        // ファームは '\n' / '\r' のどちらでも行終端として扱う
        for (;;) {
          const i = this._buf.search(/[\r\n]/);
          if (i < 0) break;
          const line = this._buf.slice(0, i).trim();
          this._buf = this._buf.slice(i + 1);
          if (line) this._line(line);
        }
        if (this._buf.length > 4096) this._buf = '';   // 暴走ガード
      }
    } catch (e) {
      this._failAll(e);
      this._emitStatus({ connected: false, port: '', kind: this.kind, error: String(e) });
    } finally {
      try { reader.releaseLock(); } catch { /* noop */ }
    }
  }

  _line(text) {
    const kind = classifyLine(text);
    if (kind === KIND.TELEMETRY) {
      const sample = parseTelemetry(text);
      if (sample) for (const cb of this._telemetryCbs) cb(sample);
      return;
    }
    const cur = this._pending;
    if (!cur) {
      // 起動バナーなど、コマンドを待っていないときに届いた行
      for (const cb of this._noticeCbs) cb(text);
      return;
    }
    if (kind === KIND.ACK_OK) return this._settle(true, text);
    if (kind === KIND.ACK_ERR) return this._settle(false, text);
    if (cur.body.length < 128) cur.body.push(text);
    return undefined;
  }

  _settle(ok, ack) {
    const cur = this._pending;
    this._pending = null;
    clearTimeout(cur.timer);
    if (ok) cur.resolve({ ack, code: ack.slice(2).trim(), body: cur.body });
    else cur.reject(new Error(`${cur.line} -> ${ack}`));
    this._pump();
  }

  _failAll(err) {
    if (this._pending) {
      clearTimeout(this._pending.timer);
      this._pending.reject(err);
      this._pending = null;
    }
    const q = this._queue.splice(0);
    for (const c of q) c.reject(err);
  }

  /** 1行送ってOK/ERRが返るまで待つ。ERRなら reject する。 */
  command(line, timeout) {
    const reason = rejectReason(line);
    if (reason) return Promise.reject(new Error(reason));
    if (!this.connected) return Promise.reject(new Error('接続されていません'));
    if (this._queue.length > 64) return Promise.reject(new Error('送信キューが飽和しています'));
    return new Promise((resolve, reject) => {
      this._queue.push({
        line, timeout: timeout || timeoutFor(line), resolve, reject, body: [],
      });
      this._pump();
    });
  }

  _pump() {
    if (this._pending || this._queue.length === 0) return;
    const cur = this._queue.shift();
    this._pending = cur;
    cur.timer = setTimeout(() => {
      this._pending = null;
      cur.reject(new Error(`${cur.line} への応答がありません（タイムアウト）`));
      this._pump();
    }, cur.timeout);
    const bytes = new TextEncoder().encode(`${cur.line}\n`);   // 終端はLFのみ
    this._writer.write(bytes).catch((e) => {
      clearTimeout(cur.timer);
      this._pending = null;
      cur.reject(e);
      this._pump();
    });
  }
}
