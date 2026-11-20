"""The web server's gates and routes, and the Codex translator against a fake upstream."""
import json
import socket
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from helpers import OWNER, make_app, wait_until

from snaplab import auth, server
from snaplab.privacy import Redactor
from snaplab.proxy import Translator
from snaplab.slots import Slots


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class ServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app, cls.llm, _ = make_app(lambda m, t, e: {"content": "Hello from the bot."})
        cls.port = free_port()
        cls.app.public_url = f"http://127.0.0.1:{cls.port}"
        cls.srv = server.make_server(cls.app, "127.0.0.1", cls.port)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        token, _ = cls.app.auth.new_pairing("test phone")
        cls.cookie = None
        r = cls.req("POST", "/api/pair", {"token": token}, cookie=False)
        cls.cookie = r[2].get("Set-Cookie", "").split(";")[0]

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    @classmethod
    def req(cls, method, path, body=None, *, user=OWNER, cookie=True, xhdr=True):
        headers = {}
        if user:
            headers["Tailscale-User-Login"] = user
        if cookie and cls.cookie:
            headers["Cookie"] = cls.cookie
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
            if xhdr:
                headers["X-SnapLab"] = "1"
        rq = urllib.request.Request(f"http://127.0.0.1:{cls.port}{path}", data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(rq, timeout=10) as r:
                raw = r.read()
                return r.status, raw, dict(r.headers)
        except urllib.error.HTTPError as e:
            return e.code, e.read(), dict(e.headers)

    def test_gates(self):
        self.assertEqual(self.req("GET", "/api/bootstrap", user=None)[0], 403)
        self.assertEqual(self.req("GET", "/api/bootstrap", user="someone@else.com")[0], 403)
        self.assertEqual(self.req("GET", "/api/bootstrap", cookie=False)[0], 401)
        self.assertEqual(self.req("GET", "/", cookie=False)[0], 401)
        self.assertEqual(self.req("GET", "/manifest.webmanifest", cookie=False)[0], 200)
        self.assertEqual(self.req("GET", "/icons/icon-192.png", cookie=False)[0], 200)
        self.assertEqual(self.req("GET", "/app.js", cookie=False)[0], 401)
        self.assertTrue(self.cookie.startswith(auth.COOKIE_NAME + "="))
        st, body, hdr = self.req("GET", "/api/bootstrap")
        self.assertEqual(st, 200)
        self.assertIn("default-src 'self'", hdr["Content-Security-Policy"])
        self.assertEqual(self.req("POST", "/api/settings", {"lang": "ja"}, xhdr=False)[0], 403)
        self.assertEqual(self.req("GET", "/../../config.json")[0], 404)
        self.assertEqual(self.req("GET", "/healthz", user=None, cookie=False)[0], 200)

    def test_onboarding_chat_and_session_routes(self):
        st, body, _ = self.req("POST", "/api/rooms/r-00000000/send", {"text": "hi"})
        self.assertEqual(st, 400)   # setup not finished yet
        st, body, _ = self.req("POST", "/api/onboarding/preview", {"lang": "ja", "name": "ミナト", "tone": "polite", "call_user": "Alex"})
        self.assertEqual(st, 200)
        self.assertIn("ミナト", json.loads(body)["prompt"])
        st, body, _ = self.req("POST", "/api/onboarding", {"lang": "en", "name": "Marlin", "color": "#2F6FED", "tone": "friendly", "call_user": "Sam"})
        self.assertEqual(st, 200, body)
        room = json.loads(body)["room"]
        boot = json.loads(self.req("GET", "/api/bootstrap")[1])
        self.assertTrue(boot["settings"]["onboarded"])
        self.assertEqual(boot["bots"][0]["name"], "Marlin")
        self.assertEqual({h["id"] for h in boot["harnesses"]}, {"codex", "bot", "tests"})
        self.assertTrue(boot["push"]["public_key"])
        self.assertEqual(self.req("POST", f"/api/rooms/{room}/send", {"text": "hello"})[0], 200)
        ok = wait_until(lambda: any(m.get("text") == "Hello from the bot." for m in json.loads(self.req("GET", f"/api/rooms/{room}/messages")[1])["messages"]))
        self.assertTrue(ok)
        msgs = json.loads(self.req("GET", f"/api/rooms/{room}/messages")[1])["messages"]
        bot_msg = [m for m in msgs if m["role"] == "bot" and m.get("context")][-1]
        st, body, _ = self.req("GET", f"/api/messages/{bot_msg['id']}/view")
        self.assertEqual(st, 200)
        view = json.loads(body)["view"]
        self.assertIn("persona", [x["name"] for x in view["sections"]])
        self.assertTrue(any("hello" in x["text"] for x in view["history"]))
        self.assertEqual(view["report"]["max_tokens"], 32000)
        self.assertEqual(self.req("GET", "/api/messages/m-00000000/view")[0], 404)
        st, body, _ = self.req("POST", "/api/projects", {"name": "Tiny tool", "description": "x"})
        self.assertEqual(st, 200)
        pid = json.loads(body)["project"]["id"]
        st, body, _ = self.req("POST", "/api/sessions", {"project": pid, "harness": "bot", "prompt": "write notes"})
        self.assertEqual(st, 200, body)
        sid = json.loads(body)["session"]["id"]
        self.assertTrue(wait_until(lambda: json.loads(self.req("GET", f"/api/sessions/{sid}")[1])["session"]["status"] in ("review", "waiting")))
        st, body, _ = self.req("POST", f"/api/sessions/{sid}/reviewed", {})
        if json.loads(self.req("GET", f"/api/sessions/{sid}")[1])["session"]["status"] != "waiting":
            self.assertEqual(json.loads(body)["session"]["status"], "idle")
        st, body, _ = self.req("POST", "/api/sessions", {"project": pid, "harness": "nope", "prompt": "x"})
        self.assertEqual(st, 400)
        st, body, _ = self.req("POST", "/api/bots", {"name": "Coral", "role": "researcher"})
        self.assertEqual(st, 200)
        st, body, _ = self.req("POST", "/api/rooms", {"name": "crew", "bots": [b["id"] for b in self.app.store.bots()]})
        self.assertEqual(st, 200)
        st, body, _ = self.req("POST", "/api/settings", {"notify": {"review": False, "nonsense": True}})
        self.assertEqual(json.loads(body)["settings"]["notify"]["review"], False)
        self.assertNotIn("nonsense", json.loads(body)["settings"]["notify"])

    def test_static_files_have_no_private_values(self):
        for path in ("/", "/app.js", "/app.css", "/i18n.js", "/sw.js", "/manifest.webmanifest", "/offline.html"):
            st, body, _ = self.req("GET", path)
            self.assertEqual(st, 200, path)
            self.assertNotIn(b"__SNAPLAB_VERSION__", body)


class FakeUpstream(BaseHTTPRequestHandler):
    seen: list = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n))
        FakeUpstream.seen.append({"auth": self.headers.get("Authorization"), "body": body})
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        chunks = [
            {"choices": [{"delta": {"reasoning_content": "thinking"}}]},
            {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "c1", "function": {"name": "shell", "arguments": '{"command":"dir C:\\\\Users\\\\snapuser\\\\proj"}'}}]}}]},
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}], "usage": {"prompt_tokens": 11, "completion_tokens": 7}},
        ]
        for c in chunks:
            self.wfile.write(f"data: {json.dumps(c)}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")


class TranslatorTest(unittest.TestCase):
    def test_translates_filters_and_checks_token(self):
        up_port = free_port()
        up = ThreadingHTTPServer(("127.0.0.1", up_port), FakeUpstream)
        threading.Thread(target=up.serve_forever, daemon=True).start()
        import tempfile
        port = free_port()
        usage = []
        red = Redactor(home=r"C:\Users\Kazuko", username="Kazuko", computer="PCNAME1")
        tr = Translator(port=port, api_cfg={"base_url": f"http://127.0.0.1:{up_port}", "model": "qwen3.8-27b"},
                        slots=Slots(tempfile.mkdtemp(), 3), key_fn=lambda: "real-key", token="local-token",
                        log_path=tempfile.mktemp(suffix=".log"), redactor=red, on_usage=lambda **k: usage.append(k))
        self.assertTrue(tr.start())
        try:
            body = {"model": "qwen3.8-27b", "instructions": "You are Codex.", "reasoning": {"effort": "medium"},
                    "input": [{"role": "user", "content": [{"type": "input_text", "text": r"cwd is C:\Users\Kazuko\proj"}]}],
                    "tools": [{"type": "function", "name": "shell", "parameters": {"type": "object"}}]}

            def post(token):
                rq = urllib.request.Request(f"http://127.0.0.1:{port}/v1/responses", data=json.dumps(body).encode(),
                                            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"}, method="POST")
                try:
                    with urllib.request.urlopen(rq, timeout=10) as r:
                        return r.status, r.read().decode()
                except urllib.error.HTTPError as e:
                    return e.code, e.read().decode()
            self.assertEqual(post("wrong")[0], 401)
            st, text = post("local-token")
            self.assertEqual(st, 200)
            sent = FakeUpstream.seen[-1]
            self.assertEqual(sent["auth"], "Bearer real-key")
            self.assertEqual(sent["body"]["chat_template_kwargs"], {"reasoning_effort": "medium"})
            dumped = json.dumps(sent["body"])
            self.assertNotIn("Kazuko", dumped)
            self.assertIn("snapuser", dumped)
            events = [json.loads(ln[5:]) for ln in text.splitlines() if ln.startswith("data:")]
            done = [e for e in events if e["type"] == "response.completed"][0]
            call = [o for o in done["response"]["output"] if o["type"] == "function_call"][0]
            self.assertIn("Kazuko", json.loads(call["arguments"])["command"])     # mapped back for the local tool
            self.assertEqual(done["response"]["usage"]["input_tokens"], 11)
            self.assertTrue(wait_until(lambda: usage and usage[-1]["ok"]))
        finally:
            tr.stop()
            up.shutdown()


if __name__ == "__main__":
    unittest.main()
