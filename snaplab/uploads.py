"""Files the user sends to a chat: photos (also taken with the camera), PDFs, text, documents and any other file.

They are kept like the files sessions deliver (data/attachments/<id>/content and meta.json, with "from": "user"),
attached to the user's message, shown in the chat and in the room's file list, and handed on:

- to the bot: each file is a line in the history ("[File from the user: ...]"). Images go to the model as images
  when it can see them (a smaller copy without the photo's metadata, view.jpg); the newest message also carries
  the text of text files, PDFs and Office documents. Older files and the rest of a long file: read_attachment.
- to SnapCode: start_session / send_to_session with files=[ids] copy them into the workspace under from_chat/.

No size or count limits beyond the disk. The name the phone gives is only a display name, never a path.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import re
import secrets
import shutil
import threading
import zipfile
from pathlib import Path
from urllib.parse import quote

from .store import now_iso

ID_RE = re.compile(r"f-[0-9a-f]{32}\Z")
MARK_RE = re.compile(r"^\[File from the user: .*\(id (f-[0-9a-f]{32})\)\]$", re.M)
FOLDER = "from_chat"               # where a session finds the files a bot gave it
VIEW_SIDE = 1600                   # the copy the model sees (the original stays as it was sent)
CHUNK = 1024 * 1024
TEXT_EXT = {".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".jsonl", ".log", ".xml", ".yaml", ".yml", ".toml",
            ".ini", ".cfg", ".conf", ".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".css", ".scss", ".sql",
            ".sh", ".bash", ".ps1", ".bat", ".cmd", ".c", ".h", ".cpp", ".hpp", ".cc", ".cs", ".java", ".kt", ".go",
            ".rs", ".rb", ".php", ".swift", ".lua", ".r", ".m", ".tex", ".srt", ".vtt", ".svg", ".diff", ".patch",
            ".env.example", ".gitignore", ".dockerfile", ".gd", ".glsl", ".vue", ".svelte"}
OFFICE = {".docx": "Word document", ".xlsx": "Excel workbook", ".pptx": "PowerPoint presentation"}
LOCK = threading.RLock()


# ---------------------------------------------------------------- receiving

def clean_name(value) -> str:
    """A display name: no folders, control characters or characters Windows refuses."""
    name = str(value or "").replace("\\", "/").rsplit("/", 1)[-1]
    name = re.sub(r'[\x00-\x1f\x7f<>:"|?*]', "", name).strip(" .")
    if not name or name in (".", ".."):
        name = "file"
    if len(name) > 120:
        stem, dot, ext = name.rpartition(".")
        name = (stem[:110] + dot + ext[:9]) if dot and len(ext) <= 9 else name[:120]
    return name


def _sniff_text(raw: bytes) -> str | None:
    """utf-8 (with or without BOM), then cp932 (Japanese Windows files). None when it does not look like text."""
    if b"\x00" in raw[:8192]:
        return None
    for enc in ("utf-8-sig", "cp932"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError as e:
            if enc == "utf-8-sig" and e.start >= len(raw) - 3:   # only a character cut at the end of the sample
                return raw[:e.start].decode(enc)
    return None


def detect(name: str, head: bytes, declared: str = "") -> tuple[str, str]:
    """(kind, mime) from the content first, the name second. kind: image, pdf, html, text or file."""
    from .attachments import kind_for
    if head.startswith((b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF87a", b"GIF89a")) or (head[:4] == b"RIFF" and head[8:12] == b"WEBP"):
        return kind_for(name, head)
    if head.startswith(b"%PDF-"):
        return "pdf", "application/pdf"
    suffix = Path(name).suffix.lower()
    looks_text = _sniff_text(head[:65536]) is not None
    if suffix in (".html", ".htm") and looks_text:
        return "html", "text/html; charset=utf-8"
    if looks_text and (suffix in TEXT_EXT or not suffix or declared.startswith("text/")):
        return "text", "text/plain; charset=utf-8"
    return "file", "application/octet-stream"


def _image_info(path: Path) -> dict:
    try:
        from PIL import Image
        with Image.open(path) as im:
            w, h = im.size
            if im.getexif().get(0x0112) in (5, 6, 7, 8):        # turned by the camera: show it as it was held
                w, h = h, w
            return {"width": w, "height": h}
    except Exception:  # noqa: BLE001 - Pillow is optional; size is only shown to the bot
        return {}


def receive(app, room_id: str, name, declared: str, stream, length: int) -> dict:
    """Store one upload from the phone or PC. Not part of a message until the message is sent (pending)."""
    room = app.store.room(room_id)
    if not room:
        raise LookupError("no such room")
    if length <= 0:
        raise ValueError("the file is empty")
    name = clean_name(name)
    declared = declared if re.fullmatch(r"[\w.+-]{1,60}/[\w.+-]{1,80}", declared or "") else ""
    aid = "f-" + secrets.token_hex(16)
    folder = app.store.path("attachments", aid)
    folder.mkdir(parents=True)
    part = folder / "content.part"
    digest, got, head = hashlib.sha256(), 0, b""
    try:
        with part.open("wb") as out:
            while got < length:
                chunk = stream.read(min(CHUNK, length - got))
                if not chunk:
                    raise ValueError("the upload was cut off; send the file again")
                out.write(chunk)
                digest.update(chunk)
                if len(head) < 65536:
                    head += chunk[:65536 - len(head)]
                got += len(chunk)
        part.replace(folder / "content")
    except BaseException:
        shutil.rmtree(folder, ignore_errors=True)
        raise
    kind, mime = detect(name, head, declared)
    meta = {"id": aid, "name": name, "caption": "", "kind": kind, "mime": mime, "type": declared, "size": got,
            "sha256": digest.hexdigest(), "session": "", "project": "", "from": "user", "room": room_id,
            "created": now_iso(), "pending": True}
    if kind == "image":
        meta.update(_image_info(folder / "content"))
    app.store.write_json(folder / "meta.json", meta)
    return public(meta)


def public(meta: dict) -> dict:
    """What a message keeps and the phone sees."""
    return {k: meta[k] for k in ("id", "name", "caption", "kind", "mime", "type", "size", "from", "width", "height")
            if k in meta}


def get(app, aid) -> dict | None:
    if not isinstance(aid, str) or not ID_RE.fullmatch(aid):
        return None
    return app.store.read_json(app.store.path("attachments", aid, "meta.json"), None)


def content_path(app, aid) -> Path:
    return app.store.path("attachments", aid, "content")


def claim(app, room_id: str, ids) -> list[dict]:
    """The uploads a message carries: they must be this room's uploads. Sending the same file again is fine."""
    if not ids:
        return []
    if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
        raise ValueError("files must be a list of file ids")
    out = []
    with LOCK:
        for aid in dict.fromkeys(ids):
            meta = get(app, aid)
            if not meta or meta.get("from") != "user" or meta.get("room") != room_id or not content_path(app, aid).is_file():
                raise ValueError("a file was not found; attach it again")
            if meta.get("pending"):
                meta.pop("pending")
                meta["sent"] = now_iso()
                app.store.write_json(app.store.path("attachments", aid, "meta.json"), meta)
            out.append(public(meta))
    return out


def discard(app, aid: str) -> bool:
    """Remove an upload the user took back before sending. Sent files stay (they belong to the history)."""
    with LOCK:
        meta = get(app, aid)
        if not meta or meta.get("from") != "user":
            raise LookupError("no such file")
        if not meta.get("pending"):
            return False
        shutil.rmtree(app.store.path("attachments", aid), ignore_errors=True)
        return True


def response(app, meta: dict, mode: str):
    """(path, mime, headers) for the server to stream. Images and PDF/text previews open in the app, the rest
    downloads. Nothing the user sent ever runs: previews carry the sandbox policy of session files."""
    from .attachments import PASSIVE_CSP
    if mode not in ("content", "download", "preview"):
        return None
    path = content_path(app, meta["id"])
    if not path.is_file():
        return None
    if mode == "preview" and meta["kind"] not in ("pdf", "text", "image"):
        return None
    inline = mode == "preview" or (mode == "content" and meta["kind"] == "image")
    mime = meta["mime"] if inline else "application/octet-stream"
    headers = {"Cache-Control": "private, max-age=86400", "Cross-Origin-Resource-Policy": "same-origin",
               "Content-Disposition": ("inline" if inline else "attachment") +
               "; filename=\"download\"; filename*=UTF-8''" + quote(meta["name"], safe=""),
               "Content-Security-Policy": PASSIVE_CSP}
    return path, mime, headers


# ---------------------------------------------------------------- reading

def describe(meta: dict) -> str:
    size = meta.get("size") or 0
    amount = f"{size / 1024 / 1024:.1f} MB" if size >= 1024 * 1024 else f"{max(1, round(size / 1024))} KB"
    kind = meta.get("kind")
    suffix = Path(meta.get("name", "")).suffix.lower()
    if kind == "image":
        what = "image" + (f" {meta['width']}×{meta['height']}" if meta.get("width") else "")
    elif kind == "pdf":
        what = "PDF"
    elif kind in ("text", "html"):
        what = "text file" if kind == "text" else "HTML file"
    elif suffix in OFFICE:
        what = OFFICE[suffix]
    else:
        what = (meta.get("type") or "file").split(";")[0]
    return f"{what}, {amount}"


def line(meta: dict) -> str:
    return f"[File from the user: {meta.get('name')} — {describe(meta)} (id {meta['id']})]"


def lines(message: dict) -> str:
    """The history lines of the files a user message carries (context.py puts them before the text)."""
    return "\n".join(line(a) for a in message.get("attachments") or [] if isinstance(a, dict)
                     and ID_RE.fullmatch(str(a.get("id") or "")))


def _office_text(path: Path, suffix: str) -> str:
    from xml.etree import ElementTree as ET
    out = []
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        if suffix == ".docx":
            parts = [n for n in names if re.fullmatch(r"word/(document|footnotes|endnotes|header\d*|footer\d*)\.xml", n)]
            parts.sort(key=lambda n: (not n.startswith("word/document"), n))
            for n in parts:
                root = ET.fromstring(z.read(n))
                for p in root.iter("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}p"):
                    text = ""
                    for t in p.iter():
                        tag = t.tag.rsplit("}", 1)[-1]
                        text += (t.text or "") if tag == "t" else "\t" if tag == "tab" else "\n" if tag == "br" else ""
                    out.append(text)
        elif suffix == ".pptx":
            slides = sorted((n for n in names if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)),
                            key=lambda n: int(re.search(r"(\d+)", n.rsplit("/", 1)[1]).group(1)))
            for i, n in enumerate(slides, 1):
                root = ET.fromstring(z.read(n))
                out.append(f"[Slide {i}]")
                for p in root.iter("{http://schemas.openxmlformats.org/drawingml/2006/main}p"):
                    text = "".join(t.text or "" for t in p.iter("{http://schemas.openxmlformats.org/drawingml/2006/main}t"))
                    if text.strip():
                        out.append(text)
        elif suffix == ".xlsx":
            ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
            shared = []
            if "xl/sharedStrings.xml" in names:
                for si in ET.fromstring(z.read("xl/sharedStrings.xml")).iter(ns + "si"):
                    shared.append("".join(t.text or "" for t in si.iter(ns + "t")))
            sheets = sorted((n for n in names if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", n)),
                            key=lambda n: int(re.search(r"(\d+)", n.rsplit("/", 1)[1]).group(1)))
            for i, n in enumerate(sheets, 1):
                out.append(f"[Sheet {i}]")
                for row in ET.fromstring(z.read(n)).iter(ns + "row"):
                    cells = []
                    for c in row.iter(ns + "c"):
                        v = c.find(ns + "v")
                        if c.get("t") == "s" and v is not None and (v.text or "").isdigit() and int(v.text) < len(shared):
                            cells.append(shared[int(v.text)])
                        elif c.get("t") == "inlineStr":
                            cells.append("".join(t.text or "" for t in c.iter(ns + "t")))
                        else:
                            cells.append(v.text if v is not None and v.text else "")
                    if any(cells):
                        out.append("\t".join(cells))
    return "\n".join(out).strip()


def _pdf_text(path: Path) -> tuple[str, str]:
    try:
        from pypdf import PdfReader
    except ImportError:
        return "", "pypdf is not installed on the PC, so SnapLab cannot read the PDF's text here."
    reader = PdfReader(str(path))
    if reader.is_encrypted:
        try:
            reader.decrypt("")
        except Exception:  # noqa: BLE001
            return "", "the PDF is protected with a password."
    pages = []
    for i, page in enumerate(reader.pages, 1):
        try:
            text = (page.extract_text() or "").strip()
        except Exception:  # noqa: BLE001 - one broken page must not hide the others
            text = "(this page could not be read)"
        pages.append(f"[Page {i}]\n{text}")
    joined = "\n\n".join(pages)
    if not re.sub(r"\[Page \d+\]|\s", "", joined):
        return "", (f"the PDF has {len(pages)} pages but no text layer (a scan or pictures). A SnapCode session can "
                    "read it with OCR tools: give it the file with files=[id].")
    return joined, ""


def text_of(app, meta: dict) -> tuple[str, str]:
    """(text, note). The text of text files, HTML, PDFs and Office documents, kept next to the file once read."""
    folder = app.store.path("attachments", meta["id"])
    cache = folder / "text.json"
    if cache.is_file():
        try:
            saved = json.loads(cache.read_text(encoding="utf-8"))
            return saved.get("text", ""), saved.get("note", "")
        except (OSError, ValueError):
            pass
    path = folder / "content"
    suffix = Path(meta.get("name", "")).suffix.lower()
    text, note = "", ""
    try:
        if meta.get("kind") in ("text", "html"):
            raw = path.read_bytes()
            text = _sniff_text(raw)
            if text is None:
                text = raw.decode("utf-8", "replace")
        elif meta.get("kind") == "pdf":
            text, note = _pdf_text(path)
        elif suffix in OFFICE:
            text = _office_text(path, suffix)
            if not text:
                note = "the document has no text."
        else:
            return "", ""
    except (OSError, ValueError, KeyError, zipfile.BadZipFile) as e:
        return "", f"SnapLab could not read it ({type(e).__name__})."
    except Exception as e:  # noqa: BLE001 - a broken document must not break the bot's turn
        return "", f"SnapLab could not read it ({type(e).__name__})."
    text = app.guard.clean(text.replace("\r\n", "\n"))
    try:
        app.store.write_json(cache, {"text": text, "note": note})
    except OSError:
        pass
    return text, note


def readable(meta: dict) -> bool:
    return meta.get("kind") in ("text", "html", "pdf") or Path(meta.get("name", "")).suffix.lower() in OFFICE


def image_view(app, meta: dict, region=None, side: int = VIEW_SIDE) -> tuple[str, bytes] | None:
    """(mime, bytes) of an image for the model: turned upright, at most `side` pixels, metadata (place, camera)
    left out. region = [x, y, width, height] in the pixels of the upright original, for a closer look."""
    path = content_path(app, meta["id"])
    cache = app.store.path("attachments", meta["id"], "view.jpg")
    if region is None and cache.is_file():
        return "image/jpeg", cache.read_bytes()
    try:
        from PIL import Image, ImageOps
    except ImportError:
        if region is None and meta.get("mime") in ("image/png", "image/jpeg", "image/webp", "image/gif") and meta.get("size", 0) <= 8 * 1024 * 1024:
            return meta["mime"], path.read_bytes()
        return None
    try:
        with Image.open(path) as im:
            im.seek(0)
            im = ImageOps.exif_transpose(im)
            if region is not None:
                x, y, w, h = (int(v) for v in region)
                if w <= 0 or h <= 0 or x < 0 or y < 0 or x >= im.width or y >= im.height:
                    raise ValueError(f"region must lie inside the image ({im.width}×{im.height} pixels)")
                im = im.crop((x, y, min(im.width, x + w), min(im.height, y + h)))
            if im.mode in ("RGBA", "LA", "P", "PA"):
                im = im.convert("RGBA")
                back = Image.new("RGB", im.size, (255, 255, 255))
                back.paste(im, mask=im.getchannel("A"))
                im = back
            else:
                im = im.convert("RGB")
            im.thumbnail((side, side))
            out = io.BytesIO()
            im.save(out, "JPEG", quality=85)
    except ValueError:
        raise
    except Exception:  # noqa: BLE001 - an image Pillow cannot open is reported, not shown
        return None
    data = out.getvalue()
    if region is None:
        try:
            cache.write_bytes(data)
        except OSError:
            pass
    return "image/jpeg", data


def data_url(mime: str, data: bytes) -> str:
    return f"data:{mime};base64," + base64.b64encode(data).decode("ascii")


def can_see(app, bot: dict) -> bool:
    """Whether the bot's model reads images (its capabilities in the model settings)."""
    try:
        spec = app.models.spec(bot.get("model"), "chat")
    except Exception:  # noqa: BLE001
        return False
    from .provider_protocol import capabilities
    return bool(capabilities(spec).get("vision"))


# ---------------------------------------------------------------- the bot's view

def expand(app, messages: list[dict], *, vision: bool, images: int = 6, text_tokens: int = 6000) -> None:
    """Turn the file lines of the context (in place) into what the model can use: images after their line (the
    newest `images` of them), and in the newest message the text of readable files (about `text_tokens`)."""
    from .context import clip_tokens
    shown = 0
    last = len(messages) - 1
    for index in range(last, -1, -1):
        m = messages[index]
        content = m.get("content")
        if m.get("role") != "user" or not isinstance(content, str) or "[File from the user: " not in content:
            continue
        found = list(MARK_RE.finditer(content))
        if not found:
            continue
        parts, pos, budget = [], 0, text_tokens
        for hit in found:
            meta = get(app, hit.group(1))
            parts.append({"type": "text", "text": content[pos:hit.end()]})
            pos = hit.end()
            if not meta:
                parts.append({"type": "text", "text": "\n(This file is no longer stored.)"})
                continue
            fid = meta["id"]
            if meta.get("kind") == "image":
                view = image_view(app, meta) if vision and shown < images else None
                if view:
                    shown += 1
                    parts.append({"type": "image_url", "image_url": {"url": data_url(*view)}})
                elif not vision:
                    parts.append({"type": "text", "text": "\n(Your model cannot see images. You can give the image to a "
                                  f"SnapCode session with files=[\"{fid}\"].)"})
                elif shown >= images:
                    parts.append({"type": "text", "text": f"\n(Not shown again here; read_attachment(\"{fid}\") shows it.)"})
                else:
                    parts.append({"type": "text", "text": "\n(SnapLab cannot show this image format. A SnapCode session "
                                  f"can open it with files=[\"{fid}\"].)"})
            elif readable(meta):
                if index == last and budget > 200:
                    text, note = text_of(app, meta)
                    if text:
                        part = clip_tokens(text, budget)
                        budget -= max(1, len(part) // 3)
                        rest = (f"\n(The first {len(part)} of {len(text)} characters. read_attachment(\"{fid}\", "
                                f"from_char={len(part) - 2}) reads on.)" if len(part) < len(text) else "")
                        parts.append({"type": "text", "text": f"\nContents of {meta['name']}:\n<<<\n{part}\n>>>{rest}"})
                    else:
                        parts.append({"type": "text", "text": f"\n(SnapLab could not read its text: {note})"})
                else:
                    parts.append({"type": "text", "text": f"\n(read_attachment(\"{fid}\") reads it.)"})
            else:
                parts.append({"type": "text", "text": "\n(SnapLab cannot read this kind of file in chat. A SnapCode session "
                              f"can work with it: give it with files=[\"{fid}\"].)"})
        parts.append({"type": "text", "text": content[pos:]})
        merged: list[dict] = []
        for p in parts:
            if p["type"] == "text" and merged and merged[-1]["type"] == "text":
                merged[-1] = {"type": "text", "text": merged[-1]["text"] + p["text"]}
            elif p["type"] != "text" or p["text"]:
                merged.append(p)
        if any(p["type"] == "image_url" for p in merged):
            messages[index] = {**m, "content": merged}
        else:
            messages[index] = {**m, "content": "".join(p["text"] for p in merged)}


def read(app, meta: dict, *, vision: bool, from_char: int = 0, max_chars: int = 30000, region=None):
    """read_attachment: (text for the model, images for the model)."""
    head = f"{meta['name']} — {describe(meta)} (id {meta['id']})"
    if meta.get("kind") == "image":
        if not vision:
            return head + "\nYour model cannot see images. Give it to a SnapCode session with files=[id].", []
        view = image_view(app, meta, region=region)
        if not view:
            return head + "\nSnapLab cannot show this image format. A SnapCode session can open it with files=[id].", []
        where = f" (region {region})" if region else ""
        return head + f"\nThe image{where} follows.", [{"mime": view[0], "data": base64.b64encode(view[1]).decode("ascii")}]
    if readable(meta):
        text, note = text_of(app, meta)
        if not text:
            return head + f"\nNo text: {note}", []
        start = max(0, int(from_char or 0))
        size = max(1000, int(max_chars or 30000))
        part = text[start:start + size]
        more = (f"\n[{len(text) - start - len(part)} more characters: read_attachment(\"{meta['id']}\", "
                f"from_char={start + len(part)})]" if start + len(part) < len(text) else "\n[end of file]")
        return f"{head}\nCharacters {start}-{start + len(part)} of {len(text)}:\n<<<\n{part}\n>>>{more}", []
    return head + "\nSnapLab cannot read this kind of file in chat. A SnapCode session can work with it: files=[id].", []


# ---------------------------------------------------------------- handing files to SnapCode

def prepare(app, ids) -> list[dict]:
    """Check files of the chat (the user's and the ones bots delivered) before a session gets them: a secret
    SnapLab keeps never goes; text with private words goes with them replaced; other files with private words
    are refused, because the session's workspace would refuse them on the way to the worker."""
    if not ids:
        return []
    if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
        raise ValueError("files must be a list of file ids (f-...)")
    from .attachments import read_local
    out = []
    for aid in dict.fromkeys(ids):
        meta = get(app, aid)
        source = content_path(app, aid) if meta else None
        if not meta or not source.is_file():
            raise ValueError(f"no file {aid!r}; take the id from a [File from the user: ...] line or an attachment")
        if meta.get("from") != "user":       # a session's delivery: checked as attachments.py serves it
            read_local(source.parent, "content")
        raw = source.read_bytes() if meta.get("size", 0) <= 64 * 1024 * 1024 else b""
        if raw and any(s.encode(enc) in raw for s in app.guard.secrets() for enc in ("utf-8", "utf-16-le")):
            raise ValueError(f"{meta['name']} contains a secret SnapLab keeps; it cannot go to a session")
        private = raw and any(app.redactor.contains_private(raw.decode(enc, "ignore"))
                              for enc in ("utf-8", "utf-16-le", "utf-16-be"))
        text = None
        if private:
            if meta.get("kind") not in ("text", "html"):
                raise ValueError(f"{meta['name']} contains private information (such as the PC user's name), which a "
                                 "session workspace refuses; describe what is needed in the instruction instead")
            text = app.redactor.clean(_sniff_text(raw) or raw.decode("utf-8", "replace"))
        out.append({"meta": meta, "source": source, "text": text})
    return out


def give_to_session(app, folder: Path, files) -> str:
    """Copy prepared files (or ids) into a session's workspace under from_chat/. Returns the instruction's lines."""
    items = files if files and isinstance(files[0], dict) else prepare(app, files)
    if not items:
        return ""
    target = Path(folder) / FOLDER
    target.mkdir(parents=True, exist_ok=True)
    out = []
    for item in items:
        meta = item["meta"]
        name = clean_name(meta["name"])
        stem, suffix = Path(name).stem, Path(name).suffix
        dest, n = target / name, 2
        while dest.exists():
            dest, n = target / f"{stem} ({n}){suffix}", n + 1
        if item["text"] is not None:
            dest.write_text(item["text"], encoding="utf-8", newline="")
        else:
            shutil.copyfile(item["source"], dest)
        out.append(f"- {FOLDER}/{dest.name} ({describe(meta)}" + ("; private words were replaced" if item["text"] is not None else "") + ")")
    return "\n\nFiles from the chat, copied into this workspace (the user or a bot sent them):\n" + "\n".join(out)
