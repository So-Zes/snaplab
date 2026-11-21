"""Sessions: background work in a project folder, driven by a harness.

A session has turns. Each turn: snapshot the folder, run the harness, diff the folder (changed files),
run the project's test command inside the sandbox (if any), then set the status:
    running (Working) -> review (Ready for review) | waiting (Needs you: the agent asked something)
                       | failed | stopped
    review -> idle (you marked it reviewed). Any finished state can take a follow-up instruction.
The order of urgency on the phone: waiting, failed, review, running, stopped, idle.
Phones see every change live; "review", "needs you" and "failed" send one push notification each.
"""
from __future__ import annotations

import os
import re
import threading
import tempfile
import time
from pathlib import Path

from . import gitops
from . import plan as planmod
from .config import limit
from .harnesses import load as load_harnesses
from .harnesses.codex import sandbox_run
from .testparse import parse_tests  # noqa: F401  (also used by the tests)
from .evidence import artifact_version, record_tests
from .dialogue import session_origin
from .store import new_id, now_iso

SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".snaplab", ".mypy_cache", ".pytest_cache"}


def snapshot(folder: Path) -> dict[str, tuple[int, int]]:
    """Size and time of every file. There was a 5,000-file limit: a bigger project then showed only part of its
    files, and a turn that rewrote them reported "4,999 files changed (+0 -0)" (2026-11-17)."""
    out: dict[str, tuple[int, int]] = {}
    base = Path(folder)
    if not base.exists():
        return out
    for root, dirs, files in os.walk(base):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for f in files:
            p = Path(root) / f
            try:
                st = p.stat()
            except OSError:
                continue
            out[p.relative_to(base).as_posix()] = (st.st_size, st.st_mtime_ns)
    return out


def diff(before: dict, after: dict) -> list[dict]:
    changes = []
    for k in sorted(set(before) | set(after)):
        if k not in before:
            changes.append({"path": k, "change": "added"})
        elif k not in after:
            changes.append({"path": k, "change": "deleted"})
        elif before[k] != after[k]:
            changes.append({"path": k, "change": "modified"})
    return changes


def brief_prompt(task: str, project: dict) -> str:
    """The first instruction for a coding agent, in a fixed shape (task, where to look, done-when, what to return)."""
    done = (f"`{project['test_command']}` passes" if project.get("test_command") else
            "the task is done and you, the coding agent, checked it yourself (the user then reviews the result)")
    return "\n".join([
        "## Task", task.strip(), "",
        "## Context",
        "- BRIEF.md: the project's goal, done-when and rules. .snaplab/STATE.md: what happened so far.",
        "- The files are UTF-8; in Windows PowerShell read them with Get-Content -Encoding UTF8.",
        "- You work in this session's own git worktree. Nothing outside it is readable and there is no network.",
        "", "## Plan",
        "- First write .snaplab/PLAN.md with apply_patch: a checklist of short steps, one per line, like `- [ ] Add the tests`.",
        "- Tick each step (`- [x]`) as soon as it is done, and change the list if the plan changes. SnapLab shows it to the user.",
        "", "## Done when", f"- {done}", "",
        "## Finish with",
        "- A short summary: what changed, how you checked it, anything left open.",
        "- If you need a decision from the user, end with one clear question instead.",
    ])


def words(text: str, n: int) -> str:
    w = (text or "").split()
    return " ".join(w[:n]) + (" …" if len(w) > n else "")


def result_card_text(r: dict, lang: str = "en") -> str:
    """The short result a bot receives (well under 300 tokens). Details stay in files."""
    st = {"review": "ready for review", "waiting": "needs the user", "failed": "failed", "stopped": "stopped"}.get(r["status"], r["status"])
    d = r.get("diff") or {}
    t = r.get("tests") or {}
    parts = [f"[Result card] session {r['session']} {st} after turn {r['turn']}, {int(r['duration_s'])} s."]
    if d:
        parts.append(f"Changes: {d.get('files', 0)} files, +{d.get('insertions', 0)} -{d.get('deletions', 0)}"
                     + (f" ({', '.join(c['path'] for c in r.get('changed_files', [])[:5])})" if r.get("changed_files") else "") + ".")
    if t:
        parts.append(f"Tests: {t.get('status')}" + (f" ({t.get('passed')} passed, {t.get('failed')} failed)" if t.get("passed") is not None else "") + ".")
    if r.get("error"):
        parts.append(f"Error: {r['error'][:160]}.")
    if r.get("summary"):
        parts.append(f"Agent summary: {words(r['summary'], 70)}")
    if r.get("attachments"):
        from .i18n import text
        parts.append(text(lang, "srv.attachment.delivered", names=", ".join(a["name"] for a in r["attachments"])))
    if r.get("attachment_errors"):
        from .i18n import text
        parts.append(text(lang, "srv.attachment.failed"))
        for error in r["attachment_errors"]:
            if isinstance(error, dict):
                parts.append(f"{error.get('path') or 'Attachment'}: {error.get('reason') or error.get('code')}.")
    return " ".join(parts)


def remaining_calls(app, res) -> int:
    """Model calls left for the fallback harness in the same turn; 0 = no limit."""
    cap = limit(app.cfg["sessions"].get("max_api_calls"))
    return 0 if cap == float("inf") else max(1, int(cap) - int(res.extra.get("proxy_calls", 0)))


def short_title(title: str, limit: int = 48) -> str:
    """A title cut at a word boundary, with an ellipsis (QA U-10: titles were cut mid-word)."""
    title = " ".join((title or "").split())
    if len(title) <= limit:
        return title
    cut = title[:limit].rsplit(" ", 1)[0]
    return (cut if len(cut) > limit // 2 else title[:limit]).rstrip(" ,.;:") + "…"


def emit_files(ctl, app, changes: list, n: int, plus, minus) -> None:
    vars = {"n": str(n), "plus": str(plus or 0), "minus": str(minus or 0)}
    key = "ev.files.1" if n == 1 else "ev.files.n"
    ctl.emit({"kind": "files", "text": app.t(key, **vars), "key": key, "vars": vars,
              "changes": [{"change": c["change"], "path": c["path"]} for c in changes[:30]],
              "files": [f"{c['change']}: {c['path']}" for c in changes[:30]]})


SHELL_RE = re.compile(r"""^\s*["']?[A-Za-z]:[\\/]+(?:[^"'\s]*[\\/]+)?(?:powershell|pwsh|cmd)(?:\.exe)?["']?\s+"""
                      r"""(?:-(?:NoProfile|NoLogo|NonInteractive|ExecutionPolicy\s+\S+)\s+)*(?:-Command|-c|/c)\s+(.*)$""",
                      re.I | re.S)


def unwrap_command(cmd: str) -> str:
    """'"C:\\...\\powershell.exe" -Command "Get-Content x"' -> 'Get-Content x' (QA V-4: show the command itself)."""
    m = SHELL_RE.match(cmd or "")
    if not m:
        return cmd
    inner = m.group(1).strip()
    if len(inner) >= 2 and inner[0] == inner[-1] and inner[0] in "\"'":
        inner = inner[1:-1]
    return inner


class Control:
    """Handed to a harness for one turn."""

    def __init__(self, mgr: "SessionManager", sid: str):
        self.mgr = mgr
        self.sid = sid
        self.cancel = threading.Event()
        self.proc = None

    def emit(self, ev: dict) -> None:
        ev = self.mgr.display(self.sid, {k: v for k, v in ev.items() if v is not None})
        ev = self.mgr.app.store.session_event(self.sid, ev)
        self.mgr.app.bus.publish("session.event", {"session": self.sid, "event": ev})
        arc = getattr(self.mgr.app, "archive", None)
        if arc is not None:
            try:
                arc.add_work_event(self.mgr.app.store.session(self.sid) or {"id": self.sid}, ev)
            except Exception:  # noqa: BLE001 - the timeline must go on
                pass

    def update(self, **fields) -> None:
        with self.mgr.lock:
            s = self.mgr.app.store.session(self.sid)
            if s:
                s.update(fields)
                self.mgr.app.store.save_session(s)

    def cancelled(self) -> bool:
        return self.cancel.is_set()

    def set_process(self, proc) -> None:
        self.proc = proc


class SessionManager:
    def __init__(self, app):
        self.app = app
        self.lock = threading.RLock()
        self.harnesses = load_harnesses(app)
        self.controls: dict[str, Control] = {}
        self._plan_seen: dict[str, int] = {}
        self._plan_stop = threading.Event()
        self.screens: dict[str, tuple[bytes, int]] = {}

    def set_screen(self, sid: str, raw: bytes) -> None:
        # Private live previews are bounded, held only in memory, and never included in prompts.
        if len(raw) > 512000 or not raw.startswith(b'\xff\xd8\xff') or not raw.endswith(b'\xff\xd9'):
            raise ValueError('invalid desktop frame')
        with self.lock:
            previous = self.screens.get(sid)
            if previous and previous[0] == raw:
                return
            seq = (previous[1] if previous else 0) + 1
            self.screens[sid] = (raw, seq)
        self.app.bus.publish('session.screen', {'session':sid, 'seq':seq})

    def clear_screen(self, sid: str) -> None:
        with self.lock:
            self.screens.pop(sid, None)
        self.app.bus.publish('session.screen', {'session':sid, 'seq':0})

    # ------------------------------------------------------------ the steps of running SnapCodes (round 6)

    def refresh_plan(self, sid: str, folder: Path | None = None) -> dict | None:
        """Read .snaplab/PLAN.md of a SnapCode; store and announce it when it changed."""
        s = self.app.store.session(sid)
        if not s:
            return None
        if folder is None:
            project = self.app.store.project(s["project"])
            if not project:
                return None
            folder = self.workdir(s, project)
        pl, mtime = planmod.read(folder)
        if not pl or mtime == self._plan_seen.get(sid):
            return s.get("plan")
        self._plan_seen[sid] = mtime
        return self.set_plan(sid, pl)

    def set_plan(self, sid: str, pl: dict | None) -> dict | None:
        """Store a SnapCode's plan (from its PLAN.md, read here or streamed by a remote worker) and announce changes."""
        if not pl:
            return None
        with self.lock:
            s = self.app.store.session(sid)
            if not s:
                return None
            if s.get("plan") == pl:
                return pl
            s["plan"] = pl
            s["plan_updated"] = now_iso()
            self.app.store.save_session(s)
        self.app.bus.publish("session.updated", {"session": self.summary(s)})
        self.app.bus.publish("projects.updated", {"projects": self.app.projects_view()})
        return pl

    def watch_plans(self, every_s: float = 4.0) -> None:
        """While SnapCodes run, read their plans every few seconds (started by the server)."""
        def loop():
            while not self._plan_stop.wait(every_s):
                for sid in list(self.controls):
                    try:
                        self.refresh_plan(sid)
                    except Exception as e:  # noqa: BLE001
                        self.app.log(f"plan watch {sid}: {e!r}")
        threading.Thread(target=loop, daemon=True, name="plan-watch").start()

    def harness_ids(self) -> list[str]:
        return list(self.harnesses)

    def harness_info(self) -> list[dict]:
        return [h.info() for h in self.harnesses.values()]

    def folder(self, project: dict) -> Path:
        return Path(self.app.cfg["projects_dir"]) / project["slug"]

    def workdir(self, s: dict, project: dict) -> Path:
        """Where the session works: its own git worktree, or the project folder for projects without git."""
        return Path(s["worktree"]) if s.get("worktree") else self.folder(project)

    RESUME_PROMPT = ("SnapLab was restarted for an update while you were working, which cut off your last turn. "
                     "Continue the task from where you left off. Look at the workspace first: a file may be half-written.")

    def recover(self, resume: bool = False) -> None:
        """After a restart, sessions that were running cannot continue: mark them stopped. resume (a restart by
        an update): then tell each one to go on, as a follow-up would."""
        cut = []
        for s in self.app.store.sessions():
            if s.get("status") == "running":
                took = 0.0
                try:
                    import datetime as _dt
                    took = (_dt.datetime.now().astimezone() - _dt.datetime.fromisoformat(s.get("started"))).total_seconds()
                except (TypeError, ValueError):
                    pass
                s.update(status="stopped", error="SnapLab restarted while this session was running; send a follow-up to go on",
                         ended=now_iso(), elapsed_s=round(float(s.get("elapsed_s") or 0) + max(0.0, min(took, 86400)), 1),
                         interrupted=True)
                self.app.store.save_session(s)
                cut.append(s["id"])
        for sid in cut if resume else []:
            if not self.app.store.project((self.app.store.session(sid) or {}).get("project") or ""):
                self.app.log(f"not resuming session {sid} after the restart: its project is gone")
                continue
            try:
                self.send(sid, self.RESUME_PROMPT, notice=False)
            except Exception as e:  # noqa: BLE001 - e.g. its project or worktree is gone
                self.app.log(f"could not resume session {sid} after the restart: {e}")

    # ------------------------------------------------------------ public actions

    def create(self, project: dict, harness: str | None = None, prompt: str = "", *, title: str = "", started_by: str = "user",
               room: str | None = None, bot: str | None = None, model: str | None = None, required_files=None, task_type=None,
               from_session: str | None = None, files=None) -> dict:
        """from_session: start from that session's files (its branch) with a fresh conversation, for example a
        review, a grading pass or an alternative attempt that must not inherit the other agent's context."""
        source = None
        if from_session:
            source = self.app.store.session(from_session)
            if not source:
                raise LookupError(f"no session {from_session!r}")
            if source.get("project") != project["id"]:
                raise ValueError("from_session must be a session of the same project")
        from .harnesses.selection import select, task_kind
        automatic = not harness
        harness = select(self.app, prompt, harness, task_type)
        required_files = required_files or []
        from .harnesses.remote_worker import file_name
        if not isinstance(required_files, list) or len(required_files) > 32:
            raise ValueError('invalid required files')
        required_files = [file_name(x) for x in required_files]
        if harness not in self.harnesses:
            raise ValueError(f"unknown harness {harness!r}")
        ok, why = self.harnesses[harness].available()
        if not ok:
            raise ValueError(f"harness {harness} is not available: {why}")
        prompt = (prompt or "").strip()
        if not prompt:
            raise ValueError("the session needs a first instruction")
        if files:          # checked before anything is created (uploads.prepare)
            from .uploads import prepare
            files = prepare(self.app, files)
        if str(started_by).startswith("b-"):
            self.app.chat.dialogue.reserve_session(started_by)
        if harness == "bot" and not bot:
            orch = self.app.store.orchestrator()
            bot = orch["id"] if orch else None
        selected_bot = self.app.store.bot(bot or project.get("bot")) or {}
        selected_model = self.app.models.name_for(model or selected_bot.get("model"), "session" if harness == "bot" else "codex")
        if harness != "bot" and self.app.models.override is None and self.app.models.is_cli(selected_model):
            # Code harnesses talk Chat Completions through the translator; a CLI bot's sessions use routing.
            selected_model = self.app.models.name_for(None, "codex")
        # Work started from the Work tab also delivers to the responsible bot's chat.
        if not room:
            owner = bot or project.get("bot") or (self.app.store.orchestrator() or {}).get("id")
            chat = self.app.store.dm_room(owner) if owner else None
            room = chat["id"] if chat else None
        s = {"id": new_id("s"), "project": project["id"], "harness": harness, "title": (title or prompt)[:80].strip(),
             "status": "queued", "created": now_iso(), "started_by": started_by, "room": room, "bot": bot,
             "elapsed_s": 0.0, "tokens": {"input": 0, "output": 0}, "calls": 0, "turns": 0, "changed_files": [],
             "tests": None, "goal": prompt[:2000], "last_prompt": prompt[:2000], "last_message": "", "error": None, "thread_id": None,
             "model": selected_model, "auto_harness": automatic, "required_files": required_files,
             "task_kind": task_type if task_type in ('code', 'web', 'desktop', 'research') else task_kind(prompt)}
        if source:
            s["from_session"] = source["id"]
        pfolder = self.folder(project)
        if harness in ("codex", "pi"):
            from .harnesses.remote import backend
            s["execution_backend"] = backend(self.app)
        if gitops.available() and gitops.is_repo(pfolder):
            try:
                wt, branch, base = gitops.add_worktree(pfolder, s["id"], (source or {}).get("branch"))
                gitops.exclude(pfolder, ".snaplab/")
                s.update(worktree=str(wt), branch=branch, base=base, git_dir=str(gitops.git_dir(pfolder)))
            except (RuntimeError, OSError) as e:
                raise ValueError(f"could not create the session's worktree: {e}") from None
        if files:          # files from the chat: copied into the workspace (from_chat/) before the first turn
            from .uploads import give_to_session
            prompt += give_to_session(self.app, self.workdir(s, project), files)
        self.app.store.save_session(s)
        who = self.app.who("you") if started_by == "user" else (self.app.store.bot(started_by) or {}).get("name", "Bot")
        h = self.harnesses[harness]
        self.app.act("session", "act.session_started", ref=s["id"], who=who, project=project["name"],
                     how=h.label.get(self.app.lang) or h.label.get("en") or harness)
        self.app.bus.publish("session.updated", {"session": self.summary(s)})
        self._start(s["id"], prompt, first=True)
        self._work_notice(s, prompt, started_by, first=True)
        return s

    def _work_notice(self, s: dict, prompt: str, by: str | None, first: bool) -> None:
        """A note in the session's room each time SnapCode really starts or gets an instruction. It is written by
        SnapLab, never by a bot, so a bot (and the user) can tell a real start from a reply that only says so."""
        if not s.get("room"):
            return
        try:
            who = (self.app.store.bot(by) or {}).get("name", "Bot") if str(by or "").startswith("b-") else self.app.who("you")
            key = "note.work_started" if first else "note.work_sent"
            vars = {"who": who, "title": short_title(s.get("title") or s["id"]), "session": s["id"],
                    "prompt": " ".join(prompt.split())[:120]}
            vars.update({k: " " + vars[k] for k in ("title", "prompt") if vars[k].startswith("@")})   # "@x" reads as a key
            m = self.app.store.add_message(s["room"], {"role": "system", "text": self.app.t(key, **vars), "key": key,
                                                       "vars": vars, "session": s["id"], "work_notice": True},
                                           touch_room=False)
            self.app.bus.publish("message.new", {"room": s["room"], "message": m})
        except Exception as e:  # noqa: BLE001 - the session has started; a missing note must not undo that
            self.app.log(f"work notice failed for {s.get('id')}: {e!r}")

    def send(self, sid: str, prompt: str, *, requested_by: str | None = None, auto: bool = False, files=None,
             notice: bool = True) -> dict:
        """auto: a bot continues the work on its own after a result card. Those turns are counted on the session;
        any other follow-up (the user, or a bot answering the user) resets the count. notice=False: no note in the
        room (SnapLab itself resumes the session after a deploy; the note would name the user as the sender)."""
        s = self.app.store.session(sid)
        if not s:
            raise LookupError("no such session")
        if s["status"] in ("running", "queued"):
            raise ValueError("the session is still running; wait or stop it first")
        prompt = (prompt or "").strip()
        if not prompt:
            raise ValueError("empty instruction")
        if files:          # files from the chat for this instruction (uploads.py)
            from .uploads import give_to_session
            project = self.app.store.project(s["project"])
            if not project:
                raise ValueError("project no longer exists")
            prompt += give_to_session(self.app, self.workdir(s, project), files)
        with self.app.store.lock:
            s = self.app.store.session(sid)
            s["auto_continues"] = int(s.get("auto_continues") or 0) + 1 if auto else 0
            self.app.store.save_session(s)
        if requested_by and requested_by.startswith("b-"):
            self.app.chat.dialogue.reserve_session(requested_by)
        self._start(sid, prompt, first=False)
        if notice:
            self._work_notice(s, prompt, requested_by, first=False)
        return s

    def auto_continue_limit(self) -> float:
        """How many times in a row a bot may continue a session from its result cards without the user (inf = no limit)."""
        return limit((self.app.cfg.get("sessions") or {}).get("auto_continue"))

    def mark_reviewed(self, sid: str) -> dict:
        with self.lock:
            s = self.app.store.session(sid)
            if not s:
                raise LookupError("no such session")
            if s["status"] not in ("review", "done"):
                raise ValueError("only a session that is ready for review can be marked reviewed")
            s["status"] = "idle"
            s["reviewed"] = now_iso()
            self.app.store.save_session(s)
        self.app.store.event("session.reviewed", actor="user", where=sid)
        self.app.bus.publish("session.updated", {"session": self.summary(s)})
        return s

    def stop(self, sid: str) -> bool:
        ctl = self.controls.get(sid)
        if not ctl:
            return False
        ctl.cancel.set()
        if ctl.proc is not None:
            from .harnesses.codex import kill_tree
            try:
                kill_tree(ctl.proc)
            except Exception:  # noqa: BLE001
                pass
        return True

    # ------------------------------------------------------------ running a turn

    def _start(self, sid: str, prompt: str, first: bool) -> None:
        with self.lock:
            if sid in self.controls:
                raise ValueError("the session is already running")
            ctl = Control(self, sid)
            self.controls[sid] = ctl
        threading.Thread(target=self._turn, args=(sid, prompt, first, ctl), daemon=True, name=f"session-{sid}").start()

    def _turn(self, sid: str, prompt: str, first: bool, ctl: Control) -> None:
        app, st = self.app, self.app.store
        try:
            s = st.session(sid)
            project = st.project(s["project"])
            folder = self.workdir(s, project)
            folder.mkdir(parents=True, exist_ok=True)
            self._write_state_file(project, folder)
            harness = self.harnesses[s["harness"]]
            from .harnesses.remote import backend, brief_prompt as remote_brief, DELIVERY as REMOTE_DELIVERY
            remote = s["harness"] in ("codex", "pi") and backend(app, s) in ("ssh", "wsl")
            with self.lock:
                s = st.session(sid)
                if s.get("last_message"):
                    s["previous_message"] = s["last_message"]
                s.pop("remaining_api_calls", None)
                s.update(status="running", started=now_iso(), last_prompt=prompt[:2000], error=None, last_message="")
                st.save_session(s)
            app.bus.publish("session.updated", {"session": self.summary(s)})
            ctl.emit({"kind": "prompt", "text": prompt[:4000]})
            st.event("session.turn_start", actor=s.get("started_by") or "user", where=sid,
                     data={"project": s["project"], "harness": s["harness"], "prompt": prompt[:300]})
            if first and not s.get("worktree"):
                try:                              # a shared project folder: the old plan belongs to another SnapCode
                    (folder / planmod.FILE).unlink()
                except OSError:
                    pass
            before = snapshot(folder)
            # Only a turn on this PC uses the code fingerprint from before the turn (to report unchanged tests);
            # for a remote turn the worker runs the tests, and reading every file of a fresh worktree here was the
            # slowest step of a new session (102 s for 27,600 files, 2026-11-18).
            code_before = None if remote else artifact_version(folder, code_only=True)
            from .attachments import INSTRUCTIONS as DELIVERY_INSTRUCTIONS
            from .harnesses.capture import prepare_capture
            preparation_started = time.monotonic()
            try:
                prepare_capture(folder)
                # A manifest belongs to one turn. Previous delivered copies remain in private storage.
                manifest = folder / ".snaplab" / "attachments.json"
                if manifest.is_file() and not manifest.is_symlink():
                    manifest.unlink()
            except Exception as e:  # Preparation must finish the turn even if no harness ran.
                from .harnesses.base import TurnResult
                detail = self.display(sid, app.guard.clean(f"{type(e).__name__}: {e}"))[:300]
                error = app.t("ev.capture_prepare_failed") + " " + detail
                took = time.monotonic() - preparation_started
                with self.lock:
                    s = st.session(sid)
                    s.update(status="failed", ended=now_iso(), error=error,
                             turns=int(s.get("turns") or 0) + 1,
                             elapsed_s=round(float(s.get("elapsed_s") or 0) + took, 1))
                    s["result"] = self._result(s, "failed", TurnResult(ok=False, error=error), took)
                    s["result"].update(tests=None, diff=None, changed_files=[], attachments=[], attachment_errors=[])
                    st.save_session(s)
                    self.controls.pop(sid, None)
                # Do not write through a rejected .snaplab path or collect an old manifest.
                st.write_json(st.path("sessions", f"{sid}.result.json"), s["result"])
                ctl.emit({"kind": "error", "text": error})
                app.bus.publish("session.updated", {"session": self.summary(s)})
                self._announce(s, project, "failed")
                return
            head_before = gitops.head(folder) if s.get("worktree") else None
            t0 = time.monotonic()
            task = (remote_brief(prompt, s) if remote else
                    brief_prompt(prompt, project) if (first and s["harness"] == "codex") else prompt)
            task += "\n\n" + (REMOTE_DELIVERY if remote else DELIVERY_INSTRUCTIONS)
            if not first and s["harness"] == "codex":
                task += ("\n\n(Keep .snaplab/PLAN.md up to date: add the steps for this instruction and tick each one "
                         "when it is done.)")
            if remote:
                from .prompts import lang_name
                language = (st.room(s.get('room')) or {}).get('lang') or app.lang
                task += '\n\nAll explanations, photo captions, plans and reports must be in ' + lang_name(language) + '.'
                history = [e for e in st.session_events(sid) if e.get('kind') in ('prompt', 'message')]
                if not first:
                    task += '\n\nEarlier conversation (reference):\n' + '\n'.join(
                        e.get('kind', '') + ': ' + str(e.get('text', '')) for e in history[:-1])[-16000:]
                if s.get('required_files'):
                    task += '\nRequired output files: ' + ', '.join(s['required_files'])
            try:
                res = harness.run_turn(s, folder, task, ctl, first)
            except Exception as e:  # noqa: BLE001
                from .harnesses.base import TurnResult
                res = TurnResult(ok=False, error=f"{type(e).__name__}: {e}"[:400])
            if (remote and s['harness'] == 'pi' and s.get('auto_harness') and not s.get('fallback_used')
                    and int(res.extra.get('proxy_calls', 0)) < limit(app.cfg['sessions'].get('max_api_calls'))
                    and not res.ok and res.extra.get('safe_fallback') and res.extra.get('no_progress') and not ctl.cancelled()):
                try:
                    if str(s.get('started_by', '')).startswith('b-'):
                        app.chat.dialogue.reserve_session(s['started_by'])
                    ctl.update(harness='codex', fallback_used=True, fallback_reason=res.error,
                               remaining_api_calls=remaining_calls(app, res))
                    s = st.session(sid)
                    previous = res
                    continuation = ('Continue the unfinished task from the existing files. The previous attempt failed: '
                                    + (res.error or 'No progress') + '\nInspect the files and plan first; use smaller steps.\n' + task)
                    res = self.harnesses['codex'].run_turn(s, folder, continuation, ctl, first)
                    res.input_tokens += previous.input_tokens
                    res.output_tokens += previous.output_tokens
                    res.calls += previous.calls
                except ValueError as error:
                    res.error = str(error)
            took = time.monotonic() - t0
            changes = diff(before, snapshot(folder))
            if remote:
                # A remote turn returns only the files it changed, and each was checked for secrets on arrival.
                # Clean those (and the plan files) again here instead of reading the whole project.
                secrets_found = app.guard.clean_paths(folder, [c["path"] for c in changes if c["change"] != "deleted"]
                                                      + [".snaplab/PLAN.md", ".snaplab/KNOWLEDGE.md"])
            else:
                secrets_found = app.guard.clean_tree(folder)
            sync_failed = bool((res.extra or {}).get("sync_failed"))
            if s["harness"] in ("codex", "pi") and not sync_failed:
                from .harnesses.codex import session_home
                app.guard.clean_tree(session_home(app, sid))
                from .web_mcp import archive_knowledge
                try:
                    archive_knowledge(app, s, folder)
                except Exception:  # Keep the turn usable when a research file cannot be archived.
                    ctl.emit({"kind": "error", "text": app.t("ev.knowledge_failed"), "key": "ev.knowledge_failed"})
            if secrets_found:
                vars = {"n": str(len(secrets_found)), "files": ", ".join(secrets_found[:5])}
                ctl.emit({"kind": "error", "text": app.t("ev.secret_removed", **vars), "key": "ev.secret_removed", "vars": vars})
                app.act("error", "act.secret_removed", ref=sid)
                st.event("guard.removed", actor="snaplab", where=sid, data={"files": secrets_found[:20]})
            dsum = None
            if s.get("worktree") and s.get("base") and not sync_failed:
                gitops.commit_all(folder, f"SnapLab session {sid}, turn {int(s.get('turns') or 0) + 1}")
                dsum = gitops.diff_summary(folder, s["base"])
                if head_before:                 # this turn's changes, in full, for the raw archive (round 6)
                    patch = gitops.patch(folder, head_before)
                    if patch.strip():
                        app.archive.add("change", patch, project=s["project"], session=sid,
                                        bot=s.get("bot"), title=f"{s.get('title') or sid} · turn {int(s.get('turns') or 0) + 1}",
                                        ref=f"#/session/{sid}", meta={"from": head_before,
                                        **({"web_material": True, "untrusted": True} if remote else {})})
            tests = (res.extra or {}).get("tests")          # a harness may run the tests itself (and emit them)
            if isinstance(tests, dict):
                tests = dict(tests)
                tests.setdefault("ran_at", now_iso())
                tests.setdefault("command", project.get("test_command"))
            else:
                tests = None
            code_changed = code_before is None or code_before != artifact_version(folder, code_only=True)
            if tests is None and project.get("test_command") and res.ok and s["harness"] == "codex" and not code_changed:
                tests = {"status": "unchanged", "command": project["test_command"], "passed": None, "failed": None}
            if tests is None and project.get("test_command") and res.ok and s["harness"] == "codex" and not remote:
                tested_version = artifact_version(folder)
                ctl.emit({"kind": "note", "text": app.t("ev.tests.run"), "key": "ev.tests.run"})
                code, out = sandbox_run(app, sid, folder, project["test_command"],
                                        app.cfg["sessions"].get("test_timeout_s") or 0,
                                        git_dir=Path(s["git_dir"]) if s.get("git_dir") else None, cancel=ctl.cancelled)
                tests = parse_tests(code, out)
                tests.update(tested_version=tested_version, version_after=artifact_version(folder),
                             exit_code=code, source_type="runtime")
                tests["ran_at"] = now_iso()
                tests["command"] = project["test_command"]
                record_tests(app, s, tests, out)
                ctl.emit({"kind": "tests", "text": f"Tests: {tests['status']}", "passed": tests["passed"],
                          "failed": tests["failed"]})
            if s['harness'] in ('codex', 'pi') and res.ok:
                from .harnesses.selection import completion_error
                if tests is not None:
                    res.extra['tests'] = tests
                error = completion_error(res, folder, s.get('required_files', []),
                                         bool(project.get('test_command')) and (remote or code_changed))
                if error:
                    res.ok, res.error = False, error
            if dsum is not None and dsum["files"]:
                emit_files(ctl, app, dsum["changes"], dsum["files"], dsum["insertions"], dsum["deletions"])
            elif changes:
                emit_files(ctl, app, changes, len(changes), None, None)
            with self.lock:
                s = st.session(sid)
                if not sync_failed:
                    app.attachments.register_outputs(s, folder, changes)
                attachments, attachment_errors = app.attachments.collect(s, folder) if not ctl.cancelled() and not sync_failed else ([], [])
                merged = {c["path"]: c for c in s.get("changed_files") or []}
                if dsum is not None:
                    merged = {c["path"]: c for c in dsum["changes"]}
                    changes = []
                for c in changes:
                    old = merged.get(c["path"])
                    if old and old["change"] == "added" and c["change"] == "modified":
                        continue
                    if old and old["change"] == "added" and c["change"] == "deleted":
                        merged.pop(c["path"])
                        continue
                    merged[c["path"]] = c
                if ctl.cancelled():
                    status = "stopped"
                elif not res.ok:
                    status = "failed"
                elif res.asks_user:
                    status = "waiting"
                else:
                    status = "done" if str(s.get("started_by", "")).startswith("b-") else "review"
                s.update(status=status, ended=now_iso(), elapsed_s=round(float(s.get("elapsed_s") or 0) + took, 1),
                         turns=int(s.get("turns") or 0) + 1, calls=int(s.get("calls") or 0) + res.calls,
                         tokens={"input": s["tokens"]["input"] + res.input_tokens,
                                 "output": s["tokens"]["output"] + res.output_tokens},
                         last_message=(res.last_message or "")[:4000],
                         error=None if res.ok else (res.error or "failed"),
                         changed_files=sorted(merged.values(), key=lambda c: c["path"])[:300])
                if tests:
                    s["tests"] = tests
                s["artifact_version"] = artifact_version(folder)
                if dsum is not None:
                    s["diff"] = {k: dsum[k] for k in ("files", "insertions", "deletions", "stat")}
                pl, _ = planmod.read(folder)
                if pl:
                    s["plan"] = pl
                s["result"] = self._result(s, status, res, took)
                if remote:
                    s["result"].update(web_material=True, untrusted=True)
                s["result"]["attachments"] = attachments
                s["result"]["attachment_errors"] = attachment_errors
                st.save_session(s)
                self.controls.pop(sid, None)       # the turn is over: a follow-up may start right away
            self._write_result_files(s, folder)
            self._write_state_file(project, folder)
            if not res.ok and status == "failed":
                ctl.emit({"kind": "error", "text": s["error"]})
            app.bus.publish("session.updated", {"session": self.summary(s)})
            self._announce(s, project, status)
        finally:
            with self.lock:
                if self.controls.get(sid) is ctl:
                    self.controls.pop(sid, None)

    # ------------------------------------------------------------ results, state, merging

    def _result(self, s: dict, status: str, res, took: float) -> dict:
        """result.json: facts measured by SnapLab (status, diff, tests, time, tokens) plus the agent's summary."""
        t = s.get("tests") or None
        return {"session": s["id"], "project": s["project"], "harness": s["harness"], "turn": int(s.get("turns") or 0),
                "status": status, "summary": words(res.last_message or "", 120), "error": s.get("error"),
                "changed_files": (s.get("changed_files") or [])[:50], "diff": s.get("diff"),
                "tests": {k: t.get(k) for k in ("status", "passed", "failed", "command", "tested_version",
                                              "version_after", "exit_code", "ran_at", "source_type", "evidence_record")} if t else None,
                "artifact_version": s.get("artifact_version"),
                "duration_s": round(took, 1), "tokens": s.get("tokens"), "branch": s.get("branch"), "ended": now_iso()}

    def _write_result_files(self, s: dict, folder: Path) -> None:
        r = s.get("result")
        if not r:
            return
        self.app.store.write_json(self.app.store.path("sessions", f"{s['id']}.result.json"), r)
        try:
            (folder / ".snaplab").mkdir(parents=True, exist_ok=True)
            text = self.app.guard.clean(__import__("json").dumps(r, ensure_ascii=False, indent=1))
            (folder / ".snaplab" / "result.json").write_text(text, encoding="utf-8")
        except OSError:
            pass

    def _write_state_file(self, project: dict, folder: Path | None = None) -> None:
        """STATE.md (written only by SnapLab): the project's sessions and their last results."""
        ss = self.app.store.sessions(project["id"])[:12]
        lines = [f"# State of {project['name']}", "", f"Updated by SnapLab at {now_iso()}.", "", "## Sessions"]
        for x in ss:
            r = x.get("result") or {}
            d = x.get("diff") or {}
            lines.append(f"- {x['id']} [{x['status']}] {x.get('title', '')[:70]}"
                         + (f" — {d.get('files', 0)} files +{d.get('insertions', 0)} -{d.get('deletions', 0)}" if d else "")
                         + (f", tests {x['tests']['status']}" if x.get("tests") else "")
                         + (" (merged)" if x.get("merged") else ""))
            if r.get("summary"):
                lines.append(f"  {words(r['summary'], 40)}")
        if not ss:
            lines.append("- none yet")
        if self.app.lang == "ru":
            lines = [f"# Состояние проекта {project['name']}", "",
                     f"Обновлено SnapLab: {now_iso()}.", "", "## Задачи SnapCode"]
            test_status = {"passed": "пройдены", "failed": "есть ошибки", "not_run": "не запускались",
                           "running": "выполняются", "unknown": "результат неизвестен"}
            for x in ss:
                d = x.get("diff") or {}
                status = self.app.t("session.status." + x["status"])
                line = f"- {x['id']} [{status}] {x.get('title', '')[:70]}"
                if d:
                    line += " — " + self.app.t("rc.files", n=d.get("files", 0))
                    line += f" +{d.get('insertions', 0)} −{d.get('deletions', 0)}"
                if x.get("tests"):
                    line += ", тесты: " + test_status.get(x["tests"]["status"], "результат неизвестен")
                if x.get("merged"):
                    line += " (изменения включены в проект)"
                lines.append(line)
                if (x.get("result") or {}).get("summary"):
                    lines.append("  " + words(x["result"]["summary"], 40))
            if not ss:
                lines.append("- Задач пока нет")
        text = "\n".join(lines) + "\n"
        try:
            self.app.archive.add_doc(project, "STATE.md", text)
        except Exception:  # noqa: BLE001
            pass
        targets = [self.folder(project) / "STATE.md"]
        if folder is not None:
            targets.append(Path(folder) / ".snaplab" / "STATE.md")
        for f in targets:
            temporary = None
            try:
                f.parent.mkdir(parents=True, exist_ok=True)
                with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8-sig", dir=f.parent,
                                                 prefix=".state-", delete=False) as stream:
                    temporary = Path(stream.name)
                    stream.write(self.app.guard.clean(text))
                os.replace(temporary, f)
            except OSError:
                pass
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)

    def merge(self, sid: str) -> dict:
        """Merge the session's branch into the project's main branch (a merge commit). Conflicts abort the merge."""
        with self.lock:
            s = self.app.store.session(sid)
            if not s:
                raise LookupError("no such session")
            if not s.get("branch"):
                raise ValueError("this session has no branch to merge")
            if sid in self.controls:
                raise ValueError("the session is still running")
            project = self.app.store.project(s["project"])
            ok, info = gitops.merge(self.folder(project), s["branch"], f"Merge session {sid}: {s.get('title', '')[:60]}")
            if not ok:
                raise ValueError(f"could not merge: {info}")
            s["merged"] = now_iso()
            if s.get("status") in ("review", "done"):
                s["status"] = "idle"                    # merging is reviewing (QA round 2, L-5)
                s["reviewed"] = s["merged"]
            s["merge_commit"] = info
            self.app.store.save_session(s)
        self.app.store.event("session.merged", actor="user", where=sid, data={"commit": info})
        self.app.act("session", "act.merged", ref=sid, title=short_title(s.get("title") or sid), project=project["name"])
        self._write_state_file(project)
        self.app.bus.publish("session.updated", {"session": self.summary(s)})
        return s

    def _announce(self, s: dict, project: dict, status: str) -> None:
        app = self.app
        title = short_title(s.get("title") or s["id"])
        key = "act.session." + (status if status in ("review", "done", "waiting", "failed", "stopped") else "review")
        rec = app.act("session", key, ref=s["id"], extra={"status": status}, title=title, project=project["name"])
        text = rec["text"]
        key = f"{s['id']}:{status}:{s.get('turns')}"
        room = app.store.room(s["room"]) if s.get("room") else None
        reporter = s.get("started_by") if str(s.get("started_by", "")).startswith("b-") else None
        will_report = bool(room and reporter in room.get("bots", []) and status in ("review", "done", "failed", "waiting")
                           and (app.cfg.get("sessions") or {}).get("report_by_bot", True))
        if status in ("review", "done") and not will_report:
            app.push.notify("review", "SnapLab", text, url=f"/#/session/{s['id']}", tag=s["id"], dedupe=key)
        elif status == "waiting" and not will_report:
            app.push.notify("needs_you", "SnapLab", text, url=f"/#/session/{s['id']}", tag=s["id"], dedupe=key)
        elif status == "failed" and not will_report:
            app.push.notify("failed", "SnapLab", text + (f": {s.get('error', '')[:80]}" if s.get("error") else ""),
                            url=f"/#/session/{s['id']}", tag=s["id"], dedupe=key)
        app.store.event("session.turn_end", actor=s["harness"], where=s["id"],
                        data={"status": status, "project": s["project"], "tokens": s.get("tokens"), "turn": s.get("turns")})
        room = app.store.room(s["room"]) if s.get("room") else None
        if room:
            r = s.get("result") or {}
            card = result_card_text(r, app.lang) if r else text
            m = app.store.add_message(s["room"], {"role": "system", "text": text + "\n" + card, "session": s["id"],
                                                  "result_notification": will_report,
                                                  "attachments": r.get("attachments", []),
                                                  "attachment_errors": r.get("attachment_errors", []),
                                                  "title": title, "project_name": project["name"],
                                                  "result": {k: r.get(k) for k in ("status", "diff", "tests", "duration_s", "turn")}})
            app.bus.publish("message.new", {"room": s["room"], "message": m})
            app.bus.publish("room.updated", {"room": app.store.room(s["room"])})
            # F10: the bot that started the session tells the user the outcome in a few lines
            reporter = s.get("started_by") if str(s.get("started_by", "")).startswith("b-") else None
            if will_report:
                # Seed the new report origin before the queue can start its first turn.
                # Follow-up turns retain session context, including earlier web material.
                for read in app.chat.dialogue.web_reads(session_origin(s["id"])):
                    app.chat.dialogue.mark_web_read(m["id"], s["room"], read)
                app.chat._enqueue(s["room"], reporter, m["id"], 1)

    # ------------------------------------------------------------ views

    def display(self, sid: str, value):
        """Shorten paths on the PC before they reach the phone or the timeline (QA V-4): the session folder becomes
        ".", the project folder its name, SnapLab's data folder <data>, the home folder ~; shell wrappers around
        commands are dropped. Applied to strings anywhere inside value."""
        s = self.app.store.session(sid) or {}
        project = self.app.store.project(s.get("project") or "") or {}
        pairs = []
        if s.get("worktree"):
            pairs.append((s["worktree"], "."))
        if project.get("slug"):
            pairs.append((str(self.folder(project)), project["slug"]))
        try:
            from .harnesses.codex import session_home
            pairs.append((str(session_home(self.app, sid)), "<codex-home>"))
        except Exception:  # noqa: BLE001
            pass
        pairs.append((str(Path(self.app.cfg["data_dir"]).resolve()), "<data>"))
        pairs.append((str(Path.home()), "~"))
        pats = []
        for real, short in pairs:
            real = real.rstrip("\\/")
            if len(real) < 4:
                continue
            parts = [re.escape(x) for x in re.split(r"[\\/]+", real) if x]
            pats.append((re.compile(r"(?i)" + r"(?:\\{1,2}|/)".join(parts)), short))

        def clean(text: str) -> str:
            for pat, short in pats:
                text = pat.sub(lambda _m, r=short: r, text)
            return self.app.redactor.clean(text) if getattr(self.app, "redactor", None) else text

        def walk(v):
            if isinstance(v, str):
                return clean(v)
            if isinstance(v, list):
                return [walk(x) for x in v]
            if isinstance(v, dict):
                return {k: walk(unwrap_command(x) if k == "text" and v.get("kind") == "command" and isinstance(x, str) else x)
                        for k, x in v.items()}
            return v
        return walk(value)

    def summary(self, s: dict, events: int = 0) -> dict:
        project = self.app.store.project(s["project"]) or {}
        elapsed = float(s.get("elapsed_s") or 0)
        if s.get("status") == "running" and s.get("started"):
            try:
                import datetime as dt
                elapsed += (dt.datetime.now().astimezone() - dt.datetime.fromisoformat(s["started"])).total_seconds()
            except ValueError:
                pass
        out = {k: s.get(k) for k in ("id", "project", "harness", "title", "status", "created", "started", "ended",
                                     "updated", "tokens", "calls", "turns", "tests", "error", "started_by", "bot", "room")}
        out["project_name"] = project.get("name")
        out["elapsed_s"] = round(elapsed, 1)
        out["last_prompt"] = self.display(s["id"], (s.get("last_prompt") or "")[:1500])
        out["last_message"] = self.display(s["id"], (s.get("last_message") or "")[:3000])
        out["error"] = self.display(s["id"], s.get("error")) if s.get("error") else None
        out["changed_files"] = (s.get("changed_files") or [])[:100]
        out["changed_count"] = len(s.get("changed_files") or [])
        for k in ("branch", "diff", "merged"):
            out[k] = s.get(k)
        pl = s.get("plan") or None
        out["plan"] = self.display(s["id"], pl) if pl else None
        out["doing"] = self.display(s["id"], pl.get("now")) if pl and s.get("status") in ("running", "queued") else None
        out["result"] = self.display(s["id"], s.get("result")) if s.get("result") else None
        out["worktree"] = bool(s.get("worktree"))
        with self.lock:
            out['screen_seq'] = self.screens.get(s['id'], (None,0))[1]
        if events:
            out["events"] = self.display(s["id"], self.app.store.session_events(s["id"], events))
        return out
