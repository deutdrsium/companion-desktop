"""Loopback-only subtitle feed for a VTube Studio Web Item (1024 x 200)."""

import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import threading

HOST = "127.0.0.1"
PAGE = Path(__file__).with_name("assets") / "subtitle.html"


def item_page(endpoint, name, voice_font=None):
    family = json.dumps(voice_font or "方正新书宋简体")
    return (PAGE.read_text(encoding="utf-8")
            .replace("ENDPOINT_JSON", json.dumps(endpoint))
            .replace("NAME_JSON", json.dumps(name, ensure_ascii=False).replace("<", "\\u003c"))
            .replace("VOICE_FONT", family))


def load_token(path):
    path = Path(path)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(secrets.token_hex(24), encoding="ascii")
    token = path.read_text(encoding="ascii").strip()
    if len(token) != 48 or any(c not in "0123456789abcdef" for c in token):
        raise ValueError("字幕令牌无效")
    return token


class SubtitleServer:
    def __init__(self, token_path, port=18676, name="", voice_font=None):
        token = load_token(token_path)
        self.lock = threading.Lock()
        self.data = {"seq": 0, "state": "idle", "text": "", "progress": 0.0, "style": "plate",
                     "size": 1.0, "hold_ms": 1800, "nameplate": True}
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                is_page = hmac.compare_digest(self.path, "/" + token + "/")
                is_state = hmac.compare_digest(self.path, "/" + token + "/state")
                if not (is_page or is_state):
                    self.send_error(404)
                    return
                if is_page:
                    body = item_page(owner.state_url, name, voice_font).encode("utf-8")
                    content_type = "text/html; charset=utf-8"
                else:
                    with owner.lock:
                        body = json.dumps(owner.data, ensure_ascii=False).encode("utf-8")
                    content_type = "application/json; charset=utf-8"
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Access-Control-Allow-Origin", "null")
                self.end_headers()
                self.wfile.write(body)

            def do_OPTIONS(self):
                if not hmac.compare_digest(self.path, "/" + token + "/state"):
                    self.send_error(404)
                    return
                self.send_response(204)
                self.send_header("Access-Control-Allow-Origin", "null")
                self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
                self.send_header("Access-Control-Allow-Private-Network", "true")
                self.end_headers()

            def log_message(self, *_args):
                pass

        self.server = ThreadingHTTPServer((HOST, port), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self.item_url = f"http://{HOST}:{self.port}/{token}/"
        self.state_url = self.item_url + "state"
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.2},
                                       daemon=True, name="subtitles")
        self.thread.start()

    def set_style(self, style=None, size=None, hold_ms=None, nameplate=None):
        with self.lock:
            if style in ("plate", "outline"):
                self.data["style"] = style
            if size is not None:
                self.data["size"] = round(float(size), 2)
            if hold_ms is not None:
                self.data["hold_ms"] = int(hold_ms)
            if nameplate is not None:
                self.data["nameplate"] = bool(nameplate)

    def set_state(self, state, text=None, progress=None):
        """speaking (new seq when the text changes) | idle (hold, then fade) | interrupted (collapse)."""
        with self.lock:
            if state == "speaking" and text is not None and text != self.data["text"]:
                self.data["seq"] += 1
            self.data["state"] = state
            if text is not None:
                self.data["text"] = str(text)
            if state == "interrupted":
                self.data["text"] = ""   # never leave the old line hanging after a stop
                self.data["progress"] = 0.0
            if progress is not None:
                self.data["progress"] = round(max(0.0, min(1.0, progress)), 3)

    def snapshot(self):
        with self.lock:
            return dict(self.data)

    def close(self):
        self.server.shutdown()
        self.server.server_close()
