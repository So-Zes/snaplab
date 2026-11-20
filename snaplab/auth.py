"""Two checks on every request (adapted from the author's research portal):

  (a) the Tailscale-User-Login header that `tailscale serve` adds must be the tailnet owner's login
      (tailscale serve strips a client-sent header of the same name before adding its own), and
  (b) device pairing: a one-time link that expires in 24 hours gives the device a long-lived cookie.

The auth file (<data>/auth.json) stores only SHA-256 digests of link tokens and cookie values.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import json
import os
import secrets
import subprocess
import threading
import time
from pathlib import Path

COOKIE_NAME = "__Host-snaplab"       # __Host- forces Secure, Path=/ and no Domain
COOKIE_MAX_AGE = 400 * 24 * 3600     # the longest browsers accept
PAIR_TTL_HOURS = 24


def now() -> dt.datetime:
    return dt.datetime.now().astimezone()


def iso(t: dt.datetime) -> str:
    return t.isoformat(timespec="seconds")


def digest(value: str) -> str:
    return hashlib.sha256(("snaplab:" + value).encode("utf-8")).hexdigest()


class AuthStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.lock_path = self.path.with_suffix(".lock")
        self._mem_lock = threading.RLock()
        self._cache = None
        self._cache_mtime = None
        self._seen_written: dict[str, float] = {}

    def load(self) -> dict:
        with self._mem_lock:
            try:
                m = self.path.stat().st_mtime_ns
            except FileNotFoundError:
                return {"pairings": [], "devices": []}
            if self._cache is not None and m == self._cache_mtime:
                return self._cache
            for _ in range(20):
                try:
                    data = json.loads(self.path.read_text(encoding="utf-8"))
                    break
                except (PermissionError, json.JSONDecodeError):
                    time.sleep(0.05)
            else:
                data = {"pairings": [], "devices": []}
            data.setdefault("pairings", [])
            data.setdefault("devices", [])
            self._cache, self._cache_mtime = data, m
            return data

    @contextlib.contextmanager
    def edit(self):
        """Read, change and atomically rewrite auth.json; a lock file keeps CLI and server in order."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._mem_lock:
            fd = None
            for _ in range(200):
                try:
                    fd = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                    break
                except FileExistsError:
                    try:
                        if time.time() - self.lock_path.stat().st_mtime > 10:
                            self.lock_path.unlink(missing_ok=True)
                            continue
                    except FileNotFoundError:
                        continue
                    time.sleep(0.05)
            if fd is None:
                raise RuntimeError("could not lock auth.json")
            try:
                self._cache = None
                data = json.loads(json.dumps(self.load()))
                yield data
                tmp = self.path.with_suffix(".tmp")
                tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
                for _ in range(20):
                    try:
                        os.replace(tmp, self.path)
                        break
                    except PermissionError:
                        time.sleep(0.05)
                self._cache = None
            finally:
                os.close(fd)
                self.lock_path.unlink(missing_ok=True)

    def new_pairing(self, label: str, ttl_hours: float = PAIR_TTL_HOURS) -> tuple[str, dict]:
        token = secrets.token_urlsafe(32)
        t = now()
        rec = {"id": "p-" + secrets.token_hex(3), "hash": digest(token), "label": (label or "device")[:40],
               "created": iso(t), "expires": iso(t + dt.timedelta(hours=float(ttl_hours))), "used": None, "device": None}
        with self.edit() as d:
            keep = []
            for p in d["pairings"]:
                try:
                    old = now() - dt.datetime.fromisoformat(p["expires"]) > dt.timedelta(days=7)
                except (KeyError, ValueError):
                    old = True
                if not old:
                    keep.append(p)
            d["pairings"] = keep + [rec]
        return token, rec

    def pairing_state(self, token: str) -> tuple[str, dict | None]:
        h = digest(token or "")
        for p in self.load()["pairings"]:
            if secrets.compare_digest(p.get("hash", ""), h):
                if p.get("used"):
                    return "used", p
                if dt.datetime.fromisoformat(p["expires"]) < now():
                    return "expired", p
                return "ok", p
        return "unknown", None

    def redeem(self, token: str, user_agent: str = "") -> tuple[str, str | None, dict | None]:
        h = digest(token or "")
        with self.edit() as d:
            hit = next((p for p in d["pairings"] if secrets.compare_digest(p.get("hash", ""), h)), None)
            if not hit:
                return "unknown", None, None
            if hit.get("used"):
                return "used", None, None
            if dt.datetime.fromisoformat(hit["expires"]) < now():
                return "expired", None, None
            value = secrets.token_urlsafe(32)
            dev = {"id": "d-" + secrets.token_hex(3), "hash": digest(value), "label": hit.get("label") or "device",
                   "created": iso(now()), "last_seen": iso(now()), "ua": short_ua(user_agent), "revoked": None,
                   "pairing": hit["id"]}
            hit["used"] = iso(now())
            hit["device"] = dev["id"]
            d["devices"].append(dev)
        return "ok", value, dev

    def device_for(self, cookie_value: str | None) -> dict | None:
        if not cookie_value:
            return None
        h = digest(cookie_value)
        for dv in self.load()["devices"]:
            if dv.get("revoked"):
                continue
            if secrets.compare_digest(dv.get("hash", ""), h):
                return dv
        return None

    def touch(self, dev: dict, every_s: float = 300) -> None:
        last = self._seen_written.get(dev["id"], 0)
        if time.time() - last < every_s:
            return
        self._seen_written[dev["id"]] = time.time()
        try:
            with self.edit() as d:
                for x in d["devices"]:
                    if x["id"] == dev["id"]:
                        x["last_seen"] = iso(now())
        except RuntimeError:
            pass

    def should_renew(self, dev: dict, every_s: float) -> bool:
        last = dev.get("renewed") or dev.get("created")
        try:
            due = (now() - dt.datetime.fromisoformat(last)).total_seconds() >= every_s
        except (TypeError, ValueError):
            due = True
        if not due:
            return False
        try:
            with self.edit() as d:
                for x in d["devices"]:
                    if x["id"] == dev["id"]:
                        x["renewed"] = iso(now())
        except RuntimeError:
            return False
        return True

    def devices(self) -> list[dict]:
        return [{k: v for k, v in dv.items() if k != "hash"} for dv in self.load()["devices"]]

    def pairings(self) -> list[dict]:
        return [{k: v for k, v in p.items() if k != "hash"} for p in self.load()["pairings"]]

    def revoke(self, ident: str) -> list[str]:
        """Revoke a device (d-...), an unused link (p-...), "all" devices, or "all-links"."""
        done = []
        with self.edit() as d:
            for dv in d["devices"]:
                if not dv.get("revoked") and (ident == "all" or dv["id"] == ident):
                    dv["revoked"] = iso(now())
                    done.append(dv["id"])
            for p in d["pairings"]:
                if not p.get("used") and (ident in ("all-links", "all") or p["id"] == ident):
                    p["used"] = "revoked " + iso(now())
                    done.append(p["id"])
        return done


def short_ua(ua: str) -> str:
    ua = ua or ""
    for k, name in (("iPhone", "iPhone"), ("iPad", "iPad"), ("Android", "Android"), ("Windows", "Windows"),
                    ("Macintosh", "Mac")):
        if k in ua:
            browser = "Edge" if "Edg/" in ua else "Chrome" if ("Chrome/" in ua or "CriOS" in ua) else \
                "Safari" if "Safari/" in ua else "browser"
            return f"{name} / {browser}"
    return ua[:40]


class Owner:
    """The tailnet owner's login, from `tailscale status --json`. Unknown owner means nobody gets in."""

    def __init__(self, fixed: str | None = None, tailscale: str = "tailscale"):
        self.fixed = fixed
        self.tailscale = tailscale
        self.login: str | None = fixed
        self.t_try = 0.0
        self._lock = threading.Lock()

    def get(self) -> str | None:
        if self.login:
            return self.login
        with self._lock:
            if self.login or time.time() - self.t_try < 30:
                return self.login
            self.t_try = time.time()
            self.login = tailscale_status(self.tailscale).get("owner")
            return self.login


def tailscale_status(tailscale: str = "tailscale") -> dict:
    """{owner, dns_name} of this machine, or {} when tailscale is not running."""
    try:
        kw = {"creationflags": 0x08000000} if os.name == "nt" else {}
        r = subprocess.run([tailscale, "status", "--json"], capture_output=True, timeout=15, **kw)
        st = json.loads(r.stdout.decode("utf-8", "replace"))
        me = st.get("Self") or {}
        user = (st.get("User") or {}).get(str(me.get("UserID"))) or {}
        return {"owner": user.get("LoginName") or None, "dns_name": (me.get("DNSName") or "").rstrip(".") or None}
    except (OSError, ValueError, subprocess.SubprocessError):
        return {}


def check_user(headers, owner_login: str | None) -> tuple[bool, str]:
    if headers.get("Tailscale-Funnel-Request"):
        return False, "Connections from the public internet (Funnel) are not accepted."
    login = (headers.get("Tailscale-User-Login") or "").strip()
    if not owner_login:
        return False, "The tailnet owner is unknown. Is Tailscale running on the PC?"
    if not login:
        return False, "This connection did not come through Tailscale."
    if login.lower() != owner_login.lower():
        return False, "Only the owner of this tailnet can use SnapLab."
    return True, ""


def parse_cookies(header: str | None) -> dict:
    out = {}
    for part in (header or "").split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def cookie_header(value: str) -> str:
    return f"{COOKIE_NAME}={value}; Path=/; Max-Age={COOKIE_MAX_AGE}; Secure; HttpOnly; SameSite=Lax"
