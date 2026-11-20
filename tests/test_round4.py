"""Round 4: server texts from the language files, proposal cards, result reports (F10), memory (F14), paths on the
phone (V-4), names and initials."""
import json
import re
import unittest
from pathlib import Path

from helpers import ROOT, make_app, onboard, wait_until

from snaplab import i18n
from snaplab.harnesses.base import Harness, TurnResult


class PathHarness(Harness):
    """Reports a command the way Codex does: wrapped in powershell, with full paths in the output."""
    id = "pathy"
    label = {"en": "Paths"}
    description = {"en": "test"}
    runs_code = True
    sandboxed = True

    def run_turn(self, session, folder, prompt, ctl, first):
        ps = '"C:\\\\WINDOWS\\\\System32\\\\WindowsPowerShell\\\\v1.0\\\\powershell.exe" -Command "Get-ChildItem -Recurse"'
        ctl.emit({"kind": "command", "text": ps, "exit_code": 0, "output": f"{folder}\\calc.py\n{Path.home()}\\notes.txt"})
        return TurnResult(ok=True, last_message=f"Wrote {folder}\\calc.py")


class ServerTextsTest(unittest.TestCase):
    def test_every_key_the_server_uses_exists_in_every_language(self):
        used = set()
        for f in (ROOT / "snaplab").rglob("*.py"):
            text = f.read_text(encoding="utf-8")
            used |= set(re.findall(r'"((?:srv|act|note|prop|push|ev|err\.code)\.[a-z0-9_.]*[a-z0-9_])"', text))
        used |= {f"act.session.{s}" for s in ("review", "waiting", "failed", "stopped")}
        used |= {f"err.code.{c}" for c in ("cut", "restart", "busy", "server", "timeout", "unreachable", "nokey", "key",
                                            "denied", "config", "too_long", "rejected", "internal")}
        used |= {f"srv.tone.{t}" for t in ("friendly", "polite", "concise", "playful")}
        used = {k for k in used if not k.endswith(".") and not k.startswith(("push.apple", "push.services", "push.json"))}
        for code in ("en", "ja"):
            missing = sorted(k for k in used if not i18n.has(code, k))
            self.assertEqual(missing, [], f"{code}.js lacks server keys")

    def test_stored_lines_carry_their_key(self):
        self.assertEqual(i18n.text("ja", "act.bot_added", who="@srv.you", name="Coral"), "あなたが Bot「Coral」を足しました")
        self.assertEqual(i18n.text("en", "act.bot_added", who="@srv.you", name="Coral"), "You added the bot Coral")
        app, _, _ = make_app()
        onboard(app, lang="ja", name="ミナト")
        app.create_bot(name="Coral", role="r")
        rec = [a for a in app.store.recent_activity(10) if a.get("key") == "act.bot_added"][0]
        self.assertEqual(rec["vars"], {"who": "@srv.you", "name": "Coral"})
        self.assertEqual(rec["text"], "あなたが Bot「Coral」を足しました")
        msgs = app.store.messages(app.store.rooms()[0]["id"])
        self.assertIn("はじめまして", msgs[0]["text"])
        self.assertIn("さん", msgs[0]["text"])                  # the polite form from the language file (QA U-2)
        self.assertIn("呼び方：Sam", app.store.user_note())

    def test_approval_in_any_language_and_note_with_key(self):
        from test_core import script
        app, _, _ = make_app(script({"tool_calls": [{"name": "start_session", "arguments": {"project": "Demo", "harness": "bot", "prompt": "notes"}}]},
                                    {"content": "Waiting."}))
        room = onboard(app, lang="ja")["room"]["id"]
        app.create_project(name="Demo", description="d")
        app.chat.post(room, "start")
        self.assertTrue(wait_until(lambda: app.store.questions()))
        q = app.store.questions()[0]
        self.assertEqual(q["options"], ["Approve", "Decline"])
        self.assertTrue(wait_until(lambda: not app.chat.busy(room)))
        app.chat.answer(q["id"], "許可する")
        self.assertEqual(len(app.store.sessions()), 1)
        note = app.store.messages(room)[-1]
        self.assertEqual(note["key"], "note.session_started")
        self.assertIn("許可しました", note["text"])
        self.assertFalse(any(m["role"] == "user" and m.get("text") == "許可する" for m in app.store.messages(room)))

    def test_failed_reply_has_a_preview_for_the_chat_list(self):
        app, _, _ = make_app(lambda m, t, e: {"content": "", "error_status": 503, "error_text": "x"})
        room = onboard(app)["room"]["id"]
        app.chat.post(room, "hi")
        self.assertTrue(wait_until(lambda: app.store.messages(room)[-1].get("status") == "error"))
        self.assertTrue(wait_until(lambda: "model server" in (app.store.room(room).get("last_text") or "")))
        act = [a for a in app.store.recent_activity(5) if a.get("key") == "act.reply_failed"][0]
        self.assertEqual(act["vars"]["reason"], "@err.code.server")


class ResultReportTest(unittest.TestCase):
    def test_bot_reports_the_result_of_its_session(self):
        from test_core import script
        app, llm, _ = make_app(script(
            {"tool_calls": [{"name": "start_session", "arguments": {"project": "Demo", "harness": "bot", "prompt": "write notes"}}]},
            {"tool_calls": [{"name": "write_file", "arguments": {"path": "notes/a.md", "content": "one\n"}}]},
            {"content": "Wrote notes/a.md."},
            {"content": "Done: the note is written (1 file). Want me to merge it?"}))
        room = onboard(app)["room"]["id"]
        app.create_project(name="Demo", description="d")
        app.chat.post(room, "please")
        self.assertTrue(wait_until(lambda: app.store.questions()))
        self.assertTrue(wait_until(lambda: not app.chat.busy(room)))
        app.chat.answer(app.store.questions()[0]["id"], "Approve")
        self.assertTrue(wait_until(lambda: app.store.messages(room)[-1].get("text", "").startswith("Done: the note"), 10))
        msgs = app.store.messages(room)
        card = [m for m in msgs if m.get("result")][-1]
        self.assertEqual(card["project_name"], "Demo")
        last_call = llm.calls[-1]["messages"]
        self.assertIn("[Result card]", last_call[-1]["content"])
        self.assertIn("Result card", last_call[0]["content"])        # the house rule about reporting


class MemoryTest(unittest.TestCase):
    def test_remember_forget_and_notes_about_the_user(self):
        from test_core import script
        app, llm, _ = make_app(script(
            {"tool_calls": [{"name": "write_memory", "arguments": {"text": "Prefers short answers"}}]},
            {"tool_calls": [{"name": "write_memory", "arguments": {"text": "Is not a morning person", "about": "user"}}]},
            {"tool_calls": [{"name": "write_memory", "arguments": {"text": "short answers", "mode": "forget"}}]},
            {"content": "ok"}))
        room = onboard(app)["room"]["id"]
        orch = app.store.orchestrator()
        app.chat.post(room, "remember things")
        self.assertTrue(wait_until(lambda: not app.chat.busy(room) and len(llm.calls) >= 4, 8))
        self.assertNotIn("short answers", app.store.memory(orch["id"]))
        self.assertIn("Is not a morning person", app.store.user_note())
        self.assertIn("Wants to be called Sam", app.store.user_note())
        daily = list((Path(app.cfg["data_dir"]) / "bots" / orch["id"] / "daily").glob("*.md"))
        self.assertTrue(daily)
        log = daily[0].read_text(encoding="utf-8")
        self.assertIn("forget", log)
        self.assertIn("Prefers short answers", log)            # what was forgotten is still in the daily log
        # every bot sees the notes about the user
        coral = app.create_bot(name="Coral", role="r")
        messages, report = app.chat.context.build(app.store.dm_room(coral["id"]), app.store.bot(coral["id"]), "en")
        self.assertIn("Is not a morning person", messages[0]["content"])
        self.assertGreater(report["user"], 0)

    def test_teammates_cannot_write_about_the_user_and_full_memory_is_refused(self):
        from snaplab.memory import MemoryFull
        from snaplab.tools import Toolbox
        app, _, _ = make_app(memory_chars=120)
        onboard(app)
        coral = app.create_bot(name="Coral", role="r")
        box = Toolbox(app, app.store.bot(coral["id"]), room=app.store.dm_room(coral["id"]), lang="en")
        res = box("write_memory", {"text": "x", "about": "user"}, "c1")
        self.assertIn("only the lead bot", res.content)
        app.memory_write(coral["id"], "a" * 80)
        with self.assertRaises(MemoryFull):
            app.memory_write(coral["id"], "b" * 80)
        res = box("write_memory", {"text": "c" * 80}, "c2")
        self.assertIn("full", res.content)


class DisplayTest(unittest.TestCase):
    def test_paths_and_shell_wrappers_are_shortened_for_the_phone(self):
        app, _, _ = make_app(harness_plugins=["test_round4:PathHarness"])
        onboard(app)
        p = app.create_project(name="Paths", description="d")
        s = app.sessions.create(p, "pathy", "go")
        self.assertTrue(wait_until(lambda: app.store.session(s["id"])["status"] == "review", 5))
        ev = [e for e in app.store.session_events(s["id"]) if e["kind"] == "command"][0]
        self.assertEqual(ev["text"], "Get-ChildItem -Recurse")
        self.assertNotIn(str(Path.home()), ev["output"])
        self.assertIn("calc.py", ev["output"])
        self.assertIn("~", ev["output"])
        view = app.sessions.summary(app.store.session(s["id"]), events=20)
        self.assertNotIn(str(Path.home()), json.dumps(view, ensure_ascii=False).replace("\\\\", "\\"))

    def test_initials_and_titles(self):
        from snaplab.app import initials_of
        from snaplab.sessions import short_title
        self.assertEqual(initials_of("Marlin"), "M")
        self.assertEqual(initials_of("Sea Otter"), "SO")
        self.assertEqual(initials_of("ミナト"), "ミ")
        self.assertEqual(short_title("Create calc.py with add(a, b) and test_calc.py with one unittest test for it"),
                         "Create calc.py with add(a, b) and test_calc.py…")


if __name__ == "__main__":
    unittest.main()
