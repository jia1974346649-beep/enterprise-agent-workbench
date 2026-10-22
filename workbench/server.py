"""Loopback-only HTTP adapter for the anonymous workflow demonstration."""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from .domain import DomainError, Workbench


DEFAULT_ACTOR = "alpha-member"
MAX_BODY = 16 * 1024
STATIC_FILES = {"/": "index.html", "/index.html": "index.html", "/styles.css": "styles.css", "/app.js": "app.js"}


def make_server(host: str = "127.0.0.1", port: int = 8765, app: Workbench | None = None) -> ThreadingHTTPServer:
    """Create a testable local server; never expose demo identities to a LAN."""
    if host not in {"127.0.0.1", "localhost"}:
        raise ValueError("This demonstration only binds to 127.0.0.1 or localhost.")
    application = app if app is not None else Workbench()
    web_root = Path(__file__).resolve().parent.parent / "web"

    class Handler(BaseHTTPRequestHandler):
        server_version = "AgentWorkbench/1.0"

        def setup(self) -> None:
            super().setup()
            self.connection.settimeout(5)

        def log_message(self, format: str, *args: object) -> None:
            # Request text, user input, headers and model responses are not logged.
            return

        def _headers(self, status: int, content_type: str, length: int) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(length))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; base-uri 'none'; frame-ancestors 'none'; form-action 'none'")
            self.end_headers()

        def _json(self, status: int, payload: dict) -> None:
            content = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
            self._headers(status, "application/json; charset=utf-8", len(content))
            self.wfile.write(content)

        def _error(self, status: int, code: str, message: str) -> None:
            self._json(status, {"error": {"code": code, "message": message}})

        def _check_local_request(self, writing: bool = False) -> bool:
            allowed_hosts = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
            host_header = self.headers.get("Host", "")
            if host_header not in allowed_hosts:
                self._error(403, "host_not_allowed", "请通过本机 localhost 或 127.0.0.1 打开演示。")
                return False
            if writing:
                origin = self.headers.get("Origin")
                if origin:
                    try:
                        parsed = urlsplit(origin)
                    except ValueError:
                        self._error(403, "origin_not_allowed", "来源格式不合法。")
                        return False
                    if parsed.scheme != "http" or parsed.netloc != host_header:
                        self._error(403, "origin_not_allowed", "此演示仅接受同源操作。")
                        return False
                if self.headers.get("Sec-Fetch-Site") == "cross-site":
                    self._error(403, "origin_not_allowed", "不接受跨站操作。")
                    return False
            return True

        def _path(self) -> str | None:
            try:
                return urlsplit(self.path).path
            except ValueError:
                self._error(400, "invalid_path", "请求路径格式不合法。")
                return None

        def _actor(self) -> str:
            return self.headers.get("X-Demo-Actor", DEFAULT_ACTOR)

        def _body(self) -> dict | None:
            if self.headers.get("Transfer-Encoding"):
                self._error(400, "invalid_body", "不支持分块请求体。")
                return None
            if self.headers.get_content_type() != "application/json":
                self._error(415, "json_required", "请使用 application/json 请求。")
                return None
            try:
                size = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                self._error(400, "invalid_body", "请求长度不合法。")
                return None
            if size <= 0 or size > MAX_BODY:
                if size > MAX_BODY:
                    # Drain a bounded, already-sent body before rejecting it.
                    # Closing a socket with unread input can reset a Windows
                    # client before it receives our structured error response.
                    try:
                        self.rfile.read(min(size, MAX_BODY * 4))
                    except OSError:
                        pass
                self._error(413 if size > MAX_BODY else 400, "invalid_body", "请求体为空或过大。")
                return None
            try:
                body = json.loads(self.rfile.read(size).decode("utf-8"), parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
            except (ValueError, UnicodeError, RecursionError):
                self._error(400, "invalid_json", "JSON 格式不合法。")
                return None
            if not isinstance(body, dict):
                self._error(400, "object_required", "请求内容必须是 JSON 对象。")
                return None
            return body

        def do_GET(self) -> None:
            if not self._check_local_request():
                return
            path = self._path()
            if path is None:
                return
            try:
                if path == "/api/health":
                    self._json(200, {"ready": True, "mode": application.state(DEFAULT_ACTOR)["mode"], "authentication": "fixed-local-demo-identities"})
                elif path == "/api/bootstrap":
                    self._json(200, application.bootstrap(self._actor()))
                elif path == "/api/state":
                    self._json(200, application.state(self._actor()))
                elif path == "/api/audit":
                    self._json(200, application.audit(self._actor()))
                elif path in STATIC_FILES:
                    target = web_root / STATIC_FILES[path]
                    if not target.is_file():
                        self._error(503, "ui_unavailable", "界面资源尚未就绪。")
                        return
                    content = target.read_bytes()
                    content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
                    self._headers(200, content_type + "; charset=utf-8", len(content))
                    self.wfile.write(content)
                else:
                    self._error(404, "not_found", "没有这个入口。")
            except DomainError as error:
                self._error(error.status, error.code, error.message)
            except Exception:
                self._error(500, "internal_error", "演示服务暂时无法处理请求，请重新尝试。")

        def do_POST(self) -> None:
            if not self._check_local_request(writing=True):
                return
            body = self._body()
            if body is None:
                return
            path = self._path()
            if path is None:
                return
            try:
                if path == "/api/run":
                    if set(body) - {"message", "scenario"}:
                        self._error(400, "unknown_field", "请求包含不支持的字段。")
                        return
                    self._json(200, application.run(self._actor(), body.get("message"), body.get("scenario")))
                elif path.startswith("/api/approvals/") and path.count("/") == 3:
                    if set(body) != {"decision"}:
                        self._error(400, "unknown_field", "审批请求只接受 decision。")
                        return
                    approval_id = path.rsplit("/", 1)[1]
                    self._json(200, application.decide(self._actor(), approval_id, body["decision"]))
                elif path == "/api/reset":
                    if body:
                        self._error(400, "unknown_field", "重置请求不接受额外字段。")
                        return
                    self._json(200, application.reset(self._actor()))
                else:
                    self._error(404, "not_found", "没有这个操作入口。")
            except DomainError as error:
                self._error(error.status, error.code, error.message)
            except Exception:
                self._error(500, "internal_error", "演示服务暂时无法处理请求，请重新尝试。")

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    return server


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Run the anonymous Agent Workbench locally.")
    parser.add_argument("--host", choices=("127.0.0.1", "localhost"), default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    # Provider configuration is resolved explicitly by the factory if available.
    mode = os.environ.get("WORKBENCH_MODE", "offline")
    provider = None
    if mode != "offline":
        from .providers import provider_from_environment
        provider = provider_from_environment()
    server = make_server(args.host, args.port, Workbench(provider=provider))
    print(f"Agent Workbench: http://{args.host}:{server.server_port}", flush=True)
    print("Local synthetic-data demo; fixed identities are not production authentication.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
