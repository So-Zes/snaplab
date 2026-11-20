"""Reading test output (unittest and pytest) into a small summary."""
from __future__ import annotations

import re


def validate_test_command(command: str) -> str:
    """Reject known persistent servers/watchers; arbitrary programs still have a timeout."""
    command = (command or "").strip()
    if len(command) > 300:
        raise ValueError("the test command is limited to 300 characters")
    persistent = (r"\b(?:http\.server|SimpleHTTPServer|http-server|uvicorn|gunicorn)\b|"
                  r"\b(?:npm|pnpm|yarn|bun)\s+(?:run\s+)?(?:dev|start|serve|watch)\b|"
                  r"\b(?:vite|next|nuxt)\b(?!\s+(?:build|test)\b)|"
                  r"\bflask\s+run\b|\bmanage\.py\s+runserver\b|"
                  r"(?:^|\s)--watch(?:[=\s]|$)|\bwhile\s+(?:true|1)\b")
    if re.search(persistent, command, re.I):
        raise ValueError("Tests must finish and exit. A server or watch command cannot be a test command; "
                         "leave it empty, or use a finite check such as python -m unittest. "
                         "Start a preview server separately inside the session only when needed.")
    return command


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
