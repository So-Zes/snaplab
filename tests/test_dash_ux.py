"""Dashboard cards the user can close, detailed API usage, lighter start-up data and compressed responses."""
import datetime as dt
import gzip
import json
import socket
import threading
import unittest
import urllib.error
import urllib.request

from helpers import OWNER, make_app, onboard

from snaplab import server, usage
from snaplab.llm import Usage


class UsageRecordTest(unittest.TestCase):
    def setUp(self):
        self.app, _, _ = make_app()
        self.st = self.app.store

    def test_days_are_kept_and_each_call_has_use_model_hour_and_caller(self):
        u = self.st.usage()
        for i in range(45):                   # older than the 30 days that used to be kept
            u["days"][(dt.date.today() - dt.timedelta(days=60 - i)).isoformat()] = {"calls": 1}
        self.st.write_json("usage.json", u)
        self.st.record_usage(input_tokens=100, output_tokens=7, ok=True, source="chat", model="qwen", caller="r-00000001")
        self.st.record_usage(input_tokens=50, output_tokens=0, ok=False, source="codex", model="qwen", caller="s-00000002")
        u = self.st.usage()
        self.assertEqual(len(u["days"]), 46)
        day = u["days"][dt.date.today().isoformat()]
        self.assertEqual(day["by_source"], {"chat": 1, "codex": 1})          # older readers still get counts
        self.assertEqual(day["sources"]["codex"], {"calls": 1, "input_tokens": 50, "output_tokens": 0, "errors": 1})
        self.assertEqual(day["by_model"]["qwen"]["errors"], 1)
        detail = self.st.usage_detail(dt.date.today().isoformat())
        self.assertEqual(sum(h["calls"] for h in detail["hours"].values()), 2)
        self.assertEqual(detail["callers"]["r-00000001"]["input_tokens"], 100)
        self.assertEqual(self.st.usage_detail("../usage"), {})

    def test_the_turn_that_made_a_call_is_its_caller(self):
        from snaplab.qwen_log import caller_id
        seen = []
        rec = Usage(on_record=lambda **kw: seen.append(kw))
        token = caller_id.set("r-12345678")
        try:
            rec.record(input_tokens=1, output_tokens=1, ok=True, source="chat", model="m")
        finally:
            caller_id.reset(token)
        rec.record(input_tokens=1, output_tokens=1, ok=True, source="codex", model="m", caller="s-87654321")
        self.assertEqual([k["caller"] for k in seen], ["r-12345678", "s-87654321"])

    def test_history_and_a_period_broken_down_with_old_days_readable(self):
        app, st = self.app, self.st
        onboard(app)
        lead = st.bots()[0]
        room = st.dm_room(lead["id"])
        old = (dt.date.today() - dt.timedelta(days=3)).isoformat()
        u = st.usage()
        u["days"][old] = {"calls": 4, "input_tokens": 400, "output_tokens": 40, "by_source": {"chat": 3, "summary": 1}}
        u["days"]["not-a-day"] = {"calls": 9}
        st.write_json("usage.json", u)
        st.record_usage(input_tokens=10, output_tokens=2, ok=True, source="chat", model="qwen", caller=room["id"])
        st.record_usage(input_tokens=5, output_tokens=1, ok=True, source="summary", model="qwen", caller="")
        hist = usage.history(st)
        self.assertEqual([r["day"] for r in hist["days"]], [old, dt.date.today().isoformat()])
        self.assertEqual(hist["detail_since"], dt.date.today().isoformat())
        d = usage.detail(app, old, dt.date.today().isoformat())
        self.assertEqual(d["sum"]["calls"], 6)
        self.assertEqual(d["days"], 4)
        self.assertEqual(d["days_with_detail"], 1)
        chat = next(s for s in d["sources"] if s["id"] == "chat")
        self.assertEqual(chat["calls"], 4)
        self.assertTrue(chat["calls_only"])                                 # the old day kept only counts
        who = {c["id"]: c for c in d["callers"]}
        self.assertEqual(who[room["id"]]["bot"], lead["id"])
        self.assertEqual(who[room["id"]]["kind"], "room")
        self.assertEqual(who[""]["kind"], "other")
        self.assertEqual(sum(h["calls"] for h in d["hours"]), 2)
        with self.assertRaises(ValueError):
            usage.detail(app, "yesterday", "today")


class DashboardCardsTest(unittest.TestCase):
    def test_closed_cards_survive_bot_rewrites_and_can_come_back(self):
        app, _, _ = make_app()
        app.update_dashboard("set", [{"id": "todo", "type": "note", "title": "Today", "text": "Ship"}], None, by="bot")
        d = app.store.hide_dashboard_card("widget:todo", True)
        self.assertEqual(d["hidden"], ["widget:todo"])
        app.store.hide_dashboard_card("widget:notes", True)
        app.update_dashboard("upsert", [{"id": "todo", "type": "note", "title": "Today", "text": "Shipped"}], None, by="bot")
        self.assertEqual(app.store.dashboard()["hidden"], ["widget:todo", "widget:notes"])
        self.assertEqual(app.store.dashboard()["widgets"][0]["text"], "Shipped")
        self.assertEqual(app.store.hide_dashboard_card("widget:todo", False)["hidden"], ["widget:notes"])


class StartupDataTest(unittest.TestCase):
    def test_session_lists_leave_out_long_texts(self):
        app, _, _ = make_app()
        onboard(app)
        p = app.create_project(name="Calc")
        s = {"id": "s-0000000a", "project": p["id"], "harness": "bot", "title": "Add", "status": "review",
             "last_prompt": "x" * 4000, "last_message": "y" * 4000, "changed_files": [{"path": "a.py", "change": "added"}] * 3,
             "result": {"summary": "z" * 2000}, "tests": {"status": "passed", "passed": 3, "output": "ok\n" * 500}}
        app.store.save_session(s)
        boot = app.bootstrap()
        brief = next(x for x in boot["sessions"] if x["id"] == s["id"])
        for k in ("last_prompt", "last_message", "changed_files", "result"):
            self.assertNotIn(k, brief)
        self.assertEqual(brief["changed_count"], 3)
        self.assertEqual(brief["tests"], {"status": "passed", "passed": 3})
        self.assertEqual(brief["project_name"], "Calc")
        self.assertEqual(app.bootstrap()["sessions"], boot["sessions"])     # served again from the kept brief
        s["title"] = "Add numbers"
        app.store.save_session(s)
        self.assertEqual(next(x for x in app.bootstrap()["sessions"] if x["id"] == s["id"])["title"], "Add numbers")
        full = app.sessions.summary(app.store.session(s["id"]))
        self.assertEqual(len(full["last_message"]), 3000)                     # the session page still gets everything


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class ServerRoutesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app, _, _ = make_app()
        onboard(cls.app)
        cls.port = free_port()
        cls.app.public_url = f"http://127.0.0.1:{cls.port}"
        cls.srv = server.make_server(cls.app, "127.0.0.1", cls.port)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        token, _ = cls.app.auth.new_pairing("test phone")
        cls.cookie = None
        cls.cookie = cls.req("POST", "/api/pair", {"token": token})[2].get("Set-Cookie", "").split(";")[0]

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    @classmethod
    def req(cls, method, path, body=None, headers=None):
        h = {"Tailscale-User-Login": OWNER, **(headers or {})}
        if cls.cookie:
            h["Cookie"] = cls.cookie
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            h.update({"Content-Type": "application/json", "X-SnapLab": "1"})
        rq = urllib.request.Request(f"http://127.0.0.1:{cls.port}{path}", data=data, headers=h, method=method)
        try:
            with urllib.request.urlopen(rq, timeout=10) as r:
                return r.status, r.read(), dict(r.headers)
        except urllib.error.HTTPError as e:
            return e.code, e.read(), dict(e.headers)

    def test_hide_route_checks_the_card_name_and_tells_the_phones(self):
        self.assertEqual(self.req("POST", "/api/dashboard/hide", {"card": "<script>", "hidden": True})[0], 400)
        self.assertEqual(self.req("POST", "/api/dashboard/hide", {"card": "experiment:exp2", "hidden": True})[0], 400)
        st, body, _ = self.req("POST", "/api/dashboard/hide", {"card": "widget:todo-2", "hidden": True})
        self.assertEqual(st, 200)
        self.assertIn("widget:todo-2", json.loads(body)["dashboard"]["hidden"])
        boot = json.loads(self.req("GET", "/api/bootstrap")[1])
        self.assertIn("widget:todo-2", boot["dashboard"]["hidden"])
        self.req("POST", "/api/dashboard/hide", {"card": "widget:todo-2", "hidden": False})
        self.assertNotIn("widget:todo-2", self.app.store.dashboard()["hidden"])

    def test_usage_routes(self):
        self.app.store.record_usage(input_tokens=3, output_tokens=1, ok=True, source="chat", model="qwen", caller="")
        st, body, _ = self.req("GET", "/api/usage/history")
        self.assertEqual(st, 200)
        self.assertTrue(json.loads(body)["days"])
        today = dt.date.today().isoformat()
        st, body, _ = self.req("GET", f"/api/usage/detail?from={today}&to={today}")
        self.assertEqual(json.loads(body)["sum"]["calls"], self.app.store.usage()["days"][today]["calls"])
        self.assertEqual(self.req("GET", "/api/usage/detail?from=x&to=y")[0], 400)

    def test_text_is_compressed_and_versioned_files_are_kept(self):
        st, body, hdr = self.req("GET", "/api/bootstrap", headers={"Accept-Encoding": "gzip"})
        self.assertEqual(hdr.get("Content-Encoding"), "gzip")
        self.assertTrue(json.loads(gzip.decompress(body))["ok"])
        st, body, hdr = self.req("GET", "/api/bootstrap")
        self.assertNotIn("Content-Encoding", hdr)
        v = server.web_version()
        st, body, hdr = self.req("GET", f"/assets/{v}/app.js", headers={"Accept-Encoding": "gzip"})
        self.assertEqual(st, 200)
        self.assertIn("immutable", hdr["Cache-Control"])
        self.assertIn(b"SnapLab phone app", gzip.decompress(body))
        st, body, hdr = self.req("GET", "/assets/000000000000/app.js")
        self.assertEqual(hdr["Cache-Control"], "no-store")                 # an old version's address is not kept
        st, body, hdr = self.req("GET", "/app.js")
        self.assertEqual(hdr["Cache-Control"], "no-store")


if __name__ == "__main__":
    unittest.main()
