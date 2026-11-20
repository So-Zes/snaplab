"""Claude Code as a bot's model (snaplab/claude_cli.py), driven by tests/fake_claude.py instead of the real CLI."""
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from helpers import make_app, onboard, wait_until

from snaplab import claude_cli
from snaplab.claude_cli import ClaudeCLI, split_messages

FAKE = str(Path(__file__).with_name("fake_claude.py"))
PLUGIN = '''
TOOL = {"name": "shout", "description": "Upper-case a text.", "approval": "always",
        "parameters": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}}
def run(ctx, text):
    return text.upper()
'''

original_command = ClaudeCLI._command


def fake_command(self, exe, *a, **k):
    return [sys.executable, FAKE] + original_command(self, exe, *a, **k)[1:]


def claude_app(**over):
    d = Path(tempfile.mkdtemp(prefix="snaplab-claude-"))
    (d / "shout.py").write_text(PLUGIN, encoding="utf-8")
    app, llm, _ = make_app(None, providers={"claude": {"kind": "claude_cli", "exe": sys.executable}},
                           models={"claude": {"provider": "claude", "model": "sonnet", "label": "Claude"}},
                           tool_dirs=[str(d)], **over)
    app.models.override = None
    r = onboard(app)
    room, bot_id = r["room"]["id"], r["room"]["bots"][0]
    bot = app.store.bot(bot_id)
    bot["model"] = "claude"
    app.store.save_bot(bot)
    return app, room


def last_bot(app, room):
    return [m for m in app.store.messages(room) if m["role"] == "bot" and m.get("origin")]


def turn(app, room, text, n=1, timeout=30):
    app.chat.post(room, text)
    assert wait_until(lambda: len([m for m in last_bot(app, room) if m.get("status") != "streaming"]) >= n, timeout)
    return last_bot(app, room)[-1]


def report(msg):
    return json.JSONDecoder().raw_decode(msg["text"].split("REPORT ", 1)[1])[0]


@patch.object(ClaudeCLI, "_command", fake_command)
class ClaudeBotTest(unittest.TestCase):
    def test_turn_uses_snaplab_tools_through_the_bridge(self):
        app, room = claude_app()
        msg = turn(app, room, 'BADTOKEN TOOL:write_memory:{"text": "Sam likes AI news"}')
        self.assertEqual(msg["status"], "done", msg.get("error"))
        rep = report(msg)
        self.assertEqual(rep["bad_token"], 404)
        self.assertTrue(rep["system_has_note"])
        self.assertIn("write_memory", rep["tools"])
        self.assertIn("shout", rep["tools"])
        for flag in ("--strict-mcp-config", "--no-session-persistence"):
            self.assertIn(flag, rep["args"])
        self.assertEqual(rep["args"][rep["args"].index("--tools") + 1], "")
        self.assertEqual(rep["args"][rep["args"].index("--model") + 1], "sonnet")
        self.assertEqual(rep["results"][0][1], False)
        self.assertIn("Sam likes AI news", app.store.memory(msg["bot"]))
        self.assertEqual([c["name"] for c in msg["tools"]], ["write_memory"])
        self.assertEqual(msg["usage"]["input_tokens"], 110)
        self.assertNotIn("REPORT", msg["thinking"])
        self.assertIn("thinking...", msg["thinking"])

    def test_approval_ends_further_tool_calls(self):
        app, room = claude_app()
        msg = turn(app, room, 'TOOL:shout:{"text": "hi"}\nTOOL:write_memory:{"text": "x"}')
        rep = report(msg)
        self.assertIn("approval", rep["results"][0][2])
        self.assertTrue(rep["results"][1][1])
        self.assertIn("waiting for the user", rep["results"][1][2])
        q = app.store.questions()[0]
        self.assertEqual((q["kind"], q["action"]["tool"]), ("approval", "shout"))
        self.assertNotIn("x", app.store.memory(msg["bot"]).split())

    def test_cli_errors_and_stops(self):
        app, room = claude_app()
        msg = turn(app, room, "FAIL")
        self.assertEqual(msg["status"], "error")
        self.assertIn("Not logged in", msg["error"])
        app.chat.post(room, "SLOW")
        self.assertTrue(wait_until(lambda: app.chat.busy(room), 10))
        t0 = time.time()
        app.chat.stop(room)
        self.assertTrue(wait_until(lambda: last_bot(app, room)[-1].get("status") == "stopped", 10))
        self.assertLess(time.time() - t0, 10)
        run = Path(app.cfg["data_dir"]) / "claude" / "run"
        self.assertTrue(wait_until(lambda: not list(run.iterdir()), 10))        # token files removed

    def test_missing_cli_is_reported(self):
        app, room = claude_app()
        app.models.get("claude").exe_setting = str(Path(app.cfg["data_dir"]) / "nothing.exe")
        self.assertFalse(app.models.has_key("claude"))
        self.assertFalse([m for m in app.models.view() if m["name"] == "claude"][0]["key_set"])
        msg = turn(app, room, "hello")
        self.assertEqual(msg["status"], "error")


class SplitTest(unittest.TestCase):
    def test_history_and_images(self):
        system, blocks = split_messages([
            {"role": "system", "content": "You are Marlin."},
            {"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"},
            {"role": "user", "content": [{"type": "text", "text": "look"},
                                         {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]}])
        self.assertEqual(system, "You are Marlin.")
        self.assertIn("[you]\nhello", blocks[0]["text"])
        self.assertEqual(blocks[1], {"type": "text", "text": "look"})
        self.assertEqual(blocks[2]["source"], {"type": "base64", "media_type": "image/png", "data": "AAAA"})

    def test_exe_behind_the_npm_wrapper(self):
        d = Path(tempfile.mkdtemp())
        exe = d / "node_modules" / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe"
        exe.parent.mkdir(parents=True)
        exe.write_text("")
        (d / "claude.cmd").write_text("")
        with patch.object(claude_cli.shutil, "which", lambda name, path=None: str(d / "claude.cmd") if name == "claude" else None), \
             patch.object(claude_cli.os, "name", "nt"):
            self.assertEqual(claude_cli.find_exe(), str(exe))


if __name__ == "__main__":
    unittest.main()
