"""Settings > Advanced: Claude Code and Codex CLI status, adding them as models, and the sign-in window.

The real programs are never run: their status commands are replaced with recorded answers."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from helpers import make_app, fake_credentials
from snaplab import claude_cli, cli_admin, codex_cli, config, setup
from snaplab.models import check

CLAUDE_STATUS = json.dumps({"loggedIn": True, "authMethod": "claude.ai", "subscriptionType": "max",
                            "email": "someone@example.com", "orgName": "Example"})


def answers(table):
    def probe(args, timeout=20):
        for key, value in table.items():
            if " ".join(args[1:]) == key:
                return value
        return None, ""
    return probe


class StatusTest(unittest.TestCase):
    def setUp(self):
        claude_cli._status_cache.clear()
        self.addCleanup(claude_cli._status_cache.clear)

    def test_claude_signed_in_shows_plan_but_never_the_account(self):
        with patch.object(claude_cli, "find_exe", lambda c=None: "claude"), \
             patch.object(claude_cli, "probe", answers({"--version": (0, "2.1.316 (Claude Code)\n"),
                                                        "auth status": (0, CLAUDE_STATUS)})):
            st = claude_cli.status(fresh=True)
        self.assertEqual(st, {"installed": True, "version": "2.1.316", "signed_in": True, "plan": "max"})
        self.assertNotIn("example", json.dumps(st))

    def test_claude_signed_out_and_missing(self):
        with patch.object(claude_cli, "find_exe", lambda c=None: "claude"), \
             patch.object(claude_cli, "probe", answers({"--version": (0, "2.1.316"),
                                                        "auth status": (1, '{"loggedIn": false}')})):
            self.assertFalse(claude_cli.status(fresh=True)["signed_in"])
        with patch.object(claude_cli, "find_exe", lambda c=None: None):
            self.assertEqual(claude_cli.status(fresh=True), {"installed": False, "version": None, "signed_in": False, "plan": None})

    def test_status_is_cached_for_a_minute(self):
        calls = []
        def probe(args, timeout=20):
            calls.append(args)
            return 0, CLAUDE_STATUS
        with patch.object(claude_cli, "find_exe", lambda c=None: "claude"), patch.object(claude_cli, "probe", probe):
            claude_cli.status(fresh=True)
            claude_cli.status()
            self.assertEqual(len(calls), 2)
            claude_cli.status(fresh=True)
            self.assertEqual(len(calls), 4)

    def test_codex_login_kind_and_feature_list(self):
        with patch.object(codex_cli, "find_exe", lambda c=None: "codex"), \
             patch.object(codex_cli, "probe", answers({"--version": (0, "codex-cli 0.162.1"),
                                                       "login status": (0, "Logged in using ChatGPT"),
                                                       "features list": (0, "shell_tool  stable  true\nmemories  stable  false\n")})):
            self.assertEqual(codex_cli.status(fresh=True)["plan"], "ChatGPT")
            self.assertEqual(codex_cli.disabled_features("codex"), ["shell_tool", "memories"])
        claude_cli._status_cache.clear()
        with patch.object(codex_cli, "probe", lambda args, timeout=20: (None, "")):
            self.assertEqual(codex_cli.disabled_features("codex"), list(codex_cli.DISABLED))

    def test_search_path_finds_usual_install_folders(self):
        d = Path(tempfile.mkdtemp())
        name = "snaplab-test-cli"
        target = d / (name + (".exe" if claude_cli.os.name == "nt" else ""))
        target.write_text("")
        self.assertEqual(claude_cli.which(name, [d]), str(target))
        self.assertIsNone(claude_cli.which(name, []))


class AddTest(unittest.TestCase):
    def setUp(self):
        claude_cli._status_cache.clear()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.fake = fake_credentials()
        self.fake.__enter__()
        self.addCleanup(self.fake.__exit__, None, None, None)
        self.app, _, _ = make_app(tmp=self.temp.name, local_mode=True)
        self.addCleanup(self.app.archive.close)

    def signed(self, signed=True):
        st = {"installed": True, "version": "1.0.0", "signed_in": signed, "plan": "max" if signed else None}
        return patch.object(claude_cli, "status", lambda c=None, fresh=False: st)

    def test_add_tests_first_then_saves_a_cli_model(self):
        tested = []
        with self.signed(), patch.object(cli_admin, "_test", lambda app, a, model: tested.append(model)):
            out = cli_admin.add(self.app, "claude", "opus")
        name = out["added"]
        self.assertEqual(tested, ["opus"])
        m = self.app.cfg["models"][name]
        self.assertEqual(self.app.cfg["providers"][m["provider"]]["kind"], "claude_cli")
        self.assertEqual(m["label"], "Claude (opus)")
        self.assertTrue(next(v for v in out["models"] if v["name"] == name)["cli"])
        reloaded = config.load(Path(self.temp.name) / "absent.json", overrides={"data_dir": self.app.cfg["data_dir"]})
        self.assertIn(name, reloaded["models"])
        self.assertEqual(self.app.models.key(name), None)
        with self.signed(), patch.object(cli_admin, "_test", lambda *a: None):
            second = cli_admin.add(self.app, "claude", "")["added"]
        self.assertEqual(self.app.cfg["models"][second]["provider"], m["provider"])
        setup.remove_model(self.app, name)
        setup.remove_model(self.app, second)
        self.assertNotIn(m["provider"], self.app.cfg["providers"])

    def test_refuses_bad_names_missing_sign_in_and_unknown_apps(self):
        with self.signed(), self.assertRaises(ValueError):
            cli_admin.add(self.app, "claude", "opus; rm -rf /")
        with self.signed(False), self.assertRaises(ValueError):
            cli_admin.add(self.app, "claude", "opus")
        with self.assertRaises(LookupError):
            cli_admin.add(self.app, "gemini", "")

    def test_check_allows_a_cli_model_without_an_id(self):
        cfg = {"providers": {"c": {"kind": "codex_cli"}}, "models": {"m": {"provider": "c", "model": ""}},
               "routing": {"default": "m"}}
        self.assertEqual(check(cfg), [])

    def test_view_and_window(self):
        with self.signed():
            apps = {a["id"]: a for a in cli_admin.view(self.app)["apps"]}
        self.assertEqual(apps["claude"]["signin"], "claude auth login")
        self.assertEqual(apps["codex"]["signin"], "codex login")
        self.assertNotIn("gemini", apps)
        opened = []
        with patch.object(cli_admin.os, "name", "nt"), patch.object(cli_admin.subprocess, "Popen", lambda args, **k: opened.append(args)), \
             patch.object(claude_cli, "find_exe", lambda c=None: r"C:\Users\x\.local\bin\claude.exe"):
            cli_admin.open_window(self.app, "claude", "signin")
            cli_admin.open_window(self.app, "codex", "install")
            with self.assertRaises(ValueError):
                cli_admin.open_window(self.app, "claude", "other")
        self.assertEqual(opened[0][0], "powershell.exe")
        self.assertIn("& 'C:\\Users\\x\\.local\\bin\\claude.exe' auth login", opened[0][-1])
        self.assertIn("https://chatgpt.com/codex/install.ps1", opened[1][-1])
        with patch.object(cli_admin.os, "name", "posix"), self.assertRaises(ValueError):
            cli_admin.open_window(self.app, "claude", "install")


if __name__ == "__main__":
    unittest.main()
