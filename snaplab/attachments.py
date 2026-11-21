"""Private, immutable copies of files produced by a session. No arbitrary path serving."""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
import threading
from pathlib import Path, PurePosixPath
from urllib.parse import quote

from .store import now_iso

# One delivered file may be as large as the worker's file transfer allows (remote_worker.MAX_FILE). There is no limit
# on how many files a turn delivers (until 2026-11-21: 8 files of 20 MiB).
MAX_BYTES = 4 * 1024 ** 3
ID_RE = re.compile(r"f-[0-9a-f]{32}\Z")
MANIFEST = ".snaplab/attachments.json"
PASSIVE_CSP = "sandbox; default-src 'none'; frame-ancestors 'self'"
INSTRUCTIONS = """
Deliver results to the user's phone, not PC file paths. Put an explicit JSON list in
.snaplab/attachments.json, e.g. [{"path":"index.html","caption":"Interactive schedule"},
{"path":"screenshot.png","caption":"Phone view"}]. SnapLab copies these files to private
storage and attaches them to the result card after the turn. Use relative paths inside this
workspace (up to 4 GiB a file), only files created or modified by this session.
For a screenshot of a local HTML app, run:
python .snaplab/capture.py index.html --output screenshot.png --width 390 --height 844
The helper runs Edge/Chrome headlessly inside the existing Windows sandbox, with an isolated
profile inside the workspace. It never captures the user's desktop. Do not run it outside
the sandbox. If capture fails, report the failure and still deliver the HTML.
HTML attachments are download-only; SnapLab never runs or previews them on the phone.
Deliver a screenshot so the user can inspect the app's appearance in chat.
If an older file has no recorded production hash, prepare a new self-contained delivery copy
inside this workspace. Never ask SnapLab to read a file outside the workspace.
Never claim a file was delivered unless SnapLab confirms it. Do not include secrets.
In Bot-only sessions, use deliver_file(path, caption) to select the files instead of writing
the manifest. This queues delivery at the end of the turn; it does not run code or take screenshots.
"""


def relative_path(value):
    if not isinstance(value, str) or not value or len(value) > 400:
        raise ValueError("give a relative file path")
    value = value.replace("\\", "/")
    parts = value.split("/")
    if any(p in ("", ".", "..") or p.startswith(".") or p.endswith((" ", ".")) for p in parts):
        raise ValueError("hidden and parent paths cannot be attached")
    if any(c in value for c in ':\x00<>"|?*') or any(ord(c) < 32 for c in value):
        raise ValueError("invalid attachment path")
    if any(p.lower() in {"node_modules", "__pycache__", "venv"} for p in parts):
        raise ValueError("dependency files cannot be attached")
    return PurePosixPath(value).as_posix()


def _check_parts(root, path):
    """Reject links, Windows junctions/reparse points and hard links, including parents."""
    if not path.is_relative_to(root):
        raise ValueError("file is outside the workspace")
    for p in [root, *reversed(path.parents[:len(path.parents) - len(root.parents) - 1]), path]:
        info = p.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("linked files cannot be attached")
    if path.resolve() != path or not path.is_file():
        raise ValueError("attachment must be a regular file")


def read_local(root, rel, limit=MAX_BYTES, *, internal=False):
    root = Path(os.path.abspath(root))
    path = root / (rel if internal else relative_path(rel))
    _check_parts(root, path)
    with path.open("rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > limit:
            raise ValueError("attachment must be a regular unlinked file within the size limit")
        # Check the opened handle, not just the pathname, against parent-link replacement.
        if os.name == "nt":
            import ctypes
            import msvcrt
            from ctypes import wintypes
            fn = ctypes.WinDLL("kernel32", use_last_error=True).GetFinalPathNameByHandleW
            fn.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
            fn.restype = wintypes.DWORD
            buf = ctypes.create_unicode_buffer(32768)
            n = fn(msvcrt.get_osfhandle(stream.fileno()), buf, len(buf), 0)
            if not n or n >= len(buf) or Path(buf.value.removeprefix("\\\\?\\")) != path:
                raise ValueError("attachment path changed")
        elif Path("/proc/self/fd").exists():
            if Path(os.readlink(f"/proc/self/fd/{stream.fileno()}")) != path:
                raise ValueError("attachment path changed")
        raw = stream.read(min(limit, before.st_size) + 1)   # read(n) reserves n bytes first: size it by the file
        after = os.fstat(stream.fileno())
        _check_parts(root, path)
        current = path.stat()
        if (len(raw) > limit or before.st_nlink != after.st_nlink or
                (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns) or
                (before.st_dev, before.st_ino) != (current.st_dev, current.st_ino)):
            raise ValueError("attachment changed while being read")
    return raw


def kind_for(name, raw):
    # Raster only. SVG and arbitrary HTML-like files are never embedded as images.
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image", "image/png"
    if raw.startswith(b"\xff\xd8\xff"):
        return "image", "image/jpeg"
    if raw.startswith((b"GIF87a", b"GIF89a")):
        return "image", "image/gif"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image", "image/webp"
    suffix = Path(name).suffix.lower()
    if suffix in (".html", ".htm"):
        raw.decode("utf-8-sig")
        return "html", "text/html; charset=utf-8"
    if suffix == ".pdf" and raw.startswith(b"%PDF-"):
        return "pdf", "application/pdf"
    if suffix in (".txt", ".md", ".csv", ".json", ".log"):
        raw.decode("utf-8-sig")
        return "text", "text/plain; charset=utf-8"
    return "file", "application/octet-stream"


class Attachments:
    def __init__(self, app):
        self.app = app
        self.lock = threading.RLock()

    def publish_generated(self, image, name, room, bot):
        """Accept only an image owned by the server's generator, never a client path."""
        raw = read_local(image.parent, image.name)
        kind, mime = kind_for(name, raw)
        if kind != 'image' or any(s.encode(enc) in raw for s in self.app.guard.secrets()
                                  for enc in ('utf-8', 'utf-16-le')):
            raise ValueError('Generated image cannot be delivered.')
        with self.lock:
            aid = 'f-' + secrets.token_hex(16)
            folder = self.app.store.path('attachments', aid)
            folder.mkdir(parents=True)
            (folder / 'content').write_bytes(raw)
            meta = {'id': aid, 'name': name, 'caption': '', 'kind': kind, 'mime': mime, 'size': len(raw),
                    'sha256': hashlib.sha256(raw).hexdigest(), 'session': '', 'project': '', 'created': now_iso()}
            self.app.store.write_json(folder / 'meta.json', meta)
            message = self.app.store.add_message(room['id'], {'role': 'bot', 'bot': bot['id'],
                'text': name, 'status': 'done', 'attachments': [meta]})
            self.app.bus.publish('message.new', {'room': room['id'], 'message': message})
            self.app.bus.publish('room.updated', {'room': self.app.store.room(room['id'])})
            return meta

    def queue(self, folder, rel, caption=""):
        rel = relative_path(rel)
        read_local(folder, rel)
        hidden = folder / ".snaplab"
        if hidden.is_symlink() or hidden.resolve() != folder.resolve() / ".snaplab":
            raise ValueError("invalid metadata folder")
        hidden.mkdir(exist_ok=True)
        manifest = folder / MANIFEST
        with self.lock:
            items = json.loads(read_local(folder, MANIFEST, internal=True).decode("utf-8-sig")) if manifest.exists() else []
            if not isinstance(items, list):
                raise ValueError("invalid manifest")
            items = [item for item in items if isinstance(item, dict) and item.get("path") != rel]
            items.append({"path": rel, "caption": str(caption)[:300]})
            manifest.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")

    def register_outputs(self, session, folder, changes):
        """Record exact content hashes in trusted server storage, never in the worktree."""
        outputs = dict(session.get("produced_files") or {})
        for change in changes:
            rel = change["path"]
            outputs.pop(rel, None)
            if change["change"] != "deleted":
                try:
                    raw = read_local(folder, rel)
                    outputs[rel] = hashlib.sha256(raw).hexdigest()
                except (OSError, ValueError):
                    pass
        session["produced_files"] = outputs

    def create(self, session, rel, caption=""):
        if not isinstance(caption, str):
            raise ValueError("caption must be text")
        rel = relative_path(rel)
        project = self.app.store.project(session["project"])
        if not project:
            raise ValueError("project no longer exists")
        folder = self.app.sessions.workdir(session, project)
        raw = read_local(folder, rel)
        digest = hashlib.sha256(raw).hexdigest()
        if (session.get("produced_files") or {}).get(rel) != digest:
            raise ValueError("file was not produced by this session, or changed since its turn; ask the session to prepare it")
        if not raw:
            raise ValueError("file is empty")
        for secret in self.app.guard.secrets():
            if any(secret.encode(enc) in raw for enc in ("utf-8", "utf-16-le")):
                raise ValueError("file contains a secret and cannot be attached")
        kind, mime = kind_for(rel, raw)
        name = Path(rel).name
        caption = self.app.guard.clean(str(caption or ""))[:300]
        from .prompts import wrong_language
        lang = (self.app.store.room(session.get('room')) or {}).get('lang') or self.app.lang
        if wrong_language(caption, lang, session.get('goal', '')):
            caption = self.app.t('attachment.result_caption')
        # Repeated manifests and Bot retries reuse the immutable stored copy.
        key = hashlib.sha256(f"{session['id']}\0{rel}\0{digest}\0{caption}".encode()).hexdigest()
        with self.lock:
            index = self.app.store.read_json("attachments/index.json", {})
            if key in index:
                existing = self.get(index[key])
                if existing:
                    return existing
            aid = "f-" + secrets.token_hex(16)
            folder = self.app.store.path("attachments", aid)
            folder.mkdir(parents=True)
            (folder / "content").write_bytes(raw)
            meta = {"id": aid, "name": name, "caption": caption, "kind": kind, "mime": mime,
                    "size": len(raw), "sha256": digest, "session": session["id"],
                    "project": session["project"], "created": now_iso()}
            self.app.store.write_json(folder / "meta.json", meta)
            index[key] = aid
            self.app.store.write_json("attachments/index.json", index)
            return meta

    def get(self, aid):
        if not isinstance(aid, str) or not ID_RE.fullmatch(aid):
            return None
        return self.app.store.read_json(self.app.store.path("attachments", aid, "meta.json"), None)

    def in_room(self, room_id, offset=0, limit=40):
        seen, items = set(), []
        for message in reversed(self.app.store.messages(room_id, limit=0)):
            for meta in message.get("attachments") or []:
                aid = meta.get("id")
                if isinstance(aid, str) and ID_RE.fullmatch(aid) and aid not in seen:
                    seen.add(aid)
                    items.append(meta)
        return {"attachments": items[offset:offset + limit], "has_more": len(items) > offset + limit}

    def response(self, aid, mode):
        meta = self.get(aid)
        if not meta or mode not in ("content", "download", "preview"):
            return None
        if mode == "preview" and meta["kind"] not in ("pdf", "text", "image"):
            return None
        try:
            raw = read_local(self.app.store.path("attachments", aid), "content")
        except (OSError, ValueError):
            return None
        if hashlib.sha256(raw).hexdigest() != meta["sha256"]:
            return None
        # A key discovered/rotated after publication must not escape on a later download.
        if any(s.encode(enc) in raw for s in self.app.guard.secrets() for enc in ("utf-8", "utf-16-le")):
            return None
        inline = mode == "preview" or (mode == "content" and meta["kind"] == "image")
        mime = meta["mime"] if inline else "application/octet-stream"
        headers = {"Cache-Control": "no-store", "Cross-Origin-Resource-Policy": "same-origin",
                   "Content-Disposition": ("inline" if inline else "attachment") +
                   "; filename=\"download\"; filename*=UTF-8''" + quote(meta["name"], safe=""),
                   "Content-Security-Policy": PASSIVE_CSP}
        return raw, mime, headers

    def collect(self, session, folder):
        """Only explicit exports are published. An invalid entry does not hide valid files."""
        if not (folder / MANIFEST).exists():
            return [], []
        try:
            items = json.loads(read_local(folder, MANIFEST, internal=True).decode("utf-8-sig"))
            if not isinstance(items, list):
                raise ValueError("the manifest must be a JSON list")
        except (OSError, ValueError):
            return [], ["invalid_manifest"]
        out, errors = [], []
        for item in items:
            try:
                if not isinstance(item, dict) or not isinstance(item.get("caption", ""), str):
                    raise ValueError("invalid entry")
                meta = self.create(session, item.get("path"), item.get("caption", ""))
                if meta["id"] not in {a["id"] for a in out}:
                    out.append(meta)
            except (OSError, ValueError) as exc:
                # Never include raw OS errors (absolute paths) or uncontrolled manifest values.
                try:
                    name = relative_path(item.get("path")) if isinstance(item, dict) else ""
                except ValueError:
                    name = ""
                reason = str(exc) if isinstance(exc, ValueError) else "file could not be read or stored"
                errors.append({"code": "file_refused", "path": self.app.guard.clean(name),
                               "reason": self.app.guard.clean(reason)[:300]})
        return out, errors

    def publish(self, session, room, bot, rel, caption=""):
        meta = self.create(session, rel, caption)
        with self.lock:
            # A retried tool call should not add duplicate cards to the same chat.
            for message in self.app.store.messages(room["id"]):
                if any(a.get("id") == meta["id"] for a in message.get("attachments") or []):
                    return meta
            message = self.app.store.add_message(room["id"], {"role": "bot", "bot": bot["id"],
                "text": meta["caption"] or meta["name"], "status": "done", "attachments": [meta]})
            self.app.bus.publish("message.new", {"room": room["id"], "message": message})
            self.app.bus.publish("room.updated", {"room": self.app.store.room(room["id"])})
        return meta
