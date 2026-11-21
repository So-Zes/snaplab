"""RustXNG: a self-hosted metasearch engine (a Rust rewrite of SearXNG, github.com/RustXNG/RustXNG, AGPL-3.0).

SnapLab uses it as the last web search source. Its "standard" profile asks only providers that allow automated use:
Mwmbl, Wikipedia, Wikidata and wiby for the web, and GitHub, Stack Overflow, arXiv and others for their topics. It
needs no key and has no limit of its own; its coverage of current and Japanese pages is small.

Settings > Web search installs it on request: the official release for this platform is downloaded from GitHub and
checked against the SHA-256 written below before it is kept in ~/.snaplab/rustxng/<version>/. SnapLab then starts
it on 127.0.0.1 (port 29483) when SnapLab starts, or uses one that already answers there (one left running from an
earlier start). Nothing is downloaded unless the user presses the button. search.rustxng.enabled = false turns it off.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

VERSION = "v0.32.0"
REPO = "RustXNG/RustXNG"
PORT = 29483
# From the release's release-manifest.json (checked 2026-11-21). A download that does not match is deleted.
ASSETS = {
    ("win32", "amd64"): ("rustxng-v0.32.0-x86_64-pc-windows-msvc.exe",
                         "58ee07c420f5d3787024210c1e2fedc8685b6593ae9d8cfadf56db632950eca1"),
    ("linux", "x86_64"): ("rustxng-v0.32.0-x86_64-unknown-linux-gnu",
                          "b19d2fbea16a1b182823edad000518391d435a1a9cca8d182ec61a6df38a484e"),
    ("linux", "aarch64"): ("rustxng-v0.32.0-aarch64-unknown-linux-gnu",
                           "e86adb048c99c8873513227eb8481b932e813082c579063a12b1f34cc82b5a26"),
    ("darwin", "arm64"): ("rustxng-v0.32.0-aarch64-apple-darwin",
                          "266dbf2ee7275ba5f6d847d636720d8d0d9e3399489c2388ef96ff28107feeb5"),
    ("darwin", "x86_64"): ("rustxng-v0.32.0-x86_64-apple-darwin",
                           "c354bed4b0ebcefca907e1119c5f30c63ce7d2a7b97f672cf19f2ad5a9003558"),
}
NO_WINDOW = 0x08000000


def _machine() -> str:
    try:
        return platform.machine().lower()
    except Exception:  # noqa: BLE001 - platform reads the registry on Windows; never let that break Settings
        return (os.environ.get("PROCESSOR_ARCHITECTURE") or "").lower()


def asset() -> tuple[str, str] | None:
    return ASSETS.get((sys.platform, _machine()))


def home() -> Path:
    return Path(os.environ.get("SNAPLAB_RUSTXNG_HOME") or Path.home() / ".snaplab" / "rustxng")


def exe_path() -> Path | None:
    a = asset()
    return home() / VERSION / ("rustxng.exe" if sys.platform == "win32" else "rustxng") if a else None


def installed() -> bool:
    p = exe_path()
    return bool(p and p.is_file())


def healthy(url: str, timeout: float = 2.0) -> bool:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url.rstrip("/") + "/healthz", timeout=timeout) as r:
            return r.status == 200
    except (OSError, urllib.error.URLError, ValueError):
        return False


class RustXNG:
    """Install, start and report RustXNG for one SnapLab."""

    def __init__(self, cfg: dict):
        self.cfg = cfg if isinstance(cfg, dict) else {}
        self.url = str(self.cfg.get("url") or f"http://127.0.0.1:{PORT}").rstrip("/")
        self.enabled = self.cfg.get("enabled", True) is not False
        self.state = "absent"          # absent | installing | starting | ready | error | off
        self.error = ""
        self.ready = False
        self.lock = threading.Lock()

    def view(self) -> dict:
        return {"state": "off" if not self.enabled else self.state, "error": self.error, "version": VERSION,
                "supported": asset() is not None, "installed": installed(), "url": self.url}

    def usable(self) -> bool:
        return self.enabled and self.ready

    # ------------------------------------------------------------------ start
    def start(self, wait_s: float = 20.0) -> bool:
        """Use a RustXNG that already answers on the URL, otherwise start the installed one. Never downloads."""
        if not self.enabled:
            self.state = "off"
            return False
        if healthy(self.url):
            self.ready, self.state, self.error = True, "ready", ""
            return True
        exe = exe_path()
        if not exe or not exe.is_file():
            self.ready, self.state = False, "absent"
            return False
        self.state = "starting"
        port = self.url.rsplit(":", 1)[-1].split("/")[0]
        log = open(home() / "rustxng.log", "ab")
        kw = {"creationflags": NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
        try:
            subprocess.Popen([str(exe), "--profile", "standard", "--bind", "127.0.0.1", "--port", port],
                             cwd=str(exe.parent), stdin=subprocess.DEVNULL, stdout=log, stderr=log, close_fds=True, **kw)
        except OSError as e:
            self.ready, self.state, self.error = False, "error", f"could not start: {e}"
            return False
        finally:
            log.close()
        end = time.time() + wait_s
        while time.time() < end:
            if healthy(self.url):
                self.ready, self.state, self.error = True, "ready", ""
                return True
            time.sleep(0.4)
        self.ready, self.state, self.error = False, "error", "started but did not answer; see ~/.snaplab/rustxng/rustxng.log"
        return False

    def start_in_background(self) -> None:
        threading.Thread(target=self.start, daemon=True, name="rustxng-start").start()

    # ------------------------------------------------------------------ install
    def install(self) -> bool:
        """Download the pinned release for this platform, check its SHA-256, keep it, then start it."""
        a = asset()
        if not a:
            self.state, self.error = "error", f"no RustXNG release for {sys.platform} {_machine()}"
            return False
        if not self.lock.acquire(blocking=False):
            return False
        try:
            self.enabled = True
            self.state, self.error = "installing", ""
            name, digest = a
            target = exe_path()
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.is_file():
                url = f"https://github.com/{REPO}/releases/download/{VERSION}/{name}"
                part = target.with_suffix(target.suffix + ".part")
                h = hashlib.sha256()
                try:
                    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "SnapLab"}), timeout=60) as r, \
                            open(part, "wb") as f:
                        while chunk := r.read(1 << 20):
                            h.update(chunk)
                            f.write(chunk)
                except (OSError, urllib.error.URLError) as e:
                    part.unlink(missing_ok=True)
                    self.state, self.error = "error", f"download failed: {getattr(e, 'reason', e)}"
                    return False
                if h.hexdigest() != digest:
                    part.unlink(missing_ok=True)
                    self.state, self.error = "error", "the download did not match the expected SHA-256; it was deleted"
                    return False
                if os.name != "nt":
                    part.chmod(0o755)
                os.replace(part, target)
                (target.parent / "SOURCE.json").write_text(json.dumps(
                    {"repo": REPO, "version": VERSION, "asset": name, "sha256": digest, "license": "AGPL-3.0-or-later",
                     "installed": time.strftime("%Y-%m-%dT%H:%M:%S")}, indent=1), encoding="utf-8")
            return self.start()
        finally:
            self.lock.release()

    def install_in_background(self) -> None:
        self.state, self.error = "installing", ""
        threading.Thread(target=self.install, daemon=True, name="rustxng-install").start()
