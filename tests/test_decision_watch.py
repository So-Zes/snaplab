"""Experimental decision watch (2026-11-22): a decision model looks at work without progress and wakes a bot.
All model answers here are synthetic; the real model was tried by hand (a page wait of 41 minutes: 0.99, a Rust
build quiet for 10 minutes: 0.28)."""
import io
import json
import time
import unittest
from unittest.mock import patch

from helpers import make_app, onboard
from snaplab import decision_watch
from snaplab.decision_watch import DecisionWatch


def iso(seconds_ago: float) -> str:
    import datetime as dt
    return dt.datetime.fromtimestamp(time.time() - seconds_ago).astimezone().isoformat(timespec="seconds")


class WatchTest(unittest.TestCase):
    def setUp(self):
        self.app, _, _ = make_app()
        r = onboard(self.app)
        self.lead, self.room = r["bot"], r["room"]
        self.asked = []
        self.answer = 0.9

        def decide(state):
            self.asked.append(state)
            return self.answer, 0.00001
        self.watch = DecisionWatch(self.app, decide=decide)
        self.queued = []
        patch.object(self.app.chat, "_enqueue", side_effect=lambda *a: self.queued.append(a)).start()
        self.addCleanup(patch.stopall)

    def session(self, quiet_s: float, sid="s-0000abcd"):
        self.app.store.save_session({"id": sid, "status": "running", "bot": self.lead["id"], "room": self.room["id"],
                                     "title": "Build the game", "harness": "pi", "turns": 1, "created": iso(3600),
                                     "started": iso(3600)})
        self.app.store.session_event(sid, {"kind": "command", "text": "npm install", "exit_code": "0", "ts": iso(quiet_s + 60)})
        self.app.store.session_event(sid, {"kind": "start", "text": "node .snaplab/browser.mjs open http://127.0.0.1:5173",
                                           "ts": iso(quiet_s)})
        return sid

    def test_off_by_default_and_switched_in_settings(self):
        self.assertFalse(self.watch.enabled())
        self.assertFalse(self.watch.view()["enabled"])
        with patch.object(DecisionWatch, "start") as start:
            v = self.watch.set_enabled(True)
        start.assert_called_once()
        self.assertTrue(v["enabled"])
        self.assertTrue(self.app.store.settings()["decision_watch"])
        self.watch.set_enabled(False)
        self.assertFalse(self.watch.enabled())

    def test_a_quiet_session_is_asked_about_and_its_bot_woken(self):
        sid = self.session(quiet_s=1500)
        done = self.watch.check()
        self.assertEqual(done, [{"key": "s:" + sid, "p": 0.9, "woke": True}])
        self.assertIn("no progress for 25.0 minutes", self.asked[0])
        self.assertIn("start (", self.asked[0])
        msgs = self.app.store.messages(self.room["id"])
        note = [m for m in msgs if m.get("key") == "srv.watch.wake"]
        self.assertEqual(len(note), 1)
        self.assertIn(sid, note[0]["text"])
        self.assertIn("[SILENT]", note[0]["text"])
        self.assertEqual(self.queued[0][:2], (self.room["id"], self.lead["id"]))
        rows = self.app.store.read_jsonl(self.app.store.path("decision-watch.jsonl"))
        self.assertTrue(rows[-1]["woke"])
        # not asked again about the same work right away
        self.assertEqual(self.watch.check(), [])
        self.assertEqual(len(self.asked), 1)

    def test_recent_progress_is_not_asked_about_and_a_low_answer_wakes_nobody(self):
        self.session(quiet_s=60)
        self.assertEqual(self.watch.check(), [])
        self.assertEqual(self.asked, [])
        self.answer = 0.2
        self.session(quiet_s=1500, sid="s-0000abce")
        done = self.watch.check()
        self.assertEqual(done[0]["woke"], False)
        self.assertEqual(self.queued, [])

    def test_a_stuck_reply_wakes_the_lead_bot_elsewhere_or_notifies_the_user(self):
        helper = self.app.create_bot(name="Helper", role="helper")
        msg = {"id": "m-0000feed", "room": self.room["id"], "bot": helper["id"], "ts": iso(2000), "text": "Looking",
               "thinking": "", "tools": [{"name": "read_page", "status": "running", "label": "https://example.org"}]}
        self.app.chat.live[msg["id"]] = msg
        t0 = time.time()
        self.watch.check(now=t0)                     # first sight: progress starts now
        self.assertEqual(self.asked, [])
        self.watch.check(now=t0 + 900)               # unchanged for 15 minutes
        self.assertEqual(len(self.asked), 1)
        self.assertIn("read_page [running]", self.asked[0])
        dm = self.app.store.dm_room(self.lead["id"])
        self.assertEqual(self.queued[-1][:2], (dm["id"], self.lead["id"]))
        # the lead's own reply is stuck and there is no other lead: the user is told
        lead_msg = dict(msg, id="m-0000beef", bot=self.lead["id"])
        self.app.chat.live = {lead_msg["id"]: lead_msg}
        with patch.object(self.app.push, "notify") as notify:
            self.watch.check(now=t0 + 1000)
            self.watch.check(now=t0 + 2000)
        notify.assert_called_once()

    def test_what_is_sent_is_redacted(self):
        self.app.redactor.add_term("secret-owner-name")
        self.app.store.save_session({"id": "s-0000abcf", "status": "running", "bot": self.lead["id"], "room": self.room["id"],
                                     "title": "Report for secret-owner-name", "started": iso(3000)})
        self.app.store.session_event("s-0000abcf", {"kind": "start", "text": "cat notes", "ts": iso(2000)})
        self.watch.check()
        self.assertNotIn("secret-owner-name", self.asked[0])

    def test_errors_are_recorded_not_raised(self):
        self.session(quiet_s=1500)
        w = DecisionWatch(self.app, decide=lambda s: (_ for _ in ()).throw(ValueError("OPENROUTER_API_KEY is not set")))
        self.assertEqual(w.check(), [])
        self.assertIn("OPENROUTER_API_KEY", w.stats["last_error"])


class ModelCallTest(unittest.TestCase):
    def test_the_decisions_endpoint_is_called_and_read(self):
        app, _, _ = make_app()
        w = DecisionWatch(app)
        app.store.update_settings(decision_watch=True)
        sent = {}

        class Resp(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                self.close()

        def fake_open(req, **kw):
            sent["url"], sent["body"], sent["auth"] = req.full_url, json.loads(req.data), req.get_header("Authorization")
            return Resp(json.dumps({"answers": {"stuck": {"noul": 0.73}}, "usage": {"cost": 0.000014}}).encode())
        with patch("snaplab.llm.get_api_key", return_value="sk-or-synthetic"), \
                patch("snaplab.provider_protocol.open_request", side_effect=fake_open):
            p, cost = w.call_model("state text")
        self.assertEqual((p, cost), (0.73, 0.000014))
        self.assertEqual(sent["url"], decision_watch.DEFAULTS["url"])
        self.assertEqual(sent["body"]["model"], "liquid/d1.2-ultra")
        self.assertEqual(sent["body"]["questions"]["stuck"]["type"], "noul")
        self.assertEqual(sent["auth"], "Bearer sk-or-synthetic")
        with patch("snaplab.llm.get_api_key", return_value=None), self.assertRaises(ValueError):
            w.call_model("state text")


if __name__ == "__main__":
    unittest.main()
