"""MCP servers added in Settings: stdio transport, input parsing, saving with secrets, live reload. No network."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from helpers import fake_credentials, make_app
from snaplab import mcp_admin
from snaplab.mcp_client import MCPError, StdioMCPClient

FIXTURE = str(Path(__file__).resolve().parent / "fixtures" / "stdio_mcp.py")


class StdioClientTest(unittest.TestCase):
    def test_lists_and_calls_tools_and_answers_server_pings(self):
        client = StdioMCPClient("fixture", sys.executable, [FIXTURE], env={"FIXTURE_SECRET": "x" * 12}, timeout_s=20)
        self.addCleanup(client.close)
        names = [t["name"] for t in client.list_tools()]
        self.assertEqual(names, ["echo", "env_set"])
        self.assertEqual(client.instructions, "A test server with an echo tool.")
        self.assertEqual(client.call_tool("echo", {"text": "hi"}), ("echo: hi", False))
        self.assertEqual(client.call_tool("env_set", {})[0], "set")
        client.close()
        # Started again on the next use.
        self.assertEqual(client.call_tool("echo", {"text": "again"})[0], "echo: again")

    def test_a_server_that_exits_reports_its_last_words(self):
        client = StdioMCPClient("broken", sys.executable, ["-c", "import sys; print('no such package', file=sys.stderr)"], timeout_s=10)
        with self.assertRaises(MCPError) as caught:
            client.list_tools()
        self.assertIn("no such package", str(caught.exception))

    def test_a_missing_program_is_a_clear_error(self):
        client = StdioMCPClient("missing", "snaplab-no-such-program-xyz", [], timeout_s=5)
        with self.assertRaises(MCPError):
            client.list_tools()


class ParsingTest(unittest.TestCase):
    def test_command_lines_keep_windows_paths_and_quotes(self):
        cmd, args = mcp_admin.split_command('npx -y @scope/server "C:\\Users\\Me\\My Documents" D:\\data')
        self.assertEqual(cmd, "npx")
        self.assertEqual(args, ["-y", "@scope/server", "C:\\Users\\Me\\My Documents", "D:\\data"])

    def test_names_env_and_json(self):
        self.assertEqual(mcp_admin.normalize_name("Server-Filesystem 2"), "serverfilesystem")
        with self.assertRaises(ValueError):
            mcp_admin.normalize_name("1")
        self.assertEqual(mcp_admin.parse_env("A=1\n\nB_KEY = two=2\n"), {"A": "1", "B_KEY": "two=2"})
        with self.assertRaises(ValueError):
            mcp_admin.parse_env("not a pair")
        found = mcp_admin.from_json(json.dumps({"mcpServers": {
            "files": {"command": "npx", "args": ["-y", "x"], "env": {"TOKEN": "abc"}},
            "remote": {"type": "http", "url": "https://example.com/mcp", "headers": {"Authorization": "Bearer t0k"}}}}))
        self.assertEqual(found[0], ("files", {"command": "npx", "args": ["-y", "x"], "env": {"TOKEN": "abc"}, "url": None, "token": None}))
        self.assertEqual(found[1][1]["url"], "https://example.com/mcp")
        self.assertEqual(found[1][1]["token"], "t0k")
        with self.assertRaises(ValueError):
            mcp_admin.from_json('{"mcpServers": {"old": {"type": "sse", "url": "https://x/sse"}}}')


class AdminTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        creds = fake_credentials()
        creds.__enter__()
        self.addCleanup(creds.__exit__, None, None, None)
        self.app, _, _ = make_app(tmp=self.temp.name)
        self.addCleanup(self.app.archive.close)
        self.addCleanup(self.app.close_mcp)
        self.saved = {}
        for name, fake in (("write_credential", lambda target, value: self.saved.__setitem__(target, value)),
                           ("read_credential", lambda target: self.saved.get(target)),
                           ("delete_credential", lambda target: self.saved.pop(target, None))):
            p = patch(f"snaplab.credentials.{name}", side_effect=fake)
            p.start()
            self.addCleanup(p.stop)

    def test_add_saves_secrets_outside_the_file_and_tools_appear_live(self):
        result = mcp_admin.add(self.app, "Fixture", command=sys.executable, args=[FIXTURE], env="FIXTURE_SECRET=s3cret-value-123")
        self.assertEqual(result["tools"], ["echo", "env_set"])
        stored = json.loads(self.app.store.path(mcp_admin.FILE).read_text(encoding="utf-8"))
        self.assertNotIn("s3cret-value-123", json.dumps(stored))
        self.assertEqual(list(stored["fixture"]["env_secrets"]), ["FIXTURE_SECRET"])
        self.assertIn("s3cret-value-123", self.saved.values())
        self.assertIn("fixture_echo", self.app.plugin_tools)
        # The secret reaches the server through the credential store.
        tool = self.app.plugin_tools["fixture_env_set"]
        self.assertEqual(tool.run(None), "set")
        view = mcp_admin.view(self.app)[0]
        self.assertEqual((view["name"], view["source"], view["env_keys"]), ("fixture", "settings", ["FIXTURE_SECRET"]))
        self.assertEqual(view["tools"], ["fixture_echo", "fixture_env_set"])
        # Chat off: SnapCode only.
        mcp_admin.update(self.app, "fixture", chat=False)
        self.assertEqual(self.app.plugin_tools["fixture_echo"].scope, ("session",))
        mcp_admin.remove(self.app, "fixture")
        self.assertNotIn("fixture_echo", self.app.plugin_tools)
        self.assertEqual(self.saved, {})

    def test_a_server_that_fails_the_check_is_not_saved(self):
        with self.assertRaises(ValueError):
            mcp_admin.add(self.app, "broken", command=sys.executable, args=["-c", "raise SystemExit(1)"], env="K=value-12345")
        self.assertFalse(self.app.store.path(mcp_admin.FILE).exists())
        self.assertEqual(self.saved, {})

    def test_import_json_reports_each_server(self):
        text = json.dumps({"mcpServers": {"good one": {"command": sys.executable, "args": [FIXTURE]},
                                          "bad": {"command": sys.executable, "args": ["-c", "pass"]}}})
        result = mcp_admin.add_json(self.app, text)
        self.assertEqual([(r["name"], r["ok"]) for r in result["results"]], [("goodone", True), ("bad", False)])
        self.assertIn("goodone_echo", self.app.plugin_tools)

    def test_workspace_sees_placeholders_and_the_pc_server_gets_real_paths(self):
        home = self.app.redactor.home
        project = self.app.create_project(name="Note check", description="Read " + home + "\notes\note.txt with the tool")
        brief = (Path(self.app.cfg["projects_dir"]) / project["slug"] / "BRIEF.md").read_text(encoding="utf-8-sig")
        self.assertNotIn(home, brief)
        placeholder = self.app.redactor.clean(home + "\notes")
        self.assertNotIn(home, placeholder)
        restored = self.app.redactor.restore_paths(placeholder)
        self.assertEqual(restored, home + "\notes")

    def test_config_servers_are_listed_but_not_removable(self):
        self.app.cfg["mcp_servers"] = {"cfgsrv": {"url": "http://127.0.0.1:9/mcp"}}
        self.assertEqual(mcp_admin.view(self.app)[0]["source"], "config")
        with self.assertRaises(ValueError):
            mcp_admin.remove(self.app, "cfgsrv")


if __name__ == "__main__":
    unittest.main()
