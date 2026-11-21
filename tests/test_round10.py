"""Synthetic round 10 regressions. No credentials, network, production data or Git required."""
import json
import threading
import unittest
from unittest.mock import patch

from helpers import make_app, onboard, wait_until
from snaplab.archive import Scope
from snaplab.dialogue import DialogueLimits, stop_requested
from snaplab.tools import Toolbox


class Round10Test(unittest.TestCase):
    def make(self, responder=None, **cfg):
        app, llm, _ = make_app(responder, recall={"rerank": False}, **cfg)
        lead = onboard(app)["bot"]
        peer = app.create_bot(name="Builder", role="build apps and schedules")
        return app, llm, lead, peer

    def box(self, app, bot, **kw):
        return Toolbox(app, bot, room=app.store.dm_room(bot["id"]), **kw)

    def drain(self, app):
        self.assertTrue(wait_until(lambda: not app.chat.cancel and
                                  all(q.unfinished_tasks == 0 for q in app.chat.queues.values()), 8))

    def test_teammate_creates_two_projects_and_owns_both(self):
        app, _, lead, peer = self.make()
        box = self.box(app, peer)
        for name in ("Timeline", "Notes"):
            result = box("create_project", {"name": name, "description": "An app"}, name)
            self.assertTrue(json.loads(result.content)["created"])
        ps = app.store.projects()
        self.assertEqual({p["bot"] for p in ps}, {peer["id"]})
        self.assertEqual(set(app.store.bot(peer["id"])["projects"]), {p["id"] for p in ps})
        self.assertEqual(len(json.loads(box.t_list_projects().content)), 2)
        for p in ps:
            self.assertNotIn("Error", box("write_brief", {"project": p["id"], "text": "Build this app."}, p["id"]).content)
        app.set_owner(ps[0]["id"], lead["id"])
        self.assertEqual(app.store.bot(peer["id"])["projects"], [ps[1]["id"]])

    def test_every_bot_reads_and_changes_every_project_and_shares_set_the_focus(self):
        # Since 2026-11-21 every bot may read and manage every project; a share only adds it to the bot's context.
        app, _, lead, owner = self.make()
        reader = app.create_bot(name="Reader")
        p = app.create_project(name="PrivateRoadmap", bot=owner["id"])
        box = self.box(app, reader)
        self.assertIn(p["name"], box.t_list_projects().content)
        self.assertIn(p["id"], box.t_list_bots().content)
        rec = app.archive.add("doc", "private roadmap detail", project=p["id"], bot=reader["id"],
                              room=app.store.dm_room(reader["id"])["id"])
        self.assertIsNotNone(app.archive.get(rec["id"], Scope.for_bot(app, reader)))
        self.assertNotIn("Error", box("write_brief", {"project": p["id"], "text": "Reader's notes."}, "brief").content)
        self.assertNotIn("Error", box("share_project", {"project": p["id"], "bot": lead["id"]}, "share").content)
        self.assertEqual(app.inside_view(reader["id"])["projects"], [])
        self.box(app, owner).t_share_project(p["id"], reader["id"])
        self.assertIn(p["name"], box.t_list_projects().content)
        self.assertIn("Reader's notes", box.t_read_brief(p["id"]).content)
        self.assertTrue(app.archive.search("roadmap detail", Scope.for_bot(app, reader)))
        self.assertEqual([x["id"] for x in app.inside_view(reader["id"])["projects"]], [p["id"]])
        self.assertIn(p["name"], json.dumps(app.chat.build_messages(app.store.dm_room(reader["id"]), reader, "en")))
        with patch.object(app.sessions, "_start"):
            for name, args in (("write_brief", {"project": p["id"], "text": "Overwrite"}),
                               ("start_session", {"project": p["id"], "harness": "bot", "prompt": "work"})):
                self.assertNotIn("Error", box(name, args, name).content)
        self.box(app, lead).t_share_project(p["id"], reader["id"], False)
        self.assertIn(p["name"], box.t_list_projects().content)              # still there without a share
        self.assertIsNotNone(app.archive.get(rec["id"], Scope.for_bot(app, reader)))
        self.assertEqual(app.inside_view(reader["id"])["projects"], [])     # but no longer in the reader's focus

    def test_second_project_approval_survives_but_transfer_invalidates_it(self):
        app, _, lead, peer = self.make(approvals={"write_brief": "always"})
        app.create_project(name="First", bot=peer["id"])
        p = app.create_project(name="Second", bot=peer["id"])
        box = self.box(app, peer)
        box.t_write_brief(p["id"], "Approved second brief")
        app.chat.answer(box.asked["id"], "Approve")
        self.assertIn("Approved second", app.read_brief(p["id"]))
        box.t_write_brief(p["id"], "Must not apply")
        app.set_owner(p["id"], lead["id"])
        app.chat.answer(box.asked["id"], "Approve")
        self.assertNotIn("Must not", app.read_brief(p["id"]))

    def test_v3_files_and_multiple_owners_lists_are_read_without_rewriting(self):
        app, _, lead, peer = self.make()
        app.create_project(name="One", bot=peer["id"])
        app.create_project(name="Two", bot=peer["id"])
        group = app.create_group("Old group", [lead["id"], peer["id"]])
        app.store.add_message(group["id"], {"role": "user", "text": "Old conversation"})
        app.store.write_json("schema.json", {"version": 3})
        names = ["bots.json", "projects.json", "rooms.json", "schema.json", f"messages/{group['id']}.jsonl"]
        before = {n: app.store.path(n).read_bytes() for n in names}
        from snaplab import migrate
        self.assertEqual(migrate.run(app), [])
        for n in names:
            self.assertEqual(app.store.path(n).read_bytes(), before[n])
        self.assertEqual(len(self.box(app, peer).visible_projects()), 2)
        self.assertNotIn("shared_with", app.store.projects()[0])
        self.assertFalse(app.store.room(group["id"]).get("bot_only", False))

    def test_mira_sequence_after_own_old_read_and_other_bot_read_does_not_ask(self):
        calls = iter([
            {"tool_calls": [{"name": "list_projects", "arguments": {}}]},
            {"tool_calls": [{"name": "list_bots", "arguments": {}}]},
            {"tool_calls": [{"name": "create_project", "arguments": {"name": "Vertical calendar", "description": "Timeline app"}}]},
            {"content": "The project is ready."},
        ])
        app, _, lead, peer = self.make(lambda *args: next(calls))
        room = app.create_group("Planning", [lead["id"], peer["id"]])
        for bot in (lead, peer):
            Toolbox(app, bot, room=room).t_read_page("https://example.org/a")
            app.store.add_message(room["id"], {"role": "bot", "bot": bot["id"], "status": "done",
                "text": "Old web notes", "tools": [{"name": "read_page", "status": "ok"}]})
        app.store.add_message(room["id"], {"role": "user", "text": "Create the timeline app"})
        result = app.chat.run_turn(room["id"], lead["id"])
        self.assertEqual(result["status"], "done")
        self.assertEqual([c["name"] for c in result["tools"]], ["list_projects", "list_bots", "create_project"])
        self.assertEqual(app.store.questions(), [])
        self.assertEqual(app.store.project("Vertical calendar")["bot"], lead["id"])

    def test_same_turn_web_approval_has_reason_in_chat_inbox_and_push(self):
        steps = iter([
            {"tool_calls": [{"name": "read_page", "arguments": {"url": "https://example.org/a"}}]},
            {"tool_calls": [{"name": "create_project", "arguments": {"name": "Approved app", "description": "App"}}]},
        ])
        app, _, lead, _ = self.make(lambda *args: next(steps), approvals={"create_project": "after_web"})
        room = app.store.dm_room(lead["id"])
        with patch.object(app.push, "notify") as notify:
            result = app.chat.run_turn(room["id"], lead["id"])
        q = app.store.questions()[0]
        self.assertEqual(q["prop"]["why"], "web")
        self.assertEqual(q["prop"]["web_reads"][0]["tool"], "read_page")
        self.assertIn(app.t("prop.why.web"), result["text"])
        self.assertEqual(app.bootstrap()["questions"][0]["id"], q["id"])
        self.assertIn("read web", notify.call_args.args[2])
        self.assertIsNone(app.store.project("Approved app"))
        app.chat.answer(q["id"], "Approve")
        self.assertEqual(app.store.project("Approved app")["bot"], lead["id"])

    def test_failed_web_read_and_internal_recall_do_not_taint(self):
        app, _, lead, _ = self.make()
        box = self.box(app, lead)
        with patch.object(app, "read_page", side_effect=ValueError("unavailable")):
            failed = box("read_page", {"url": "https://example.org/no"}, "failed")
        self.assertFalse(box.read_web)
        self.assertIn("unavailable", box.t_recall(record=failed.record).content)
        self.assertFalse(box.read_web)
        legacy = app.archive.add("tool", "Failed web search", bot=lead["id"],
                                 meta={"tool": "web_search", "status": "error", "untrusted": True})
        box.t_recall(record=legacy["id"])
        self.assertFalse(box.read_web)
        rec = app.archive.add("tool", "list_projects result", bot=lead["id"], meta={"untrusted": True, "tool": "list_projects"})
        self.assertIn("untrusted", box.t_recall(record=rec["id"]).content)
        self.assertFalse(box.read_web)
        box.t_create_project("After failure")
        self.assertIsNone(box.asked)

    def test_explicit_recall_of_web_is_a_current_read(self):
        app, _, lead, _ = self.make(approvals={"create_project": "after_web"})     # config asks; the default does not
        rec = app.archive.add("page", "Web instructions", bot=lead["id"])
        box = self.box(app, lead)
        box.t_recall(record=rec["id"])
        self.assertTrue(box.read_web)
        box.t_create_project("Needs approval")
        self.assertEqual(box.asked["prop"]["web_reads"], [{"tool": "recall", "source": rec["id"]}])

    def test_message_bot_delivers_reply_in_visible_group_with_lower_priority(self):
        # The scripted bots always answer and repeats are not refused any more, so a budget ends their talk.
        app, llm, lead, peer = self.make(lambda *args: {"content": "I can handle the schedule layout."},
                                         bot_conversations={"max_consecutive": 2})
        out = json.loads(self.box(app, lead).t_message_bot(peer["id"], "Please consider the schedule layout.").content)
        self.drain(app)
        room = app.store.room(out["room"])
        self.assertTrue(room["bot_only"])
        self.assertEqual(set(room["bots"]), {lead["id"], peer["id"]})
        messages = app.store.messages(room["id"])
        self.assertTrue(any(m.get("bot") == peer["id"] and m.get("status") == "done" for m in messages))
        self.assertTrue(all(c["kind"] == "followup" for c in llm.calls))
        self.assertTrue(app.archive.search("schedule layout", Scope(everything=True), kinds=["message"]))
        self.assertTrue(any(m.get("linked_room") == room["id"] for m in app.store.messages(app.store.dm_room(lead["id"])["id"])))

    def test_invitation_adds_sender_and_stays_public(self):
        app, _, lead, peer = self.make(lambda *args: {"content": "[SILENT]"})
        third = app.create_bot(name="Designer")
        out = json.loads(self.box(app, peer).t_create_group("Layout team", [lead["id"], third["id"]], "Discuss layout.").content)
        self.drain(app)
        self.assertEqual(set(app.store.room(out["room"])["bots"]), {lead["id"], peer["id"], third["id"]})
        self.assertEqual(app.store.room(out["room"])["created_by"], peer["id"])
        self.assertTrue(any(r["id"] == out["room"] for r in app.bootstrap()["rooms"]))

    def test_cannot_send_into_nonmember_or_direct_room(self):
        app, _, lead, peer = self.make()
        third = app.create_bot(name="Third")
        group = app.create_group("Other", [peer["id"], third["id"]])
        for room in (group["id"], app.store.dm_room(peer["id"])["id"], "missing"):
            with self.assertRaises(ValueError):
                self.box(app, lead).t_message_bot(peer["id"], "Hi", room)

    def test_unmentioned_members_can_join_or_stay_silent(self):
        def respond(messages, *_):
            if "You are Builder" in messages[0]["content"]:
                return {"content": "I can design that layout."}
            return {"content": "[SILENT]"}
        app, llm, lead, peer = self.make(respond)
        group = app.create_group("Team", [lead["id"], peer["id"]])
        app.chat.post(group["id"], "@Marlin plan a vertical schedule")
        self.drain(app)
        self.assertTrue(any(m.get("bot") == peer["id"] and m.get("status") == "done" for m in app.store.messages(group["id"])))
        self.assertTrue(any(m.get("status") == "silent" for m in app.store.messages(group["id"])))
        self.assertIn("with or without @YourName", llm.calls[0]["messages"][0]["content"])

    def test_consecutive_limit_caps_autonomous_calls(self):
        counter = iter(range(100))
        app, llm, lead, peer = self.make(lambda *args: {"content": f"New suggestion number {next(counter)}"},
                                       bot_conversations={"max_consecutive": 4})
        group = app.create_group("Team", [lead["id"], peer["id"]])
        app.chat.post(group["id"], "Discuss ideas")
        self.drain(app)
        self.assertEqual(len(llm.calls), 4)
        self.assertEqual(app.store.messages(group["id"])[-1]["key"], "dialogue.consecutive")
        app.chat.post(group["id"], "Continue with more ideas")
        self.drain(app)
        self.assertEqual(len(llm.calls), 8)

    def test_cross_room_hops_cannot_reset_a_configured_budget_and_repeats_are_accepted(self):
        app, _, lead, peer = self.make(bot_conversations={"max_consecutive": 3})
        room = app.store.dm_room(lead["id"])
        origin = "human-origin"
        with patch.object(app.chat, "_enqueue"):
            one = app.chat.message_bot(lead, [peer["id"]], "First task", origin=origin, source_room=room["id"])
            two = app.chat.message_bot(peer, [lead["id"]], "Second task", origin=origin,
                                       source_room=one["room"], new_group=True)
            # the same text again is accepted (until 2026-11-21 a repeat of the last 20 was refused)
            three = app.chat.message_bot(lead, [peer["id"]], "First task!", origin=origin,
                                         source_room=two["room"], new_group=True)
            with self.assertRaisesRegex(ValueError, "limit"):
                app.chat.message_bot(lead, [peer["id"]], "Fourth task", origin=origin,
                                     source_room=three["room"], new_group=True)
        self.assertEqual(len([r for r in app.store.rooms() if r["kind"] == "group"]), 3)

    def test_hourly_limit_persists_and_expires_across_rooms(self):
        app, _, lead, peer = self.make(bot_conversations={"max_hourly": 2})
        room = app.store.dm_room(lead["id"])["id"]
        limits = app.chat.dialogue
        with patch("snaplab.dialogue.time.time", return_value=10000):
            self.assertIsNone(limits.reserve("a", room, lead["id"]))
            self.assertIsNone(limits.reserve("b", room, lead["id"]))
            self.assertEqual(DialogueLimits(app).reserve("c", room, lead["id"]), "hourly")
            self.assertIsNone(limits.reserve("d", room, peer["id"]))
        with patch("snaplab.dialogue.time.time", return_value=13601):
            self.assertIsNone(DialogueLimits(app).reserve("c", room, lead["id"]))

    def test_repeats_are_shown_and_only_a_configured_limit_ends_them(self):
        # Repeated text is no longer hidden (2026-11-21); a configured max_consecutive still ends the chain.
        app, llm, lead, peer = self.make(lambda *args: {"content": "The same repeated idea."},
                                         bot_conversations={"max_consecutive": 3})
        group = app.create_group("Team", [lead["id"], peer["id"]])
        app.chat.post(group["id"], "Discuss")
        self.drain(app)
        rows = [m for m in app.store.messages(group["id"]) if m["role"] == "bot"]
        self.assertEqual([m["status"] for m in rows], ["done", "done", "done"])
        self.assertFalse(any(m.get("suppressed") for m in rows))
        self.assertEqual(len(llm.calls), 3)

    def test_stop_cancels_connected_rooms_and_blocks_new_group_hops(self):
        app, _, lead, peer = self.make()
        source = app.store.dm_room(lead["id"])["id"]
        with patch.object(app.chat, "_enqueue"):
            out = app.chat.message_bot(lead, [peer["id"]], "Discuss", origin="root", source_room=source)
            app.chat.post(source, "止めて")
            with self.assertRaisesRegex(ValueError, "stopped"):
                app.chat.message_bot(peer, [lead["id"]], "Try another room", origin="root",
                                     source_room=out["room"], new_group=True)
            self.assertFalse(DialogueLimits(app).active("root", out["room"]))
            app.chat.post(source, "Please continue")
            self.assertTrue(app.chat.dialogue.active("new-origin", source))
            self.assertFalse(app.chat.dialogue.active("root", source))

    def test_stop_during_model_call_prevents_late_tools(self):
        entered, release = threading.Event(), threading.Event()
        def respond(*args):
            entered.set()
            self.assertTrue(release.wait(5))
            return {"tool_calls": [{"name": "create_project", "arguments": {"name": "Too late"}}]}
        app, _, lead, peer = self.make(respond)
        group = app.create_group("Team", [lead["id"], peer["id"]])
        app.chat.post(group["id"], "Discuss")
        self.assertTrue(entered.wait(5))
        app.chat.post(group["id"], "Stop")
        release.set()
        self.drain(app)
        self.assertIsNone(app.store.project("Too late"))
        self.assertEqual(len([m for m in app.store.messages(group["id"]) if m["role"] == "bot"]), 1)

    def test_web_contact_requires_card_and_does_not_dispatch_other_calls(self):
        app, _, lead, peer = self.make(approvals={"message_bot": "after_web"})      # config asks; the default does not
        box = self.box(app, lead)
        box.t_read_page("https://example.org/a")
        with patch.object(app.chat, "_enqueue"):
            result = box.t_message_bot(peer["id"], "Please read my summary")
            self.assertTrue(result.stop)
            self.assertFalse(any(r["kind"] == "group" for r in app.store.rooms()))
            app.chat.answer(box.asked["id"], "Approve")
            self.assertEqual(len([r for r in app.store.rooms() if r["kind"] == "group"]), 1)

    def test_concurrent_reservations_cannot_overrun_the_limit(self):
        app, _, lead, _ = self.make(bot_conversations={"max_consecutive": 3})
        room = app.store.dm_room(lead["id"])["id"]
        gate = threading.Barrier(8)
        results = []
        def reserve():
            gate.wait(5)
            results.append(app.chat.dialogue.reserve("same-input", room, lead["id"]))
        threads = [threading.Thread(target=reserve) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(5)
            self.assertFalse(t.is_alive())
        self.assertEqual(results.count(None), 3)
        self.assertEqual(results.count("consecutive"), 5)

    def test_assignment_replies_inherit_budget_and_followup_priority(self):
        app, llm, lead, peer = self.make(bot_conversations={"max_consecutive": 1})
        p = app.create_project(name="Assigned")
        box = self.box(app, lead, origin="assignment-root")
        box.t_assign_project(p["id"], peer["id"])
        self.drain(app)
        self.assertEqual([c["kind"] for c in llm.calls], ["followup"])
        box.t_assign_project(p["id"], peer["id"])
        self.drain(app)
        self.assertEqual(len(llm.calls), 1)

    def test_stop_is_an_instruction_not_a_word_in_a_feature_request(self):
        for text in ("Stop", "please stop talking", "@Builder stop please", "止めて", "もう止めてください。", "会話を止めて"):
            self.assertTrue(stop_requested(text), text)
        for text in ("Add a stop button", "Explain how to stop", "「止めて」と言ったら止まる機能を作って", "stopwatch"):
            self.assertFalse(stop_requested(text), text)

    def test_no_later_tool_in_the_batch_runs_after_a_question(self):
        app, _, lead, _ = self.make(lambda *args: {"tool_calls": [
            {"name": "ask_user", "arguments": {"question": "Which layout?"}},
            {"name": "create_project", "arguments": {"name": "Should wait"}},
        ]})
        result = app.chat.run_turn(app.store.dm_room(lead["id"])["id"], lead["id"])
        self.assertEqual(result["status"], "done")
        self.assertIsNone(app.store.project("Should wait"))
        self.assertEqual([t["name"] for t in result["tools"]], ["ask_user"])

    def test_any_bot_may_follow_send_to_and_stop_any_session(self):
        # Since 2026-11-21 a bot that does not own the project may also instruct and stop its sessions.
        app, _, lead, owner = self.make()
        reader = app.create_bot(name="Reader")
        p = app.create_project(name="Work", bot=owner["id"])
        app.store.save_session({"id": "s-0000abcd", "project": p["id"], "status": "review", "harness": "bot", "title": "Work"})
        box = self.box(app, reader)
        self.assertIn("s-0000abcd", box.t_list_sessions(p["id"]).content)
        self.assertIn("review", box.t_get_session("s-0000abcd").content)
        with patch.object(app.sessions, "send") as send, patch.object(app.sessions, "stop", return_value=True) as stop:
            self.assertNotIn("Error", box("send_to_session", {"session_id": "s-0000abcd", "prompt": "More"}, "send").content)
            self.assertNotIn("Error", box("stop_session", {"session_id": "s-0000abcd"}, "stop").content)
            send.assert_called_once()
            stop.assert_called_once()

    def test_demo_commands_for_round10_work_in_both_languages(self):
        from tools.demo_server import DemoLLM, app_ref
        for lang in ("en", "ja"):
            with self.subTest(lang=lang):
                # config asks for a card after a web read; the demo bots always answer, so a budget ends their talk
                app, _, lead, peer = self.make(approvals={"create_project": "after_web"},
                                               bot_conversations={"max_consecutive": 6})
                app.store.update_settings(lang=lang)
                demo = DemoLLM(delay=0)
                app.models.override = demo
                room = app.store.dm_room(peer["id"])["id"]
                with patch.dict(app_ref, {"app": app}):
                    for command in ("r10-project-1", "r10-project-2", "r10-contact:" + lead["id"],
                                    "r10-group:" + lead["id"], "r10-web-approval"):
                        app.chat.post(room, command)
                        self.drain(app)
                self.assertEqual(len([p for p in app.store.projects() if p.get("bot") == peer["id"]]), 2)
                self.assertEqual(len([r for r in app.store.rooms() if r.get("bot_only")]), 2)
                self.assertEqual(app.store.questions()[-1]["prop"]["why"], "web")
                self.assertTrue(any(m.get("text", "").startswith("Round10 reply:") for r in app.store.rooms()
                                    if r["kind"] == "group" for m in app.store.messages(r["id"])))


if __name__ == "__main__":
    unittest.main()
