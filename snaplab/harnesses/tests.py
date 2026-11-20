"""The "Run tests" harness: runs the project's test command inside the session sandbox. No model is called.

It is the smallest complete example of the harness contract (see contract.py and docs/extending.md):
it checks availability, reports progress with ctl.emit, honours Stop, and returns a TurnResult that carries
the test summary in extra["tests"] so the session page and the result card show it.
"""
from __future__ import annotations

import os
from pathlib import Path

from ..testparse import parse_tests
from ..evidence import artifact_version, record_tests
from .base import Harness, TurnResult
from .codex import find_codex, sandbox_run


class TestsHarness(Harness):
    __test__ = False                  # not a unittest class, despite the name
    id = "tests"
    label = {"en": "Run tests · sandboxed", "ja": "試験だけ（囲いの中）"}
    description = {"en": "Runs the project's test command in the same sandbox as Codex (session folder only, no "
                         "network). No model is used. The instruction is kept as a note.",
                   "ja": "プロジェクトの試験のコマンドを、Codex と同じ囲いの中（作業場所だけ・ネットなし）で動かします。"
                         "モデルは使いません。指示はメモとして残ります。"}
    runs_code = True
    sandboxed = True
    icon = "flask"

    def available(self) -> tuple[bool, str]:
        if os.name != "nt":
            return False, "needs Windows (mxc sandbox)"
        if not find_codex():
            return False, "Codex CLI not found (its sandbox runs the tests)"
        return True, ""

    def run_turn(self, session, folder, prompt, ctl, first) -> TurnResult:
        project = self.app.store.project(session["project"]) or {}
        cmd = (project.get("test_command") or "").strip()
        if not cmd:
            return TurnResult(ok=False, error="this project has no test command (set one on the project page)")
        if ctl.cancelled():
            return TurnResult(ok=False, error="stopped")
        ctl.emit({"kind": "command", "text": cmd})
        before = artifact_version(Path(folder))
        code, out = sandbox_run(self.app, session["id"], Path(folder), cmd,
                                float(self.app.cfg["sessions"]["test_timeout_s"]),
                                git_dir=Path(session["git_dir"]) if session.get("git_dir") else None)
        tests = parse_tests(code, out)
        tests.update(tested_version=before, version_after=artifact_version(Path(folder)),
                     exit_code=code, source_type="runtime")
        tests["command"] = cmd
        record_tests(self.app, session, tests, out)
        ctl.emit({"kind": "tests", "text": f"Tests: {tests['status']}", "passed": tests["passed"], "failed": tests["failed"],
                  "exit_code": code})
        ok = tests["status"] == "passed"
        summary = (f"Tests {tests['status']}: {tests['passed']} passed, {tests['failed']} failed."
                   if tests["passed"] is not None else f"Tests {tests['status']} (exit code {code}).")
        if self.app.lang == 'ru':
            summary = (self.app.t('ev.tests.result', passed=tests['passed'], failed=tests['failed'])
                       if tests['passed'] is not None else self.app.t('session.tests.' + tests['status']))
        return TurnResult(ok=ok, last_message=summary, error=None if ok else summary, extra={"tests": tests})
