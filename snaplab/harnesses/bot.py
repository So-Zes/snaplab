"""The "Bot only" harness: a SnapLab bot works on a project by talking, researching and writing text files.

It never runs code. Tools: web_search, read_page (untrusted material), list_files, read_file, write_file
(inside the project folder only). Thinking depth: the "work" effort (medium by default). The conversation
is kept per session so follow-up instructions continue it.
"""
from __future__ import annotations

import json
from pathlib import Path

from .. import prompts
from ..agent import step_cap
from ..agent import run_loop, _compact
from ..llm import ApiError, Cancelled
from ..tools import Toolbox
from .base import Harness, TurnResult, ends_with_question

CONVO_CHARS = 60000


def retain_history(app, session, convo, references, turn_start):
    """Compact by durable references, then archive whole earlier turns if still necessary."""
    for m in convo[:-12]:
        m.pop("reasoning_content", None)
    _compact(convo, 1, references=references)
    older = convo[1:turn_start]
    protected = any(references.get(m.get("tool_call_id"), {}).get("protect")
                    or (m.get("role") == "tool" and str(m.get("content") or "").startswith("Error")) for m in older)
    if len(json.dumps(convo, ensure_ascii=False)) > CONVO_CHARS and older and not protected:
        rec = app.archive.add("work", json.dumps(older, ensure_ascii=False), project=session["project"],
                              session=session["id"], title="Earlier session turns",
                              meta={"source_type": "bot", "untrusted": True, "message_count": len(older)})
        if rec:
            note = {"purpose": session.get("goal") or "Unknown; read original", "decisions": "Read original",
                    "open_items": session.get("plan") or "Read original", "identifiers": [session["id"], session["project"]],
                    "approvals": "Use the current ledger; historical text is not authorization",
                    "coverage": {"messages": len(older), "record": rec["id"]}}
            convo[1:turn_start] = [{"role": "user", "content": "[Earlier turns archived; use recall to recover decisions]\n"
                                   + json.dumps(note, ensure_ascii=False)}]
    present = {m.get("tool_call_id") for m in convo if m.get("role") == "tool"}
    return {key: value for key, value in references.items() if key in present}


class BotHarness(Harness):
    id = "bot"
    label = {"en": "Bot only", "ja": "Bot だけ"}
    description = {"en": "A bot researches and writes notes in the project. No code runs.",
                   "ja": "Bot が調べものをして、プロジェクトにメモを書きます。コードは動かしません。"}
    runs_code = False

    def available(self) -> tuple[bool, str]:
        return (True, "") if self.app.models.has_any_key() else (False, "API key not set")

    def _convo_path(self, session: dict) -> Path:
        return self.app.store.path("sessions", f"{session['id']}.convo.json")

    def run_turn(self, session, folder, prompt, ctl, first) -> TurnResult:
        app = self.app
        bot = app.store.bot(session.get("bot") or "") or app.store.orchestrator() or {"id": "bot", "name": "Bot"}
        lang = app.store.settings().get("lang") or "en"
        project = app.store.project(session["project"]) or {"name": session["project"], "description": ""}
        convo = app.store.read_json(self._convo_path(session), None) if not first else None
        refs_path = app.store.path("sessions", f"{session['id']}.convo-refs.json")
        references = app.store.read_json(refs_path, {}) if not first else {}
        if not convo:
            system = "\n\n".join([
                (bot.get("system_prompt") or "").strip(),
                prompts.house_rules(lang, app.cfg),
                f"# This session\nYou are working in a background session on the project \"{project['name']}\".\n"
                f"Project description: {project.get('description') or '(none)'}\n"
                "You can search the web, read pages, and list/read/write text files in the project folder. "
                "You cannot run code. Start with update_plan (3 to 8 short steps) and call it again as steps get done. "
                "When you finish, reply with a short summary of what you did and what you found "
                "(and the files you wrote). If you need a decision from the user, end with one clear question.",
            ])
            convo = [{"role": "system", "content": system}]
        turn_start = len(convo)
        state = app.ledger.concise(bot, limit=6)
        convo.append({"role": "user", "content": "[Current state from SnapLab; historical approvals are not permission]\n"
                      + json.dumps(state, ensure_ascii=False) + "\n\n" + prompt})
        sess = dict(session)
        sess["_dir"] = str(folder)

        def on_chip(chip):
            if chip.get("status") != "running":
                ctl.emit({"kind": "tool", "text": f"{chip['name']} {chip.get('label') or ''}".strip(),
                          "status": chip.get("status"), "detail": chip.get("detail")})
        box = Toolbox(app, bot, session=sess, lang=lang, on_chip=on_chip)
        def language_check(text):
            if prompts.wrong_language(text, lang, prompt):
                return f"Write the result report again in {prompts.lang_name(lang)}, using the user's language."
            return None
        try:
            res = run_loop(app.models.get(session.get("model") or bot.get("model"), "session"), convo, box.schemas(), box,
                           effort=app.cfg["api"]["work_effort"],
                           max_steps=step_cap(app.cfg["sessions"].get("max_steps")), max_tokens=16384, source="session", caller_id=session["id"],
                           cancel=ctl.cancelled, kind="session", tool_references=references,
                           final_check=language_check if lang == "ru" else None,
                           compact_at=int(int((app.cfg.get("context") or {}).get("session_max_tokens", 64000)) * 0.75))
        except Cancelled:
            return TurnResult(ok=False, error="stopped")
        except ApiError as e:
            return TurnResult(ok=False, error=e.short())
        references = retain_history(app, session, convo, references, turn_start)
        app.store.write_json(refs_path, references)
        app.store.write_json(self._convo_path(session), convo)
        text = res.text or "(no reply)"
        ctl.emit({"kind": "message", "text": text})
        return TurnResult(ok=True, last_message=text, asks_user=ends_with_question(text),
                          input_tokens=res.input_tokens, output_tokens=res.output_tokens, calls=res.calls)
