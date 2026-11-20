"""Harness plugins used by the tests (one declares sandboxing, one does not)."""
from snaplab.harnesses.base import Harness, TurnResult


class Unsafe(Harness):
    id = "unsafe"
    runs_code = True

    def run_turn(self, session, folder, prompt, ctl, first):
        return TurnResult(ok=True)


class Leaky(Harness):
    """Behaves like a command that dumped a secret into the project: writes the prompt text into files."""
    id = "leaky"
    runs_code = True
    sandboxed = True

    def run_turn(self, session, folder, prompt, ctl, first):
        (folder / "leak.txt").write_text("value=" + prompt, encoding="utf-8")
        (folder / "leak16.txt").write_text("value=" + prompt, encoding="utf-16-le")
        ctl.emit({"kind": "command", "text": "reg query", "output": "value " + prompt})
        return TurnResult(ok=True, last_message="I printed " + prompt)


class Safe(Harness):
    id = "safe"
    runs_code = True
    sandboxed = True

    def run_turn(self, session, folder, prompt, ctl, first):
        return TurnResult(ok=True, last_message="ok")
