"""Local HTTP server for the PDF editor (standard library only)."""

from __future__ import annotations

import json
import mimetypes
import os
import re
import sys
import threading
import time
import traceback
import urllib.parse
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import __version__, tools
from .document import LOCK, DocSession, NeedsPassword
from .fonts import SYSTEM_FONTS
from . import config
from .textedit import EditError

STATIC_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static")
MAX_BODY = config.MAX_UPLOAD
VISITOR_COOKIE = "pdfe_visitor"

mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("text/css", ".css")
mimetypes.add_type("image/svg+xml", ".svg")


CURRENT = threading.local()  # the visitor this thread is serving


def visitor_id() -> str | None:
    return getattr(CURRENT, "visitor", None)


class ApiError(Exception):
    def __init__(self, status: int, message: str, **extra):
        super().__init__(message)
        self.status = status
        self.extra = extra


class App:
    """The open documents.

    Each one belongs to the visitor who opened it, so one person filling the server
    with files can only ever push out their own, and nobody can reach a document that
    is not theirs.  Documents live in memory, so they are also dropped once they have
    been left alone for a while.
    """

    def __init__(self) -> None:
        self.sessions: dict[str, DocSession] = {}
        self.preload_id: str | None = None
        self.last_heartbeat = time.time()
        self.had_client = False

    def add_session(self, session: DocSession, visitor: str | None = None) -> None:
        session.visitor = visitor if visitor is not None else visitor_id()
        self.sessions[session.id] = session
        mine = sorted((s for s in self.sessions.values() if s.visitor == session.visitor and s.id != self.preload_id),
                      key=lambda s: s.last_access)
        for old in mine[:max(0, len(mine) - config.MAX_SESSIONS_PER_VISITOR)]:
            self.sessions.pop(old.id, None)
        if len(self.sessions) > config.MAX_SESSIONS_TOTAL:
            others = sorted((s for s in self.sessions.values() if s.id != self.preload_id),
                            key=lambda s: s.last_access)
            for old in others[:len(self.sessions) - config.MAX_SESSIONS_TOTAL]:
                self.sessions.pop(old.id, None)

    def expire(self) -> None:
        if not config.SESSION_IDLE_MINUTES:
            return
        cutoff = time.time() - config.SESSION_IDLE_MINUTES * 60
        for s in [s for s in self.sessions.values() if s.last_access < cutoff and s.id != self.preload_id]:
            self.sessions.pop(s.id, None)

    def session(self, doc_id: str, visitor: str | None = None) -> DocSession:
        visitor = visitor if visitor is not None else visitor_id()
        s = self.sessions.get(doc_id)
        if not s:
            raise ApiError(404, "This document is no longer open. Please open it again.", closed=True)
        owner = getattr(s, "visitor", None)
        if config.PUBLIC and owner is not None and visitor != owner:
            raise ApiError(404, "This document is no longer open. Please open it again.", closed=True)
        s.last_access = time.time()
        return s


APP = App()
ROUTES: list[tuple[str, re.Pattern, object]] = []


def route(method: str, pattern: str):
    def deco(fn):
        ROUTES.append((method, re.compile("^" + pattern + "$"), fn))
        return fn
    return deco


# ---------------------------------------------------------------------------- handlers
@route("GET", r"/api/config")
def api_config(req, m):
    langs = tools.ocr_languages()
    return {
        "version": __version__,
        "fonts": SYSTEM_FONTS.ui_list(),
        "ocr": {"available": bool(tools.tesseract_path()), "languages": langs},
        "preload": APP.preload_id if APP.preload_id in APP.sessions else None,
        "appMode": req.server.app_mode,
        "maxUploadMb": round(MAX_BODY / (1024 * 1024)),
        "sourceUrl": config.SOURCE_URL,
    }


@route("POST", r"/api/heartbeat")
def api_heartbeat(req, m):
    APP.last_heartbeat = time.time()
    APP.had_client = True
    return {"ok": True}


@route("POST", r"/api/open")
def api_open(req, m):
    data = req.body()
    filename = urllib.parse.unquote(req.headers.get("X-Filename") or "document.pdf")
    password = urllib.parse.unquote(req.headers.get("X-Password") or "") or None
    if not data:
        raise ApiError(400, "The file is empty.")
    try:
        session = DocSession(data, filename, None, password)
    except NeedsPassword:
        raise ApiError(401, "This PDF is password protected.", needsPassword=True, wrong=bool(password))
    APP.add_session(session)
    return {"doc": session.info()}


@route("POST", r"/api/new")
def api_new(req, m):
    import pymupdf as fitz

    body = req.json()
    size = body.get("size") or "a4"
    w, h = {"a4": (595.28, 841.89), "letter": (612, 792)}.get(size, (595.28, 841.89))
    doc = fitz.open()
    doc.new_page(width=w, height=h)
    session = DocSession(doc.tobytes(), body.get("filename") or "Untitled.pdf")
    APP.add_session(session)
    return {"doc": session.info()}


@route("GET", r"/api/doc/(\w+)")
def api_info(req, m):
    return {"doc": APP.session(m.group(1)).info()}


@route("POST", r"/api/doc/(\w+)/close")
def api_close(req, m):
    if m.group(1) != APP.preload_id:
        APP.sessions.pop(m.group(1), None)
    return {"ok": True}


@route("GET", r"/api/doc/(\w+)/page/(\w+)\.png")
def api_page_png(req, m):
    s = APP.session(m.group(1))
    q = req.query()
    scale = float(q.get("scale", "1"))
    rev = q.get("rev")
    idx = s.page_index(m.group(2))
    current_rev = s.state.page_revs[idx]
    data = s.page_render(m.group(2), scale)
    cache = "private, max-age=86400" if rev and rev == str(current_rev) else "no-store"
    return RawResponse(data, "image/png", cache)


@route("GET", r"/api/doc/(\w+)/page/(\w+)/text")
def api_page_text(req, m):
    s = APP.session(m.group(1))
    return {"text": s.page_text(m.group(2)), "rev": s.state.page_revs[s.page_index(m.group(2))]}


@route("GET", r"/api/doc/(\w+)/page/(\w+)/forms")
def api_page_forms(req, m):
    s = APP.session(m.group(1))
    return {"fields": s.form_fields(m.group(2))}


@route("GET", r"/api/doc/(\w+)/page/(\w+)/scanfont")
def api_page_scanfont(req, m):
    s = APP.session(m.group(1))
    q = req.query()
    return {"scan": s.scan_font(m.group(2), q.get("line"), q.get("para"))}


@route("GET", r"/api/doc/(\w+)/page/(\w+)/font")
def api_page_font(req, m):
    s = APP.session(m.group(1))
    name = req.query().get("name", "")
    data = s.font_file(m.group(2), name)
    if not data:
        raise ApiError(404, "Font not available")
    return RawResponse(data, "font/ttf", "private, max-age=3600")


def mutation(fn):
    """Wraps an operation on a session and returns its result plus fresh document info."""
    def handler(req, m):
        s = APP.session(m.group(1))
        body = req.json() if req.is_json() else {}
        result = fn(s, body, req) or {}
        return {"result": result, "doc": s.info()}
    return handler


route("POST", r"/api/doc/(\w+)/text/edit")(mutation(lambda s, b, r: s.edit_text(b["pageId"], b.get("edits", []))))
route("POST", r"/api/doc/(\w+)/text/paragraph")(mutation(lambda s, b, r: s.edit_paragraph(b["pageId"], b)))
route("POST", r"/api/doc/(\w+)/text/delete")(mutation(lambda s, b, r: s.delete_text(b["pageId"], b.get("items", []))))
route("POST", r"/api/doc/(\w+)/text/replace")(mutation(lambda s, b, r: s.find_replace(b)))
route("POST", r"/api/doc/(\w+)/objects")(mutation(lambda s, b, r: s.objects_op(b.get("ops", []))))
route("POST", r"/api/doc/(\w+)/undo")(mutation(lambda s, b, r: {"done": s.undo()}))
route("POST", r"/api/doc/(\w+)/redo")(mutation(lambda s, b, r: {"done": s.redo()}))
route("POST", r"/api/doc/(\w+)/pages/rotate")(mutation(lambda s, b, r: s.rotate_pages(b.get("pageIds", []), int(b.get("angle", 90)))))
route("POST", r"/api/doc/(\w+)/pages/delete")(mutation(lambda s, b, r: s.delete_pages(b.get("pageIds", []))))
route("POST", r"/api/doc/(\w+)/pages/reorder")(mutation(lambda s, b, r: s.reorder_pages(b.get("order", []))))
route("POST", r"/api/doc/(\w+)/pages/duplicate")(mutation(lambda s, b, r: s.duplicate_pages(b.get("pageIds", []))))
route("POST", r"/api/doc/(\w+)/pages/blank")(mutation(lambda s, b, r: s.insert_blank(b.get("at", 0), b.get("width"), b.get("height"))))
route("POST", r"/api/doc/(\w+)/pages/crop")(mutation(lambda s, b, r: s.crop_pages(b.get("pageIds", []), b.get("margins", {}))))
route("POST", r"/api/doc/(\w+)/forms")(mutation(lambda s, b, r: s.form_set(b["pageId"], int(b["xref"]), b.get("value"))))
route("POST", r"/api/doc/(\w+)/tools/watermark")(mutation(lambda s, b, r: s.watermark(b)))
route("POST", r"/api/doc/(\w+)/tools/stamp")(mutation(lambda s, b, r: s.text_stamp(b)))
route("POST", r"/api/doc/(\w+)/tools/metadata")(mutation(lambda s, b, r: s.set_metadata(b)))
route("POST", r"/api/doc/(\w+)/tools/ocr")(mutation(lambda s, b, r: s.ocr(b)))
route("POST", r"/api/doc/(\w+)/save")(mutation(lambda s, b, r: s.save_to_path(b)))


@route("POST", r"/api/doc/(\w+)/pages/insert-images")
def api_insert_images(req, m):
    s = APP.session(m.group(1))
    body = req.json()
    images = []
    for aid in body.get("assets", []):
        asset = s.assets.get(aid)
        if asset:
            images.append(asset["data"])
    if not images:
        raise ApiError(400, "No images to insert.")
    result = s.insert_images(images, int(body.get("at", 0)), body.get("pageSize") or "fit")
    return {"result": result, "doc": s.info()}


@route("POST", r"/api/doc/(\w+)/pages/insert-pdf")
def api_insert_pdf(req, m):
    s = APP.session(m.group(1))
    data = req.body()
    filename = urllib.parse.unquote(req.headers.get("X-Filename") or "inserted.pdf")
    password = urllib.parse.unquote(req.headers.get("X-Password") or "") or None
    try:
        result = s.insert_pdf(data, filename, int(req.headers.get("X-At") or 0), password)
    except NeedsPassword:
        raise ApiError(401, "That PDF is password protected.", needsPassword=True, wrong=bool(password))
    return {"result": result, "doc": s.info()}


@route("POST", r"/api/doc/(\w+)/assets")
def api_asset_upload(req, m):
    s = APP.session(m.group(1))
    return s.add_asset(req.body(), req.headers.get("Content-Type", "image/png"))


@route("GET", r"/api/doc/(\w+)/assets/(\w+)")
def api_asset(req, m):
    s = APP.session(m.group(1))
    asset = s.assets.get(m.group(2))
    if not asset:
        raise ApiError(404, "Image not found")
    return RawResponse(asset["data"], asset["mime"], "private, max-age=86400")


@route("POST", r"/api/doc/(\w+)/search")
def api_search(req, m):
    s = APP.session(m.group(1))
    body = req.json()
    return {"hits": s.search(body.get("q", ""), bool(body.get("matchCase")))}


def _download_name(s: DocSession, suffix: str = "", ext: str = ".pdf") -> str:
    stem = os.path.splitext(s.filename)[0]
    return f"{stem}{suffix}{ext}"


@route("POST", r"/api/doc/(\w+)/export")
def api_export(req, m):
    s = APP.session(m.group(1))
    body = req.json()
    data = s.export_pdf(body)
    if body.get("markSaved", True):
        s.mark_saved()
    return RawResponse(data, "application/pdf", "no-store", filename=body.get("filename") or s.filename)


@route("POST", r"/api/doc/(\w+)/extract")
def api_extract(req, m):
    s = APP.session(m.group(1))
    body = req.json()
    ids = body.get("pageIds") or []
    pages = sorted(s.page_index(p) for p in ids) if ids else tools.parse_page_spec(body.get("pages"), s.doc.page_count)
    doc = s.baked_document()
    return RawResponse(tools.extract_pages(doc, pages), "application/pdf", "no-store", filename=_download_name(s, " (extract)"))


@route("POST", r"/api/doc/(\w+)/split")
def api_split(req, m):
    s = APP.session(m.group(1))
    body = req.json()
    doc = s.baked_document()
    files = tools.split_document(doc, body.get("mode", "every"), str(body.get("value", "1")), os.path.splitext(s.filename)[0])
    return RawResponse(tools.zip_files(files), "application/zip", "no-store", filename=_download_name(s, " (split)", ".zip"))


@route("POST", r"/api/doc/(\w+)/export-images")
def api_export_images(req, m):
    s = APP.session(m.group(1))
    body = req.json()
    doc = s.baked_document()
    pages = tools.parse_page_spec(body.get("pages"), doc.page_count)
    files = tools.export_images(doc, pages, body.get("format", "png"), int(body.get("dpi") or 150), os.path.splitext(s.filename)[0])
    if len(files) == 1:
        name, data = files[0]
        return RawResponse(data, "image/png" if name.endswith("png") else "image/jpeg", "no-store", filename=name)
    return RawResponse(tools.zip_files(files), "application/zip", "no-store", filename=_download_name(s, " (images)", ".zip"))


@route("POST", r"/api/doc/(\w+)/extract-images")
def api_extract_images(req, m):
    s = APP.session(m.group(1))
    files = tools.extract_embedded_images(s.baked_document(), os.path.splitext(s.filename)[0])
    if not files:
        raise ApiError(400, "This document doesn't contain any embedded images.")
    return RawResponse(tools.zip_files(files), "application/zip", "no-store", filename=_download_name(s, " (embedded images)", ".zip"))


@route("POST", r"/api/doc/(\w+)/extract-text")
def api_extract_text(req, m):
    s = APP.session(m.group(1))
    text = tools.extract_text(s.baked_document())
    return RawResponse(text.encode("utf-8"), "text/plain; charset=utf-8", "no-store", filename=_download_name(s, "", ".txt"))


# ---------------------------------------------------------------------------- plumbing
class RawResponse:
    def __init__(self, data: bytes, content_type: str, cache: str = "no-store", filename: str | None = None):
        self.data, self.content_type, self.cache, self.filename = data, content_type, cache, filename


class Handler(BaseHTTPRequestHandler):
    server_version = "PDFEditor/" + __version__
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # quieter logs
        if self.server.verbose:
            sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))

    # helpers used by route handlers
    def query(self) -> dict:
        return {k: v[-1] for k, v in urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).items()}

    def body(self) -> bytes:
        # NB: one handler instance serves every request on a keep-alive connection,
        # so the cached body is reset at the start of each request in _dispatch.
        if self._body is not None:
            return self._body
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            # read a little of it so a modest overage still gets a proper answer rather
            # than a closed connection; a huge one is dropped on the floor, which is the
            # point of having a limit at all
            spare = min(length, 1 << 18)
            while spare > 0:
                chunk = self.rfile.read(min(1 << 16, spare))
                if not chunk:
                    break
                spare -= len(chunk)
            self.close_connection = True
            raise ApiError(413, f"That file is too large. The limit is {round(MAX_BODY / (1024 * 1024))} MB.")
        data = b""
        while len(data) < length:
            chunk = self.rfile.read(min(1 << 20, length - len(data)))
            if not chunk:
                break
            data += chunk
        self._body = data
        return data

    def is_json(self) -> bool:
        return "json" in (self.headers.get("Content-Type") or "")

    def json(self) -> dict:
        raw = self.body()
        if not raw:
            return {}
        try:
            value = json.loads(raw)
        except ValueError:
            raise ApiError(400, "Invalid request body.")
        if not isinstance(value, dict):
            raise ApiError(400, "Invalid request body.")
        return value

    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").lower()
        port = self.server.server_address[1]
        if host in (f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"):
            return True
        name = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
        return config.host_allowed(name) or config.host_allowed(host)

    def _visitor(self) -> str:
        """Who this is, from a cookie; a new one is handed out with the response."""
        for part in (self.headers.get("Cookie") or "").split(";"):
            name, _, value = part.strip().partition("=")
            if name == VISITOR_COOKIE and re.fullmatch(r"[0-9a-f]{32}", value or ""):
                self._set_cookie = None
                return value
        fresh = uuid.uuid4().hex
        self._set_cookie = fresh
        return fresh

    def _send(self, status: int, data: bytes, content_type: str, cache: str = "no-store", filename: str | None = None) -> None:
        self.send_response(status)
        if getattr(self, "_set_cookie", None):
            self.send_header("Set-Cookie", f"{VISITOR_COOKIE}={self._set_cookie}; Path=/; HttpOnly; SameSite=Lax"
                             + ("; Secure" if config.BEHIND_PROXY else ""))
            self._set_cookie = None
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        if filename:
            quoted = urllib.parse.quote(filename)
            self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{quoted}")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _json(self, status: int, payload: dict) -> None:
        self._send(status, json.dumps(payload, separators=(",", ":")).encode(), "application/json")

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def _dispatch(self, method: str) -> None:
        self._body = None
        try:
            self._route(method)
        finally:
            if method == "POST" and self._body is None and not self.close_connection:
                # keep the connection in sync: consume a body the handler didn't read
                try:
                    self.body()
                except ApiError:
                    self.close_connection = True

    def _route(self, method: str) -> None:
        CURRENT.visitor = self._visitor()
        APP.expire()
        if not self._host_ok():
            self.close_connection = True
            self._send(403, b"Forbidden", "text/plain")
            return
        path = urllib.parse.urlsplit(self.path).path
        if not path.startswith("/api/"):
            if method == "POST":
                self._send(405, b"Method not allowed", "text/plain")
                return
            self._static(path)
            return
        if method == "POST" and self.headers.get("X-PDF-Editor") != "1":
            self._json(403, {"error": "Missing request header."})
            return
        for m_method, pattern, fn in ROUTES:
            if m_method != ("GET" if method == "HEAD" else method):
                continue
            match = pattern.match(path)
            if not match:
                continue
            try:
                with LOCK:
                    result = fn(self, match)
                if isinstance(result, RawResponse):
                    self._send(200, result.data, result.content_type, result.cache, result.filename)
                else:
                    self._json(200, result)
            except ApiError as exc:
                self._json(exc.status, {"error": str(exc), **exc.extra})
            except EditError as exc:
                self._json(400, {"error": str(exc)})
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as exc:  # pragma: no cover - reported to the UI
                traceback.print_exc()
                self._json(500, {"error": f"Something went wrong: {exc}"})
            return
        self._json(404, {"error": "Not found"})

    def _static(self, path: str) -> None:
        if path in ("", "/"):
            path = "/home.html"  # the tools page; the editor itself lives at /editor
        elif path.rstrip("/") == "/editor":
            path = "/index.html"
        elif path.startswith("/t/"):
            path = "/task.html"  # one page per job; which one is read from the URL
        rel = os.path.normpath(urllib.parse.unquote(path).lstrip("/"))
        if rel.startswith("..") or os.path.isabs(rel):
            self._send(404, b"Not found", "text/plain")
            return
        full = os.path.join(STATIC_DIR, rel)
        if not os.path.isfile(full):
            self._send(404, b"Not found", "text/plain")
            return
        with open(full, "rb") as f:
            data = f.read()
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "image/svg+xml"):
            ctype += "; charset=utf-8"
        self._send(200, data, ctype, "no-cache")


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, handler, app_mode=False, verbose=False):
        super().__init__(addr, handler)
        self.app_mode = app_mode
        self.verbose = verbose


def should_exit(now: float, started: float, last_heartbeat: float, had_client: bool, has_unsaved: bool,
                idle_seconds: int = 180, unsaved_idle_seconds: int = 1800, first_client_seconds: int = 600) -> bool:
    """App-mode exit policy: quit once no editor tab has checked in for a while."""
    if not had_client:
        return now - started > first_client_seconds
    limit = unsaved_idle_seconds if has_unsaved else idle_seconds
    return now - last_heartbeat > limit


def watchdog(server: Server) -> None:
    """In app mode, exit once the browser tab has been closed for a while."""
    started = time.time()
    previous = started
    while True:
        time.sleep(5)
        now = time.time()
        if now - previous > 30:
            # the computer was asleep: give open tabs time to check in again
            APP.last_heartbeat = now
        previous = now
        with LOCK:
            unsaved = any(s.index != s.saved_index for s in APP.sessions.values())
        if should_exit(now, started, APP.last_heartbeat, APP.had_client, unsaved):
            break
    threading.Thread(target=server.shutdown, daemon=True).start()
