"""Extension points: providers and models, tool plugins, the harness contract and UI languages.

A small local server stands in for a second OpenAI-compatible provider, so these tests never call a real model.
"""
import json
import os
import re
import tempfile
import threading
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from helpers import ROOT, make_app, onboard, wait_until

from snaplab import config
from snaplab import models as modelsmod
from snaplab import plugins as plugmod
from snaplab.harnesses import contract
from snaplab.harnesses import load as load_harnesses
from snaplab.harnesses.base import Harness, TurnResult
from snaplab.harnesses.bot import BotHarness
from snaplab.harnesses.codex import CodexHarness
from snaplab.harnesses.tests import TestsHarness
from snaplab.llm import StreamCut, apply_effort
from snaplab.slots import Slots
from snaplab.tools import builtin_tool_names

TEST_KEY_ENV = "SNAPLAB_TEST_OTHER_KEY"
TEST_KEY = "test-key-for-the-fake-provider"


class FakeProvider:
    """An OpenAI-compatible /v1/chat/completions that streams a fixed answer and records what it received.
    Under /cut/v1 the stream ends without its end marker (a dropped connection)."""

    def __init__(self):
        self.requests = []
        owner = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
                owner.requests.append({"path": self.path, "body": body, "auth": self.headers.get("Authorization"),
                                       "ua": self.headers.get("User-Agent")})
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                chunks = [{"choices": [{"delta": {"content": "Hello "}}]}, {"choices": [{"delta": {"content": "there"}}]}]
                if not self.path.startswith("/cut/"):
                    chunks.append({"choices": [{"delta": {}, "finish_reason": "stop"}],
                                   "usage": {"prompt_tokens": 7, "completion_tokens": 2}})
                for c in chunks:
                    self.wfile.write(f"data: {json.dumps(c)}\n\n".encode())
                if not self.path.startswith("/cut/"):
                    self.wfile.write(b"data: [DONE]\n\n")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def url(self, prefix=""):
        return f"http://127.0.0.1:{self.port}{prefix}/v1"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def two_provider_cfg(tmp: str, fake: FakeProvider, **routing) -> dict:
    return config.load(path=Path(tmp) / "none.json", overrides={
        "api": {"model": "qwen3.8-27b", "api_key_env": "SNAPSCRIBE_API_KEY"},
        "data_dir": str(Path(tmp) / "data"), "slots": {"dir": str(Path(tmp) / "locks"), "count": 3},
        "providers": {"other": {"base_url": fake.url(), "api_key_env": TEST_KEY_ENV, "slots": {"count": 2},
                                "user_agent": "snaplab-test"},
                      "other_cut": {"base_url": fake.url("/cut"), "api_key_env": TEST_KEY_ENV}},
        "models": {"fast": {"provider": "other", "model": "tiny-1", "effort_style": "reasoning_effort",
                            "context_limit": 16384, "label": "Tiny"},
                   "plain": {"provider": "other", "model": "tiny-2", "effort_style": "none"},
                   "cut": {"provider": "other_cut", "model": "tiny-1"}},
        "routing": routing})


class ModelRegistryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fake = FakeProvider()
        os.environ[TEST_KEY_ENV] = TEST_KEY

    @classmethod
    def tearDownClass(cls):
        cls.fake.close()
        os.environ.pop(TEST_KEY_ENV, None)

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="snaplab-models-")
        self.fake.requests.clear()

    def registry(self, **routing):
        cfg = two_provider_cfg(self.tmp, self.fake, **routing)
        return cfg, modelsmod.ModelRegistry(cfg, Slots(Path(self.tmp) / "locks", 3))

    def test_legacy_api_block_becomes_the_default_model(self):
        cfg = config.load(path=Path(self.tmp) / "none.json", overrides={"data_dir": str(Path(self.tmp) / "data"), "api": {"model": "qwen3.8-27b", "api_key_env": "SNAPSCRIBE_API_KEY"}})
        self.assertEqual(cfg["models"]["qwen"]["model"], "qwen3.8-27b")
        self.assertEqual(cfg["models"]["qwen"]["effort_style"], "chat_template_kwargs")
        self.assertEqual(cfg["providers"]["snapscribe"]["api_key_env"], "SNAPSCRIBE_API_KEY")
        self.assertEqual({k: cfg["routing"][k] for k in ("default", "chat", "session", "codex")},
                         {"default": "qwen", "chat": "qwen", "session": "qwen", "codex": "qwen"})
        self.assertNotIn("key", json.dumps(cfg["providers"]).replace("api_key_env", "").replace("key_expires", ""))

    def test_routing_per_kind_and_per_bot(self):
        cfg, reg = self.registry(chat="fast", session="plain")
        self.assertEqual(reg.name_for(None, "chat"), "fast")
        self.assertEqual(reg.name_for(None, "session"), "plain")
        self.assertEqual(reg.name_for(None, "codex"), "qwen")          # falls back to default
        self.assertEqual(reg.name_for("plain", "chat"), "plain")       # a bot's own choice wins
        with self.assertRaises(modelsmod.ConfigError):
            reg.name_for("no-such-model", "chat")
        self.assertEqual(reg.find("tiny-2"), "plain")                  # provider model id -> registry name
        self.assertEqual(reg.spec("fast")["context_limit"], 16384)
        self.assertIn(TEST_KEY_ENV, reg.key_envs())
        self.assertIn("SNAPSCRIBE_API_KEY", reg.key_envs())
        names = {m["name"] for m in reg.view()}
        self.assertEqual(names, {"qwen", "fast", "plain", "cut"})
        self.assertNotIn(TEST_KEY, json.dumps(reg.view()))

    def test_second_provider_is_called_with_its_own_model_key_and_effort_style(self):
        cfg, reg = self.registry(chat="fast")
        got = []
        r = reg.get(None, "chat").stream_chat([{"role": "user", "content": "hi"}], effort="low", on_content=got.append)
        self.assertEqual(r.content, "Hello there")
        self.assertEqual("".join(got), "Hello there")
        req = self.fake.requests[-1]
        self.assertEqual(req["path"], "/v1/chat/completions")
        self.assertEqual(req["body"]["model"], "tiny-1")
        self.assertEqual(req["body"]["reasoning_effort"], "low")
        self.assertNotIn("chat_template_kwargs", req["body"])
        self.assertEqual(req["auth"], f"Bearer {TEST_KEY}")
        self.assertEqual(req["ua"], "snaplab-test")
        reg.get("plain", "chat").stream_chat([{"role": "user", "content": "hi"}], effort="medium")
        body = self.fake.requests[-1]["body"]
        self.assertEqual(body["model"], "tiny-2")
        self.assertNotIn("reasoning_effort", body)
        self.assertNotIn("chat_template_kwargs", body)
        # a provider with its own slot limit keeps its own lock folder
        self.assertEqual(reg.slots_for("fast").count, 2)
        self.assertTrue(str(reg.slots_for("fast").dir).endswith(str(Path("locks") / "other")))
        self.assertIsNot(reg.slots_for("cut"), reg.slots_for("qwen"))

    def test_cut_stream_raises_with_the_partial_text(self):
        cfg, reg = self.registry()
        with self.assertRaises(StreamCut) as cm:
            reg.get("cut").stream_chat([{"role": "user", "content": "hi"}])
        self.assertEqual(cm.exception.partial.content, "Hello there")

    def test_config_problems_are_reported(self):
        cfg = two_provider_cfg(self.tmp, self.fake)
        cfg["models"]["broken"] = {"provider": "nowhere", "model": ""}
        cfg["routing"]["chat"] = "missing"
        cfg["providers"]["nourl"] = {"api_key_env": "X"}
        problems = " | ".join(modelsmod.check(cfg))
        self.assertIn("unknown provider 'nowhere'", problems)
        self.assertIn("has no model id", problems)
        self.assertIn("unknown model 'missing'", problems)
        self.assertIn("'nourl' needs a base_url", problems)
        with self.assertRaises(modelsmod.ConfigError):
            modelsmod.ModelRegistry(cfg, Slots(Path(self.tmp) / "locks", 3))

    def test_effort_styles(self):
        b = {}
        apply_effort(b, "low", "chat_template_kwargs")
        self.assertEqual(b, {"chat_template_kwargs": {"reasoning_effort": "low"}})
        b = {}
        apply_effort(b, "high", "reasoning_effort")
        self.assertEqual(b, {"reasoning_effort": "high"})
        b = {}
        apply_effort(b, "medium", "none")
        self.assertEqual(b, {})
        b = {}
        apply_effort(b, "server", "chat_template_kwargs")
        self.assertEqual(b, {})

    def test_translator_routes_codex_to_the_chosen_model(self):
        from snaplab.proxy import Translator
        cfg, reg = self.registry(codex="fast")

        def resolve(ref):
            name = reg.find(ref)
            return {**reg.spec(name), "key": TEST_KEY, "slots": reg.slots_for(name)}
        tr = Translator(port=0, api_cfg=cfg["api"], slots=Slots(Path(self.tmp) / "locks", 3), key_fn=lambda: None,
                        token="tok", log_path=Path(self.tmp) / "tr.jsonl", resolve=resolve)
        self.assertTrue(tr.start())
        try:
            port = tr.server.server_address[1]
            body = {"model": "fast", "stream": True, "reasoning": {"effort": "medium"},
                    "input": [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]}]}
            req = urllib.request.Request(f"http://127.0.0.1:{port}/v1/responses", data=json.dumps(body).encode(),
                                         method="POST", headers={"Content-Type": "application/json",
                                                                 "Authorization": "Bearer tok"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                out = resp.read().decode()
            self.assertIn("Hello there", out.replace('"delta":"', "").replace('"', "") + json.dumps(out))
            sent = self.fake.requests[-1]
            self.assertEqual(sent["body"]["model"], "tiny-1")
            self.assertEqual(sent["auth"], f"Bearer {TEST_KEY}")
            self.assertNotIn("chat_template_kwargs", sent["body"])
            self.assertIn(sent["body"].get("reasoning_effort"), ("low", "medium", "high"))
        finally:
            tr.stop()

    def test_bot_model_choice_is_used_for_its_turns(self):
        os.environ[TEST_KEY_ENV] = TEST_KEY
        tmp = tempfile.mkdtemp(prefix="snaplab-botmodel-")
        cfg = two_provider_cfg(tmp, self.fake)
        app, llm, _ = make_app(tmp=tmp, providers=cfg["providers"], models=cfg["models"], routing={"chat": "qwen"})
        app.models.override = None                      # use the real registry for this test
        room = onboard(app)["room"]["id"]
        bot = app.store.bots()[0]
        app.update_bot(bot["id"], {"model": "fast"})
        self.assertEqual(app.store.bot(bot["id"])["model"], "fast")
        app.chat.post(room, "hello")
        self.assertTrue(wait_until(lambda: any(m.get("text") == "Hello there" for m in app.store.messages(room)), 10))
        self.assertEqual(self.fake.requests[-1]["body"]["model"], "tiny-1")
        self.assertTrue(wait_until(lambda: "fast" in app.store.usage()["total"].get("by_model", {}), 3))


TOOL_GOOD = '''
TOOL = {"name": "shout", "description": "Upper-case a text.", "approval": "always",
        "parameters": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
        "label": {"en": "Shouted", "ja": "叫んだ"}}
def run(ctx, text):
    return {"text": text.upper(), "bot": ctx.bot["name"]}
'''
TOOL_CLASH = '''
TOOL = {"name": "web_search", "description": "pretends to be a built-in"}
def run(ctx, **a):
    return "x"
'''
TOOL_SYNTAX = "TOOL = {\n"
TOOL_NORUN = 'TOOL = {"name": "norun", "description": "no run function"}\n'
TOOL_BADSCOPE = '''
TOOL = {"name": "anywhere", "description": "bad scope", "scope": ["everywhere"]}
def run(ctx):
    return "x"
'''


class PluginToolTest(unittest.TestCase):
    def test_example_calculator_is_loaded_and_works_in_chat(self):
        from test_core import script, wait_reply
        app, llm, _ = make_app(script({"tool_calls": [{"name": "calculator", "arguments": {"expression": "12.5 * (3 + 4) / 2"}}]},
                                      {"content": "It is 43.75."}))
        self.assertIn("calculator", app.plugin_tools)
        room = onboard(app)["room"]["id"]
        app.chat.post(room, "compute")
        self.assertTrue(wait_reply(app, room))
        self.assertIn("calculator", llm.calls[0]["tools"])
        tool_msg = [m for m in llm.calls[1]["messages"] if m["role"] == "tool"][-1]
        self.assertIn("43.75", tool_msg["content"])
        bot_msg = app.store.messages(room)[-1]
        self.assertEqual(bot_msg["text"], "It is 43.75.")

    def test_calculator_refuses_code(self):
        calc = plugmod.load({"data_dir": tempfile.mkdtemp(), "tool_dirs": [str(ROOT / "plugins" / "tools")]}, set())["calculator"]
        self.assertEqual(calc.run(None, expression="2 ** 10")["value"], 1024)
        for bad in ("__import__('os').system('x')", "open('f')", "(1).__class__", "9 ** 99999"):
            with self.assertRaises(ValueError):
                calc.run(None, expression=bad)

    def test_folder_plugins_are_validated(self):
        d = Path(tempfile.mkdtemp(prefix="snaplab-plugins-"))
        for name, text in {"good.py": TOOL_GOOD, "clash.py": TOOL_CLASH, "syntax.py": TOOL_SYNTAX, "norun.py": TOOL_NORUN,
                           "badscope.py": TOOL_BADSCOPE, "_private.py": TOOL_GOOD.replace("shout", "hidden")}.items():
            (d / name).write_text(text, encoding="utf-8")
        logs = []
        found = plugmod.load({"data_dir": str(d), "tool_dirs": [str(d)]}, builtin_tool_names(), log=logs.append)
        self.assertEqual(set(found), {"shout"})
        text = "\n".join(logs)
        for f in ("clash.py", "syntax.py", "norun.py", "badscope.py"):
            self.assertIn(f, text)
        self.assertIn("already used", text)
        self.assertEqual(found["shout"].label["ja"], "叫んだ")

    def test_plugin_with_approval_waits_for_the_card(self):
        from test_core import script, wait_reply
        d = Path(tempfile.mkdtemp(prefix="snaplab-plugins-"))
        (d / "good.py").write_text(TOOL_GOOD, encoding="utf-8")
        app, llm, _ = make_app(script({"tool_calls": [{"name": "shout", "arguments": {"text": "hi"}}]},
                                      {"content": "Waiting."}), tool_dirs=[str(d)])
        room = onboard(app)["room"]["id"]
        app.chat.post(room, "shout")
        self.assertTrue(wait_reply(app, room))
        q = app.store.questions()[0]
        self.assertEqual(q["kind"], "approval")
        self.assertEqual(q["action"]["type"], "plugin")
        app.chat.answer(q["id"], "Approve")
        self.assertTrue(wait_until(lambda: any("HI" in (m.get("text") or "") for m in app.store.messages(room))))
        boot = app.bootstrap() if hasattr(app, "bootstrap") else None
        if boot:
            self.assertEqual(boot["tool_labels"]["shout"]["en"], "Shouted")


class NoSignature(Harness):
    id = "nosig"
    label = {"en": "No signature"}

    def run_turn(self, session, prompt):
        return TurnResult(ok=True)


class BadId(Harness):
    id = "Bad Id!"
    label = {"en": "Bad"}

    def run_turn(self, session, folder, prompt, ctl, first):
        return TurnResult(ok=True)


class NoLabel(Harness):
    id = "nolabel"
    label = {"ja": "名前だけ"}

    def run_turn(self, session, folder, prompt, ctl, first):
        return TurnResult(ok=True)


class Recorder:
    """A stand-in for the session Control object (what SnapLab passes to run_turn as ctl)."""

    def __init__(self):
        self.events, self.fields = [], {}

    def emit(self, ev):
        self.events.append(ev)

    def update(self, **kw):
        self.fields.update(kw)

    def cancelled(self):
        return False

    def set_process(self, p):
        pass


class HarnessContractTest(unittest.TestCase):
    def test_builtin_harnesses_follow_the_contract(self):
        app, _, _ = make_app()
        for cls in (CodexHarness, BotHarness, TestsHarness):
            h = cls(app)
            self.assertEqual(contract.problems(h), [], cls.__name__)
            info = h.info()
            for k in ("id", "label", "description", "available", "reason", "runs_code", "icon"):
                self.assertIn(k, info, f"{cls.__name__} info lacks {k}")
            self.assertIn("en", info["label"])
            self.assertIn("ja", info["label"])          # built-ins carry both shipped languages
        self.assertEqual(set(load_harnesses(app)), {"codex", "bot", "tests"})

    def test_broken_harnesses_are_not_loaded(self):
        app, _, _ = make_app()
        self.assertTrue(any("run_turn must take" in p for p in contract.problems(NoSignature(app))))
        self.assertTrue(any("id must be" in p for p in contract.problems(BadId(app))))
        self.assertTrue(any("label must be" in p for p in contract.problems(NoLabel(app))))
        app2, _, _ = make_app(harness_plugins=["test_extend:NoSignature", "test_extend:BadId", "helpers_plugin:Safe",
                                               "no_such_module:X"])
        self.assertEqual(set(load_harnesses(app2)), {"codex", "bot", "tests", "safe"})

    def test_bot_harness_turn_returns_a_turn_result(self):
        from test_core import script
        app, llm, _ = make_app(script({"content": "Read the brief. Done."}))
        onboard(app)
        p = app.create_project(name="Contract", description="d")
        s = app.sessions.create(p, "bot", "say done")
        self.assertTrue(wait_until(lambda: app.store.session(s["id"])["status"] in ("review", "failed"), 5))
        s = app.store.session(s["id"])
        self.assertEqual(s["status"], "review")
        self.assertEqual(s["last_message"], "Read the brief. Done.")
        self.assertTrue(s.get("result"))
        self.assertEqual(llm.calls[0]["kind"], "session")

    def test_tests_harness_without_a_test_command_fails_cleanly(self):
        app, _, _ = make_app()
        h = TestsHarness(app)
        onboard(app)
        p = app.create_project(name="No tests", description="d")
        res = h.run_turn({"id": "s-00000000", "project": p["id"]}, Path(tempfile.mkdtemp()), "run", Recorder(), True)
        self.assertIsInstance(res, TurnResult)
        self.assertFalse(res.ok)
        self.assertIn("no test command", res.error)


class LanguageFilesTest(unittest.TestCase):
    """UI strings live in web/i18n/<code>.js; every language has the same keys as English."""

    def keys(self, code):
        text = (ROOT / "web" / "i18n" / f"{code}.js").read_text(encoding="utf-8")
        return set(re.findall(r'^\s*"([a-z0-9_.]+)"\s*:', text, re.M))

    def test_every_language_has_every_english_key(self):
        files = sorted(p.stem for p in (ROOT / "web" / "i18n").glob("*.js") if p.stem != "core")
        self.assertIn("en", files)
        self.assertIn("ja", files)
        en = self.keys("en")
        self.assertGreater(len(en), 200)
        for code in files:
            have = self.keys(code)
            self.assertEqual(sorted(en - have), [], f"{code}.js is missing keys")
            extras = {k for k in have - en if not (k.rsplit('.', 1)[-1] in {'one', 'few', 'many', 'other'}
                      and k.rsplit('.', 1)[0] in en)}
            self.assertEqual(sorted(extras), [], f"{code}.js has unknown keys")
            for k in ("lang.name", "lang.hello"):
                self.assertIn(k, have)

    def test_server_lists_languages_from_the_files(self):
        from snaplab.i18n import languages
        langs = languages()
        self.assertEqual([x["code"] for x in langs][:2], ["en", "ja"])
        self.assertEqual({x["code"]: x["name"] for x in langs}["ja"], "日本語")

    def test_a_third_language_needs_only_a_file(self):
        from snaplab import i18n
        d = ROOT / "web" / "i18n"
        en = (d / "en.js").read_text(encoding="utf-8")
        fr = d / "xx.js"
        try:
            fr.write_text(en.replace("window.SNAPLAB_I18N.en =", "window.SNAPLAB_I18N.xx =")
                          .replace('"lang.name": "English"', '"lang.name": "Test language"')
                          .replace('"lang.english": "English"', '"lang.english": "Testish"'), encoding="utf-8")
            self.assertIn("xx", i18n.codes())
            self.assertEqual(i18n.valid("xx"), "xx")
            self.assertIn(b"window.SNAPLAB_I18N.xx =", i18n.bundle())
            from snaplab.prompts import lang_name, persona_prompt
            self.assertEqual(lang_name("xx"), "Testish")
            self.assertIn("Language: Testish", persona_prompt(name="N", tone="friendly", call_user="Sam", lang="xx",
                                                              orchestrator=True))
            app, _, _ = make_app()
            r = app.onboard(lang="xx", name="Nova", color="#2F6FED", tone="friendly", tone_custom="", call_user="Sam")
            self.assertEqual(app.store.settings()["lang"], "xx")
            self.assertTrue(r["room"])
        finally:
            fr.unlink(missing_ok=True)
        self.assertNotIn("xx", i18n.codes())
        self.assertEqual(i18n.valid("xx"), "en")


class OrchestratorTest(unittest.TestCase):
    """F3: the orchestrator writes a project's BRIEF and hands the project to a teammate, who answers after reading it."""

    def test_write_brief_and_assign_project(self):
        from test_core import script
        brief = "# Tiny game\n\n## Goal\nA tiny text adventure.\n\n## Done when\n- three rooms\n"
        seen = {}

        def responder(messages, tools, effort):
            sys_prompt = messages[0]["content"]
            if "You are Marlin" in sys_prompt:
                return next(orch)
            seen["teammate_system"] = sys_prompt
            seen["teammate_last"] = messages[-1]["content"]
            return {"content": "Plan: rooms first, then items."}
        orch = iter([{"tool_calls": [{"name": "write_brief", "arguments": {"project": "Tiny game", "text": brief}},
                                     {"name": "assign_project", "arguments": {"project": "Tiny game", "bot": "Coral",
                                                                             "note": "start small"}}]},
                     {"content": "Done, Coral has it."}])
        app, llm, _ = make_app(responder)
        room = onboard(app)["room"]["id"]
        p = app.create_project(name="Tiny game", description="d")
        coral = app.create_bot(name="Coral", role="game maker")
        orch_tools = [t["function"]["name"] for t in __import__("snaplab.tools", fromlist=["x"]).Toolbox(
            app, app.store.orchestrator(), room=app.store.room(room), lang="en").schemas()]
        self.assertIn("write_brief", orch_tools)
        self.assertIn("assign_project", orch_tools)
        app.chat.post(room, "make Coral own the game")
        dm = app.store.dm_room(coral["id"])["id"]
        self.assertTrue(wait_until(lambda: any(m.get("text") == "Plan: rooms first, then items." for m in app.store.messages(dm)), 8))
        folder = Path(app.cfg["projects_dir"]) / p["slug"]
        self.assertEqual((folder / "BRIEF.md").read_text(encoding="utf-8-sig").strip(), brief.strip())
        self.assertTrue((folder / "BRIEF.md").read_bytes().startswith(bytes([0xEF, 0xBB, 0xBF])))   # PowerShell 5.1 reads it as UTF-8
        self.assertEqual(app.store.project(p["id"])["bot"], coral["id"])
        self.assertIn(p["id"], app.store.bot(coral["id"])["projects"])
        self.assertIn("A tiny text adventure.", seen["teammate_system"])          # the BRIEF is in its context
        self.assertIn("Tiny game", seen["teammate_last"])
        self.assertIn("start small", seen["teammate_last"])
        from snaplab import gitops
        if gitops.available():
            log = gitops.run(folder, "log", "--oneline", "-3").stdout
            self.assertIn("Update BRIEF.md", log)

    def test_after_web_the_brief_needs_approval(self):
        # No card by default since 2026-11-21; config "after_web" still asks after a web read.
        from test_core import script, wait_reply
        app, llm, _ = make_app(script({"tool_calls": [{"name": "read_page", "arguments": {"url": "https://example.org"}}]},
                                      {"tool_calls": [{"name": "write_brief", "arguments": {"project": "G", "text": "# G\nnew"}}]},
                                      {"content": "Waiting."}), approvals={"write_brief": "after_web"})
        room = onboard(app)["room"]["id"]
        p = app.create_project(name="G", description="d")
        before = (Path(app.cfg["projects_dir"]) / p["slug"] / "BRIEF.md").read_text(encoding="utf-8")
        app.chat.post(room, "read and rewrite")
        self.assertTrue(wait_reply(app, room))
        q = app.store.questions()[0]
        self.assertEqual(q["action"]["type"], "write_brief")
        self.assertEqual((Path(app.cfg["projects_dir"]) / p["slug"] / "BRIEF.md").read_text(encoding="utf-8"), before)
        app.chat.answer(q["id"], "Approve")
        self.assertIn("new", (Path(app.cfg["projects_dir"]) / p["slug"] / "BRIEF.md").read_text(encoding="utf-8"))

    def test_bot_cap(self):
        app, _, _ = make_app(max_bots=3)
        onboard(app)
        app.create_bot(name="One", role="r")
        app.create_bot(name="Two", role="r")
        with self.assertRaises(ValueError):
            app.create_bot(name="Three", role="r")

    def test_no_bot_cap_by_default(self):
        app, _, _ = make_app()
        onboard(app)
        for i in range(9):
            app.create_bot(name=f"Bot{i}", role="r")
        self.assertEqual(len(app.store.bots()), 10)

    def test_start_session_lists_every_harness(self):
        from snaplab.tools import Toolbox
        app, _, _ = make_app()
        onboard(app)
        box = Toolbox(app, app.store.orchestrator(), room=app.store.rooms()[0], lang="en")
        tool = next(t for t in box.schemas() if t["function"]["name"] == "start_session")
        self.assertEqual(set(tool["function"]["parameters"]["properties"]["harness"]["enum"]), {"codex", "bot", "tests"})

    def test_chips_show_names_not_ids(self):
        from snaplab.tools import Toolbox
        app, _, _ = make_app()
        onboard(app)
        p = app.create_project(name="Named project", description="d")
        coral = app.create_bot(name="Coral", role="r")
        box = Toolbox(app, app.store.orchestrator(), room=app.store.rooms()[0], lang="en")
        self.assertEqual(box.nice_label("list_sessions", {"project": p["id"]}), "Named project")
        self.assertEqual(box.nice_label("assign_project", {"project": p["id"], "bot": coral["id"]}), "Named project → Coral")
        self.assertEqual(box.nice_label("web_search", {"query": "x y"}), "x y")


class StatusTest(unittest.TestCase):
    """F18: one place with the real state of what the phone depends on."""

    def test_status_levels(self):
        app, _, _ = make_app()
        st = app.status()
        ids = {i["id"]: i for i in st["items"]}
        self.assertEqual(ids["model"]["state"], "ok")
        self.assertIn("slots", ids)
        self.assertEqual(st["level"], "ok")
        app.note_model_error("unreachable")
        self.assertEqual(app.status()["level"], "bad")
        app._on_usage(input_tokens=1, output_tokens=1, ok=True, source="chat", model="qwen")
        self.assertEqual(app.status()["level"], "ok")
        app.note_model_error("busy")
        self.assertEqual(app.status()["level"], "warn")

    def test_missing_key_is_bad_and_watch_publishes(self):
        tmp = tempfile.mkdtemp(prefix="snaplab-status-")
        app, _, _ = make_app(tmp=tmp, providers={"none": {"base_url": "http://127.0.0.1:9/v1",
                                                          "api_key_env": "SNAPLAB_TEST_NO_SUCH_KEY"}},
                             models={"m": {"provider": "none", "model": "x"}}, routing={"default": "m", "chat": "m"})
        app.models.override = None
        st = app.status()
        self.assertEqual(st["level"], "bad")
        self.assertEqual(next(i for i in st["items"] if i["id"] == "model")["code"], "nokey")
        events = app.bus.subscribe()
        app.start_status_watch(every_s=0.05)
        try:
            got = None
            end = __import__("time").time() + 3
            while __import__("time").time() < end and not got:
                try:
                    ev = events.get(timeout=0.2)
                except Exception:  # noqa: BLE001
                    continue
                if ev["type"] == "status.updated":
                    got = ev
            self.assertTrue(got)
            self.assertEqual(got["status"]["level"], "bad")
        finally:
            app._status_stop.set()

    def test_activity_is_pushed(self):
        app, _, _ = make_app()
        events = app.bus.subscribe()
        onboard(app)
        app.create_project(name="Pushed", description="d")
        kinds = []
        while not events.empty():
            kinds.append(events.get())
        self.assertTrue(any(e["type"] == "activity.new" and "Pushed" in e["activity"]["text"] for e in kinds))


if __name__ == "__main__":
    unittest.main()
