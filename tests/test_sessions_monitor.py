"""Parallel and fresh sessions, progress on demand, and wake-ups a bot sets for itself (2026-11-15)."""
import datetime as dt
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from helpers import fake_credentials, make_app, onboard
from snaplab import gitops
from snaplab import tasks as tasksmod
from snaplab.harnesses import adapters
from snaplab.harnesses import remote_worker
from snaplab.tools import Toolbox


def data(result):
    """Tool output as JSON, also when it is marked as untrusted material (sessions on an internet-capable worker)."""
    text = result.content
    return json.loads(text[text.index("{"):text.rindex("}") + 1])


def ago(seconds):
    return (dt.datetime.now().astimezone() - dt.timedelta(seconds=seconds)).isoformat(timespec="seconds")


class Base(unittest.TestCase):
    git = False

    def setUp(self):
        self.creds = fake_credentials()
        self.creds.__enter__()
        self.addCleanup(self.creds.__exit__, None, None, None)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.app, _, _ = make_app(tmp=self.tmp.name, codex={"backend": "ssh", "remote": {"host": "agent.invalid"}})
        self.addCleanup(self.app.archive.close)
        self.bot = onboard(self.app)["bot"]
        self.room = self.app.store.dm_room(self.bot["id"])
        if self.git:
            self.project = self.app.create_project(name="Island", bot=self.bot["id"])
        else:
            with patch("snaplab.gitops.available", return_value=False):
                self.project = self.app.create_project(name="Island", bot=self.bot["id"])
        self.starts = []
        p1 = patch.object(self.app.sessions, "_start", side_effect=lambda sid, prompt, first: self.starts.append(sid))
        p2 = patch("snaplab.harnesses.remote.available", return_value=(True, ""))
        p1.start(), p2.start()
        self.addCleanup(p1.stop)
        self.addCleanup(p2.stop)

    def box(self):
        return Toolbox(self.app, self.bot, room=self.room)

    def start(self, prompt, **kw):
        return json.loads(self.box().t_start_session(self.project["id"], prompt, **kw).content)

    def finish(self, sid, status="done"):
        s = self.app.store.session(sid)
        s["status"] = status
        self.app.store.save_session(s)


class ParallelSessionsTest(Base):
    def test_a_project_with_earlier_work_still_gets_new_sessions(self):
        first = self.start("Build the island scene")
        self.finish(first["session_id"])
        second = self.start("Grade the island scene from scratch")
        third = self.start("Try a different terrain approach")
        self.assertTrue(second["started"] and third["started"])
        self.assertEqual(len({first["session_id"], second["session_id"], third["session_id"]}), 3)

    def test_a_result_card_turn_may_start_other_sessions(self):
        first = self.start("Build it")
        self.finish(first["session_id"], "failed")
        box = self.box()
        box.result_session = first["session_id"]
        out = json.loads(box.t_start_session(self.project["id"], "Review it with fresh eyes",
                                             from_session=first["session_id"]).content)
        self.assertTrue(out["started"])
        self.assertEqual(self.app.store.session(out["session_id"])["from_session"], first["session_id"])

    def test_from_session_must_be_in_the_same_project(self):
        first = self.start("Build it")
        with patch("snaplab.gitops.available", return_value=False):
            other = self.app.create_project(name="Other", bot=self.bot["id"])
        with self.assertRaises(ValueError):
            self.box().t_start_session(other["id"], "Review", from_session=first["session_id"])
        with self.assertRaises(LookupError):
            self.box().t_start_session(self.project["id"], "Review", from_session="s-missing")

    def test_descriptions_no_longer_forbid_new_sessions(self):
        tools = {t["function"]["name"]: t["function"] for t in self.box().schemas()}
        self.assertIn("from_session", tools["start_session"]["parameters"]["properties"])
        self.assertNotIn("Never start another session", tools["send_to_session"]["description"])
        self.assertIn("schedule_wakeup", tools)


@unittest.skipUnless(gitops.available(), "git is not installed")
class FreshSessionFilesTest(Base):
    git = True

    def test_a_fresh_session_starts_from_the_other_sessions_files(self):
        first = self.start("Render four shots")
        s1 = self.app.store.session(first["session_id"])
        Path(s1["worktree"], "shot_0.png").write_bytes(b"\x89PNG fake")
        head = gitops.commit_all(Path(s1["worktree"]), "turn 1")
        self.assertTrue(head)
        self.finish(s1["id"])
        out = self.start("Grade shot_0.png strictly", from_session=s1["id"])
        s2 = self.app.store.session(out["session_id"])
        self.assertTrue(Path(s2["worktree"], "shot_0.png").is_file())
        self.assertEqual(s2["base"], gitops.head(Path(s1["worktree"])))
        self.assertNotEqual(s2["branch"], s1["branch"])
        self.assertIn("no conversation", out["files"])


@unittest.skipUnless(gitops.available(), "git is not installed")
class GitCeilingTest(unittest.TestCase):
    def test_a_folder_without_git_never_commits_the_enclosing_repository(self):
        with tempfile.TemporaryDirectory() as tmp:
            outer = Path(tmp)
            gitops.init_repo(outer, {"README.md": "outer"})
            before = gitops.head(outer)
            inner = outer / "data" / "projects" / "game"
            inner.mkdir(parents=True)
            (outer / "README.md").write_text("changed but not to be committed")
            (inner / "index.html").write_text("<p>game</p>")
            self.assertIsNone(gitops.head(inner))
            gitops.commit_all(inner, "Initialize experiment game template")
            self.assertEqual(gitops.head(outer), before)


class ProgressTest(Base):
    def test_get_session_shows_where_a_running_session_is(self):
        sid = self.start("Make the shots")["session_id"]
        s = self.app.store.session(sid)
        s.update(status="running", started=ago(600), turns=2,
                 plan={"steps": [{"text": "code", "done": True}, {"text": "shots", "done": False}], "done": 1, "total": 2, "now": "shots"})
        self.app.store.save_session(s)
        self.app.store.session_event(sid, {"kind": "command", "text": "ls", "exit_code": 0, "output": "index.html", "ts": ago(400)})
        self.app.store.session_event(sid, {"kind": "start", "text": "node shot.mjs --cam 0", "ts": ago(300)})
        view = data(self.box().t_get_session(sid))
        pr = view["progress"]
        self.assertEqual(pr["step"], "1/2")
        self.assertEqual(pr["doing"], "shots")
        self.assertEqual(pr["turn"], 3)
        self.assertGreaterEqual(pr["running_for_s"], 590)
        self.assertGreaterEqual(pr["idle_s"], 290)
        self.assertEqual(pr["in_flight"]["text"], "node shot.mjs --cam 0")
        self.assertEqual([e["kind"] for e in view["recent"]][-2:], ["command", "start"])
        rows = json.loads(self.box().t_list_sessions(status="active").content)
        self.assertEqual(rows[0]["id"], sid)
        self.assertEqual(rows[0]["step"], "1/2")

    def test_more_records_only_when_asked(self):
        sid = self.start("Make the shots")["session_id"]
        self.finish(sid)
        for i in range(30):
            self.app.store.session_event(sid, {"kind": "command", "text": f"step {i}", "exit_code": 0})
        self.assertNotIn("recent", data(self.box().t_get_session(sid)))
        view = data(self.box().t_get_session(sid, events=25))
        self.assertEqual(len(view["recent"]), 25)

    def test_streamed_plan_is_stored(self):
        sid = self.start("Make the shots")["session_id"]
        from snaplab import plan as planmod
        self.app.sessions.set_plan(sid, planmod.parse("- [x] code\n- [ ] shots\n"))
        s = self.app.store.session(sid)
        self.assertEqual(s["plan"]["now"], "shots")
        self.assertTrue(s["plan_updated"])


class WakeupTest(Base):
    def test_wakeup_runs_once_in_the_room_where_it_was_set(self):
        out = json.loads(self.box().t_schedule_wakeup(5, "Check whether s-123 started").content)
        task = self.app.tasks.get(out["task_id"])
        self.assertEqual(task["schedule"]["kind"], "once")
        self.assertEqual(task["room"], self.room["id"])
        with patch.object(self.app.chat, "_enqueue") as enqueue:
            self.assertEqual(self.app.tasks.tick(dt.datetime.now().astimezone() + dt.timedelta(minutes=1)), [])
            ran = self.app.tasks.tick(dt.datetime.now().astimezone() + dt.timedelta(minutes=6))
        self.assertEqual(ran, [out["task_id"]])
        enqueue.assert_called_once()
        self.assertIsNone(self.app.tasks.get(out["task_id"]))
        note = [m for m in self.app.store.messages(self.room["id"], 20) if m.get("task") == out["task_id"]]
        self.assertTrue(note and "Check whether s-123 started" in note[-1]["text"])

    def test_no_minimum_interval_and_no_task_cap(self):
        for i in range(40):
            self.box().t_schedule_wakeup(0.5, f"note {i}")
        self.assertEqual(len(self.app.tasks.all()), 40)
        self.assertEqual(tasksmod.clean_schedule({"kind": "every", "every_minutes": 1})["every_minutes"], 1)


class WorkerRecordsTest(unittest.TestCase):
    def test_pi_records_say_which_command_ran(self):
        start = adapters.events("pi", {"type": "tool_execution_start", "toolCallId": "c1", "toolName": "bash",
                                       "args": {"command": "node shot.mjs"}})
        self.assertEqual(start, [{"kind": "start", "text": "node shot.mjs"}])
        end = adapters.events("pi", {"type": "tool_execution_end", "toolCallId": "c1", "toolName": "bash", "isError": False,
                                     "durationMs": 1500, "result": {"content": [{"type": "text", "text": "saved shot_0.png"}]}})
        self.assertEqual(end[0]["text"], "node shot.mjs")
        self.assertEqual(end[0]["output"], "saved shot_0.png")
        self.assertEqual(end[0]["duration_s"], 1.5)

    def test_failures_keep_their_reason(self):
        pi = adapters.events("pi", {"type": "message_end", "message": {"role": "assistant", "stopReason": "error",
                                                                      "errorMessage": "socket hang up", "content": []}})
        self.assertIn({"kind": "failure"}, pi)
        self.assertIn({"kind": "error", "text": "Pi (error): socket hang up"}, pi)
        codex = adapters.events("codex", {"type": "turn.failed", "error": {"message": "stream disconnected"}})
        self.assertEqual(codex, [{"kind": "failure"}, {"kind": "error", "text": "Codex: stream disconnected"}])

    def test_codex_command_start_is_recorded(self):
        out = adapters.events("codex", {"type": "item.started", "item": {"type": "command_execution", "command": "npm test"}})
        self.assertEqual(out, [{"kind": "start", "text": "npm test"}])

    def test_worker_streams_plan_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan = Path(tmp, ".snaplab", "PLAN.md")
            plan.parent.mkdir()
            plan.write_text("- [ ] one\n")
            sent, ended = [], threading.Event()
            with patch.object(remote_worker, "emit", side_effect=sent.append):
                t = threading.Thread(target=remote_worker.watch_plan, args=(tmp, ended, 0.02))
                t.start()
                deadline = dt.datetime.now() + dt.timedelta(seconds=3)
                while not sent and dt.datetime.now() < deadline:
                    ended.wait(0.02)
                plan.write_text("- [x] one\n- [ ] two\n")
                while len(sent) < 2 and dt.datetime.now() < deadline:
                    ended.wait(0.02)
                ended.set()
                t.join()
            self.assertEqual([x["kind"] for x in sent[:2]], ["plan", "plan"])
            self.assertIn("- [x] one", sent[1]["text"])


if __name__ == "__main__":
    unittest.main()
