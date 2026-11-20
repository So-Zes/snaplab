"""Round 9: synthetic data, in-memory HTTP and fake web providers; no sockets or Git."""
import datetime as dt
import io
import json
import tempfile
import threading
import tomllib
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import quote

from helpers import fake_credentials, make_app, onboard, wait_until
from snaplab.agent import LoopResult
from snaplab.harnesses.base import TurnResult
from snaplab.archive import Scope
from snaplab.guard import variants
from snaplab.harnesses import codex
from snaplab.migrate import SCHEMA, run as migrate
from snaplab.proxy import _Handler
from snaplab.store import Store
from snaplab.tools import CHAT_TOOLS
from snaplab.web_mcp import archive_knowledge
import test_round7


class Round9Test(unittest.TestCase):
    def setUp(self):
        self.enterContext(fake_credentials())
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.app, self.llm, _ = make_app(tmp=self.tmp.name, summary={"enabled": False}, recall={"rerank": False})
        self.addCleanup(self.app.archive.close)
        self.bot = onboard(self.app)["bot"]
        self.other = self.app.create_bot(name="Builder", role="Code")
        self.scope = Scope(everything=True)
        self.session = {"id": "s-00000009", "bot": self.bot["id"], "project": "p-00000009", "harness": "codex", "status": "running"}
        self.app.store.save_session(self.session)
        self.events = []
        self.ctl = SimpleNamespace(cancelled=lambda: False, emit=self.events.append)
        self.token = self.app.web_mcp.activate(self.session, self.ctl)
        self.addCleanup(self.app.web_mcp.revoke, self.session["id"])

    def group(self):
        return self.app.create_group("Game team", [self.bot["id"], self.other["id"]])

    def records(self):
        return [r for p in self.app.archive.dir.glob("*/*.jsonl") for r in self.app.store.read_jsonl(p)]

    def rpc(self, method, params=None):
        return self.app.web_mcp.dispatch(self.session["id"], self.token,
            {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}})

    def call(self, name, args):
        return self.rpc("tools/call", {"name": name, "arguments": args})

    def test_v3_rooms_need_no_migration_and_pin_survives_reload(self):
        self.app.store.write_json("schema.json", {"version": 3})
        dm = self.app.store.dm_room(self.bot["id"])
        self.assertNotIn("pinned", dm)
        before = self.app.store.path("schema.json").read_bytes()
        migrate(self.app)
        self.assertEqual(SCHEMA, 3)
        self.assertEqual(before, self.app.store.path("schema.json").read_bytes())
        for room in (dm, self.group()):
            self.app.update_room(room["id"], {"pinned": True})
            self.assertTrue(Store(self.app.store.dir).room(room["id"])["pinned"])
            self.app.update_room(room["id"], {"pinned": False})
            self.assertFalse(self.app.store.room(room["id"])["pinned"])

    def test_group_rename_add_remove_and_direct_chat_rename(self):
        r = self.group()
        self.app.update_room(r["id"], {"name": "  New  name ", "bots": [self.other["id"]]})
        self.assertEqual(self.app.store.room(r["id"])["name"], "New name")
        self.assertIsNone(self.app.chat.run_turn(r["id"], self.bot["id"]))
        self.app.update_room(r["id"], {"bots": [self.other["id"], self.bot["id"]]})
        self.assertEqual(len(self.app.store.room(r["id"])["bots"]), 2)
        self.app.update_bot(self.other["id"], {"name": "Renamed"})
        self.assertEqual(self.app.store.dm_room(self.other["id"])["name"], "Renamed")

    def test_room_validation_is_atomic(self):
        r = self.group()
        for change in ({"bots": []}, {"bots": ["missing"]}, {"bots": [self.bot["id"]] * 2}, {"name": " "},
                       {"name": "a" * 41}, {"name": "Valid", "bots": []}, {"pinned": "true"}, {"deleted": True}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.app.update_room(r["id"], change)
            self.assertEqual(self.app.store.room(r["id"]), r)
        dm = self.app.store.dm_room(self.bot["id"])
        for change in ({"name": "X"}, {"bots": [self.other["id"]]}, {"action": "delete", "confirmed": True}):
            with self.assertRaises(ValueError):
                self.app.update_room(dm["id"], change)

    def test_group_delete_requires_confirmation_keeps_originals_and_cancels_cards(self):
        r = self.group()
        self.app.store.add_message(r["id"], {"role": "user", "text": "Keep this original conversation"})
        q = self.app.store.save_question({"id": "q-00000009", "room": r["id"], "bot": self.bot["id"], "status": "open", "question": "Continue?"})
        msg_path = self.app.store.path("messages", r["id"] + ".jsonl")
        original = msg_path.read_bytes()
        originals = {p: p.read_bytes() for p in self.app.archive.dir.glob("*/*.jsonl")}
        with self.assertRaises(ValueError):
            self.app.update_room(r["id"], {"action": "delete"})
        with patch.object(self.app.chat, "stop", wraps=self.app.chat.stop) as stop:
            self.app.update_room(r["id"], {"action": "delete", "confirmed": True})
            stop.assert_called_once_with(r["id"])
        self.assertIsNone(self.app.store.room(r["id"]))
        self.assertNotIn(r["id"], [x["id"] for x in self.app.bootstrap()["rooms"]])
        self.assertEqual(msg_path.read_bytes(), original)
        for p, data in originals.items():
            self.assertTrue(p.read_bytes().startswith(data))
        self.assertTrue(self.app.archive.search("original conversation", self.scope))
        self.assertEqual(self.app.store.question(q["id"])["status"], "cancelled")
        with self.assertRaises(LookupError):
            self.app.chat.post(r["id"], "not allowed")
        # Late writes cannot restore the hidden room.
        self.app.store.add_message(r["id"], {"role": "bot", "bot": self.bot["id"], "text": "late reply"})
        self.assertIsNone(Store(self.app.store.dir).room(r["id"]))

    def test_all_work_is_available_beyond_sixty_sessions(self):
        for i in range(65):
            self.app.store.save_session({**self.session, "id": f"s-{i:08x}", "status": "idle"})
        self.assertEqual(len(self.app.bootstrap()["sessions"]), 65)

    def test_delete_during_reply_cannot_reopen_question_or_publish_null_room(self):
        room = self.group()
        def replying(llm, messages, schemas, box, **kwargs):
            box.t_ask_user("Continue?")
            self.app.update_room(room["id"], {"action": "delete", "confirmed": True})
            self.assertTrue(kwargs["cancel"]())
            return LoopResult("Question saved", "ask", 1, 0, 0, 0)
        with patch("snaplab.chat.run_loop", side_effect=replying), patch.object(self.app.bus, "publish", wraps=self.app.bus.publish) as publish:
            self.app.chat.run_turn(room["id"], self.bot["id"])
        self.assertFalse(any(q["status"] == "open" and q["room"] == room["id"] for q in self.app.store.questions()))
        self.assertFalse(any(c.args[0] == "room.updated" and c.args[1]["room"] is None for c in publish.call_args_list))
        with self.assertRaises(ValueError):
            self.app.chat.create_question(room, self.bot, question="Too late")

    def test_session_completion_archives_knowledge_even_without_git(self):
        project = self.app.create_project(name="Research game", description="Synthetic game")
        harness = self.app.sessions.harnesses["codex"]
        def turn(session, folder, prompt, ctl, first):
            (folder / ".snaplab").mkdir(exist_ok=True)
            (folder / ".snaplab/KNOWLEDGE.md").write_text("Game notes from https://example.org/", encoding="utf-8")
            return TurnResult(ok=True, last_message="Research recorded.")
        with patch.object(harness, "available", return_value=(True, "")), patch.object(harness, "run_turn", side_effect=turn):
            session = self.app.sessions.create(project, "codex", "Research the game", bot=self.bot["id"])
            self.assertTrue(wait_until(lambda: self.app.store.session(session["id"])["status"] == "review"))
            # The review status precedes the final state/report writes.
            for worker in threading.enumerate():
                if worker.name == "session-" + session["id"]:
                    worker.join(timeout=5)
                    self.assertFalse(worker.is_alive(), "session must finish before temporary data is removed")
        records = [r for r in self.records() if r.get("key") == "knowledge:" + session["id"]]
        self.assertEqual(len(records), 1)
        self.assertIn("Game notes", records[0]["text"])

    def test_agenda_daily_weekly_interval_paused_and_day_boundary(self):
        now = dt.datetime(2026, 11, 1, 23, 59, tzinfo=dt.timezone(dt.timedelta(hours=9)))
        task = {"status": "active", "schedule": {"kind": "daily", "time": "08:00"}}
        times = self.app.tasks.occurrences(task, now)
        self.assertEqual([x[:16] for x in times], ["2026-11-01T08:00", "2026-11-02T08:00"])
        task["schedule"] = {"kind": "weekly", "time": "18:00", "days": ["mon"]}
        self.assertEqual(len(self.app.tasks.occurrences(task, now)), 1)
        task["schedule"] = {"kind": "every", "every_minutes": 30}
        task["next_run"] = "2026-11-01T23:45:00+09:00"
        times = self.app.tasks.occurrences(task, now)
        self.assertEqual(len(times), 49)
        self.assertTrue(times[-1].startswith("2026-11-02T23:45"))
        task["status"] = "paused"
        self.assertEqual(self.app.tasks.occurrences(task, now), [])

    def test_mcp_initialize_lists_only_two_tools_and_rejects_invalid_requests(self):
        init = self.rpc("initialize", {"protocolVersion": "2025-03-26"})["result"]
        self.assertEqual(init["protocolVersion"], "2025-03-26")
        self.assertIn("untrusted", init["instructions"])
        self.assertEqual({x["name"] for x in self.rpc("tools/list")["result"]["tools"]}, {"web_search", "read_page"})
        denied = {t["function"]["name"] for t in CHAT_TOOLS} - {"web_search", "read_page", "generate_image"}
        denied.update({"write_file", "shell", "exec_command", "mcp__snaplab_web__web_search"})
        with patch("snaplab.web_mcp.Toolbox") as toolbox:
            for name in sorted(denied):
                with self.subTest(tool=name):
                    self.assertEqual(self.call(name, {})["error"]["code"], -32602)
            toolbox.assert_not_called()
        self.assertIn("error", self.call([], {}))
        self.assertIn("error", self.rpc("resources/list"))
        self.assertIsNone(self.app.web_mcp.dispatch(self.session["id"], self.token,
            {"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "web_search", "arguments": {"query": "ignored"}}}))
        self.assertEqual(self.app.searcher.queries, [])

    def test_mcp_capability_is_per_turn_per_session_and_stop_revokes_access(self):
        for sid, token in (("s-00000008", self.token), (self.session["id"], "bad")):
            with self.assertRaises(PermissionError):
                self.app.web_mcp.dispatch(sid, token, {})
        self.ctl.cancelled = lambda: True
        with self.assertRaises(PermissionError):
            self.rpc("tools/list")
        self.ctl.cancelled = lambda: False
        self.app.web_mcp.revoke(self.session["id"])
        with self.assertRaises(PermissionError):
            self.rpc("tools/list")

    def test_web_results_are_wrapped_and_full_pages_are_archived_in_session_scope(self):
        res = self.call("web_search", {"query": "game collision", "count": 3})["result"]
        self.assertFalse(res["isError"])
        self.assertIn("untrusted web material", res["content"][0]["text"])
        full = "A useful finding. " * 2000
        self.app._page_reader = lambda url: {"url": url, "title": "Guide", "text": full[:12000], "full_text": full, "truncated": True}
        res = self.call("read_page", {"url": "https://example.org/guide"})["result"]
        self.assertLess(len(res["content"][0]["text"]), len(full))
        page = next(r for r in self.records() if r["kind"] == "page")
        self.assertEqual(page["text"], full)
        self.assertEqual(page["session"], self.session["id"])
        self.assertEqual(page["project"], self.session["project"])
        self.assertEqual(len([r for r in self.records() if r["kind"] == "tool"]), 2)
        self.assertEqual(len(self.llm.calls), 0)

    def test_secret_arguments_and_encodings_never_reach_web_or_archive(self):
        secret = "round9-fake-secret+/value"
        self.app.guard.add(secret)
        encoded = variants(secret) | {quote(secret, safe=""), "".join("%%%02X" % b for b in secret.encode())}
        with patch.object(self.app, "read_page") as reader:
            for value in encoded:
                for name, args in (("web_search", {"query": "game " + value}), ("read_page", {"url": "https://example.org/?q=" + value})):
                    self.assertTrue(self.call(name, args)["result"]["isError"])
            reader.assert_not_called()
        self.assertEqual(self.app.searcher.queries, [])
        self.assertFalse(any(r["kind"] == "tool" for r in self.records()))

    def test_registry_provider_and_search_keys_are_guarded(self):
        secret = "fake-registry-value-12345"
        self.assertIn("BRAVE_API_KEY", self.app.guard.extra_env)
        self.assertIn("TAVILY_API_KEY", self.app.guard.extra_env)
        with fake_credentials(registry={name: secret for name in self.app.guard.extra_env}):
            self.assertTrue(self.call("web_search", {"query": secret})["result"]["isError"])
        self.assertEqual(self.app.searcher.queries, [])

    def test_json_escaped_secrets_and_same_page_in_different_sessions(self):
        secret = 'fake-secret-with-"quote\\slash'
        self.app.guard.add(secret)
        self.assertTrue(self.call("web_search", {"query": "game " + secret})["result"]["isError"])
        url = "https://example.org/shared"
        self.call("read_page", {"url": url})
        session = {**self.session, "id": "s-00000010", "project": "p-00000010"}
        token = self.app.web_mcp.activate(session, self.ctl)
        self.addCleanup(self.app.web_mcp.revoke, session["id"])
        self.app.web_mcp.dispatch(session["id"], token, {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "read_page", "arguments": {"url": url}}})
        pages = [r for r in self.records() if r["kind"] == "page"]
        self.assertEqual({r["session"] for r in pages}, {self.session["id"], session["id"]})

    def test_bad_arguments_and_private_pages_keep_existing_boundaries(self):
        for args in ({"query": ""}, {"query": "x", "count": True}, {"query": "x", "count": 100}, {"query": "x", "extra": "x"}):
            self.assertTrue(self.call("web_search", args)["result"]["isError"])
        self.app._page_reader = None
        with patch("snaplab.websearch._http") as http:
            for url in ("file:///local", "https://127.0.0.1/", "http://192.168.1.1/", "http://100.64.0.1/", "http://[::1]/"):
                self.assertTrue(self.call("read_page", {"url": url})["result"]["isError"])
            http.assert_not_called()

    def test_knowledge_versions_are_guarded_archived_and_readable_without_git(self):
        folder = Path(self.tmp.name) / "work"
        (folder / ".snaplab").mkdir(parents=True)
        path = folder / ".snaplab/KNOWLEDGE.md"
        self.app.guard.add("fake-secret-for-knowledge")
        path.write_text("Collision guide https://example.org/ fake-secret-for-knowledge", encoding="utf-8")
        archive_knowledge(self.app, self.session, folder)
        archive_knowledge(self.app, self.session, folder)
        path.write_text("Updated collision findings", encoding="utf-8")
        archive_knowledge(self.app, self.session, folder)
        versions = [r for r in self.records() if r.get("key") == "knowledge:" + self.session["id"]]
        self.assertEqual(len(versions), 2)
        self.assertNotIn("fake-secret-for-knowledge", versions[0]["text"])
        self.assertTrue(all(r["meta"]["untrusted"] for r in versions))
        self.assertEqual(versions[1]["meta"]["supersedes"], [versions[0]["id"]])
        path.write_bytes(b"x" * 1_000_001)
        with self.assertRaises(ValueError):
            archive_knowledge(self.app, self.session, folder)

    def test_knowledge_archive_failure_does_not_leave_session_running(self):
        project = self.app.create_project(name="Research limit", description="Synthetic game")
        harness = self.app.sessions.harnesses["codex"]
        with patch.object(harness, "available", return_value=(True, "")), \
             patch.object(harness, "run_turn", return_value=TurnResult(ok=True, last_message="Done")), \
             patch("snaplab.web_mcp.archive_knowledge", side_effect=ValueError("too large")):
            session = self.app.sessions.create(project, "codex", "Research", bot=self.bot["id"])
            self.assertTrue(wait_until(lambda: self.app.store.session(session["id"])["status"] == "review"))
            for worker in threading.enumerate():
                if worker.name == "session-" + session["id"]:
                    worker.join(timeout=5)
                    self.assertFalse(worker.is_alive())
        events = self.app.store.session_events(session["id"])
        self.assertTrue(any(e.get("key") == "ev.knowledge_failed" for e in events))

    def test_generated_codex_config_keeps_commands_offline_and_capability_out_of_files(self):
        with patch.object(codex, "base_instructions", return_value=None):
            text = codex.config_text(self.app, workspace=Path(self.tmp.name), git_dir=None, sid=self.session["id"])
        cfg = tomllib.loads(text)
        self.assertFalse(cfg["permissions"]["snaplab"]["network"]["enabled"])
        self.assertEqual(cfg["web_search"], "disabled")
        self.assertEqual(cfg["mcp_servers"]["snaplab_web"]["enabled_tools"], ["web_search", "read_page"])
        self.assertNotIn(self.token, text)
        self.assertNotIn("SNAPLAB_WEB_TOKEN", codex.child_env(self.app, Path(self.tmp.name)))
        self.assertIn("*TOKEN*", cfg["shell_environment_policy"]["exclude"])
        with patch.object(codex.CodexHarness, "_run_turn", side_effect=RuntimeError("failed to start")):
            with self.assertRaises(RuntimeError):
                codex.CodexHarness(self.app).run_turn(self.session, Path(self.tmp.name), "Build", self.ctl, False)
        self.assertNotIn(self.session["id"], self.app.web_mcp.turns)

    def test_mcp_real_http_parser_auth_limits_and_json_responses(self):
        def request(body, *, token=None, origin=None, host="127.0.0.1:18432", method="POST", length=None):
            data = json.dumps(body).encode()
            headers = {"Host": host, "Content-Type": "application/json", "Content-Length": str(len(data) if length is None else length),
                       "Authorization": "Bearer " + (token if token is not None else self.token)}
            if origin:
                headers["Origin"] = origin
            raw = (f"{method} /mcp/{self.session['id']} HTTP/1.0\r\n" + "".join(f"{k}: {v}\r\n" for k, v in headers.items()) + "\r\n").encode() + data
            class Connection:
                output = bytearray()
                def settimeout(self, value): pass
                def makefile(self, *args): return io.BytesIO(raw)
                def sendall(self, value): self.output.extend(value)
            connection = Connection()
            owner = SimpleNamespace(web_mcp=self.app.web_mcp)
            cls = type("MCPHandler", (_Handler,), {"owner": owner})
            cls(connection, ("127.0.0.1", 12345), SimpleNamespace(server_port=18432))
            head, data = bytes(connection.output).split(b"\r\n\r\n", 1)
            return int(head.split()[1]), json.loads(data) if data else None
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        dispatch = self.app.web_mcp.dispatch
        # 2026-11-16: the translator has no read deadline any more, so there is nothing to end before the tool runs.
        with patch.object(self.app.web_mcp, "dispatch", side_effect=dispatch):
            self.assertEqual(request(body)[0], 200)
        self.assertEqual(request(body)[0], 200)
        self.assertEqual(request(body, token="bad")[0], 401)
        self.assertEqual(request(body, origin="https://example.org")[0], 403)
        self.assertEqual(request(body, host="example.org")[0], 403)
        self.assertEqual(request(body, length=32769)[0], 413)
        self.assertEqual(request(body, method="GET")[0], 405)
        self.assertEqual(request({"jsonrpc": "2.0", "method": "notifications/initialized"}), (202, None))


class RoomRouteTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        test_round7.AvatarRouteTest.setUpClass.__func__(cls)
    req = classmethod(test_round7.AvatarRouteTest.req.__func__)

    def test_room_updates_use_existing_auth_and_origin_gates(self):
        r = self.app.create_group("Route group", [self.app.store.orchestrator()["id"]])
        url = "/api/rooms/" + r["id"]
        for options, status in (({"cookie": False}, 401), ({"user": None}, 403), ({"xhdr": False}, 403), ({"origin": "https://example.org"}, 403)):
            self.assertEqual(self.req("POST", url, {"pinned": True}, **options)[0], status)
        self.assertEqual(self.req("POST", url, {"pinned": True})[0], 200)
        self.assertEqual(self.req("POST", url, {"action": "delete"})[0], 400)
        self.assertEqual(self.req("POST", url, {"action": "delete", "confirmed": True})[0], 200)
        self.assertEqual(self.req("POST", url, {"pinned": True})[0], 404)
