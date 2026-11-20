"""SnapLab command line.

  python snaplab.py serve                 run the server in the foreground (127.0.0.1 only)
  python snaplab.py up                    make sure `tailscale serve` publishes SnapLab, then serve (used by the logon task)
  python snaplab.py status                server, tailscale serve, logon task, devices, API key presence
  python snaplab.py pair [--label NAME]   print a one-time pairing link (valid 24 hours)
  python snaplab.py devices               paired devices and unused links
  python snaplab.py revoke ID|all         revoke a device (d-...), a link (p-...) or all devices and links
  python snaplab.py start | stop          start the logon task now / stop the running server
  python snaplab.py install-task          register the logon task (no console window; backs up an earlier one)
  python snaplab.py uninstall-task        remove the logon task
  python snaplab.py tailscale [--apply]   check (and with --apply add) the tailscale serve entry for SnapLab
  python snaplab.py reset --yes           move the data folder aside (fresh first-run setup); nothing is deleted
  python snaplab.py check-secrets         make sure no API key value is inside the repository files

Settings: config.json (copy config.example.json). SNAPLAB_DATA / SNAPLAB_PORT override the data folder / port.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    if _s is not None:
        _s.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from snaplab import auth, config  # noqa: E402

NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def run(args, timeout=60, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, timeout=timeout, creationflags=NO_WINDOW, **kw)


def out_text(p: subprocess.CompletedProcess) -> str:
    raw = (p.stdout or b"") + (p.stderr or b"")
    for enc in ("utf-8", "cp932"):
        try:
            return raw.decode(enc).strip()
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace").strip()


def tailscale_exe(c: dict) -> str:
    return c.get("tailscale_exe") or shutil.which("tailscale") or r"C:\Program Files\Tailscale\tailscale.exe"


def public_url(c: dict) -> str | None:
    if c.get("public_url"):
        return c["public_url"].rstrip("/")
    st = auth.tailscale_status(tailscale_exe(c))
    if st.get("dns_name"):
        return f"https://{st['dns_name']}:{c['tailscale_https_port']}"
    return None


def health(c: dict) -> dict | None:
    try:
        with urllib.request.urlopen(f"http://{c['listen_host']}:{c['listen_port']}/healthz", timeout=2) as r:
            return json.loads(r.read())
    except Exception:  # noqa: BLE001
        return None


def data_dir(c: dict) -> Path:
    return Path(c["data_dir"])


def backup_dir(c: dict) -> Path:
    d = data_dir(c) / "backup"
    d.mkdir(parents=True, exist_ok=True)
    return d


def stamp() -> str:
    return dt.datetime.now().strftime("%Y%m%d-%H%M%S")


# ------------------------------------------------------------------ tailscale serve

def serve_target(c: dict) -> str:
    return f"http://{c['listen_host']}:{c['listen_port']}"


def serve_state(c: dict) -> tuple[str, dict | None]:
    """(state, current config). state: ok | missing | other (port used by something else) | error."""
    try:
        p = run([tailscale_exe(c), "serve", "status", "--json"], timeout=20)
        st = json.loads(out_text(p) or "{}")
    except (ValueError, OSError, subprocess.SubprocessError):
        return "error", None
    url = public_url(c)
    if not url:
        return "error", st
    host = urllib.request.urlparse(url).hostname
    port = str(c["tailscale_https_port"])
    web = (st.get("Web") or {}).get(f"{host}:{port}") or {}
    proxy = ((web.get("Handlers") or {}).get("/") or {}).get("Proxy")
    if proxy is None:
        tcp = (st.get("TCP") or {}).get(port)
        return ("other" if tcp else "missing"), st
    return ("ok" if proxy.rstrip("/") == serve_target(c) else "other"), st


def ensure_serve(c: dict, apply: bool) -> str:
    state, st = serve_state(c)
    url = public_url(c) or "(unknown)"
    if state == "ok":
        return f"tailscale serve: {url} -> {serve_target(c)} (tailnet only)"
    if state == "other":
        return f"tailscale serve: port {c['tailscale_https_port']} is used by something else; change tailscale_https_port"
    if state == "error":
        return "tailscale serve: could not read the configuration (is Tailscale running?)"
    if not apply:
        return "tailscale serve: not configured yet (python snaplab.py tailscale --apply)"
    (backup_dir(c) / f"tailscale-serve-{stamp()}.json").write_text(json.dumps(st, ensure_ascii=False, indent=1),
                                                                    encoding="utf-8")
    p = run([tailscale_exe(c), "serve", "--bg", f"--https={c['tailscale_https_port']}", serve_target(c)], timeout=30)
    state, _ = serve_state(c)
    if state == "ok":
        return f"tailscale serve: added {url} -> {serve_target(c)} (tailnet only; previous config saved in data/backup)"
    return f"tailscale serve: could not add the entry ({out_text(p)[-300:]})"


# ------------------------------------------------------------------ logon task

def task_xml(c: dict) -> str:
    user = f"{os.environ.get('USERDOMAIN', '')}\\{os.environ.get('USERNAME', '')}".lstrip("\\")
    py = sys.executable
    if py.lower().endswith("pythonw.exe"):
        py = py[:-len("pythonw.exe")] + "python.exe"

    def x(s) -> str:
        return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    args = f'--headless "{py}" "{ROOT / "snaplab.py"}" up'
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>{x('SnapLab: starts the SnapLab server at logon without a console window. Stop it with: python snaplab.py stop')}</Description>
    <URI>\\{x(c['task_name'])}</URI>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>{x(user)}</UserId>
      <Delay>PT25S</Delay>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{x(user)}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings><StopOnIdleEnd>false</StopOnIdleEnd><RestartOnIdle>false</RestartOnIdle></IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>7</Priority>
    <RestartOnFailure><Interval>PT1M</Interval><Count>5</Count></RestartOnFailure>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>C:\\Windows\\System32\\conhost.exe</Command>
      <Arguments>{x(args)}</Arguments>
      <WorkingDirectory>{x(ROOT)}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def task_state(c: dict) -> str | None:
    if os.name != "nt":
        return None
    p = run(["schtasks", "/Query", "/TN", c["task_name"], "/FO", "CSV", "/NH"], timeout=20)
    if p.returncode != 0:
        return None
    line = out_text(p).splitlines()[-1] if out_text(p) else ""
    parts = [x.strip('"') for x in line.split('","')]
    return parts[-1] if parts else "?"


def cmd_install_task(a, c) -> int:
    if os.name != "nt":
        print("The logon task is for Windows. On other systems use your service manager to run: python snaplab.py up")
        return 1
    if task_state(c) is not None:
        p = run(["schtasks", "/Query", "/TN", c["task_name"], "/XML"], timeout=20)
        (backup_dir(c) / f"task-{c['task_name']}-{stamp()}.xml").write_bytes(p.stdout or b"")
    f = data_dir(c) / "task.xml"
    f.write_text(task_xml(c), encoding="utf-16")
    p = run(["schtasks", "/Create", "/TN", c["task_name"], "/XML", str(f), "/F"], timeout=30)
    print(out_text(p) or "")
    print("Registered." if p.returncode == 0 else "Could not register the task.")
    return p.returncode


def cmd_uninstall_task(a, c) -> int:
    if task_state(c) is None:
        print("The task is not registered.")
        return 0
    p = run(["schtasks", "/Query", "/TN", c["task_name"], "/XML"], timeout=20)
    (backup_dir(c) / f"task-{c['task_name']}-{stamp()}.xml").write_bytes(p.stdout or b"")
    p = run(["schtasks", "/Delete", "/TN", c["task_name"], "/F"], timeout=30)
    print(out_text(p))
    return p.returncode


# ------------------------------------------------------------------ commands

def make_app(c: dict):
    from snaplab.app import App
    url = public_url(c)
    if url:
        c["public_url"] = url
    app = App(c)
    if c.get("local_mode"):
        return app
    st = auth.tailscale_status(tailscale_exe(c))
    app.learn_private_terms(st.get("dns_name") or "", st.get("owner") or "", (st.get("dns_name") or "").split(".")[0])
    return app


def cmd_serve(a, c) -> int:
    from snaplab import server
    if health(c):
        print("The server is already running.")
        return 0
    app = make_app(c)
    print(f"SnapLab {app.version} on {c['listen_host']}:{c['listen_port']} (public: {app.public_url or 'unknown'})", flush=True)
    if app.translator and app.translator.error:
        print(f"warning: translator not running: {app.translator.error}", flush=True)
    server.serve_forever(app, c["listen_host"], int(c["listen_port"]))
    return 0


def cmd_up(a, c) -> int:
    log = data_dir(c) / "logs" / "up.log"
    log.parent.mkdir(parents=True, exist_ok=True)

    def note(s: str) -> None:
        with open(log, "a", encoding="utf-8") as f:
            f.write(f"{dt.datetime.now():%Y-%m-%d %H:%M:%S} {s}\n")
        if sys.stdout is not None:
            print(s, flush=True)
    note(f"starting (pid {os.getpid()})")
    if health(c):
        note("already running; exiting")
        return 0
    if c.get("manage_tailscale_serve", True):
        for _ in range(6):
            msg = ensure_serve(c, apply=True)
            note(msg)
            if "could not read" not in msg:
                break
            time.sleep(10)
    rc = cmd_serve(a, c)
    end_soon(note)
    return rc


def end_soon(note, grace: float = 15.0) -> None:
    """The server has stopped. Python would wait for every worker thread (a reply or a summary in a long model call)
    before the process ends, and while it lingers Windows does not start the logon task again: on 2026-11-17 SnapLab
    stayed down after a deploy. Give the threads `grace` seconds, then end the process; the next start picks up
    the work that was cut off."""
    import threading
    end = time.time() + grace
    me = threading.current_thread()
    for t in threading.enumerate():
        if t is not me and not t.daemon:
            t.join(max(0.0, end - time.time()))
    left = [t.name for t in threading.enumerate() if t is not me and not t.daemon and t.is_alive()]
    if left:
        note(f"ending the process; still running: {', '.join(left[:8])}")
        logging_flush()
        os._exit(0)


def logging_flush() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream is not None:
                stream.flush()
        except Exception:
            pass


def cmd_status(a, c) -> int:
    h = health(c)
    info = json.loads((data_dir(c) / "server.json").read_text(encoding="utf-8")) if (data_dir(c) / "server.json").exists() else {}
    print(f"server: {'running' if h else 'stopped'}" + (f" ({c['listen_host']}:{c['listen_port']}, pid {info.get('pid')}, since {info.get('started')})" if h else ""))
    print(f"url: {public_url(c) or '(unknown: is Tailscale running?)'}")
    print(ensure_serve(c, apply=False))
    t = task_state(c)
    print(f"logon task {c['task_name']}: {t or 'not registered'}")
    from snaplab.llm import get_api_key
    print(f"API key ({c['api']['api_key_env']}): {'set' if get_api_key(c['api']['api_key_env']) else 'NOT SET'}")
    print(f"data folder: {c['data_dir']}")
    print(f"API slots: {c['slots']['count']} in {c['slots']['dir']}")
    st = auth.AuthStore(data_dir(c) / "auth.json")
    devs = [d for d in st.devices() if not d.get("revoked")]
    now = dt.datetime.now().astimezone()
    links = [p for p in st.pairings() if not p.get("used") and dt.datetime.fromisoformat(p["expires"]) > now]
    print(f"paired devices: {len(devs)}, unused links: {len(links)}")
    return 0


def cmd_pair(a, c) -> int:
    url = public_url(c)
    if not url:
        print("Cannot work out the public URL. Start Tailscale or set public_url in config.json.")
        return 1
    st = auth.AuthStore(data_dir(c) / "auth.json")
    token, rec = st.new_pairing(a.label, a.ttl_hours)
    print(f"Pairing link ({rec['id']} \"{rec['label']}\", one use, valid until {rec['expires'][:16].replace('T', ' ')}):")
    print(f"{url}/pair/{token}")
    return 0


def cmd_devices(a, c) -> int:
    st = auth.AuthStore(data_dir(c) / "auth.json")
    devs = st.devices()
    print("devices:" + (" none" if not devs else ""))
    for d in devs:
        state = f"revoked {d['revoked']}" if d.get("revoked") else "active"
        print(f"  {d['id']}  {d['label']}  {d.get('ua') or ''}  paired {d['created']}  last {d.get('last_seen')}  {state}")
    links = st.pairings()
    print("links:" + (" none" if not links else ""))
    now = dt.datetime.now().astimezone()
    for p in links:
        state = f"used {p['used']}" if p.get("used") else ("expired" if dt.datetime.fromisoformat(p["expires"]) < now else "unused")
        print(f"  {p['id']}  {p['label']}  created {p['created']}  expires {p['expires']}  {state}")
    return 0


def cmd_revoke(a, c) -> int:
    done = auth.AuthStore(data_dir(c) / "auth.json").revoke(a.id)
    print("revoked: " + (", ".join(done) if done else "nothing matched"))
    return 0 if done else 1


def cmd_start(a, c) -> int:
    if health(c):
        print("The server is already running.")
        return 0
    if task_state(c) is not None and not os.environ.get("SNAPLAB_DATA") and not os.environ.get("SNAPLAB_CONFIG"):
        print(out_text(run(["schtasks", "/Run", "/TN", c["task_name"]], timeout=30)))
    else:
        flags = NO_WINDOW | 0x00000200 | 0x01000000 if os.name == "nt" else 0
        subprocess.Popen([sys.executable, str(ROOT / "snaplab.py"), "up"], cwd=ROOT, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags, close_fds=True)
    for _ in range(60):
        time.sleep(1)
        if health(c):
            print("The server is running.")
            return 0
    print(f"Not running yet. See {data_dir(c) / 'logs'}")
    return 1


def cmd_stop(a, c) -> int:
    if not health(c):
        print("The server is not running.")
        return 0
    (data_dir(c) / "stop").write_text("stop", encoding="utf-8")
    for _ in range(40):
        time.sleep(0.5)
        if not health(c):
            print("Stopped.")
            return 0
    print("The server did not stop.")
    return 1


def cmd_tailscale(a, c) -> int:
    print(ensure_serve(c, apply=a.apply))
    return 0


def cmd_reset(a, c) -> int:
    if not a.yes:
        print("This moves the data folder aside so SnapLab starts with the first-run setup. Add --yes to do it.")
        return 1
    if health(c):
        print("Stop the server first: python snaplab.py stop")
        return 1
    d = data_dir(c)
    if not d.exists():
        print("Nothing to reset.")
        return 0
    dest = d.with_name(d.name + f"-old-{stamp()}")
    d.rename(dest)
    print(f"Moved {d} to {dest}. Delete it yourself when you no longer need it.")
    return 0


def cmd_check_secrets(a, c) -> int:
    from snaplab.guard import MARK
    from snaplab.llm import api_key_values
    names = {c["api"]["api_key_env"], "SNAPSCRIBE_API_KEY", "SNAPLAB_API_KEY", "BRAVE_API_KEY", "TAVILY_API_KEY"}
    names.update(p["api_key_env"] for p in (c.get("providers") or {}).values() if p.get("api_key_env"))
    names.update((c.get("guard") or {}).get("secret_env") or [])
    values = {value for name in names for value in api_key_values(name)}
    if not values:
        print("No API keys found in the configured sources; nothing to compare.")
        return 0
    p = subprocess.run(["git", "ls-files", "-co", "--exclude-standard"], cwd=ROOT, capture_output=True, text=True)
    bad = []
    for name in p.stdout.splitlines():
        f = ROOT / name
        try:
            if f.is_file():
                data = f.read_bytes()
                if any(value in name or value.encode(encoding) in data for value in values
                       for encoding in ("utf-8", "utf-16-le")):
                    for value in sorted(values, key=len, reverse=True):
                        name = name.replace(value, MARK)
                    bad.append(name)
        except OSError:
            continue
    if bad:
        print("The API key value was found in: " + ", ".join(bad))
        return 1
    print("OK: the API key value is not in any tracked or new file.")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("serve", "up", "status", "devices", "start", "stop", "install-task", "uninstall-task", "check-secrets"):
        sub.add_parser(name)
    pr = sub.add_parser("pair")
    pr.add_argument("--label", default="phone")
    pr.add_argument("--ttl-hours", type=float, default=auth.PAIR_TTL_HOURS)
    rv = sub.add_parser("revoke")
    rv.add_argument("id")
    ts = sub.add_parser("tailscale")
    ts.add_argument("--apply", action="store_true")
    rs = sub.add_parser("reset")
    rs.add_argument("--yes", action="store_true")
    a = p.parse_args(argv)
    if a.cmd == "pair" and not (0 < a.ttl_hours <= 24):
        p.error("--ttl-hours must be between 0 and 24")
    c = config.load()
    data_dir(c).mkdir(parents=True, exist_ok=True)
    return {"serve": cmd_serve, "up": cmd_up, "status": cmd_status, "pair": cmd_pair, "devices": cmd_devices,
            "revoke": cmd_revoke, "start": cmd_start, "stop": cmd_stop, "install-task": cmd_install_task,
            "uninstall-task": cmd_uninstall_task, "tailscale": cmd_tailscale, "reset": cmd_reset,
            "check-secrets": cmd_check_secrets}[a.cmd](a, c)


if __name__ == "__main__":
    sys.exit(main())
