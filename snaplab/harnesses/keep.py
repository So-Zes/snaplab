"""Keep processes running for the whole SnapCode turn: dev servers, watchers, a live browser, game servers.

Coding agents end what a shell command leaves running when the command returns (Codex does this after every
tool call). SnapLab therefore starts the agent under this supervisor, inside the job sandbox; processes started
with `keep.py start` are the supervisor's children, so they stay up across tool calls and are stopped when the
turn ends (the sandbox ends with it).

    python3 .snaplab/keep.py start web -- python3 -m http.server 8000
    python3 .snaplab/keep.py list
    python3 .snaplab/keep.py log web [--lines 40]
    python3 .snaplab/keep.py stop web

Without the supervisor (an older worker or a local run) `start` launches the process in its own session instead.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import secrets
import signal
import subprocess
import sys
import tempfile
import threading
import time

STATE = Path(os.environ.get("SNAPLAB_KEEP_DIR") or (Path(tempfile.gettempdir()) / "snaplab-keep"))
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,40}")


def _alive(pid: int) -> bool:
    if os.name == "nt":            # os.kill(pid, 0) would terminate the process on Windows
        import ctypes
        kernel = ctypes.windll.kernel32
        handle = kernel.OpenProcess(0x1000, False, int(pid))     # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259   # STILL_ACTIVE
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ValueError):
        return False


def _records() -> dict:
    out = {}
    for f in STATE.glob("proc-*.json"):
        try:
            rec = json.loads(f.read_text(encoding="utf-8"))
            out[rec["name"]] = rec
        except (OSError, ValueError, KeyError):
            pass
    return out


SECRET_NAME = re.compile(r"KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL", re.I)


def _environment() -> dict:
    """The caller's environment (DISPLAY, PATH, PORT ...) without secret-looking names."""
    return {k: v for k, v in os.environ.items() if not SECRET_NAME.search(k)}


def _launch(name: str, cmd: list[str], cwd: str, env: dict | None = None) -> dict:
    log = STATE / f"{name}.log"
    out = open(log, "ab", buffering=0)
    proc = subprocess.Popen(cmd, cwd=cwd, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                            start_new_session=True, env=env)
    out.close()
    rec = {"name": name, "pid": proc.pid, "cmd": cmd, "cwd": cwd, "log": str(log), "started": time.time()}
    (STATE / f"proc-{name}.json").write_text(json.dumps(rec), encoding="utf-8")
    return rec


def supervise(cmd: list[str]) -> int:
    """Run the agent as a child; serve start requests until it exits; then stop everything that was kept."""
    STATE.mkdir(parents=True, exist_ok=True)
    (STATE / "supervisor").write_text(str(os.getpid()), encoding="utf-8")
    ended = threading.Event()
    kept: list[subprocess.Popen] = []

    def requests():
        while not ended.is_set():
            for req in sorted(STATE.glob("start-*.json")):
                try:
                    data = json.loads(req.read_text(encoding="utf-8"))
                    if NAME.fullmatch(data.get("name", "")) and isinstance(data.get("cmd"), list) and data["cmd"] \
                            and all(isinstance(v, str) for v in data["cmd"]):
                        env = data.get("env")
                        # The requester's display and paths, so a kept browser opens on the job's desktop.
                        env = ({str(k): str(v) for k, v in env.items() if not SECRET_NAME.search(str(k))}
                               if isinstance(env, dict) else None)
                        rec = _launch(data["name"], data["cmd"], data.get("cwd") or os.getcwd(), env)
                        kept.append(rec["pid"])
                        req.with_suffix(".done").write_text(json.dumps(rec), encoding="utf-8")
                    else:
                        req.with_suffix(".done").write_text(json.dumps({"error": "invalid request"}), encoding="utf-8")
                except Exception as e:  # noqa: BLE001 - one bad request must not stop the supervisor
                    try:
                        req.with_suffix(".done").write_text(json.dumps({"error": str(e)[:300]}), encoding="utf-8")
                    except OSError:
                        pass
                finally:
                    req.unlink(missing_ok=True)
            ended.wait(0.1)

    thread = threading.Thread(target=requests, daemon=True)
    thread.start()
    try:
        child = subprocess.Popen(cmd, stdin=sys.stdin, stdout=sys.stdout, stderr=sys.stderr)
        return child.wait()
    finally:
        ended.set()
        thread.join(timeout=2)
        for pid in kept:
            try:
                os.killpg(pid, signal.SIGTERM)
            except (OSError, AttributeError):
                pass
        # A browser writes its cookies and open tabs when it gets SIGTERM; give it a few seconds before SIGKILL.
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and any(_alive(pid) for pid in kept):
            time.sleep(0.1)
        for pid in kept:
            try:
                os.killpg(pid, signal.SIGKILL)
            except (OSError, AttributeError):
                pass


def start(name: str, cmd: list[str]) -> dict:
    if not NAME.fullmatch(name):
        raise ValueError("name: letters, digits, _ . - (up to 41 characters)")
    if not cmd:
        raise ValueError("give the command after --")
    STATE.mkdir(parents=True, exist_ok=True)
    old = _records().get(name)
    if old and _alive(old["pid"]):
        raise ValueError(f"{name} is already running (pid {old['pid']}); stop it first")
    sup = STATE / "supervisor"
    if sup.exists() and _alive(int(sup.read_text() or 0)):
        req = STATE / f"start-{secrets.token_hex(8)}.json"
        req.write_text(json.dumps({"name": name, "cmd": cmd, "cwd": os.getcwd(), "env": _environment()}),
                       encoding="utf-8")
        done = req.with_suffix(".done")
        deadline = time.monotonic() + 15
        while not done.exists():
            if time.monotonic() > deadline:
                raise RuntimeError("the keep supervisor did not answer")
            time.sleep(0.05)
        time.sleep(0.02)
        rec = json.loads(done.read_text(encoding="utf-8"))
        done.unlink(missing_ok=True)
        if "error" in rec:
            raise RuntimeError(rec["error"])
        rec["kept_until"] = "the end of this turn"
    else:
        rec = _launch(name, cmd, os.getcwd())
        rec["kept_until"] = "it is stopped (no supervisor here)"
    time.sleep(0.3)
    if not _alive(rec["pid"]):
        rec["warning"] = "the process already exited; see its log"
    return rec


def stop(name: str) -> dict:
    rec = _records().get(name)
    if not rec:
        raise ValueError(f"nothing named {name} is kept")
    if os.name == "nt":            # the whole tree, like a process group on Linux
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(rec["pid"])], capture_output=True)
    else:
        for sig, grace in ((signal.SIGTERM, 5.0), (signal.SIGKILL, 0.5)):
            try:
                os.killpg(rec["pid"], sig)
            except OSError:
                break
            deadline = time.monotonic() + grace       # a browser saves cookies and tabs on SIGTERM
            while time.monotonic() < deadline and _alive(rec["pid"]):
                time.sleep(0.1)
            if not _alive(rec["pid"]):
                break
    (STATE / f"proc-{name}.json").unlink(missing_ok=True)
    return {"stopped": name}


def tail(path: str, lines: int) -> str:
    try:
        data = Path(path).read_bytes()[-200000:].decode("utf-8", "replace")
    except OSError:
        return ""
    return "\n".join(data.splitlines()[-lines:])


def main(argv: list[str]) -> int:
    if argv and argv[0] == "--supervise":
        return supervise(argv[1:])
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    action, rest = argv[0], argv[1:]
    if action == "start":
        if "--" not in rest or rest.index("--") != 1:
            raise ValueError("usage: keep.py start NAME -- COMMAND ...")
        print(json.dumps(start(rest[0], rest[2:])))
    elif action == "stop":
        print(json.dumps(stop(rest[0])))
    elif action == "list":
        print(json.dumps([{"name": r["name"], "pid": r["pid"], "running": _alive(r["pid"]), "cmd": " ".join(r["cmd"])[:200]}
                          for r in _records().values()], indent=1))
    elif action == "log":
        rec = _records().get(rest[0]) if rest else None
        if not rec:
            raise ValueError("usage: keep.py log NAME [--lines N]")
        n = int(rest[rest.index("--lines") + 1]) if "--lines" in rest else 40
        print(tail(rec["log"], max(1, min(n, 2000))))
    else:
        raise ValueError("unknown action " + action + "; use start, stop, list or log")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except (ValueError, RuntimeError, OSError, IndexError) as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)
