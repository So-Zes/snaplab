"""Bundled faces and bounded image uploads. No filenames supplied by clients are used."""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
import struct
import zlib
from functools import lru_cache

from .config import WEB

MAX_BYTES = 1024 * 1024
MAX_SIDE = 1024
UPLOAD_RE = re.compile(r"upload-[0-9a-f]{64}\.(?:png|jpg|webp)\Z")


@lru_cache(maxsize=1)
def catalog():
    data = json.loads((WEB / "avatars/avatars.json").read_text(encoding="utf-8"))
    seen = set()
    for face in data["avatars"]:
        key = face["id"]
        if not re.fullmatch(r"[a-e][1-8]", key) or key in seen or face["set"] != key[0]:
            raise ValueError("invalid avatar catalog")
        seen.add(key)
        for size in ("96", "256"):
            path = f"{key[0]}/{key}-{size}.png"
            if face["files"][size] != path or not (WEB / "avatars" / path).is_file():
                raise ValueError("missing avatar image")
    return data


def default_face(role="", used=(), lead=False):
    if lead:
        return "c1"
    faces = [f for f in catalog()["avatars"] if f["set"] == "c" and not f["lead_mark"]]
    # Role examples in the catalog are the recommendations, in both languages.
    ranked = sorted(faces, key=lambda f: -sum(word.casefold() in role.casefold()
                    for words in f["roles"].values() for word in words))
    return next((f["id"] for f in ranked if f["id"] not in used), ranked[0]["id"])


def validate_choice(value, store):
    if value == "":
        return value  # initials and colour, also the fallback for older bots
    if not isinstance(value, str):
        raise ValueError("invalid avatar")
    if value in {f["id"] for f in catalog()["avatars"]}:
        return value
    if UPLOAD_RE.fullmatch(value) and store.path("avatars", value).is_file():
        return value
    raise ValueError("invalid avatar")


def image_info(raw):
    """Check signatures, container boundaries and dimensions, without a third-party decoder.

    PNG also checks CRCs and the bounded pixel stream. JPEG/WebP validate their container
    and frame headers; the browser decodes and re-encodes the user's source before upload.
    """
    if not raw or len(raw) > MAX_BYTES:
        raise ValueError("image must be at most 1 MB")
    try:
        if raw.startswith(b"\x89PNG\r\n\x1a\n"):
            pos, width, height, pixels, ended = 8, 0, 0, bytearray(), False
            while pos < len(raw):
                n = int.from_bytes(raw[pos:pos + 4], "big")
                tag = raw[pos + 4:pos + 8]
                end = pos + n + 12
                if end > len(raw) or len(tag) != 4:
                    raise ValueError("invalid PNG chunk")
                part = raw[pos + 8:pos + 8 + n]
                crc = int.from_bytes(raw[pos + 8 + n:end], "big")
                if zlib.crc32(tag + part) != crc:
                    raise ValueError("invalid PNG checksum")
                if pos == 8:
                    if tag != b"IHDR" or n != 13:
                        raise ValueError("invalid PNG header")
                    width, height, depth, color, comp, filt, interlace = struct.unpack(">IIBBBBB", part)
                    _dimensions(width, height)
                    if comp or filt or interlace or depth != 8 or color not in (0, 2, 3, 4, 6):
                        raise ValueError("unsupported PNG encoding")
                elif tag == b"IHDR" or tag == b"acTL":
                    raise ValueError("invalid or animated PNG")
                if tag == b"IDAT":
                    pixels.extend(part)
                if tag == b"IEND":
                    ended = n == 0 and end == len(raw)
                    break
                pos = end
            if not ended or not pixels:
                raise ValueError("incomplete PNG")
            stride = width * {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[color] + 1
            expected = height * stride
            dec = zlib.decompressobj()
            decoded = dec.decompress(pixels, expected + 1)
            if len(decoded) != expected or not dec.eof or dec.unused_data or any(decoded[i] > 4 for i in range(0, expected, stride)):
                raise ValueError("invalid PNG pixels")
            return "png", width, height
        if raw.startswith(b"\xff\xd8") and raw.endswith(b"\xff\xd9"):
            pos, dims = 2, None
            while pos < len(raw) - 2:
                if raw[pos] != 255:
                    raise ValueError("invalid JPEG marker")
                while raw[pos] == 255:
                    pos += 1
                marker = raw[pos]
                pos += 1
                n = int.from_bytes(raw[pos:pos + 2], "big")
                if n < 2 or pos + n > len(raw) - 2:
                    raise ValueError("invalid JPEG segment")
                if marker in (0xC0, 0xC1, 0xC2):
                    if n < 11 or raw[pos + 2] != 8:
                        raise ValueError("invalid JPEG frame")
                    height, width = struct.unpack(">HH", raw[pos + 3:pos + 7])
                    _dimensions(width, height)
                    dims = (width, height)
                if marker == 0xDA:
                    if not dims or pos + n >= len(raw) - 2:
                        raise ValueError("missing JPEG pixels")
                    return "jpg", *dims
                pos += n
        if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP" and int.from_bytes(raw[4:8], "little") + 8 == len(raw):
            pos, dims, canvas = 12, None, None
            while pos < len(raw):
                tag = raw[pos:pos + 4]
                n = int.from_bytes(raw[pos + 4:pos + 8], "little")
                end = pos + 8 + n
                if pos + 8 > len(raw) or end + (n % 2) > len(raw):
                    raise ValueError("invalid WebP chunk")
                part = raw[pos + 8:end]
                if tag == b"VP8X":
                    if n != 10 or part[0] & 2:
                        raise ValueError("animated or invalid WebP")
                    canvas = (1 + int.from_bytes(part[4:7], "little"), 1 + int.from_bytes(part[7:10], "little"))
                    _dimensions(*canvas)
                elif tag == b"VP8 " and n > 10 and part[3:6] == b"\x9d\x01\x2a" and not part[0] & 1:
                    dims = (int.from_bytes(part[6:8], "little") & 0x3FFF, int.from_bytes(part[8:10], "little") & 0x3FFF)
                elif tag == b"VP8L" and n > 5 and part[0] == 0x2F:
                    bits = int.from_bytes(part[1:5], "little")
                    if bits >> 29:
                        raise ValueError("invalid WebP version")
                    dims = ((bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1)
                elif tag in (b"ANIM", b"ANMF"):
                    raise ValueError("animated WebP")
                pos = end + n % 2
            if dims and (not canvas or dims == canvas):
                _dimensions(*dims)
                return "webp", *dims
    except (IndexError, struct.error, zlib.error) as exc:
        raise ValueError("invalid image") from exc
    raise ValueError("use a PNG, JPEG or WebP image")


def _dimensions(width, height):
    if not (1 <= width <= MAX_SIDE and 1 <= height <= MAX_SIDE):
        raise ValueError("image dimensions must be between 1 and 1024 pixels")


def save_upload(store, encoded):
    if not isinstance(encoded, str) or len(encoded) > ((MAX_BYTES + 2) // 3) * 4:
        raise ValueError("image must be at most 1 MB")
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError("invalid image encoding") from exc
    ext, _, _ = image_info(raw)
    name = "upload-" + hashlib.sha256(raw).hexdigest() + "." + ext
    with store.lock:
        folder = store.path("avatars")
        folder.mkdir(exist_ok=True)
        target = folder / name
        if not target.exists():
            temporary = folder / (name + ".tmp")
            temporary.write_bytes(raw)
            temporary.replace(target)
    return name


def static_files():
    files = {"/avatars/avatars.json": ("avatars/avatars.json", "application/json; charset=utf-8")}
    for face in catalog()["avatars"]:
        for path in face["files"].values():
            files["/avatars/" + path] = ("avatars/" + path, "image/png")
    return files
