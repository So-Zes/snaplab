"""Web Push notifications to the installed app (PWA), implemented with the `cryptography` package.

- VAPID (RFC 8292): an ES256 key pair kept in <data>/push/vapid_private.pem (git-ignored). The public key
  goes to the browser as applicationServerKey; each push carries a short-lived signed JWT.
- Payload encryption (RFC 8291, "aes128gcm" content coding): ephemeral ECDH on P-256 with the browser's
  key, HKDF with the browser's auth secret, AES-128-GCM.
- Only https endpoints of the known browser push services are contacted.

Three kinds (each can be switched off in Settings): needs_you (a bot asks something, an approval, a session waiting
for you), review (a session finished and is ready for review), failed (a session failed). The same notice is sent once.
"""
from __future__ import annotations

import base64
import json
import os
import struct
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from .store import new_id, now_iso

PUSH_HOST_SUFFIXES = ("fcm.googleapis.com", "push.services.mozilla.com", "notify.windows.com", "push.apple.com",
                      "android.googleapis.com")
KINDS = ("needs_you", "review", "failed")


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def unb64u(s: str) -> bytes:
    s = (s or "").strip()
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _hkdf(salt: bytes, ikm: bytes, info: bytes, length: int) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=length, salt=salt, info=info).derive(ikm)


def public_bytes(key: ec.EllipticCurvePublicKey) -> bytes:
    return key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)


def encrypt(payload: bytes, ua_public_b64: str, auth_b64: str, *, salt: bytes | None = None,
            as_private: ec.EllipticCurvePrivateKey | None = None, record_size: int = 4096) -> bytes:
    """RFC 8291 message encryption. Returns the request body (header + one encrypted record)."""
    ua_public = unb64u(ua_public_b64)
    auth = unb64u(auth_b64)
    if len(ua_public) != 65 or len(auth) < 16:
        raise ValueError("bad subscription keys")
    ua_key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_public)
    as_private = as_private or ec.generate_private_key(ec.SECP256R1())
    as_public = public_bytes(as_private.public_key())
    salt = salt or os.urandom(16)
    ecdh = as_private.exchange(ec.ECDH(), ua_key)
    ikm = _hkdf(auth, ecdh, b"WebPush: info\x00" + ua_public + as_public, 32)
    cek = _hkdf(salt, ikm, b"Content-Encoding: aes128gcm\x00", 16)
    nonce = _hkdf(salt, ikm, b"Content-Encoding: nonce\x00", 12)
    if len(payload) > record_size - 17 - 1:
        raise ValueError("payload too large for one record")
    ciphertext = AESGCM(cek).encrypt(nonce, payload + b"\x02", None)
    header = salt + struct.pack("!I", record_size) + bytes([len(as_public)]) + as_public
    return header + ciphertext


def decrypt(body: bytes, ua_private: ec.EllipticCurvePrivateKey, auth_b64: str) -> bytes:
    """The receiving side of RFC 8291 (used by the tests to prove encrypt() is correct)."""
    salt, rs, idlen = body[:16], struct.unpack("!I", body[16:20])[0], body[20]
    as_public = body[21:21 + idlen]
    ct = body[21 + idlen:]
    ua_public = public_bytes(ua_private.public_key())
    as_key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), as_public)
    ecdh = ua_private.exchange(ec.ECDH(), as_key)
    ikm = _hkdf(unb64u(auth_b64), ecdh, b"WebPush: info\x00" + ua_public + as_public, 32)
    cek = _hkdf(salt, ikm, b"Content-Encoding: aes128gcm\x00", 16)
    nonce = _hkdf(salt, ikm, b"Content-Encoding: nonce\x00", 12)
    plain = AESGCM(cek).decrypt(nonce, ct, None)
    assert rs >= len(ct)
    return plain.rstrip(b"\x00")[:-1]  # strip padding and the 0x02 delimiter


def vapid_jwt(private: ec.EllipticCurvePrivateKey, endpoint: str, subject: str, ttl_s: int = 12 * 3600) -> str:
    u = urllib.parse.urlparse(endpoint)
    header = b64u(json.dumps({"typ": "JWT", "alg": "ES256"}, separators=(",", ":")).encode())
    claims = b64u(json.dumps({"aud": f"{u.scheme}://{u.netloc}", "exp": int(time.time()) + ttl_s, "sub": subject},
                             separators=(",", ":")).encode())
    signing_input = f"{header}.{claims}".encode("ascii")
    der = private.sign(signing_input, ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(der)
    return f"{header}.{claims}.{b64u(r.to_bytes(32, 'big') + s.to_bytes(32, 'big'))}"


def endpoint_allowed(endpoint: str) -> bool:
    try:
        u = urllib.parse.urlparse(endpoint)
    except ValueError:
        return False
    host = (u.hostname or "").lower()
    return u.scheme == "https" and any(host == s or host.endswith("." + s) for s in PUSH_HOST_SUFFIXES)


class Push:
    def __init__(self, store, subject: str = "mailto:snaplab@example.com", sender=None, guard=None):
        self.store = store
        self.guard = guard
        self.subject = subject
        self.key_path = store.path("push", "vapid_private.pem")
        self._key = None
        self._lock = threading.Lock()
        self.sender = sender or self._post        # tests replace this
        self.last_results: list[dict] = []
        self._sent_keys: dict[str, float] = {}

    # ---------------------------------------------------------------- keys

    def private_key(self) -> ec.EllipticCurvePrivateKey:
        with self._lock:
            if self._key is None:
                if self.key_path.exists():
                    self._key = serialization.load_pem_private_key(self.key_path.read_bytes(), password=None)
                else:
                    self._key = ec.generate_private_key(ec.SECP256R1())
                    self.key_path.parent.mkdir(parents=True, exist_ok=True)
                    self.key_path.write_bytes(self._key.private_bytes(
                        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
            return self._key

    def public_key_b64(self) -> str:
        return b64u(public_bytes(self.private_key().public_key()))

    # ---------------------------------------------------------------- subscriptions

    def subscriptions(self) -> list[dict]:
        return self.store.read_json("push.json", {}).get("subscriptions", [])

    def subscribe(self, sub: dict, device: str = "", ua: str = "") -> dict:
        endpoint = str((sub or {}).get("endpoint") or "")
        keys = (sub or {}).get("keys") or {}
        if not endpoint_allowed(endpoint):
            raise ValueError("this push endpoint is not a known browser push service")
        if len(unb64u(keys.get("p256dh", ""))) != 65 or len(unb64u(keys.get("auth", ""))) < 16:
            raise ValueError("the subscription keys are not valid")
        with self.store.lock:
            data = self.store.read_json("push.json", {})
            subs = [s for s in data.get("subscriptions", []) if s["endpoint"] != endpoint]
            rec = {"id": new_id("ps"), "endpoint": endpoint, "keys": {"p256dh": keys["p256dh"], "auth": keys["auth"]},
                   "device": device, "ua": ua[:80], "created": now_iso()}
            subs.append(rec)
            data["subscriptions"] = subs[-20:]
            self.store.write_json("push.json", data)
        return rec

    def unsubscribe(self, endpoint: str | None = None, device: str | None = None) -> int:
        with self.store.lock:
            data = self.store.read_json("push.json", {})
            before = data.get("subscriptions", [])
            keep = [s for s in before if not ((endpoint and s["endpoint"] == endpoint) or (device and s.get("device") == device))]
            data["subscriptions"] = keep
            self.store.write_json("push.json", data)
            return len(before) - len(keep)

    # ---------------------------------------------------------------- sending

    def _post(self, endpoint: str, body: bytes, headers: dict) -> int:
        req = urllib.request.Request(endpoint, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code
        except (urllib.error.URLError, TimeoutError, OSError):
            return 0

    def send_one(self, sub: dict, message: dict, ttl: int = 3600, urgency: str = "normal") -> int:
        payload = json.dumps(message, ensure_ascii=False).encode("utf-8")
        body = encrypt(payload, sub["keys"]["p256dh"], sub["keys"]["auth"])
        jwt = vapid_jwt(self.private_key(), sub["endpoint"], self.subject)
        headers = {"TTL": str(ttl), "Content-Encoding": "aes128gcm", "Content-Type": "application/octet-stream",
                   "Urgency": urgency, "Authorization": f"vapid t={jwt}, k={self.public_key_b64()}"}
        return self.sender(sub["endpoint"], body, headers)

    def notify(self, kind: str, title: str, body: str, url: str = "/", tag: str | None = None,
               force: bool = False, wait: bool = False, dedupe: str | None = None) -> None:
        """Send to every subscribed device if this kind is switched on. Runs in the background.

        dedupe: a key such as "<session>:<kind>:<turn>"; the same key is sent only once (per 24 hours).
        """
        if dedupe:
            now = time.time()
            self._sent_keys = {k: v for k, v in self._sent_keys.items() if now - v < 86400}
            if dedupe in self._sent_keys:
                return
            self._sent_keys[dedupe] = now
        if not force:
            settings = self.store.settings()
            if not settings.get("notify", {}).get(kind, False):
                return
        subs = self.subscriptions()
        if not subs:
            return
        if self.guard:
            title, body = self.guard.clean(title), self.guard.clean(body)
        msg = {"title": title[:80], "body": body[:180], "url": url if url.startswith("/") else "/",
               "tag": tag or kind, "kind": kind, "ts": now_iso()}

        def go():
            results = []
            for s in subs:
                try:
                    code = self.send_one(s, msg, urgency="high" if kind == "needs_you" else "normal")
                except Exception as e:  # noqa: BLE001
                    code = -1
                    results.append({"device": s.get("device"), "error": str(e)[:120]})
                results.append({"device": s.get("device"), "status": code})
                if code in (404, 410):
                    self.unsubscribe(endpoint=s["endpoint"])
            self.last_results = results
        if wait:
            go()
        else:
            threading.Thread(target=go, daemon=True).start()
