"""Loopback-only HTTP API and static WebUI, using Python's standard library."""

from __future__ import annotations

import hmac
import json
import logging
import mimetypes
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

LOGGER = logging.getLogger(__name__)
MAX_BODY = 65536


class AppServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, address, manager, web_root: Path, identity: dict):
        self.manager = manager
        self.web_root = web_root.resolve()
        self.identity = identity
        self.token = secrets.token_urlsafe(32)
        self.shutting_down = False
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    server: AppServer
    server_version = "JumperManager/1.0"

    def setup(self):
        super().setup()
        self.connection.settimeout(10)
        self._body_consumed = False

    def log_message(self, fmt, *args):
        if str(args[1] if len(args) > 1 else "").startswith(("4", "5")):
            LOGGER.warning("HTTP %s", fmt % args)

    def _safe_origin(self):
        port = self.server.server_address[1]
        allowed = {f"127.0.0.1:{port}", f"localhost:{port}"}
        host = self.headers.get("Host", "").lower()
        if host not in allowed:
            self._json(403, {"error": "WebUI 仅接受本机地址访问。"})
            return False
        origin = self.headers.get("Origin")
        if origin and origin.lower() not in {"http://" + item for item in allowed}:
            self._json(403, {"error": "拒绝跨站请求。"})
            return False
        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            self._json(403, {"error": "拒绝跨站请求。"})
            return False
        return True

    def _headers(self, code, content_type, length):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        # The frontend draws SVG and positions nodes using style attributes.
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        self.end_headers()

    def _json(self, code, value):
        if code >= 400:
            self._drain_rejected_body()
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self._headers(code, "application/json; charset=utf-8", len(body))
        if self.command != "HEAD":
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _drain_rejected_body(self):
        # Closing a socket with unread request data can turn a useful 4xx into
        # TCP RST on Windows. Drain small, bounded requests before rejecting.
        if self._body_consumed or self.command not in {"POST", "PUT", "DELETE"}:
            return
        self._body_consumed = True
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if 0 < length <= MAX_BODY:
                self.connection.settimeout(2)
                self.rfile.read(length)
        except (ValueError, OSError):
            pass
        finally:
            self.connection.settimeout(10)

    def _body(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise ValueError("无效的请求长度。")
        if length < 0 or length > MAX_BODY:
            raise ValueError("请求内容过大（上限 64 KB）。")
        if not length:
            return {}
        if self.headers.get_content_type() != "application/json":
            raise ValueError("请求必须使用 application/json。")
        try:
            raw = self.rfile.read(length)
            self._body_consumed = True
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ValueError("请求不是有效的 JSON。") from None
        if not isinstance(value, dict):
            raise ValueError("请求内容必须是 JSON 对象。")
        return value

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        if not self._safe_origin():
            return
        path = urlsplit(self.path).path
        try:
            if path == "/api/ping":
                self._json(200, self.server.identity)
            elif path == "/api/session":
                self._json(200, {"token": self.server.token})
            elif path == "/api/state":
                state = self.server.manager.state()
                state["server"] = {"url": f"http://127.0.0.1:{self.server.server_address[1]}", "shutting_down": self.server.shutting_down}
                self._json(200, state)
            elif path.startswith("/api/mappings/") and path.endswith("/logs") and len(path.split("/")) == 5:
                mapping_id = path.split("/")[3]
                item = next((m for m in self.server.manager.state()["mappings"] if m["id"] == mapping_id), None)
                if item is None:
                    raise KeyError(mapping_id)
                self._json(200, {"logs": item.get("logs", [])})
            elif path.startswith("/api/"):
                self._json(404, {"error": "接口不存在。"})
            else:
                self._static(path)
        except Exception as error:
            self._error(error)

    def _static(self, path):
        relative = unquote(path).lstrip("/") or "index.html"
        resolved = (self.server.web_root / relative).resolve()
        if not resolved.is_relative_to(self.server.web_root) or not resolved.is_file():
            self._json(404, {"error": "页面不存在。"})
            return
        if resolved.suffix.lower() not in {".html", ".css", ".js", ".svg", ".png", ".ico", ".woff2"}:
            self._json(404, {"error": "资源不存在。"})
            return
        content = resolved.read_bytes()
        content_type = {".js": "text/javascript", ".css": "text/css", ".html": "text/html", ".svg": "image/svg+xml"}.get(resolved.suffix.lower(), mimetypes.guess_type(str(resolved))[0] or "application/octet-stream")
        if content_type.startswith("text/"):
            content_type += "; charset=utf-8"
        self._headers(200, content_type, len(content))
        if self.command != "HEAD":
            self.wfile.write(content)

    def do_POST(self):
        self._mutate()

    def do_PUT(self):
        self._mutate()

    def do_DELETE(self):
        self._mutate()

    def _mutate(self):
        if not self._safe_origin():
            return
        token = self.headers.get("X-Jumper-Token", "")
        if not token.isascii() or not hmac.compare_digest(token, self.server.token):
            self._json(403, {"error": "页面会话已失效，请刷新页面。"})
            return
        if self.server.shutting_down:
            self._json(503, {"error": "程序正在关闭。"})
            return
        try:
            body = self._body()
            path = urlsplit(self.path).path.rstrip("/")
            manager = self.server.manager
            code = 200
            if path == "/api/hosts/refresh" and self.command == "POST":
                result = {"hosts": manager.refresh_hosts()}
            elif path == "/api/hosts/probe" and self.command == "POST":
                result = manager.probe_host(body.get("alias", ""))
            elif path == "/api/preview" and self.command == "POST":
                result = manager.preview(body)
            elif path == "/api/mappings" and self.command == "POST":
                result = manager.create(body)
                code = 201
            elif path == "/api/shutdown" and self.command == "POST":
                self.server.shutting_down = True
                self._json(200, {"ok": True})
                threading.Thread(target=self.server.shutdown, daemon=True, name="http-shutdown").start()
                return
            elif path.startswith("/api/mappings/"):
                parts = path.split("/")
                mapping_id = parts[3]
                action = parts[4] if len(parts) == 5 else None
                if len(parts) == 4 and self.command == "PUT":
                    result = manager.update(mapping_id, body)
                elif len(parts) == 4 and self.command == "DELETE":
                    manager.delete(mapping_id)
                    result = {"ok": True}
                elif self.command == "POST" and action in {"start", "stop", "check"}:
                    result = getattr(manager, action)(mapping_id)
                else:
                    self._json(404, {"error": "接口不存在。"})
                    return
            else:
                self._json(404, {"error": "接口不存在。"})
                return
            self._json(code, result)
        except Exception as error:
            self._error(error)

    def _error(self, error):
        if isinstance(error, KeyError):
            self._json(404, {"error": "映射不存在或已被删除。"})
        elif isinstance(error, ValueError):
            self._json(400, {"error": str(error)})
        elif isinstance(error, (RuntimeError, OSError)):
            self._json(409, {"error": str(error)})
        else:
            LOGGER.exception("Unhandled API error")
            self._json(500, {"error": "内部错误，请查看 data/server.log。"})
