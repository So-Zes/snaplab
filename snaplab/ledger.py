"""Program-owned projections of canonical state; no LLM writes this ledger."""
from __future__ import annotations

import json
from collections import Counter

from .archive import Scope
from .temporal import digest, instant


def test_evidence(session):
    t = dict(session.get("tests") or {})
    version = session.get("artifact_version")
    matches = bool(version and t.get("tested_version") == version and t.get("version_after") == version
                   and session.get("status") not in ("running", "queued") and t.get("source_type") == "runtime"
                   and isinstance(t.get("exit_code"), int)
                   and (t.get("status") != "passed" or t["exit_code"] == 0))
    t["applies_to_current"] = matches
    t["current_status"] = t.get("status", "not_run") if matches else "unverified"
    return t


# States that are still going on: shown even when they began before a fresh start.
LIVE = {"running", "queued", "waiting", "review", "open", "active", "paused", "pending"}


class StateLedger:
    def __init__(self, app):
        self.app = app

    def record(self, kind, value):
        st = self.app.store
        with st.lock:
            rows = st.read_json("context/state.json", {})
            key = f"{kind}:{value['id']}"
            fields = {
                "project": ("id", "name", "slug", "description", "test_command", "bot", "aliases", "brief_version", "shared_with"),
                "work": ("id", "project", "bot", "title", "status", "goal", "last_prompt", "turns", "plan", "dependencies",
                         "artifact_version", "tests", "error", "started", "ended", "last_message"),
                "approval": ("id", "room", "bot", "status", "action", "authorization", "answer", "answered_at"),
                "task": ("id", "room", "bot", "title", "status", "schedule", "last_run", "next_run", "runs"),
            }[kind]
            body = {k: value[k] for k in fields if k in value}
            if kind == "work":
                body["tests"] = test_evidence(value)
                body["worker_report"] = body.pop("last_message", "")
            version = digest(body)
            prev = rows.get(key) or {}
            if prev.get("version") == version:
                return prev
            tags = {"project": value.get("project"), "bot": value.get("bot"), "room": value.get("room")}
            if kind == "project":
                tags = {"project": value["id"]}
            elif kind == "work":
                tags = {"project": value.get("project"), "session": value["id"]}
            now = instant()
            rec = self.app.archive.add("state", json.dumps(body, ensure_ascii=False), key=key,
                title=value.get("name") or value.get("title") or value["id"], **tags,
                meta={"source_type": "runtime", "source_version": version, "entity": value["id"],
                      "untrusted": kind == "work",
                      "entity_name": value.get("name") or value.get("title") or value["id"],
                      "aliases": value.get("aliases") or [], "status": "verified", "valid_from": now,
                      "supersedes": [prev["source"]] if prev.get("source") else [],
                      "ledger_kind": kind, "sequence": int(prev.get("sequence", 0)) + 1})
            if not rec:
                return prev
            row = {"id": value["id"], "kind": kind, "version": version, "source": rec["id"],
                   "sequence": int(prev.get("sequence", 0)) + 1, "observed_at": now, "value": body,
                   "permissions": tags}
            rows[key] = row
            st.write_json("context/state.json", rows)
            return row

    def sync(self):
        st = self.app.store
        with st.lock:
            groups = {"project": st.projects(), "work": st.sessions(),
                      "approval": [q for q in st.questions() if q.get("kind") == "approval"],
                      "task": st.read_json("tasks.json", [])}
            for kind, values in groups.items():
                for value in values:
                    self.record(kind, value)
            # Retain tombstones when a recurring task is deleted.
            tids = {t["id"] for t in groups["task"]}
            for row in st.read_json("context/state.json", {}).values():
                if row["kind"] == "task" and row["id"] not in tids:
                    self.record("task", {**row["value"], "status": "deleted"})

    def concise(self, bot=None, limit=12, token_budget=1500):
        """P3 entry point: bounded, scoped facts and evidence; never invokes a model."""
        self.sync()
        scope = Scope.for_bot(self.app, bot)
        rows = [r for r in self.app.store.read_json("context/state.json", {}).values()
                if scope.allows(r["permissions"])]
        since = (bot or {}).get("context_since")
        if since:                     # the bot was started afresh: what was already over by then is left out
            since = instant(since)
            rows = [r for r in rows if r["kind"] == "project" or r["value"].get("status") in LIVE
                    or str(r.get("observed_at") or "") >= since]
        priority = {"waiting": 0, "failed": 1, "stopped": 2, "open": 3, "running": 4, "queued": 5}
        rows.sort(key=lambda r: (priority.get(r["value"].get("status"), 6), r["kind"], r["id"]))
        out = []
        for r in rows[:max(0, min(limit, 40))]:
            b = r["value"]
            item = {k: r[k] for k in ("id", "kind", "version", "source", "observed_at")}
            item.update({k: b[k] for k in ("title", "name", "status", "project", "bot", "artifact_version",
                                         "dependencies", "schedule", "next_run") if k in b})
            if r["kind"] == "work":
                item["goal"] = str(b.get("goal") or b.get("last_prompt") or "")[:180]
                item["tests"] = {k: b["tests"].get(k) for k in
                                 ("current_status", "tested_version", "exit_code", "applies_to_current")}
                pl = b.get("plan") or {}
                item["plan"] = {"done": pl.get("done"), "total": pl.get("total"), "now": str(pl.get("now") or "")[:120]}
                item["worker_report_is_evidence"] = False
            if r["kind"] == "approval":
                q = self.app.store.question(r["id"]) or {}
                item["authorization"] = self.approval_state(q)
            out.append(item)
        from .context import estimate_tokens
        while out and estimate_tokens(json.dumps(out, ensure_ascii=False)) > token_budget:
            out.pop()
        return {"items": out, "counts": dict(Counter(r["kind"] for r in rows)), "omitted": len(rows)-len(out)}

    def binding(self, action):
        st = self.app.store
        target = action.get("session") or action.get("project")
        value = st.session(target) if action.get("session") else st.project(target or "")
        if action.get("type") == "write_memory":
            shared = action.get("scope") == "user"
            target = "memory:user" if shared else "memory:" + str(action.get("bot"))
            value = st.user_note() if shared else st.memory(action.get("bot") or "")
        elif action.get("type") == "update_dashboard":
            target, value = "dashboard", st.dashboard()
        elif action.get("session") and value:
            value = {k: value.get(k) for k in ("id", "project", "status", "turns", "artifact_version", "last_prompt")}
        elif value:
            value = {k: value.get(k) for k in ("id", "bot", "description", "test_command", "brief_version")}
        return {"action_hash": digest(action), "target": target, "target_version": digest(value) if value is not None else None}

    def bind_approval(self, q):
        q["authorization"] = {**self.binding(q.get("action") or {}), "state": "pending", "created_at": instant()}

    def approval_state(self, q):
        a = dict(q.get("authorization") or {})
        if not a:
            return {"state": "legacy_unbound", "valid": False}
        valid = a.get("state") == "pending" and all(a.get(k) == v for k, v in self.binding(q.get("action") or {}).items())
        room = self.app.store.room(q.get("room"))
        valid = bool(valid and q.get("status") in ("open", "answered") and room
                     and q.get("bot") in room.get("bots", []))
        if a.get("expires_at") and instant(a["expires_at"]) <= instant():
            valid = False
        # An open proposal is not permission to act. Only the answer path can consume it.
        a.update(valid=False, can_accept=valid, state=a.get("state") if valid else
                 (a.get("state") if a.get("state") != "pending" else "stale"))
        return a

    def consume_approval(self, q, approved):
        st = self.app.store
        with st.lock:
            saved = st.question(q.get("id"))
            if not saved or saved.get("action") != q.get("action"):
                raise ValueError("approval does not match the stored proposal")
            from .chat import is_approval
            room = st.room(saved.get("room"))
            if (saved.get("status") != "answered" or not room or saved.get("bot") not in room.get("bots", [])
                    or (approved and not is_approval(saved.get("answer") or ""))):
                raise ValueError("approval has no valid answer in an active room")
            state = self.approval_state(saved)
            if approved and not state.get("can_accept"):
                raise ValueError("approval is stale or already used; create a new proposal")
            action = saved.get("action") or {}
            bot = st.bot(saved.get("bot") or "") or {}
            target = st.session(action.get("session")) if action.get("session") else None
            pid = (target or {}).get("project") or action.get("project")
            if approved and pid:
                from .access import check_project
                check_project(self.app, bot, pid, manage=True)
            saved.setdefault("authorization", {})["state"] = "consumed" if approved else "declined"
            saved["authorization"]["consumed_at"] = instant()
            st.save_question(saved)
