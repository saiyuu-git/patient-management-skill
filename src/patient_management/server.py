"""Local web service for the patient dashboard (stdlib only).

Routes (GET):
  /patient/<id>                      full server-rendered page
  /api/patient/<id>/view             Dashboard View JSON (the only data contract)
  /api/patient/<id>/fragments        re-rendered module HTML for in-place updates
  /api/patient/<id>/events           SSE: "update" when the patient's data changes
  /api/health                        service status (no patient data)
  /static/app.css|app.js             local assets
POST /api/patient/<id>/task-completion: same-origin explicit user checkbox action.
The browser never touches SQLite; each request opens its own connection.
"""
from __future__ import annotations

import ipaddress
import json
import re
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import dashboard, render, schema, state, tasks
from .persistence import DB, MIGRATIONS, PMError

STATIC = Path(__file__).with_name("static")
_PID = re.compile(r"^pt_[a-z0-9]{1,40}$")
_ROUTE = re.compile(r"^/(?:api/)?patient/([^/]+)(/view|/fragments|/events)?/?$")
SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
                               "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'",
    "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer", "Cache-Control": "no-store",
}
TYPES = {".css": "text/css; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".png": "image/png", '.svg': 'image/svg+xml'}


def check_host(host: str, allow_remote: bool = False) -> str:
    """Loopback only unless explicitly allowed."""
    try:
        loopback = host == "localhost" or ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = False
    if not loopback and not allow_remote:
        raise PMError(f"refusing to listen on {host}: localhost only (use --allow-remote deliberately)")
    return host


def _view(db_path: str, pid: str, include_background_labs: bool = False) -> dict:
    db = DB(db_path)
    try:
        if db.get_patient(pid) is None:
            raise LookupError(pid)
        view = dashboard.build_view(db, pid, include_background_labs=include_background_labs)
        view["fingerprint"] = db.fingerprint(pid)
        return view
    finally:
        db.close()


def respond(db_path: str, path: str) -> tuple[int, dict, bytes]:
    """Pure request handler (testable without sockets) for every non-streaming route; always adds security headers."""
    code, headers, body = _route(db_path, path)
    return code, {**SECURITY_HEADERS, **headers}, body


def complete_task(db_path: str, path: str, headers: dict, body: bytes) -> tuple[int, dict, bytes]:
    """Same-origin JSON mutation, scoped to one patient and one explicit task."""
    headers = {k.lower(): v for k, v in headers.items()}
    m = re.fullmatch(r"/api/patient/(pt_[a-z0-9]{1,40})/task-completion", path)
    if not m:
        return _json(404, {"error": "接口不存在"})
    try:
        host = urlsplit("http://" + headers.get("host", ""))
        origin = urlsplit(headers.get("origin", ""))
        local = host.hostname == "localhost" or ipaddress.ip_address(host.hostname or "").is_loopback
    except ValueError:
        local = False
        origin = host = None
    if (not local or not origin or origin.scheme != "http" or origin.netloc != host.netloc or
            headers.get("sec-fetch-site") == "cross-site" or headers.get("x-pm-action") != "task-completion"):
        return _json(403, {"error": "只允许本页面修改待办"})
    if headers.get("content-type", "").split(";")[0] != "application/json" or len(body) > 2048:
        return _json(400, {"error": "无效请求"})
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return _json(400, {"error": "无效 JSON"})
    if schema.validate(payload, "task-completion-request.schema.json"):
        return _json(400, {"error": "待办数据格式不正确"})
    db = DB(db_path)
    try:
        if not db.get_patient(m[1]) or not db.get("tasks", m[1], payload["task_id"]):
            return _json(404, {"error": "待办不存在"})
        task = db.get("tasks", m[1], payload["task_id"])
        if task["origin"] != "explicit" or task["status"] == "cancelled":
            return _json(409, {"error": "该项不属于可修改的明确待办"})
        tasks.set_completion(db, m[1], payload["task_id"], payload["completed"])
        return _json(200, {"status": "saved"})
    except PMError:
        return _json(409, {"error": "待办更新失败"})
    except Exception:
        return _json(500, {"error": "保存失败，请重试"})
    finally:
        db.close()


def _route(db_path: str, path: str) -> tuple[int, dict, bytes]:
    include_background_labs = parse_qs(urlsplit(path).query).get('include_background_labs') == ['1']
    path = path.split("?", 1)[0]
    if path in ("/", "/favicon.ico"):  # no patient list on purpose
        return (204, {}, b"") if path != "/" else _text(200, "Patient Management · open /patient/<patient_id>")
    if path == "/api/health":
        return _json(200, {"status": "ok", "db_version": len(MIGRATIONS), "view_version": dashboard.VIEW_VERSION})
    if path.startswith("/static/"):
        f = STATIC / path[len("/static/"):]
        if f.parent != STATIC or f.suffix not in TYPES or not f.is_file():
            return _text(404, "not found")
        return 200, {"Content-Type": TYPES[f.suffix]}, f.read_bytes()
    m = _ROUTE.match(path)
    if not m:
        return _text(404, "not found")
    pid, sub = m.group(1), m.group(2)
    if not _PID.match(pid):
        return _text(400, "invalid patient id")
    if path.startswith("/patient/") and sub:
        return _text(404, "not found")
    try:
        view = _view(db_path, pid, include_background_labs)
    except LookupError:
        return _text(404, "patient not found")
    if sub == "/view":
        return _json(200, view)
    if sub == "/fragments":
        return _json(200, render.fragments(view))
    return 200, {"Content-Type": "text/html; charset=utf-8"}, render.page(view).encode()


def events(db_path: str, pid: str, poll: float = 1.5, heartbeat: float = 15.0, limit: int | None = None):
    """SSE byte chunks: an 'update' event whenever the patient's fingerprint changes."""
    db = DB(db_path)
    try:
        last, beat, sent = db.fingerprint(pid), time.monotonic(), 0
        yield b"retry: 3000\n\n"
        while limit is None or sent < limit:
            time.sleep(poll)
            fp = db.fingerprint(pid)
            if fp != last:
                last = fp
                sent += 1
                yield f"event: update\ndata: {json.dumps({'fingerprint': fp})}\n\n".encode()
            elif time.monotonic() - beat > heartbeat:
                beat = time.monotonic()
                yield b": keep-alive\n\n"
    finally:
        db.close()


def _json(code: int, obj) -> tuple[int, dict, bytes]:
    return code, {"Content-Type": "application/json; charset=utf-8"}, json.dumps(obj, ensure_ascii=False).encode()


def _text(code: int, msg: str) -> tuple[int, dict, bytes]:
    return code, {"Content-Type": "text/plain; charset=utf-8"}, msg.encode()


def make_handler(db_path: str):
    class Handler(BaseHTTPRequestHandler):
        server_version = "pm-dashboard"

        def log_message(self, fmt, *args):  # no patient URLs in logs
            pass

        def _head(self, code, headers):
            self.send_response(code)
            for k, v in {**SECURITY_HEADERS, **headers}.items():
                self.send_header(k, v)
            self.end_headers()

        def do_GET(self):
            m = _ROUTE.match(self.path.split("?", 1)[0])
            if m and m.group(2) == "/events" and self.path.startswith("/api/"):
                pid = m.group(1)
                if not _PID.match(pid) or not _exists(db_path, pid):
                    code, headers, body = _text(404, "patient not found")
                    self._head(code, headers)
                    self.wfile.write(body)
                    return
                self._head(200, {"Content-Type": "text/event-stream", "Connection": "keep-alive"})
                try:
                    for chunk in events(db_path, pid):
                        self.wfile.write(chunk)
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    return
                return
            code, headers, body = respond(db_path, self.path)
            self._head(code, headers)
            self.wfile.write(body)

        def do_POST(self):
            try:
                size = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                size = -1
            if size < 1 or size > 2048:
                self._head(400, {"Content-Type": "application/json"})
                self.wfile.write(b'{"error":"invalid request size"}')
                return
            body = self.rfile.read(size)
            code, headers, result = complete_task(db_path, self.path, dict(self.headers), body)
            self._head(code, headers)
            self.wfile.write(result)

    return Handler


def _exists(db_path: str, pid: str) -> bool:
    db = DB(db_path)
    try:
        return db.get_patient(pid) is not None
    finally:
        db.close()


def serve(db_path: str, host: str = "127.0.0.1", port: int = 8765, allow_remote: bool = False) -> None:
    check_host(host, allow_remote)
    httpd = ThreadingHTTPServer((host, port), make_handler(db_path))
    httpd.daemon_threads = True
    print(json.dumps({"listening": f"http://{host}:{port}", "patient_url": f"http://{host}:{port}/patient/<patient_id>"}), flush=True)
    httpd.serve_forever()
