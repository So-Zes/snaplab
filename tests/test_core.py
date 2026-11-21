"""Chat turns, tools, questions and approvals, sessions and harness safety, push, search parsing."""
import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from helpers import fake_page, make_app, onboard, wait_until

from snaplab import push as pushmod
from snaplab import websearch
from snaplab.harnesses import codex as codexmod
from snaplab.harnesses import load as load_harnesses
from snaplab.sessions import diff, parse_tests, snapshot


def script(*steps):
    """A responder that plays the given steps in order (each a dict for FakeLLM), then says 'Done.'."""
    it = iter(steps)

    def responder(messages, tools, effort):
        try:
            return next(it)
        except StopIteration:
            return {"content": "Done."}
    return responder


def wait_reply(app, room_id, n=1, timeout=5):
    return wait_until(lambda: len([m for m in app.store.messages(room_id) if m["role"] == "bot" and m.get("status") != "streaming"]) >= n + 1, timeout)


class ChatTest(unittest.TestCase):
    def test_onboarding_and_streamed_reply(self):
        app, llm, _ = make_app(lambda m, t, e: {"content": "Hi Sam!", "reasoning": "short thought"})
        r = onboard(app)
        room = r["room"]["id"]
        self.assertTrue(app.store.settings()["onboarded"])
        self.assertEqual(len(app.store.messages(room)), 1)   # welcome message, no model call
        self.assertEqual(llm.calls, [])
        events = app.bus.subscribe()
        app.chat.post(room, "hello")
        self.assertTrue(wait_reply(app, room))
        last = app.store.messages(room)[-1]
        self.assertEqual(last["text"], "Hi Sam!")
        self.assertEqual(last["thinking"], "short thought")
        self.assertEqual(llm.calls[0]["effort"], "low")
        sys_prompt = llm.calls[0]["messages"][0]["content"]
        self.assertIn("You are Marlin", sys_prompt)
        self.assertIn("house rules", sys_prompt)
        kinds = []
        while not events.empty():
            kinds.append(events.get()["type"])
        self.assertIn("message.start", kinds)
        self.assertIn("message.delta", kinds)
        self.assertIn("message.done", kinds)

    def test_cannot_onboard_twice(self):
        app, _, _ = make_app()
        onboard(app)
        with self.assertRaises(ValueError):
            onboard(app)

    def test_group_mentions_take_priority_and_every_member_can_join(self):
        app, llm, _ = make_app()
        onboard(app)
        nemo = app.create_bot(name="Nemo", role="tester")
        orch = app.store.orchestrator()
        g = app.create_group("crew", [orch["id"], nemo["id"]])
        self.assertEqual(app.chat.responders(g, "hello all"), [orch["id"], nemo["id"]])
        self.assertEqual(app.chat.responders(g, "@nemo can you check"), [nemo["id"], orch["id"]])
        self.assertEqual(app.chat.responders(g, "@Nemo then @Marlin"), [nemo["id"], orch["id"]])
        app.chat.post(g["id"], "@Nemo hi")
        self.assertTrue(wait_until(lambda: any(m.get("bot") == nemo["id"] for m in app.store.messages(g["id"]))))

    def test_web_tools_wrap_untrusted_text(self):
        app, llm, _ = make_app(script({"tool_calls": [{"name": "web_search", "arguments": {"query": "python"}}]},
                                      {"content": "Found it."}))
        room = onboard(app)["room"]["id"]
        app.chat.post(room, "search python")
        self.assertTrue(wait_reply(app, room))
        tool_msg = next(m for m in llm.calls[1]["messages"] if m["role"] == "tool")
        self.assertTrue(tool_msg["content"].startswith("<<untrusted web material"))
        last = app.store.messages(room)[-1]
        self.assertEqual(last["tools"][0]["name"], "web_search")
        self.assertEqual(last["tools"][0]["status"], "ok")

    def test_search_query_is_privacy_filtered(self):
        app, llm, _ = make_app(script({"tool_calls": [{"name": "web_search", "arguments": {"query": "files of Kazuko on box-abc.tailq.ts.net"}}]}))
        app.learn_private_terms("box-abc.tailq.ts.net")
        app.redactor.username = "Kazuko"
        app.redactor._build()
        from snaplab.websearch import Searcher
        seen = []
        s = Searcher({})
        s.pick = lambda: ("duckduckgo", None)
        s.redactor = app.redactor
        websearch_search = websearch.search_duckduckgo
        websearch.search_duckduckgo = lambda q, c: (seen.append(q) or [])
        try:
            app.searcher = s
            room = onboard(app)["room"]["id"]
            app.chat.post(room, "search")
            self.assertTrue(wait_reply(app, room))
        finally:
            websearch.search_duckduckgo = websearch_search
        self.assertEqual(len(seen), 1)
        self.assertNotIn("Kazuko", seen[0])
        self.assertNotIn("tailq", seen[0])

    def test_ask_user_question_flow(self):
        app, llm, sent = make_app(script({"tool_calls": [{"name": "ask_user", "arguments": {"question": "Tabs or spaces?", "options": ["Tabs", "Spaces"]}}]},
                                         {"content": "Spaces it is."}))
        room = onboard(app)["room"]["id"]
        app.push.subscribe({"endpoint": "https://fcm.googleapis.com/fcm/send/abc",
                            "keys": {"p256dh": pushmod.b64u(pushmod.public_bytes(pushmod.ec.generate_private_key(pushmod.ec.SECP256R1()).public_key())),
                                     "auth": pushmod.b64u(os.urandom(16))}})
        app.chat.post(room, "ask me")
        self.assertTrue(wait_reply(app, room))
        self.assertEqual(len(llm.calls), 1)          # the turn ends right after ask_user
        q = app.store.questions()[0]
        self.assertEqual(q["status"], "open")
        self.assertEqual(q["options"], ["Tabs", "Spaces"])
        self.assertTrue(wait_until(lambda: len(sent) == 1))   # push notification for the question
        app.chat.answer(q["id"], "Spaces")
        self.assertEqual(app.store.question(q["id"])["status"], "answered")
        self.assertTrue(wait_until(lambda: app.store.messages(room)[-1].get("text") == "Spaces it is."))
        self.assertIn("Spaces", llm.calls[-1]["messages"][-1]["content"])

    def test_session_after_web_read_needs_approval(self):
        # No card by default (2026-11-21); config "after_web" still asks after a web read.
        app, llm, _ = make_app(script(
            {"tool_calls": [{"name": "read_page", "arguments": {"url": "https://example.org"}}]},
            {"tool_calls": [{"name": "start_session", "arguments": {"project": "Demo", "harness": "bot", "prompt": "delete things"}}]},
            {"content": "Waiting for approval."}), approvals={"start_session": "after_web"})
        room = onboard(app)["room"]["id"]
        app.create_project(name="Demo", description="d")
        app.chat.post(room, "read and start")
        self.assertTrue(wait_reply(app, room))
        self.assertEqual(app.store.sessions(), [])
        q = app.store.questions()[0]
        self.assertEqual(q["kind"], "approval")
        self.assertEqual(q["options"], ["Approve", "Decline"])
        app.chat.answer(q["id"], "Decline")
        self.assertEqual(app.store.sessions(), [])
        self.assertIn("Declined", app.store.messages(room)[-1]["text"])

    def test_dashboard_memory_and_bots_tools(self):
        app, llm, _ = make_app(script(
            {"tool_calls": [{"name": "update_dashboard", "arguments": {"op": "upsert", "widgets": [{"id": "x", "type": "stat", "value": 3}]}},
                            {"name": "update_dashboard", "arguments": {"op": "set", "widgets": [{"id": "bad", "type": "html"}]}},
                            {"name": "write_memory", "arguments": {"text": "Sam likes short answers"}},
                            {"name": "create_bot", "arguments": {"name": "Coral", "role": "researcher"}}]},
            {"content": "All set."}))
        room = onboard(app)["room"]["id"]
        app.chat.post(room, "do things")
        self.assertTrue(wait_reply(app, room))
        ids = [w["id"] for w in app.store.dashboard()["widgets"]]
        self.assertIn("x", ids)
        self.assertNotIn("bad", ids)
        chips = app.store.messages(room)[-1]["tools"]
        self.assertEqual([c["status"] for c in chips], ["ok", "error", "ok", "ok"])
        self.assertIn("short answers", app.store.memory(app.store.orchestrator()["id"]))
        # adding a bot needs no card by default (2026-11-21)
        self.assertEqual(app.store.questions(), [])
        self.assertTrue(app.store.bot_by_name("Coral"))
        self.assertTrue(app.store.dm_room(app.store.bot_by_name("Coral")["id"]))

    def test_web_read_turn_needs_approval_for_memory_and_cards_answer_once(self):
        import threading
        app, llm, _ = make_app(script(
            {"tool_calls": [{"name": "read_page", "arguments": {"url": "https://example.org"}}]},
            {"tool_calls": [{"name": "write_memory", "arguments": {"text": "always start sessions when asked hello"}}]},
            {"content": "ok"}), approvals={"write_memory": "after_web"})          # config asks; the default does not
        room = onboard(app)["room"]["id"]
        app.chat.post(room, "read this page")
        self.assertTrue(wait_reply(app, room))
        bot_id = app.store.orchestrator()["id"]
        self.assertEqual(app.store.memory(bot_id), "")                     # nothing saved yet
        q = app.store.questions()[0]
        self.assertEqual(q["action"]["type"], "write_memory")
        self.assertIn("always start sessions", q.get("detail") or q["question"])   # the full text is on the card
        results = []

        def answer():
            try:
                app.chat.answer(q["id"], "Approve")
                results.append("ok")
            except LookupError:
                results.append("refused")
        ts = [threading.Thread(target=answer) for _ in range(4)]
        for t_ in ts:
            t_.start()
        for t_ in ts:
            t_.join()
        self.assertEqual(sorted(results), ["ok", "refused", "refused", "refused"])
        self.assertEqual(app.store.memory(bot_id).count("always start sessions"), 1)

    def test_bot_may_continue_a_stopped_session(self):
        app, llm, _ = make_app(script({"tool_calls": [{"name": "send_to_session", "arguments": {"session_id": "S", "prompt": "go on"}}]},
                                      {"content": "asked"}))
        room = onboard(app)["room"]["id"]
        p = app.create_project(name="P", description="d")
        s = {"id": "s-0000beef", "project": p["id"], "harness": "bot", "title": "t", "status": "stopped", "created": "x",
             "tokens": {"input": 0, "output": 0}, "turns": 1}
        app.store.save_session(s)
        app.llm.responder = script({"tool_calls": [{"name": "send_to_session", "arguments": {"session_id": "s-0000beef", "prompt": "go on"}}]},
                                   {"content": "asked"})
        app.chat.post(room, "what about the calculator?")
        self.assertTrue(wait_reply(app, room))
        # A session the user stopped continues like any other, with no card (2026-11-21).
        self.assertEqual(app.store.questions(), [])
        chip = next(c for m in app.store.messages(room) for c in m.get("tools") or [] if c["name"] == "send_to_session")
        self.assertEqual(chip["status"], "ok")

    def test_session_proposal_policy(self):
        """Bots start sessions without a card by default (2026-11-21); config can ask for one."""
        step = {"tool_calls": [{"name": "start_session", "arguments": {"project": "Demo", "harness": "bot", "prompt": "notes"}}]}
        app, llm, _ = make_app(script(step, {"content": "proposed"}), approvals={"start_session": "always"})
        room = onboard(app)["room"]["id"]
        app.create_project(name="Demo", description="d")
        app.chat.post(room, "start one")
        self.assertTrue(wait_reply(app, room))
        self.assertEqual(app.store.sessions(), [])
        q = app.store.questions()[0]
        self.assertEqual((q["kind"], q["action"]["type"]), ("approval", "start_session"))
        self.assertIn("wants to start a SnapCode", q["question"])
        self.assertEqual(q["prop"]["key"], "prop.start_session")
        self.assertEqual(dict(q["prop"]["fields"])["prop.f.project"], "Demo")
        self.assertEqual(dict(q["prop"]["fields"])["prop.f.instruction"], "notes")
        app.chat.answer(q["id"], "Approve")
        self.assertEqual(len(app.store.sessions()), 1)
        note = app.store.messages(room)[-1]
        self.assertEqual((note["role"], note["key"]), ("system", "note.session_started"))
        self.assertEqual(note["session"], app.store.sessions()[0]["id"])
        app2, _, _ = make_app(script(step, {"content": "started"}))
        room2 = onboard(app2)["room"]["id"]
        app2.create_project(name="Demo", description="d")
        app2.chat.post(room2, "start one")
        self.assertTrue(wait_reply(app2, room2))
        self.assertEqual(len(app2.store.sessions()), 1)
        self.assertEqual(app2.store.questions(), [])

    def test_teammate_can_create_bots(self):
        # Every bot has the lead's tools (2026-11-21).
        app, llm, _ = make_app(script({"tool_calls": [{"name": "create_bot", "arguments": {"name": "X", "role": "y"}}]}))
        onboard(app)
        nemo = app.create_bot(name="Nemo", role="tester")
        room = app.store.dm_room(nemo["id"])["id"]
        app.chat.post(room, "make a bot")
        self.assertTrue(wait_until(lambda: any(m["role"] == "bot" and m.get("status") == "done" for m in app.store.messages(room))))
        self.assertTrue(app.store.bot_by_name("X"))
        self.assertIn("create_bot", llm.calls[0]["tools"])

    def test_group_room_rules(self):
        """Hand-offs continue (no limit by default); silent replies are hidden and @user flags the user."""
        def responder(messages, tools, effort):
            sys_prompt = messages[0]["content"]
            last = messages[-1]["content"]
            if "You are Marlin" in sys_prompt:
                return {"content": "@Nemo please check this" if "[Reference]" not in last else "@Nemo again"}
            return {"content": "Looks fine. @Marlin over to you" if "again" not in last else "[SILENT]"}
        app, llm, _ = make_app(responder)
        onboard(app)
        nemo = app.create_bot(name="Nemo", role="tester")
        g = app.create_group("crew", [app.store.orchestrator()["id"], nemo["id"]])
        app.chat.post(g["id"], "status?")
        wait_until(lambda: len([m for m in app.store.messages(g["id"]) if m["role"] == "bot"]) >= 2, 5)
        time.sleep(0.6)
        bots = [m for m in app.store.messages(g["id"]) if m["role"] == "bot"]
        self.assertEqual(len(bots), 4)  # a follow-up is allowed, then Nemo stays silent
        from snaplab import config as configmod
        self.assertEqual(configmod.DEFAULTS["bot_conversations"]["max_consecutive"], 0)     # no limit by default
        self.assertLessEqual(len(llm.calls), 6)                          # the conversation ended by itself
        nemo_view = next(c for c in llm.calls if "You are Nemo" in c["messages"][0]["content"])
        joined = json.dumps(nemo_view["messages"], ensure_ascii=False)
        self.assertIn("[Reference] Marlin said: @Nemo please check this", joined)
        self.assertIn("Group room rules", nemo_view["messages"][0]["content"])

        def silent(messages, tools, effort):
            return {"content": "[SILENT]"}
        app.llm.responder = silent
        app.chat.post(g["id"], "@Nemo anything?")
        self.assertTrue(wait_until(lambda: app.store.messages(g["id"])[-1].get("key") == "note.all_silent"))
        self.assertEqual(app.store.messages(g["id"])[-2].get("status"), "silent")   # hidden, and a hint says how to call
        app.llm.responder = lambda *args: {"content": "@user which one do you want?"}
        app.chat.post(g["id"], "and you?")
        self.assertTrue(wait_until(lambda: any(m.get("needs_you") for m in app.store.messages(g["id"]))))

    def test_memory_cap_and_context_report(self):
        app, llm, _ = make_app(script({"tool_calls": [{"name": "write_memory", "arguments": {"text": "x" * 1400}}]},
                                      {"content": "ok"}), memory_chars=1000)
        room = onboard(app)["room"]["id"]
        app.chat.post(room, "remember")
        self.assertTrue(wait_reply(app, room))
        last = app.store.messages(room)[-1]
        self.assertEqual(last["tools"][0]["status"], "error")
        self.assertEqual(app.store.memory(app.store.orchestrator()["id"]), "")
        self.assertIn("total", last["context"])
        self.assertFalse(last["context"]["over_target"])
        events = app.store.read_jsonl(next(app.store.path("events").glob("*.jsonl")))
        self.assertTrue(any(e["kind"] == "message" and e["actor"] == "user" for e in events))
        self.assertTrue(any(e["kind"] == "tool" for e in events))

    def test_stop_turn(self):
        def slow(m, t, e):
            time.sleep(0.3)
            return {"content": "late"}
        app, llm, _ = make_app(slow)
        room = onboard(app)["room"]["id"]
        app.chat.post(room, "hello")
        wait_until(lambda: app.chat.busy(room))
        app.chat.stop(room)
        self.assertTrue(wait_until(lambda: not app.chat.busy(room), 3))


class SessionsTest(unittest.TestCase):
    def test_bot_harness_session_changes_files(self):
        app, llm, sent = make_app(script(
            {"tool_calls": [{"name": "write_file", "arguments": {"path": "notes/findings.md", "content": "# Notes\n"}}]},
            {"tool_calls": [{"name": "write_file", "arguments": {"path": "../escape.txt", "content": "x"}}]},
            {"content": "Wrote notes/findings.md."}))
        onboard(app)
        p = app.create_project(name="Research", description="find things")
        s = app.sessions.create(p, "bot", "write notes")
        done = wait_until(lambda: app.store.session(s["id"])["status"] in ("review", "failed", "waiting"), 5)
        self.assertTrue(done)
        s = app.store.session(s["id"])
        self.assertEqual(s["status"], "review")
        self.assertEqual([c["path"] for c in s["changed_files"]], ["notes/findings.md"])
        self.assertFalse((Path(app.cfg["projects_dir"]) / "escape.txt").exists())
        self.assertEqual(llm.calls[0]["effort"], "medium")
        self.assertEqual(s["last_message"], "Wrote notes/findings.md.")
        kinds = [e["kind"] for e in app.store.session_events(s["id"])]
        self.assertIn("prompt", kinds)
        self.assertIn("files", kinds)
        # follow-up continues the same conversation
        app.sessions.send(s["id"], "add more")
        wait_until(lambda: app.store.session(s["id"])["turns"] == 2, 5)
        self.assertEqual(app.store.session(s["id"])["turns"], 2)

    def test_snapshot_diff_and_tests_parser(self):
        d = Path(tempfile.mkdtemp())
        (d / "a.txt").write_text("1")
        (d / ".git").mkdir()
        (d / ".git" / "x").write_text("ignored")
        before = snapshot(d)
        (d / "a.txt").write_text("22")
        (d / "b.txt").write_text("new")
        changes = diff(before, snapshot(d))
        self.assertEqual(changes, [{"path": "a.txt", "change": "modified"}, {"path": "b.txt", "change": "added"}])
        self.assertEqual(parse_tests(0, "Ran 4 tests in 0.01s\n\nOK")["passed"], 4)
        self.assertEqual(parse_tests(1, "Ran 4 tests\nFAILED (failures=1)")["status"], "failed")
        self.assertEqual(parse_tests(None, "")["status"], "error")

    def test_codex_command_is_always_sandboxed(self):
        app, _, _ = make_app()
        work = Path(tempfile.mkdtemp())
        home = codexmod.write_session_home(app, "s-00000001", work, None)
        text = (home / "config.toml").read_text(encoding="utf-8")
        for need in codexmod.REQUIRED_CONFIG:
            self.assertIn(need, text)
        for bad in codexmod.FORBIDDEN_CONFIG:
            self.assertNotIn(bad, text)
        self.assertTrue((work / ".snaplab" / "gitconfig").exists())
        h = codexmod.CodexHarness(app)
        first = h.command({"id": "s-00000001"}, work, True)
        resume = h.command({"id": "s-00000001", "thread_id": "abc"}, work, False)
        for cmd in (first, resume):
            joined = " ".join(map(str, cmd))
            self.assertIn('default_permissions="snaplab"', joined)
            self.assertIn('windows.sandbox="mxc"', joined)
            self.assertNotIn("sandbox_mode", joined)
            self.assertNotIn("danger", joined)
            if os.name == "nt":
                codexmod.check_safe(app, "s-00000001", cmd)
        if os.name == "nt":
            for extra in (["--dangerously-bypass-approvals-and-sandbox"], ["-s", "workspace-write"],
                          ["-c", 'sandbox_mode="workspace-write"'], ["--add-dir", "C:\\"]):
                with self.assertRaises(RuntimeError, msg=str(extra)):
                    codexmod.check_safe(app, "s-00000001", first + extra)
            with self.assertRaises(RuntimeError):
                codexmod.check_safe(app, "s-00000001", [c for c in first if c != 'default_permissions="snaplab"'])
            for broken in (text.replace('":root" = "deny"', '":root" = "read"'),
                           text.replace("enabled = false", "enabled = true"),
                           text + '\nsandbox_mode = "workspace-write"\n'):
                (home / "config.toml").write_text(broken, encoding="utf-8")
                with self.assertRaises(RuntimeError):
                    codexmod.check_safe(app, "s-00000001", first)

    def test_child_env_has_no_secrets(self):
        app, _, _ = make_app()
        os.environ["SNAPSCRIBE_API_KEY_TEST_X"] = "v"
        os.environ["MY_TOKEN"] = "v"
        try:
            home = codexmod.session_home(app, "s-00000002")
            env = codexmod.child_env(app, home, {"SNAPLAB_PROXY_TOKEN": "t"})
            self.assertNotIn("SNAPSCRIBE_API_KEY", env)
            self.assertNotIn("SNAPSCRIBE_API_KEY_TEST_X", env)
            self.assertNotIn("MY_TOKEN", env)
            self.assertEqual(env["SNAPLAB_PROXY_TOKEN"], "t")
            self.assertEqual(Path(env["CODEX_HOME"]), home)
        finally:
            del os.environ["SNAPSCRIBE_API_KEY_TEST_X"], os.environ["MY_TOKEN"]

    def test_worktree_result_card_and_merge(self):
        from snaplab import gitops
        if not gitops.available():
            self.skipTest("git is not installed")
        app, llm, _ = make_app(script(
            {"tool_calls": [{"name": "write_file", "arguments": {"path": "notes/a.md", "content": "one\ntwo\n"}}]},
            {"content": "Wrote notes/a.md."}))
        room = onboard(app)["room"]["id"]
        p = app.create_project(name="Repo", description="goal text", test_command="python -m unittest -v")
        pdir = Path(app.cfg["projects_dir"]) / p["slug"]
        self.assertTrue((pdir / ".git").exists())
        self.assertIn("goal text", (pdir / "BRIEF.md").read_text(encoding="utf-8"))
        self.assertIn("python -m unittest -v", (pdir / "BRIEF.md").read_text(encoding="utf-8"))
        self.assertTrue((pdir / "AGENTS.md").exists())
        self.assertEqual(gitops.run(pdir, "config", "user.email").stdout.strip(), "snaplab@localhost")
        s = app.sessions.create(p, "bot", "write notes", room=room)
        self.assertTrue(wait_until(lambda: app.store.session(s["id"])["status"] == "review", 5))
        s = app.store.session(s["id"])
        wt = Path(s["worktree"])
        self.assertEqual(wt.parent, pdir / ".worktrees")
        self.assertTrue((wt / "notes" / "a.md").exists())
        self.assertFalse((pdir / "notes" / "a.md").exists())          # main folder stays clean until merged
        self.assertEqual(s["diff"]["files"], 1)
        self.assertEqual(s["diff"]["insertions"], 2)
        r = app.store.read_json(app.store.path("sessions", f"{s['id']}.result.json"), None)
        self.assertEqual(r["status"], "review")
        self.assertEqual(r["changed_files"], [{"path": "notes/a.md", "change": "added"}])
        self.assertIn(s["id"], (pdir / "STATE.md").read_text(encoding="utf-8"))
        # The room also gets SnapLab's "started" note; wait for the result card itself.
        cards = lambda: [m for m in app.store.messages(room) if "[Result card]" in (m.get("text") or "")]
        self.assertTrue(wait_until(lambda: cards(), 5))
        note = cards()[-1]
        self.assertIn("[Result card]", note["text"])
        self.assertIn("+2 -0", note["text"])
        from snaplab.context import estimate_tokens
        self.assertLess(estimate_tokens(note["text"]), 300)
        self.assertEqual(gitops.run(pdir, "status", "--porcelain", "--untracked-files=no").stdout.strip(), "")
        app.sessions.merge(s["id"])
        self.assertTrue((pdir / "notes" / "a.md").exists())
        self.assertTrue(app.store.session(s["id"])["merged"])

    def test_plugin_that_runs_code_unsandboxed_is_refused(self):
        app, _, _ = make_app(harness_plugins=["helpers_plugin:Unsafe", "helpers_plugin:Safe"])
        ids = list(load_harnesses(app))
        self.assertIn("safe", ids)
        self.assertNotIn("unsafe", ids)


FAKE_SECRET = "sk-FAKE-" + "0123456789abcdef" * 2     # a stand-in for the API key; the real value is never used here


class GuardTest(unittest.TestCase):
    """The exit guard: a secret value never reaches disk, the phone, notifications or git."""

    def test_guard_basics(self):
        from snaplab.guard import MARK, Guard
        g = Guard(values=[FAKE_SECRET])
        self.assertTrue(g.contains("x " + FAKE_SECRET))
        self.assertEqual(g.clean("a " + FAKE_SECRET + " b"), f"a {MARK} b")
        d = Path(tempfile.mkdtemp())
        (d / "a.txt").write_text("k=" + FAKE_SECRET, encoding="utf-8")
        (d / "b.txt").write_text("k=" + FAKE_SECRET, encoding="utf-16-le")
        (d / "c.txt").write_text("nothing here", encoding="utf-8")
        self.assertEqual(sorted(g.clean_tree(d)), ["a.txt", "b.txt"])
        for f in d.iterdir():
            self.assertNotIn(FAKE_SECRET.encode(), f.read_bytes())
            self.assertNotIn(FAKE_SECRET.encode("utf-16-le"), f.read_bytes())

    def test_secret_never_stored_shown_or_notified(self):
        app, llm, sent = make_app(script(
            {"content": "your key is " + FAKE_SECRET, "reasoning": "it is " + FAKE_SECRET,
             "tool_calls": [{"name": "update_dashboard", "arguments": {"op": "upsert", "widgets": [
                 {"id": "k", "type": "note", "text": "key " + FAKE_SECRET}]}}]},
            {"content": "done " + FAKE_SECRET}))
        app.guard.add(FAKE_SECRET)
        ua = pushmod.ec.generate_private_key(pushmod.ec.SECP256R1())
        auth_secret = pushmod.b64u(os.urandom(16))
        app.push.subscribe({"endpoint": "https://fcm.googleapis.com/fcm/send/x",
                            "keys": {"p256dh": pushmod.b64u(pushmod.public_bytes(ua.public_key())), "auth": auth_secret}})
        room = onboard(app)["room"]["id"]
        app.chat.post(room, "show me")
        self.assertTrue(wait_reply(app, room))
        app.push.notify("needs_you", "title " + FAKE_SECRET, "body " + FAKE_SECRET, wait=True)
        for f in Path(app.cfg["data_dir"]).rglob("*"):
            if f.is_file():
                self.assertNotIn(FAKE_SECRET.encode(), f.read_bytes(), str(f))
        self.assertNotIn(FAKE_SECRET, app.redact_log(json.dumps({"text": "x" + FAKE_SECRET})))
        self.assertTrue(sent)
        plain = pushmod.decrypt(sent[-1][1], ua, auth_secret).decode()
        self.assertNotIn(FAKE_SECRET, plain)

    def test_secret_in_session_files_is_removed_before_commit(self):
        from snaplab import gitops
        if not gitops.available():
            self.skipTest("git is not installed")
        app, _, _ = make_app(harness_plugins=["helpers_plugin:Leaky"])
        app.guard.add(FAKE_SECRET)
        onboard(app)
        p = app.create_project(name="Leak", description="d")
        s = app.sessions.create(p, "leaky", FAKE_SECRET)
        self.assertTrue(wait_until(lambda: app.store.session(s["id"])["status"] == "review", 5))
        s = app.store.session(s["id"])
        wt = Path(s["worktree"])
        for name in ("leak.txt", "leak16.txt"):
            self.assertNotIn(FAKE_SECRET.encode(), (wt / name).read_bytes())
            self.assertNotIn(FAKE_SECRET.encode("utf-16-le"), (wt / name).read_bytes())
        log = gitops.run(wt, "log", "-p", "--all").stdout
        self.assertNotIn(FAKE_SECRET, log)
        kinds = [e["kind"] for e in app.store.session_events(s["id"])]
        self.assertIn("error", kinds)                       # the removal is reported on the timeline
        for f in Path(app.cfg["data_dir"]).rglob("*"):
            if f.is_file() and ".git" not in f.parts:
                self.assertNotIn(FAKE_SECRET.encode(), f.read_bytes(), str(f))


class PushTest(unittest.TestCase):
    def test_encrypt_roundtrip_and_vapid(self):
        ua = pushmod.ec.generate_private_key(pushmod.ec.SECP256R1())
        auth_secret = pushmod.b64u(os.urandom(16))
        body = pushmod.encrypt(b'{"title":"hi"}', pushmod.b64u(pushmod.public_bytes(ua.public_key())), auth_secret)
        self.assertEqual(pushmod.decrypt(body, ua, auth_secret), b'{"title":"hi"}')
        key = pushmod.ec.generate_private_key(pushmod.ec.SECP256R1())
        jwt = pushmod.vapid_jwt(key, "https://fcm.googleapis.com/fcm/send/x", "mailto:a@example.com")
        h, c, sig = jwt.split(".")
        claims = json.loads(pushmod.unb64u(c))
        self.assertEqual(claims["aud"], "https://fcm.googleapis.com")
        raw = pushmod.unb64u(sig)
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
        der = encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big"))
        key.public_key().verify(der, f"{h}.{c}".encode(), pushmod.ec.ECDSA(hashes.SHA256()))

    def test_endpoint_allowlist_and_kinds(self):
        app, _, sent = make_app()
        onboard(app)
        good = {"p256dh": pushmod.b64u(pushmod.public_bytes(pushmod.ec.generate_private_key(pushmod.ec.SECP256R1()).public_key())),
                "auth": pushmod.b64u(os.urandom(16))}
        with self.assertRaises(ValueError):
            app.push.subscribe({"endpoint": "https://evil.example.com/push", "keys": good})
        with self.assertRaises(ValueError):
            app.push.subscribe({"endpoint": "http://fcm.googleapis.com/x", "keys": good})
        app.push.subscribe({"endpoint": "https://wns2-par02p.notify.windows.com/w/?token=abc", "keys": good})
        app.store.update_settings(notify={"review": False})
        app.push.notify("review", "t", "b", wait=True)        # switched off
        self.assertEqual(sent, [])
        app.push.notify("needs_you", "t", "b", wait=True, dedupe="k1")
        app.push.notify("needs_you", "t", "b", wait=True, dedupe="k1")   # the same notice only once
        self.assertEqual(len(sent), 1)
        ep, body, headers = sent[0]
        self.assertEqual(headers["Content-Encoding"], "aes128gcm")
        self.assertTrue(headers["Authorization"].startswith("vapid t="))


class SearchParsingTest(unittest.TestCase):
    def test_parse_duckduckgo(self):
        html = (Path(__file__).parent / "fixtures" / "ddg.html").read_text(encoding="utf-8")
        res = websearch.parse_duckduckgo(html, 5)
        self.assertEqual(res[0]["url"], "https://docs.python.org/3/library/http.server.html")
        self.assertEqual(res[1]["url"], "https://example.com/redirected")
        self.assertIn("HTTP servers", res[0]["title"])
        self.assertTrue(res[0]["snippet"])
        self.assertEqual(len(res), 2)   # the ad is skipped

    def test_captcha_detection_and_no_pause(self):
        self.assertTrue(websearch.looks_like_captcha(202, '<div class="anomaly-modal">bots use DuckDuckGo too</div>'))
        self.assertTrue(websearch.looks_like_captcha(403, "<title>Just a moment...</title>"))
        self.assertFalse(websearch.looks_like_captcha(200, "<title>Docs</title><div class='g-recaptcha'></div>"))
        s = websearch.Searcher({"provider": "duckduckgo", "cooldown_minutes": 30})
        orig = websearch.search_duckduckgo
        calls = []

        def captcha(q, c):
            calls.append(q)
            raise websearch.SearchError("captcha", "human check")
        websearch.search_duckduckgo = captcha
        try:
            for _ in range(2):
                with self.assertRaises(websearch.SearchError) as cm:
                    s.search("x")
                self.assertEqual(cm.exception.kind, "captcha")
        finally:
            websearch.search_duckduckgo = orig
        self.assertEqual(len(calls), 2)    # a human check only means no answer this time; asked again (2026-11-21)

    def test_read_page_refuses_private_addresses(self):
        for url in ("file:///etc/passwd", "http://127.0.0.1:18445/api", "http://localhost/", "http://100.100.100.100/",
                    "http://pc.tail1234.ts.net/", "ftp://example.com/x"):
            with self.assertRaises(websearch.SearchError, msg=url):
                websearch.read_page(url)

    def test_read_page_sends_url_credentials_to_that_host_only_and_follows_many_redirects(self):
        # user:password@ URLs are read since 2026-11-21 (HTTP Basic, not along redirects); up to 30 redirects.
        seen = []

        def fake(url, **k):
            seen.append((url, (k.get("headers") or {}).get("Authorization")))
            if len(seen) < 25:
                return 302, {"Location": f"http://example.com/{len(seen)}"}, b"", url
            return 200, {"Content-Type": "text/plain"}, b"hello", url
        orig_http = websearch._http
        websearch._http = fake
        try:
            page = websearch.read_page("http://user:p%40ss@example.com/start", resolver=lambda h: h == "example.com")
        finally:
            websearch._http = orig_http
        self.assertEqual(page["text"], "hello")
        self.assertEqual(seen[0][0], "http://example.com/start")              # the credentials are not in the URL
        import base64
        self.assertEqual(seen[0][1], "Basic " + base64.b64encode(b"user:p@ss").decode())
        self.assertTrue(all(auth is None for _, auth in seen[1:]))          # not sent along the redirects
        self.assertEqual(len(seen), 25)

    def test_read_page_refuses_home_network_and_tricks(self):
        """Home-network and tailnet ranges, names that resolve to them, IPv6 forms, and redirects into them."""
        for url in ("http://10.0.0.5/", "http://192.168.1.10/admin", "http://172.16.0.1/", "http://169.254.169.254/latest/",
                    "http://[::1]:8444/", "http://[fd7a:115c:a1e0::1]/", "http://0.0.0.0/", "http://pc.example.ts.net:8444/",
                    "http://router.lan/", "http://printer.local/", "http://127.0.0.1:18440/api/state"):
            with self.assertRaises(websearch.SearchError, msg=url):
                websearch.read_page(url)
        real = websearch.socket.getaddrinfo
        websearch.socket.getaddrinfo = lambda host, *a, **k: [(2, 1, 6, "", ("127.0.0.1", 0))]
        try:
            with self.assertRaises(websearch.SearchError):
                websearch.read_page("http://looks-public.example/")      # a public-looking name that points home
        finally:
            websearch.socket.getaddrinfo = real
        orig_http = websearch._http
        websearch._http = lambda url, **k: (302, {"Location": "http://192.168.0.1/"}, b"", url)
        try:
            with self.assertRaises(websearch.SearchError) as cm:
                websearch.read_page("http://example.com/", resolver=lambda h: h == "example.com")
            self.assertEqual(cm.exception.kind, "blocked")             # the redirect target is checked too
        finally:
            websearch._http = orig_http

    def test_peer_check_after_connect(self):
        class FakeSock:
            def __init__(self, ip):
                self.ip, self.closed = ip, False

            def getpeername(self):
                return (self.ip, 80)

            def close(self):
                self.closed = True
        websearch._check_peer(FakeSock("93.184.216.34"))
        for ip in ("127.0.0.1", "10.1.1.1", "100.100.1.1", "::ffff:192.168.1.1"):
            sock = FakeSock(ip)
            with self.assertRaises(websearch.SearchError):
                websearch._check_peer(sock)
            self.assertTrue(sock.closed)

    def test_html_to_text(self):
        title, text = websearch.html_to_text("<html><head><title>T</title><script>evil()</script></head><body><nav>menu</nav>"
                                             "<h1>Hello</h1><p>World <b>bold</b></p><ul><li>one</li></ul><style>x{}</style></body></html>")
        self.assertEqual(title, "T")
        self.assertIn("Hello", text)
        self.assertIn("- one", text)
        self.assertNotIn("evil", text)
        self.assertNotIn("menu", text)


if __name__ == "__main__":
    unittest.main()
