"""Minimal ingress-only GET UI. No mutation or deployment endpoints."""
from __future__ import annotations
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from .check import check, file_diff
from .plan import prepare_plan

PORT = 8099
INGRESS_PROXY = "172.30.32.2"
STATIC_DIR = Path(__file__).resolve().parent

class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        dev = os.environ.get("SMARTHOME_DEV_ALLOW_LOCAL") == "1"
        if not dev and self.client_address[0] != INGRESS_PROXY:
            self.send_error(403)
            return
        parsed = urlsplit(self.path)
        path = parsed.path
        if path in ("", "/", "/index.html"):
            self._send(200, "text/html; charset=utf-8", (STATIC_DIR / "index.html").read_bytes())
        elif path == "/app.js":
            self._send(200, "text/javascript; charset=utf-8", (STATIC_DIR / "app.js").read_bytes())
        elif path == "/api/status":
            try:
                payload = check()
                self._send(200, "application/json", json.dumps(payload, ensure_ascii=False).encode())
            except Exception as error:
                print("SmartHome Deploy check failed:", type(error).__name__, str(error), flush=True)
                self._send(503, "application/json", json.dumps({"error": "Не удалось выполнить проверку. См. журнал приложения."}, ensure_ascii=False).encode())
        elif path == "/api/plan":
            try:
                payload = prepare_plan()
                self._send(200, "application/json", json.dumps(payload, ensure_ascii=False).encode())
            except ValueError:
                self._send(400, "application/json", b'{"error":"Invalid planner configuration"}')
            except Exception as error:
                print("SmartHome Deploy plan failed:", type(error).__name__, str(error), flush=True)
                self._send(503, "application/json", json.dumps({"error": "Не удалось подготовить план. См. журнал приложения."}, ensure_ascii=False).encode())
        elif path == "/api/diff":
            args = parse_qs(parsed.query, keep_blank_values=True)
            if set(args) != {"path", "commit"} or len(args["path"]) != 1 or len(args["commit"]) != 1:
                self._send(400, "application/json", b'{"error":"Invalid parameters"}')
                return
            try:
                payload = file_diff(args["path"][0], args["commit"][0])
                self._send(200, "application/json", json.dumps(payload, ensure_ascii=False).encode())
            except ValueError as error:
                self._send(400, "application/json", json.dumps({"error": str(error)}, ensure_ascii=False).encode())
            except Exception as error:
                print("SmartHome Deploy diff failed:", type(error).__name__, str(error), flush=True)
                self._send(503, "application/json", json.dumps({"error": "Не удалось получить различия. См. журнал приложения."}, ensure_ascii=False).encode())
        else:
            self.send_error(404)

    def do_POST(self) -> None:
        self.send_error(405, "Read-only application")

    def _send(self, status: int, content_type: str, payload: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'self'; style-src 'unsafe-inline'; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'self'")
        self.end_headers()
        self.wfile.write(payload)

if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
