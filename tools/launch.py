"""Start an isolated local installation and open its paired browser. No API calls here."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import urllib.request
import webbrowser

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def probe(port):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=1) as r:
            return json.loads(r.read(4096))
    except Exception:
        return None


def choose_port(preferred):
    for port in range(preferred, min(preferred + 100, 65536)):
        with socket.socket() as sock:
            try:
                sock.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise ValueError("No local port is free. Try again with --port and a different port number.")


def launch(data, port=18445, open_browser=True):
    from snaplab.auth import AuthStore
    data = Path(data).expanduser().resolve()
    data.mkdir(parents=True, exist_ok=True)
    settings = data / "local-config.json"
    if settings.exists():
        cfg = json.loads(settings.read_text(encoding="utf-8"))
        if not cfg.get("local_mode") or Path(cfg.get("data_dir", "")).resolve() != data:
            raise ValueError("This folder belongs to another configuration. Choose an empty folder with --data.")
        port = int(cfg["listen_port"])
    else:
        # Never reuse a legacy data directory or change its configuration.
        if any(data.iterdir()):
            raise ValueError("This folder already contains files. Choose an empty folder with --data.")
        port = choose_port(port)
        cfg = {"local_mode": True, "data_dir": str(data), "listen_port": port,
               "listen_host": "127.0.0.1", "manage_tailscale_serve": False,
               "require_tailscale_user": False, "slots": {"count": 2},
               "codex": {"backend": "wsl"}}
        with settings.open("x", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    expected = hashlib.sha256(str(data).encode()).hexdigest()
    state = probe(port)
    if state and state.get("instance") != expected:
        raise ValueError("Another application is using this installation's port. Close that application or choose a new --data folder and --port.")
    if not state:
        env = dict(os.environ, SNAPLAB_CONFIG=str(settings), SNAPLAB_DATA=str(data), SNAPLAB_PORT=str(port))
        flags = 0x08000000 | 0x00000200 | 0x01000000 if os.name == "nt" else 0
        process = subprocess.Popen([sys.executable, str(ROOT / "snaplab.py"), "serve"], cwd=ROOT, env=env,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=flags, start_new_session=os.name != "nt")
        for _ in range(100):
            state = probe(port)
            if state and state.get("instance") == expected:
                break
            if process.poll() is not None:
                raise ValueError("SnapLab could not start. Check the logs folder inside the data folder and run the installer again.")
            time.sleep(.1)
        else:
            raise ValueError("SnapLab did not become ready. Check its logs folder before running the launcher again.")
    token, _ = AuthStore(data / "auth.json").new_pairing("This PC", .25)
    url = f"http://127.0.0.1:{port}/local#{token}"
    if open_browser:
        if not webbrowser.open(url):
            print("The browser did not open. Open this one-use link yourself:")
            print(url)
    return {"url": url, "port": port, "data": str(data)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", default=os.environ.get("SNAPLAB_DATA") or str(Path.home() / ".snaplab" / "data"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("SNAPLAB_PORT") or 18445))
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("Choose a port from 1024 to 65535.")
    try:
        result = launch(args.data, args.port)
        print(f"SnapLab is ready at http://127.0.0.1:{result['port']}. Run the same command to open it again.")
        return 0
    except (ValueError, OSError):
        # Exception text may come from a damaged settings file; print only our own safe messages.
        print("SnapLab could not open. Check that the data folder is empty on first use, its port is free, and Python dependencies are installed. Your existing files were kept.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
