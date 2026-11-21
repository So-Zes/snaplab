"""All data SnapLab keeps, as plain JSON / JSONL files in the data folder (git-ignored).

    settings.json          language, onboarding, notification switches
    bots.json              bots (name, look, prompt, projects); bots/<id>/memory.md is each bot's memory
    rooms.json             chat rooms; messages/<room>.jsonl holds the history (append-only)
    questions.json         question cards that bots asked the user
    projects.json          projects (folders under projects_dir)
    sessions/<id>.json     one file per session; sessions/<id>.events.jsonl is its timeline
    dashboard.json         widgets that bots can rewrite (validated data only) and the cards the user closed
    activity.jsonl         recent events for the dashboard
    usage.json             API calls and tokens: totals and one entry per day (by use and by model)
    usage/<day>.json       the same day by hour and by caller (room or session)
    push.json              Web Push subscriptions (vapid key in push/)
    auth.json              pairing links and device marks (hashes only)

One process owns the folder; a re-entrant lock serialises writes. Files are replaced atomically.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import secrets
import threading
import time
from pathlib import Path


def now_iso() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def new_id(prefix: str) -> str:
    return f"{prefix}-{secrets.token_hex(4)}"


def today() -> str:
    return dt.date.today().isoformat()


SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(name: str, fallback: str = "project") -> str:
    """A folder name: ASCII words when there are any; otherwise the name's own letters (a Japanese name stays
    readable instead of becoming "project"; QA round 2, L-11)."""
    s = SLUG_RE.sub("-", (name or "").lower()).strip("-")[:40]
    if s and re.search(r"[a-z0-9]", s):
        return s
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f.\s]+', "-", (name or "").strip()).strip("-")[:24]
    return s or fallback


# Three kinds of phone notification (each can be switched off): something needs you, a session is ready for review, a session failed.
DEFAULT_NOTIFY = {"needs_you": True, "review": True, "failed": True}


class Store:
    def __init__(self, data_dir: str | Path):
        self.dir = Path(data_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        for sub in ("messages", "sessions", "bots", "push", "logs", "events"):
            (self.dir / sub).mkdir(exist_ok=True)
        self.lock = threading.RLock()
        self.guard = None          # set by the app: removes secret values from everything written to disk

    # ------------------------------------------------------------ files

    def path(self, *parts) -> Path:
        return self.dir.joinpath(*parts)

    def read_json(self, name: str | Path, default):
        p = self.path(name) if not isinstance(name, Path) else name
        with self.lock:
            for _ in range(10):
                try:
                    return json.loads(p.read_text(encoding="utf-8"))
                except FileNotFoundError:
                    return default
                except (json.JSONDecodeError, PermissionError):
                    time.sleep(0.05)
            return default

    def write_json(self, name: str | Path, data) -> None:
        p = self.path(name) if not isinstance(name, Path) else name
        with self.lock:
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_name(p.name + ".tmp")
            text = json.dumps(data, ensure_ascii=False, indent=1)
            if self.guard:
                text = self.guard.clean(text)
            tmp.write_text(text, encoding="utf-8")
            for _ in range(20):
                try:
                    os.replace(tmp, p)
                    return
                except PermissionError:
                    time.sleep(0.05)
            os.replace(tmp, p)

    def append_jsonl(self, name: str | Path, rec: dict) -> None:
        p = self.path(name) if not isinstance(name, Path) else name
        with self.lock:
            p.parent.mkdir(parents=True, exist_ok=True)
            with open(p, "a", encoding="utf-8") as f:
                line = json.dumps(rec, ensure_ascii=False)
                if self.guard:
                    line = self.guard.clean(line)
                f.write(line + "\n")

    def read_jsonl(self, name: str | Path, limit: int | None = None) -> list[dict]:
        p = self.path(name) if not isinstance(name, Path) else name
        with self.lock:
            try:
                lines = p.read_text(encoding="utf-8").splitlines()
            except FileNotFoundError:
                return []
        if limit:
            lines = lines[-limit:]
        out = []
        for ln in lines:
            try:
                out.append(json.loads(ln))
            except json.JSONDecodeError:
                continue
        return out

    # ------------------------------------------------------------ settings

    def settings(self) -> dict:
        s = self.read_json("settings.json", {})
        s.setdefault("lang", None)
        s.setdefault("onboarded", False)
        s.setdefault("user_name", "")
        s.setdefault("theme", "system")          # light | dark | system (follow the phone; round 6)
        s.setdefault("icon_style", "line")
        n = dict(DEFAULT_NOTIFY)
        n.update({k: v for k, v in (s.get("notify") or {}).items() if k in DEFAULT_NOTIFY})
        s["notify"] = n
        return s

    def update_settings(self, **changes) -> dict:
        with self.lock:
            s = self.settings()
            for k, v in changes.items():
                if k == "notify" and isinstance(v, dict):
                    s["notify"].update({kk: bool(vv) for kk, vv in v.items() if kk in DEFAULT_NOTIFY})
                else:
                    s[k] = v
            self.write_json("settings.json", s)
            return s

    # ------------------------------------------------------------ bots

    def bots(self) -> list[dict]:
        return self.read_json("bots.json", [])

    def bot(self, bot_id: str) -> dict | None:
        return next((b for b in self.bots() if b["id"] == bot_id), None)

    def bot_by_name(self, name: str) -> dict | None:
        n = (name or "").strip().lower()
        return next((b for b in self.bots() if b["name"].lower() == n), None)

    def orchestrator(self) -> dict | None:
        return next((b for b in self.bots() if b.get("orchestrator")), None)

    def save_bot(self, bot: dict) -> dict:
        with self.lock:
            bots = self.bots()
            bot["updated"] = now_iso()
            for i, b in enumerate(bots):
                if b["id"] == bot["id"]:
                    bots[i] = bot
                    break
            else:
                bot.setdefault("created", now_iso())
                bots.append(bot)
            self.write_json("bots.json", bots)
            mem = self.path("bots", bot["id"], "memory.md")
            if not mem.exists():
                mem.parent.mkdir(parents=True, exist_ok=True)
                mem.write_text("", encoding="utf-8")
            return bot

    def former_bots(self) -> list[dict]:
        """Bots the user deleted (2026-11-16). Kept so their old messages still show a name."""
        return self.read_json("former_bots.json", [])

    def archived_bots(self) -> list[dict]:
        """Bots the user archived (2026-11-17): off the team for now with everything kept, so they can come back.
        Each record is the bot as it was plus "archived", what archiving changed (see App.archive_bot)."""
        return self.read_json("archived_bots.json", [])

    def archive_bot(self, bot_id: str, record: dict) -> dict | None:
        """Move a bot from bots.json to archived_bots.json; its memory, messages and files stay where they are."""
        with self.lock:
            bots = self.bots()
            bot = next((b for b in bots if b["id"] == bot_id), None)
            if not bot:
                return None
            self.write_json("archived_bots.json", [b for b in self.archived_bots() if b["id"] != bot_id]
                            + [{**bot, "archived": record}])
            self.write_json("bots.json", [b for b in bots if b["id"] != bot_id])
            return bot

    def unarchive_bot(self, bot_id: str) -> tuple[dict, dict] | None:
        """Put an archived bot back on the team at its old place in the list. Returns the bot and what archiving changed."""
        with self.lock:
            archived = self.archived_bots()
            rec = next((b for b in archived if b["id"] == bot_id), None)
            if not rec:
                return None
            bot = {k: v for k, v in rec.items() if k != "archived"}
            bots = [b for b in self.bots() if b["id"] != bot_id]
            at = rec.get("archived") or {}
            bots.insert(min(int(at.get("index", len(bots))), len(bots)), bot)
            self.write_json("bots.json", bots)
            self.write_json("archived_bots.json", [b for b in archived if b["id"] != bot_id])
            return bot, at

    def remove_bot(self, bot_id: str) -> dict | None:
        """Take a bot off the team: its record moves to former_bots.json; its memory and messages stay."""
        with self.lock:
            bots = self.bots()
            gone = next((b for b in bots if b["id"] == bot_id), None)
            if not gone:
                return None
            self.write_json("bots.json", [b for b in bots if b["id"] != bot_id])
            self.write_json("former_bots.json", self.former_bots() + [{**gone, "deleted": now_iso()}])
            return gone

    def memory(self, bot_id: str) -> str:
        p = self.path("bots", bot_id, "memory.md")
        try:
            return p.read_text(encoding="utf-8")
        except FileNotFoundError:
            return ""

    def user_note(self) -> str:
        try:
            return self.path("shared", "user.md").read_text(encoding="utf-8")
        except FileNotFoundError:
            return ""

    def save_note_file(self, rel: tuple, text: str) -> str:
        with self.lock:
            p = self.path(*rel)
            p.parent.mkdir(parents=True, exist_ok=True)
            if self.guard:
                text = self.guard.clean(text)
            p.write_text(text, encoding="utf-8")
            return text

    def note_mtime(self, rel: tuple) -> str | None:
        try:
            ts = self.path(*rel).stat().st_mtime
        except OSError:
            return None
        return dt.datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds")

    def memory_log(self, bot_id: str, line: str) -> None:
        """Append-only daily record of memory changes (never loaded into a model call)."""
        stamp = dt.datetime.now()
        p = self.path("bots", bot_id or "_user", "daily", f"{stamp:%Y-%m-%d}.md")
        with self.lock:
            p.parent.mkdir(parents=True, exist_ok=True)
            if self.guard:
                line = self.guard.clean(line)
            with open(p, "a", encoding="utf-8") as f:
                f.write(f"- {stamp:%H:%M} {line}\n")

    def write_memory(self, bot_id: str, text: str, mode: str = "append", limit: float | None = None) -> str:
        with self.lock:
            p = self.path("bots", bot_id, "memory.md")
            p.parent.mkdir(parents=True, exist_ok=True)
            cur = self.memory(bot_id)
            if mode == "replace":
                new = text.strip() + "\n" if text.strip() else ""
            else:
                stamp = dt.datetime.now().strftime("%Y-%m-%d")
                new = (cur.rstrip() + "\n" if cur.strip() else "") + f"- [{stamp}] {' '.join(text.split())}\n"
            if limit and limit != float("inf") and len(new) > limit:       # only when a size limit is configured
                new = new[-int(limit):]
            if self.guard:
                new = self.guard.clean(new)
            p.write_text(new, encoding="utf-8")
            return new

    # ------------------------------------------------------------ rooms and messages

    def rooms(self, include_deleted: bool = False) -> list[dict]:
        rooms = self.read_json("rooms.json", [])
        return rooms if include_deleted else [r for r in rooms if not r.get("deleted")]

    def room(self, room_id: str) -> dict | None:
        return next((r for r in self.rooms() if r["id"] == room_id), None)

    def save_room(self, room: dict) -> dict:
        with self.lock:
            rooms = self.rooms(include_deleted=True)
            for i, r in enumerate(rooms):
                if r["id"] == room["id"]:
                    rooms[i] = room
                    break
            else:
                room.setdefault("created", now_iso())
                rooms.append(room)
            self.write_json("rooms.json", rooms)
            return room

    def dm_room(self, bot_id: str) -> dict | None:
        return next((r for r in self.rooms() if r.get("kind") == "dm" and r.get("bots") == [bot_id]), None)

    def ensure_dm(self, bot: dict) -> dict:
        with self.lock:
            r = self.dm_room(bot["id"])
            if r:
                return r
            return self.save_room({"id": new_id("r"), "kind": "dm", "name": bot["name"], "bots": [bot["id"]],
                                   "last_ts": now_iso(), "last_text": ""})

    def add_message(self, room_id: str, msg: dict, touch_room: bool = True) -> dict:
        """Append a message. Writing again with the same id replaces it (it keeps its first place in the room):
        a bot reply is saved when it starts ("streaming") and again when it ends (QA B-2)."""
        with self.lock:
            msg.setdefault("id", new_id("m"))
            msg.setdefault("ts", now_iso())
            msg["room"] = room_id
            self.append_jsonl(Path(self.path("messages", f"{room_id}.jsonl")), msg)
            cb = getattr(self, "on_message", None)
            if cb:
                try:
                    cb(room_id, msg)            # the raw archive (round 6)
                except Exception:  # noqa: BLE001 - archiving must never break a message
                    pass
            room = self.room(room_id) if touch_room else None
            if room and msg.get("status") != "silent":     # a bot that chose to stay quiet leaves the preview alone
                room["last_ts"] = msg["ts"]
                room["last_text"] = (msg.get("text") or msg.get("preview") or "")[:120]
                room["last_role"] = msg.get("role")
                self.save_room(room)
            return msg

    # a reply being written is also kept in a small draft file (overwritten every few seconds), so a restart
    # in the middle keeps what had already arrived (QA2 L-1); the draft goes away when the reply is saved
    def save_draft(self, msg_id: str, text: str, thinking: str) -> None:
        self.write_json(self.path("messages", "drafts", f"{msg_id}.json"), {"text": text, "thinking": thinking[-20000:]})

    def draft(self, msg_id: str) -> dict | None:
        return self.read_json(self.path("messages", "drafts", f"{msg_id}.json"), None)

    def drop_draft(self, msg_id: str) -> None:
        try:
            self.path("messages", "drafts", f"{msg_id}.json").unlink()
        except (FileNotFoundError, PermissionError):
            pass

    def messages(self, room_id: str, limit: int = 200) -> list[dict]:
        rows = self.read_jsonl(self.path("messages", f"{room_id}.jsonl"), limit * 2 + 20 if limit else None)
        order: dict[str, dict] = {}
        for m in rows:                       # later writes of the same id replace the earlier one, in place
            order[m.get("id") or f"_{len(order)}"] = m
        out = list(order.values())
        return out[-limit:] if limit else out

    # ------------------------------------------------------------ questions

    def questions(self) -> list[dict]:
        return self.read_json("questions.json", [])

    def question(self, qid: str) -> dict | None:
        return next((q for q in self.questions() if q["id"] == qid), None)

    def save_question(self, q: dict) -> dict:
        with self.lock:
            qs = self.questions()
            for i, x in enumerate(qs):
                if x["id"] == q["id"]:
                    qs[i] = q
                    break
            else:
                q.setdefault("created", now_iso())
                qs.append(q)
            # keep the file small: drop answered questions older than the newest 200
            if len(qs) > 300:
                open_ = [x for x in qs if x.get("status") == "open"]
                done = [x for x in qs if x.get("status") != "open"][-200:]
                qs = done + open_
            self.write_json("questions.json", qs)
            if q.get("kind") == "approval" and getattr(self, "on_state", None):
                self.on_state("approval", q)
            return q

    # ------------------------------------------------------------ projects

    def projects(self) -> list[dict]:
        return self.read_json("projects.json", [])

    def project(self, ref: str) -> dict | None:
        ref = (ref or "").strip()
        ps = self.projects()
        return (next((p for p in ps if p["id"] == ref), None) or next((p for p in ps if p["slug"] == ref), None)
                or next((p for p in ps if p["name"].lower() == ref.lower()), None))

    def save_project(self, p: dict) -> dict:
        with self.lock:
            ps = self.projects()
            for i, x in enumerate(ps):
                if x["id"] == p["id"]:
                    ps[i] = p
                    break
            else:
                p.setdefault("created", now_iso())
                ps.append(p)
            self.write_json("projects.json", ps)
            if getattr(self, "on_state", None):
                self.on_state("project", p)
            return p

    # ------------------------------------------------------------ sessions

    def session(self, sid: str) -> dict | None:
        if not re.fullmatch(r"s-[0-9a-f]{8}", sid or ""):
            return None
        return self.read_json(self.path("sessions", f"{sid}.json"), None)

    def save_session(self, s: dict) -> dict:
        with self.lock:
            s["updated"] = now_iso()
            self.write_json(self.path("sessions", f"{s['id']}.json"), s)
            if getattr(self, "on_state", None):
                self.on_state("work", s)
            return s

    def sessions(self, project_id: str | None = None) -> list[dict]:
        out = []
        for f in sorted(self.path("sessions").glob("s-*.json")):
            if not re.fullmatch(r"s-[0-9a-f]{8}\.json", f.name):     # skip *.result.json, *.convo.json
                continue
            s = self.read_json(f, None)
            if s and (project_id is None or s.get("project") == project_id):
                out.append(s)
        out.sort(key=lambda s: s.get("created") or "", reverse=True)
        return out

    def session_event(self, sid: str, ev: dict) -> dict:
        ev.setdefault("ts", now_iso())
        self.append_jsonl(self.path("sessions", f"{sid}.events.jsonl"), ev)
        return ev

    def session_events(self, sid: str, limit: int = 80) -> list[dict]:
        return self.read_jsonl(self.path("sessions", f"{sid}.events.jsonl"), limit)

    # ------------------------------------------------------------ dashboard, activity, usage

    def dashboard(self) -> dict:
        d = self.read_json("dashboard.json", {})
        d.setdefault("widgets", [])
        d.setdefault("hidden", [])
        return d

    def save_dashboard(self, widgets: list, by: str) -> dict:
        with self.lock:
            # cards the user closed stay closed when a bot rewrites the widgets
            d = {"widgets": widgets, "updated": now_iso(), "updated_by": by, "hidden": self.dashboard()["hidden"]}
            self.write_json("dashboard.json", d)
        return d

    def hide_dashboard_card(self, key: str, hidden: bool) -> dict:
        """Remember which cards the user closed on the phone ("widget:<id>")."""
        with self.lock:
            d = self.dashboard()
            keys = [k for k in d["hidden"] if k != key]
            if hidden:
                keys.append(key)
            d["hidden"] = keys
            self.write_json("dashboard.json", d)
        return d

    def activity(self, kind: str, text: str, ref: str | None = None, **more) -> dict:
        rec = {"ts": now_iso(), "kind": kind, "text": text[:240], "ref": ref, **more}
        self.append_jsonl("activity.jsonl", rec)
        cb = getattr(self, "on_activity", None)
        if cb:
            try:
                cb(rec)
            except Exception:  # noqa: BLE001 - the phone update must never break a write
                pass
        p = self.path("activity.jsonl")
        try:
            if p.stat().st_size > 2_000_000:
                keep = self.read_jsonl(p, 2000)
                with self.lock:
                    p.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in keep), encoding="utf-8")
        except OSError:
            pass
        return rec

    def event(self, kind: str, *, actor: str, where: str, data: dict | None = None, visible_to: str = "all") -> dict:
        """The append-only event log (who did what, where, and who may see it). It is the raw record that later
        context builders, summaries and the "what this bot saw" view are built from. Large bodies stay in their
        own files; only references and short text go here."""
        rec = {"ts": now_iso(), "kind": kind, "actor": actor, "where": where, "visible_to": visible_to, **(data or {})}
        self.append_jsonl(self.path("events", f"{today()}.jsonl"), rec)
        return rec

    def save_view(self, mid: str, view: dict, keep: int = 300) -> None:
        """What a bot saw on one call (system prompt sections and history), for the phone's inspection screen."""
        d = self.path("views")
        d.mkdir(exist_ok=True)
        self.write_json(d / f"{mid}.json", view)
        files = sorted(d.glob("m-*.json"), key=lambda f: f.stat().st_mtime)
        for f in files[:-keep]:
            try:
                f.unlink()
            except OSError:
                pass

    def view(self, mid: str) -> dict | None:
        if not re.fullmatch(r"m-[0-9a-f]{8}", mid or ""):
            return None
        return self.read_json(self.path("views", f"{mid}.json"), None)

    def recent_activity(self, limit: int = 30) -> list[dict]:
        return list(reversed(self.read_jsonl("activity.jsonl", limit)))

    def record_usage(self, *, input_tokens: int, output_tokens: int, ok: bool, source: str, model: str | None = None,
                     caller: str | None = None) -> dict:
        """Count one model call. usage.json keeps the totals and one small entry per day (kept for good, so weeks and
        months can be drawn); usage/<day>.json adds the hour of the day and the caller (room or session id).
        Every field is optional when read: days recorded before a field existed simply lack it."""
        tin, tout = int(input_tokens or 0), int(output_tokens or 0)
        now = dt.datetime.now()

        def add(bucket: dict) -> dict:
            bucket["calls"] = bucket.get("calls", 0) + 1
            bucket["input_tokens"] = bucket.get("input_tokens", 0) + tin
            bucket["output_tokens"] = bucket.get("output_tokens", 0) + tout
            if not ok:
                bucket["errors"] = bucket.get("errors", 0) + 1
            return bucket

        with self.lock:
            u = self.read_json("usage.json", {})
            for bucket in (u.setdefault("total", {}), u.setdefault("days", {}).setdefault(today(), {})):
                add(bucket)
                src = bucket.setdefault("by_source", {})          # calls only (older readers expect a number)
                src[source] = src.get(source, 0) + 1
                add(bucket.setdefault("sources", {}).setdefault(source, {}))   # calls, tokens and errors per use
                if model:
                    add(bucket.setdefault("by_model", {}).setdefault(model, {"calls": 0, "input_tokens": 0, "output_tokens": 0}))
            u["last_call"] = now_iso()
            self.write_json("usage.json", u)
            name = self.path("usage", f"{today()}.json")
            d = self.read_json(name, {})
            add(d.setdefault("hours", {}).setdefault(f"{now.hour:02d}", {}))
            add(d.setdefault("callers", {}).setdefault(str(caller or ""), {}))
            self.write_json(name, d)
            return u

    def usage(self) -> dict:
        u = self.read_json("usage.json", {})
        u.setdefault("total", {})
        u.setdefault("days", {})
        return u

    def usage_detail_days(self) -> list[str]:
        """The days that have an hour and caller record (usage/<day>.json)."""
        folder = self.path("usage")
        if not folder.is_dir():
            return []
        return sorted(p.stem for p in folder.glob("????-??-??.json"))

    def usage_detail(self, day: str) -> dict:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day or ""):
            return {}
        return self.read_json(self.path("usage", f"{day}.json"), {})
