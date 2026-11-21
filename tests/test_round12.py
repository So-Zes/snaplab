"""Remote execution contract, using synthetic data and a fake SSH peer over real pipes."""
import base64
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import tomllib
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
import zlib

from helpers import fake_credentials, make_app, onboard
from snaplab import config
from snaplab.harnesses import remote
from snaplab.harnesses import remote_worker as files
from snaplab.harnesses.capture import browser_candidates
from snaplab.sessions import Control


class RemoteTest(unittest.TestCase):
    def setUp(self):
        self.creds = fake_credentials()
        self.creds.__enter__()
        self.addCleanup(self.creds.__exit__, None, None, None)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.app, _, _ = make_app(tmp=self.tmp.name, codex={"backend": "ssh", "remote": {"host": "agent.invalid"}},
                                  sessions={"report_by_bot": False})
        self.addCleanup(self.app.archive.close)
        self.bot = onboard(self.app)["bot"]
        with patch("snaplab.gitops.available", return_value=False):
            self.project = self.app.create_project(name="Remote work", bot=self.bot["id"])
        with patch.object(self.app.sessions, "_start"), patch("snaplab.harnesses.remote.available", return_value=(True, "")):
            self.session = self.app.sessions.create(self.project, "codex", "Make a page and screenshot", bot=self.bot["id"])
        self.folder = self.app.sessions.workdir(self.session, self.project)
        self.folder.mkdir(parents=True, exist_ok=True)
        (self.folder / "input.txt").write_text("task input")
        (self.folder / "delete.txt").write_text("remove this")
        self.app.store.add_message(self.session["room"], {"role": "user", "text": "private-chat-canary"})
        self.app.guard.add("synthetic-real-provider-key")
        self.ctl = Control(self.app.sessions, self.session["id"])
        self.proxy = Mock(port=18499, incomplete=False, uncertain=False, calls=0, output_limit=20000, length_count=0, call_limit_reached=False, checkpoint_required=False)
        self.proxy.start.return_value = True
        self.real_popen = subprocess.Popen
        self.launches = []

    def fake_popen(self, command, **kw):
        if command[0] != "ssh":
            return self.real_popen(command, **kw)
        self.launches.append((command, kw))
        return self.real_popen([sys.executable, str(Path(__file__).parent / "fixtures/fake_ssh_round12.py")], **kw)

    def run_job(self, prompt="Make a page", session_turn=False):
        with patch("snaplab.proxy.Translator", return_value=self.proxy) as constructor, \
             patch("snaplab.harnesses.remote.subprocess.Popen", side_effect=self.fake_popen), \
             patch("snaplab.sessions.sandbox_run", side_effect=AssertionError("remote tests must never execute at home")):
            if session_turn:
                self.app.sessions._turn(self.session["id"], prompt, True, self.ctl)
                result = self.app.store.session(self.session["id"])
            else:
                result = remote.run_turn(self.app, self.session, self.folder, prompt, self.ctl)
        self.proxy.stop.assert_called_once()
        self.assertFalse(constructor.call_args.kwargs["restore_paths"])
        self.assertIsNotNone(constructor.call_args.kwargs["web_mcp"])
        self.assertIs(constructor.call_args.kwargs["slots"], self.app.slots)
        return result

    def test_normal_work_gets_no_line_limit_or_staging_rule(self):
        # SnapCode work is not told to write at most 200 lines or to build web apps in stages (2026-11-21).
        from snaplab.harnesses.base import TurnResult
        seen = []

        def fake(s, folder, task, ctl, first):
            seen.append(task)
            return TurnResult(ok=False, error="synthetic stop")
        self.app.store.save_session({**self.app.store.session(self.session["id"]), "task_kind": "web",
                                     "required_files": ["index.html"]})
        harness = self.app.sessions.harnesses[self.session["harness"]]
        with patch.object(harness, "run_turn", side_effect=fake), \
             patch("snaplab.sessions.sandbox_run", side_effect=AssertionError("no local tests")):
            self.app.sessions._turn(self.session["id"], "Make a page", True, self.ctl)
        self.assertTrue(seen)
        for gone in ("200行", "担当のファイルの外", "Build in stages", "Do not generate the whole app"):
            self.assertNotIn(gone, seen[0])
        self.assertIn("Required output files: index.html", seen[0])

    def test_default_local_and_existing_session_remains_local(self):
        self.assertEqual(config.DEFAULTS["codex"]["backend"], "local")
        self.assertEqual(remote.backend(self.app, {}), "local")
        self.assertEqual(self.session["execution_backend"], "ssh")
        self.app.cfg["codex"]["backend"] = "local"
        self.assertEqual(remote.backend(self.app, self.session), "ssh")

    def test_full_session_delivers_screenshot_to_chat(self):
        self.project["test_command"] = "python3 -m unittest"
        self.app.store.save_project(self.project)
        result = self.run_job(session_turn=True)
        self.assertEqual(result["status"], "review", result.get("error"))
        self.assertEqual(result["tests"]["source_type"], "remote_runtime")
        self.assertEqual(result["tests"]["status"], "passed")
        self.assertFalse((self.folder / "delete.txt").exists())
        attachments = result["result"]["attachments"]
        self.assertEqual([a["kind"] for a in attachments], ["image", "html"])
        messages = self.app.store.messages(self.session["room"])
        card = next(m for m in reversed(messages) if m.get("attachments"))
        self.assertEqual(card["attachments"], attachments)
        raw, mime, headers = self.app.attachments.response(attachments[0]["id"], "content")
        self.assertTrue(raw.startswith(b"\x89PNG"))
        self.assertEqual(mime, "image/png")
        cmd, kw = self.launches[0]
        self.assertIn("127.0.0.1:18499", cmd[cmd.index("-R") + 1])
        self.assertNotIn("SNAPLAB_PROXY_TOKEN", kw["env"])
        self.assertNotIn("SNAPSCRIBE_API_KEY", kw["env"])
        if os.environ.get("PROGRAMDATA"):  # Windows OpenSSH exits 255 without it (found in the outside check)
            self.assertEqual(kw["env"].get("PROGRAMDATA"), os.environ["PROGRAMDATA"])

    def test_disconnect_does_not_publish_partial_files(self):
        result = self.run_job("fixture:disconnect")
        self.assertFalse(result.ok)
        self.assertFalse((self.folder / "partial.txt").exists())
        self.assertTrue((self.folder / "delete.txt").exists())

    def test_bad_remote_paths_do_not_write_anything(self):
        result = self.run_job("fixture:traversal")
        self.assertFalse(result.ok)
        self.assertFalse((self.folder.parent / "outside.txt").exists())
        self.assertFalse((self.folder / "screenshot.png").exists())

    def test_proxy_token_is_not_saved_in_outputs(self):
        result = self.run_job("fixture:secret")
        self.assertFalse(result.ok)
        self.assertFalse((self.folder / "leak.txt").exists())

    def test_failed_codex_still_returns_screenshot(self):
        result = self.run_job("fixture:codex-failed", session_turn=True)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["result"]["attachments"][0]["kind"], "image")

    def test_cancellation_closes_process_and_proxy(self):
        timer = threading.Timer(0.3, self.ctl.cancel.set)
        timer.start()
        self.addCleanup(timer.cancel)
        result = self.run_job("fixture:hang")
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "stopped")
        self.assertIsNotNone(self.ctl.proc.poll())

    def test_input_secret_is_never_sent(self):
        # Since 2026-11-17 files are checked when the worker asks for them; a secret stops the job before it is sent
        # (the fake worker also asserts that the value never reaches it).
        (self.folder / "secret.txt").write_text("synthetic-real-provider-key")
        result = self.run_job("Go")
        self.assertFalse(result.ok)
        self.assertIn("upload refused", result.error)
        self.assertTrue((self.folder / "secret.txt").exists())

    def test_only_task_files_are_uploaded(self):
        for name in (".git", ".env", "STATE.md", ".snaplab/STATE.md", ".codex/auth.json", "node_modules/pkg/code.js"):
            p = self.folder / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("private-memory-canary")
        self.assertEqual(set(remote.local_manifest(self.app, self.session["id"], self.folder)),
                         {"input.txt", "delete.txt", "BRIEF.md", "AGENTS.md"})

    def test_config_and_remote_ui_are_bilingual(self):
        parsed = tomllib.loads(remote.config_text(self.app, None, 34000))
        self.assertEqual(parsed["model_providers"]["snaplab"]["base_url"], "http://127.0.0.1:34000/v1")
        from snaplab.harnesses.adapters import NO_TIMEOUT_S
        self.assertEqual(parsed["mcp_servers"]["snaplab_web"]["tool_timeout_sec"], NO_TIMEOUT_S)   # no limit in effect
        self.assertNotIn("windows", parsed)
        with patch("snaplab.harnesses.remote.available", return_value=(True, "")):
            info = self.app.sessions.harnesses["codex"].info()
        for lang in ("en", "ja"):
            self.assertIn("SSH", info["label"][lang])
            self.assertNotIn("harness.remote", info["description"][lang])

    def test_remote_worker_uses_ephemeral_codex_and_returns_images(self):
        job = self.folder / "synthetic-job"
        job.mkdir()
        request = {"sid": "s-synthetic", "manifest": {},
                   "token": "synthetic-job-token", "config": remote.config_text(self.app, None, 35000),
                   "instructions": remote.INSTRUCTIONS, "codex_bin": ".local/bin/codex", "prompt": "Take a screenshot",
                   "timeout": 30, "test_timeout": 10, "test_command": "python3 -m unittest", "helpers": {"adapters.py": Path(remote.__file__).with_name("adapters.py").read_text(encoding="utf-8")}}
        wire = io.BytesIO(b'{"kind":"start"}\n')

        def execute(cmd, folder, env, prompt, cancel, timeout, **kwargs):
            if kwargs.get("events"):
                self.assertIn("--ephemeral", cmd)
                self.assertIn("--dangerously-bypass-approvals-and-sandbox", cmd)
                self.assertNotIn("synthetic-job-token", " ".join(cmd))
                self.assertEqual(env["SNAPLAB_PROXY_TOKEN"], "synthetic-job-token")
                self.assertNotIn("SNAPSCRIBE_API_KEY", env)
                (folder / "screenshot.png").write_bytes(b"\x89PNG\r\n\x1a\nsynthetic")
                (folder / ".snaplab/attachments.json").write_text('[{"path":"screenshot.png"}]')
            else:
                self.assertNotIn("SNAPLAB_PROXY_TOKEN", env)
            return 0, "2 passed"

        with patch.object(files.sys, "stdin", SimpleNamespace(buffer=wire)), \
             patch.object(files, "execute", side_effect=execute) as run, \
             patch.object(files.threading, "Thread"), patch.object(files, "emit") as emit:
            self.assertEqual(files.run(request, job, self.folder), 0)
        self.assertEqual(run.call_count, 2)  # Required tests are checked before completion.
        events = [call.args[0] for call in emit.call_args_list]
        self.assertIn("screenshot.png", [e.get("path") for e in events])
        self.assertEqual(events[-1], {"kind": "complete", "exit_code": 0})

    def test_link_and_size_checks_without_real_links(self):
        linked = self.folder / "input.txt"
        native = type(linked)
        original = native.is_symlink
        with patch.object(native, "is_symlink", lambda p: p == linked or original(p)):
            with self.assertRaises(ValueError):
                files.read_file(self.folder, "input.txt")
        with patch.object(files, "MAX_FILE", 2):
            with self.assertRaises(ValueError):
                files.read_file(self.folder, "input.txt")

    def test_proxy_keeps_remote_arguments_and_shares_existing_slots(self):
        from snaplab.proxy import Translator
        from test_proxy_namespace import NamespaceProxyTest, sse, call_chunk, request_body
        self.app.slots.count = 6
        owner = Translator(port=0, api_cfg=self.app.cfg["api"], slots=self.app.slots,
                           key_fn=lambda: "synthetic-upstream-key", token="test-token",
                           log_path=Path(self.tmp.name) / "proxy.log", redactor=self.app.redactor, restore_paths=False)
        self.assertIs(owner.target(None)["slots"], self.app.slots)
        self.assertEqual(owner.target(None)["slots"].count, 6)
        body = request_body()
        body["stream"] = False
        with patch.object(self.app.redactor, "restore_paths", side_effect=AssertionError("home paths must stay home")), \
             patch("snaplab.proxy.open_request", return_value=sse([
                 call_chunk(0, "cmd", "shell", '{"command":"pwd"}'),
                 {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]}])):
            headers, raw = NamespaceProxyTest.http_request(self, owner, body)
        self.assertIn("200", headers)
        self.assertEqual(json.loads(raw)["output"][0]["arguments"], '{"command":"pwd"}')
        owner.token = "different-token"
        with patch("snaplab.proxy.open_request") as upstream:
            headers, _ = NamespaceProxyTest.http_request(self, owner, body)
            self.assertIn("401", headers)
            upstream.assert_not_called()

    def test_invalid_settings_fail_closed(self):
        for host in ("-oProxyCommand=oops", "host;echo no", "name@host", "host\n"):
            self.app.cfg["codex"]["remote"]["host"] = host
            with self.assertRaises(ValueError):
                remote.settings(self.app)

    def test_ssh_has_no_credentials_or_persistent_forward(self):
        command = remote.ssh_command(remote.settings(self.app), 18499, 35001)
        for value in ("BatchMode=yes", "StrictHostKeyChecking=yes", "ForwardAgent=no", "ExitOnForwardFailure=yes"):
            self.assertIn(value, command)
        self.assertIn("127.0.0.1:35001:127.0.0.1:18499", command)
        self.assertNotIn("-f", command)
        self.assertLess(sum(len(arg) + 3 for arg in command), 30000)

    def test_case_alias_and_reserved_paths_refused(self):
        for path in ("../x", "/tmp/a", "C:/x", "x\\y", "NUL.txt", "a:stream", "a/../b", ".git/config", ".snaplab/result.json"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                files.file_name(path)
        for batch in ({"a": b"x", "A": b"y"}, {"a": b"x", "a/b": b"y"}):
            with self.assertRaises(ValueError):
                remote.apply_download(self.folder, batch, {})
        self.assertFalse((self.folder / "a").exists())

    def test_data_runtime_and_config_transfer_but_credentials_do_not(self):
        # 2026-11-22: a project's data/, runtime/ and config.json come back from the worker; credentials do not.
        allowed = ["src/data/enemies.json", "data/messages.json", "runtime/state.json", "other/data/private.json",
                   "src/data/nested/config.json", "config.json"]
        refused = ["src/data/auth.json", "src/data/.env", ".ssh/id_ed25519", "credentials.json", ".config/gh/hosts.yml"]
        for name in allowed + refused:
            path = self.folder / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{}", encoding="utf-8")
        inventory = files.inventory(self.folder)
        for name in allowed:
            with self.subTest(name=name):
                self.assertEqual(files.file_name(name), name)
                self.assertIn(name, inventory)
        for name in refused:
            with self.subTest(name=name):
                self.assertNotIn(name, inventory)
                with self.assertRaises(ValueError):
                    files.file_name(name)
        uploaded = remote.local_manifest(self.app, self.session["id"], self.folder)
        for name in allowed:
            self.assertIn(name, uploaded)
            files.write_file(self.folder, name, b"[]")
            self.assertEqual(files.read_file(self.folder, name), b"[]")

    def test_linux_browser_discovery(self):
        native_path = type(self.folder)
        with patch("snaplab.harnesses.capture.os.name", "posix"), patch("snaplab.harnesses.capture.Path", native_path), \
             patch("snaplab.harnesses.capture.shutil.which", side_effect=lambda n: "/usr/bin/chromium" if n == "chromium" else None):
            self.assertEqual(str(next(browser_candidates())).replace("\\", "/"), "/usr/bin/chromium")


if __name__ == "__main__":
    unittest.main()
