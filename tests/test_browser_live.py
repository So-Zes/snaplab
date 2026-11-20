"""The browser panel (snaplab/browser_live.py) against tests/fake_cdp_worker.py instead of a real workspace."""
import http.client
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from helpers import fake_credentials, make_app, wait_until
from snaplab import browser_live
from snaplab.browser_live import BrowserError, LiveBrowsers, page_url
from snaplab.harnesses import remote
from snaplab.server import make_server

FAKE = str(Path(__file__).with_name("fake_cdp_worker.py"))
SID = "s-0000abcd"


class Base(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        creds = fake_credentials()
        creds.__enter__()
        self.addCleanup(creds.__exit__, None, None, None)
        self.app, _, _ = make_app(tmp=self.temp.name, local_mode=True)
        self.addCleanup(self.app.archive.close)
        self.app.store.save_session({"id": SID, "project": "p-00000001", "harness": "codex", "execution_backend": "wsl",
                                     "title": "Sign in", "status": "done"})
        self.log = Path(self.temp.name) / "browser.log"
        self.p1 = patch.object(LiveBrowsers, "connect_config", lambda self_, sid, stack, host: ({"host_addresses": []}, None, None, None))
        self.p2 = patch.object(remote, "ssh_command", lambda cfg, a, b: [sys.executable, FAKE, str(self.log)])
        self.p1.start()
        self.p2.start()
        self.addCleanup(self.p1.stop)
        self.addCleanup(self.p2.stop)
        self.addCleanup(self.app.browsers.close_all)

    def sent(self):
        try:
            return [json.loads(x) for x in self.log.read_text(encoding="utf-8").splitlines()]
        except OSError:
            return []

    def methods(self):
        return [m.get("method") for m in self.sent() if m.get("method")]

    def open(self):
        lb = self.app.browsers.get(SID)
        lb.add_viewer()
        self.assertTrue(wait_until(lambda: lb.view()["status"] == "ready" and lb.casting, 10))
        return lb


class LiveBrowserTest(Base):
    def test_host_mode_shows_frames_and_takes_input(self):
        lb = self.open()
        v = lb.view()
        self.assertEqual(v["mode"], "host")
        self.assertEqual(v["holder"], "user")
        self.assertTrue(v["can_input"])
        self.assertEqual([t["id"] for t in v["tabs"]], ["T1"])        # DevTools windows are not tabs
        self.assertEqual(v["url"], "https://example.com/")
        self.assertTrue(wait_until(lambda: lb.frame and lb.frame[1] == "BBBB", 5))
        frame, _ = lb.wait(0, 10 ** 9, 1)
        self.assertEqual(frame[2], {"w": 1000, "h": 500})
        self.assertEqual(lb.input([{"t": "tap", "x": 0.5, "y": 0.25}, {"t": "text", "text": "こんにちは"},
                                   {"t": "key", "type": "down", "key": "Enter", "code": "Enter", "vk": 13, "text": "\r"},
                                   {"t": "wheel", "x": 0.1, "y": 0.1, "dy": 120}]), 4)
        sent = self.sent()
        press = next(m for m in sent if m.get("method") == "Input.dispatchMouseEvent" and m["params"]["type"] == "mousePressed")
        self.assertEqual((press["params"]["x"], press["params"]["y"]), (500.0, 125.0))
        self.assertEqual(press["sessionId"], "S-T1")
        self.assertIn({"text": "こんにちは"}, [m["params"] for m in sent if m.get("method") == "Input.insertText"])
        key = next(m for m in sent if m.get("method") == "Input.dispatchKeyEvent")
        self.assertEqual((key["params"]["type"], key["params"]["text"]), ("keyDown", "\r"))
        self.assertTrue(any(m.get("method") == "Page.screencastFrameAck" for m in sent))
        lb.navigate("go", "example.org/a")
        self.assertIn({"url": "https://example.org/a"}, [m["params"] for m in self.sent() if m.get("method") == "Page.navigate"])
        lb.navigate("back")
        self.assertIn({"entryId": 11}, [m["params"] for m in self.sent() if m.get("method") == "Page.navigateToHistoryEntry"])
        lb.tab("new")
        self.assertTrue(wait_until(lambda: lb.view()["url"] == "about:blank" and lb.active == "T2", 5))
        req = self.sent()[0]
        self.assertTrue(req["request"]["host"])
        self.assertEqual(req["helpers"], sorted(browser_live.HELPERS))
        lb.remove_viewer()
        self.assertTrue(wait_until(lambda: "Page.stopScreencast" in self.methods(), 5))

    def test_turn_mode_needs_take_over(self):
        self.app.browsers.before_turn(SID)
        lb = self.open()
        self.assertEqual((lb.view()["mode"], lb.view()["holder"], lb.view()["can_input"]), ("turn", "agent", False))
        with self.assertRaises(BrowserError):
            lb.input([{"t": "tap", "x": 0.1, "y": 0.1}])
        lb.control("user")
        self.assertTrue(wait_until(lambda: {"snaplab": "control", "holder": "user"} in self.sent(), 5))
        self.assertEqual(lb.input([{"t": "tap", "x": 0.1, "y": 0.1}]), 1)
        lb.control("agent")
        with self.assertRaises(BrowserError):
            lb.input([{"t": "text", "text": "x"}])
        self.app.browsers.after_turn(SID)
        self.assertFalse(self.app.browsers.turn_running(SID))

    def test_phone_layout_only_while_the_user_holds_it(self):
        self.app.browsers.before_turn(SID)
        lb = self.app.browsers.get(SID)
        lb.add_viewer((390, 520))
        self.assertTrue(wait_until(lambda: lb.view()["status"] == "ready", 10))
        self.assertNotIn("Emulation.setDeviceMetricsOverride", self.methods())      # the agent's turn: its own size
        lb.control("user")
        self.assertTrue(wait_until(lambda: "Emulation.setDeviceMetricsOverride" in self.methods(), 5))
        override = [m for m in self.sent() if m.get("method") == "Emulation.setDeviceMetricsOverride"][-1]["params"]
        self.assertEqual((override["width"], override["height"], override["mobile"]), (390, 520, True))
        lb.control("agent")
        self.assertTrue(wait_until(lambda: "Emulation.clearDeviceMetricsOverride" in self.methods(), 5))
        lb.remove_viewer((390, 520))

    def test_closing_a_host_browser_lets_it_save(self):
        lb = self.open()
        lb.end(wait=True)
        self.assertIn("Browser.close", self.methods())

    def test_input_is_checked(self):
        lb = self.open()
        for bad in ([{"t": "tap", "x": "a", "y": 0}], [{"t": "eval"}], [{"t": "text", "text": ""}], "tap", [{}] * 201):
            with self.assertRaises(BrowserError):
                lb.input(bad)
        with self.assertRaises(BrowserError):
            lb.navigate("go", "javascript:alert(1)")
        with self.assertRaises(BrowserError):
            lb.tab("show", "nope")

    def test_page_url(self):
        self.assertEqual(page_url("example.com"), "https://example.com")
        self.assertEqual(page_url("http://localhost:3000/x"), "http://localhost:3000/x")
        self.assertEqual(page_url("about:blank"), "about:blank")
        for bad in ("", "file:///etc/passwd", "data:text/html,x", "chrome://settings", "a\nb"):
            with self.assertRaises(BrowserError):
                page_url(bad)

    def test_only_workspace_sessions_have_a_browser(self):
        self.app.store.save_session({"id": "s-0000abce", "project": "p-00000001", "harness": "bot", "title": "x"})
        self.assertFalse(self.app.browsers.available("s-0000abce")[0])
        self.assertFalse(self.app.browsers.available("s-ffffffff")[0])
        with self.assertRaises(BrowserError):
            self.app.browsers.get("s-0000abce")

    def test_ssh_command_without_forward(self):
        self.p2.stop()
        try:
            cfg = {"ssh_exe": "ssh", "ssh_port": 22, "user": "snaplab", "host": "127.0.0.1"}
            self.assertNotIn("-R", remote.ssh_command(cfg, None, None))
            self.assertIn("-R", remote.ssh_command(cfg, 1, 2))
        finally:
            self.p2.start()


class BrowserHTTPTest(Base):
    def setUp(self):
        super().setUp()
        self.server = make_server(self.app, "127.0.0.1", 0)
        self.port = self.server.server_port
        self.app.cfg["listen_port"] = self.port
        self.app.public_url = f"http://127.0.0.1:{self.port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.app.store.update_settings(onboarded=True)
        token, _ = self.app.auth.new_pairing("test", 1)
        status, h, _ = self.request("/api/pair", "POST", {"token": token})
        self.cookie = h["Set-Cookie"].split(";", 1)[0]

    def request(self, path, method="GET", body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=6)
        h = {"Content-Type": "application/json", "X-SnapLab": "1", **(headers or {})}
        conn.request(method, path, json.dumps(body).encode() if body is not None else None, h)
        r = conn.getresponse()
        out = r.status, dict(r.getheaders()), r.read()
        conn.close()
        return out

    def test_stream_state_and_actions(self):
        status, _, body = self.request(f"/api/browser/{SID}", headers={"Cookie": self.cookie})
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["available"])
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("GET", f"/api/browser/{SID}/live", headers={"Cookie": self.cookie})
        r = conn.getresponse()
        self.assertEqual(r.getheader("Content-Type"), "text/event-stream; charset=utf-8")
        got, deadline = b"", time.time() + 10
        while b'"jpeg":"BBBB"' not in got and time.time() < deadline:
            got += r.fp.readline()
        self.assertIn(b"event: state", got)
        self.assertIn(b'event: frame\ndata: {"seq":', got)
        self.assertIn(b'"w":1000,"h":500,"jpeg":"BBBB"', got)
        status, _, body = self.request(f"/api/browser/{SID}", "POST", {"action": "input", "events": [{"t": "text", "text": "secret-typed"}]},
                                       {"Cookie": self.cookie, "Origin": self.app.public_url})
        self.assertEqual((status, json.loads(body)["done"]), (200, 1))
        conn.close()
        # Typed text never reaches SnapLab's logs, events or archive.
        data = Path(self.app.cfg["data_dir"])
        for f in data.rglob("*"):
            if f.is_file() and f.suffix in (".json", ".jsonl", ".log", ".txt", ".db"):
                self.assertNotIn(b"secret-typed", f.read_bytes(), f)

    def test_gates(self):
        self.assertEqual(self.request(f"/api/browser/{SID}")[0], 401)
        self.assertEqual(self.request(f"/api/browser/{SID}/live")[0], 401)
        self.assertEqual(self.request(f"/api/browser/{SID}", "POST", {"action": "close"},
                                      {"Cookie": self.cookie, "Origin": "https://evil.example"})[0], 403)
        status, _, body = self.request(f"/api/browser/{SID}", "POST", {"action": "input", "events": []},
                                       {"Cookie": self.cookie, "Origin": self.app.public_url})
        self.assertEqual(status, 400)
        self.assertEqual(self.request("/api/browser/s-ffffffff/live", headers={"Cookie": self.cookie})[0], 404)


if __name__ == "__main__":
    unittest.main()
