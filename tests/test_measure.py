"""Changes from the harness measurement (2026-11-01): context numbers, thinking depth after tools, said-but-not-done,
a stable prompt prefix, the dashboard, group-room rules and session ids in the context."""
import time
import unittest

from helpers import make_app, onboard, wait_until

from snaplab import agent
from snaplab.agent import ToolResult, run_loop
from snaplab.llm import FakeLLM


def script(*steps):
    it = iter(steps)

    def responder(messages, tools, effort):
        try:
            return next(it)
        except StopIteration:
            return {"content": "Done."}
    return responder


def settle(app, room, quiet=0.4, timeout=8):
    """Wait until the room has a finished bot reply after the last user message and nothing ran for `quiet` s."""
    end = time.time() + timeout
    calm_since = None
    while time.time() < end:
        msgs = app.store.messages(room)
        users = [i for i, m in enumerate(msgs) if m["role"] == "user"]
        answered = users and any(m["role"] == "bot" and m.get("status") != "streaming" for m in msgs[users[-1]:])
        q = app.chat.queues.get(room)
        idle = answered and not app.chat.busy(room) and (q is None or q.empty())
        if idle:
            calm_since = calm_since or time.time()
            if time.time() - calm_since >= quiet:
                return True
        else:
            calm_since = None
        time.sleep(0.03)
    return False


class NumbersAndDepthTest(unittest.TestCase):
    def test_context_numbers_are_the_measured_defaults(self):
        app, _, _ = make_app()
        c = app.chat.context.cfg
        self.assertEqual((c["target_tokens"], c["summarize_tokens"], c["max_tokens"], c["session_max_tokens"]),
                         (16000, 24000, 32000, 64000))
        self.assertEqual((app.cfg["api"]["chat_effort"], app.cfg["api"]["after_tools_effort"]), ("low", "medium"))

    def test_low_first_then_medium_after_a_tool(self):
        app, llm, _ = make_app(script({"tool_calls": [{"name": "list_projects", "arguments": {}}]}, {"content": "None yet."},
                                      {"content": "Hello!"}))
        room = onboard(app)["room"]["id"]
        app.chat.post(room, "any projects?")
        self.assertTrue(settle(app, room))
        app.chat.post(room, "hi")
        self.assertTrue(settle(app, room))
        self.assertEqual([c["effort"] for c in llm.calls], ["low", "medium", "low"])


class ClaimsTest(unittest.TestCase):
    def test_saying_it_remembered_without_the_tool_is_sent_back_once(self):
        app, llm, _ = make_app(script({"content": "Got it, I'll remember that you like short answers."},
                                      {"tool_calls": [{"name": "write_memory", "arguments": {"text": "Likes short answers"}}]},
                                      {"content": "Saved."}))
        room = onboard(app)["room"]["id"]
        app.chat.post(room, "remember that I like short answers")
        self.assertTrue(settle(app, room))
        self.assertEqual(len(llm.calls), 3)
        self.assertIn("[SnapLab check]", llm.calls[1]["messages"][-1]["content"])
        self.assertIn("Likes short answers", app.store.memory(app.store.orchestrator()["id"]))
        evs = [e for e in app.store.read_jsonl(next(app.store.path("events").glob("*.jsonl")))]
        self.assertTrue(any(e.get("kind") == "claim.unbacked" for e in evs))

    def test_claim_patterns(self):
        av = {"write_memory", "update_dashboard", "start_session"}
        self.assertEqual(agent.claim_gaps("覚えておきます！", set(), av)[0][0], "write_memory")
        self.assertEqual(agent.claim_gaps("I've updated the dashboard.", set(), av)[0][0], "update_dashboard")
        self.assertEqual(agent.claim_gaps("I'll remember that.", {"write_memory"}, av), [])
        self.assertEqual(agent.claim_gaps("Shall I start a session?", set(), av), [])
        self.assertEqual(agent.claim_gaps("I'll remember that.", set(), {"start_session"}), [])

    def test_old_tool_results_are_shortened_past_the_limit(self):
        msgs = [{"role": "system", "content": "s"}] + [{"role": "tool", "tool_call_id": str(i), "content": "x" * 4000}
                                                       for i in range(6)]
        self.assertTrue(agent._compact(msgs, 1000))
        self.assertTrue(msgs[1]["content"].startswith("[earlier tool result, shortened]"))
        self.assertEqual(len(msgs[-1]["content"]), 4000)                 # the latest results stay whole

    def test_loop_reports_the_first_prompt_size(self):
        llm = FakeLLM(lambda m, t, e: {"content": "hi", "input_tokens": 1234})
        r = run_loop(llm, [{"role": "user", "content": "x"}], [], lambda *a: ToolResult("x"), effort="low", max_steps=3,
                     max_tokens=100, source="t")
        self.assertEqual(r.first_input_tokens, 1234)


class StablePrefixTest(unittest.TestCase):
    def test_time_and_sessions_are_in_the_latest_message_not_the_system_prompt(self):
        app, _, _ = make_app()
        room = onboard(app)["room"]["id"]
        p = app.create_project(name="Calc", description="d")
        s = {"id": "s-0000abcd", "project": p["id"], "harness": "codex", "title": "Add mul", "status": "review",
             "updated": time.strftime("%Y-%m-%dT%H:%M:%S+09:00"), "started_by": "user", "tests": {"passed": 3, "failed": 0},
             "tokens": {"input": 0, "output": 0}}
        app.store.save_session(s)
        app.chat.post(room, "hello")
        orch = app.store.orchestrator()
        m1, _ = app.chat.context.build(app.store.room(room), orch, "en")
        m2, report = app.chat.context.build(app.store.room(room), orch, "en")
        self.assertNotIn("Now:", m1[0]["content"])
        self.assertEqual(m1[0]["content"], m2[0]["content"])             # the same prefix every call
        last = m1[-1]["content"]
        self.assertTrue(last.startswith("[SnapLab context"))
        self.assertIn("s-0000abcd", last)
        self.assertIn("review", last)
        self.assertIn("tests 3/3", last)
        self.assertTrue(last.rstrip().endswith("hello"))
        self.assertIn("state", report)

    def test_estimate_learns_from_the_server_count(self):
        app, _, _ = make_app()
        app.chat.context.learn("r-1", 2000, 2600)
        self.assertAlmostEqual(app.chat.context.factor["r-1"], 1.3)
        app.chat.context.learn("r-1", 2000, 1)                         # nonsense values are clamped
        self.assertGreaterEqual(app.chat.context.factor["r-1"], 0.6)


class DashboardTest(unittest.TestCase):
    def test_no_full_replace_and_removing_asks_first(self):
        app, _, _ = make_app()
        room = onboard(app)["room"]["id"]
        from snaplab.tools import Toolbox
        box = Toolbox(app, app.store.orchestrator(), room=app.store.room(room), lang="en")
        before = [w["id"] for w in app.store.dashboard()["widgets"]]
        res = box("update_dashboard", {"op": "set", "widgets": []}, "c1")
        self.assertIn("Error", res.content)
        self.assertEqual([w["id"] for w in app.store.dashboard()["widgets"]], before)
        res = box("update_dashboard", {"op": "remove", "ids": before}, "c2")
        self.assertTrue(res.stop)
        self.assertEqual([w["id"] for w in app.store.dashboard()["widgets"]], before)
        q = app.store.questions()[-1]
        self.assertEqual(q["action"]["op"], "remove")
        app.chat.answer(q["id"], "Approve")
        self.assertEqual(app.store.dashboard()["widgets"], [])
        tool = next(t for t in box.schemas() if t["function"]["name"] == "update_dashboard")
        self.assertNotIn("set", tool["function"]["parameters"]["properties"]["op"]["enum"])


class GroupRoomTest(unittest.TestCase):
    def test_session_instructions_in_a_group_wait_for_the_card(self):
        app, _, _ = make_app()
        onboard(app)
        p = app.create_project(name="Calc", description="d")
        coral = app.create_bot(name="Coral", role="r", projects=[p["id"]])      # round 6: a teammate works in its own project
        g = app.create_group("Crew", [app.store.orchestrator()["id"], coral["id"]])
        app.store.save_session({"id": "s-0000beef", "project": p["id"], "harness": "bot", "title": "t", "status": "review",
                                "tokens": {"input": 0, "output": 0}, "started_by": "user"})
        from snaplab.tools import Toolbox
        box = Toolbox(app, app.store.bot(coral["id"]), room=app.store.room(g["id"]), lang="en")
        res = box("send_to_session", {"session_id": "s-0000beef", "prompt": "do more"}, "c1")
        self.assertTrue(res.stop)
        q = app.store.questions()[-1]
        self.assertEqual((q["action"]["type"], q["prop"]["why"]), ("send_to_session", "group"))

    def test_a_question_in_the_text_reaches_needs_you(self):
        app, _, _ = make_app(lambda m, t, e: {"content": "We could use unittest or pytest. Which one do you prefer, Sam?"})
        onboard(app)
        coral = app.create_bot(name="Coral", role="r")
        g = app.create_group("Crew", [app.store.orchestrator()["id"], coral["id"]])
        app.chat.post(g["id"], "how should we test?")
        self.assertTrue(settle(app, g["id"]))
        last = [m for m in app.store.messages(g["id"]) if m["role"] == "bot" and m.get("status") == "done"][-1]
        self.assertTrue(last.get("needs_you"))

    def test_repeated_bot_contributions_are_hidden_and_bounded(self):
        calls = []

        def responder(messages, tools, effort):
            sys = messages[0]["content"]
            who = "Coral" if "You are Coral" in sys else ("Nemo" if "You are Nemo" in sys else "Marlin")
            calls.append(who)
            if who == "Marlin":
                return {"content": "@Coral and @Nemo, what do you think?"}
            if who == "Nemo":
                return {"content": "@Coral please check this too."}
            return {"content": "Looks fine."}
        app, _, _ = make_app(responder)
        onboard(app)
        coral = app.create_bot(name="Coral", role="r")
        nemo = app.create_bot(name="Nemo", role="r")
        g = app.create_group("Crew", [app.store.orchestrator()["id"], coral["id"], nemo["id"]])
        app.chat.post(g["id"], "thoughts?")
        self.assertTrue(settle(app, g["id"]))
        time.sleep(0.3)
        coral_replies = [m for m in app.store.messages(g["id"]) if m.get("bot") == coral["id"] and m.get("status") == "done"]
        self.assertEqual(len(coral_replies), 1)
        self.assertLessEqual(len(calls), app.cfg["bot_conversations"]["max_consecutive"])


if __name__ == "__main__":
    unittest.main()
