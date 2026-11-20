"""Content fingerprints bind test observations to the files they actually exercised."""
from collections import OrderedDict
import hashlib
import json
import os
from pathlib import Path
import threading
import time

SKIP = {".git", ".worktrees", ".snaplab", "__pycache__", ".pytest_cache", ".mypy_cache", "node_modules", ".venv", "venv"}


# Per folder: relative path -> ((size, mtime), file digest, when it was read). A turn computes the fingerprint three
# or four times; reading every file each time took about 16 s apiece for a 383 MB project (2026-11-17). Only files
# whose size or modification time changed are read again. Clocks advance in steps (about 15 ms on Windows), so a
# file modified less than RACY_NS before it was read could change again with the same time: it is always reread
# (the way git treats "racily clean" files).
RACY_NS = 2_000_000_000
_DIGESTS: "OrderedDict[str, dict]" = OrderedDict()
_DIGESTS_LOCK = threading.Lock()


def remember_digest(folder, name: str, size: int, mtime_ns: int, digest: bytes, read_at: int) -> None:
    """Record a digest found without reading the file (git's record of an unchanged tracked file)."""
    key = os.path.abspath(folder)
    with _DIGESTS_LOCK:
        _DIGESTS.setdefault(key, {})[name] = ((size, mtime_ns), digest, read_at)
        _DIGESTS.move_to_end(key)
        while len(_DIGESTS) > 16:
            _DIGESTS.popitem(last=False)


def shared_digest(folder, name: str, size: int, mtime_ns: int, read) -> bytes:
    """SHA-256 of folder/name from the same cache artifact_version uses, or read() and remember it. The file list
    sent to the worker uses it, so a new session reads each file of a big project once instead of twice."""
    key = os.path.abspath(folder)
    with _DIGESTS_LOCK:
        hit = (_DIGESTS.get(key) or {}).get(name)
    if hit and hit[0] == (size, mtime_ns) and mtime_ns < hit[2] - RACY_NS:
        return hit[1]
    read_at = time.time_ns()
    digest = read()
    with _DIGESTS_LOCK:
        _DIGESTS.setdefault(key, {})[name] = ((size, mtime_ns), digest, read_at)
        _DIGESTS.move_to_end(key)
        while len(_DIGESTS) > 16:
            _DIGESTS.popitem(last=False)
    return digest


def artifact_version(folder: Path, *, code_only=False) -> str:
    folder = Path(folder)
    key = os.path.abspath(folder)
    with _DIGESTS_LOCK:
        known = _DIGESTS.pop(key, {})
    h = hashlib.sha256()
    now = time.time_ns()

    def visit(directory, rel):
        try:
            entries = list(os.scandir(directory))
        except OSError:
            return
        files = sorted((e for e in entries if not e.is_dir(follow_symlinks=False)), key=lambda e: e.name)
        dirs = sorted((e for e in entries if e.is_dir(follow_symlinks=False) and e.name not in SKIP),
                      key=lambda e: e.name)
        for entry in files:
            if entry.is_symlink() or entry.name == "STATE.md":
                continue
            if code_only and os.path.splitext(entry.name)[1].lower() in (".png", ".jpg", ".jpeg", ".gif", ".webp"):
                continue
            name = rel + entry.name
            info = entry.stat(follow_symlinks=False)
            stamp = (info.st_size, info.st_mtime_ns)
            hit = known.get(name)
            if hit and hit[0] == stamp and stamp[1] < hit[2] - RACY_NS:
                digest = hit[1]
            else:
                fh = hashlib.sha256()
                with open(entry.path, "rb") as stream:
                    for block in iter(lambda: stream.read(1 << 20), b""):
                        fh.update(block)
                digest = fh.digest()
                known[name] = (stamp, digest, now)
            h.update(name.encode("utf-8") + b"\0")
            h.update(digest)
        for entry in dirs:
            visit(entry.path, rel + entry.name + "/")

    visit(folder, "")
    with _DIGESTS_LOCK:
        _DIGESTS[key] = known
        while len(_DIGESTS) > 16:
            _DIGESTS.popitem(last=False)
    return "sha256:" + h.hexdigest()


def record_tests(app, session, tests, output):
    rec = app.archive.add("work", json.dumps(tests, ensure_ascii=False) + "\n" + output,
                          project=session["project"], session=session["id"], title="Test observation",
                          ref=f"#/session/{session['id']}",
                          meta={"source_type": "runtime", "untrusted": True,
                                "tested_version": tests.get("tested_version"), "exit_code": tests.get("exit_code")})
    if rec:
        tests["evidence_record"] = rec["id"]
