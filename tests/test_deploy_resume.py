"""After a restart by an updater, SnapLab holds the cut-off work until the data check passes, then writes the
cut-off replies again and tells the cut-off sessions to go on (snaplab/deploy.py, 2026-11-17)."""
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock

from snaplab import deploy
from tests.helpers import make_app, onboard, wait_until


def marker(tmp, hold, age=0.0):
    path = Path(tmp) / "data" / "deploy.json"
    path.write_text(json.dumps({"hold": hold, "resume": True, "written": time.time() - age}), encoding="utf-8")
    return path


class DeployResumeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="snaplab-deploy-")
        self.app, _, _ = make_app(tmp=self.tmp)
        self.room = onboard(self.app)["room"]["id"]
        wait_until(lambda: not self.app.chat.busy(self.room))
        self.bot = self.app.store.bots()[0]["id"]
        self.app.store.add_message(self.room, {"id": "m-0000beef", "role": "bot", "bot": self.bot, "status": "streaming",
                                               "text": "half a", "thinking": ""}, touch_room=False)
        self.app.store.save_session({"id": "s-0000cafe", "status": "running", "project": "p", "started": "2026-11-17T01:00:00+09:00"})

    def message(self, app, mid):
        return next(x for x in app.store.messages(self.room) if x["id"] == mid)

    def replies(self, app):
        return [m for m in app.store.messages(self.room) if m.get("role") == "bot" and m["id"] != "m-0000beef"]

    def test_a_reply_cut_by_a_deploy_is_written_again(self):
        before = len(self.replies(self.app))
        path = marker(self.tmp, hold=False)
        app2, _, _ = make_app(tmp=self.tmp)
        m = self.message(app2, "m-0000beef")
        self.assertEqual((m["status"], m["error_code"], m.get("retried")), ("error", "restart", True))
        self.assertTrue(wait_until(lambda: any(r.get("status") == "done" for r in self.replies(app2)[before:])))
        self.assertFalse(path.exists())

    def test_a_plain_restart_does_not_write_it_again(self):
        before = len(self.replies(self.app))
        app2, _, _ = make_app(tmp=self.tmp)
        m = self.message(app2, "m-0000beef")
        self.assertEqual((m["status"], m.get("retried")), ("error", None))
        time.sleep(0.3)
        self.assertEqual(len(self.replies(app2)), before)

    def test_a_leftover_marker_resumes_nothing(self):
        before = len(self.replies(self.app))
        path = marker(self.tmp, hold=True, age=deploy.FRESH_S + 5)
        app2, _, _ = make_app(tmp=self.tmp)
        self.assertEqual(self.message(app2, "m-0000beef").get("retried"), None)
        self.assertEqual(len(self.replies(app2)), before)
        self.assertFalse(path.exists())

    def test_held_start_leaves_the_data_alone_until_the_check_passes(self):
        path = marker(self.tmp, hold=True)
        app2, _, _ = make_app(tmp=self.tmp)
        self.assertEqual(self.message(app2, "m-0000beef")["status"], "streaming", "not touched while held")
        self.assertEqual(app2.store.session("s-0000cafe")["status"], "running")
        app2.sessions.send = Mock()
        app2.store.save_project({"id": "p", "slug": "p", "name": "P"})
        app2.store.save_session({"id": "s-0000dead", "status": "running", "project": "gone", "started": "2026-11-17T01:00:00+09:00"})
        started, stopping = threading.Event(), threading.Event()
        self.addCleanup(stopping.set)
        deploy.begin(app2, stopping, started.set)
        time.sleep(0.8)
        self.assertFalse(started.is_set(), "scheduled tasks and summaries wait too")
        marker(self.tmp, hold=False)                       # the updater: the check passed
        self.assertTrue(started.wait(5))
        self.assertTrue(wait_until(lambda: not path.exists()))
        self.assertEqual(self.message(app2, "m-0000beef").get("retried"), True)
        s = app2.store.session("s-0000cafe")
        self.assertEqual((s["status"], s.get("interrupted")), ("stopped", True))
        app2.sessions.send.assert_called_once_with("s-0000cafe", app2.sessions.RESUME_PROMPT, notice=False)
        self.assertEqual(app2.store.session("s-0000dead")["status"], "stopped", "its project is gone: marked, not resumed")

    def test_the_hold_ends_by_itself_when_release_py_never_answers(self):
        marker(self.tmp, hold=True, age=deploy.HOLD_S + 1)
        app2, _, _ = make_app(tmp=self.tmp)
        self.assertEqual(self.message(app2, "m-0000beef").get("retried"), True)


if __name__ == "__main__":
    unittest.main()
