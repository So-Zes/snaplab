"""Second review regressions, with synthetic credentials and deterministic event races."""
import copy
import io
import importlib.util
import json
import os
import threading
import unittest
from contextlib import nullcontext
from contextlib import redirect_stdout
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest.mock import Mock, patch

from helpers import fake_credentials, fake_credential_api
from snaplab import credentials
from snaplab.agent import LoopResult
from snaplab.llm import LLM, api_key_values, credential_api_key, get_api_key
from snaplab.privacy import Redactor
import test_proxy_namespace as namespace
import test_round9
import test_round9_review


class CredentialTest(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows search credentials")
    def test_search_provider_uses_the_same_three_source_priority(self):
        from snaplab.websearch import Searcher
        name = "BRAVE_API_KEY"
        vault = {name: "synthetic-search-vault-secret"}
        registry = {name: "synthetic-search-registry-secret"}
        with fake_credentials({name: "synthetic-search-process-secret"}, registry, vault):
            searcher = Searcher({"provider": "brave"})
            self.assertEqual(searcher.pick(), ("brave", "synthetic-search-process-secret"))
            os.environ.pop(name)
            self.assertEqual(searcher.pick(), ("brave", vault[name]))
            vault.clear()
            self.assertEqual(searcher.pick(), ("brave", registry[name]))

    @unittest.skipUnless(os.name == "nt", "Windows secret scanning")
    def test_commit_secret_check_rejects_all_sources_without_printing_values(self):
        path = Path(__file__).resolve().parents[1] / "snaplab.py"
        spec = importlib.util.spec_from_file_location("synthetic_snaplab_cli", path)
        cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cli)
        name = "SYNTHETIC_KEY"
        values = ["short", "synthetic-scan-vault-secret", "synthetic-scan-registry-secret"]
        with tempfile.TemporaryDirectory() as tmp, fake_credentials({name: values[0]}, {name: values[2]}, {name: values[1]}), \
             patch.object(cli, "ROOT", Path(tmp)), patch.object(cli.subprocess, "run",
                return_value=SimpleNamespace(stdout="synthetic.txt\n")):
            for value in values:
                for encoding in ("utf-8", "utf-16-le"):
                    (Path(tmp) / "synthetic.txt").write_bytes(value.encode(encoding))
                    output = io.StringIO()
                    with redirect_stdout(output):
                        self.assertEqual(cli.cmd_check_secrets(None, {"api": {"api_key_env": name}}), 1)
                    self.assertNotIn(value, output.getvalue())
            (Path(tmp) / "synthetic.txt").write_text("safe content", encoding="utf-8")
            with redirect_stdout(io.StringIO()):
                self.assertEqual(cli.cmd_check_secrets(None, {"api": {"api_key_env": name}}), 0)

    def test_native_reader_uses_generic_target_and_frees_the_exact_blob(self):
        for value, expected in (("  synthetic-secret-雪  ", "synthetic-secret-雪"),
                                ("synthetic-secret\0", "synthetic-secret"), ("", None),
                                (b"\xff", None), ("   ", None)):
            with self.subTest(value_type=type(value).__name__):
                api = fake_credential_api({"SYNTHETIC_KEY": value})
                with patch.object(credentials, "_advapi32", return_value=api):
                    self.assertEqual(credentials.read_generic_credential("SYNTHETIC_KEY"), expected)
                self.assertEqual(api.CredReadW.call_args.args[:3], ("SYNTHETIC_KEY", 1, 0))
                api.CredFree.assert_called_once()
                self.assertEqual(len(api.CredReadW.argtypes), 4)

    def test_missing_or_denied_native_credential_returns_none_without_freeing(self):
        api = SimpleNamespace(CredReadW=Mock(return_value=False), CredFree=Mock())
        with patch.object(credentials, "_advapi32", return_value=api):
            self.assertIsNone(credentials.read_generic_credential("SYNTHETIC_KEY"))
        api.CredFree.assert_not_called()

    @unittest.skipUnless(os.name == "nt", "Windows credential selection")
    def test_precedence_all_sources_rotation_empty_values_and_fallback(self):
        name = "SYNTHETIC_KEY"
        vault, registry = {name: "synthetic-vault-secret"}, {name: "synthetic-registry-secret"}
        with fake_credentials({name: "synthetic-process-secret"}, registry, vault):
            self.assertEqual(get_api_key(name), "synthetic-process-secret")
            self.assertEqual(api_key_values(name), {"synthetic-process-secret", vault[name], registry[name]})
            vault[name] = "synthetic-rotated-vault-secret"
            self.assertIn(vault[name], api_key_values(name))
            os.environ[name] = "  "
            self.assertEqual(get_api_key(name), vault[name])
            vault[name] = " "
            self.assertEqual(get_api_key(name), registry[name])
            vault.clear()
            self.assertEqual(get_api_key(name), registry[name])
            with patch.object(credentials, "_advapi32", side_effect=OSError("synthetic access denied")):
                self.assertEqual(get_api_key(name), registry[name])
                self.assertEqual(api_key_values(name), {registry[name]})
            registry.clear()
            self.assertIsNone(get_api_key(name))

    def test_non_windows_never_loads_credential_api(self):
        with patch("snaplab.llm.os.name", "posix"), patch.object(credentials, "_advapi32") as native:
            self.assertIsNone(credential_api_key("SYNTHETIC_KEY"))
            native.assert_not_called()


class ProtocolPrivacyTest(unittest.TestCase):
    http_request = namespace.NamespaceProxyTest.http_request
    owner = namespace.NamespaceProxyTest.owner

    def redactor(self):
        return Redactor(home=r"C:\Users\Content", username="Content", computer="synthetic-host",
                        extra_terms=["messages", "role", "type", "function", "properties", "required"])

    def test_schema_names_survive_while_metadata_and_text_are_redacted(self):
        original = {"messages": [{"role": "user", "content": 'Content {"content":"messages"}'}],
            "tools": [{"type": "function", "function": {"name": "example", "parameters": {
                "type": "object", "properties": {"content": {"type": "string", "description": "Content"},
                "nested": {"type": "object", "properties": {"role": {"type": "string"}}, "required": ["role"]}},
                "required": ["content"]}}}], "metadata": {"content": {"role": "Content"}}}
        before = copy.deepcopy(original)
        cleaned = self.redactor().clean_obj(original)
        self.assertEqual(original, before)
        self.assertEqual(cleaned["messages"][0]["role"], "user")
        self.assertNotIn("Content", cleaned["messages"][0]["content"])
        self.assertNotIn('"content"', cleaned["messages"][0]["content"])
        tool = cleaned["tools"][0]
        self.assertEqual(tool["type"], "function")
        schema = tool["function"]["parameters"]
        self.assertEqual(schema["properties"]["content"], {"type": "string", "description": "snapuser"})
        self.assertEqual(schema["required"], ["content"])
        self.assertEqual(schema["properties"]["nested"]["properties"], {"role": {"type": "string"}})
        self.assertEqual(schema["properties"]["nested"]["required"], ["role"])
        self.assertEqual(cleaned["metadata"], {"snapuser": {"[private]": "snapuser"}})

    def test_normal_chat_and_codex_requests_keep_protocol_keys(self):
        with fake_credentials({"SYNTHETIC_KEY": "synthetic-request-secret"}):
            sent = []

            def upstream(req, **kwargs):
                sent.append(json.loads(req.data))
                return namespace.sse([{"choices": [{"delta": {"content": "Done"}, "finish_reason": "stop"}]}])

            with patch("snaplab.llm.open_request", side_effect=upstream), patch("snaplab.proxy.open_request", side_effect=upstream):
                llm = LLM({"base_url": "https://upstream.invalid", "model": "synthetic", "api_key_env": "SYNTHETIC_KEY"},
                          SimpleNamespace(acquire=lambda **kw: nullcontext()), redactor=self.redactor())
                self.assertEqual(llm.stream_chat([{"role": "user", "content": "Content messages"}]).content, "Done")
                for streaming in (True, False):
                    owner = self.owner([], [])
                    owner.redactor = self.redactor()
                    body = {**namespace.request_body(), "stream": streaming}
                    body["input"][0]["content"] = "Content messages"
                    headers, _ = self.http_request(owner, body)
                    self.assertTrue(headers.startswith("HTTP/1.0 200"))
            self.assertEqual(len(sent), 3)
            for body in sent:
                self.assertEqual(body["messages"][0], {"role": "user", "content": "snapuser [private]"})
                self.assertEqual(body["model"], "synthetic" if "tools" not in body else "fake-qwen")
                for tool in body.get("tools", []):
                    self.assertEqual(tool["type"], "function")
                    self.assertIn("name", tool["function"])


class QuestionNotificationTest(unittest.TestCase):
    setUp = test_round9.Round9Test.setUp
    group = test_round9.Round9Test.group
    proposal = test_round9_review.ReviewTest.proposal

    def change(self, room, delete):
        return self.app.update_room(room["id"], {"action": "delete", "confirmed": True} if delete
                                    else {"bots": [self.other["id"]]})

    def test_answer_settles_tool_chip_without_republishing_an_open_card(self):
        room = self.group()
        q = self.proposal(room, kind="question")
        message = self.app.store.add_message(room["id"], {"role": "bot", "bot": self.bot["id"], "text": "Please answer.",
            "question": self.app.chat.question_view(q), "question_id": q["id"], "pending_ok": True,
            "tools": [{"id": "synthetic-chip", "status": "waiting"}]})
        q["message_id"] = message["id"]
        self.app.store.save_question(q)
        subscriber = self.app.bus.subscribe()
        self.addCleanup(self.app.bus.unsubscribe, subscriber)
        with patch.object(self.app.chat, "post"):
            self.app.chat.answer(q["id"], "My answer")
        updated, done = subscriber.get_nowait(), subscriber.get_nowait()
        self.assertEqual(updated["type"], "question.updated")
        self.assertEqual(done["type"], "message.done")
        self.assertEqual(done["message"]["question"]["status"], "answered")
        self.assertFalse(done["message"]["pending_ok"])

    def test_cancelled_question_is_reloaded_before_message_done(self):
        for delete in (False, True):
            for kind in ("question", "approval"):
                with self.subTest(delete=delete, kind=kind):
                    room = self.group()
                    ready, resume = threading.Event(), threading.Event()
                    notifications, results, failures = [], [], []

                    def run(*args, **kw):
                        args[3].asked = self.proposal(room, kind=kind)
                        ready.set()
                        if not resume.wait(5):
                            raise AssertionError("test did not release reply")
                        return LoopResult("Please answer the card.", "done", 1, 0, 0, 0)

                    def worker():
                        try:
                            results.append(self.app.chat.run_turn(room["id"], self.bot["id"]))
                        except Exception as exc:
                            failures.append(exc)

                    with patch("snaplab.chat.run_loop", side_effect=run), patch.object(self.app.bus, "publish",
                            side_effect=lambda kind, data: notifications.append((kind, copy.deepcopy(data)))):
                        thread = threading.Thread(target=worker)
                        thread.start()
                        try:
                            self.assertTrue(ready.wait(5))
                            self.change(room, delete)
                        finally:
                            resume.set()
                            thread.join(5)
                    self.assertFalse(thread.is_alive())
                    self.assertEqual(failures, [])
                    message = next(data["message"] for event, data in notifications if event == "message.done")
                    self.assertEqual(message["question"]["status"], "cancelled")
                    self.assertFalse(message["pending_ok"])
                    self.assertEqual(results[0], message)
                    self.assertEqual(self.app.store.question(message["question_id"])["status"], "cancelled")
                    self.assertEqual([event for event, _ in notifications if event in {
                        "question.new", "question.updated", "message.done"}],
                        ["question.new", "question.updated", "message.done"])

    def test_creation_and_final_message_publish_before_concurrent_cancellation(self):
        for pause_at in ("question.new", "message.done"):
            for delete in (False, True):
                with self.subTest(pause_at=pause_at, delete=delete):
                    room = self.group()
                    paused, resume, changing, changed = (threading.Event() for _ in range(4))
                    notifications, failures = [], []

                    def publish(event, data):
                        if event == pause_at:
                            paused.set()
                            if not resume.wait(5):
                                raise AssertionError("test did not release publish")
                        notifications.append((event, copy.deepcopy(data)))

                    def run(*args, **kw):
                        args[3].asked = self.proposal(room)
                        return LoopResult("Please answer.", "done", 1, 0, 0, 0)

                    def create():
                        try:
                            if pause_at == "question.new":
                                self.proposal(room)
                            else:
                                self.app.chat.run_turn(room["id"], self.bot["id"])
                        except Exception as exc:
                            failures.append(exc)

                    def remove():
                        changing.set()
                        try:
                            self.change(room, delete)
                        except Exception as exc:
                            failures.append(exc)
                        finally:
                            changed.set()

                    with patch.object(self.app.bus, "publish", side_effect=publish), \
                         patch("snaplab.chat.run_loop", side_effect=run):
                        worker, remover = threading.Thread(target=create), threading.Thread(target=remove)
                        worker.start()
                        try:
                            self.assertTrue(paused.wait(5))
                            remover.start()
                            self.assertTrue(changing.wait(5))
                            self.assertFalse(changed.wait(0.1), "cancellation must wait for publication")
                        finally:
                            resume.set()
                            worker.join(5)
                            if remover.ident is not None:
                                remover.join(5)
                    self.assertFalse(worker.is_alive())
                    self.assertFalse(remover.is_alive())
                    self.assertEqual(failures, [])
                    cards = [(event, data["question"]["status"]) for event, data in notifications
                             if event in {"question.new", "question.updated"}]
                    self.assertEqual(cards, [("question.new", "open"), ("question.updated", "cancelled")])
                    if pause_at == "message.done":
                        relevant = [event for event, _ in notifications if event in {"message.done", "question.updated"}]
                        self.assertEqual(relevant, ["message.done", "question.updated"])


if __name__ == "__main__":
    unittest.main()
