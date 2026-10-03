"""Small web UI + JSON API for the headless sender (stdlib only)."""

from __future__ import annotations

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional

from .sender import Sender

_STATIC = Path(__file__).resolve().parent / "static"
_SAFE_NAME = re.compile(r"^[A-Za-z0-9_.\- ]+\.dbc$")


def make_server(sender: Sender, dbc_dir: Path, port: int, host: str = "0.0.0.0") -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        server_version = "candbcemu"

        def log_message(self, fmt, *args):  # keep the journal quiet
            pass

        # ---- helpers ----
        def _json(self, payload, status=200):
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > 65536:
                return {}
            try:
                data = json.loads(self.rfile.read(length))
            except ValueError:
                return {}
            return data if isinstance(data, dict) else {}

        def _dbcs(self) -> list:
            return sorted(p.name for p in dbc_dir.glob("*.dbc")) if dbc_dir.is_dir() else []

        # ---- routes ----
        def do_GET(self):
            if self.path in ("/", "/index.html"):
                body = (_STATIC / "index.html").read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif self.path == "/api/state":
                state = sender.snapshot()
                state["dbcs"] = self._dbcs()
                self._json(state)
            else:
                self.send_error(404)

        def do_POST(self):
            data = self._body()
            path = self.path
            try:
                if path == "/api/dbc":
                    name = str(data.get("name", ""))
                    if not _SAFE_NAME.match(name) or not (dbc_dir / name).is_file():
                        return self._json({"error": "unknown DBC"}, 400)
                    sender.load_dbc(str(dbc_dir / name))
                elif path == "/api/connect":
                    sender.start(str(data.get("interface", "socketcan")), str(data.get("channel", "can0")),
                                 int(data.get("bitrate", 500000)), bool(data.get("setBitrate", True)))
                elif path == "/api/disconnect":
                    sender.stop()
                elif path == "/api/all":
                    sender.set_all_enabled(bool(data.get("enabled")))
                elif path == "/api/message":
                    if not sender.set_message(int(data["key"]), data.get("enabled"), data.get("cycle")):
                        return self._json({"error": "unknown message"}, 404)
                elif path == "/api/signal":
                    if not sender.set_signal(int(data["key"]), str(data["name"]), data.get("kind"),
                                             data.get("value"), data.get("period")):
                        return self._json({"error": "unknown signal"}, 404)
                else:
                    return self.send_error(404)
            except (KeyError, ValueError, TypeError) as exc:
                return self._json({"error": f"bad request: {exc}"}, 400)
            except Exception as exc:  # noqa: BLE001 - e.g. interface missing
                return self._json({"error": str(exc)}, 500)
            self._json({"ok": True})

    return ThreadingHTTPServer((host, port), Handler)
