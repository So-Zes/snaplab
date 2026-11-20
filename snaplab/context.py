"""Context building: what a bot sees on each model call. Assembled by code from files, never kept as one long
model conversation. This follows the research recommendation for SnapLab:

- Files are the source of truth (bot prompt, memory, projects, room history); each call builds a small "view".
- The event log (store.event) is written from day one, so later builders and the "what this bot saw" screen can
  be made from it.
- Rooms are assembled by code: other bots' messages are rewritten as "[Reference] Name said: ..." and their tool
  details are left out, so a bot does not mistake another bot's words or work for its own.

Budgets are estimates (see `estimate_tokens`) and come from config "context": target 16K and hard limit 32K
tokens per call are the research proposal and are NOT measured yet; change them in config.json.

To replace the builder, write a class with the same `build(room, bot, lang) -> (messages, report)` method and
set "context_builder": "module:Class" in config.json.
"""
from __future__ import annotations

import importlib
from pathlib import Path
import json

from . import mcp_client
from . import prompts

DEFAULTS = {
    "target_tokens": 16000,      # measured: first token ~2.6 s for a new 16K context
    "summarize_tokens": 24000,   # above this the room summary is updated and old tool results are shortened
    "max_tokens": 32000,         # chats and rooms (~6 s for a new 32K context)
    "session_max_tokens": 64000, # background sessions (~12 s for a new 64K context; nobody waits on the screen)
    "memory_tokens": 800,
    "recent_tokens": 6000,
    "message_tokens": 900,       # one history message at most
    "history_messages": 40,
    "brief_tokens": 1200,        # BRIEF.md of the projects the bot owns (all of them together)
    "user_tokens": 400,          # data/shared/user.md: what every bot knows about the user
    "summary_tokens": 1000,      # data/rooms/<room>/SUMMARY.md: what happened earlier in the room
}


def later_than(ts, since: str) -> bool:
    """True when the time stamp ts comes after since (both ISO 8601, any offset, any precision)."""
    import datetime as dt
    try:
        return dt.datetime.fromisoformat(str(ts)) > dt.datetime.fromisoformat(since)
    except (TypeError, ValueError):
        return False


def estimate_tokens(text: str) -> int:
    """Rough token estimate without a tokenizer: about 4 ASCII characters or 1.3 other characters per token."""
    if not text:
        return 0
    ascii_n = sum(1 for ch in text if ord(ch) < 128)
    return int(ascii_n / 4 + (len(text) - ascii_n) / 1.3) + 1


def clip_tokens(text: str, tokens: int) -> str:
    if estimate_tokens(text) <= tokens:
        return text
    lo, hi = 0, len(text)
    while lo < hi:                 # longest prefix within the budget
        mid = (lo + hi + 1) // 2
        if estimate_tokens(text[:mid]) <= tokens:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo].rstrip() + " …"


class ContextBuilder:
    def __init__(self, app):
        self.app = app
        self.cfg = {**DEFAULTS, **(app.cfg.get("context") or {})}
        self.factor: dict[str, float] = {}     # room -> real/estimated tokens (learned from the server's counts)

    def briefs(self, bot: dict, budget: int) -> str:
        """BRIEF.md of each project this bot owns (teammates only; the orchestrator sees the project list)."""
        if budget <= 0:
            return ""
        st, parts = self.app.store, []
        from .access import project_ids
        pids = project_ids(self.app, bot)
        if not pids:
            return ""
        each = max(200, budget // max(1, len(pids)))
        for pid in pids[:6]:
            p = st.project(pid)
            f = Path(self.app.cfg["projects_dir"]) / p["slug"] / "BRIEF.md"
            try:
                text = f.read_text(encoding="utf-8-sig").strip()
            except OSError:
                continue
            parts.append(f"## {p['name']} (id {p['id']}) - BRIEF.md\n" + clip_tokens(text, each))
        if not parts:
            return ""
        return "# Briefs of projects you may read (shared projects are read-only)\n\n" + "\n\n".join(parts)

    def learn(self, room_id: str, estimate: int, real: int) -> None:
        """Correct the token estimate with what the server counted (usage.prompt_tokens of the first call).
        Measured: the estimate was ~20% low for short Japanese contexts and ~10% high for long English ones."""
        if estimate > 200 and real > 0:
            ratio = max(0.6, min(1.6, real / estimate))
            self.factor[room_id] = round(0.5 * self.factor.get(room_id, ratio) + 0.5 * ratio, 3)

    def build(self, room: dict, bot: dict, lang: str) -> tuple[list[dict], dict]:
        st = self.app.store
        cfg = dict(self.cfg)
        model = self.app.models.spec(bot.get("model"), "chat")
        limit = model["context_limit"]
        cfg["max_tokens"] = min(int(cfg["max_tokens"]), max(512, int(limit * .6)))
        cfg["target_tokens"] = min(int(cfg["target_tokens"]), cfg["max_tokens"])
        settings = st.settings()
        members = [b for b in (st.bot(i) for i in room.get("bots", [])) if b]
        memory = st.memory(bot["id"])
        summary, smeta = self.app.summaries.get(room["id"]) if getattr(self.app, "summaries", None) else ("", {})
        fresh = room.get("fresh_start") or {}
        if fresh and smeta.get("since") != fresh.get("at"):
            summary, smeta = "", {}              # the summary is from before the chat was started afresh
        mem_limit = int(self.app.cfg.get("memory_chars", 4000))
        chat_mcp = {n: t for n, t in (getattr(self.app, "plugin_tools", None) or {}).items() if "chat" in t.scope}
        sections = {
            "persona": (bot.get("system_prompt") or "").strip(),
            "rules": prompts.house_rules(lang, self.app.cfg) + ("\n" + prompts.ROOM_RULES if room.get("kind") == "group" else ""),
            "tools": "\n\n".join(x for x in (prompts.tools_hint(lang, self.app.cfg),
                                              mcp_client.prompt_block(chat_mcp),
                                              mcp_client.chat_note(getattr(self.app, "plugin_tools", None) or {})) if x),
            "room": prompts.room_block(lang=lang, call_user=bot.get("call_user") or settings.get("user_name", ""),
                                       bot=bot, room=room, members=members, model=model),
            "memory": ("# Your memory (" + f"{len(memory)}/{mem_limit} characters used" + ")\n"
                       + clip_tokens(memory.strip(), cfg["memory_tokens"])) if memory.strip() else "",
            "user": ("# About the user (shared by all bots; the lead bot keeps it, the user can edit it)\n"
                     + clip_tokens(st.user_note().strip(), int(cfg.get("user_tokens", 400)))) if st.user_note().strip() else "",
            "briefs": self.briefs(bot, int(cfg.get("brief_tokens", 1200))),
            "fresh": ("# This chat was started afresh on " + str(fresh.get("at"))[:16].replace("T", " ") + "\n"
                      "The messages before that are no longer shown to you; they stay in SnapLab's record and recall "
                      "finds them. What still mattered then:\n" + (fresh.get("note") or "(nothing was kept)")) if fresh else "",
            "summary": ("# Earlier in this room (a summary SnapLab keeps; the messages after it follow)\n"
                        + clip_tokens(summary.strip(), int(cfg.get("summary_tokens", 1000)))) if summary.strip() else "",
        }
        system = "\n\n".join(v for v in sections.values() if v)
        names = {b["id"]: b["name"] for b in st.bots()}
        hist = []
        recent = st.messages(room["id"], int(cfg["history_messages"]))
        if fresh.get("through"):
            ids = [m.get("id") for m in recent]
            if fresh["through"] in ids:
                recent = recent[ids.index(fresh["through"]) + 1:]
        if summary.strip() and smeta.get("through"):
            ids = [m.get("id") for m in recent]
            if smeta["through"] in ids:          # the summary covers everything up to here; keep a few for flow
                cut = ids.index(smeta["through"]) + 1
                min_recent = int((self.app.cfg.get("summary") or {}).get("min_recent", 6))
                recent = recent[min(cut, max(0, len(recent) - min_recent)):]
        for m in recent:
            if m.get("status") in ("silent", "streaming") or m.get("retried"):
                continue                    # hidden, still being written elsewhere, or replaced by a retry
            text = (m.get("text") or "").strip()
            if m["role"] == "user" and m.get("answer_to"):
                text = prompts.answer_text(m["answer_to"], text, lang)
            if m["role"] == "user" and m.get("attachments"):     # files the user sent; chat.py expands them (uploads.py)
                from .uploads import lines
                text = (lines(m) + "\n" + text).strip()
            mine = m["role"] == "bot" and m.get("bot") == bot["id"]
            # What the bot did besides writing (tools, cards, a cut-off reply) follows its reply as a SnapLab line on
            # the user side, so the bot never sees such notes inside its own words and does not copy them.
            record = prompts.turn_record(m) if mine else ""
            if text:
                text = clip_tokens(text, cfg["message_tokens"])
                if m["role"] == "user":
                    hist.append({"role": "user", "content": text})
                elif mine:
                    hist.append({"role": "assistant", "content": text})
                elif m["role"] == "bot":
                    hist.append({"role": "user", "content": f"[Reference] {names.get(m.get('bot'), 'Another bot')} said: {text}"})
                else:
                    hist.append({"role": "user", "content": f"[SnapLab note] {text}"})
            if record:
                hist.append({"role": "user", "content": record, "record": True})
        budget = min(int(cfg["recent_tokens"]), int(cfg["max_tokens"]) - estimate_tokens(system) - 2000)
        total, kept = 0, []
        for h in reversed(hist):
            total += estimate_tokens(h["content"])
            if total > budget and kept:
                break
            kept.append(h)
        kept.reverse()
        while kept and (kept[0]["role"] == "assistant" or kept[0].get("record")):
            kept.pop(0)                         # a record whose reply was cut away would point at nothing
        if not kept or kept[-1]["role"] != "user" or kept[-1].get("record"):
            kept.append({"role": "user", "content": "[SnapLab note] Continue."})
        merged: list[dict] = []
        for h in kept:
            if merged and merged[-1]["role"] == h["role"] == "user":
                merged[-1]["content"] += "\n\n" + h["content"]
            else:
                merged.append({"role": h["role"], "content": h["content"]})
        # what changes every minute goes into the latest message, so the long prefix stays the same between calls
        names = {p["id"]: p["name"] for p in st.projects()}
        names.update({b_["id"]: b_["name"] for b_ in st.bots()})
        projects, sessions = st.projects(), st.sessions()[:30]
        since = bot.get("context_since")
        if since:                                # finished work from before the bot's fresh start is left out
            sessions = [s for s in sessions if s.get("status") in ("running", "queued", "waiting")
                        or later_than(s.get("updated"), since)]
        if not bot.get("orchestrator"):          # a teammate sees the project it looks after (round 6)
            from .access import project_ids
            mine = set(project_ids(self.app, bot))
            projects = [p for p in projects if p["id"] in mine]
            sessions = [s for s in sessions if s.get("project") in mine]
        state = prompts.state_block(bot=bot, projects=projects, sessions=sessions, names=names,
                                    widgets=st.dashboard().get("widgets") or [], lang=lang)
        if getattr(self.app, "ledger", None):
            state += ("\n# Program-owned current state (observations; worker reports do not prove completion)\n"
                      + json.dumps(self.app.ledger.concise(bot, limit=8), ensure_ascii=False))
        merged[-1] = {**merged[-1], "content": state + "\n\n" + merged[-1]["content"]}
        sections["state"] = state
        messages = [{"role": "system", "content": system}] + merged
        report = {k: estimate_tokens(v) for k, v in sections.items()}
        report["history"] = sum(estimate_tokens(m["content"]) for m in merged)
        report["history_messages"] = len(merged)
        report["total"] = estimate_tokens(system) + report["history"]
        f = self.factor.get(room["id"], 1.0)
        report["total_real"] = int(report["total"] * f)
        report["over_target"] = report["total_real"] > int(cfg["target_tokens"])
        if report["total_real"] > int(cfg.get("summarize_tokens", 24000)) and getattr(self.app, "summaries", None):
            if self.app.summaries.pending(room["id"]):
                self.app.summaries.maybe_schedule(room["id"], force=True)
        report["target_tokens"] = int(cfg["target_tokens"])
        report["max_tokens"] = int(cfg["max_tokens"])
        # the full view for the "What this bot saw" screen (stored apart from the message)
        report["sections"] = [{"name": k, "tokens": estimate_tokens(v), "text": v} for k, v in sections.items() if v]
        report["history_view"] = [{"role": m["role"], "tokens": estimate_tokens(m["content"]), "text": m["content"]}
                                  for m in merged]
        return messages, report


def load(app):
    spec = app.cfg.get("context_builder")
    if spec:
        mod, _, name = str(spec).partition(":")
        try:
            return getattr(importlib.import_module(mod), name)(app)
        except Exception as e:  # noqa: BLE001
            app.log(f"could not load context builder {spec}: {e!r}; using the default")
    return ContextBuilder(app)
