"""File delivery with synthetic data; the real HTTP handler runs over in-memory streams."""
import json
import io
import os
import queue
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from helpers import make_app, onboard, fake_credentials
import test_round7
from snaplab.attachments import Attachments, MAX_BYTES, read_local
from snaplab.harnesses.base import TurnResult
from snaplab.harnesses.capture import local_path, prepare_capture
from snaplab.sessions import Control
from snaplab.server import Handler
from snaplab.store import Store
from snaplab.tools import Toolbox


class DeliveryTest(unittest.TestCase):
    def setUp(self):
        self.credentials = fake_credentials()
        self.credentials.__enter__()
        self.addCleanup(self.credentials.__exit__, None, None, None)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.app, self.llm, _ = make_app(tmp=self.tmp.name, sessions={"report_by_bot": False})
        self.addCleanup(self.app.archive.close)
        self.bot = onboard(self.app)["bot"]
        with patch("snaplab.gitops.available", return_value=False):
            self.project = self.app.create_project(name="Schedule", bot=self.bot["id"])
        self.room = self.app.store.dm_room(self.bot["id"])
        with patch.object(self.app.sessions, "_start"), patch("snaplab.gitops.available", return_value=False):
            self.session = self.app.sessions.create(self.project, "bot", "Build a schedule", bot=self.bot["id"])
        self.folder = self.app.sessions.workdir(self.session, self.project)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.session["status"] = "review"
        self.app.store.save_session(self.session)
        self.box = Toolbox(self.app, self.bot, room=self.room)

    def produced(self, name="index.html", raw=b"<button onclick='this.textContent=42'>Try</button>"):
        path = self.folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        self.app.attachments.register_outputs(self.session, self.folder, [{"path": name, "change": "added"}])
        self.app.store.save_session(self.session)
        return path

    def route(self):
        route = type("Routes", (), {"app": self.app, "cookie": None,
                                    "req": classmethod(test_round7.AvatarRouteTest.req.__func__)})
        token, _ = self.app.auth.new_pairing("synthetic phone")
        status, body, headers = route.req("POST", "/api/pair", {"token": token}, cookie=False)
        self.assertEqual(status, 200)
        route.cookie = headers["Set-Cookie"].split(";")[0]
        return route, json.loads(body)["device"]["id"]

    def test_bot_delivers_persistent_immutable_copy_and_deduplicates(self):
        source = self.produced()
        answer = json.loads(self.box.t_publish_file(self.session["id"], "index.html", "Schedule").content)
        meta = answer["attachment"]
        self.assertTrue(answer["delivered"])
        self.box.t_publish_file(self.session["id"], "index.html", "Schedule")
        messages = [m for m in self.app.store.messages(self.room["id"]) if m.get("attachments")]
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["attachments"][0]["id"], meta["id"])
        original = source.read_bytes()
        source.unlink()
        restarted = Attachments(SimpleNamespace(store=Store(self.app.store.dir), guard=self.app.guard))
        self.assertEqual(restarted.response(meta["id"], "download")[0], original)
        self.assertEqual(Store(self.app.store.dir).messages(self.room["id"])[-1]["attachments"][0], meta)
        self.assertNotIn(str(self.folder), json.dumps(meta))

    def test_real_routes_auth_head_revocation_and_html_isolation(self):
        self.produced()
        meta = self.app.attachments.create(self.session, "index.html")
        route, device = self.route()
        base = "/api/attachments/" + meta["id"]
        for mode in ("content", "preview", "download"):
            self.assertEqual(route.req("GET", base + "/" + mode, cookie=False)[0], 401)
            self.assertEqual(route.req("GET", base + "/" + mode, user=None)[0], 403)
            self.assertEqual(route.req("GET", base + "/" + mode, user="intruder@example.com")[0], 403)
        status, raw, headers = route.req("GET", base + "/download")
        self.assertEqual(status, 200)
        self.assertIn(b"button", raw)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["Cross-Origin-Resource-Policy"], "same-origin")
        self.assertTrue(headers["Content-Disposition"].startswith("attachment;"))
        head_status, head_body, head_headers = route.req("HEAD", base + "/download")
        self.assertEqual((head_status, head_body), (200, b""))
        self.assertEqual(head_headers["Content-Length"], str(len(raw)))
        for suffix in ("../auth.json", "f-" + "0" * 32 + "/content", meta["id"] + "/other"):
            self.assertEqual(route.req("GET", "/api/attachments/" + suffix)[0], 404)
        self.app.auth.revoke(device)
        self.assertEqual(route.req("GET", base + "/preview")[0], 401)

    def test_html_never_executes_through_any_attachment_route(self):
        payload = b"<script>new RTCPeerConnection({iceServers:[{urls:'stun:outside.invalid'}]})</script>"
        for name in ("index.html", "index.htm", "INDEX.HTML"):
            self.produced(name=name, raw=payload)
            meta = self.app.attachments.create(self.session, name)
            route, _ = self.route()
            for mode in ("preview", "content", "download"):
                for dest in (None, "document", "iframe", "empty", "object", "embed", "frame"):
                    for method in ("GET", "HEAD"):
                        with self.subTest(name=name, mode=mode, dest=dest, method=method):
                            extra = {} if dest is None else {"Sec-Fetch-Dest": dest}
                            status, body, headers = route.req(method, f"/api/attachments/{meta['id']}/{mode}", extra_headers=extra)
                            self.assertEqual(headers["Cache-Control"], "no-store")
                            if mode == "preview":
                                self.assertEqual(status, 404)
                                self.assertNotIn(b"RTCPeerConnection", body)
                            else:
                                self.assertEqual(status, 200)
                                self.assertEqual(headers["Content-Type"], "application/octet-stream")
                                self.assertTrue(headers["Content-Disposition"].startswith("attachment;"))
                                self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
                                self.assertNotIn("allow-scripts", headers["Content-Security-Policy"])
                                self.assertEqual(body, payload if method == "GET" else b"")

    def test_revoked_device_stops_existing_event_stream(self):
        for idle in (False, True):
            with self.subTest(idle=idle):
                route, device = self.route()
                handler = object.__new__(Handler)
                handler.app = self.app
                handler.headers = {"Cookie": route.cookie}
                handler.wfile = io.BytesIO()
                handler._headers = Mock()
                q = Mock()
                calls = 0
                def get(timeout):
                    nonlocal calls
                    calls += 1
                    if calls == 1:
                        return {"type": "message.new", "text": "before-revocation"}
                    if calls == 2:
                        self.app.auth.revoke(device)
                        if idle:
                            raise queue.Empty
                        return {"type": "message.new", "text": "after-revocation"}
                    raise OSError("end synthetic connection")
                q.get.side_effect = get
                with patch.object(self.app.bus, "subscribe", return_value=q), \
                     patch.object(self.app.bus, "unsubscribe") as unsubscribe:
                    handler.events()
                self.assertIn(b"before-revocation", handler.wfile.getvalue())
                self.assertNotIn(b"after-revocation", handler.wfile.getvalue())
                self.assertNotIn(b": ping", handler.wfile.getvalue())
                self.assertEqual(calls, 2)
                self.assertTrue(handler.close_connection)
                unsubscribe.assert_called_once_with(q)

    def test_prepare_capture_failure_finishes_and_reports_to_chat(self):
        for failure in (ValueError("invalid screenshot helper path"), PermissionError("helper cannot be written")):
            with self.subTest(failure=type(failure).__name__):
                sid = self.session["id"]
                ctl = Control(self.app.sessions, sid)
                self.app.sessions.controls[sid] = ctl
                with patch("snaplab.harnesses.capture.prepare_capture", side_effect=failure), \
                     patch.object(self.app.sessions.harnesses["bot"], "run_turn") as run:
                    self.app.sessions._turn(sid, "Prepare capture", True, ctl)
                run.assert_not_called()
                saved = self.app.store.session(sid)
                self.assertEqual(saved["status"], "failed")
                self.assertTrue(saved["ended"])
                self.assertEqual(saved["result"]["status"], "failed")
                self.assertIn(str(failure), saved["error"])
                self.assertNotIn(sid, self.app.sessions.controls)
                message = self.app.store.messages(self.room["id"])[-1]
                self.assertIn(str(failure), message["text"])
                self.assertEqual(message["result"]["status"], "failed")
                self.assertTrue(any(e.get("kind") == "error" for e in self.app.store.session_events(sid, 20)))

    def test_real_capture_preparation_error_can_be_retried(self):
        hidden = self.folder / ".snaplab"
        hidden.mkdir(exist_ok=True)
        helper = hidden / "capture.py"
        helper.mkdir()
        (hidden / "attachments.json").write_text('[{"path":"old.html"}]')
        sid = self.session["id"]
        with patch.object(self.app.sessions.harnesses["bot"], "run_turn") as run:
            self.app.sessions._turn(sid, "Prepare", True, Control(self.app.sessions, sid))
        run.assert_not_called()
        failed = self.app.store.session(sid)
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["result"]["attachments"], [])
        self.assertFalse((hidden / "result.json").exists())
        self.assertTrue(self.app.store.path("sessions", f"{sid}.result.json").exists())
        helper.rmdir()
        with patch.object(self.app.sessions.harnesses["bot"], "run_turn", return_value=TurnResult(ok=True)):
            self.app.sessions._turn(sid, "Retry", False, Control(self.app.sessions, sid))
        retried = self.app.store.session(sid)
        self.assertEqual(retried["status"], "review")
        self.assertIsNone(retried["error"])
        self.assertEqual(retried["turns"], 2)
        self.assertFalse((hidden / "attachments.json").exists())

    def test_preparation_failure_redacts_error_before_persistence(self):
        synthetic = "synthetic-preparation-secret"
        self.app.guard.add(synthetic)
        sid = self.session["id"]
        with patch("snaplab.harnesses.capture.prepare_capture", side_effect=ValueError(synthetic)):
            self.app.sessions._turn(sid, "Prepare", True, Control(self.app.sessions, sid))
        saved = self.app.store.session(sid)
        self.assertEqual(saved["status"], "failed")
        for item in (saved, self.app.store.messages(self.room["id"]), self.app.store.session_events(sid, 20)):
            self.assertNotIn(synthetic, json.dumps(item))

    def test_inline_image_pdf_text_and_active_svg_download(self):
        files = [("picture.png", test_round7.png(12, 20), "image/png", "image"),
                 ("report.pdf", b"%PDF-1.4\n%%EOF", "application/pdf", "pdf"),
                 ("notes.txt", "メモです。".encode(), "text/plain; charset=utf-8", "text"),
                 ("unsafe.svg", b"<svg onload='alert(1)'/>", "application/octet-stream", "file")]
        route, _ = self.route()
        for name, raw, mime, kind in files:
            with self.subTest(name=name):
                self.produced(name, raw)
                a = self.app.attachments.create(self.session, name)
                self.assertEqual(a["kind"], kind)
                mode = "content" if kind in ("image", "file") else "preview"
                status, body, headers = route.req("GET", f"/api/attachments/{a['id']}/{mode}")
                self.assertEqual((status, body, headers["Content-Type"]), (200, raw, mime))
                self.assertEqual(headers["Cache-Control"], "no-store")

    def test_traversal_devices_metadata_and_nonproduced_files_are_refused(self):
        self.produced()
        (self.folder / "unrelated.txt").write_text("not made by this session")
        bad = ("../outside.txt", str(Path(self.tmp.name) / "outside.txt"), "/root.txt", "C:relative.txt",
               "index.html:secret", ".git/config", ".snaplab/capture.py", "nested/../index.html",
               "index.html.", "//host/share/file", "unrelated.txt", "CON", "index.html\x00")
        for name in bad:
            with self.subTest(name=name), self.assertRaises((ValueError, OSError)):
                self.app.attachments.create(self.session, name)
        (self.folder / "index.html").write_text("changed after the session")
        with self.assertRaises(ValueError):
            self.app.attachments.create(self.session, "index.html")
        reply = self.box("publish_file", {"session_id": self.session["id"], "path": "missing.txt"}, "missing")
        self.assertTrue(reply.content.startswith("Error:"))
        self.assertNotIn(str(self.folder), reply.content)

    def test_hardlinks_are_refused(self):
        outside = Path(self.tmp.name) / "private.txt"
        outside.write_text("synthetic private data")
        try:
            os.link(outside, self.folder / "linked.txt")
        except PermissionError:
            self.skipTest("sandbox prevents creating hard links; rerun outside the sandbox")
        with self.assertRaises(ValueError):
            read_local(self.folder, "linked.txt")
        self.app.attachments.register_outputs(self.session, self.folder, [{"path": "linked.txt", "change": "added"}])
        self.assertNotIn("linked.txt", self.session["produced_files"])

    def test_symlink_and_parent_junction_are_refused(self):
        outside = Path(self.tmp.name) / "outside"
        outside.mkdir()
        (outside / "private.txt").write_text("synthetic")
        link = self.folder / "linked"
        try:
            os.symlink(outside, link, target_is_directory=True)
        except OSError:
            if os.name != "nt":
                raise
            import subprocess
            result = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(outside)], capture_output=True)
            if result.returncode:
                self.skipTest("sandbox prevents creating junctions; rerun outside the sandbox")
        try:
            with self.assertRaises(ValueError):
                read_local(self.folder, "linked/private.txt")
        finally:
            if link.is_symlink():
                link.unlink()
            else:
                os.rmdir(link)  # remove only the junction, never its target

    def test_size_empty_secret_and_tampered_storage_are_refused(self):
        self.produced("empty.txt", b"")
        with self.assertRaises(ValueError):
            self.app.attachments.create(self.session, "empty.txt")
        path = self.folder / "large.bin"
        with path.open("wb") as f:
            f.truncate(1025)
        self.assertEqual(MAX_BYTES, 4 * 1024 ** 3)            # one delivered file may be as large as a transfer
        with self.assertRaises(ValueError):
            read_local(self.folder, "large.bin", 1024)
        synthetic = "synthetic-round11-sensitive-value"
        self.app.guard.add(synthetic)
        self.produced("secret.txt", synthetic.encode())
        with self.assertRaises(ValueError):
            self.app.attachments.create(self.session, "secret.txt")
        self.produced()
        a = self.app.attachments.create(self.session, "index.html")
        self.app.store.path("attachments", a["id"], "content").write_bytes(b"tampered")
        self.assertIsNone(self.app.attachments.response(a["id"], "download"))

    def test_link_metadata_is_refused_without_os_link_creation(self):
        path = self.produced("nested/report.txt", b"report")
        original = Path.lstat
        def linked(p, *args, **kwargs):
            value = original(p, *args, **kwargs)
            return SimpleNamespace(st_mode=value.st_mode, st_file_attributes=0x400) if p == path.parent else value
        with patch.object(Path, "lstat", linked), self.assertRaises(ValueError):
            read_local(self.folder, "nested/report.txt")
        st = path.stat()
        with patch("snaplab.attachments.os.fstat", return_value=SimpleNamespace(st_mode=st.st_mode, st_nlink=2,
                   st_size=st.st_size)), self.assertRaises(ValueError):
            read_local(self.folder, "nested/report.txt")

    def test_session_manifest_delivers_to_chat_and_failed_entry_is_visible(self):
        def run(s, folder, prompt, ctl, first):
            self.assertIn("capture.py", prompt)
            (folder / "index.html").write_text("<h1>Schedule</h1>")
            (folder / "screenshot.png").write_bytes(test_round7.png(12, 20))
            (folder / ".snaplab/attachments.json").write_text(json.dumps([
                {"path": "index.html"}, {"path": "screenshot.png"}, {"path": "../private.txt"}]))
            return TurnResult(ok=True, last_message="Created the schedule.")
        with patch.object(self.app.sessions.harnesses["bot"], "run_turn", side_effect=run):
            self.app.sessions._turn(self.session["id"], "Build and capture", True, Control(self.app.sessions, self.session["id"]))
        result = self.app.store.session(self.session["id"])["result"]
        self.assertEqual(len(result["attachments"]), 2)
        self.assertEqual(result["attachment_errors"][0]["code"], "file_refused")
        self.assertTrue(result["attachment_errors"][0]["reason"])
        message = self.app.store.messages(self.room["id"])[-1]
        self.assertEqual(message["attachments"], result["attachments"])
        self.assertIn("Delivered to chat", message["text"])
        self.assertIn("failed", message["text"])
        # A new turn with no selection must not re-deliver the previous manifest.
        with patch.object(self.app.sessions.harnesses["bot"], "run_turn", return_value=TurnResult(ok=True, last_message="Done")):
            self.app.sessions._turn(self.session["id"], "Continue", False, Control(self.app.sessions, self.session["id"]))
        self.assertEqual(self.app.store.session(self.session["id"])["result"]["attachments"], [])
        self.assertTrue(self.app.attachments.response(result["attachments"][0]["id"], "download"))

    def test_bot_only_session_can_queue_a_written_file(self):
        steps = iter([
            {"tool_calls": [{"name": "write_file", "arguments": {"path": "report.txt", "content": "Research report"}}]},
            {"tool_calls": [{"name": "deliver_file", "arguments": {"path": "report.txt", "caption": "Report"}}]},
            {"content": "The report is ready."}])
        self.llm.responder = lambda *args: next(steps)
        self.app.sessions._turn(self.session["id"], "Write and deliver", True, Control(self.app.sessions, self.session["id"]))
        saved = self.app.store.session(self.session["id"])
        self.assertEqual(saved["status"], "review")
        self.assertEqual(saved["result"]["attachments"][0]["name"], "report.txt")

    def test_manifest_limit_and_invalid_json(self):
        self.produced()
        prepare_capture(self.folder)
        path = self.folder / ".snaplab/attachments.json"
        for text in ("{", "{}", "x" * 16385):
            path.write_text(text)
            self.assertEqual(self.app.attachments.collect(self.session, self.folder), ([], ["invalid_manifest"]))
        # any number of entries since 2026-11-21 (it was at most 8); the same file is delivered once
        path.write_text(json.dumps([{"path": "index.html"}] * 9))
        out, errors = self.app.attachments.collect(self.session, self.folder)
        self.assertEqual((len(out), errors), (1, []))

    def test_bot_scope_and_running_session(self):
        self.produced()
        peer = self.app.create_bot(name="Other")
        box = Toolbox(self.app, peer, room=self.app.store.dm_room(peer["id"]))
        # every bot may use every project's results in its own chat since 2026-11-21
        self.assertIn("delivered", box.t_publish_file(self.session["id"], "index.html").content)
        self.session["status"] = "running"
        self.app.store.save_session(self.session)
        with self.assertRaises(ValueError):
            self.box.t_publish_file(self.session["id"], "index.html")

    def test_capture_helper_checks_input_and_copies_without_dependencies(self):
        prepare_capture(self.folder)
        self.assertTrue((self.folder / ".snaplab/capture.py").is_file())
        for name in ("../private.html", "C:/private.html", "https://example.com", ".snaplab/secret.html"):
            with self.assertRaises(ValueError):
                local_path(self.folder.resolve(), name)

    def test_old_attachments_remain_browsable_beyond_recent_chat_messages(self):
        self.produced()
        self.box.t_publish_file(self.session["id"], "index.html")
        for i in range(205):
            self.app.store.add_message(self.room["id"], {"role": "user", "text": f"Later message {i}"})
        self.assertFalse(any(m.get("attachments") for m in self.app.store.messages(self.room["id"])))
        route, _ = self.route()
        url = f"/api/rooms/{self.room['id']}/attachments"
        self.assertEqual(route.req("GET", url, cookie=False)[0], 401)
        status, body, _ = route.req("GET", url)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["attachments"][0]["name"], "index.html")
        self.assertEqual(json.loads(route.req("GET", url + "?offset=1")[1])["attachments"], [])
        self.assertEqual(route.req("GET", url + "?offset=-1")[0], 400)


if __name__ == "__main__":
    unittest.main()
