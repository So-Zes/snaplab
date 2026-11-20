"""The steps of a SnapCode and how far it has got (round 6).

Each SnapCode keeps a checklist in .snaplab/PLAN.md inside its work folder:

    - [x] Read BRIEF.md and the code
    - [ ] Add the subtract command
    - [ ] Run the tests

Codex is asked to write and tick it (see sessions.brief_prompt); the "Bot only" harness has an update_plan tool.
SnapLab reads the file while the SnapCode runs (every few seconds) and after each turn, and shows "4 of 6 steps"
on the phone, on the project and on the dashboard. The file is not part of the project's git history (.snaplab/ is
excluded), so plans never end up in main.
"""
from __future__ import annotations

import re
from pathlib import Path

CHECK_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s*\[( |x|X|-|~)\]\s+(.+?)\s*$")
FILE = Path(".snaplab") / "PLAN.md"
MAX_STEPS = 30


def parse(text: str) -> dict | None:
    steps = []
    for line in (text or "").splitlines():
        m = CHECK_RE.match(line)
        if m:
            label = " ".join(m.group(2).split())[:160]
            if label:
                steps.append({"text": label, "done": m.group(1) in "xX"})
        if len(steps) >= MAX_STEPS:
            break
    if not steps:
        return None
    done = sum(1 for s in steps if s["done"])
    now = next((s["text"] for s in steps if not s["done"]), "")
    return {"steps": steps, "done": done, "total": len(steps), "now": now}


def read(folder: Path) -> tuple[dict | None, int]:
    """(plan, mtime_ns) of the PLAN.md in a work folder; (None, 0) when there is none."""
    f = Path(folder) / FILE
    try:
        st = f.stat()
        return parse(f.read_text(encoding="utf-8-sig", errors="replace")), st.st_mtime_ns
    except OSError:
        return None, 0


def write(folder: Path, steps: list) -> dict | None:
    lines = ["# Plan", ""]
    for s in steps[:MAX_STEPS]:
        if isinstance(s, str):
            text, done = s, False
        elif isinstance(s, dict):
            text, done = str(s.get("text") or s.get("step") or ""), bool(s.get("done"))
        else:
            continue
        text = " ".join(text.split())[:160]
        if text:
            lines.append(f"- [{'x' if done else ' '}] {text}")
    f = Path(folder) / FILE
    f.parent.mkdir(parents=True, exist_ok=True)
    body = "\n".join(lines) + "\n"
    f.write_text(body, encoding="utf-8")
    return parse(body)


def total(plans: list[dict | None]) -> dict | None:
    """Steps of several SnapCodes added together (a project's progress)."""
    ps = [p for p in plans if p and p.get("total")]
    if not ps:
        return None
    return {"done": sum(p["done"] for p in ps), "total": sum(p["total"] for p in ps), "works": len(ps)}
