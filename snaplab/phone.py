"""Use SnapLab from a phone: Tailscale on this PC, `tailscale serve` for HTTPS inside the user's own tailnet, and a
QR code that pairs the phone with a one-time link.

The setup runs as a small state machine in a background thread, started from the PC's browser (never from a phone):

    install  -> Tailscale is installed (Windows: winget, or the signed MSI; Windows asks for permission once)
    signin   -> the user signs in to Tailscale (we show the sign-in link that `tailscale login` prints)
    https    -> `tailscale serve` publishes SnapLab on https://<this-pc>.<tailnet>.ts.net:<port> inside the tailnet;
                a tailnet that has never used HTTPS asks the user to allow it once (we show that link)
    pair     -> a QR code with a 15-minute one-time pairing link; the phone needs Tailscale signed in to the same account
    done     -> a phone was paired

Requests that arrive through `tailscale serve` carry Tailscale-User-Login; server.py accepts them only from the
tailnet owner and only from paired devices, exactly like the PC browser's own pairing.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

URL_RE = re.compile(r"https://login\.tailscale\.com/[^\s\"']+")
DENIED_RE = re.compile(r"access denied|permission denied|--operator", re.I)
PAIR_TTL_HOURS = 0.25
NO_WINDOW = 0x08000000 if os.name == "nt" else 0
MSI_URL = "https://pkgs.tailscale.com/stable/tailscale-setup-latest-amd64.msi"


def tailscale_exe(cfg: dict) -> str | None:
    if cfg.get("tailscale_exe"):
        return cfg["tailscale_exe"]
    found = shutil.which("tailscale")
    if found:
        return found
    for c in (r"C:\Program Files\Tailscale\tailscale.exe", "/Applications/Tailscale.app/Contents/MacOS/Tailscale",
              "/usr/bin/tailscale", "/usr/local/bin/tailscale"):
        if Path(c).exists():
            return c
    return None


def run(cmd, timeout=30):
    return subprocess.run(cmd, capture_output=True, timeout=timeout, creationflags=NO_WINDOW)


def qr_svg(text: str) -> str:
    from .vendor.qrcodegen import QrCode
    qr = QrCode.encode_text(text, QrCode.Ecc.MEDIUM)
    n, border = qr.get_size(), 3
    parts = [f"M{x + border},{y + border}h1v1h-1z" for y in range(n) for x in range(n) if qr.get_module(x, y)]
    size = n + border * 2
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {size} {size}" shape-rendering="crispEdges">'
            f'<rect width="100%" height="100%" fill="#fff"/><path d="{"".join(parts)}" fill="#000"/></svg>')


class Phone:
    def __init__(self, app):
        self.app = app
        self.lock = threading.Lock()
        self.worker: threading.Thread | None = None
        self.login_proc: subprocess.Popen | None = None
        self.state = {"step": "idle", "error": None, "auth_url": None, "enable_url": None}
        self._pair: dict | None = None
        self._status_cache: tuple[float, dict] = (0.0, {})

    # ------------------------------------------------------------ settings and status

    def settings(self) -> dict:
        return self.app.store.settings().get("phone") or {}

    @property
    def enabled(self) -> bool:
        return bool(self.settings().get("enabled"))

    def https_port(self) -> int:
        return int(self.settings().get("https_port") or self.app.cfg.get("tailscale_https_port") or 8445)

    def exe(self) -> str | None:
        return tailscale_exe(self.app.cfg)

    def tailscale(self, fresh: bool = False) -> dict:
        """{installed, backend, dns_name, owner, auth_url} from `tailscale status --json` (cached for 5 s)."""
        at, cached = self._status_cache
        if not fresh and time.time() - at < 5:
            return cached
        exe = self.exe()
        out = {"installed": bool(exe), "backend": None, "dns_name": None, "owner": None, "auth_url": None}
        if exe:
            try:
                st = json.loads(run([exe, "status", "--json"], 15).stdout.decode("utf-8", "replace"))
                me = st.get("Self") or {}
                user = (st.get("User") or {}).get(str(me.get("UserID"))) or {}
                out.update(backend=st.get("BackendState"), dns_name=(me.get("DNSName") or "").rstrip(".") or None,
                           owner=user.get("LoginName") or None, auth_url=st.get("AuthURL") or None)
            except (OSError, ValueError, subprocess.SubprocessError):
                pass
        self._status_cache = (time.time(), out)
        return out

    def url(self) -> str | None:
        dns = self.tailscale().get("dns_name")
        if not dns:
            return None
        port = self.https_port()
        return f"https://{dns}" + ("" if port == 443 else f":{port}")

    def host_ok(self, host: str) -> bool:
        """Is this Host header the address `tailscale serve` publishes SnapLab on?"""
        dns = self.tailscale().get("dns_name")
        if not dns or not host:
            return False
        host = host.lower()
        port = self.https_port()
        return host == dns.lower() + f":{port}" or (port == 443 and host == dns.lower())

    def summary(self) -> dict:
        ts = self.tailscale()
        with self.lock:
            st = dict(self.state)
        out = {"enabled": self.enabled, "os": sys.platform, "installed": ts["installed"], "backend": ts["backend"],
               "dns_name": ts["dns_name"], "url": self.url() if self.enabled else None, **st,
               "devices": len(self.app.auth.devices())}
        if st["step"] == "pair" and self._pair:
            # The token goes on its own: JSON responses hide every "/pair/<token>" (redact_log), and the PC's page
            # puts the link back together for the user to read.
            out.update(pair_token=self._pair["token"], qr=self._pair["qr"], pair_expires=self._pair["expires"])
        return out

    # ------------------------------------------------------------ the setup worker

    def start(self) -> dict:
        with self.lock:
            if self.worker and self.worker.is_alive():
                return self.summary_unlocked()
            self.state = {"step": "install", "error": None, "auth_url": None, "enable_url": None}
            self.worker = threading.Thread(target=self._run, daemon=True, name="phone-setup")
            self.worker.start()
        return self.summary()

    def summary_unlocked(self) -> dict:
        return dict(self.state)

    def _set(self, **changes):
        with self.lock:
            self.state.update(changes)

    def _run(self):
        try:
            if not self.tailscale(fresh=True)["installed"]:
                self._set(step="install")
                self.install()
                if not self.tailscale(fresh=True)["installed"]:
                    raise RuntimeError("install_failed")
            self._set(step="signin")
            self.sign_in()
            self._set(step="https", auth_url=None)
            self.publish()
            self.app.store.update_settings(phone={**self.settings(), "enabled": True, "https_port": self.https_port()})
            self.app.log("phone access: on at " + str(self.url()))
            self.new_pairing()
            before = len(self.app.auth.devices())
            self._set(step="pair", enable_url=None)
            deadline = time.time() + 3600
            while time.time() < deadline:
                if len(self.app.auth.devices()) > before:
                    self._set(step="done")
                    return
                if self._pair and time.time() > self._pair["until"]:
                    self.new_pairing()
                time.sleep(2)
            self._set(step="pair_timeout")
        except Exception as e:  # noqa: BLE001 - shown to the user as a short code
            code = str(e) if re.fullmatch(r"[a-z_]+", str(e)) else "failed"
            self.app.log(f"phone setup stopped: {e!r}")
            self._set(error=code)

    def install(self):
        if os.name != "nt":
            raise RuntimeError("install_manually")
        winget = shutil.which("winget")
        if winget:
            r = run([winget, "install", "--id", "Tailscale.Tailscale", "--exact", "--source", "winget", "--silent",
                     "--accept-package-agreements", "--accept-source-agreements", "--disable-interactivity"], 1800)
            self.app.log(f"phone: winget install tailscale -> {r.returncode}")
            if self.tailscale(fresh=True)["installed"]:
                return
        self._install_msi()

    def _install_msi(self):
        """Download the signed MSI and run it with administrator permission (Windows shows its own prompt)."""
        folder = Path(self.app.cfg["data_dir"]) / "tmp"
        folder.mkdir(parents=True, exist_ok=True)
        msi = folder / "tailscale-setup.msi"
        urllib.request.urlretrieve(MSI_URL, msi)
        check = run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                     f"$s=Get-AuthenticodeSignature -LiteralPath '{msi}'; $s.Status.ToString(); $s.SignerCertificate.Subject"], 120)
        text = check.stdout.decode("utf-8", "replace")
        if not text.startswith("Valid") or "Tailscale" not in text:
            msi.unlink(missing_ok=True)
            raise RuntimeError("install_failed")
        import ctypes
        from ctypes import wintypes

        class SHELLEXECUTEINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("fMask", ctypes.c_ulong), ("hwnd", wintypes.HWND),
                        ("lpVerb", wintypes.LPCWSTR), ("lpFile", wintypes.LPCWSTR), ("lpParameters", wintypes.LPCWSTR),
                        ("lpDirectory", wintypes.LPCWSTR), ("nShow", ctypes.c_int), ("hInstApp", wintypes.HINSTANCE),
                        ("lpIDList", ctypes.c_void_p), ("lpClass", wintypes.LPCWSTR), ("hkeyClass", wintypes.HKEY),
                        ("dwHotKey", wintypes.DWORD), ("hIcon", wintypes.HANDLE), ("hProcess", wintypes.HANDLE)]
        info = SHELLEXECUTEINFO(cbSize=ctypes.sizeof(SHELLEXECUTEINFO), fMask=0x40, lpVerb="runas",
                                lpFile="msiexec.exe", lpParameters=f'/i "{msi}" /quiet /norestart', nShow=0)
        if not ctypes.windll.shell32.ShellExecuteExW(ctypes.byref(info)):
            msi.unlink(missing_ok=True)
            raise RuntimeError("install_denied")
        ctypes.windll.kernel32.WaitForSingleObject(info.hProcess, 30 * 60 * 1000)
        ctypes.windll.kernel32.CloseHandle(info.hProcess)
        msi.unlink(missing_ok=True)
        for _ in range(30):
            if self.tailscale(fresh=True)["installed"]:
                return
            time.sleep(2)

    def sign_in(self):
        deadline = time.time() + 1800
        while time.time() < deadline:
            ts = self.tailscale(fresh=True)
            if ts["backend"] == "Running" and ts["dns_name"]:
                return
            if self.state.get("error"):
                raise RuntimeError(self.state["error"])
            url = ts.get("auth_url") or self._login_url()
            if url:
                self._set(auth_url=url)
            time.sleep(3)
        raise RuntimeError("signin_timeout")

    def _login_url(self) -> str | None:
        """Start `tailscale login` once and read the sign-in link it prints."""
        if self.login_proc is None or self.login_proc.poll() is not None:
            self.login_proc = subprocess.Popen([self.exe(), "login"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                               stdin=subprocess.DEVNULL, creationflags=NO_WINDOW)
            found: list[str] = []

            def read(proc=self.login_proc):
                for raw in iter(proc.stdout.readline, b""):
                    if DENIED_RE.search(raw.decode("utf-8", "replace")):
                        self._set(error="operator")          # Linux: this account may not manage Tailscale yet
                    m = URL_RE.search(raw.decode("utf-8", "replace"))
                    if m:
                        found.append(m.group(0))
                        self._set(auth_url=m.group(0))
            threading.Thread(target=read, daemon=True).start()
            time.sleep(4)
            return found[0] if found else None
        return None

    def serve_state(self) -> str:
        """'ours', 'free' or 'taken' for our HTTPS port in `tailscale serve status --json`."""
        try:
            st = json.loads(run([self.exe(), "serve", "status", "--json"], 20).stdout.decode("utf-8", "replace") or "{}")
        except (OSError, ValueError, subprocess.SubprocessError):
            return "free"
        target = f"http://127.0.0.1:{self.app.cfg['listen_port']}"
        for name, web in (st.get("Web") or {}).items():
            if not name.endswith(f":{self.https_port()}"):
                continue
            proxies = [h.get("Proxy") for h in (web.get("Handlers") or {}).values()]
            return "ours" if target in proxies or target + "/" in proxies else "taken"
        return "free"

    def publish(self):
        deadline = time.time() + 1800
        while time.time() < deadline:
            state = self.serve_state()
            if state == "ours":
                return
            if state == "taken":
                raise RuntimeError("port_taken")
            cmd = [self.exe(), "serve", "--bg", f"--https={self.https_port()}", f"http://127.0.0.1:{self.app.cfg['listen_port']}"]
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                    creationflags=NO_WINDOW)
            try:
                out, _ = proc.communicate(timeout=20)
            except subprocess.TimeoutExpired:
                # It waits when HTTPS is not allowed on the tailnet yet; it printed the link to allow it.
                proc.kill()
                out, _ = proc.communicate()
            text = out.decode("utf-8", "replace")
            if DENIED_RE.search(text):
                raise RuntimeError("operator")
            m = URL_RE.search(text)
            if self.serve_state() == "ours":
                return
            if m:
                self._set(enable_url=m.group(0))
            time.sleep(5)
        raise RuntimeError("https_timeout")

    def new_pairing(self) -> dict:
        token, rec = self.app.auth.new_pairing("Phone", PAIR_TTL_HOURS)
        url = f"{self.url()}/pair/{token}"
        self._pair = {"url": url, "token": token, "qr": qr_svg(url), "expires": rec["expires"],
                      "until": time.time() + PAIR_TTL_HOURS * 3600 - 30}
        return self._pair

    def refresh_pairing(self) -> dict:
        if not self.enabled:
            raise ValueError("phone access is off")
        self.new_pairing()
        with self.lock:
            if self.state.get("step") not in ("pair",):
                self.state = {"step": "pair", "error": None, "auth_url": None, "enable_url": None}
        if not (self.worker and self.worker.is_alive()):
            self.worker = threading.Thread(target=self._wait_for_phone, daemon=True, name="phone-pair")
            self.worker.start()
        return self.summary()

    def _wait_for_phone(self):
        before = len(self.app.auth.devices())
        deadline = time.time() + 3600
        while time.time() < deadline:
            if len(self.app.auth.devices()) > before:
                self._set(step="done")
                return
            if self._pair and time.time() > self._pair["until"]:
                self.new_pairing()
            time.sleep(2)

    def disable(self) -> dict:
        exe = self.exe()
        if exe and self.serve_state() == "ours":
            run([exe, "serve", f"--https={self.https_port()}", "off"], 30)
        self.app.store.update_settings(phone={**self.settings(), "enabled": False})
        with self.lock:
            self.state = {"step": "idle", "error": None, "auth_url": None, "enable_url": None}
        self._pair = None
        return self.summary()

    def ack(self) -> dict:
        """The user saw the result: next time the page shows the plain on/off state."""
        with self.lock:
            if self.state.get("step") in ("done", "pair_timeout"):
                self.state = {"step": "idle", "error": None, "auth_url": None, "enable_url": None}
        return self.summary()

    def decline(self) -> dict:
        self.app.store.update_settings(phone={**self.settings(), "offered": True})
        return self.summary()
