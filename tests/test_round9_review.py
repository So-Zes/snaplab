"""Review regressions: synthetic credentials, deterministic races and isolated files."""
import copy
import io
import os
import queue
import threading
import unittest
import urllib.error
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import quote

from helpers import fake_credentials
from snaplab.agent import LoopResult
from snaplab.guard import MARK, variants
from snaplab.llm import LLM, ApiError, api_key_values, get_api_key
from snaplab.tools import Toolbox
import test_round9


class ReviewTest(unittest.TestCase):
    setUp = test_round9.Round9Test.setUp
    group = test_round9.Round9Test.group
    records = test_round9.Round9Test.records
    rpc = test_round9.Round9Test.rpc
    call = test_round9.Round9Test.call

    def proposal(self, room, bot=None, kind="approval"):
        bot = bot or self.bot
        return self.app.chat.create_question(room, bot, question="Continue?", kind=kind,
            action={"type": "create_project", "name": "Synthetic proposal", "bot": bot["id"]}
            if kind == "approval" else None)

    @unittest.skipUnless(os.name == "nt", "Windows registry sources")
    def test_rotated_keys_from_all_sources_never_reach_either_web_tool(self):
        names = set(self.app.guard.extra_env) | {"SYNTHETIC_EXTRA_KEY"}
        self.app.guard.extra_env = sorted(names)
        env = {name: "synthetic-old-" + str(i) + "+/secret" for i, name in enumerate(sorted(names))}
        registry = dict(env)
        credentials = dict(env)
        with fake_credentials(env, registry, credentials) as winreg, patch.object(self.app, "read_page") as reader:
            self.app.guard.clean("warm the guard before rotation")
            registry.update({name: "synthetic-new-" + str(i) + "+/secret" for i, name in enumerate(sorted(names))})
            credentials.update({name: "synthetic-vault-" + str(i) + "+/secret" for i, name in enumerate(sorted(names))})
            for name in sorted(names):
                self.assertEqual(get_api_key(name), env[name])  # Authentication still prefers the old process value.
                self.assertEqual(api_key_values(name), {env[name], registry[name], credentials[name]})
                for secret in (env[name], registry[name], credentials[name]):
                    for value in variants(secret) | {quote(quote(secret, safe=""), safe="")}:
                        for tool, args in (("web_search", {"query": "research " + value}),
                                           ("read_page", {"url": "https://example.org/?q=" + value})):
                            result = self.call(tool, args)["result"]
                            self.assertTrue(result["isError"])
                            self.assertNotIn(value, str(result))
                self.assertTrue(any(c.args[1] == name for c in winreg.QueryValueEx.call_args_list))
            box = Toolbox(self.app, self.bot, session=self.session)
            for secret in list(env.values()) + list(registry.values()) + list(credentials.values()):
                with self.assertRaises(ValueError):
                    box.t_web_search(secret)
                with self.assertRaises(ValueError):
                    box.t_read_page("https://example.org/?q=" + quote(secret, safe=""))
            reader.assert_not_called()
        self.assertEqual(self.app.searcher.queries, [])
        self.assertFalse(any(r["kind"] == "tool" for r in self.records()))

    @unittest.skipUnless(os.name == "nt", "Windows registry sources")
    def test_exit_guard_and_privacy_filter_refresh_all_sources_without_waiting(self):
        env_name = self.app.guard.extra_env[0]
        old, new = "synthetic-process-secret", "synthetic-registry-secret"
        registry = {env_name: old}
        credentials = {env_name: old}
        with fake_credentials({env_name: old}, registry, credentials):
            self.app.guard.clean("warm before rotation")
            registry[env_name] = new
            credentials[env_name] = "synthetic-rotated-vault-secret"
            for value in (old, new, credentials[env_name]):
                self.assertTrue(self.app.guard.contains(value))
                self.assertEqual(self.app.guard.clean_obj({value: [value]}), {MARK: [MARK]})
                self.assertNotIn(value, self.app.redactor.clean(value))
                self.assertNotIn(value, self.app.redact_log(value))
                self.app.store.write_json("synthetic-secret.json", {"value": value})
                self.assertNotIn(value, self.app.store.path("synthetic-secret.json").read_text(encoding="utf-8"))
                path = Path(self.tmp.name) / "synthetic-secret.txt"
                path.write_text(value, encoding="utf-16-le")
                self.assertTrue(self.app.guard.clean_file(path))
                self.assertNotIn(value, path.read_text(encoding="utf-16-le"))
            registry.clear()
            credentials.clear()
            self.assertTrue(self.app.guard.contains(new))  # Previously seen values remain protected.
            self.assertTrue(self.app.guard.contains("synthetic-rotated-vault-secret"))

    @unittest.skipUnless(os.name == "nt", "Windows registry sources")
    def test_llm_request_and_error_guard_all_sources_even_without_a_redactor(self):
        name = "SYNTHETIC_LLM_KEY"
        old, new = "synthetic-llm-process-secret", "synthetic-llm-registry-secret"
        vault = "synthetic-llm-vault-secret"
        for redactor in (None, self.app.redactor):
            with self.subTest(shared_redactor=redactor is not None), fake_credentials({name: old}, {name: new}, {name: vault}):
                llm = LLM({"base_url": "https://upstream.invalid/v1", "model": "synthetic", "api_key_env": name},
                          SimpleNamespace(acquire=lambda **kw: nullcontext()), redactor=redactor)
                sent = []

                def upstream(req, **kw):
                    sent.append(req.data.decode())
                    return io.BytesIO(b'data: {"choices":[{"delta":{"content":"Done"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n')

                with patch("snaplab.llm.open_request", side_effect=upstream):
                    result = llm.stream_chat([{"role": "user", "content": old + " " + new + " " + vault}],
                        tools=[{"type": "function", "function": {"name": "example", "description": new + " " + vault}}])
                self.assertEqual(result.content, "Done")
                for secret in (old, new, vault):
                    self.assertNotIn(secret, sent[0])
                with patch("snaplab.llm.open_request", side_effect=urllib.error.URLError(old + " " + new + " " + vault)):
                    with self.assertRaises(ApiError) as raised:
                        llm.stream_chat([{"role": "user", "content": "hello"}])
                for secret in (old, new, vault):
                    self.assertNotIn(secret, str(raised.exception))

    def test_removed_bot_questions_and_approvals_are_cancelled_and_cannot_be_answered(self):
        room = self.group()
        removed = [self.proposal(room, kind=kind) for kind in ("question", "approval")]
        kept = [self.proposal(room, self.other, kind) for kind in ("question", "approval")]
        with patch.object(self.app.chat, "stop", wraps=self.app.chat.stop) as stop:
            self.app.update_room(room["id"], {"bots": [self.other["id"]]})
            stop.assert_called_once_with(room["id"])
        with patch.object(self.app, "create_project") as create, patch.object(self.app.chat, "post") as post:
            for q in removed:
                self.assertEqual(self.app.store.question(q["id"])["status"], "cancelled")
                with self.assertRaises(LookupError):
                    self.app.chat.answer(q["id"], "Approve" if q["kind"] == "approval" else "Answer")
            self.assertEqual(self.app.run_approval(removed[1], True)[0], "note.approval_invalid")
            create.assert_not_called()
            post.assert_not_called()
        for q in kept:
            self.assertEqual(self.app.store.question(q["id"])["status"], "open")
        self.assertFalse(self.app.ledger.approval_state(self.app.store.question(removed[1]["id"]))["can_accept"])
        self.app.update_room(room["id"], {"bots": room["bots"]})
        with self.assertRaises(LookupError):
            self.app.chat.answer(removed[1]["id"], "Approve")

    def test_answer_and_consumption_recheck_current_room_and_membership(self):
        for change in ("deleted", "removed"):
            with self.subTest(change=change):
                room = self.group()
                questions = [self.proposal(room, kind=kind) for kind in ("question", "approval")]
                # Bypass the cancellation path to represent legacy/stale stored cards.
                changed = copy.deepcopy(room)
                if change == "deleted":
                    changed["deleted"] = "synthetic deletion"
                else:
                    changed["bots"] = [self.other["id"]]
                self.app.store.save_room(changed)
                with patch.object(self.app, "create_project") as create, patch.object(self.app.chat, "post") as post:
                    for q in questions:
                        with self.assertRaises(LookupError):
                            self.app.chat.answer(q["id"], "Approve")
                        self.assertEqual(self.app.store.question(q["id"])["status"], "open")
                    q = questions[1]
                    q.update(status="answered", answer="Approve")
                    self.app.store.save_question(q)
                    self.assertEqual(self.app.run_approval(q, True)[0], "note.approval_invalid")
                    create.assert_not_called()
                    post.assert_not_called()

    def test_consumption_rejects_cancelled_unanswered_and_declined_cards(self):
        room = self.group()
        for status, answer in (("cancelled", "Approve"), ("open", None), ("answered", "Decline")):
            q = self.proposal(room)
            q.update(status=status, answer=answer)
            self.app.store.save_question(q)
            with patch.object(self.app, "create_project") as create:
                self.assertEqual(self.app.run_approval(q, True)[0], "note.approval_invalid")
                create.assert_not_called()

    def test_answer_read_and_approval_execution_are_ordered_before_concurrent_deletion(self):
        for pause_at in ("question_read", "approval_consumed"):
            with self.subTest(pause_at=pause_at):
                room = self.group()
                q = self.proposal(room)
                paused, resume, deleting, deleted = (threading.Event() for _ in range(4))
                failures, executed = [], []
                original_read = self.app.store.question
                original_consume = self.app.ledger.consume_approval

                def pause():
                    paused.set()
                    if not resume.wait(5):
                        raise AssertionError("test did not release the answer thread")

                def read(qid):
                    value = original_read(qid)
                    if threading.current_thread().name == "review-answer" and not paused.is_set():
                        pause()
                    return value

                def consume(*args):
                    result = original_consume(*args)
                    pause()
                    return result

                def answer():
                    try:
                        self.app.chat.answer(q["id"], "Approve")
                    except Exception as exc:
                        failures.append(exc)

                def delete():
                    deleting.set()
                    try:
                        self.app.update_room(room["id"], {"action": "delete", "confirmed": True})
                    except Exception as exc:
                        failures.append(exc)
                    finally:
                        deleted.set()

                def create(**kwargs):
                    executed.append(self.app.store.room(room["id"]) is not None)
                    return {"name": kwargs["name"]}

                target = self.app.store if pause_at == "question_read" else self.app.ledger
                attribute = "question" if pause_at == "question_read" else "consume_approval"
                with patch.object(target, attribute, side_effect=read if pause_at == "question_read" else consume), \
                     patch.object(self.app, "create_project", side_effect=create):
                    worker = threading.Thread(target=answer, name="review-answer")
                    deleter = threading.Thread(target=delete, name="review-delete")
                    worker.start()
                    try:
                        self.assertTrue(paused.wait(5))
                        deleter.start()
                        self.assertTrue(deleting.wait(5))
                        self.assertFalse(deleted.wait(0.1), "deletion must wait for the answer/action")
                    finally:
                        resume.set()
                        worker.join(5)
                        if deleter.ident is not None:
                            deleter.join(5)
                    self.assertFalse(worker.is_alive())
                    self.assertFalse(deleter.is_alive())
                self.assertEqual(failures, [])
                self.assertEqual(executed, [True])
                self.assertIsNone(self.app.store.room(room["id"]))
                with patch.object(self.app, "create_project") as create:
                    self.assertEqual(self.app.run_approval(q, True)[0], "note.approval_invalid")
                    create.assert_not_called()

    def test_deletion_first_cancels_pending_answer_and_stale_approval(self):
        room = self.group()
        q = self.proposal(room)
        # A previously read snapshot cannot reinstate approval after cancellation.
        stale = copy.deepcopy(q)
        self.app.update_room(room["id"], {"action": "delete", "confirmed": True})
        with patch.object(self.app, "create_project") as create:
            with self.assertRaises(LookupError):
                self.app.chat.answer(q["id"], "Approve")
            self.assertEqual(self.app.run_approval(stale, True)[0], "note.approval_invalid")
            create.assert_not_called()
        self.assertEqual(self.app.store.question(q["id"])["status"], "cancelled")

    def test_rename_with_same_members_preserves_running_reply_and_queue(self):
        room = self.group()
        started, resume = threading.Event(), threading.Event()
        results = []

        def replying(*args, **kwargs):
            started.set()
            if not resume.wait(5):
                raise AssertionError("test did not release reply")
            self.assertFalse(kwargs["cancel"]())
            return LoopResult("Still replying", "done", 1, 0, 0, 0)

        with patch("snaplab.chat.run_loop", side_effect=replying), \
             patch.object(self.app.chat, "stop", wraps=self.app.chat.stop) as stop:
            worker = threading.Thread(target=lambda: results.append(self.app.chat.run_turn(room["id"], self.bot["id"])))
            worker.start()
            try:
                self.assertTrue(started.wait(5))
                waiting = queue.Queue()
                queued = (self.other["id"], "synthetic-origin", 1, False)
                waiting.put(queued)
                self.app.chat.queues[room["id"]] = waiting
                for members in (room["bots"], list(reversed(room["bots"]))):
                    self.app.update_room(room["id"], {"name": "Renamed", "bots": members})
                    self.assertFalse(self.app.chat.cancel[room["id"]].is_set())
                    self.assertEqual(waiting.qsize(), 1)
                stop.assert_not_called()
                self.assertEqual(waiting.get_nowait(), queued)
            finally:
                resume.set()
                worker.join(5)
            self.assertFalse(worker.is_alive())
        self.assertEqual(results[0]["status"], "done")
        self.assertEqual(results[0]["text"], "Still replying")


if __name__ == "__main__":
    unittest.main()
