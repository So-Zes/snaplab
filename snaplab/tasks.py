"""Scheduled tasks (round 6): "every morning at 8, check ..." becomes a task of the bot that was asked.

- Made by a bot with schedule_task (config approvals.schedule_task decides whether the user confirms on a card).
- Kinds: daily at HH:MM, weekly on some days at HH:MM, every N minutes, or once (a wake-up a bot sets for itself
  with schedule_wakeup: one run in the chat where it was set, then the task is removed).
- No cap on how many tasks a bot or SnapLab has unless tasks.max_per_bot / tasks.max_total are set (0 = no limit).
- Runs inside the SnapLab server (no Windows Task Scheduler): a thread checks every 20 seconds. A run posts a short
  note into the bot's own chat and the bot answers there, behind the user's chats in the slot order (priority
  "session"). The phone is notified only when the bot needs the user (it then asks on a card).
- When the PC was off at the time, a run that was missed by less than `tasks.catch_up_hours` (6) is done once
  when SnapLab is back; older ones are skipped. A missed wake-up always runs once.
- data/tasks.json holds the tasks; every run is also in the raw archive through the messages of the chat.
"""
from __future__ import annotations

import datetime as dt
import re
import threading

from .store import new_id, now_iso

DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
MIN_MINUTES = 1
TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
DEFAULTS = {"catch_up_hours": 6, "max_per_bot": 0, "max_total": 0, "check_s": 20}   # 0 = no limit


def _now() -> dt.datetime:
    return dt.datetime.now().astimezone()


def clean_schedule(sch: dict) -> dict:
    """Check and normalise a schedule. Raises ValueError with a sentence the model can act on."""
    if not isinstance(sch, dict):
        raise ValueError("give the schedule as kind daily, weekly, every or once")
    kind = str(sch.get("kind") or "").lower()
    if kind in ("day", "everyday", "every_day"):
        kind = "daily"
    if kind in ("week",):
        kind = "weekly"
    if kind in ("interval", "minutes", "hourly"):
        kind = "every"
    if kind in ("wakeup", "wake", "later", "after", "at"):
        kind = "once"
    if kind == "once":
        if sch.get("at"):
            try:
                at = dt.datetime.fromisoformat(str(sch["at"]))
            except ValueError:
                raise ValueError("at must be a date and time like 2026-11-16T07:30") from None
            at = at if at.tzinfo else at.astimezone()
        else:
            try:
                minutes = float(sch.get("in_minutes") or sch.get("minutes") or 0)
            except (TypeError, ValueError):
                minutes = 0
            if minutes <= 0:
                raise ValueError("give minutes (more than 0) or at")
            at = _now() + dt.timedelta(minutes=minutes)
        return {"kind": "once", "at": at.isoformat(timespec="seconds")}
    if kind in ("daily", "weekly"):
        tm = str(sch.get("time") or "").strip()
        m = TIME_RE.match(tm)
        if not m:
            raise ValueError("time must be HH:MM in 24-hour form, for example 08:00")
        out = {"kind": kind, "time": f"{int(m.group(1)):02d}:{m.group(2)}"}
        if kind == "weekly":
            days = [str(d).strip().lower()[:3] for d in (sch.get("days") or [])]
            days = [d for d in DAYS if d in days]
            if not days:
                raise ValueError("a weekly task needs days, for example [\"mon\", \"thu\"]")
            out["days"] = days
        return out
    if kind == "every":
        try:
            minutes = int(sch.get("every_minutes") or sch.get("minutes") or 0)
        except (TypeError, ValueError):
            minutes = 0
        if minutes < MIN_MINUTES:
            raise ValueError(f"the shortest interval is {MIN_MINUTES} minutes")
        return {"kind": "every", "every_minutes": min(minutes, 7 * 24 * 60)}
    raise ValueError("kind must be daily, weekly, every or once")


def next_run(sch: dict, after: dt.datetime) -> dt.datetime:
    """The first run time strictly after `after` (local time). A once task keeps its time (it runs when due)."""
    if sch["kind"] == "once":
        return dt.datetime.fromisoformat(sch["at"])
    if sch["kind"] == "every":
        return after + dt.timedelta(minutes=int(sch["every_minutes"]))
    hh, mm = (int(x) for x in sch["time"].split(":"))
    day = after.replace(hour=hh, minute=mm, second=0, microsecond=0)
    for i in range(0, 9):
        cand = day + dt.timedelta(days=i)
        if cand <= after:
            continue
        if sch["kind"] == "weekly" and DAYS[cand.weekday()] not in sch["days"]:
            continue
        return cand
    return after + dt.timedelta(days=1)


def describe(sch: dict) -> str:
    """The schedule in plain English (for the model; the phone words it from the language files)."""
    if sch["kind"] == "once":
        at = dt.datetime.fromisoformat(sch["at"]).astimezone()
        return f"once at {at.strftime('%H:%M')}" + ("" if at.date() == _now().date() else f" on {at.date().isoformat()}")
    if sch["kind"] == "every":
        m = int(sch["every_minutes"])
        return f"every {m // 60} h {m % 60} min" if m % 60 and m >= 60 else (f"every {m // 60} hours" if m % 60 == 0 else f"every {m} minutes")
    if sch["kind"] == "weekly":
        return f"on {', '.join(d.capitalize() for d in sch['days'])} at {sch['time']}"
    return f"every day at {sch['time']}"


class Scheduler:
    def __init__(self, app):
        self.app = app
        self.cfg = {**DEFAULTS, **(app.cfg.get("tasks") or {})}
        self.lock = threading.RLock()
        self.stop_ev = threading.Event()
        self.running: set[str] = set()

    # ------------------------------------------------------------ data

    def all(self) -> list[dict]:
        return self.app.store.read_json("tasks.json", [])

    def _save_all(self, tasks: list[dict]) -> None:
        self.app.store.write_json("tasks.json", tasks)
        for task in tasks:
            self.app.ledger.record("task", task)

    def get(self, tid: str) -> dict | None:
        return next((t for t in self.all() if t["id"] == tid), None)

    def for_bot(self, bot_id: str | None) -> list[dict]:
        return [t for t in self.all() if bot_id is None or t["bot"] == bot_id]

    def view(self, t: dict) -> dict:
        b = self.app.store.bot(t["bot"]) or {}
        return {**t, "bot_name": b.get("name"), "when": describe(t["schedule"]),
                "upcoming": self.occurrences(t)}

    def occurrences(self, task: dict, now: dt.datetime | None = None) -> list[str]:
        """Today's and tomorrow's scheduled times, computed in the scheduler's timezone."""
        if task.get("status") != "active":
            return []
        now = now or _now()
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + dt.timedelta(days=2)
        sch = task["schedule"]
        if sch["kind"] == "once":
            when = dt.datetime.fromisoformat(sch["at"])
            return [when.isoformat()] if start <= when < end else []
        if sch["kind"] == "every":
            if not task.get("next_run"):
                return []
            when = dt.datetime.fromisoformat(task["next_run"]).astimezone(now.tzinfo)
            step = dt.timedelta(minutes=sch["every_minutes"])
            if when < start:
                when += ((start - when) // step) * step
                if when < start:
                    when += step
            # Earlier interval runs cannot be reconstructed after a manual run/resume.
        else:
            when = next_run(sch, start - dt.timedelta(microseconds=1))
        out = []
        while when < end and len(out) < 96:
            out.append(when.isoformat())
            when = next_run(sch, when)
        return out

    def _publish(self) -> None:
        self.app.bus.publish("tasks.updated", {"tasks": [self.view(t) for t in self.all()]})

    # ------------------------------------------------------------ changes

    def create(self, *, bot_id: str, title: str, prompt: str, schedule: dict, by: str = "you",
               room_id: str | None = None) -> dict:
        """room_id: the chat a run is posted to (default: the bot's own chat)."""
        b = self.app.store.bot(bot_id)
        if not b:
            raise LookupError("no such bot")
        title = " ".join((title or "").split())[:80]
        prompt = (prompt or "").strip()[:2000]
        if not prompt:
            raise ValueError("say what the task should do (prompt)")
        sch = clean_schedule(schedule)
        from .config import limit
        with self.lock:
            tasks = self.all()
            per_bot, total = limit(self.cfg.get("max_per_bot")), limit(self.cfg.get("max_total"))
            if sum(1 for t in tasks if t["bot"] == bot_id) >= per_bot:
                raise ValueError(f"{b['name']} already has {int(per_bot)} scheduled tasks; stop one first")
            if len(tasks) >= total:
                raise ValueError("SnapLab already has the most scheduled tasks it allows; stop one first")
            room = self.app.store.room(room_id) if room_id else None
            if not room or bot_id not in (room.get("bots") or []):
                room = self.app.store.ensure_dm(b)
            t = {"id": new_id("t"), "bot": bot_id, "room": room["id"], "title": title or prompt[:60], "prompt": prompt,
                 "schedule": sch, "status": "active", "created": now_iso(), "created_by": by, "runs": 0,
                 "last_run": None, "next_run": next_run(sch, _now()).isoformat(timespec="seconds")}
            tasks.append(t)
            self._save_all(tasks)
        self.app.act("task", "act.task_added", ref=t["id"], bot=b["name"], title=t["title"])
        self.app.store.event("task.create", actor=by, where=bot_id, data={"task": t["id"], "schedule": sch})
        self._publish()
        return t

    def change(self, tid: str, action: str) -> dict | None:
        with self.lock:
            tasks = self.all()
            t = next((x for x in tasks if x["id"] == tid), None)
            if not t:
                raise LookupError("no such task")
            if action == "delete":
                self.app.ledger.record("task", {**t, "status": "deleted"})
                tasks = [x for x in tasks if x["id"] != tid]
                self._save_all(tasks)
                self.app.store.event("task.delete", actor="user", where=t["bot"], data={"task": tid, "title": t["title"]})
                self._publish()
                return None
            if action == "pause":
                t["status"] = "paused"
            elif action == "resume":
                t["status"] = "active"
                t["next_run"] = next_run(t["schedule"], _now()).isoformat(timespec="seconds")
            else:
                raise ValueError("action must be pause, resume or delete")
            self._save_all(tasks)
        self._publish()
        return t

    # ------------------------------------------------------------ running

    def tick(self, now: dt.datetime | None = None) -> list[str]:
        """Run every task that is due. Returns the ids that ran."""
        now = now or _now()
        ran, once = [], []
        with self.lock:
            tasks = self.all()
            changed = False
            for t in tasks:
                if t.get("status") != "active" or not t.get("next_run"):
                    continue
                try:
                    due = dt.datetime.fromisoformat(t["next_run"])
                except ValueError:
                    due = now
                if due > now:
                    continue
                if t["schedule"]["kind"] == "once":      # a wake-up: one run, however late, then it is gone
                    t.update(status="done", next_run=None, last_run=now.isoformat(timespec="seconds"), runs=1)
                    once.append(t)
                    ran.append(t["id"])
                    changed = True
                    continue
                late = (now - due).total_seconds() / 3600
                t["next_run"] = next_run(t["schedule"], now).isoformat(timespec="seconds")
                changed = True
                if late > float(self.cfg["catch_up_hours"]):
                    t["missed"] = int(t.get("missed") or 0) + 1
                    continue
                t["last_run"] = now.isoformat(timespec="seconds")
                t["runs"] = int(t.get("runs") or 0) + 1
                ran.append(t["id"])
            if changed:
                self._save_all(tasks)
                if once:
                    for t in once:
                        self.app.ledger.record("task", {**t, "status": "deleted"})
                    self.app.store.write_json("tasks.json", [x for x in tasks if x not in once])
        for tid in ran:
            self.run(tid, next((t for t in once if t["id"] == tid), None))
        if ran:
            self._publish()
        return ran

    def run(self, tid: str, task: dict | None = None) -> dict | None:
        """Post the task into its chat (the bot's own chat, or the room a wake-up was set in) and let the bot do it there."""
        t = task or self.get(tid)
        if not t:
            return None
        st, chat = self.app.store, self.app.chat
        b = st.bot(t["bot"])
        if not b:
            return None
        room = st.room(t.get("room") or "")
        if not room or b["id"] not in (room.get("bots") or []):
            room = st.dm_room(b["id"]) or st.ensure_dm(b)
        vars = {"title": t["title"]}
        if t["schedule"]["kind"] == "once":
            text = (f"[Wake-up you set at {str(t.get('created', ''))[11:16]}] {t['prompt']}\n"
                    "Check what you meant to check (get_session / list_sessions show progress) and carry on. "
                    "If there is nothing to tell the user, reply exactly [SILENT].")
        else:
            text = (f"[Scheduled task \"{t['title']}\" ({describe(t['schedule'])})] {t['prompt']}\n"
                    "Do it now and report the result in a few short lines. Use ask_user only if the user must decide "
                    "something; otherwise do not ask anything.")
        m = st.add_message(room["id"], {"role": "system", "text": self.app.guard.clean(text), "task": tid,
                                        "key": "srv.task.run", "vars": vars})
        self.app.bus.publish("message.new", {"room": room["id"], "message": m})
        self.app.bus.publish("room.updated", {"room": st.room(room["id"])})
        st.event("task.run", actor="snaplab", where=b["id"], data={"task": tid, "message": m["id"]})
        chat.task_origins.add(m["id"])
        chat._enqueue(room["id"], b["id"], m["id"], 1)
        return m

    def start(self) -> None:
        def loop():
            while not self.stop_ev.wait(float(self.cfg["check_s"])):
                try:
                    self.tick()
                except Exception as e:  # noqa: BLE001
                    self.app.log(f"scheduled tasks: {e!r}")
        threading.Thread(target=loop, daemon=True, name="tasks").start()
