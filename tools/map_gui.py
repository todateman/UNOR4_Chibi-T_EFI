#!/usr/bin/env python3
"""MAP調整 Web GUI のローカルサーバ。

webgui/ を配信し、ブラウザと USB シリアルの間を中継する。
Web Serial API が使えないブラウザ（Safari / Firefox）でも GUI を使えるようにする
ための経路で、GitHub Pages 版と同じフロントエンドをそのまま動かす。

追加の依存は無い（標準ライブラリ + 既存の pyserial だけ）。テレメトリの
プッシュに WebSocket ではなく SSE を使うのは、そのためにライブラリを1つも
増やさずに済むから。

使い方:
    python3 tools/map_gui.py                  # ポート自動検出、ブラウザを開く
    python3 tools/map_gui.py --port /dev/cu.usbmodem1101
    python3 tools/map_gui.py --fake           # 実機なしのモックECU
    python3 tools/map_gui.py --http-port 8765 --no-browser

サーバは 127.0.0.1 にのみバインドする（外部公開しない）。
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import map_protocol as mp
from map_protocol import MapConsole, MapConsoleError

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB_ROOT = os.path.join(REPO, "webgui")

MIME = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".csv": "text/csv; charset=utf-8",
}


class Bridge:
    """シリアル接続を1本だけ保持し、コマンドを直列化してテレメトリを配る。"""

    def __init__(self):
        self._lock = threading.Lock()
        self.console: MapConsole | None = None
        self._ser = None
        self.port_name = ""
        self.fake = False
        self._subs: list[queue.Queue] = []
        self._subs_lock = threading.Lock()
        self.last_telemetry: dict | None = None

    # -- 購読 ---------------------------------------------------------------
    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=512)
        with self._subs_lock:
            self._subs.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._subs_lock:
            if q in self._subs:
                self._subs.remove(q)

    def _publish(self, event: str, data: dict) -> None:
        payload = (event, data)
        with self._subs_lock:
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(payload)
            except queue.Full:
                pass   # 読み手が遅れている。テレメトリは落としてよい

    def _on_telemetry(self, s: mp.Telemetry) -> None:
        d = {
            "seq": s.seq, "ms": s.ms, "rpm": s.rpm, "inj01": s.inj01,
            "ign": s.ign, "spd01": s.spd01, "ne": s.ne, "row": s.row,
            "flags": s.flags, "legacy": s.legacy,
        }
        self.last_telemetry = d
        self._publish("telemetry", d)

    def _on_unsolicited(self, text: str) -> None:
        # 起動バナー "MAP SOURCE: ..." など、コマンドを待っていないときの行
        self._publish("notice", {"text": text})

    # -- 接続 ---------------------------------------------------------------
    def connect(self, port: str | None, fake: bool = False) -> dict:
        with self._lock:
            self._close_locked()
            if fake:
                from fake_ecu import FakeSerial
                ser = FakeSerial()
                name = "fake"
            else:
                import serial
                name = port or mp.find_port()
                ser = serial.Serial(name, mp.BAUD, timeout=0.2)
                time.sleep(0.3)
                ser.reset_input_buffer()

            self._ser = ser
            self.port_name = name
            self.fake = fake
            self.console = MapConsole(
                ser, on_telemetry=self._on_telemetry,
                on_unsolicited=self._on_unsolicited)

        info: dict = {}
        version = {"fw": "unknown", "proto": 1}
        try:
            version = self.console.version()
            info = self.console.info()
        except MapConsoleError:
            pass   # 旧ファーム、または起動直後で応答が遅い

        # proto>=2 のファームだけが機械可読テレメトリに対応している
        if version.get("proto", 1) >= 2:
            try:
                self.console.telemetry(True, 100)
            except MapConsoleError:
                pass

        self._publish("status", self.status())
        return {"port": name, "fake": fake, "version": version, "info": info}

    def disconnect(self) -> None:
        with self._lock:
            self._close_locked()
        self._publish("status", self.status())

    def _close_locked(self) -> None:
        if self.console is not None:
            try:
                self.console.telemetry(False)
            except Exception:
                pass
            self.console.close()
            self.console = None
        if self._ser is not None:
            try:
                self._ser.close()
            except Exception:
                pass
            self._ser = None
        self.port_name = ""

    def status(self) -> dict:
        return {
            "connected": self.console is not None,
            "port": self.port_name,
            "fake": self.fake,
        }

    def command(self, line: str, timeout: float | None = None) -> dict:
        console = self.console
        if console is None:
            raise MapConsoleError("接続されていません")
        res = console.command(line, timeout)
        return {"ack": res.ack, "code": res.code, "body": res.body}


bridge = Bridge()


class Handler(BaseHTTPRequestHandler):
    server_version = "MapGui/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass   # アクセスログは出さない（テレメトリのSSEで埋まるため）

    # -- 応答ヘルパ ---------------------------------------------------------
    def _json(self, obj, status: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        return json.loads(self.rfile.read(n).decode("utf-8"))

    # -- ルーティング -------------------------------------------------------
    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/status":
            return self._json({**bridge.status(), "last": bridge.last_telemetry})
        if path == "/api/ports":
            try:
                return self._json({"ports": mp.list_ports()})
            except ImportError:
                return self._json({"ports": [], "error": "pyserial がありません"})
        if path == "/api/telemetry":
            return self._sse()
        return self._static(path)

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        try:
            body = self._read_json()
            if path == "/api/connect":
                return self._json(bridge.connect(body.get("port"), bool(body.get("fake"))))
            if path == "/api/disconnect":
                bridge.disconnect()
                return self._json({"ok": True})
            if path == "/api/command":
                return self._json(bridge.command(body["line"], body.get("timeout")))
        except MapConsoleError as e:
            return self._json({"error": str(e), "kind": "console"}, 400)
        except Exception as e:
            return self._json({"error": str(e), "kind": e.__class__.__name__}, 500)
        self._json({"error": "not found"}, 404)

    # -- SSE ----------------------------------------------------------------
    def _sse(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()

        q = bridge.subscribe()
        try:
            self.wfile.write(b": connected\n\n")
            self.wfile.flush()
            while True:
                try:
                    event, data = q.get(timeout=15)
                except queue.Empty:
                    self.wfile.write(b": keepalive\n\n")   # プロキシ対策
                    self.wfile.flush()
                    continue
                chunk = (f"event: {event}\n"
                         f"data: {json.dumps(data, ensure_ascii=False)}\n\n")
                self.wfile.write(chunk.encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass   # クライアントが閉じた
        finally:
            bridge.unsubscribe(q)

    # -- 静的配信 -----------------------------------------------------------
    def _static(self, path: str) -> None:
        rel = "index.html" if path in ("/", "") else path.lstrip("/")
        full = os.path.normpath(os.path.join(WEB_ROOT, rel))
        if not full.startswith(WEB_ROOT) or not os.path.isfile(full):
            return self._json({"error": "not found", "path": path}, 404)
        ctype = MIME.get(os.path.splitext(full)[1], "application/octet-stream")
        with open(full, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")   # 開発中の取り違え防止
        self.end_headers()
        self.wfile.write(data)


def main() -> int:
    ap = argparse.ArgumentParser(description="MAP調整 Web GUI のローカルサーバ")
    ap.add_argument("--port", help="シリアルポート（省略時は自動検出）")
    ap.add_argument("--http-port", type=int, default=8765, help="HTTPポート")
    ap.add_argument("--fake", action="store_true",
                    help="実機の代わりにモックECUへ繋ぐ（動作確認用）")
    ap.add_argument("--no-browser", action="store_true", help="ブラウザを開かない")
    ap.add_argument("--no-connect", action="store_true",
                    help="起動時に接続せず、GUIからポートを選ぶ")
    args = ap.parse_args()

    if not os.path.isdir(WEB_ROOT):
        print(f"エラー: {WEB_ROOT} がありません", file=sys.stderr)
        return 1

    if not args.no_connect:
        try:
            info = bridge.connect(args.port, args.fake)
            print(f"接続: {info['port']}"
                  f"{'（モックECU）' if info['fake'] else ''}"
                  f" fw={info['version'].get('fw')} proto={info['version'].get('proto')}")
        except Exception as e:
            print(f"接続できませんでした（GUIから選び直せます）: {e}", file=sys.stderr)

    url = f"http://localhost:{args.http_port}/"
    try:
        httpd = ThreadingHTTPServer(("127.0.0.1", args.http_port), Handler)
    except OSError as e:
        bridge.disconnect()       # 開いたシリアルを閉じてから抜ける
        print(f"ポート {args.http_port} を使用できません: {e}\n"
              f"別の map_gui.py が動いていないか確認するか、"
              f"--http-port で別のポートを指定してください。", file=sys.stderr)
        return 1
    httpd.daemon_threads = True
    print(f"MAP GUI: {url}  （Ctrl+C で終了）")
    if not args.no_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n終了します")
    finally:
        bridge.disconnect()
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
