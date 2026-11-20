"""Phone access: requests through `tailscale serve` on a local installation. All logins and hosts are synthetic."""
import http.client
import json
import tempfile
import threading
import time
import unittest

from helpers import fake_credentials, make_app
from snaplab import phone
from snaplab.server import make_server

HOST = "pc.example.ts.net"
OWNER = "owner@example.com"
VIA = {"Host": f"{HOST}:8445", "Tailscale-User-Login": OWNER}
ORIGIN = {"Origin": f"https://{HOST}:8445"}


class PhoneHTTPTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        creds = fake_credentials()
        creds.__enter__()
        self.addCleanup(creds.__exit__, None, None, None)
        self.app, _, _ = make_app(tmp=self.temp.name, local_mode=True, api={"model": "configure-model", "api_key_env": "SNAPLAB_API_KEY"})
        self.addCleanup(self.app.archive.close)
        self.server = make_server(self.app, "127.0.0.1", 0)
        self.port = self.server.server_port
        self.app.cfg["listen_port"] = self.port
        self.app.public_url = f"http://127.0.0.1:{self.port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.app.store.update_settings(phone={"enabled": True, "https_port": 8445}, onboarded=True)
        self.status(OWNER)

    def request(self, path="/", method="GET", body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=4)
        h = {"Content-Type": "application/json", "X-SnapLab": "1", **(headers or {})}
        conn.request(method, path, json.dumps(body).encode() if body is not None else None, h)
        r = conn.getresponse()
        result = r.status, dict(r.getheaders()), r.read()
        conn.close()
        return result

    def pair(self):
        token, _ = self.app.auth.new_pairing("test", 1)
        status, h, _ = self.request("/api/pair", "POST", {"token": token})
        self.assertEqual(status, 200)
        return h["Set-Cookie"].split(";", 1)[0]

    def status(self, owner):
        # A cached `tailscale status` for this PC: no Tailscale needed in tests.
        self.app.phone._status_cache = (time.time() + 3600, {"installed": True, "backend": "Running", "dns_name": HOST,
                                                             "owner": owner, "auth_url": None})

    def pair_phone(self):
        token, _ = self.app.auth.new_pairing("Phone", .25)
        status, headers, _ = self.request("/api/pair", "POST", {"token": token}, {**VIA, **ORIGIN})
        self.assertEqual(status, 200)
        cookie = headers["Set-Cookie"]
        self.assertTrue(cookie.startswith("__Host-snaplab="))
        self.assertIn("Secure", cookie)
        return cookie.split(";", 1)[0]

    def test_owner_through_tailscale_serve_still_needs_pairing(self):
        self.assertEqual(self.request("/api/bootstrap", headers=VIA)[0], 401)
        cookie = self.pair_phone()
        self.assertEqual(self.request("/api/bootstrap", headers={**VIA, "Cookie": cookie})[0], 200)
        # The phone's cookie is not the PC's local cookie.
        self.assertEqual(self.request("/api/bootstrap", headers={"Cookie": cookie})[0], 401)

    def test_other_tailnet_users_funnel_and_other_hosts_are_denied(self):
        self.assertEqual(self.request("/api/bootstrap", headers={**VIA, "Tailscale-User-Login": "guest@example.com"})[0], 403)
        self.assertEqual(self.request("/api/bootstrap", headers={**VIA, "Tailscale-Funnel-Request": "?1"})[0], 403)
        self.assertEqual(self.request("/api/bootstrap", headers={**VIA, "Host": "other.example.ts.net:8445"})[0], 403)
        self.assertEqual(self.request("/api/bootstrap", headers={**VIA, "Host": f"{HOST}:9999"})[0], 403)

    def test_nothing_gets_through_when_phone_access_is_off(self):
        self.app.store.update_settings(phone={"enabled": False, "https_port": 8445})
        token, _ = self.app.auth.new_pairing("Phone", .25)
        self.assertEqual(self.request("/api/pair", "POST", {"token": token}, {**VIA, **ORIGIN})[0], 403)

    def test_unknown_owner_denies(self):
        self.status(None)
        self.assertEqual(self.request("/api/bootstrap", headers=VIA)[0], 403)

    def test_phone_posts_need_the_phone_origin_and_cannot_change_phone_setup(self):
        cookie = self.pair_phone()
        headers = {**VIA, "Cookie": cookie}
        self.assertEqual(self.request("/api/settings", "POST", {"theme": "dark"}, {**headers, "Origin": "https://attacker.example"})[0], 403)
        self.assertEqual(self.request("/api/settings", "POST", {"theme": "dark"}, {**headers, **ORIGIN})[0], 200)
        status, _, body = self.request("/api/phone", "POST", {"action": "disable"}, {**headers, **ORIGIN})
        self.assertNotEqual(status, 200)
        self.assertTrue(self.app.phone.enabled)

    def test_pc_can_read_status_and_decline(self):
        cookie = self.pair()
        status, _, body = self.request("/api/phone", headers={"Cookie": cookie})
        self.assertEqual(status, 200)
        self.assertIn(b'"via_phone":false', body)
        self.assertEqual(self.request("/api/phone", "POST", {"action": "decline"}, {"Cookie": cookie})[0], 200)
        self.assertTrue(self.app.store.settings()["phone"]["offered"])


class PhoneUnitTest(unittest.TestCase):
    def test_pairing_link_reaches_the_pc_page_unredacted(self):
        app = PhoneHTTPTest("setUp")
        app.setUp()
        try:
            app.app.phone.new_pairing()
            with app.app.phone.lock:
                app.app.phone.state["step"] = "pair"
            cookie = app.pair()
            status, _, body = app.request("/api/phone", headers={"Cookie": cookie})
            data = json.loads(body)
            self.assertEqual(status, 200)
            self.assertEqual(len(data["pair_token"]), 43)
            self.assertTrue(app.app.auth.pairing_state(data["pair_token"])[0] == "ok")
        finally:
            app.doCleanups()

    def test_qr_svg_is_a_square_svg(self):
        svg = phone.qr_svg("https://pc.example.ts.net:8445/pair/" + "a" * 43)
        self.assertTrue(svg.startswith("<svg"))
        self.assertIn("<path d=\"M", svg)
        self.assertNotIn("<script", svg)

    def test_login_url_pattern(self):
        text = "To authenticate, visit:\n\n\thttps://login.tailscale.com/a/abc123def\n"
        self.assertEqual(phone.URL_RE.search(text).group(0), "https://login.tailscale.com/a/abc123def")


if __name__ == "__main__":
    unittest.main()
