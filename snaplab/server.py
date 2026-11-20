"""SnapLab web server (Python standard library only).

Listens on 127.0.0.1 only; `tailscale serve` publishes it to your tailnet over HTTPS. Every request is
checked twice (auth.py): the Tailscale-User-Login header must be the tailnet owner, and the device must
carry a pairing cookie. Static files are served from a fixed table. State-changing requests must be JSON
with the X-SnapLab header from the app's own origin.
"""
from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import shutil
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import auth, i18n, avatars, uploads
from .config import WEB
from .dashboard import WidgetError
from .store import now_iso

STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/update.js": ("update.js", "text/javascript; charset=utf-8"),
    "/app.css": ("app.css", "text/css; charset=utf-8"),
    "/i18n.js": ("i18n/core.js", "text/javascript; charset=utf-8"),      # served as core + every language file
    "/pair.js": ("pair.js", "text/javascript; charset=utf-8"),
    "/local.js": ("local.js", "text/javascript; charset=utf-8"),
    "/setup": ("setup.html", "text/html; charset=utf-8"),
    "/setup.js": ("setup.js", "text/javascript; charset=utf-8"),
    "/sw.js": ("sw.js", "text/javascript; charset=utf-8"),
    "/manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json"),
    "/offline.html": ("offline.html", "text/html; charset=utf-8"),
    "/offline.js": ("offline.js", "text/javascript; charset=utf-8"),
    "/logo.png": ("logo.png", "image/png"),
    **{f"/icons/{n}": (f"icons/{n}", "image/png") for n in (
        "icon-192.png", "icon-512.png", "maskable-192.png", "maskable-512.png", "monochrome-512.png",
        "apple-touch-icon.png", "badge-96.png")},
    **{f"/brand/{n}": (f"brand/{n}", "image/png") for n in ("sail-blue.png", "sail-gold.png", "sail-navy.png")},
    **avatars.static_files(),
    **{f"/icons/soft3d/{p.relative_to(WEB / 'icons/soft3d').as_posix()}":
       (p.relative_to(WEB).as_posix(), "image/png") for p in (WEB / "icons/soft3d").rglob("*.png")},
}
# Served without the device cookie (the tailnet owner check still applies): files without secrets that the
# pairing page and the browser's app installer need.
NO_DEVICE_OK = {"/pair.js", "/local.js", "/app.css", "/i18n.js", "/manifest.webmanifest", "/sw.js", "/offline.html", "/offline.js", "/logo.png",
                *(k for k in STATIC if k.startswith(("/icons/", "/brand/")))}
COOKIE_RENEW_S = 24 * 3600
# blob: images are the previews of photos picked for the chat, shown before they are sent.
CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
       "connect-src 'self'; base-uri 'none'; form-action 'self'; frame-src 'self'; frame-ancestors 'none'; object-src 'none'; "
       "worker-src 'self'; manifest-src 'self'")
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{20,100}$")
ID = r"[a-z]{1,2}-[0-9a-f]{8}"


def _esc(s) -> str:
    return (str(s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;").replace("'", "&#39;"))


_VERSION = {"key": None, "value": "", "checked": 0.0}
VERSION_RECHECK_S = 1.0     # every phone asks for the version every few seconds; a stat of every file costs ~20 ms


def web_version() -> str:
    if time.monotonic() - _VERSION["checked"] < VERSION_RECHECK_S and _VERSION["value"]:
        return _VERSION["value"]
    files = sorted(f for f in WEB.rglob("*") if f.is_file())
    key = tuple((str(f), f.stat().st_mtime_ns, f.stat().st_size) for f in files)
    if key != _VERSION["key"]:
        h = hashlib.sha256()
        for f in files:
            h.update(f.relative_to(WEB).as_posix().encode())
            h.update(f.read_bytes())
        _VERSION.update(key=key, value=h.hexdigest()[:12])
    _VERSION["checked"] = time.monotonic()
    return _VERSION["value"]


# Text responses are sent gzip-compressed when the browser accepts it (app.js 165 KB -> 43 KB, the start-up
# state about 4x smaller). Compressed static files are kept by content, so each version is compressed once.
COMPRESSIBLE = ("text/", "application/json", "application/manifest+json", "image/svg+xml")
_GZIP: dict = {}


def gzipped(body: bytes, keep: bool = False) -> bytes:
    import gzip
    if not keep:
        return gzip.compress(body, 5)
    key = hashlib.sha256(body).digest()
    if key not in _GZIP:
        if len(_GZIP) > 64:
            _GZIP.clear()
        _GZIP[key] = gzip.compress(body, 9)
    return _GZIP[key]


class Handler(BaseHTTPRequestHandler):
    server_version = "snaplab"
    sys_version = ""
    protocol_version = "HTTP/1.1"
    app = None
    BODY_LIMIT = 256_000

    # ------------------------------------------------------------ helpers

    def log_message(self, fmt, *args):
        self.app.log(f"{self.client_address[0]} {fmt % args}")

    def _headers(self, status: int, ctype: str, length: int | None, extra: dict | None = None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        if length is not None:
            self.send_header("Content-Length", str(length))
        # images never change between versions of a file name: let the phone keep them a day (QA V-2)
        if not extra or "Cache-Control" not in extra:
            self.send_header("Cache-Control", "private, max-age=86400" if ctype == "image/png" else "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        if not extra or "Content-Security-Policy" not in extra:
            self.send_header("Content-Security-Policy", CSP)
        self.send_header("Cross-Origin-Opener-Policy", "same-origin")
        # camera: the chat's "take a photo" on a PC (getUserMedia); phones use their own camera app.
        self.send_header("Permissions-Policy", "camera=(self), microphone=(), screen-wake-lock=(self), geolocation=(), payment=()")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        renew = getattr(self, "_renew_cookie", None)
        if renew and not any(k.lower() == "set-cookie" for k in (extra or {})):
            self.send_header("Set-Cookie", self.cookie_header(renew))
            self._renew_cookie = None
        self.end_headers()

    def send_bytes(self, status: int, body: bytes, ctype: str, extra: dict | None = None, keep_gzip: bool = False):
        if (len(body) > 1024 and ctype.startswith(COMPRESSIBLE) and not (extra and "Content-Encoding" in extra)
                and "gzip" in (self.headers.get("Accept-Encoding") or "").lower()):
            body = gzipped(body, keep_gzip)
            extra = {**(extra or {}), "Content-Encoding": "gzip", "Vary": "Accept-Encoding"}
        self._headers(status, ctype, len(body), extra)
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_file(self, path: Path, ctype: str, extra: dict | None = None):
        size = path.stat().st_size
        self._headers(200, ctype, size, extra)
        if self.command == "HEAD":
            return
        with path.open("rb") as f:
            shutil.copyfileobj(f, self.wfile, 1024 * 1024)

    def send_json(self, status: int, obj, extra=None):
        if status >= 400 and isinstance(obj, dict) and obj.get("error") and self.ui_language() == "ru":
            # Preserve machine-readable diagnostics separately; never render raw English exceptions in Russian UI.
            obj = {**obj, "error_code": obj["error"], "error": i18n.text("ru", "err.request")}
        text = self.app.redact_log(json.dumps(obj, ensure_ascii=False, separators=(",", ":")))
        self.send_bytes(status, text.encode("utf-8"), "application/json; charset=utf-8", extra)

    def ui_language(self):
        cookie = auth.parse_cookies(self.headers.get("Cookie")).get("snaplab_lang", "")
        requested = self.headers.get("X-SnapLab-Language") or cookie
        if requested in i18n.codes():
            return requested
        for item in self.headers.get("Accept-Language", "").split(","):
            code = item.split(";")[0].strip().lower().split("-")[0]
            if code in i18n.codes():
                return code
        return "en"

    def page(self, status: int, title: str, body_html: str, script: str | None = None):
        s = f'<script src="/i18n.js"></script><script src="{script}"></script>' if script else ""
        html = f"""<!doctype html><html lang="{self.ui_language()}"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#FBF8F6"><meta name="robots" content="noindex, nofollow">
<title>{_esc(title)} · SnapLab</title><link rel="icon" href="/icons/icon-192.png"><link rel="stylesheet" href="/app.css"></head>
<body class="gate-body"><main class="gate"><div class="gate-card"><img class="gate-logo" src="/logo.png" alt="SnapLab">{body_html}</div></main>{s}</body></html>"""
        self.send_bytes(status, html.encode("utf-8"), "text/html; charset=utf-8")

    def deny(self, status: int, why: str, api: bool):
        if api:
            self.send_json(status, {"ok": False, "error": why, "need": "pairing" if status == 401 else "tailnet"})
            return
        lang = self.ui_language()
        key = "err.notpaired" if status == 401 else "pair.denied"
        body = f"<h1>{_esc(i18n.text(lang, key))}</h1><p>{_esc(i18n.text(lang, key + '.body'))}</p>"
        self.page(status, i18n.text(lang, key), body)

    def gate(self, *, api: bool, need_device: bool = True):
        if self.via_phone:
            # Through `tailscale serve`: only the tailnet owner (and then only a paired device) gets in.
            ok, why = auth.check_user(self.headers, self.app.phone.tailscale().get("owner"))
            if not ok:
                self.deny(403, why, api)
                return None
        elif self.app.cfg.get("require_tailscale_user", True):
            ok, why = auth.check_user(self.headers, self.app.owner.get())
            if not ok:
                self.deny(403, why, api)
                return None
        value = auth.parse_cookies(self.headers.get("Cookie")).get(self.cookie_name())
        dev = self.app.auth.device_for(value)
        if dev:
            self.app.auth.touch(dev)
            if need_device and self.app.auth.should_renew(dev, COOKIE_RENEW_S):
                self._renew_cookie = value
            return dev
        if need_device:
            self.deny(401, "This device is not paired.", api)
            return None
        return {}

    def cookie_name(self):
        return f"snaplab-local-{self.app.cfg['listen_port']}" if self.app.cfg.get("local_mode") and not self.via_phone else auth.COOKIE_NAME

    def cookie_header(self, value):
        if self.app.cfg.get("local_mode") and not self.via_phone:
            return f"{self.cookie_name()}={value}; Path=/; Max-Age={auth.COOKIE_MAX_AGE}; HttpOnly; SameSite=Strict"
        return auth.cookie_header(value)

    via_phone = False

    def own_origin(self) -> str | None:
        return self.app.phone.url() if self.via_phone else self.app.public_url

    def local_request(self):
        self.via_phone = False
        if not self.app.cfg.get("local_mode"):
            return True
        expected = f"127.0.0.1:{self.app.cfg['listen_port']}"
        loopback = self.client_address[0] in ("127.0.0.1", "::1")
        if (loopback and self.headers.get("Tailscale-User-Login") and not self.headers.get("Tailscale-Funnel-Request")
                and self.app.phone.enabled and self.app.phone.host_ok(self.headers.get("Host") or "")):
            self.via_phone = True      # a phone (or another device) through `tailscale serve`; gate() checks who
            return True
        if (not loopback or self.headers.get("Host") != expected
                or any(self.headers.get(k) for k in ("Forwarded", "X-Forwarded-For", "X-Forwarded-Host", "Tailscale-User-Login", "Tailscale-Funnel-Request"))):
            self.send_json(403, {"ok": False, "error": "Open SnapLab directly on this PC using its launch command."})
            return False
        return True

    def same_origin_post(self) -> bool:
        if self.headers.get("X-SnapLab") != "1":
            return False
        if not (self.headers.get("Content-Type") or "").startswith("application/json"):
            return False
        origin = self.headers.get("Origin")
        if origin and self.own_origin() and origin.rstrip("/") != self.own_origin():
            return False
        site = self.headers.get("Sec-Fetch-Site")
        if site and site not in ("same-origin", "none"):
            return False
        return True

    def read_body(self, limit=None) -> bytes | None:
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = -1
        if n < 0 or n > (limit or self.BODY_LIMIT):
            self.close_connection = True
            return None
        return self.rfile.read(n) if n else b""

    # ------------------------------------------------------------ GET

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        try:
            self._get()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        except Exception:  # noqa: BLE001
            self.app.log("GET error: " + traceback.format_exc(limit=4).replace("\n", " | "))
            try:
                self.send_json(500, {"ok": False, "error": "internal error"})
            except OSError:
                pass

    def _get(self):
        self._renew_cookie = None
        if not self.local_request():
            return
        u = urlsplit(self.path)
        path = u.path
        asset_version = None
        if m := re.fullmatch(r"/assets/([a-zA-Z0-9_-]+)/(app\.js|app\.css|i18n\.js|update\.js)", path):
            asset_version, path = m.group(1), "/" + m.group(2)
        if path == "/healthz":
            health = {"ok": True, "app": "snaplab", "version": self.app.version}
            if self.app.cfg.get("local_mode"):
                health["instance"] = hashlib.sha256(str(Path(self.app.cfg["data_dir"]).resolve()).encode()).hexdigest()
            self.send_json(200, health)
            return
        if path == "/local" and self.app.cfg.get("local_mode"):
            self.page(200, "Open SnapLab", "<h1>Opening SnapLab</h1><p id='local-status'>Connecting this browser...</p>", script="/local.js")
            return
        if path.startswith("/pair/"):
            self.pair_page(path[len("/pair/"):])
            return
        if path in STATIC:
            if self.gate(api=False, need_device=path not in NO_DEVICE_OK) is None:
                return
            if path == "/":
                from .setup import needed
                if needed(self.app):
                    self.send_bytes(303, b"", "text/plain", {"Location": "/setup"})
                    return
            name, ctype = STATIC[path]
            f = WEB / name
            if not f.exists():
                self.send_json(404, {"ok": False, "error": "not found"})
                return
            body = f.read_bytes()
            if path == "/i18n.js":
                body = i18n.bundle()
            if path in ("/sw.js", "/"):
                body = body.replace(b"__SNAPLAB_VERSION__", web_version().encode("ascii"))
            if path == "/":
                from . import icons
                body = body.replace(b"<!--snaplab-icons-->", icons.sprite(WEB / "icons" / "ui").encode("utf-8"))
            extra = None
            if asset_version and asset_version == web_version():
                # The address names the version, so the phone may keep this file until the version changes.
                extra = {"Cache-Control": "private, max-age=31536000, immutable"}
            self.send_bytes(200, body, ctype, extra, keep_gzip=True)
            return
        api = path.startswith("/api/")
        dev = self.gate(api=api)
        if dev is None:
            return
        app = self.app
        if path == "/api/web-version":
            self.send_json(200, {"version": web_version()})
            return
        if path == "/api/phone":
            self.send_json(200, {"ok": True, **app.phone.summary(), "via_phone": self.via_phone})
            return
        if path == "/api/mcp":
            from . import mcp_admin
            self.send_json(200, {"ok": True, "servers": mcp_admin.view(app)})
            return
        if path == "/api/cli":
            from . import cli_admin
            fresh = (parse_qs(u.query).get("fresh") or [""])[0] == "1"
            self.send_json(200, {"ok": True, **cli_admin.view(app, fresh), "via_phone": self.via_phone})
            return
        if path == "/api/setup/presets":
            from .presets import presets
            self.send_json(200, {"ok": True, "presets": presets(app.cfg)})
            return
        if m := re.fullmatch(r"/api/attachments/(f-[0-9a-f]{32})/(content|download|preview)", path):
            meta = uploads.get(app, m.group(1))
            if meta and meta.get("from") == "user":       # a file the user sent: any size, streamed from disk
                result = uploads.response(app, meta, m.group(2))
                if result is None:
                    self.send_json(404, {"ok": False, "error": "not found"})
                else:
                    self.send_file(*result)
                return
            result = app.attachments.response(m.group(1), m.group(2))
            if result is None:
                self.send_json(404, {"ok": False, "error": "not found"})
            else:
                self.send_bytes(200, *result)
        elif path == "/api/bootstrap":
            self.send_json(200, {"ok": True, **app.bootstrap(dev)})
        elif path.startswith("/api/avatars/"):
            name = path.removeprefix("/api/avatars/")
            if not avatars.UPLOAD_RE.fullmatch(name) or not app.store.path("avatars", name).is_file():
                self.send_json(404, {"ok": False, "error": "not found"})
                return
            mime = {"png": "image/png", "jpg": "image/jpeg", "webp": "image/webp"}[name.rsplit(".", 1)[1]]
            self.send_bytes(200, app.store.path("avatars", name).read_bytes(), mime, {"Cache-Control": "no-store"})
        elif path == "/api/events":
            self.events()
        elif m := re.fullmatch(rf"/api/browser/({ID})", path):
            ok, why = app.browsers.available(m.group(1))
            lb = app.browsers.peek(m.group(1))
            self.send_json(200, {"ok": True, "available": ok, "why": why, "browser": lb.view() if lb else None})
        elif m := re.fullmatch(rf"/api/browser/({ID})/live", path):
            self.browser_stream(m.group(1))
        elif m := re.fullmatch(rf"/api/rooms/({ID})/attachments", path):
            if not app.store.room(m.group(1)):
                self.send_json(404, {"ok": False, "error": "no such room"})
                return
            offset = (parse_qs(u.query).get("offset") or ["0"])[0]
            if not re.fullmatch(r"[0-9]{1,8}", offset):
                self.send_json(400, {"ok": False, "error": "invalid offset"})
                return
            self.send_json(200, {"ok": True, **app.attachments.in_room(m.group(1), int(offset))})
        elif m := re.fullmatch(rf"/api/rooms/({ID})/messages", path):
            room = app.store.room(m.group(1))
            if not room:
                self.send_json(404, {"ok": False, "error": "no such room"})
                return
            live = app.chat.live_messages(room["id"])
            live_ids = {x["id"] for x in live}
            msgs = [x for x in app.store.messages(room["id"], 200) if x.get("id") not in live_ids]
            for m_ in msgs:                      # cards show their current state (answered in the inbox, etc.)
                if m_.get("question_id"):
                    q_ = app.store.question(m_["question_id"])
                    if q_:
                        m_["question"] = app.chat.question_view(q_)
            self.send_json(200, {"ok": True, "room": room, "messages": msgs,
                                 "live": live, "busy": app.chat.busy(room["id"])})
        elif m := re.fullmatch(rf"/api/sessions/({ID})/screen", path):
            with app.sessions.lock:
                frame = app.sessions.screens.get(m.group(1))
            if not frame:
                self.send_json(404, {'ok':False, 'error':'no live screen'})
            else:
                self.send_bytes(200, frame[0], 'image/jpeg', {'Cache-Control':'no-store', 'X-Content-Type-Options':'nosniff'})
        elif m := re.fullmatch(rf"/api/sessions/({ID})", path):
            s = app.store.session(m.group(1))
            if not s:
                self.send_json(404, {"ok": False, "error": "no such session"})
                return
            self.send_json(200, {"ok": True, "session": app.sessions.summary(s, events=120)})
        elif m := re.fullmatch(rf"/api/bots/({ID})/memory", path):
            if not app.store.bot(m.group(1)):
                self.send_json(404, {"ok": False, "error": "no such bot"})
                return
            self.send_json(200, {"ok": True, "memory": app.store.memory(m.group(1)),
                                 "limit": int(app.cfg.get("memory_chars", 4000))})
        elif m := re.fullmatch(rf"/api/rooms/({ID})/summary", path):
            if not app.store.room(m.group(1)):
                self.send_json(404, {"ok": False, "error": "no such room"})
                return
            text, meta = app.summaries.get(m.group(1))
            self.send_json(200, {"ok": True, "text": text, "meta": meta, "pending": len(app.summaries.pending(m.group(1))),
                                 "running": m.group(1) in app.summaries.running})
        elif path == "/api/profile":
            self.send_json(200, {"ok": True, "text": app.store.user_note(), "limit": int(app.cfg.get("user_note_chars", 1400))})
        elif m := re.fullmatch(rf"/api/projects/({ID})/brief", path):
            p = app.store.project(m.group(1))
            if not p:
                self.send_json(404, {"ok": False, "error": "no such project"})
                return
            self.send_json(200, {"ok": True, "text": app.read_brief(p["id"])})
        elif m := re.fullmatch(rf"/api/messages/({ID})/view", path):
            v = app.store.view(m.group(1))
            if not v:
                self.send_json(404, {"ok": False, "error": "no view for this message"})
                return
            self.send_json(200, {"ok": True, "view": v})
        elif m := re.fullmatch(rf"/api/bots/({ID})/inside", path):
            self.send_json(200, {"ok": True, **app.inside_view(m.group(1), (parse_qs(u.query).get("room") or [""])[0])})
        elif path == "/api/tasks":
            self.send_json(200, {"ok": True, "tasks": [app.tasks.view(t) for t in app.tasks.all()]})
        elif path == "/api/archive":
            from .archive import Scope, terms_of
            qs = parse_qs(u.query)
            q = (qs.get("q") or [""])[0][:200]
            b = app.store.bot((qs.get("bot") or [""])[0]) if qs.get("bot") else None
            scope = Scope.for_bot(app, b)
            kinds = [k for k in (qs.get("kind") or [""])[0].split(",") if k] or None
            hits = app.archive.search(q, scope, kinds=kinds, limit=40) if q.strip() else []
            terms = terms_of(q)
            self.send_json(200, {"ok": True, "results": [app.archive.view(h, terms) for h in hits],
                                 "stats": app.archive.stats(scope)})
        elif m := re.fullmatch(r"/api/archive/(a-[0-9a-f]{8})", path):
            from .archive import Scope
            rec = app.archive.get(m.group(1), Scope(everything=True))
            if not rec:
                self.send_json(404, {"ok": False, "error": "no such record"})
                return
            v = app.archive.view(rec, [])
            v["text"] = rec["text"][:300000]
            v["cut"] = len(rec["text"]) > 300000
            self.send_json(200, {"ok": True, "record": v})
        elif path == "/api/status":
            self.send_json(200, {"ok": True, "status": app.status()})
        elif path == "/api/usage":
            self.send_json(200, {"ok": True, "usage": app.usage_view(), "activity": app.store.recent_activity(40)})
        elif path == "/api/usage/history":
            from . import usage
            self.send_json(200, {"ok": True, **usage.history(app.store)})
        elif path == "/api/usage/detail":
            from . import usage
            qs = parse_qs(u.query)
            try:
                out = usage.detail(app, (qs.get("from") or [""])[0], (qs.get("to") or [""])[0])
            except ValueError as e:
                self.send_json(400, {"ok": False, "error": str(e)})
                return
            self.send_json(200, {"ok": True, **out})
        else:
            self.send_json(404, {"ok": False, "error": "not found"})

    def pair_page(self, token: str):
        if self.gate(api=False, need_device=False) is None:
            return
        lang = self.ui_language()
        t = lambda key, **v: _esc(i18n.text(lang, key, **v))
        title = i18n.text(lang, "pair.title")
        if not TOKEN_RE.match(token or ""):
            self.page(404, title, f"<h1>{t('pair.invalid')}</h1>")
            return
        if self.app.auth.device_for(auth.parse_cookies(self.headers.get("Cookie")).get(self.cookie_name())):
            self.page(200, title, f"<h1>{t('pair.already')}</h1><a class='btn primary' href='/'>{t('pair.open')}</a>")
            return
        state, rec = self.app.auth.pairing_state(token)
        if state != "ok":
            self.page(410 if state != "unknown" else 404, title,
                      f"<h1>{t('pair.failed')}</h1><p>{t('pair.' + state)}</p>"
                      f"<p>{t('pair.new')} <code>python snaplab.py pair</code></p>")
            return
        self.page(200, title, f"<h1>{t('pair.confirm')}</h1><p>{_esc(rec.get('label'))}</p>"
                  f"<p>{t('pair.until', expires=rec.get('expires', '')[:16].replace('T', ' '))}</p>"
                  f"<button class='btn primary wide' id='pair-go' type='button'>{t('pair.go')}</button>"
                  "<p class='muted' id='pair-msg'></p>", script="/pair.js")

    def events(self):
        app = self.app
        cookie = auth.parse_cookies(self.headers.get("Cookie")).get(self.cookie_name())
        self.close_connection = True
        self._headers(200, "text/event-stream; charset=utf-8", None, {"X-Accel-Buffering": "no", "Connection": "close"})
        q = app.bus.subscribe()
        t_end = time.time() + float(app.cfg.get("sse_seconds", 900))
        try:
            if not app.auth.device_for(cookie):
                return
            self.wfile.write(b"retry: 2000\n\nevent: hello\ndata: {}\n\n")
            self.wfile.flush()
            last_usage = 0.0
            while time.time() < t_end and not app_stopping(app):
                try:
                    ev = q.get(timeout=15)
                except queue.Empty:
                    if not app.auth.device_for(cookie):
                        break
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue
                if not app.auth.device_for(cookie):
                    break
                if ev["type"] == "usage.updated":
                    if time.time() - last_usage < 2:
                        continue
                    last_usage = time.time()
                data = app.redact_log(json.dumps(ev, ensure_ascii=False, separators=(",", ":")))
                self.wfile.write(f"data: {data}\n\n".encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass
        finally:
            app.bus.unsubscribe(q)

    def browser_stream(self, sid: str):
        """The browser panel's live stream: the page as JPEG frames (only while someone watches) and its state."""
        app = self.app
        cookie = auth.parse_cookies(self.headers.get("Cookie")).get(self.cookie_name())
        try:
            lb = app.browsers.get(sid)
        except ValueError as e:
            self.send_json(404, {"ok": False, "error": str(e)})
            return
        self.close_connection = True
        self._headers(200, "text/event-stream; charset=utf-8", None,
                      {"X-Accel-Buffering": "no", "Connection": "close", "Cache-Control": "no-store"})
        lb.add_viewer()
        seq = version = 0
        try:
            state = lb.view()
            version = state["version"]
            self.wfile.write(b"retry: 1500\n\nevent: state\ndata: " + json.dumps(state).encode("utf-8") + b"\n\n")
            self.wfile.flush()
            t_end = time.time() + float(app.cfg.get("sse_seconds", 900))
            checked = time.time()
            while time.time() < t_end and not app_stopping(app):
                frame, changed = lb.wait(seq, version, 15)
                if time.time() - checked > 15:
                    if not app.auth.device_for(cookie):
                        break
                    checked = time.time()
                out = b""
                if frame:
                    seq = frame[0]
                    size = frame[2]
                    out += (b'event: frame\ndata: {"seq":%d,"w":%s,"h":%s,"jpeg":"' % (
                        seq, json.dumps(size.get("w")).encode(), json.dumps(size.get("h")).encode())
                        + frame[1].encode("ascii", "ignore") + b'"}\n\n')
                if changed:
                    state = lb.view()
                    version = state["version"]
                    out += b"event: state\ndata: " + json.dumps(state).encode("utf-8") + b"\n\n"
                self.wfile.write(out or b": ping\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass
        finally:
            lb.remove_viewer()

    # ------------------------------------------------------------ POST

    def do_POST(self):
        try:
            self._post()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        except Exception:  # noqa: BLE001
            self.app.log("POST error: " + traceback.format_exc(limit=4).replace("\n", " | "))
            try:
                self.send_json(500, {"ok": False, "error": "internal error"})
            except OSError:
                pass

    def _post(self):
        self._renew_cookie = None
        if not self.local_request():
            return
        path = urlsplit(self.path).path
        if m := re.fullmatch(rf"/api/rooms/({ID})/files", path):
            self.upload(m.group(1))
            return
        raw = self.read_body(1_400_000 if path == "/api/avatars" else None)
        if raw is None:
            self.send_json(413, {"ok": False, "error": "too large"})
            return
        try:
            body = json.loads(raw.decode("utf-8")) if raw else {}
        except (ValueError, UnicodeDecodeError):
            body = None
        if path == "/api/pair":
            if self.gate(api=True, need_device=False) is None:
                return
            if not self.same_origin_post() or not isinstance(body, dict):
                self.send_json(403, {"ok": False, "error": "not allowed from here"})
                return
            token = str(body.get("token") or "")
            if not TOKEN_RE.match(token):
                self.send_json(400, {"ok": False, "error": "bad link"})
                return
            state, value, dev = self.app.auth.redeem(token, self.headers.get("User-Agent", ""))
            if state != "ok":
                time.sleep(0.3)
                self.send_json(410, {"ok": False, "error": {"used": "This link was already used.",
                                                            "expired": "This link has expired."}.get(state, "Link not found.")})
                return
            self.app.log(f"paired device {dev['id']} {dev['label']} {dev['ua']}")
            self.app.act("device", "act.device", ref=dev["id"], label=dev["label"])
            self.send_json(200, {"ok": True, "device": {"id": dev["id"], "label": dev["label"]}},
                           {"Set-Cookie": self.cookie_header(value)})
            return
        dev = self.gate(api=True)
        if dev is None:
            return
        if not self.same_origin_post():
            self.send_json(403, {"ok": False, "error": "not allowed from here"})
            return
        if not isinstance(body, dict):
            self.send_json(400, {"ok": False, "error": "could not read the request"})
            return
        try:
            self.send_json(200, {"ok": True, **(self.route_post(path, body, dev) or {})})
        except LookupError as e:
            self.send_json(404 if "no such" in str(e) else 409, {"ok": False, "error": str(e).strip("'\"")})
        except (ValueError, WidgetError) as e:
            self.send_json(400, {"ok": False, "error": str(e), "error_code": getattr(e, "error_code", None)})

    def route_post(self, path: str, b: dict, dev: dict) -> dict | None:
        app = self.app
        s = lambda k, d="": str(b.get(k) if b.get(k) is not None else d)  # noqa: E731
        if path == "/api/setup/provider":
            from .setup import configure
            return configure(app, b)
        if path == "/api/providers/models":
            from .setup import discover
            return discover(app, b)
        if path == "/api/onboarding" and app.cfg.get("local_mode"):
            from .setup import needed
            if needed(app):
                raise ValueError("Configure and test your provider first.")
        if path == "/api/onboarding/preview":
            return {"prompt": app.preview_prompt(lang=s("lang", "en"), name=s("name"), tone=s("tone", "friendly"),
                                                 tone_custom=s("tone_custom"), call_user=s("call_user"))}
        if path == "/api/onboarding":
            r = app.onboard(lang=s("lang", "en"), name=s("name"), color=s("color"), tone=s("tone", "friendly"),
                            tone_custom=s("tone_custom"), call_user=s("call_user"), system_prompt=s("system_prompt"), avatar=b.get("avatar"))
            return {"room": r["room"]["id"], "bot": r["bot"]["id"]}
        if path == "/api/avatars":
            return {"avatar": avatars.save_upload(app.store, b.get("image"))}
        if not app.store.settings().get("onboarded"):
            raise ValueError("finish the first-run setup first")
        if path == "/api/mcp":
            from . import mcp_admin
            action = str(b.get("action") or "")
            if action == "add":
                return mcp_admin.add(app, s("name"), command=b.get("command") or None, url=b.get("url") or None,
                                     env=b.get("env") or "", token=b.get("token") or None, label=b.get("label") or None,
                                     chat=b.get("chat") is not False)
            if action == "import":
                return mcp_admin.add_json(app, str(b.get("json") or ""), chat=b.get("chat") is not False)
            if action == "remove":
                return mcp_admin.remove(app, s("name"))
            if action == "update":
                return mcp_admin.update(app, s("name"), enabled=b.get("enabled"), chat=b.get("chat"))
            if action == "test":
                return mcp_admin.retest(app, s("name"))
            raise ValueError("unknown action")
        if path == "/api/phone":
            if self.via_phone:
                raise ValueError("set up phone access on the PC")
            action = str(b.get("action") or "")
            if action == "start":
                return app.phone.start()
            if action == "pair":
                return app.phone.refresh_pairing()
            if action == "disable":
                return app.phone.disable()
            if action == "decline":
                return app.phone.decline()
            if action == "ack":
                return app.phone.ack()
            raise ValueError("unknown action")
        if path == "/api/providers":
            from .setup import configure
            return configure(app, b, add=True)
        if path == "/api/cli":
            from . import cli_admin
            action = str(b.get("action") or "")
            if action == "open":
                if self.via_phone:
                    raise ValueError("do this on the PC")
                return cli_admin.open_window(app, s("app"), s("step"))
            if action == "add":
                return cli_admin.add(app, s("app"), s("model"))
            raise ValueError("unknown action")
        if m := re.fullmatch(rf"/api/browser/({ID})", path):
            lb = app.browsers.get(m.group(1))
            action = s("action")
            if action == "input":
                return {"done": lb.input(b.get("events"))}
            if action == "nav":
                lb.navigate(s("to"), s("url"))
                return {}
            if action == "tab":
                lb.tab(s("to"), s("tab"), s("url"))
                return {}
            if action == "control":
                lb.control(s("holder"))
                return {"browser": lb.view()}
            if action == "dialog":
                lb.answer_dialog(b.get("accept") is True, s("text"))
                return {}
            if action == "close":
                lb.end(wait=False)
                return {}
            raise ValueError("unknown action")
        if path == "/api/models":
            from .setup import remove_model, set_default
            action = str(b.get("action") or "")
            if action == "default":
                return set_default(app, s("name"))
            if action == "remove":
                return remove_model(app, s("name"))
            raise ValueError("unknown action")
        if path == "/api/settings":
            changes = {}
            if b.get("lang") in i18n.codes():
                changes["lang"] = b["lang"]
            if isinstance(b.get("notify"), dict):
                changes["notify"] = b["notify"]
            if b.get("theme") in ("system", "light", "dark"):
                changes["theme"] = b["theme"]
            if "icon_style" in b:
                if b["icon_style"] not in ("line", "soft3d"):
                    raise ValueError("invalid icon style")
                changes["icon_style"] = b["icon_style"]
            if b.get("tour_done") is True:
                changes["tour_done"] = True
            st = app.store.update_settings(**changes)
            app.bus.publish("settings.updated", {"settings": st})
            return {"settings": st}
        if m := re.fullmatch(rf"/api/rooms/({ID})/send", path):
            msg = app.chat.post(m.group(1), s("text"), device=dev.get("id"), files=b.get("files") or None)
            return {"message": msg}
        if m := re.fullmatch(r"/api/files/(f-[0-9a-f]{32})/discard", path):
            return {"removed": uploads.discard(app, m.group(1))}
        if m := re.fullmatch(rf"/api/rooms/({ID})/stop", path):
            return {"stopped": app.chat.stop(m.group(1))}
        if m := re.fullmatch(rf"/api/rooms/({ID})/retry", path):
            return {"message": app.chat.retry(m.group(1), s("message"))}
        if path == "/api/rooms":
            return {"room": app.create_group(s("name"), b.get("bots") or [])}
        if m := re.fullmatch(rf"/api/rooms/({ID})", path):
            return {"room": app.update_room(m.group(1), b)}
        if m := re.fullmatch(rf"/api/questions/({ID})/answer", path):
            return {"question": app.chat.question_view(app.chat.answer(m.group(1), s("text")))}
        if path == "/api/bots":
            bot = app.create_bot(name=s("name"), role=s("role"), color=s("color"), tone=s("tone"),
                                 tone_custom=s("tone_custom"), call_user=s("call_user"),
                                 projects=b.get("projects") or [], system_prompt=s("system_prompt"), avatar=b.get("avatar"))
            return {"bot": bot, "room": app.store.dm_room(bot["id"])["id"]}
        if m := re.fullmatch(rf"/api/bots/({ID})", path):
            if b.get("action") == "delete":
                return app.delete_bot(m.group(1), confirmed=b.get("confirmed") is True)
            if b.get("action") == "archive":
                return app.archive_bot(m.group(1), confirmed=b.get("confirmed") is True)
            if b.get("action") == "restore":
                return app.restore_bot(m.group(1))
            return {"bot": app.update_bot(m.group(1), b)}
        if m := re.fullmatch(rf"/api/bots/({ID})/memory", path):
            if not app.store.bot(m.group(1)):
                raise LookupError("no such bot")
            text = s("text")
            if len(text) > 20000:
                raise ValueError("memory is limited to 20000 characters")
            app.store.write_memory(m.group(1), text, "replace")
            app.store.memory_log(m.group(1), "the user edited this memory")
            app.archive_memory(m.group(1))
            return {"memory": app.store.memory(m.group(1))}
        if m := re.fullmatch(rf"/api/rooms/({ID})/fresh", path):
            return {"fresh_start": app.start_fresh(m.group(1), bot_id=s("bot") or None, note=s("note"),
                                                   undo=b.get("undo") is True)}
        if m := re.fullmatch(rf"/api/rooms/({ID})/summary", path):
            if not app.store.room(m.group(1)):
                raise LookupError("no such room")
            if not app.summaries.pending(m.group(1)):
                raise ValueError("there is nothing new to summarize")
            return {"started": app.summaries.maybe_schedule(m.group(1), force=True)}
        if m := re.fullmatch(rf"/api/bots/({ID})/memory/tidy", path):
            b = app.store.bot(m.group(1))
            if not b:
                raise LookupError("no such bot")
            threading.Thread(target=lambda: app.tidy.tidy(b["id"], force=True), daemon=True).start()
            return {"started": True}
        if path == "/api/profile":
            text = s("text")
            limit = int(app.cfg.get("user_note_chars", 1400))
            if len(text) > limit:
                raise ValueError(f"keep it under {limit} characters")
            app.store.save_note_file(("shared", "user.md"), (text.strip() + "\n") if text.strip() else "")
            app.store.memory_log("_user", "the user edited the notes about themselves")
            app.archive_memory(None)
            return {"text": app.store.user_note()}
        if m := re.fullmatch(rf"/api/projects/({ID})/brief", path):
            p = app.write_brief(m.group(1), s("text"), by="you")
            return {"text": app.read_brief(p["id"])}
        if m := re.fullmatch(rf"/api/tasks/({ID})", path):
            action = s("action")
            if action == "run":
                if not app.tasks.get(m.group(1)):
                    raise LookupError("no such task")
                app.tasks.run(m.group(1))
                return {"task": app.tasks.view(app.tasks.get(m.group(1)))}
            t = app.tasks.change(m.group(1), action)
            return {"task": app.tasks.view(t) if t else None}
        if path == "/api/projects":
            p = app.create_project(name=s("name"), description=s("description"), test_command=s("test_command"),
                                   bot=s("bot") or None)
            return {"project": p}
        if m := re.fullmatch(rf"/api/projects/({ID})", path):
            return {"project": app.update_project(m.group(1), b)}
        if path == "/api/sessions":
            p = app.store.project(s("project"))
            if not p:
                raise LookupError("no such project")
            sess = app.sessions.create(p, s("harness"), s("prompt"), title=s("title"), started_by="user",
                                       bot=s("bot") or None, model=s("model") or None)
            return {"session": app.sessions.summary(sess)}
        if m := re.fullmatch(rf"/api/sessions/({ID})/send", path):
            app.sessions.send(m.group(1), s("prompt"))
            return {}
        if m := re.fullmatch(rf"/api/sessions/({ID})/merge", path):
            return {"session": app.sessions.summary(app.sessions.merge(m.group(1)))}
        if m := re.fullmatch(rf"/api/sessions/({ID})/reviewed", path):
            return {"session": app.sessions.summary(app.sessions.mark_reviewed(m.group(1)))}
        if m := re.fullmatch(rf"/api/sessions/({ID})/stop", path):
            return {"stopping": app.sessions.stop(m.group(1))}
        if path == "/api/dashboard/remove":
            app.update_dashboard("remove", None, [s("id")], by="you")
            return {}
        if path == "/api/dashboard/hide":
            key = s("card")
            if not re.fullmatch(r"widget:[A-Za-z0-9_.:-]{1,80}", key):
                raise ValueError("card must be widget:<id>")
            d = app.store.hide_dashboard_card(key, b.get("hidden") is not False)
            app.store.event("dashboard.hide", actor="you", where="dashboard", data={"card": key, "hidden": key in d["hidden"]})
            app.bus.publish("dashboard.updated", {"dashboard": d})
            return {"dashboard": d}
        if path == "/api/push/subscribe":
            rec = app.push.subscribe(b.get("subscription") or {}, device=dev.get("id") or "",
                                     ua=self.headers.get("User-Agent", ""))
            return {"subscription": rec["id"]}
        if path == "/api/push/unsubscribe":
            return {"removed": app.push.unsubscribe(endpoint=s("endpoint") or None, device=None if s("endpoint") else dev.get("id"))}
        if path == "/api/push/test":
            lang = app.store.settings().get("lang") or "en"
            app.push.notify("needs_you", "SnapLab", app.t("push.test"),
                            url="/#/settings", tag="test", force=True, wait=True)
            return {"results": app.push.last_results}
        raise LookupError("no such endpoint")

    def upload(self, room_id: str):
        """A file for the chat, sent as the raw request body (any type and size). Same checks as the JSON posts:
        a paired device, the app's own origin and the X-SnapLab header; the name comes in X-SnapLab-Name."""
        from urllib.parse import unquote
        self.close_connection = True
        if self.gate(api=True) is None:
            return
        origin = self.headers.get("Origin")
        if (self.headers.get("X-SnapLab") != "1" or self.headers.get("Sec-Fetch-Site", "same-origin") not in ("same-origin", "none")
                or (origin and self.own_origin() and origin.rstrip("/") != self.own_origin())):
            self.send_json(403, {"ok": False, "error": "not allowed from here"})
            return
        if self.headers.get("Transfer-Encoding") or len(self.headers.get_all("Content-Length") or []) != 1:
            self.send_json(411, {"ok": False, "error": "send the file with its length"})
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            size = -1
        try:
            meta = uploads.receive(self.app, room_id, unquote(self.headers.get("X-SnapLab-Name") or ""),
                                   (self.headers.get("Content-Type") or "").split(";")[0].strip().lower(), self.rfile, size)
        except LookupError as e:
            self.send_json(404, {"ok": False, "error": str(e).strip("'\"")})
            return
        except ValueError as e:
            self.send_json(400, {"ok": False, "error": str(e)})
            return
        except OSError:
            self.send_json(500, {"ok": False, "error": "the file could not be stored"})
            return
        self.send_json(200, {"ok": True, "file": meta})

    def do_PUT(self):
        self.send_json(405, {"ok": False, "error": "method not allowed"})

    do_DELETE = do_PATCH = do_OPTIONS = do_PUT


_STOPPING: dict[int, threading.Event] = {}


def app_stopping(app) -> bool:
    ev = _STOPPING.get(id(app))
    return bool(ev and ev.is_set())


class Server(ThreadingHTTPServer):
    daemon_threads = True
    # On Windows SO_REUSEADDR would let another program take the port, so it stays off there. On Linux and macOS it
    # only lets a restart bind while the old connections wait out TIME_WAIT; without it systemd restarts failed with
    # "Address already in use" for about a minute (found on Ubuntu, 2026-11-20).
    allow_reuse_address = os.name != "nt"


def make_server(app, host: str, port: int) -> Server:
    if host not in ("127.0.0.1", "::1", "localhost"):
        raise ValueError("SnapLab listens on 127.0.0.1 only; publish it with tailscale serve")
    handler = type("SnapLabHandler", (Handler,), {"app": app})
    return Server((host, port), handler)


def serve_forever(app, host: str, port: int) -> None:
    srv = make_server(app, host, port)
    stop_file = app.store.path("stop")
    stop_file.unlink(missing_ok=True)
    stopping = _STOPPING.setdefault(id(app), threading.Event())
    info = {"pid": os.getpid(), "port": port, "host": host, "started": now_iso()}
    app.store.write_json("server.json", info)
    app.log(f"started on {host}:{port} pid {os.getpid()}")

    def watch():
        while not stopping.is_set():
            if stop_file.exists():
                stop_file.unlink(missing_ok=True)
                stopping.set()
                srv.shutdown()
                return
            time.sleep(1)
    threading.Thread(target=watch, daemon=True).start()
    app.start_status_watch()
    app.sessions.watch_plans()

    def background():
        app.housekeeper.start()
        app.tasks.start()
    from . import deploy
    deploy.begin(app, stopping, background)     # after an update, once the updater has checked the data
    try:
        srv.serve_forever(poll_interval=0.5)
    finally:
        stopping.set()
        srv.server_close()
        app._status_stop.set()
        app.housekeeper.stop.set()
        if app.translator:
            app.translator.stop()
        app.close_mcp()
        app.browsers.close_all()
        try:
            app.store.path("server.json").unlink()
        except OSError:
            pass
        app.log("stopped")
