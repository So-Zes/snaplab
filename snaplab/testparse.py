"""Reading test output (unittest and pytest) into a small summary."""
from __future__ import annotations

import re


def validate_test_command(command: str) -> str:
    """The project's test command, trimmed. Any command is accepted (until 2026-11-21 commands over 300 characters
    and server or watch commands were refused); a run that does not end waits until the session is stopped."""
    return (command or "").strip()


def parse_tests(code: int | None, out: str, *, timed_out: bool = False) -> dict:
    """Read unittest / pytest output. status: passed | failed | no tests | error."""
    passed = failed = None
    m = re.search(r"Ran (\d+) tests?", out)
    if m:
        total = int(m.group(1))
        fm = re.search(r"FAILED \(([^)]*)\)", out)
        failed = sum(int(x) for x in re.findall(r"(?:failures|errors)=(\d+)", fm.group(1))) if fm else 0
        passed = total - failed
    else:
        pm = re.search(r"(\d+) passed", out)
        fm = re.search(r"(\d+) failed", out)
        if pm or fm:
            passed = int(pm.group(1)) if pm else 0
            failed = int(fm.group(1)) if fm else 0
    if timed_out or code == 124:
        status = "timeout"
    elif code is None or code in (126, 127):
        status = "error"
    elif passed == 0 and not failed:
        status = "no tests"
    elif code == 0:
        status = "passed"
    else:
        status = "failed"
    return {"status": status, "exit_code": code, "passed": passed, "failed": failed, "output": out[-1500:]}
