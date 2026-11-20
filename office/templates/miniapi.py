"""Tiny JSON API framework on the Python standard library (do not edit).

api.py registers handlers:

    from miniapi import route, HTTPError

    @route("GET", "/api/items/<id>")
    def get_item(req):
        row = req.db.execute("SELECT * FROM items WHERE id = ?", (req.params["id"],)).fetchone()
        if not row:
            raise HTTPError(404, "not found")
        return dict(row)

A handler returns JSON-serialisable data (status 200) or a (status, data) tuple.
req.params: path parameters, req.query: query string, req.json: parsed JSON body,
req.db: sqlite3 connection (committed after the handler returns, rolled back on error).
schema.sql is executed on startup.
"""
import json
import mimetypes
import os
import re
import sqlite3
import sys
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlparse

ROOT = Path(__file__).resolve().parent
DB_PATH = os.environ.get("DB_PATH") or str(ROOT / "data.db")
MAX_BODY = 1_000_000
HIDDEN = {".py", ".sql", ".db", ".log"}
_routes = []


class HTTPError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status, self.message = status, message


class Request:
    def __init__(self, method, path, params, query, body, db):
        self.method, self.path, self.params, self.query, self.json, self.db = method, path, params, query, body, db


def route(method, pattern):
    regex = re.compile("^" + re.sub(r"<(\w+)>", r"(?P<\1>[^/]+)", pattern.rstrip("/")) + "/?$")

    def register(fn):
        _routes.append((method.upper(), regex, fn))
        return fn

    return register


def connect():
    db = sqlite3.connect(DB_PATH, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    return db


def init_db():
    schema = ROOT / "schema.sql"
    if schema.exists():
        db = connect()
        try:
            db.executescript(schema.read_text())
            db.commit()
        finally:
            db.close()


class Handler(BaseHTTPRequestHandler):
    server_version = "miniapi"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s\n" % (self.command, fmt % args))

    def _send(self, status, payload, content_type="application/json; charset=utf-8"):
        data = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _static(self, path):
        rel = unquote(path).lstrip("/") or "index.html"
        target = (ROOT / rel).resolve()
        if target.is_dir():
            target = target / "index.html"
        if ROOT not in target.parents or not target.is_file() or target.suffix in HIDDEN or target.name.startswith("."):
            return self._send(404, {"error": "not found"})
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        self._send(200, target.read_bytes(), ctype)

    def _handle(self):
        url = urlparse(self.path)
        if not url.path.startswith("/api/") and url.path != "/api":
            if self.command in ("GET", "HEAD"):
                return self._static(url.path)
            return self._send(405, {"error": "method not allowed"})
        allowed = False
        for method, regex, fn in _routes:
            match = regex.match(url.path)
            if not match:
                continue
            allowed = True
            if method != self.command:
                continue
            body = None
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                return self._send(413, {"error": "request too large"})
            if length:
                try:
                    body = json.loads(self.rfile.read(length))
                except ValueError:
                    return self._send(400, {"error": "invalid JSON body"})
            db = connect()
            try:
                params = {k: unquote(v) for k, v in match.groupdict().items()}
                result = fn(Request(self.command, url.path, params, dict(parse_qsl(url.query)), body, db))
                db.commit()
            except HTTPError as e:
                db.rollback()
                return self._send(e.status, {"error": e.message})
            except Exception:
                db.rollback()
                traceback.print_exc()
                return self._send(500, {"error": "internal server error"})
            finally:
                db.close()
            if isinstance(result, tuple) and len(result) == 2 and isinstance(result[0], int):
                return self._send(result[0], result[1])
            return self._send(200, result)
        self._send(405 if allowed else 404, {"error": "method not allowed" if allowed else "not found"})

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = _handle


def run():
    init_db()
    host, port = os.environ.get("HOST", "127.0.0.1"), int(os.environ.get("PORT", "8000"))
    print(f"Serving on http://{host}:{port}", flush=True)
    ThreadingHTTPServer((host, port), Handler).serve_forever()
