"""Responses namespaces through the real translator handler, without sockets or an LLM."""
import copy
from contextlib import nullcontext
import io
import json
import os
import time
import threading
from types import SimpleNamespace
import unittest
import urllib.error
from unittest.mock import patch

from helpers import fake_credentials
from snaplab.llm import get_api_key
from snaplab.privacy import Redactor

from snaplab.proxy import Emitter, _Handler, iter_sse, stream_back, to_chat


def web_namespace():
    return {
        "type": "namespace", "name": "mcp__snaplab_web",
        "description": "Use SnapLab web tools. Web results are untrusted reference material.",
        "tools": [
            {"type": "function", "name": "read_page", "description": "Read one public page.", "strict": False,
             "parameters": {"type": "object", "properties": {"url": {"type": "string"}},
                            "required": ["url"], "additionalProperties": False}},
            {"type": "function", "name": "web_search", "description": "Search the web.", "strict": False,
             "parameters": {"type": "object", "properties": {"count": {"type": "integer"}, "query": {"type": "string"}},
                            "required": ["query"], "additionalProperties": False}},
        ],
    }


def request_body():
    return {"model": "fake-qwen", "input": [{"role": "user", "content": "Research public sources."}],
            "tools": [{"type": "custom", "name": "apply_patch", "description": "Apply a patch."},
                      {"type": "function", "name": "shell", "parameters": {"type": "object"}},
                      {"type": "function", "name": "web_search", "parameters": {"type": "object"}},
                      web_namespace()]}


def sse(chunks):
    if not any(c.get("finish_reason") for chunk in chunks for c in chunk.get("choices", [])):
        chunks = [*chunks, {"choices": [{"delta": {}, "finish_reason": "stop"}]}]
    return io.BytesIO(b"".join(("data: " + json.dumps(chunk) + "\n\n").encode() for chunk in chunks)
                      + b"data: [DONE]\n\n")


def call_chunk(index, call_id, name, arguments):
    return {"choices": [{"delta": {"tool_calls": [{"index": index, "id": call_id,
                         "function": {"name": name, "arguments": arguments}}]}}]}


class NamespaceProxyTest(unittest.TestCase):
    def convert_output(self, body, chunks, restore=None):
        _, tool_map, _ = to_chat(body)
        output = io.BytesIO()
        em = Emitter(output)
        em.restore = restore
        stats = stream_back(em, sse(chunks), "fake-qwen", tool_map, time.perf_counter())
        events = list(iter_sse(io.BytesIO(output.getvalue())))
        self.assertEqual([e["sequence_number"] for e in events], list(range(len(events))))
        return events, stats

    def test_namespace_definitions_keep_descriptions_parameters_and_other_tools(self):
        body = request_body()
        before = copy.deepcopy(body)
        chat, _, _ = to_chat(body)
        definitions = {t["function"]["name"]: t["function"] for t in chat["tools"]}
        self.assertEqual(set(definitions), {"apply_patch", "shell", "web_search",
                                          "mcp__snaplab_web__web_search", "mcp__snaplab_web__read_page"})
        for child in body["tools"][-1]["tools"]:
            actual = definitions["mcp__snaplab_web__" + child["name"]]
            self.assertEqual(actual["parameters"], child["parameters"])
            self.assertIs(actual["strict"], False)
            self.assertIn(child["description"], actual["description"])
            self.assertIn(body["tools"][-1]["description"], actual["description"])
        self.assertEqual(definitions["apply_patch"]["parameters"]["required"], ["input"])
        self.assertEqual(body, before)

    def test_aliases_avoid_plain_custom_namespace_and_long_name_collisions(self):
        body = {"tools": [
            {"type": "function", "name": "a__b__c"},
            {"type": "custom", "name": "a__c"},
            {"type": "namespace", "name": "a", "tools": [{"type": "function", "name": "b__c"},
                                                          {"type": "function", "name": "c"}]},
            {"type": "namespace", "name": "a__b", "tools": [{"type": "function", "name": "c"}]},
            {"type": "namespace", "name": "n" * 64, "tools": [{"type": "function", "name": "x" * 64},
                                                               {"type": "function", "name": "x" * 63 + "y"}]},
        ]}
        chat, mapping, _ = to_chat(body)
        names = [t["function"]["name"] for t in chat["tools"]]
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(names[:2], ["a__b__c", "a__c"])
        for name in names:
            self.assertRegex(name, r"^[A-Za-z0-9_-]{1,64}$")
        reversed_body = {"tools": list(reversed(body["tools"]))}
        self.assertEqual(to_chat(reversed_body)[1].aliases, mapping.aliases)
        chunks = [call_chunk(i, f"call_{i}", name, '{"input":"patch"}' if i == 1 else "{}")
                  for i, name in enumerate(names)]
        events, _ = self.convert_output(body, chunks)
        items = events[-1]["response"]["output"]
        self.assertEqual([(it.get("namespace"), it["name"]) for it in items],
                         [(None, "a__b__c"), (None, "a__c"), ("a", "b__c"), ("a", "c"),
                          ("a__b", "c"), ("n" * 64, "x" * 64), ("n" * 64, "x" * 63 + "y")])

    def test_history_and_forced_choice_use_the_declared_alias_and_call_id(self):
        body = request_body()
        body["tool_choice"] = {"type": "function", "namespace": "mcp__snaplab_web", "name": "read_page"}
        body["input"] += [
            {"type": "function_call", "namespace": "mcp__snaplab_web", "name": "web_search",
             "arguments": '{"query":"public sources"}', "call_id": "search-1"},
            {"type": "function_call_output", "call_id": "search-1",
             "output": [{"type": "input_text", "text": "A public source."}]},
        ]
        chat, _, _ = to_chat(body)
        self.assertEqual(chat["tool_choice"], {"type": "function", "function": {"name": "mcp__snaplab_web__read_page"}})
        self.assertEqual(chat["messages"][-2]["tool_calls"], [{"id": "search-1", "type": "function", "function": {
            "name": "mcp__snaplab_web__web_search", "arguments": '{"query":"public sources"}'}}])
        self.assertEqual(chat["messages"][-1], {"role": "tool", "tool_call_id": "search-1", "content": "A public source."})
        body["tools"] = []
        history_only, _, _ = to_chat(body)
        self.assertEqual(history_only["messages"], chat["messages"])
        self.assertNotIn("tools", history_only)

    def test_stream_restores_namespace_on_added_done_and_completed_items(self):
        events, stats = self.convert_output(request_body(), [
            call_chunk(0, "search-2", "mcp__snaplab_", '{"query":'),
            call_chunk(1, "page-2", "mcp__snaplab_web__read_page", '{"url":"https://example.org"}'),
            call_chunk(0, None, "web__web_search", '"Codex"}'),
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}],
             "usage": {"prompt_tokens": 11, "completion_tokens": 7}},
        ])
        expected = [("search-2", "web_search", '{"query":"Codex"}'),
                    ("page-2", "read_page", '{"url":"https://example.org"}')]
        for kind in ("response.output_item.added", "response.output_item.done", "response.completed"):
            items = (events[-1]["response"]["output"] if kind == "response.completed" else
                     [e["item"] for e in events if e["type"] == kind])
            self.assertEqual([(it["call_id"], it["name"], it["arguments"]) for it in items], expected)
            self.assertTrue(all(it["namespace"] == "mcp__snaplab_web" and it["type"] == "function_call" for it in items))
        self.assertEqual(stats["tool_calls"], 2)
        self.assertEqual(events[-1]["response"]["usage"]["total_tokens"], 18)

    def test_plain_and_namespaced_custom_tools_keep_their_own_type_and_restore_paths(self):
        body = request_body()
        body["tools"] += [{"type": "namespace", "name": "editor", "tools": [
            {"type": "custom", "name": "apply_patch"}, {"type": "function", "name": "shell"}]}]
        events, _ = self.convert_output(body, [
            call_chunk(0, "patch-1", "apply_patch", '{"input":"replace PLACEHOLDER"}'),
            call_chunk(1, "patch-2", "editor__apply_patch", '{"input":"replace PLACEHOLDER"}'),
            call_chunk(2, "shell-1", "shell", '{"path":"PLACEHOLDER"}'),
            call_chunk(3, "shell-2", "editor__shell", '{"path":"PLACEHOLDER"}'),
        ], restore=lambda text: text.replace("PLACEHOLDER", "local-path"))
        items = events[-1]["response"]["output"]
        self.assertEqual([i["type"] for i in items], ["custom_tool_call", "custom_tool_call", "function_call", "function_call"])
        self.assertEqual([i.get("namespace") for i in items], [None, "editor", None, "editor"])
        self.assertEqual([i.get("input") for i in items[:2]], ["replace local-path"] * 2)
        self.assertEqual([i["arguments"] for i in items[2:]], ['{"path":"local-path"}'] * 2)
        body["input"] += items + [{"type": "custom_tool_call_output", "call_id": "patch-2", "output": "applied"}]
        chat, _, _ = to_chat(body)
        self.assertEqual([c["function"]["name"] for c in chat["messages"][-2]["tool_calls"]],
                         ["apply_patch", "editor__apply_patch", "shell", "editor__shell"])
        self.assertEqual(chat["messages"][-1], {"role": "tool", "tool_call_id": "patch-2", "content": "applied"})

    def test_codex_default_namespace_does_not_rename_plain_tools(self):
        for namespace in (None, "", "functions"):
            with self.subTest(namespace=namespace):
                body = request_body()
                body["input"] += [{"type": "function_call", "name": "shell", "namespace": namespace,
                                   "arguments": "{}", "call_id": "shell-1"}]
                body["tool_choice"] = {"type": "function", "name": "shell", "namespace": namespace}
                chat, _, _ = to_chat(body)
                self.assertEqual(chat["messages"][-1]["tool_calls"][0]["function"]["name"], "shell")
                self.assertEqual(chat["tool_choice"]["function"]["name"], "shell")

    def http_request(self, owner, body, disconnect_on=None):
        data = json.dumps(body).encode()
        raw = (b"POST /v1/responses HTTP/1.0\r\nHost: 127.0.0.1\r\nAuthorization: Bearer test-token\r\n"
               + f"Content-Length: {len(data)}\r\nContent-Type: application/json\r\n\r\n".encode() + data)

        class Connection:
            def __init__(self): self.output = bytearray()
            def settimeout(self, value): pass
            def makefile(self, *args): return io.BytesIO(raw)
            def sendall(self, value):
                if disconnect_on and disconnect_on in value:
                    raise BrokenPipeError("synthetic client disconnected")
                self.output.extend(value)

        connection = Connection()
        handler = type("NamespaceHandler", (_Handler,), {"owner": owner})
        handler(connection, ("127.0.0.1", 12345), SimpleNamespace(server_port=18432))
        headers, data = bytes(connection.output).split(b"\r\n\r\n", 1)
        return headers.decode(), data

    def owner(self, logs, usage):
        return SimpleNamespace(web_mcp=None, token="test-token", redactor=None, timeout=5,
                               cancelled=threading.Event(), _active_lock=threading.Lock(), _active=set(),
                               log=logs.append, usage=lambda *args: usage.append(args),
                               target=lambda model: {"key": "synthetic-key", "name": model, "model": "fake-qwen",
                                   "base_url": "https://upstream.invalid/v1", "user_agent": "test",
                                   "slots": SimpleNamespace(acquire=lambda **kwargs: nullcontext())})

    def test_http_search_page_and_final_answer_in_streaming_and_json_modes(self):
        for streaming in (True, False):
            with self.subTest(stream=streaming):
                logs, usage, sent = [], [], []
                owner = self.owner(logs, usage)
                body = request_body()
                body["stream"] = streaming
                scripted = [
                    call_chunk(0, "search-http", "mcp__snaplab_web__web_search", '{"query":"Codex"}'),
                    call_chunk(0, "page-http", "mcp__snaplab_web__read_page", '{"url":"https://example.org"}'),
                    {"choices": [{"delta": {"content": "A sourced answer."}, "finish_reason": "stop"}]},
                ]

                def upstream(req, **kwargs):
                    sent.append(json.loads(req.data))
                    return sse([scripted[len(sent) - 1], {"usage": {"prompt_tokens": 11, "completion_tokens": 7}}])

                with patch("snaplab.proxy.open_request", side_effect=upstream):
                    for turn in range(3):
                        headers, data = self.http_request(owner, body)
                        self.assertTrue(headers.startswith("HTTP/1.0 200"))
                        if streaming:
                            self.assertIn("Content-Type: text/event-stream", headers)
                            response = list(iter_sse(io.BytesIO(data)))[-1]["response"]
                        else:
                            self.assertIn("Content-Type: application/json", headers)
                            response = json.loads(data)
                        self.assertEqual(response["usage"]["total_tokens"], 18)
                        self.assertEqual(response["status"], "completed")
                        item = response["output"][0]
                        if turn < 2:
                            self.assertEqual(item["namespace"], "mcp__snaplab_web")
                            self.assertEqual(item["name"], ("web_search", "read_page")[turn])
                            body["input"] += [item, {"type": "function_call_output", "call_id": item["call_id"],
                                                     "output": "Public source text."}]
                        else:
                            self.assertEqual(item["content"][0]["text"], "A sourced answer.")
                for request in sent[1:]:
                    self.assertEqual(request["messages"][-1]["role"], "tool")
                    call = request["messages"][-2]["tool_calls"][0]
                    self.assertEqual(request["messages"][-1]["tool_call_id"], call["id"])
                    self.assertTrue(call["function"]["name"].startswith("mcp__snaplab_web__"))
                self.assertEqual(len(logs), 3)
                for record in logs:
                    self.assertEqual(record["sent_tool_kinds"], ["custom", "function", "namespace"])
                    self.assertEqual(record["tools"], 5)
                    self.assertNotIn("TEMP_namespace_sample", record)
                    self.assertNotIn("untrusted reference", json.dumps(record))
                    self.assertNotIn("synthetic-key", json.dumps(record))
                self.assertEqual(usage, [(11, 7, True, "fake-qwen")] * 3)

    def test_json_mode_returns_conversion_failure_as_json(self):
        logs, usage = [], []
        body = request_body()
        body["stream"] = False
        with patch("snaplab.proxy.open_request", return_value=sse([{"choices": [{"delta": {"tool_calls": 1}}]}])):
            headers, data = self.http_request(self.owner(logs, usage), body)
        self.assertTrue(headers.startswith("HTTP/1.0 502"))
        self.assertIn("Content-Type: application/json", headers)
        self.assertEqual(json.loads(data)["error"]["code"], "server_error")
        self.assertEqual(usage, [(0, 0, False, "fake-qwen")])

    def test_namespace_stream_disconnect_never_completes_a_partial_call(self):
        for streaming in (True, False):
            for failure in (None, TimeoutError, ConnectionResetError):
                for name, arguments in (("mcp__snaplab_", '{"query":'),
                                         ("mcp__snaplab_web__web_search", '{"query":"unfinished'),
                                         ("mcp__snaplab_web__web_search", '{"query":"complete JSON"}')):
                    with self.subTest(stream=streaming, failure=failure, name=name, arguments=arguments):
                        chunks = [call_chunk(0, "cut-call", name, arguments)]
                        payload = b"".join(("data: " + json.dumps(c) + "\n\n").encode() for c in chunks)

                        class CutStream(io.BytesIO):
                            def __next__(self):
                                try:
                                    return super().__next__()
                                except StopIteration:
                                    if failure:
                                        raise failure("synthetic upstream disconnect")
                                    raise

                        upstream = CutStream(payload)
                        logs, usage = [], []
                        body = {**request_body(), "stream": streaming}
                        with patch("snaplab.proxy.open_request", return_value=upstream):
                            headers, data = self.http_request(self.owner(logs, usage), body)
                        self.assertTrue(upstream.closed)
                        if streaming:
                            self.assertTrue(headers.startswith("HTTP/1.0 200"))
                            events = list(iter_sse(io.BytesIO(data)))
                            self.assertEqual(events[-1]["type"], "response.failed")
                            self.assertFalse(any(e["type"] in ("response.completed", "response.output_item.done",
                                                               "response.output_item.added") for e in events))
                        else:
                            self.assertTrue(headers.startswith("HTTP/1.0 502"))
                            self.assertEqual(json.loads(data)["error"]["code"], "server_error")
                        self.assertNotIn(b'"call_id"', data)
                        self.assertEqual(logs[-1]["status"], 502)
                        self.assertEqual(usage, [(0, 0, False, "fake-qwen")])

    def test_client_disconnect_during_namespace_output_closes_upstream(self):
        upstream = sse([call_chunk(0, "client-cut", "mcp__snaplab_web__web_search", '{"query":"public"}')])
        logs, usage = [], []
        with patch("snaplab.proxy.open_request", return_value=upstream):
            _, data = self.http_request(self.owner(logs, usage), request_body(), b"response.output_item.added")
        self.assertTrue(upstream.closed)
        self.assertNotIn(b"response.completed", data)
        self.assertEqual(logs[-1]["status"], 499)
        self.assertEqual(usage, [(0, 0, False, "fake-qwen")])

    def test_generation_limit_or_filter_never_returns_executable_calls(self):
        for streaming in (True, False):
            for finish, reason in (("length", "max_output_tokens"), ("content_filter", "content_filter")):
                for name, arguments in (("mcp__snaplab_web__web_search", '{"query":"unfinished'),
                                        ("editor__apply_patch", '{"input":"unfinished'),
                                        ("editor__apply_patch", '{"input":"complete JSON"}')):
                    with self.subTest(stream=streaming, finish=finish, name=name, arguments=arguments):
                        body = {**request_body(), "stream": streaming}
                        body["tools"].append({"type": "namespace", "name": "editor",
                                              "tools": [{"type": "custom", "name": "apply_patch"}]})
                        upstream = sse([
                            {"choices": [{"delta": {"content": "Partial answer"}}]},
                            call_chunk(0, "complete-call", "shell", "{}"),
                            call_chunk(1, "cut-call", name, arguments),
                            {"choices": [{"delta": {}, "finish_reason": finish}],
                             "usage": {"prompt_tokens": 11, "completion_tokens": 7}}])
                        logs, usage = [], []
                        with patch("snaplab.proxy.open_request", return_value=upstream):
                            headers, data = self.http_request(self.owner(logs, usage), body)
                        self.assertTrue(upstream.closed)
                        self.assertTrue(headers.startswith("HTTP/1.0 200"))
                        if streaming:
                            events = list(iter_sse(io.BytesIO(data)))
                            self.assertEqual(events[-1]["type"], "response.incomplete")
                            self.assertNotIn("response.completed", [e["type"] for e in events])
                            self.assertFalse(any(e.get("item", {}).get("type") in {
                                "function_call", "custom_tool_call"} for e in events))
                            response = events[-1]["response"]
                            self.assertEqual(response["id"], events[0]["response"]["id"])
                        else:
                            response = json.loads(data)
                        self.assertEqual(response["status"], "incomplete")
                        self.assertEqual(response["incomplete_details"], {"reason": reason})
                        self.assertEqual(response["usage"]["total_tokens"], 18)
                        self.assertEqual(len(response["output"]), 1)
                        self.assertEqual(response["output"][0]["status"], "incomplete")
                        self.assertEqual(response["output"][0]["content"][0]["text"], "Partial answer")
                        self.assertNotIn(b'"call_id"', data)
                        self.assertNotIn(b"unfinished", data)
                        self.assertEqual(logs[-1]["response_status"], "incomplete")
                        self.assertEqual(logs[-1]["finish_reason"], finish)
                        self.assertEqual(logs[-1]["tool_calls"], 0)
                        self.assertEqual(usage, [(11, 7, False, "fake-qwen")])

    def test_done_marker_without_a_successful_finish_reason_does_not_complete(self):
        for streaming in (True, False):
            for finish in (None, "unknown"):
                with self.subTest(stream=streaming, finish=finish):
                    chunk = call_chunk(0, "missing-finish", "shell", "{}")
                    chunk["choices"][0]["finish_reason"] = finish
                    upstream = io.BytesIO(("data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n").encode())
                    logs, usage = [], []
                    with patch("snaplab.proxy.open_request", return_value=upstream):
                        headers, data = self.http_request(self.owner(logs, usage), {**request_body(), "stream": streaming})
                    if streaming:
                        self.assertEqual(list(iter_sse(io.BytesIO(data)))[-1]["type"], "response.failed")
                    else:
                        self.assertTrue(headers.startswith("HTTP/1.0 502"))
                    self.assertNotIn(b'"call_id"', data)
                    self.assertEqual(logs[-1]["status"], 502)
                    self.assertEqual(usage, [(0, 0, False, "fake-qwen")])

    @unittest.skipUnless(os.name == "nt", "Windows registry sources")
    def test_proxy_guards_all_key_sources_in_requests_and_errors(self):
        name = "SYNTHETIC_PROXY_KEY"
        old, new = "synthetic-proxy-process-secret", "synthetic-proxy-registry-secret"
        vault = "synthetic-proxy-vault-secret"
        for with_redactor in (True, False):
            for streaming in (True, False):
                for error_at in ("connect", "http_error", "stream"):
                    with self.subTest(redactor=with_redactor, stream=streaming, error_at=error_at), \
                         fake_credentials({name: old}, {name: new}, {name: vault}):
                        logs, usage, sent = [], [], []
                        owner = self.owner(logs, usage)
                        target = {**owner.target("fake-qwen"), "api_key_env": name, "key": get_api_key(name)}
                        owner.target = lambda model: target
                        owner.redactor = Redactor() if with_redactor else None
                        body = {**request_body(), "stream": streaming}
                        body["input"][0]["content"] = old + " " + new + " " + vault
                        body["tools"][-1]["description"] = new + " " + vault

                        class FailedStream(io.BytesIO):
                            def __next__(self):
                                raise OSError(old + " " + new + " " + vault)

                        def upstream(req, **kwargs):
                            sent.append(req.data.decode())
                            if error_at == "connect":
                                raise urllib.error.URLError(old + " " + new + " " + vault)
                            if error_at == "http_error":
                                raise urllib.error.HTTPError(req.full_url, 400, "synthetic error", {},
                                                             io.BytesIO((old + " " + new + " " + vault).encode()))
                            return FailedStream()

                        with patch("snaplab.proxy.open_request", side_effect=upstream):
                            _, data = self.http_request(owner, body)
                        self.assertEqual(len(sent), 1)
                        for secret in (old, new, vault):
                            self.assertNotIn(secret, sent[0])
                            self.assertNotIn(secret, data.decode())
                            self.assertNotIn(secret, json.dumps(logs))
                        self.assertEqual(usage, [(0, 0, False, "fake-qwen")])


if __name__ == "__main__":
    unittest.main()
