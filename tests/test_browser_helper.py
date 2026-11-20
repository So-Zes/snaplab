"""The SnapCode browser helper (.snaplab/browser.mjs) drives a real local Chromium/Edge against a synthetic page."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

HARNESSES = Path(__file__).resolve().parent.parent / "snaplab" / "harnesses"
PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>Helper test</title></head><body>
<button id="start">Start game</button><p id="status">idle</p><input id="name"><p id="hello"></p>
<canvas id="c" width="200" height="100"></canvas>
<script>
let x = 0; window.game = { get x() { return x; }, clicks: 0 };
document.getElementById('start').onclick = async () => {
  const lv = await (await fetch('data/level.json')).json(); document.getElementById('status').textContent = 'Level ' + lv.level; };
document.getElementById('name').addEventListener('input', e => { document.getElementById('hello').textContent = 'Hi ' + e.target.value; });
addEventListener('keydown', e => { if (e.key === 'ArrowRight') x += 1; });
document.getElementById('c').addEventListener('mousedown', () => window.game.clicks++);
fetch('data/missing.json'); console.error('synthetic console error');
</script></body></html>"""


def browser_available():
    if os.name == "nt":
        bases = [os.environ.get(v) for v in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA")]
        return any(b and (Path(b, "Google/Chrome/Application/chrome.exe").is_file()
                          or Path(b, "Microsoft/Edge/Application/msedge.exe").is_file()) for b in bases)
    return any(shutil.which(n) for n in ("chromium", "chromium-browser", "google-chrome"))


@unittest.skipUnless(shutil.which("node") and browser_available(), "needs Node.js and Chromium, Chrome or Edge")
class BrowserHelperTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / ".snaplab").mkdir()
        for name in ("capture.mjs", "browser.mjs", "keep.py"):
            shutil.copy(HARNESSES / name, self.root / ".snaplab" / name)
        (self.root / "data").mkdir()
        (self.root / "data" / "level.json").write_text('{"level": 3}')
        (self.root / "index.html").write_text(PAGE, encoding="utf-8")

    def run_helper(self, *args):
        env = {**os.environ, "SNAPLAB_KEEP_DIR": str(self.root / "keep-state"), "SNAPLAB_PYTHON": sys.executable}
        p = subprocess.run(["node", ".snaplab/browser.mjs", *args], cwd=self.root, capture_output=True, timeout=180, env=env)
        return p.returncode, json.loads(p.stdout.decode("utf-8"))

    def test_a_user_flow_runs_and_reports_page_problems(self):
        code, out = self.run_helper("index.html", "click:text=Start game", "wait:text=Level 3", "press:ArrowRight",
                                    "type:#name=Alex", "click:#c", "eval:window.game", "text", "shot:shots/a.png")
        self.assertEqual(code, 0, out)
        steps = {s["step"]: s for s in out["steps"]}
        self.assertEqual(steps["eval:window.game"]["value"], {"x": 1, "clicks": 1})
        self.assertIn("Hi Alex", steps["text"]["value"])
        self.assertTrue((self.root / "shots" / "a.png").read_bytes().startswith(b"\x89PNG"))
        self.assertIn("404 data/missing.json", out["failed_requests"])
        self.assertTrue(any("synthetic console error" in e for e in out["console_errors"]))

    def test_a_failed_step_stops_the_run_with_a_reason(self):
        code, out = self.run_helper("index.html", "click:#absent", "shot:never.png")
        self.assertEqual(code, 1)
        self.assertEqual(len(out["steps"]), 1)
        self.assertIn("#absent", out["steps"][0]["error"])
        self.assertFalse((self.root / "never.png").exists())

    def test_live_browser_keeps_page_state_between_commands(self):
        code, out = self.run_helper("start")
        self.addCleanup(self.run_helper, "stop")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.run_helper("open", "index.html")[1]["screenshot"], ".snaplab/browser.png")
        self.assertEqual(self.run_helper("do", "press:ArrowRight")[0], 0)
        self.assertEqual(self.run_helper("do", "press:ArrowRight")[0], 0)
        code, out = self.run_helper("do", "eval:window.game.x")
        self.assertEqual(out["steps"][0]["value"], 2)                 # state survived two separate commands
        code, out = self.run_helper("do", "reload", "eval:window.game.x")
        self.assertEqual(out["steps"][1]["value"], 0)
        self.assertTrue((self.root / ".snaplab" / "browser.png").read_bytes().startswith(b"\x89PNG"))
        self.assertEqual(self.run_helper("stop")[0], 0)
        code, out = self.run_helper("do", "press:ArrowRight")
        self.assertEqual(code, 1)
        self.assertIn("no live browser", out["error"])

    def test_files_outside_the_workspace_are_refused(self):
        code, out = self.run_helper("index.html", "shot:../outside.png")
        self.assertEqual(code, 1)
        self.assertIn("outside the workspace", out["steps"][0]["error"])
        code, out = self.run_helper("../index.html")
        self.assertEqual(code, 1)
        self.assertIn("outside the workspace", out["error"])


if __name__ == "__main__":
    unittest.main()
