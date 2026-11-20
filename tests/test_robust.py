"""Robustness of chat turns (QA round 1: B-2 restart, B-3 errors and retry, B-4 cut replies, B-5 rapid messages,
U-8 plain answers)."""
import tempfile
import time
import unittest

from helpers import make_app, onboard, wait_until

from snaplab.llm import ApiError, StreamCut, error_code


def done_replies(app, room):
    return [m for m in app.store.messages(room) if m["role"] == "bot" and m.get("status") not in ("streaming",)]


class RobustChatTest(unittest.TestCase):
    def test_rapid_messages_are_answered_together(self):
        def slow(messages, tools, effort):
            time.sleep(0.4)
            return {"content": "ok"}
        app, llm, _ = make_app(slow)
        room = onboard(app)["room"]["id"]
        for i in range(4):
            app.chat.post(room, f"message number {i}")
            time.sleep(0.03)
        self.assertTrue(wait_until(lambda: not app.chat.busy(room) and app.chat.queues[room].empty()
                                   and len(done_replies(app, room)) >= 3, 8))
        time.sleep(0.5)
        self.assertLessEqual(len(llm.calls), 2)                 # not once per message
        last_user = [m for m in llm.calls[-1]["messages"] if m["role"] == "user"][-1]["content"]
        self.assertIn("message number 3", last_user)
        self.assertNotIn("[SnapLab note] Continue.", last_user)

    def test_cut_reply_keeps_text_and_is_marked(self):
        app, llm, _ = make_app(lambda m, t, e: {"content": "This reply will be cut in the mid", "cut": True})
        room = onboard(app)["room"]["id"]
        app.chat.post(room, "hi")
        self.assertTrue(wait_until(lambda: len(done_replies(app, room)) >= 2))
        m = done_replies(app, room)[-1]
        self.assertEqual(m["status"], "error")
        self.assertEqual(m["error_code"], "cut")
        self.assertEqual(m["text"], "This reply will be cut in the mid")

    def test_error_codes_and_try_again(self):
        answers = iter([{"content": "", "error_status": 429, "error_text": '{"error": {"message": "busy"}}'},
                        {"content": "Second time lucky."}])
        app, llm, _ = make_app(lambda m, t, e: next(answers))
        room = onboard(app)["room"]["id"]
        app.chat.post(room, "hi")
        self.assertTrue(wait_until(lambda: len(done_replies(app, room)) >= 2))
        bad = done_replies(app, room)[-1]
        self.assertEqual((bad["status"], bad["error_code"]), ("error", "busy"))
        self.assertTrue(wait_until(lambda: not app.chat.busy(room)))
        app.chat.retry(room, bad["id"])
        self.assertTrue(wait_until(lambda: done_replies(app, room)[-1].get("text") == "Second time lucky."))
        msgs = app.store.messages(room)
        self.assertTrue(next(m for m in msgs if m["id"] == bad["id"])["retried"])
        with self.assertRaises(ValueError):
            app.chat.retry(room, bad["id"])                    # only once
        # the failed reply is not shown to the model again
        self.assertEqual(len(llm.calls), 2)
        self.assertEqual(sum(1 for m in llm.calls[1]["messages"] if m["role"] == "assistant"), 0)

    def test_error_code_mapping(self):
        from snaplab.llm import Reply
        self.assertEqual(error_code(StreamCut(Reply())), "cut")
        self.assertEqual(error_code(ApiError(None, "SNAPSCRIBE_API_KEY is not set")), "nokey")
        self.assertEqual(error_code(ApiError(None, "timed out")), "timeout")
        self.assertEqual(error_code(ApiError(None, "connection refused")), "unreachable")
        self.assertEqual(error_code(ApiError(401, "x")), "key")
        self.assertEqual(error_code(ApiError(400, "maximum context length")), "too_long")
        self.assertEqual(error_code(ApiError(503, "x")), "server")
        self.assertEqual(error_code(RuntimeError("x")), "internal")

    def test_reply_cut_by_a_restart_is_marked_after_start(self):
        tmp = tempfile.mkdtemp(prefix="snaplab-restart-")
        app, _, _ = make_app(tmp=tmp)
        room = onboard(app)["room"]["id"]
        app.store.add_message(room, {"id": "m-0000beef", "role": "bot", "bot": app.store.bots()[0]["id"],
                                     "status": "streaming", "text": "half a", "thinking": ""}, touch_room=False)
        app2, _, _ = make_app(tmp=tmp)                      # SnapLab starts again on the same data
        m = next(x for x in app2.store.messages(room) if x["id"] == "m-0000beef")
        self.assertEqual((m["status"], m["error_code"]), ("error", "restart"))
        self.assertEqual(m["text"], "half a")
        self.assertEqual(sum(1 for x in app2.store.messages(room) if x["id"] == "m-0000beef"), 1)

    def test_restart_keeps_the_text_that_had_arrived(self):
        tmp = tempfile.mkdtemp(prefix="snaplab-restart-")
        app, _, _ = make_app(tmp=tmp)
        room = onboard(app)["room"]["id"]
        app.store.add_message(room, {"id": "m-0000cafe", "role": "bot", "bot": app.store.bots()[0]["id"],
                                     "status": "streaming", "text": "", "thinking": ""}, touch_room=False)
        app.store.save_draft("m-0000cafe", "Sure thing! SnapLab is ready. I can", "planning")
        app2, _, _ = make_app(tmp=tmp)
        m = next(x for x in app2.store.messages(room) if x["id"] == "m-0000cafe")
        self.assertEqual((m["status"], m["text"], m["thinking"]), ("error", "Sure thing! SnapLab is ready. I can", "planning"))
        self.assertIsNone(app2.store.draft("m-0000cafe"))

    def test_draft_is_written_while_streaming_and_dropped_at_the_end(self):
        from snaplab import chat as chat_mod
        old = chat_mod.DRAFT_EVERY_S
        chat_mod.DRAFT_EVERY_S = 0
        try:
            app, _, _ = make_app(lambda m, t, e: {"content": "All done here."})
            room = onboard(app)["room"]["id"]
            seen = []
            real = app.store.save_draft
            app.store.save_draft = lambda mid, text, th: (seen.append(text), real(mid, text, th))
            app.chat.post(room, "hi")
            self.assertTrue(wait_until(lambda: app.store.messages(room)[-1].get("status") == "done"))
            self.assertTrue(seen)
            d = app.store.path("messages", "drafts")
            self.assertEqual([f.name for f in d.glob("*.json")] if d.exists() else [], [])
        finally:
            chat_mod.DRAFT_EVERY_S = old

    def test_reply_is_saved_while_it_is_written(self):
        gate = []

        def slow(messages, tools, effort):
            while not gate:
                time.sleep(0.02)
            return {"content": "finished"}
        app, _, _ = make_app(slow)
        room = onboard(app)["room"]["id"]
        app.chat.post(room, "hi")
        self.assertTrue(wait_until(lambda: any(m.get("status") == "streaming" for m in app.store.messages(room))))
        gate.append(1)
        self.assertTrue(wait_until(lambda: app.store.messages(room)[-1].get("text") == "finished"))
        ids = [m["id"] for m in app.store.messages(room)]
        self.assertEqual(len(ids), len(set(ids)))

    def test_answer_shows_plainly_but_the_model_sees_the_question(self):
        from test_core import script
        app, llm, _ = make_app(script({"tool_calls": [{"name": "ask_user", "arguments": {"question": "Tabs or spaces?", "options": ["Tabs", "Spaces"]}}]},
                                      {"content": "Spaces it is."}))
        room = onboard(app)["room"]["id"]
        app.chat.post(room, "ask me")
        self.assertTrue(wait_until(lambda: app.store.questions()))
        q = app.store.questions()[0]
        self.assertTrue(wait_until(lambda: not app.chat.busy(room)))
        app.chat.answer(q["id"], "Spaces")
        self.assertTrue(wait_until(lambda: app.store.messages(room)[-1].get("text") == "Spaces it is."))
        mine = [m for m in app.store.messages(room) if m["role"] == "user"][-1]
        self.assertEqual(mine["text"], "Spaces")
        self.assertEqual(mine["answer_to"], "Tabs or spaces?")
        self.assertIn("Tabs or spaces?", llm.calls[-1]["messages"][-1]["content"])


if __name__ == "__main__":
    unittest.main()
