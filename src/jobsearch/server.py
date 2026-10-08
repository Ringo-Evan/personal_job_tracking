"""The tracker's page (plan-tracker.md Phase T2): `python -m jobsearch serve`, then http://127.0.0.1:8787.

A standard-library HTTP server on 127.0.0.1 serving the page ported from the claude.ai tracker and a small JSON API over
`Tracker`, writing as the owner ("you"). Only this PC can use it: every request must name a local Host (which also
stops DNS rebinding), and every write needs the X-Tracker header and, when the browser sends one, a local Origin. No
CORS headers are ever sent, so no other website open in the browser can write. The reports and the files they link
to are served read-only from out/.
"""

from __future__ import annotations

import json
import mimetypes
import os
import re
import socket
import threading
import webbrowser
from datetime import date
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from jobsearch.config import ROOT
from jobsearch.tracker import (
    CLOSED_STAGES,
    COLLECTIONS,
    OWNER,
    STAGES,
    ConflictError,
    Tracker,
    TrackerError,
    collection,
    to_csv,
)

WEB = Path(__file__).parent / "web"
OUT = ROOT / "out"
DEFAULT_PORT = 8787
MAX_BODY = 20_000_000  # a CSV of every company is about 1 MB
_REPORT_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TEXT = (".md", ".txt", ".csv", ".json", ".jsonl")  # shown in the browser rather than downloaded


def schema() -> dict[str, Any]:
    """What the page needs to draw every tab: fields, labels, groups, options, default columns."""
    return {
        "stages": list(STAGES),
        "closed": [s for s in STAGES if s in CLOSED_STAGES],
        "collections": [
            {
                "name": c.name,
                "title": c.title,
                "id": c.id_key,
                "prefix": c.prefix,
                "columns": list(c.columns),
                "fields": [
                    {"key": f.key, "label": f.label, "kind": f.kind, "group": f.group, "options": list(f.options)}
                    for f in c.fields
                ],
            }
            for c in COLLECTIONS.values()
        ],
    }


def fingerprint(tracker: Tracker, name: str) -> str:
    """Changes whenever a row in the collection is added, changed or deleted; the page polls it."""
    d = tracker.root / name
    if not d.is_dir():
        return "0"
    count = newest = size = 0
    with os.scandir(d) as entries:
        for e in entries:
            if e.name.endswith(".json"):
                st = e.stat()
                count, newest, size = count + 1, max(newest, st.st_mtime_ns), size + st.st_size
    return f"{count}-{newest}-{size}"


def newest_report(out_root: Path) -> str | None:
    days = [p.name for p in out_root.iterdir() if _REPORT_DAY.match(p.name) and (p / "digest.html").is_file()]
    return f"/out/{max(days)}/digest.html" if days else None


class TrackerServer(ThreadingHTTPServer):
    daemon_threads = True
    # On Windows SO_REUSEADDR lets a second server bind a port that's already in use (found 2026-10-07: two `serve`
    # processes on one port); there, take the port exclusively instead, so the second one fails cleanly.
    allow_reuse_address = os.name != "nt"

    def server_bind(self) -> None:
        if os.name == "nt":
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

    def __init__(self, tracker: Tracker, port: int = DEFAULT_PORT, out_root: Path = OUT, web_root: Path = WEB):
        self.tracker, self.out_root, self.web_root = tracker, out_root, web_root
        self.accepting: set[str] = set()  # queued batches being added right now, so a second click can't double up
        self.accept_lock = threading.Lock()
        super().__init__(("127.0.0.1", port), Handler)
        self.port = self.server_address[1]
        self.hosts = {f"127.0.0.1:{self.port}", f"localhost:{self.port}"}
        self.origins = {f"http://{h}" for h in self.hosts}


class Handler(BaseHTTPRequestHandler):
    server: TrackerServer
    protocol_version = "HTTP/1.1"  # keep-alive for the page's polling; every response sets Content-Length
    body_read = False

    def log_message(self, format: str, *args: Any) -> None:
        pass  # pythonw has no console to log to, and the page polls every few seconds

    def handle_one_request(self) -> None:
        self.body_read = False  # one handler serves every request on a kept-alive connection
        super().handle_one_request()

    # --- responses ----------------------------------------------------------------------------------------------

    def _send(self, status: int, body: bytes, ctype: str, extra: dict[str, str] | None = None) -> None:
        if not self.body_read and self.headers.get("Content-Length", "0") not in ("", "0"):
            # A refused or unrouted request's body is still unread; on a kept-alive connection it would be taken
            # for the start of the next request. Close the connection instead of reading what a stranger sent.
            self.close_connection = True
        self.send_response(status)
        if self.close_connection:
            self.send_header("Connection", "close")
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, data: Any) -> None:
        self._send(status, json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _error(self, status: int, message: str, **extra: Any) -> None:
        self._json(status, {"error": message, **extra})

    def _file(self, root: Path, rel: str) -> None:
        base = root.resolve()
        target = (base / rel).resolve()
        if not target.is_relative_to(base) or not target.is_file():
            self._error(HTTPStatus.NOT_FOUND, "not found")
            return
        ctype = "text/plain" if target.suffix in _TEXT else mimetypes.guess_type(target.name)[0]
        ctype = ctype or "application/octet-stream"
        if ctype.startswith("text/"):
            ctype += "; charset=utf-8"
        self._send(HTTPStatus.OK, target.read_bytes(), ctype)

    # --- checks -------------------------------------------------------------------------------------------------

    def _allowed(self, write: bool) -> bool:
        """Local Host on every request; the X-Tracker header and a local Origin (when sent) on every write."""
        if self.headers.get("Host") not in self.server.hosts:
            self._error(HTTPStatus.FORBIDDEN, "the tracker only answers at 127.0.0.1 on this PC")
            return False
        if write:
            if self.headers.get("X-Tracker") != "1":
                self._error(HTTPStatus.FORBIDDEN, "writes come only from the tracker page")
                return False
            origin = self.headers.get("Origin")
            if origin is not None and origin not in self.server.origins:
                self._error(HTTPStatus.FORBIDDEN, "writes come only from the tracker page")
                return False
        return True

    def _body(self) -> bytes:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise TrackerError("bad Content-Length") from None
        if length > MAX_BODY:
            raise TrackerError("that's too large to send")
        body = self.rfile.read(length) if length > 0 else b""
        self.body_read = True
        return body

    def _parts(self) -> list[str]:
        return [unquote(p) for p in urlsplit(self.path).path.split("/") if p]

    # --- routes -------------------------------------------------------------------------------------------------

    def do_GET(self) -> None:
        if not self._allowed(write=False):
            return
        parts, tracker = self._parts(), self.server.tracker
        try:
            match parts:
                case [] | ["index.html"]:
                    self._file(self.server.web_root, "tracker.html")
                case ["out", *rest] if rest:
                    self._file(self.server.out_root, "/".join(rest))
                case ["api", "schema"]:
                    self._json(HTTPStatus.OK, schema())
                case ["api", "state"]:
                    self._json(HTTPStatus.OK, {
                        "collections": {name: fingerprint(tracker, name) for name in COLLECTIONS},
                        "imports": fingerprint(tracker, "imports"),
                        "report": newest_report(self.server.out_root) if self.server.out_root.is_dir() else None,
                    })  # fmt: skip
                case ["api", "imports"]:
                    self._json(HTTPStatus.OK, {"imports": tracker.imports()})
                case ["api", "rows", name]:
                    collection(name)
                    self._json(HTTPStatus.OK, {"rows": tracker.load(name), "fingerprint": fingerprint(tracker, name)})
                case ["api", "next-id", name]:
                    self._json(HTTPStatus.OK, {"id": tracker.next_id(name)})
                case ["api", "csv", name]:
                    body = ("﻿" + to_csv(name, tracker.load(name))).encode("utf-8")  # the BOM keeps Excel on UTF-8
                    filename = f"{name}-{date.today().isoformat()}.csv"
                    self._send(HTTPStatus.OK, body, "text/csv; charset=utf-8",
                               {"Content-Disposition": f'attachment; filename="{filename}"'})  # fmt: skip
                case _:
                    self._error(HTTPStatus.NOT_FOUND, "not found")
        except TrackerError as e:
            self._error(HTTPStatus.BAD_REQUEST, str(e))

    def do_POST(self) -> None:
        if not self._allowed(write=True):
            return
        parts, tracker = self._parts(), self.server.tracker
        try:
            match parts:
                case ["api", "rows", name, row_id]:
                    req = json.loads(self._body() or b"{}")
                    if not isinstance(req, dict) or not isinstance(req.get("changes") or {}, dict):
                        raise TrackerError("send {changes, seen, create}")
                    row = tracker.write(name, row_id, req.get("changes") or {}, by=OWNER, seen=req.get("seen") or None,
                                        create=bool(req.get("create")))  # fmt: skip
                    self._json(HTTPStatus.OK, {"row": row})
                case ["api", "csv", name]:
                    rep = tracker.import_csv(name, self._body().decode("utf-8-sig"), by=OWNER)
                    self._json(HTTPStatus.OK, {"created": rep.created, "updated": rep.updated,
                                               "unchanged": rep.unchanged, "refused": rep.refused,
                                               "ignored_columns": rep.ignored_columns})  # fmt: skip
                case ["api", "imports", doc_id, "accept"]:
                    self._accept(doc_id)
                case _:
                    self._error(HTTPStatus.NOT_FOUND, "not found")
        except ConflictError as e:
            self._error(HTTPStatus.CONFLICT, str(e), current=e.current)
        except (TrackerError, ValueError, UnicodeDecodeError) as e:  # json.JSONDecodeError is a ValueError
            self._error(HTTPStatus.BAD_REQUEST, str(e))

    def _accept(self, doc_id: str) -> None:
        """Add a queued batch. Runs in this request's thread; the page polls /api/imports for its progress."""
        with self.server.accept_lock:
            if doc_id in self.server.accepting:
                self._error(HTTPStatus.CONFLICT, "these companies are already being added")
                return
            self.server.accepting.add(doc_id)
        try:
            self._json(HTTPStatus.OK, {"batch": self.server.tracker.accept_import(doc_id)})
        finally:
            with self.server.accept_lock:
                self.server.accepting.discard(doc_id)

    def do_DELETE(self) -> None:
        if not self._allowed(write=True):
            return
        match self._parts():
            case ["api", "rows", name, row_id]:
                try:
                    self.server.tracker.delete(name, row_id, by=OWNER)
                except TrackerError as e:
                    self._error(HTTPStatus.BAD_REQUEST, str(e))
                    return
                self._json(HTTPStatus.OK, {"deleted": row_id})
            case _:
                self._error(HTTPStatus.NOT_FOUND, "not found")


def serve(port: int = DEFAULT_PORT, root: Path | None = None, open_browser: bool = False) -> None:
    """Serve until stopped (Ctrl+C)."""
    server = TrackerServer(Tracker(root) if root else Tracker(), port)
    url = f"http://127.0.0.1:{server.port}/"
    print(f"Tracker at {url} (this PC only). Ctrl+C stops it.", flush=True)
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
