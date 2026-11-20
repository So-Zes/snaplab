"""The harness contract, checked in code (used when loading harnesses and by tests/test_harness_contract.py).

A harness is a class with:
    id: str                         lowercase letters, digits, "-" or "_"; unique
    label, description: dict        at least {"en": ...}; other languages optional (falls back to English)
    icon: str                       optional; an icon name from web/app.js ICONS ("bot", "terminal", "flask", ...)
    runs_code: bool                 True if it executes code or commands
    sandboxed: bool                 must be True when runs_code is True (otherwise it is not loaded)
    __init__(self, app)
    available(self) -> (bool, reason)
    run_turn(self, session, folder, prompt, ctl, first) -> TurnResult

run_turn runs in a background thread. It receives:
    session   the session record (read it; store your own fields with ctl.update(...), e.g. a thread id to resume)
    folder    the session's own folder (a git worktree when the project has git) - the only place to work in
    prompt    the instruction for this turn (first turn: a brief built by SnapLab for coding harnesses)
    ctl       Control: emit(event), update(**fields), cancelled(), set_process(popen)
    first     True on the first turn of the session

It returns TurnResult(ok, last_message, error, asks_user, input_tokens, output_tokens, calls, extra).
extra may carry {"tests": {...}} (the parse_tests() shape) when the harness ran tests itself.

Status the phone shows is derived by SnapLab from the result: ok -> "review" (or "waiting" when asks_user),
not ok -> "failed", Stop pressed -> "stopped". Around every turn SnapLab itself snapshots the folder, removes
secret values (exit guard), commits to the session branch, measures the diff, runs the project's tests for
coding harnesses, writes result.json, posts the result card and sends notifications.

Events for the timeline (ctl.emit): {"kind": "message" | "command" | "file" | "tool" | "tests" | "note" | "error" |
"thinking", "text": "...", ...}. "command" may add "exit_code" and "output"; "tests" may add "passed"/"failed".
"""
from __future__ import annotations

import inspect
import re

ID_RE = re.compile(r"^[a-z][a-z0-9_-]{1,30}$")
EVENT_KINDS = ("prompt", "message", "command", "file", "files", "tool", "tests", "note", "error", "thinking")


def problems(h) -> list[str]:
    """Contract problems of a harness instance (empty list = conforms)."""
    out = []
    hid = getattr(h, "id", None)
    if not isinstance(hid, str) or not ID_RE.match(hid):
        out.append("id must be 2-31 lowercase letters, digits, '-' or '_'")
    lbl = getattr(h, "label", None)
    if not isinstance(lbl, dict) or not isinstance(lbl.get("en"), str) or not lbl["en"].strip():
        out.append("label must be a dict with at least an English ('en') text")
    desc = getattr(h, "description", None)
    if not isinstance(desc, dict) or not isinstance(desc.get("en"), str):
        out.append("description must be a dict with an English ('en') text (may be empty)")
    if not isinstance(getattr(h, "icon", "bot"), str):
        out.append("icon must be the name of an icon (a string)")
    if not isinstance(getattr(h, "runs_code", None), bool):
        out.append("runs_code must be True or False")
    if getattr(h, "runs_code", False) and getattr(h, "sandboxed", False) is not True:
        out.append("a harness that runs code must declare sandboxed = True (and really run inside a sandbox)")
    run = getattr(h, "run_turn", None)
    if not callable(run):
        out.append("run_turn is missing")
    else:
        params = list(inspect.signature(run).parameters)
        if params[:5] != ["session", "folder", "prompt", "ctl", "first"]:
            out.append("run_turn must take (session, folder, prompt, ctl, first)")
    try:
        ok, why = h.available()
        if not isinstance(ok, bool) or not isinstance(why, str):
            out.append("available() must return (bool, str)")
    except Exception as e:  # noqa: BLE001
        out.append(f"available() raised {e!r}")
    return out
