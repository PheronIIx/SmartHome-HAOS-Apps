"""Minimal ingress-only GET UI. No mutation or deployment endpoints."""
from __future__ import annotations
import json
import os
import secrets
import hmac
from . import backup_service
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from .check import check, file_diff
from .plan import prepare_plan

PORT = 8099
INGRESS_PROXY = "172.30.32.2"
STATIC_DIR = Path(__file__).resolve().parent
CSRF_TOKEN = secrets.token_urlsafe(32)
MAX_REQUEST_BYTES = 2048

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
        elif path == "/api/security":
            self._send(200, "application/json", json.dumps({"csrf_token": CSRF_TOKEN}).encode())
        elif path == "/api/backups":
            try:
                self._send(200, "application/json", json.dumps(backup_service.list_backups()).encode())
            except Exception as error:
                print("Backup listing failed:", type(error).__name__, str(error), flush=True)
                self._send(503, "application/json", b'{"error":"Unable to list backups"}')
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
        # HA Ingress is the authenticated admin boundary (panel_admin=true).
        # A per-process CSRF secret plus same-origin metadata protects POST.
        if os.environ.get("SMARTHOME_DEV_ALLOW_LOCAL") != "1" and self.client_address[0] != INGRESS_PROXY:
            self.send_error(403)
            return
        parsed = urlsplit(self.path)
        if parsed.path not in ("/api/backups/create", "/api/backups/verify", "/api/backups/rehearse"):
            self.send_error(404)
            return
        if self.headers.get("Sec-Fetch-Site") not in ("same-origin", "none"):
            self.send_error(403)
            return
        origin = self.headers.get("Origin")
        host = self.headers.get("X-Forwarded-Host") or self.headers.get("Host", "")
        proto = self.headers.get("X-Forwarded-Proto") or "https"
        if origin and origin != proto + "://" + host:
            self.send_error(403)
            return
        if not hmac.compare_digest(self.headers.get("X-CSRF-Token", ""), CSRF_TOKEN):
            self.send_error(403)
            return
        if self.headers.get("Content-Type", "").split(";", 1)[0] != "application/json":
            self.send_error(415)
            return
        try:
            length = int(self.headers.get("Content-Length", "-1"))
            if not 0 <= length <= MAX_REQUEST_BYTES:
                self.send_error(413)
                return
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict):
                raise ValueError("Expected JSON object")
            if parsed.path == "/api/backups/create":
                if body:
                    raise ValueError("Unexpected create parameters")
                payload = backup_service.create_manual_backup()
            else:
                if set(body) != {"backup"} or not isinstance(body["backup"], str):
                    raise ValueError("Invalid backup request")
                if parsed.path == "/api/backups/verify":
                    payload = backup_service.verify_existing_backup(body["backup"])
                else:
                    payload = backup_service.rehearse_existing_backup(body["backup"])
            self._send(200, "application/json", json.dumps(payload, ensure_ascii=False).encode())
        except ValueError as error:
            self._send(400, "application/json", json.dumps({"error": str(error)}, ensure_ascii=False).encode())
        except Exception as error:
            print("Backup operation failed:", type(error).__name__, str(error), flush=True)
            self._send(503, "application/json", b'{"error":"Backup operation failed; see app log"}')

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
