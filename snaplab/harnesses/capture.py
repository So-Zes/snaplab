"""Standalone HTML screenshot helper, copied into a session and run by its sandboxed shell."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys


def browser_candidates():
    if os.name != "nt":
        for name in ("chromium", "chromium-browser", "google-chrome"):
            found = shutil.which(name)
            if found:
                yield Path(found)
    for var in ("PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA"):
        base = os.environ.get(var)
        if base:
            for name in ("Microsoft/Edge/Application/msedge.exe", "Google/Chrome/Application/chrome.exe"):
                path = Path(base) / name
                if path.is_file():
                    yield path


def prepare_capture(folder):
    hidden = folder / ".snaplab"
    if hidden.is_symlink() or hidden.resolve() != folder.resolve() / ".snaplab":
        raise ValueError("invalid session metadata folder")
    hidden.mkdir(exist_ok=True)
    for name in ("capture.py", "capture.mjs", "browser.mjs", "keep.py"):
        target = hidden / name
        if target.exists():
            if target.is_symlink() or target.stat().st_nlink != 1 or target.resolve() != hidden.resolve() / name:
                raise ValueError("invalid screenshot helper path")
        target.write_bytes(Path(__file__).with_name(name).read_bytes())


def local_path(root, value):
    parts = value.replace("\\", "/").split("/")
    if any(p in ("", ".", "..") or p.startswith(".") or p.endswith((" ", ".")) for p in parts):
        raise ValueError("use a relative file path inside this workspace")
    if re.search(r'[:\x00-\x1f<>"|?*]', value):
        raise ValueError("invalid path")
    path = root.joinpath(*parts)
    if not path.resolve().is_relative_to(root):
        raise ValueError("file is outside this workspace")
    for part in (path, *path.parents):
        if part == root:
            break
        if part.exists() and (part.is_symlink() or getattr(part.stat(), "st_file_attributes", 0) & 0x400 or
                              (part.is_file() and part.stat().st_nlink != 1)):
            raise ValueError("linked files cannot be captured")
    return path


def split_page(value):
    """'index.html?cam=1#top' -> ('index.html', '?cam=1#top'). Only the file part is a path."""
    cut = min([i for i in (value.find("?"), value.find("#")) if i >= 0] or [len(value)])
    suffix = value[cut:]
    if re.search(r"[\x00-\x1f\s<>\"\\]", suffix) or len(suffix) > 500:
        raise ValueError("the ?query or #fragment after the file name has characters that are not allowed")
    return value[:cut], suffix


def main():
    parser = argparse.ArgumentParser(description="Capture local HTML inside the SnapCode sandbox. "
                                     "Pages load over file:// with access to their own files (ES modules, textures).")
    parser.add_argument("html", help="file inside this workspace, optionally with ?query or #fragment, e.g. index.html?cam=1")
    parser.add_argument("--output", default="screenshot.png")
    parser.add_argument("--width", type=int, default=390)
    parser.add_argument("--height", type=int, default=844)
    parser.add_argument("--timeout", type=float, default=300, help="seconds for the whole capture (10-1800, default 300)")
    parser.add_argument("--wait", type=float, default=1, help="seconds to wait after loading before the picture (0-600)")
    parser.add_argument("--ready", default="", help="JavaScript expression to wait for, e.g. window.__ready === true")
    args = parser.parse_args()
    root = Path.cwd().resolve()
    page, query = split_page(args.html)
    source, target = local_path(root, page), local_path(root, args.output)
    if source.suffix.lower() not in (".html", ".htm") or not source.is_file():
        raise ValueError("input must be an HTML file")
    if target.suffix.lower() != ".png":
        raise ValueError("choose a .png output filename")
    if not 320 <= args.width <= 3840 or not 320 <= args.height <= 3000:
        raise ValueError("width must be 320-3840 and height 320-3000")
    if not 10 <= args.timeout <= 1800 or not 0 <= args.wait <= 600:
        raise ValueError("--timeout must be 10-1800 seconds and --wait 0-600 seconds")
    if len(args.ready) > 300 or re.search(r"[\x00-\x1f]", args.ready):
        raise ValueError("--ready must be one short line of JavaScript")
    # Chrome keeps the CDP pipe connected on Windows; Edge may hand off to another process.
    browser = next(iter(sorted(browser_candidates(), key=lambda p: p.name != "chrome.exe")), None)
    if not browser:
        raise ValueError("Chromium, Edge or Chrome is unavailable; deliver the HTML and report that capture failed")
    hidden = root / ".snaplab"
    if hidden.resolve() != hidden or hidden.is_symlink():
        raise ValueError("invalid metadata folder")
    profile = hidden / ("capture-profile-" + secrets.token_hex(8))
    profile.mkdir(parents=True)
    target.parent.mkdir(parents=True, exist_ok=True)
    env = {k: v for k, v in os.environ.items() if not re.search("KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL", k, re.I)}
    env.update(TEMP=str(profile), TMP=str(profile))
    node = shutil.which("node")
    if not node:
        raise ValueError("Node.js is unavailable; deliver the HTML and report that capture failed")
    temporary = target.with_name(".capture-" + secrets.token_hex(8) + ".png")
    cmd = [node, "--preserve-symlinks", "--preserve-symlinks-main", str(Path(__file__).with_suffix(".mjs")),
           str(browser), str(profile), source.as_uri() + query, str(temporary), str(args.width), str(args.height),
           str(args.timeout), str(args.wait), args.ready]
    proc = subprocess.Popen(cmd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.PIPE, creationflags=0x08000000 if os.name == "nt" else 0)
    try:
        _, err = proc.communicate(timeout=args.timeout + args.wait + 30)
        notes = (err or b"").decode("utf-8", "replace").strip()[-3000:]
    except subprocess.TimeoutExpired:
        if os.name == "nt":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=0x08000000)
        else:
            proc.kill()
        proc.wait()
        raise ValueError(f"capture took longer than {args.timeout:.0f} s; try a larger --timeout") from None
    try:
        if proc.returncode != 0 or not temporary.is_file():
            raise ValueError("the browser did not create an image" + (":\n" + notes if notes else ""))
        raw = temporary.read_bytes()
        if (not raw.startswith(b"\x89PNG\r\n\x1a\n") or int.from_bytes(raw[16:20], "big") != args.width or
                int.from_bytes(raw[20:24], "big") != args.height):
            raise ValueError("invalid screenshot dimensions")
        local_path(root, args.output)  # recheck the destination before replacing an older screenshot
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
        # Only our random profile, inside this workspace, may be removed.
        if profile.resolve().parent == hidden.resolve() and profile.name.startswith("capture-profile-"):
            shutil.rmtree(profile, ignore_errors=True)
    # Only report a relative name. The calling task chooses which files to deliver.
    out = {"screenshot": target.relative_to(root).as_posix(), "width": args.width, "height": args.height}
    if notes:
        out["page_problems"] = notes.splitlines()[:12]     # errors the page logged while it was captured
    print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ValueError, OSError) as e:
        # Say what went wrong; a bare "failed" sent workers on long hunts for the cause.
        print("Screenshot failed: " + str(e), file=sys.stderr)
        sys.exit(1)
