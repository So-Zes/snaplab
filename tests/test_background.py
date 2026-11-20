"""Room summaries and the nightly memory tidy: they call the model only behind chats, and as often as config says."""
import datetime as dt
import tempfile
import threading
import time
import unittest
from pathlib import Path

from helpers import make_app, onboard, wait_until

from snaplab.slots import Slots, SlotsBusy


def responder(messages, tools, effort):
    sys = messages[0]["content"]
    if "running summary" in sys:
        return {"content": "- Sam wants a tiny calculator.\n- Marlin will start a session for it."}
    if "tidy the long-term memory" in sys:
        return {"content": "- [2026-11-01] Prefers short answers.\n- [2026-11-01] Works in Python."}
    return {"content": "ok"}


class SlotsOrderTest(unittest.TestCase):
    def test_background_is_capped_and_waits_behind_chat(self):
        d = Path(tempfile.mkdtemp())
        s = Slots(d, 3, session_max=2, background_max=1)
        with s.acquire(kind="background"):
            with self.assertRaises(SlotsBusy):
                with s.acquire(kind="background", wait=False):
                    pass
            with s.acquire(kind="chat", wait=False):
                pass                                      # a chat still gets in
        one = Slots(Path(tempfile.mkdtemp()), 1)
        order = []
        with one.acquire(kind="chat"):
            def take(kind):
                with one.acquire(kind=kind, poll_s=0.02):
                    order.append(kind)
                    time.sleep(0.05)
            tb = threading.Thread(target=take, args=("background",))
            tb.start()
            time.sleep(0.1)                               # the background job asked first...
            tc = threading.Thread(target=take, args=("chat",))
            tc.start()
            time.sleep(0.1)
        tb.join(3)
        tc.join(3)
        self.assertEqual(order, ["chat", "background"])  # ...but the chat goes first


class RoomSummaryTest(unittest.TestCase):
    def test_summary_after_n_messages_and_context_uses_it(self):
        app, llm, _ = make_app(responder, summary={"every_messages": 6, "min_recent": 2})
        room = onboard(app)["room"]["id"]
        for i in range(3):
            app.chat.post(room, f"message {i}")
            self.assertTrue(wait_until(lambda: not app.chat.busy(room) and app.chat.queues[room].empty()))
            time.sleep(0.05)
        self.assertTrue(wait_until(lambda: (app.store.path("rooms", room, "SUMMARY.md")).exists(), 5))
        text, meta = app.summaries.get(room)
        self.assertIn("tiny calculator", text)
        self.assertTrue(meta["through"])
        bg = [c for c in llm.calls if c["kind"] == "background"]
        self.assertEqual(len(bg), 1)
        self.assertEqual(bg[0]["source"], "summary")
        self.assertEqual(bg[0]["tools"], [])
        msgs, report = app.chat.context.build(app.store.room(room), app.store.orchestrator(), "en")
        self.assertIn("Earlier in this room", msgs[0]["content"])
        self.assertGreater(report["summary"], 0)
        self.assertLessEqual(report["history_messages"], 3)   # what came after the summary (+ a few)

    def test_frequency_is_a_setting(self):
        app, llm, _ = make_app(responder, summary={"enabled": False, "every_messages": 1})
        room = onboard(app)["room"]["id"]
        app.chat.post(room, "hello")
        self.assertTrue(wait_until(lambda: not app.chat.busy(room) and app.chat.queues[room].empty()))
        time.sleep(0.3)
        self.assertFalse(app.store.path("rooms", room, "SUMMARY.md").exists())
        self.assertFalse(app.summaries.due(room))
        self.assertTrue(app.summaries.maybe_schedule(room, force=True))     # "Update now" still works
        self.assertTrue(wait_until(lambda: app.store.path("rooms", room, "SUMMARY.md").exists(), 5))


class MemoryTidyTest(unittest.TestCase):
    def test_tidy_rewrites_within_the_limit_and_keeps_the_old_text(self):
        app, llm, _ = make_app(responder, memory_tidy={"min_chars": 10})
        onboard(app)
        orch = app.store.orchestrator()
        for t in ("Prefers short answers", "Likes short answers", "Works in Python", "Prefers short answers"):
            app.memory_write(orch["id"], t)
        self.assertEqual(app.tidy.tidy(orch["id"]), "tidied")
        mem = app.store.memory(orch["id"])
        self.assertEqual(mem.count("short answers"), 1)
        daily = next((Path(app.cfg["data_dir"]) / "bots" / orch["id"] / "daily").glob("*.md")).read_text(encoding="utf-8")
        self.assertIn("before the nightly tidy", daily)
        self.assertIn("Likes short answers", daily)
        self.assertEqual(llm.calls[-1]["kind"], "background")
        self.assertEqual(app.tidy.tidy(orch["id"]), "skipped")              # nothing changed since

    def test_unusable_result_is_thrown_away(self):
        app, _, _ = make_app(lambda m, t, e: {"content": "Sure! Here are your notes: " + "x" * 5000},
                             memory_tidy={"min_chars": 10})
        onboard(app)
        orch = app.store.orchestrator()
        app.memory_write(orch["id"], "Prefers short answers")
        before = app.store.memory(orch["id"])
        self.assertEqual(app.tidy.tidy(orch["id"], force=True), "kept")
        self.assertEqual(app.store.memory(orch["id"]), before)

    def test_schedule(self):
        app, _, _ = make_app(memory_tidy={"hour": 3, "every_days": 1})
        self.assertFalse(app.tidy.is_due(dt.datetime(2026, 11, 2, 2, 59)))
        self.assertTrue(app.tidy.is_due(dt.datetime(2026, 11, 2, 3, 0)))
        app.tidy._save_state(last_run_day="2026-11-02")
        self.assertFalse(app.tidy.is_due(dt.datetime(2026, 11, 2, 23, 0)))
        self.assertTrue(app.tidy.is_due(dt.datetime(2026, 11, 3, 4, 0)))
        app2, _, _ = make_app(memory_tidy={"enabled": False})
        self.assertFalse(app2.tidy.is_due(dt.datetime(2026, 11, 3, 4, 0)))


if __name__ == "__main__":
    unittest.main()


class HousekeeperTest(unittest.TestCase):
    def test_nothing_runs_before_first_setup(self):
        from snaplab.background import Housekeeper
        app, _, _ = make_app(responder)
        calls = []
        app.tidy.is_due = lambda: True
        app.tidy.run_all = lambda: calls.append(1)
        hk = Housekeeper(app, every_s=0.05)
        hk.start()
        try:
            time.sleep(0.3)
            self.assertEqual(calls, [])
            self.assertFalse((app.store.dir / "housekeeping.json").exists())
            onboard(app)
            self.assertTrue(wait_until(lambda: calls, 3))
        finally:
            hk.stop.set()
