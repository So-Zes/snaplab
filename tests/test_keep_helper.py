"""keep.py keeps processes running across an agent's tool calls and reports on them (synthetic commands only)."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

KEEP = Path(__file__).resolve().parent.parent / "snaplab" / "harnesses" / "keep.py"
SLEEP = [sys.executable, "-c", "import time; print('ready', flush=True); time.sleep(60)"]


class KeepHelperTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = {**os.environ, "SNAPLAB_KEEP_DIR": str(Path(self.tmp.name) / "state")}

    def keep(self, *args, stdin=None):
        p = subprocess.run([sys.executable, str(KEEP), *args], cwd=self.tmp.name, env=self.env, capture_output=True,
                           text=True, input=stdin, timeout=60)
        return p.returncode, p.stdout

    def test_start_list_log_and_stop(self):
        code, out = self.keep("start", "sleeper", "--", *SLEEP)
        self.assertEqual(code, 0, out)
        self.addCleanup(self.keep, "stop", "sleeper")
        self.assertEqual(json.loads(self.keep("list")[1])[0]["running"], True)
        code, out = self.keep("start", "sleeper", "--", *SLEEP)
        self.assertEqual(code, 1)
        self.assertIn("already running", out)
        for _ in range(50):
            if "ready" in self.keep("log", "sleeper")[1]:
                break
        self.assertIn("ready", self.keep("log", "sleeper")[1])
        self.assertEqual(json.loads(self.keep("stop", "sleeper")[1]), {"stopped": "sleeper"})
        self.assertEqual(json.loads(self.keep("list")[1]), [])

    def test_bad_names_and_usage_are_refused(self):
        for args in (("start", "../x", "--", *SLEEP), ("start", "x", *SLEEP), ("stop", "missing"), ("dance",)):
            with self.subTest(args=args[:2]):
                code, out = self.keep(*args)
                self.assertEqual(code, 1)
                self.assertIn("error", json.loads(out))

    def test_supervisor_passes_input_through_and_serves_start_requests(self):
        agent = ("import json, subprocess, sys; print('input:', sys.stdin.read().strip());"
                 f"r = subprocess.run([sys.executable, {str(KEEP)!r}, 'start', 's', '--', *{SLEEP!r}],"
                 " capture_output=True, text=True); print(json.loads(r.stdout)['kept_until'])")
        code, out = self.keep("--supervise", sys.executable, "-c", agent, stdin="prompt text")
        self.assertEqual(code, 0, out)
        self.assertIn("input: prompt text", out)
        self.assertIn("the end of this turn", out)
        self.keep("stop", "s")


if __name__ == "__main__":
    unittest.main()
