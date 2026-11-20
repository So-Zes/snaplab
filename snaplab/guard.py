"""Exit guard: the API key value must never leave SnapLab, whatever a model or a sandboxed command does.

Why it exists: the Codex sandbox blocks reading files outside the session folder, but on Windows it does not
block reading the registry, and `setx` stores the key in HKCU\\Environment. A command could therefore read the
key and print it or write it into a file. The model never sees it in clear (the translator masks it in every
request), and the sandbox has no network, so the only ways out are SnapLab's own outputs. This guard compares
everything SnapLab stores or sends against the secret values and replaces them with a marker:

- every JSON file and log line SnapLab writes (messages, sessions, events, results, cards, dashboard, views)
- every response and live event sent to the phone (server.py) and every push notification
- files a session changed, before SnapLab commits them (the commit never contains the key)
- the session's own Codex home (its transcript files) after each turn
- text a "bot only" session writes to the project

Values are refreshed from the process environment, Windows Credential Manager and the legacy registry
environment at run time; they are never printed or stored by this module.
"""
from __future__ import annotations

import os
import threading
from pathlib import Path
from urllib.parse import unquote

MARK = "[secret removed by SnapLab]"
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv"}


def variants(value: str) -> set[str]:
    """The secret and a few encodings a command could use to slip it out (QA round 2, M-10): base64 (also URL-safe,
    without padding), hex, reversed. Other changes (spaces between letters, splitting it) are not caught; see
    docs/security.md."""
    import base64
    import binascii
    raw = value.encode("utf-8")
    out = {value, value[::-1], binascii.hexlify(raw).decode(), binascii.hexlify(raw).decode().upper()}
    for enc in (base64.b64encode(raw), base64.urlsafe_b64encode(raw)):
        s = enc.decode()
        out.update({s, s.rstrip("=")})
    return {x for x in out if len(x) >= 8}


class Guard:
    def __init__(self, key_fn=None, extra_env: list[str] | None = None, values: list[str] | None = None):
        self.key_fn = key_fn
        self.extra_env = list(extra_env or [])
        self.fixed = [v for v in (values or []) if v]
        self._cache: list[str] = []
        self._values: set[str] = set()
        self._lock = threading.Lock()
        self.hits = 0                       # how many times something was removed (a number only)

    def secrets(self) -> list[str]:
        from .llm import api_key_values
        with self._lock:
            vals = set(self.fixed)
            if self.key_fn:
                k = self.key_fn()
                if k:
                    vals.add(k)
            # Refresh all three sources at every boundary, even immediately after a rotation.
            for name in self.extra_env:
                vals.update(api_key_values(name))
            fresh = {v for v in vals if len(v) >= 8} - self._values
            if fresh:
                forms = set(self._cache)
                for v in fresh:
                    forms.update(variants(v))
                self._cache = sorted(forms, key=len, reverse=True)
                self._values.update(fresh)  # Previously observed keys stay protected, too.
            return self._cache

    def add(self, value: str) -> None:
        if value and len(value) >= 8:
            with self._lock:
                if value not in self.fixed:
                    self.fixed.append(value)

    def reject_web_args(self, args) -> None:
        def strings(obj):
            if isinstance(obj, str):
                yield obj
            elif isinstance(obj, dict):
                for key, value in obj.items():
                    yield from strings(key)
                    yield from strings(value)
            elif isinstance(obj, list):
                for value in obj:
                    yield from strings(value)
        for value in strings(args):
            for _ in range(5):
                if self.contains(value) or MARK in value:
                    raise ValueError("web arguments contain a secret; nothing was sent")
                decoded = unquote(value)
                if decoded == value:
                    break
                value = decoded
            else:
                raise ValueError("web arguments use excessive URL encoding")

    def contains(self, text) -> bool:
        if not text or not isinstance(text, str):
            return False
        return any(s in text for s in self.secrets())

    def clean(self, text):
        if not text or not isinstance(text, str):
            return text
        hit = False
        for s in self.secrets():
            if s in text:
                text = text.replace(s, MARK)
                hit = True
        if hit:
            self.hits += 1
        return text

    def clean_obj(self, obj):
        if isinstance(obj, str):
            return self.clean(obj)
        if isinstance(obj, list):
            return [self.clean_obj(x) for x in obj]
        if isinstance(obj, dict):
            return {self.clean(k): self.clean_obj(v) for k, v in obj.items()}
        return obj

    def byte_forms(self) -> list[tuple[bytes, bytes]]:
        """(secret, marker) in the encodings files are checked for. Read once for a batch of files: each read of the
        secrets asks Windows Credential Manager and the registry (about 1 ms, 25 s for 27,600 files)."""
        return [(s.encode(enc), MARK.encode(enc)) for s in self.secrets() for enc in ("utf-8", "utf-16-le")]

    def clean_file(self, path: Path, max_bytes: int | None = None, forms=None) -> bool:
        """Replace secret values inside one file. Returns True if the file contained one. Files of any size are
        checked (there was a 20 MB limit, so a larger file was never checked)."""
        path = Path(path)
        try:
            if not path.is_file() or (max_bytes is not None and path.stat().st_size > max_bytes):
                return False
            data = path.read_bytes()
        except OSError:
            return False
        changed = False
        for b, mark in (self.byte_forms() if forms is None else forms):
            if b in data:
                data = data.replace(b, mark)
                changed = True
        if changed:
            try:
                path.write_bytes(data)
            except OSError:
                return False
            self.hits += 1
        return changed

    def clean_tree(self, root: Path, limit: int | None = None) -> list[str]:
        """Clean every file under a folder (skipping .git). Returns the relative paths that contained a secret.
        Every file is checked; the old 20,000-file limit left the rest of a big project unchecked."""
        root = Path(root)
        found = []
        forms = self.byte_forms()
        if not root.exists() or not forms:
            return found
        n = 0
        for dirpath, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            for f in files:
                n += 1
                if limit is not None and n > limit:
                    return found
                p = Path(dirpath) / f
                if self.clean_file(p, forms=forms):
                    found.append(p.relative_to(root).as_posix())
        return found

    def clean_paths(self, root: Path, names) -> list[str]:
        """Clean the named files under a folder (the ones a turn changed). Returns the names that contained a secret."""
        root = Path(root)
        forms = self.byte_forms()
        if not forms:
            return []
        return [name for name in names if self.clean_file(root.joinpath(*name.split("/")), forms=forms)]
