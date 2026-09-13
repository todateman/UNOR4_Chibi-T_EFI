// ローカル Python サーバ（tools/map_gui.py）経由の接続。
//
// Web Serial に対応していないブラウザ（Safari / Firefox）向けの経路。
// コマンドの直列化はサーバ側が行うので、こちらは fetch するだけでよい。
// テレメトリは SSE（EventSource）で受ける。WebSocket を使わないのは、
// サーバ側の依存ライブラリを1つも増やさずに済むため。
//
// WebSerialTransport と同じインターフェースを満たす。

export class HttpBridgeTransport {
  constructor(base = '') {
    this.kind = 'httpbridge';
    this.base = base;            // 同一オリジンで配信されるので既定は空
    this.name = '';
    this._es = null;
    this._connected = false;
    this._telemetryCbs = new Set();
    this._statusCbs = new Set();
    this._noticeCbs = new Set();
  }

  /**
   * ブリッジを試す価値があるか（file: では fetch も EventSource も使えない）。
   * これは「使える」保証ではない。GitHub Pages のような静的配信でも true になるので、
   * 実際に使えるかは probe() でサーバの応答を確かめること。
   */
  static get available() {
    return typeof location !== 'undefined'
      && (location.protocol === 'http:' || location.protocol === 'https:');
  }

  /**
   * ローカルサーバ(tools/map_gui.py)が実際に居るかを確かめる。
   * 居れば status を、居なければ null を返す。
   * GitHub Pages では /api/status が404になるのでここで null になり、
   * ブリッジ用のボタンを無効化できる。
   */
  static async probe(base = '') {
    if (!HttpBridgeTransport.available) return null;
    try {
      const res = await fetch(`${base}/api/status`, { cache: 'no-store' });
      if (!res.ok) return null;
      const data = await res.json();
      return (data && typeof data.connected === 'boolean') ? data : null;
    } catch {
      return null;   // サーバが居ない（静的配信）
    }
  }

  onTelemetry(cb) { this._telemetryCbs.add(cb); return () => this._telemetryCbs.delete(cb); }
  onStatus(cb) { this._statusCbs.add(cb); return () => this._statusCbs.delete(cb); }
  onNotice(cb) { this._noticeCbs.add(cb); return () => this._noticeCbs.delete(cb); }

  _emitStatus(s) { for (const cb of this._statusCbs) cb(s); }

  get connected() { return this._connected; }

  async _post(path, body) {
    const res = await fetch(this.base + path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {}),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
    return data;
  }

  /** サーバが握っているポート候補を返す。GUIのポート選択に使う。 */
  async listPorts() {
    const res = await fetch(`${this.base}/api/ports`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return (await res.json()).ports || [];
  }

  /** サーバの現在の接続状態。map_gui.py は起動時に自分で接続する。 */
  async status() {
    const res = await fetch(`${this.base}/api/status`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return res.json();
  }

  /**
   * サーバが既に握っている接続へ追従する。
   * 開き直さないので、テレメトリが途切れず MAP の状態も保たれる。
   */
  attach(status) {
    this.name = status.fake ? 'モックECU' : status.port;
    this._connected = true;
    this._openStream();
    this._emitStatus({ connected: true, port: this.name, kind: this.kind });
  }

  async connect(opts = {}) {
    const info = await this._post('/api/connect', {
      port: opts.port || null,
      fake: !!opts.fake,
    });
    this.name = info.fake ? 'モックECU' : info.port;
    this._connected = true;
    this._openStream();
    this._emitStatus({ connected: true, port: this.name, kind: this.kind, info });
    return info;
  }

  async disconnect() {
    this._closeStream();
    this._connected = false;
    try { await this._post('/api/disconnect'); } catch { /* noop */ }
    this._emitStatus({ connected: false, port: '', kind: this.kind });
  }

  _openStream() {
    this._closeStream();
    const es = new EventSource(`${this.base}/api/telemetry`);
    es.addEventListener('telemetry', (ev) => {
      try {
        const sample = JSON.parse(ev.data);
        for (const cb of this._telemetryCbs) cb(sample);
      } catch { /* 壊れた行は捨てる */ }
    });
    es.addEventListener('notice', (ev) => {
      try {
        const { text } = JSON.parse(ev.data);
        for (const cb of this._noticeCbs) cb(text);
      } catch { /* noop */ }
    });
    es.addEventListener('status', (ev) => {
      try {
        const s = JSON.parse(ev.data);
        this._connected = !!s.connected;
        this._emitStatus({ ...s, kind: this.kind });
      } catch { /* noop */ }
    });
    // EventSource は自動で再接続するので、ここでは握りつぶしてよい
    es.onerror = () => {};
    this._es = es;
  }

  _closeStream() {
    if (this._es) { this._es.close(); this._es = null; }
  }

  /** 1行送ってOK/ERRが返るまで待つ。直列化とタイムアウトはサーバ側の責務。 */
  async command(line, timeout) {
    if (!this._connected) throw new Error('接続されていません');
    const res = await this._post('/api/command', {
      line,
      timeout: timeout ? timeout / 1000 : null,
    });
    return { ack: res.ack, code: res.code, body: res.body || [] };
  }
}
