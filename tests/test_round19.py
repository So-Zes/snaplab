"""Provider setup, protocol, routing and leak regressions, using only synthetic keys."""
import io
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from helpers import make_app, fake_credentials, onboard
from snaplab import setup, config
from snaplab.llm import LLM, ApiError, StreamCut, error_code
from snaplab.models import ModelRegistry, ConfigError
from snaplab.provider_protocol import prepare
from snaplab.proxy import Translator
from snaplab.slots import Slots
from tools.round19_provider import make_server
import urllib.request
import urllib.error

KEY = "synthetic-round19-provider-credential"
TOOL = {"type": "function", "function": {"name": "echo", "parameters": {"type": "object", "properties": {"value": {"type": "integer"}}}}}


class ProvidersTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.fake = fake_credentials()
        self.fake.__enter__()
        self.addCleanup(self.fake.__exit__, None, None, None)
        self.app, _, _ = make_app(tmp=self.temp.name, local_mode=True)
        self.addCleanup(self.app.archive.close)
        self.srv = make_server()
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)
        self.url = f"http://127.0.0.1:{self.srv.server_port}/v1"

    def client(self, model="ordinary", **caps):
        client = LLM({"base_url": self.url, "model": model, "effort_style": "none", "capabilities": caps}, self.app.slots)
        client.key = lambda: KEY
        return client

    def test_stream_tools_and_image_without_usage(self):
        client = self.client()
        msgs = [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA="}}]}]
        r = client.stream_chat(msgs, tools=[TOOL])
        self.assertEqual(json.loads(r.tool_calls[0]["arguments"]), {"value": 7})
        msgs += [{"role": "assistant", "content": "", "reasoning_content": "do not forward", "tool_calls": []},
                 {"role": "tool", "tool_call_id": r.tool_calls[0]["id"], "content": "7"}]
        self.assertEqual(client.stream_chat(msgs, tools=[TOOL]).content, "OK")
        self.assertEqual(r.input_tokens, 0)
        self.assertNotIn("reasoning_content", self.srv.requests[-1]["messages"][1])
        self.assertIn("image_url", json.dumps(self.srv.requests[0]))

    def test_alternate_limit_and_no_usage(self):
        for model, caps in (("alternate-limit", {"token_parameter": "max_completion_tokens"}),
                            ("no-usage", {"stream_usage": False})):
            with self.subTest(model=model):
                self.assertEqual(self.client(model, **caps).stream_chat([{"role": "user", "content": "hi"}]).content, "OK")

    def test_proxy_preserves_omitted_output_limit(self):
        # The existing Snapscribe Codex path did not impose an output cap.
        spec = {"effort_style": "chat_template_kwargs", "context_limit": 262144}
        body = {"messages": [{"role": "user", "content": "hello"}], "stream": True}
        self.assertNotIn("max_tokens", prepare(body, spec))
        self.assertEqual(prepare({**body, "max_tokens": 8192}, spec)["max_tokens"], 8192)
        self.assertEqual(prepare({**body, "max_tokens": 128}, {"capabilities": {
            "token_parameter": "max_completion_tokens"}})["max_completion_tokens"], 128)

    def test_later_provider_probe_completes_four_calls(self):
        from contextlib import redirect_stdout
        from tools.check_provider import main
        path = Path(self.temp.name) / "probe.json"
        path.write_text(json.dumps({"data_dir": str(Path(self.temp.name) / "probe-data"),
            "providers": {"probe": {"base_url": self.url, "api_key_env": "ROUND19_PROBE"}},
            "models": {"probe": {"provider": "probe", "model": "probe-check", "effort_style": "none"}}}), encoding="utf-8")
        output = io.StringIO()
        with patch.dict("os.environ", {"ROUND19_PROBE": KEY}), redirect_stdout(output), \
             patch("sys.argv", ["check_provider.py", "--config", str(path), "--model", "probe"]):
            self.assertEqual(main(), 0)
        result = json.loads(output.getvalue())
        self.assertTrue(result["ok"])
        self.assertEqual(result["calls"], 4)
        self.assertEqual(len(self.srv.requests), 4)
        self.assertNotIn(KEY, output.getvalue())

    def test_cut_is_not_success_or_retried(self):
        with self.assertRaises(StreamCut):
            self.client("cut").stream_chat([{"role": "user", "content": "hi"}])
        self.assertEqual(len(self.srv.requests), 1)

    def test_stream_error_and_truncated_tool_are_not_success(self):
        from snaplab.llm import read_stream
        error = io.BytesIO(('data: '+json.dumps({"error": {"code": 402, "message": KEY}})+'\n\n').encode())
        with self.assertRaises(ApiError) as caught: read_stream(error)
        self.assertEqual(error_code(caught.exception), "credit")
        self.assertNotIn(KEY, str(caught.exception))
        partial = io.BytesIO(b'data: {"choices":[{"delta":{"tool_calls":[{"function":{"name":"echo","arguments":"{}"}}]},"finish_reason":"length"}]}\n\ndata: [DONE]\n\n')
        self.assertFalse(read_stream(partial).finished)

    def test_redirect_is_never_followed(self):
        other = make_server()
        threading.Thread(target=other.serve_forever, daemon=True).start()
        self.srv.redirect = f"http://127.0.0.1:{other.server_port}/leak"
        try:
            with self.assertRaises(ApiError):
                self.client("redirect").stream_chat([{"role": "user", "content": "synthetic data"}])
            self.assertEqual(other.requests, [])
        finally:
            other.shutdown(); other.server_close()

    def test_errors_are_plain_codes(self):
        for status, code in ((401, "key"), (402, "credit"), (404, "retired"), (410, "retired"), (429, "busy")):
            with self.subTest(status=status), self.assertRaises(ApiError) as caught:
                self.client("error-" + str(status)).stream_chat([])
            self.assertEqual(error_code(caught.exception), code)
            self.assertNotIn(KEY, str(caught.exception))
        self.assertEqual(error_code(ApiError(400, "tools unsupported")), "tools")

    def test_capabilities_reject_before_network(self):
        for caps, messages, tools in (({"vision": False}, [{"role": "user", "content": [{"type": "image_url"}]}], None),
                                      ({"tools": False}, [], [TOOL]), ({"protocol": "responses"}, [], None)):
            with self.assertRaises(ApiError): self.client(**caps).stream_chat(messages, tools=tools)
        self.assertEqual(self.srv.requests, [])
        with self.assertRaises(ApiError):
            prepare({"messages": [{"role": "user", "content": "x" * 50000}]}, {"context_limit": 2048})

    def test_add_preserves_default_and_backs_up_metadata(self):
        original = json.loads(json.dumps(self.app.cfg))
        b = {"provider": "openrouter", "base_url": self.url, "model": "ordinary", "key": KEY}
        with patch("snaplab.credentials.write_credential") as save:
            setup.configure(self.app, b, add=True)
            setup.configure(self.app, {**b, "provider": "opencode", "model": "kimi-k2.5"}, add=True)
        self.assertEqual(save.call_count, 2)
        self.assertEqual(self.app.cfg["routing"], original["routing"])
        self.assertEqual(self.app.cfg["api"], original["api"])
        added = [n for n in self.app.cfg["models"] if n not in original["models"]]
        for name in added:
            self.assertIsNot(self.app.models.slots_for(name), self.app.slots)
            self.assertEqual(self.app.models.slots_for(name).count, 2)
        self.assertTrue(list(self.app.store.path().glob("provider.json.bak-*")))
        self.assertNotIn(KEY, self.app.store.path("provider.json").read_text(encoding="utf-8"))
        reloaded = config.load(Path(self.temp.name)/"absent.json", overrides={"data_dir": self.app.cfg["data_dir"]})
        self.assertTrue(set(added) <= reloaded["models"].keys())

    def test_opencode_catalog_and_nonchat_rejection(self):
        body = {"provider": "opencode", "base_url": self.url, "key": KEY}
        rows = setup.discover(self.app, body)["models"]
        self.assertEqual([m["supported"] for m in rows], [True, False, False])
        for model in ("gpt-5", "claude-sonnet-4-5", "unknown"):
            with self.assertRaises(ValueError), patch("snaplab.credentials.write_credential") as save:
                setup.configure(self.app, {**body, "model": model}, add=True)
            save.assert_not_called()

    def test_unknown_model_never_falls_back(self):
        with self.assertRaises(ConfigError): self.app.models.name_for("removed")
        with self.assertRaises(ConfigError): self.app.models.find("removed")

    def test_session_pins_its_bots_model(self):
        self.app.cfg["models"]["external"] = {"provider": "snapscribe", "model": "synthetic-external"}
        bot = onboard(self.app)["bot"]
        self.app.update_bot(bot["id"], {"model": "external"})
        project = self.app.create_project(name="Synthetic model selection", bot=bot["id"])
        with patch.object(self.app.sessions, "_start"):
            session = self.app.sessions.create(project, "bot", "A synthetic task.", bot=bot["id"])
        self.assertEqual(session["model"], "external")
        self.app.update_bot(bot["id"], {"model": "qwen"})
        self.assertEqual(self.app.store.session(session["id"])["model"], "external")

    def test_alternate_key_enables_harness_without_default_key(self):
        with patch.object(self.app.models, "has_key", side_effect=lambda name=None, kind="chat": name == "external"):
            self.app.cfg["models"]["external"] = {"provider": "snapscribe", "model": "synthetic-external"}
            self.assertTrue(self.app.models.has_any_key())
            self.assertTrue(self.app.sessions.harnesses["bot"].available()[0])

    def test_proxy_alternate_limit_tools_images_and_no_usage(self):
        slots = Slots(Path(self.temp.name)/"external", 2)
        proxy = Translator(port=0, api_cfg=self.app.cfg["api"], slots=slots, token="synthetic-job-token", key_fn=lambda: None, log_path=Path(self.temp.name)/"proxy.log",
            resolve=lambda _: {"name": "selected", "model": "alternate-limit", "base_url": self.url, "key": KEY,
                                "slots": slots, "user_agent": "test", "effort_style": "none",
                                "capabilities": {"token_parameter": "max_completion_tokens"}})
        self.assertTrue(proxy.start())
        try:
            body = {"model": "selected", "stream": False, "input": [{"role": "user", "content": [
                {"type": "input_text", "text": "Look"}, {"type": "input_image", "image_url": "data:image/png;base64,AAA="}]}],
                "tools": [{"type": "function", "name": "echo", "parameters": TOOL["function"]["parameters"]}]}
            req = urllib.request.Request(f"http://127.0.0.1:{proxy.port}/v1/responses", data=json.dumps(body).encode(),
                headers={"Authorization": "Bearer synthetic-job-token", "Content-Type": "application/json"})
            with urllib.request.urlopen(req) as response: result = json.load(response)
            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["output"][0]["type"], "function_call")
            self.assertIn("image_url", json.dumps(self.srv.requests[-1]))
            self.assertNotIn("chat_template_kwargs", self.srv.requests[-1])
        finally:
            proxy.stop()
