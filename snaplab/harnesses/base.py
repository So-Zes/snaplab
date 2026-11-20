"""The harness interface. A harness runs one turn of a session in a project folder.

The full contract (what a harness receives, returns and reports) is in contract.py and docs/extending.md.
To add a harness (for example another coding agent), write a class like this, then either add it to
BUILTIN in harnesses/__init__.py or list "your_module:YourHarness" in config.json under harness_plugins.

    class MyHarness(Harness):
        id = "mine"
        label = {"en": "My agent", "ja": "わたしのエージェント"}
        description = {"en": "...", "ja": "..."}
        def available(self): return True, ""
        def run_turn(self, session, folder, prompt, ctl, first): ...

run_turn() is called in a background thread and must return a TurnResult. Use ctl to report progress:
    ctl.emit({"kind": "command" | "file" | "message" | "note" | "error", "text": ..., ...})
    ctl.update(thread_id=...)            # store fields on the session (e.g. an id needed to resume)
    ctl.cancelled()                      # True once the user pressed Stop
    ctl.set_process(popen)               # lets Stop kill a child process tree
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class TurnResult:
    ok: bool
    last_message: str = ""
    error: str | None = None
    asks_user: bool = False          # the agent ended its turn with a question for the user
    input_tokens: int = 0
    output_tokens: int = 0
    calls: int = 0
    extra: dict = field(default_factory=dict)


class Harness:
    id = "base"
    label = {"en": "Harness", "ja": "ハーネス"}
    description = {"en": "", "ja": ""}
    runs_code = False                # True if this harness executes code (must then be sandboxed)
    sandboxed = False                # True if every command it runs is inside the session sandbox
    icon = "person"                  # an icon file name from web/icons/ui (without .svg)

    def __init__(self, app):
        self.app = app
        from .. import i18n
        self.label, self.description = dict(self.label), dict(self.description)
        for lang in i18n.codes():
            for field in ('label', 'description'):
                key = 'harness.' + self.id + '.' + field
                if lang not in getattr(self, field) and i18n.has(lang, key):
                    getattr(self, field)[lang] = i18n.text(lang, key)

    def available(self) -> tuple[bool, str]:
        return True, ""

    def run_turn(self, session: dict, folder: Path, prompt: str, ctl, first: bool) -> TurnResult:
        raise NotImplementedError

    def info(self) -> dict:
        ok, why = self.available()
        return {"id": self.id, "label": self.label, "description": self.description, "available": ok, "reason": why,
                "runs_code": self.runs_code, "icon": self.icon}


def ends_with_question(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    last = t.splitlines()[-1].strip()
    return last.endswith(("?", "？")) or last.lower().startswith(("question:", "質問:", "質問："))
