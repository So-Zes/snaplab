"""Regression cases for the independent round-12 security review (synthetic inputs)."""
from contextlib import contextmanager
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import test_round12
import test_proxy_namespace
from test_proxy_namespace import request_body
from snaplab.harnesses import remote, remote_worker
from snaplab.proxy import Translator, _Handler
from snaplab.dialogue import session_origin
from snaplab.tools import Toolbox
from snaplab.archive import Archive, Scope


class ExportSecurityTest(unittest.TestCase):
    def setUp(self):
        test_round12.RemoteTest.setUp(self)
        # The provenance checks follow external reads to the cards that config can ask for (no cards by default).
        from helpers import CARD_APPROVALS
        self.app.cfg["approvals"] = dict(CARD_APPROVALS)

    fake_popen = test_round12.RemoteTest.fake_popen
    run_job = test_round12.RemoteTest.run_job

    def reader(self, origin="fresh-reader"):
        return Toolbox(self.app, self.bot, room=self.app.store.room(self.session["room"]), origin=origin)

    def mark_session(self):
        read = {"tool": "ssh", "source": "synthetic-external-result", "session": self.session["id"]}
        self.app.chat.dialogue.mark_web_read(session_origin(self.session["id"]), self.session["room"], read)
        return read

    def assert_approval(self, box):
        before = self.app.store.memory(self.bot["id"])
        result = box.t_write_memory("synthetic external instruction")
        self.assertTrue(result.stop, result.content)
        self.assertEqual(self.app.store.memory(self.bot["id"]), before)

    def test_get_session_requires_approval_and_persists_sources(self):
        read = self.mark_session()
        box = self.reader()
        result = box("get_session", {"session_id": self.session["id"]}, "get-result")
        self.assertIn("untrusted web material", result.content)
        self.assert_approval(box)
        self.assertIn(read, self.app.chat.dialogue.web_reads("fresh-reader"))
        self.assert_approval(self.reader())
        # A later recall of the archived tool result must not remove the requirement.
        later = self.reader("tool-recall")
        later.t_recall(record=result.record)
        self.assert_approval(later)

    def test_get_session_inherits_local_web_reads_without_chat_origin(self):
        self.session["execution_backend"] = "local"
        self.app.store.save_session(self.session)
        self.mark_session()
        box = self.reader(None)
        box.t_get_session(self.session["id"])
        self.assert_approval(box)

    def test_clean_local_session_does_not_require_approval(self):
        self.session["execution_backend"] = "local"
        self.app.store.save_session(self.session)
        box = self.reader()
        box.t_get_session(self.session["id"])
        self.assertFalse(box.read_web)

    def test_get_session_denied_access_does_not_read_provenance(self):
        self.mark_session()
        box = self.reader()
        with patch.object(box, "check_project", side_effect=ValueError("not shared")):
            with self.assertRaises(ValueError):
                box.t_get_session(self.session["id"])
        self.assertFalse(box.read_web)

    def test_archived_automatic_report_and_derived_reply_keep_sources_after_reload(self):
        read = self.mark_session()
        self.app.cfg["sessions"]["report_by_bot"] = True
        self.session["started_by"] = self.bot["id"]
        with patch.object(self.app.chat, "_enqueue") as enqueue:
            self.app.sessions._announce(self.session, self.project, "review")
        origin = enqueue.call_args.args[2]
        report = self.app.store.add_message(self.session["room"], {
            "role": "bot", "bot": self.bot["id"], "origin": origin, "text": "Syntheticreport external result"})
        hits = self.app.archive.search("Syntheticreport", Scope(everything=True))
        record = next(r for r in hits if r["meta"].get("message") == report["id"])
        self.assertTrue(record["meta"].get("web_material"))
        self.assertTrue(record["meta"].get("untrusted"))
        self.assertIn(read, record["meta"]["web_reads"])
        # Rebuild from durable JSONL, without the original conversation state.
        self.app.store.write_json("bot-conversations.json", {"chains": {}, "hour": []})
        self.app.archive.close()
        self.app.archive.dir.joinpath("index.sqlite3").unlink()
        self.app.archive = Archive(self.app)
        self.app.store.on_message = self.app.archive.on_message
        self.addCleanup(self.app.archive.close)
        later = self.reader("report-recall")
        later.t_recall(record=record["id"])
        self.assert_approval(later)
        self.app.store.add_message(self.session["room"], {
            "role": "bot", "bot": self.bot["id"], "origin": "report-recall", "text": "Derivedreport summary"})
        derived = self.app.archive.search("Derivedreport", Scope(everything=True))[0]
        third = self.reader("derived-recall")
        third.t_recall(record=derived["id"])
        self.assert_approval(third)

    def test_validation_error_after_first_write_rolls_back(self):
        original = remote.files.safe_path
        def fail_after_write(folder, name):
            if name == "delete.txt" and (self.folder / "input.txt").read_bytes() == b"new":
                raise ValueError("synthetic path changed")
            return original(folder, name)
        with patch.object(remote.files, "safe_path", side_effect=fail_after_write):
            with self.assertRaises(ValueError):
                remote.apply_download(self.folder, {"input.txt": b"new", "delete.txt": b"new"}, {})
        self.assertEqual((self.folder / "input.txt").read_bytes(), b"task input")
        self.assertEqual((self.folder / "delete.txt").read_bytes(), b"remove this")

    def test_partial_write_failure_restores_current_target(self):
        original = remote.files.write_file
        def fail_after_write(folder, name, raw):
            original(folder, name, raw)
            if name == "delete.txt" and raw == b"new":
                raise ValueError("synthetic post-write validation failure")
        with patch.object(remote.files, "write_file", side_effect=fail_after_write):
            with self.assertRaises(ValueError):
                remote.apply_download(self.folder, {"input.txt": b"new", "delete.txt": b"new"}, {})
        self.assertEqual((self.folder / "input.txt").read_bytes(), b"task input")
        self.assertEqual((self.folder / "delete.txt").read_bytes(), b"remove this")

    def test_failed_restore_continues_and_keeps_originals(self):
        original = remote.files.write_file
        replace = remote.os.replace
        def fail_write(folder, name, raw):
            if name == "fail.txt" or (name == "delete.txt" and raw == b"remove this"):
                raise OSError("synthetic locked destination")
            return original(folder, name, raw)
        def fail_restore(source, target):
            if Path(source).name == "old-1":
                raise OSError("synthetic locked destination")
            return replace(source, target)
        with patch.object(remote.files, "write_file", side_effect=fail_write), \
             patch.object(remote.os, "replace", side_effect=fail_restore):
            with self.assertRaises(OSError) as caught:
                remote.apply_download(self.folder, {
                    "input.txt": b"new", "delete.txt": b"new", "fail.txt": b"new"}, {})
        self.assertEqual((self.folder / "input.txt").read_bytes(), b"task input")
        self.assertEqual((self.folder / "delete.txt").read_bytes(), b"new")
        recovery = getattr(caught.exception, "recovery_dir", None)
        self.assertIsNotNone(recovery)
        self.assertTrue(Path(recovery).is_dir())
        originals = [p.read_bytes() for p in Path(recovery).glob("old-*")]
        self.assertIn(b"task input", originals)
        self.assertIn(b"remove this", originals)
        manifest = json.loads((Path(recovery) / "recovery.json").read_text(encoding="utf-8"))
        self.assertIn("delete.txt", manifest["failed"])
    def test_private_workspace_refused_before_ssh(self):
        self.app.redactor.add_term("private-owner-canary")
        (self.folder / "input.txt").write_text("private-owner-canary", encoding="utf-16")
        with self.assertRaises(ValueError):
            remote.checked_upload(self.app, self.folder, "input.txt", remote.secret_bytes(self.app.guard))

    def test_credentials_are_excluded_but_project_data_is_sent(self):
        for name in (".npmrc", ".netrc", "auth.json", ".config/token", "credentials.json", ".env.local"):
            p = self.folder / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("synthetic-credential")
        for name in ("config.json", "data/levels.json", "runtime/cache.txt"):
            p = self.folder / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("project-file")
        forms = remote.secret_bytes(self.app.guard)
        sent = {n: remote.checked_upload(self.app, self.folder, n, forms)
                for n in remote.local_manifest(self.app, self.session["id"], self.folder)}
        self.assertFalse(any(b"synthetic-credential" in raw for raw in sent.values()))
        for name in ("config.json", "data/levels.json", "runtime/cache.txt"):
            self.assertIn(name, sent)

    def test_outgoing_prose_is_redacted(self):
        self.app.redactor.add_term("private-owner-canary")
        self.project["test_command"] = "echo private-owner-canary"
        self.app.store.save_project(self.project)
        with patch.object(self.app.redactor, "clean", wraps=self.app.redactor.clean) as clean:
            self.run_job("private-owner-canary")
        self.assertIn("private-owner-canary", [c.args[0] for c in clean.call_args_list])
        self.assertIn("echo private-owner-canary", [c.args[0] for c in clean.call_args_list])

    def test_remote_output_marks_external_origin_before_consumption(self):
        self.run_job(session_turn=True)
        reads = self.app.chat.dialogue.web_reads(session_origin(self.session["id"]))
        self.assertTrue(reads)
        self.assertEqual(reads[0].get("session"), self.session["id"])
        from snaplab.archive import Scope
        archived = self.app.archive.search("Screenshot", Scope(everything=True))
        self.assertTrue(any((r.get("meta") or {}).get("web_material") for r in archived))

    def test_case_only_rename_preserves_file(self):
        (self.folder / "readme.md").write_bytes(b"old")
        remote.apply_download(self.folder, {"README.md": b"new"}, ["readme.md"])
        self.assertEqual((self.folder / "README.md").read_bytes(), b"new")

    def test_failed_batch_restores_previous_files(self):
        original = remote.files.write_file
        def fail_second(folder, name, raw):
            if Path(folder) == self.folder and name == "delete.txt":
                raise OSError("synthetic locked destination")
            return original(folder, name, raw)
        with patch.object(remote.files, "write_file", side_effect=fail_second):
            with self.assertRaises(OSError):
                remote.apply_download(self.folder, {"input.txt": b"new", "delete.txt": b"new"}, [])
        self.assertEqual((self.folder / "input.txt").read_bytes(), b"task input")
        self.assertEqual((self.folder / "delete.txt").read_bytes(), b"remove this")

    def test_existing_local_session_does_not_check_remote_availability(self):
        harness = self.app.sessions.harnesses["codex"]
        self.app.cfg["codex"]["remote"]["host"] = ""
        with patch("snaplab.harnesses.codex.find_codex", return_value=None):
            result = harness._run_turn({}, self.folder, "hello", self.ctl, True, "fake")
        # Windows explains the missing Codex CLI; other systems have no local Codex sandbox at all.
        self.assertTrue("Codex CLI" in result.error or "needs Windows" in result.error, result.error)


class RelaySecurityTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.owner = Translator(port=0, api_cfg={"base_url": "http://invalid", "model": "fake"},
                                slots=Mock(), key_fn=lambda: "synthetic-provider-key", token="synthetic-job-token",
                                log_path=Path(self.tmp.name) / "proxy.log")

    def handler(self, headers):
        h = object.__new__(_Handler)
        h.owner = self.owner
        h.path = "/v1/responses"
        h.headers = headers
        h.rfile = Mock()
        h._json = Mock()
        return h

    def test_unauthenticated_body_never_read(self):
        h = self.handler({"Content-Length": "999999999"})
        h.do_POST()
        h.rfile.read.assert_not_called()
        self.assertEqual(h._json.call_args.args[0], 401)

    def test_bad_framing_never_read(self):
        # 2026-11-16: no size cap on an authenticated body any more (screenshots made real requests several MB).
        for extra in ({"Content-Length": "-1"}, {"Content-Length": "invalid"}, {"Transfer-Encoding": "chunked"}):
            h = self.handler({"Authorization": "Bearer synthetic-job-token", **extra})
            h.do_POST()
            h.rfile.read.assert_not_called()
            self.assertIn(h._json.call_args.args[0], (400, 411, 413))

    def test_stop_revokes_waiting_request(self):
        @contextmanager
        def acquire(**kw):
            self.owner.stop()
            yield 0
        self.owner.slots.acquire.side_effect = acquire
        with patch("snaplab.proxy.open_request") as upstream:
            body = request_body()
            body["stream"] = False
            self.owner.token = "test-token"
            test_proxy_namespace.NamespaceProxyTest.http_request(self, self.owner, body)
        upstream.assert_not_called()
        self.assertTrue(callable(self.owner.slots.acquire.call_args.kwargs.get("cancel")))

    def test_log_scrubs_token_in_all_metadata(self):
        self.owner.log({"tool_names": [self.owner.token], "sent_tool_kinds": [self.owner.token]})
        self.assertNotIn(self.owner.token, self.owner.log_path.read_text(encoding="utf-8"))


class IsolationSecurityTest(unittest.TestCase):
    def test_worker_command_enforces_mount_and_process_boundary(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            job = home / "jobs" / "synthetic"
            job.mkdir(parents=True)
            # The executable and process tree must be inside an OS-enforced boundary.
            cmd = remote_worker.isolated_command(["/usr/bin/true"], job, home, home / "config")
        self.assertEqual(cmd[0], "/usr/bin/bwrap")
        for flag in ("--unshare-user", "--unshare-pid", "--unshare-ipc", "--die-with-parent", "--new-session"):
            self.assertIn(flag, cmd)
        self.assertNotIn(["--ro-bind", "/", "/"], [cmd[i:i+3] for i in range(len(cmd))])
        self.assertNotIn(["--bind", str(home), str(home)], [cmd[i:i+3] for i in range(len(cmd))])


if __name__ == "__main__":
    unittest.main()
