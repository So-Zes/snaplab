"""Deleting a bot, and starting a chat afresh so a bot keeps only what matters now (2026-11-16)."""
import json
import tempfile
import unittest

from helpers import fake_credentials, make_app, onboard
from snaplab.tools import Toolbox


class BotManageTest(unittest.TestCase):
    def setUp(self):
        self.enterContext(fake_credentials())
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.app, self.llm, _ = make_app(tmp=self.tmp.name, summary={"enabled": False}, recall={"rerank": False})
        self.addCleanup(self.app.archive.close)
        self.lead = onboard(self.app)["bot"]
        self.wi = self.app.create_bot(name="Mika", role="3D")
        self.dm = self.app.store.dm_room(self.wi["id"])

    def say(self, room, role, text, **extra):
        return self.app.store.add_message(room, {"role": role, "text": text, **extra})

    def history(self, room=None, bot=None):
        room = self.app.store.room(room or self.dm["id"])
        messages, report = self.app.chat.context.build(room, bot or self.app.store.bot(self.wi["id"]), "en")
        return "\n".join(m["content"] for m in messages[1:]), messages[0]["content"], report

    # ------------------------------------------------------------ deleting a bot

    def test_deleting_a_bot_takes_it_off_the_team_and_keeps_its_record(self):
        group = self.app.create_group("Team", [self.lead["id"], self.wi["id"]])
        alone = self.app.create_group("Mika alone", [self.wi["id"]])
        project = self.app.create_project(name="Mont", description="3D", bot=self.wi["id"])
        self.app.tasks.create(bot_id=self.wi["id"], title="look", prompt="look", schedule={"kind": "once", "in_minutes": 5},
                              room_id=self.dm["id"])
        self.app.memory_write(self.wi["id"], "the castle uses 4k textures")
        self.say(group["id"], "bot", "hello from Mika", bot=self.wi["id"])
        with self.assertRaises(ValueError):
            self.app.delete_bot(self.wi["id"])                      # not confirmed
        with self.assertRaises(ValueError):
            self.app.delete_bot(self.lead["id"], confirmed=True)    # the lead stays
        out = self.app.delete_bot(self.wi["id"], confirmed=True)
        self.assertEqual(out["deleted_tasks"], 1)
        st = self.app.store
        self.assertNotIn(self.wi["id"], [b["id"] for b in st.bots()])
        self.assertIsNone(st.bot(self.wi["id"]))
        self.assertIsNone(st.bot_by_name("Mika"))
        self.assertEqual([b["name"] for b in st.former_bots()], ["Mika"])
        self.assertIsNone(st.room(self.dm["id"]), "its own chat is hidden")
        self.assertIsNone(st.room(alone["id"]), "a group it was alone in is hidden")
        self.assertEqual(st.room(group["id"])["bots"], [self.lead["id"]])
        self.assertEqual(st.project(project["id"]).get("bot"), self.lead["id"], "the project goes back to the lead")
        self.assertEqual(self.app.tasks.for_bot(self.wi["id"]), [])
        self.assertIn("4k textures", st.memory(self.wi["id"]), "its memory stays")
        self.assertIn("hello from Mika", [m["text"] for m in st.messages(group["id"])])
        self.assertEqual([b["name"] for b in self.app.bootstrap()["former_bots"]], ["Mika"])
        self.app.create_bot(name="Mika", role="again")                # the name is free again
        with self.assertRaises(LookupError):
            self.app.delete_bot(self.wi["id"], confirmed=True)

    def test_deleting_a_bot_stops_its_running_work(self):
        stopped = []
        self.app.sessions.stop = stopped.append
        self.app.store.save_session({"id": "s-0000000a", "started_by": self.wi["id"], "status": "running", "project": "p"})
        self.app.store.save_session({"id": "s-0000000b", "started_by": self.wi["id"], "status": "done", "project": "p"})
        self.app.store.save_session({"id": "s-0000000c", "started_by": self.lead["id"], "status": "running", "project": "p"})
        self.assertEqual(self.app.delete_bot(self.wi["id"], confirmed=True)["stopped_sessions"], 1)
        self.assertEqual(stopped, ["s-0000000a"])

    # ------------------------------------------------------------ archiving a bot (2026-11-17)

    def test_archiving_a_bot_puts_it_away_and_restoring_brings_everything_back(self):
        st = self.app.store
        group = self.app.create_group("Team", [self.lead["id"], self.wi["id"]])
        alone = self.app.create_group("Mika alone", [self.wi["id"]])
        other = self.app.create_bot(name="Rox", role="code")
        project = self.app.create_project(name="Mont", description="3D", bot=self.wi["id"])
        shared = self.app.create_project(name="Shared", description="x", bot=other["id"])
        p = st.project(shared["id"]); p["shared_with"] = [self.wi["id"]]; st.save_project(p)
        self.app.update_room(self.dm["id"], {"pinned": True})
        daily = self.app.tasks.create(bot_id=self.wi["id"], title="daily", prompt="look",
                                      schedule={"kind": "daily", "time": "09:00"}, room_id=self.dm["id"])
        soon = self.app.tasks.create(bot_id=self.wi["id"], title="soon", prompt="look",
                                     schedule={"kind": "once", "in_minutes": 60}, room_id=self.dm["id"])
        already = self.app.tasks.create(bot_id=self.wi["id"], title="paused", prompt="look",
                                        schedule={"kind": "daily", "time": "10:00"}, room_id=self.dm["id"])
        self.app.tasks.change(already["id"], "pause")
        self.app.memory_write(self.wi["id"], "the castle uses 4k textures")
        self.say(group["id"], "bot", "hello from Mika", bot=self.wi["id"])
        order = [b["id"] for b in st.bots()]
        with self.assertRaises(ValueError):
            self.app.archive_bot(self.wi["id"])                      # not confirmed
        with self.assertRaises(ValueError):
            self.app.archive_bot(self.lead["id"], confirmed=True)    # the lead stays
        out = self.app.archive_bot(self.wi["id"], confirmed=True)
        self.assertEqual(out["paused_tasks"], 2)
        self.assertIsNone(st.bot(self.wi["id"]))
        self.assertEqual([b["name"] for b in st.archived_bots()], ["Mika"])
        self.assertEqual([b["name"] for b in self.app.bootstrap()["archived_bots"]], ["Mika"])
        self.assertIsNone(st.room(self.dm["id"]), "its own chat is hidden")
        self.assertIsNone(st.room(alone["id"]))
        self.assertEqual(st.room(group["id"])["bots"], [self.lead["id"]])
        self.assertEqual(st.project(project["id"]).get("bot"), self.lead["id"])
        self.assertEqual(st.project(shared["id"])["shared_with"], [])
        self.assertEqual({t["id"]: t["status"] for t in self.app.tasks.for_bot(self.wi["id"])},
                         {daily["id"]: "paused", soon["id"]: "paused", already["id"]: "paused"}, "nothing is deleted")
        self.assertIn("4k textures", st.memory(self.wi["id"]))
        self.assertEqual(st.former_bots(), [], "archiving is not deleting")

        back = self.app.restore_bot(self.wi["id"])
        self.assertEqual(back["resumed_tasks"], 2)
        self.assertEqual([b["id"] for b in st.bots()], order, "it comes back at its old place")
        self.assertEqual(st.archived_bots(), [])
        self.assertTrue(st.room(self.dm["id"])["pinned"], "its chat comes back, pinned as before")
        self.assertIsNotNone(st.room(alone["id"]))
        self.assertEqual(st.room(group["id"])["bots"], [self.lead["id"], self.wi["id"]])
        self.assertEqual(st.project(project["id"]).get("bot"), self.wi["id"])
        self.assertIn(project["id"], st.bot(self.wi["id"])["projects"])
        self.assertEqual(st.project(shared["id"])["shared_with"], [self.wi["id"]])
        status = {t["id"]: t["status"] for t in self.app.tasks.for_bot(self.wi["id"])}
        self.assertEqual(status, {daily["id"]: "active", soon["id"]: "active", already["id"]: "paused"},
                         "only what archiving paused starts again")
        self.assertIn("hello from Mika", [m["text"] for m in st.messages(group["id"])])
        with self.assertRaises(LookupError):
            self.app.restore_bot(self.wi["id"])

    def test_restoring_leaves_a_missed_one_time_wake_up_paused_and_keeps_names_unique(self):
        st = self.app.store
        task = self.app.tasks.create(bot_id=self.wi["id"], title="wake", prompt="look",
                                     schedule={"kind": "once", "in_minutes": 5}, room_id=self.dm["id"])
        self.app.archive_bot(self.wi["id"], confirmed=True)
        tasks = self.app.tasks.all()
        for t in tasks:
            if t["id"] == task["id"]:
                t["schedule"]["at"] = "2026-01-01T09:00:00+09:00"
        self.app.tasks._save_all(tasks)
        twin = self.app.create_bot(name="Mika", role="new")             # the name is free while it is archived
        with self.assertRaises(ValueError):
            self.app.restore_bot(self.wi["id"])
        self.app.update_bot(twin["id"], {"name": "Mika 2"})
        self.assertEqual(self.app.restore_bot(self.wi["id"])["resumed_tasks"], 0)
        self.assertEqual([t["status"] for t in self.app.tasks.for_bot(self.wi["id"])], ["paused"])
        self.assertEqual(st.bot(self.wi["id"])["name"], "Mika")

    def test_archiving_stops_running_work(self):
        stopped = []
        self.app.sessions.stop = stopped.append
        self.app.store.save_session({"id": "s-0000000a", "started_by": self.wi["id"], "status": "running", "project": "p"})
        self.app.store.save_session({"id": "s-0000000b", "started_by": self.wi["id"], "status": "done", "project": "p"})
        self.assertEqual(self.app.archive_bot(self.wi["id"], confirmed=True)["stopped_sessions"], 1)
        self.assertEqual(stopped, ["s-0000000a"])
        self.assertEqual(self.app.restore_bot(self.wi["id"])["bot"]["name"], "Mika")

    # ------------------------------------------------------------ starting a chat afresh

    def test_a_fresh_start_hides_earlier_messages_and_keeps_the_note(self):
        self.say(self.dm["id"], "user", "round 3 used the old castle")
        self.say(self.dm["id"], "bot", "round 3 failed on the worker", bot=self.wi["id"])
        rec = self.app.start_fresh(self.dm["id"], bot_id=self.wi["id"], note="Round 7 is current; use WSL.")
        self.say(self.dm["id"], "user", "now polish the sea")
        text, system, report = self.history()
        self.assertNotIn("round 3", text)
        self.assertIn("now polish the sea", text)
        self.assertIn("Round 7 is current; use WSL.", system)
        self.assertIn("fresh", [s["name"] for s in report["sections"]])
        self.assertEqual(self.app.store.bot(self.wi["id"])["context_since"], rec["at"])
        notes = [m for m in self.app.store.messages(self.dm["id"]) if m.get("key") == "srv.fresh_start"]
        self.assertEqual(len(notes), 1, "the chat shows where it started afresh")
        # Nothing is deleted, and undo brings the earlier messages back.
        self.assertIn("round 3 used the old castle", [m["text"] for m in self.app.store.messages(self.dm["id"])])
        self.app.start_fresh(self.dm["id"], undo=True)
        text, system, _ = self.history()
        self.assertIn("round 3 used the old castle", text)
        self.assertNotIn("Round 7 is current", system)
        self.assertNotIn("context_since", self.app.store.bot(self.wi["id"]))
        with self.assertRaises(ValueError):
            self.app.start_fresh(self.dm["id"], undo=True)

    def test_the_room_summary_starts_again_and_comes_back_on_undo(self):
        folder = self.app.summaries.folder(self.dm["id"])
        folder.mkdir(parents=True, exist_ok=True)
        old = self.say(self.dm["id"], "user", "an old message")
        self.app.store.write_json(folder / "summary.json", {"through": old["id"], "updated": "x"})
        (folder / "SUMMARY.md").write_text("OLD SUMMARY about round 3\n", encoding="utf-8")
        _, system, _ = self.history()
        self.assertIn("OLD SUMMARY", system)
        self.app.start_fresh(self.dm["id"], bot_id=self.wi["id"])
        _, system, _ = self.history()
        self.assertNotIn("OLD SUMMARY", system, "the summary from before is not given")
        self.assertEqual(self.app.summaries.pending(self.dm["id"]), [], "the next summary covers only new messages")
        new = self.say(self.dm["id"], "user", "a new message")
        self.assertEqual([m["id"] for m in self.app.summaries.pending(self.dm["id"])], [new["id"]])
        self.app.start_fresh(self.dm["id"], undo=True)
        _, system, _ = self.history()
        self.assertIn("OLD SUMMARY", system)

    def test_finished_work_from_before_is_left_out_but_live_work_stays(self):
        st = self.app.store
        project = self.app.create_project(name="Mont", description="3D", bot=self.wi["id"])
        st.save_session({"id": "s-000000d1", "project": project["id"], "started_by": self.wi["id"], "status": "failed",
                         "title": "OLD ROUND", "harness": "pi"})
        st.save_session({"id": "s-000000d2", "project": project["id"], "started_by": self.wi["id"], "status": "running",
                         "title": "LIVE ROUND", "harness": "pi"})
        text, _, _ = self.history()
        self.assertIn("OLD ROUND", text)
        self.app.start_fresh(self.dm["id"], bot_id=self.wi["id"])
        text, _, _ = self.history()
        self.assertNotIn("OLD ROUND", text)
        self.assertIn("LIVE ROUND", text)
        ledger = json.dumps(self.app.ledger.concise(st.bot(self.wi["id"])), ensure_ascii=False)
        self.assertNotIn("s-000000d1", ledger)
        self.assertIn("s-000000d2", ledger)

    def test_the_bot_can_start_afresh_itself_when_asked(self):
        self.say(self.dm["id"], "user", "forget everything before round 7")
        box = Toolbox(self.app, self.app.store.bot(self.wi["id"]), room=self.app.store.room(self.dm["id"]))
        self.assertIn("start_fresh", {t["function"]["name"] for t in box.schemas()})
        res = box("start_fresh", {"keep": "Round 7 on WSL; sea needs Gerstner waves."}, "c1")
        self.assertIn("started_fresh", res.content)
        room = self.app.store.room(self.dm["id"])
        self.assertEqual(room["fresh_start"]["by"], self.wi["id"])
        self.assertEqual(room["fresh_start"]["note"], "Round 7 on WSL; sea needs Gerstner waves.")
        text, system, _ = self.history()
        self.assertNotIn("forget everything before round 7", text)
        self.assertIn("Gerstner", system)

    def test_a_group_fresh_start_from_one_bot_marks_only_that_bot(self):
        group = self.app.create_group("Team", [self.lead["id"], self.wi["id"]])
        self.app.start_fresh(group["id"], bot_id=self.wi["id"])
        self.assertIn("context_since", self.app.store.bot(self.wi["id"]))
        self.assertNotIn("context_since", self.app.store.bot(self.lead["id"]))
        with self.assertRaises(ValueError):
            self.app.start_fresh(self.dm["id"], bot_id=self.lead["id"])

    def test_the_inside_view_says_whether_the_chat_was_started_afresh(self):
        self.app.start_fresh(self.dm["id"], bot_id=self.wi["id"], note="keep this")
        view = self.app.inside_view(self.wi["id"])["view"]
        self.assertEqual(view["fresh_start"]["note"], "keep this")
        self.assertEqual(view["room"], self.dm["id"])
        self.assertEqual(view["room_kind"], "dm")


if __name__ == "__main__":
    unittest.main()
