"""Real sandbox check (Windows with the Codex CLI only; skipped elsewhere).

Runs commands inside the exact sandbox a Codex session gets (the generated per-session permission profile on
mxc) and proves that files outside the session's worktree cannot be read, while the worktree, git, Python
and tests keep working and the network stays off. No model is called.
"""
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from helpers import ROOT, make_app

from snaplab import gitops
from snaplab.harnesses import codex as codexmod


def _work_dir():
    """Session folders must not be in the system temp folder: the sandbox cannot read it (QA round 2, M-7)."""
    d = ROOT / "runtime"
    d.mkdir(exist_ok=True)
    return d


@unittest.skipUnless(os.name == "nt" and codexmod.find_codex() and gitops.available(), "needs Windows, Codex CLI and git")
class SandboxReadTest(unittest.TestCase):
    def test_outside_reads_are_blocked(self):
        base = Path(tempfile.mkdtemp(prefix="sbx-", dir=_work_dir()))
        try:
            (base / "outside-secret.txt").write_text("CANARY-OUTSIDE-7f3a", encoding="utf-8")
            app, _, _ = make_app(tmp=str(base / "app"))
            p = app.create_project(name="Sandbox check", description="check reads", test_command="python -m unittest -v")
            s = app.sessions  # noqa: F841
            pdir = Path(app.cfg["projects_dir"]) / p["slug"]
            wt, branch, base_commit = gitops.add_worktree(pdir, "s-0000abcd")
            (wt / "inside.txt").write_text("INSIDE-OK", encoding="utf-8")
            (wt / "test_x.py").write_text("import unittest\nclass T(unittest.TestCase):\n    def test_ok(self):\n        self.assertEqual(1, 1)\n",
                                          encoding="utf-8")
            codexmod.write_session_home(app, "s-0000abcd", wt, gitops.git_dir(pdir))
            outside = base / "outside-secret.txt"
            home_git = Path.home() / ".gitconfig"
            script = "\n".join([
                f"try {{ Get-Content '{outside}' -ErrorAction Stop }} catch {{ 'R1-BLOCKED' }}",
                f"try {{ Get-Content '{pdir / 'BRIEF.md'}' -ErrorAction Stop | Out-Null; 'R2-MAIN-READABLE' }} catch {{ 'R2-BLOCKED' }}",
                f"try {{ Get-ChildItem '{Path.home()}' -ErrorAction Stop | Out-Null; 'R3-HOME-LISTED' }} catch {{ 'R3-BLOCKED' }}",
                (f"try {{ Get-Content '{home_git}' -ErrorAction Stop | Out-Null; 'R4-GITCONFIG-READ' }} catch {{ 'R4-BLOCKED' }}"
                 if home_git.exists() else "'R4-BLOCKED'"),
                f"try {{ Set-Content -Path '{base / 'written-outside.txt'}' -Value x -ErrorAction Stop; 'W1-WRITTEN' }} catch {{ 'W1-BLOCKED' }}",
                "Get-Content inside.txt",
                "Set-Content -Path made.txt -Value hi; if (Test-Path made.txt) { 'W2-INSIDE-OK' }",
                "python -c \"print('PY-' + 'OK')\"",
                "git status --short 2>$null | Out-Null; if ($LASTEXITCODE -eq 0) { 'GIT-OK' } else { 'GIT-FAILED' }",
                "try { (Invoke-WebRequest -UseBasicParsing https://example.com -TimeoutSec 6).StatusCode } catch { 'NET-BLOCKED' }",
                "python -m unittest -v 2>&1 | Out-String",
                "if ($env:SNAPSCRIBE_API_KEY) { 'ENV-KEY-VISIBLE' } else { 'ENV-KEY-ABSENT' }",
                "if ($env:SNAPLAB_PROXY_TOKEN) { 'ENV-TOKEN-VISIBLE' } else { 'ENV-TOKEN-ABSENT' }",
                f"try {{ Get-ChildItem '{app.cfg['data_dir']}' -ErrorAction Stop | Out-Null; 'DATA-LISTED' }} catch {{ 'DATA-BLOCKED' }}",
            ])
            code, out = codexmod.sandbox_run(app, "s-0000abcd", wt, script, timeout_s=120, git_dir=gitops.git_dir(pdir))
            for want in ("R1-BLOCKED", "R2-BLOCKED", "R3-BLOCKED", "R4-BLOCKED", "W1-BLOCKED", "INSIDE-OK", "W2-INSIDE-OK",
                         "PY-OK", "GIT-OK", "NET-BLOCKED", "Ran 1 test", "ENV-KEY-ABSENT", "ENV-TOKEN-ABSENT", "DATA-BLOCKED"):
                self.assertIn(want, out, f"missing {want} in sandbox output:\n{out[-1500:]}")
            self.assertNotIn("CANARY-OUTSIDE-7f3a", out)
            self.assertFalse((base / "written-outside.txt").exists())
        finally:
            shutil.rmtree(base, ignore_errors=True)

    def test_run_tests_harness_end_to_end(self):
        """The "Run tests" harness: a session that only runs the project's tests in the sandbox (no model)."""
        from helpers import wait_until
        base = Path(tempfile.mkdtemp(prefix="sbx-", dir=_work_dir()))
        try:
            app, llm, _ = make_app(tmp=str(base / "app"))
            from helpers import onboard
            onboard(app)
            p = app.create_project(name="Tests only", description="d", test_command="python -m unittest -v")
            pdir = Path(app.cfg["projects_dir"]) / p["slug"]
            (pdir / "test_ok.py").write_text("import unittest\nclass T(unittest.TestCase):\n    def test_a(self):\n        self.assertTrue(True)\n"
                                             "    def test_b(self):\n        self.assertEqual(2, 1 + 1)\n", encoding="utf-8")
            gitops.commit_all(pdir, "add tests")
            s = app.sessions.create(p, "tests", "run them")
            self.assertTrue(wait_until(lambda: app.store.session(s["id"])["status"] in ("review", "failed"), 120))
            s = app.store.session(s["id"])
            self.assertEqual(s["status"], "review", s.get("error"))
            self.assertEqual((s["tests"]["passed"], s["tests"]["failed"]), (2, 0))
            self.assertEqual(llm.calls, [])                         # no model was called
            kinds = [e["kind"] for e in app.store.session_events(s["id"])]
            self.assertEqual(kinds.count("tests"), 1)               # reported once, by the harness
        finally:
            shutil.rmtree(base, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
