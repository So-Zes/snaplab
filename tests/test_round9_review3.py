"""Third review: executable tool contracts and deletion races replayed in the UI."""
import copy
import io
import json
from pathlib import Path
import subprocess
import threading
import unittest
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import patch

from helpers import fake_credentials
from snaplab.agent import LoopResult
from snaplab.llm import LLM
from snaplab.privacy import Redactor
import test_proxy_namespace as namespace
import test_round9


class SchemaPrivacyTest(unittest.TestCase):
    def test_schema_contract_names_and_selectors_survive_at_every_level(self):
        redactor = Redactor(home="/synthetic/home", username="Query", computer="",
                            extra_terms=["type", "string", "object", "properties", "required", "description",
                                         "metadata", "title", "query", "input", "format", "email"])
        leaf = {"type": "string", "description": "Query", "title": "Query", "$comment": "Query"}
        expected_leaf = {**leaf, "description": "[private]", "title": "[private]", "$comment": "[private]"}
        schema = {"type": "object", "required": ["query", "description"],
                  "dependentRequired": {"query": ["description"]}, "$ref": "#/$defs/query",
                  "$dynamicRef": "#query", "$id": "https://example.org/query", "$anchor": "query",
                  "format": "email", "pattern": "^query$", "additionalProperties": False}
        expected = copy.deepcopy(schema)
        for key in ("properties", "patternProperties", "$defs", "definitions", "dependentSchemas", "dependencies"):
            schema[key] = {"query": leaf, "description": leaf, "metadata": leaf}
            expected[key] = {"query": expected_leaf, "description": expected_leaf, "metadata": expected_leaf}
        schema["dependencies"]["required"] = ["query"]
        expected["dependencies"]["required"] = ["query"]
        for key in ("allOf", "anyOf", "oneOf", "prefixItems"):
            schema[key], expected[key] = [leaf, True], [expected_leaf, True]
        for key in ("items", "additionalItems", "unevaluatedItems", "unevaluatedProperties", "contains",
                    "propertyNames", "not", "if", "then", "else", "contentSchema"):
            schema[key], expected[key] = leaf, expected_leaf
        schema["properties"]["input"] = {"items": [leaf, False], "required": ["query"]}
        expected["properties"]["input"] = {"items": [expected_leaf, False], "required": ["query"]}
        schema["propertyNames"] = {"description": "Query", "anyOf": [{"const": "query"}, {"enum": ["input"]}]}
        expected["propertyNames"] = {**schema["propertyNames"], "description": "[private]"}
        before = copy.deepcopy(schema)
        for wrapper in ("parameters", "inputSchema", "schema"):
            with self.subTest(wrapper=wrapper):
                self.assertEqual(redactor.clean_obj({wrapper: schema}), {wrapper: expected})
                self.assertEqual(redactor.clean_obj({wrapper: False}), {wrapper: False})
        self.assertEqual(schema, before)

    def test_schema_user_values_are_still_redacted_without_mutating_the_input(self):
        redactor = Redactor(home="/synthetic/home", username="Query", computer="")
        schema = {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"],
                  "default": {"Query": "Query"}, "const": {"Query": "Query"},
                  "enum": ["Query"], "examples": [{"Query": {"name": "Query"}}]}
        before = copy.deepcopy(schema)
        cleaned = redactor.clean_obj({"parameters": schema})["parameters"]
        self.assertEqual(cleaned["properties"], schema["properties"])
        self.assertEqual(cleaned["required"], ["query"])
        self.assertEqual(cleaned["default"], {"Query": "snapuser"})
        self.assertEqual(cleaned["const"], {"Query": "snapuser"})
        self.assertEqual(cleaned["enum"], ["snapuser"])
        self.assertEqual(cleaned["examples"], [{"Query": {"name": "snapuser"}}])
        self.assertEqual(schema, before)


class ToolExecutionTest(unittest.TestCase):
    setUp = test_round9.Round9Test.setUp
    rpc = test_round9.Round9Test.rpc
    call = test_round9.Round9Test.call
    owner = namespace.NamespaceProxyTest.owner
    http_request = namespace.NamespaceProxyTest.http_request

    def test_returned_arguments_execute_real_mcp_tools_after_redaction(self):
        definitions = [t for t in self.rpc("tools/list")["result"]["tools"] if t["name"] != "generate_image"]
        for username, private_terms in (("Query", []), ("synthetic-user", ["query", "url", "count", "input",
                                                                                    "web_search", "read_page"])):
            for streaming in (True, False):
                with self.subTest(username=username, private_terms=bool(private_terms), stream=streaming):
                    owner = self.owner([], [])
                    owner.redactor = Redactor(home="/synthetic/home", username=username, computer="",
                                              extra_terms=private_terms, guard=self.app.guard)
                    body = {"model": "fake-qwen", "stream": streaming,
                            "input": [{"role": "user", "content": username}],
                            "tools": [{"type": "namespace", "name": "mcp__snaplab_web", "tools": [
                                {"type": "function", "name": tool["name"], "description": username,
                                 "parameters": tool["inputSchema"]} for tool in definitions]}]}
                    before = copy.deepcopy(body)
                    sent = []

                    def upstream(req, **kwargs):
                        sent.append(json.loads(req.data))
                        chunks = []
                        # Model double follows the definitions it actually receives.
                        for index, tool in enumerate(sent[-1]["tools"]):
                            function = tool["function"]
                            schema = function["parameters"]
                            value = "public sources" if function["name"].endswith("web_search") else "https://example.org/guide"
                            args = {key: value for key in schema["required"]}
                            if "count" in schema["properties"]:
                                args["count"] = 2
                            chunks.append(namespace.call_chunk(index, f"call-{index}", function["name"], json.dumps(args)))
                        chunks.append({"choices": [{"delta": {}, "finish_reason": "tool_calls"}]})
                        return namespace.sse(chunks)

                    with patch("snaplab.proxy.open_request", side_effect=upstream):
                        headers, raw = self.http_request(owner, body)
                    self.assertTrue(headers.startswith("HTTP/1.0 200"))
                    response = (list(namespace.iter_sse(io.BytesIO(raw)))[-1]["response"] if streaming else json.loads(raw))
                    self.assertEqual(response["status"], "completed")
                    self.assertEqual(len(response["output"]), 2)
                    for item in response["output"]:
                        self.assertEqual(item["namespace"], "mcp__snaplab_web")
                        result = self.call(item["name"], json.loads(item["arguments"]))
                        self.assertNotIn("error", result)
                        self.assertFalse(result["result"]["isError"])
                    self.assertEqual(self.app.searcher.queries[-1], "public sources")
                    for tool, original in zip(sent[0]["tools"], definitions):
                        self.assertEqual(tool["function"]["parameters"], original["inputSchema"])
                        self.assertNotEqual(tool["function"]["description"], username)
                    self.assertEqual(body, before)

    def test_normal_chat_preserves_tool_names_and_executes_returned_arguments(self):
        redactor = Redactor(home="/synthetic/home", username="Query", computer="", extra_terms=["web_search"])
        definition = {"type": "function", "function": {"name": "web_search", "description": "Query",
                      "parameters": next(tool["inputSchema"] for tool in self.rpc("tools/list")["result"]["tools"]
                                         if tool["name"] == "web_search")}}
        sent = []

        def upstream(req, **kwargs):
            sent.append(json.loads(req.data))
            function = sent[-1]["tools"][0]["function"]
            args = {key: "public sources" for key in function["parameters"]["required"]}
            return namespace.sse([namespace.call_chunk(0, "search", function["name"], json.dumps(args)),
                                  {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]}])

        with fake_credentials({"SYNTHETIC_KEY": "synthetic-request-secret"}), \
             patch("snaplab.llm.open_request", side_effect=upstream):
            llm = LLM({"base_url": "https://upstream.invalid", "model": "synthetic", "api_key_env": "SYNTHETIC_KEY"},
                      SimpleNamespace(acquire=lambda **kw: nullcontext()), redactor=redactor)
            reply = llm.stream_chat([{"role": "user", "content": "Query"}], tools=[definition])
        self.assertEqual(sent[0]["tools"][0]["function"]["description"], "snapuser")
        self.assertEqual(sent[0]["messages"][0]["content"], "snapuser")
        self.assertEqual(len(reply.tool_calls), 1)
        call = reply.tool_calls[0]
        result = self.call(call["name"], json.loads(call["arguments"]))
        self.assertFalse(result["result"]["isError"])
        self.assertEqual(self.app.searcher.queries, ["public sources"])
        self.assertEqual(definition["function"]["description"], "Query")


class RoomNotificationTest(unittest.TestCase):
    setUp = test_round9.Round9Test.setUp
    group = test_round9.Round9Test.group

    def replay(self, rooms, room, notifications):
        fixture = {"rooms": rooms, "bots": self.app.store.bots(), "deleted": room["id"], "events": notifications}
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run(["node", str(root / "tests" / "round9_room_events.mjs")], cwd=root,
                                input=json.dumps(fixture), text=True, encoding="utf-8", capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_delete_after_final_room_read_cannot_overtake_its_notification(self):
        for pause_at in ("read", "publish"):
            with self.subTest(pause_at=pause_at):
                room = self.group()
                rooms = self.app.store.rooms()
                paused, resume, attempted, deleted, final = (threading.Event() for _ in range(5))
                notifications, failures, blocked = [], [], []
                lock = self.app.chat.answer_lock

                class ObservedLock:
                    def __enter__(self):
                        if threading.current_thread() is remover:
                            acquired = lock.acquire(blocking=False)
                            blocked.append(not acquired)
                            attempted.set()
                            if acquired:
                                return self
                        lock.acquire()
                        return self

                    def __exit__(self, *args):
                        lock.release()

                def pause():
                    paused.set()
                    if not resume.wait(5):
                        raise AssertionError("test did not release final room notification")

                read_room = self.app.store.room

                def read(rid):
                    snapshot = read_room(rid)
                    if threading.current_thread() is worker and final.is_set() and pause_at == "read":
                        pause()  # The old snapshot already exists when deletion is requested.
                    return snapshot

                def publish(event, data):
                    if event == "message.done":
                        final.set()
                    if threading.current_thread() is worker and event == "room.updated" and pause_at == "publish":
                        pause()
                    notifications.append({"type": event, **copy.deepcopy(data)})

                def reply():
                    try:
                        self.app.chat.run_turn(room["id"], self.bot["id"])
                    except Exception as exc:
                        failures.append(exc)

                def remove():
                    try:
                        self.app.update_room(room["id"], {"action": "delete", "confirmed": True})
                    except Exception as exc:
                        failures.append(exc)
                    finally:
                        deleted.set()

                worker, remover = threading.Thread(target=reply), threading.Thread(target=remove)
                with patch.object(self.app.chat, "answer_lock", ObservedLock()), \
                     patch.object(self.app.store, "room", side_effect=read), \
                     patch.object(self.app.bus, "publish", side_effect=publish), \
                     patch("snaplab.chat.run_loop", return_value=LoopResult("Done.", "done", 1, 0, 0, 0)):
                    worker.start()
                    try:
                        self.assertTrue(paused.wait(5))
                        remover.start()
                        self.assertTrue(attempted.wait(5))
                        if not blocked[0]:
                            self.assertTrue(deleted.wait(5))  # Reproduce the old failure deterministically.
                    finally:
                        resume.set()
                        worker.join(5)
                        if remover.ident is not None:
                            remover.join(5)
                self.assertFalse(worker.is_alive())
                self.assertFalse(remover.is_alive())
                self.assertEqual(failures, [])
                self.assertIsNone(self.app.store.room(room["id"]))
                self.replay(rooms, room, notifications)
                self.assertEqual(blocked, [True])
                self.assertEqual([ev["type"] for ev in notifications if ev["type"] in {"room.updated", "rooms.updated"}],
                                 ["room.updated", "rooms.updated"])

    def test_deletion_before_final_room_read_emits_no_room_update(self):
        room = self.group()
        rooms, notifications = self.app.store.rooms(), []

        def run(*args, **kwargs):
            self.app.update_room(room["id"], {"action": "delete", "confirmed": True})
            return LoopResult("Done.", "done", 1, 0, 0, 0)

        with patch("snaplab.chat.run_loop", side_effect=run), patch.object(self.app.bus, "publish",
                side_effect=lambda event, data: notifications.append({"type": event, **copy.deepcopy(data)})):
            self.app.chat.run_turn(room["id"], self.bot["id"])
        self.assertFalse(any(ev["type"] == "room.updated" for ev in notifications))
        self.assertTrue(any(ev["type"] == "message.done" for ev in notifications))
        self.assertIsNone(self.app.store.room(room["id"]))
        self.replay(rooms, room, notifications)
