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
    # None = sized from the model's window and the note limits (budgets()); a number in config.json wins.
    "memory_tokens": None,       # the bot's own notes
    "recent_tokens": None,       # the recent messages together
    "message_tokens": 900,       # one history message at most
    "history_messages": 40,
    "brief_tokens": None,        # BRIEF.md of the projects the bot owns (all of them together)
    "user_tokens": None,         # data/shared/user.md: what every bot knows about the user
    "summary_tokens": None,      # data/rooms/<room>/SUMMARY.md: what happened earlier in the room
}


AUTO_RECALL = {
    "enabled": True,
    "max_items": 3,
    "min_coverage": 0.5,     # share of the message's words found in the record (lexical agreement)
    "min_dense": 0.86,       # cosine of the best chunk when semantic search is on (multilingual-e5-small)
}
# Records the automatic lane may show: what the user and the bots said and kept. Web pages, tool output and worker
# events never come in by themselves (they may carry instructions written by others); recall still finds them.
AUTO_KINDS = ("message", "memory", "summary", "room", "doc")


def auto_recall(app, room: dict, bot: dict, query: str, budget: int, visible_since: str | None) -> tuple[str, list]:
    """Lane 1 of memory: before each reply, look in the archive for what the latest message is about and put at most a
    few trusted, clearly relevant lines into the latest message. No model call. The bot can still call recall for
    more (lane 2). Research basis: small models often do not call the tool on their own, and a short block of
    retrieved text at the end of the prompt helps more than a long history (docs/MAP.md, memory)."""
    cfg = {**AUTO_RECALL, **(((app.cfg.get("recall") or {}).get("auto")) or {})}
    q = " ".join((query or "").split())[:400]
    if not cfg["enabled"] or budget < 80 or len(q) < 4 or not getattr(app, "archive", None):
        return "", []
    from .archive import Scope, snippet, terms_of
    try:
        hits = app.archive.search(q, Scope.for_bot(app, bot), kinds=list(AUTO_KINDS), limit=12, mode="current")
    except Exception:  # noqa: BLE001 - memory help is optional; the reply goes on without it
        return "", []
    try:
        from .hybrid_index import match_terms
        words = match_terms(q)
    except ImportError:
        words = terms_of(q)
    lines, ids, used = [], [], estimate_tokens(AUTO_HEADER)
    for h in hits:
        meta = h.get("meta") or {}
        if meta.get("untrusted") or meta.get("web_material") or meta.get("source_type") == "web_research":
            continue
        if h.get("room") == room.get("id") and (h.get("kind") == "summary" or (
                visible_since and str(h.get("ts") or "") >= visible_since)):
            continue                                   # already in front of the bot (history or summary)
        coverage = h.get("coverage")
        if coverage is None and len(words) >= 2:
            low = (str(h.get("text") or "") + " " + str(h.get("title") or "")).lower()
            coverage = sum(1 for w in words if w.lower() in low) / len(words)
        dense = h.get("dense")
        if not ((coverage or 0) >= float(cfg["min_coverage"]) or (dense is not None and dense >= float(cfg["min_dense"]))):
            continue
        who = meta.get("role") or h.get("kind")
        when = str(h.get("ts") or "")[:10]
        line = f"- [{when} · {app.archive.where(h)} · {who}] {snippet(h.get('text') or '', [w.lower() for w in words], 240)} (record {h['id']})"
        cost = estimate_tokens(line) + 1
        if used + cost > budget:
            break
        lines.append(line)
        ids.append(h["id"])
        used += cost
        if len(lines) >= int(cfg["max_items"]):
            break
    if not lines:
        return "", []
    return AUTO_HEADER + "\n" + "\n".join(lines), ids


AUTO_HEADER = ("# From earlier (SnapLab looked these up for the latest message; they may be outdated or beside the point. "
               "Use them only if they help, and recall(record=...) shows a whole record)")


def budgets(cfg: dict, limit: int, *, memory_chars: int = 4000, user_chars: int = 1400, tool_tokens: int = 0,
            output_tokens: int = 8192) -> dict:
    """Token budgets for one call to a model with this context window.

    The messages may use what the provider check (provider_protocol.prepare) leaves after the reply, its margin and
    the tool list, and at most 60% of the window and config "max_tokens". Each section then gets a share of that,
    but never more than its full content can need: a full 4,000-character Japanese note is about 3,100 tokens, so
    large windows show whole notes and 16K-32K windows keep room for the conversation. A number in config.json
    "context" wins. (Until 2026-11-21 the notes had fixed budgets that showed a quarter of a Japanese note.)"""
    out = dict(cfg)
    limit = int(limit)
    room = limit - min(int(output_tokens), max(1, limit // 4)) - min(1024, limit // 10) - int(tool_tokens)
    cap = max(512, min(int(cfg["max_tokens"]), int(limit * .6), room))
    out["max_tokens"] = cap
    out["target_tokens"] = min(int(cfg["target_tokens"]), cap)
    auto = {"memory_tokens": min(int(memory_chars / 1.3) + 20, int(cap * .12)),
            "user_tokens": min(int(user_chars / 1.3) + 20, int(cap * .05)),
            "summary_tokens": min(2000, int(cap * .06)),
            "brief_tokens": min(1200, int(cap * .05)),
            "recent_tokens": min(6000, int(cap * .3))}
    for k, v in auto.items():
        if cfg.get(k) is None:
            out[k] = max(64, v)
    # Shorten old tool results and update the room summary before the provider would refuse the call.
    out["summarize_tokens"] = max(1024, min(int(cfg["summarize_tokens"]), int(max(room, 1280) * .8)))
    out["reserve_tokens"] = min(2000, max(300, int(cap * .15)))    # the state block added after the history
    out["recall_tokens"] = min(3000, max(0, int(cap * .08)))        # lines found by auto_recall
    return out


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


def clip_newest(text: str, tokens: int, keep_head: int = 0) -> str:
    """Notes grow at the end, so keep the newest lines (and the first keep_head lines) within the budget. The
    lines left out become one marker line, so the bot knows they exist and that recall finds them."""
    if estimate_tokens(text) <= tokens:
        return text
    lines = text.splitlines()
    head, rest = lines[:keep_head], lines[keep_head:]
    marker = "- ({n} older lines are not shown here; recall finds them)"
    used = estimate_tokens("\n".join(head)) + estimate_tokens(marker) + 2
    kept: list[str] = []
    for line in reversed(rest):
        cost = estimate_tokens(line) + 1
        if used + cost > tokens and kept:
            break
        used += cost
        kept.append(line)
    kept.reverse()
    hidden = len(rest) - len(kept)
    out = "\n".join(head + ([marker.format(n=hidden)] if hidden else []) + kept)
    return out if estimate_tokens(out) <= tokens else clip_tokens(out, tokens)


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
        model = self.app.models.spec(bot.get("model"), "chat")
        limit = model["context_limit"]
        mem_limit = int(self.app.cfg.get("memory_chars", 4000))
        from .tools import schema_tokens
        cfg = budgets(self.cfg, limit, memory_chars=mem_limit, user_chars=int(self.app.cfg.get("user_note_chars", 1400)),
                      tool_tokens=schema_tokens(self.app, bot, limit),
                      output_tokens=int((self.app.cfg.get("chat") or {}).get("max_tokens") or 8192))
        settings = st.settings()
        members = [b for b in (st.bot(i) for i in room.get("bots", [])) if b]
        memory = st.memory(bot["id"])
        summary, smeta = self.app.summaries.get(room["id"]) if getattr(self.app, "summaries", None) else ("", {})
        fresh = room.get("fresh_start") or {}
        if fresh and smeta.get("since") != fresh.get("at"):
            summary, smeta = "", {}              # the summary is from before the chat was started afresh
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
                       + clip_newest(memory.strip(), int(cfg["memory_tokens"]))) if memory.strip() else "",
            "user": ("# About the user (shared by all bots; the lead bot keeps it, the user can edit it)\n"
                     + clip_newest(st.user_note().strip(), int(cfg["user_tokens"]), keep_head=1)) if st.user_note().strip() else "",
            "briefs": self.briefs(bot, int(cfg["brief_tokens"])),
            "fresh": ("# This chat was started afresh on " + str(fresh.get("at"))[:16].replace("T", " ") + "\n"
                      "The messages before that are no longer shown to you; they stay in SnapLab's record and recall "
                      "finds them. What still mattered then:\n" + (fresh.get("note") or "(nothing was kept)")) if fresh else "",
            "summary": ("# Earlier in this room (a summary SnapLab keeps; the messages after it follow)\n"
                        + clip_tokens(summary.strip(), int(cfg["summary_tokens"]))) if summary.strip() else "",
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
        recall_budget = int(cfg.get("recall_tokens") or 0)
        budget = min(int(cfg["recent_tokens"]), int(cfg["max_tokens"]) - estimate_tokens(system) - int(cfg["reserve_tokens"])
                     - recall_budget)
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
        latest = next((m for m in reversed(recent) if m.get("role") == "user" and (m.get("text") or "").strip()), None)
        found, found_ids = ("", [])
        if latest and recall_budget:
            since = min((str(m.get("ts") or "") for m in recent if m.get("ts")), default=None)
            found, found_ids = auto_recall(self.app, room, bot, latest.get("text") or "", recall_budget, since)
        merged[-1] = {**merged[-1], "content": state + "\n\n" + (found + "\n\n" if found else "") + merged[-1]["content"]}
        sections["state"] = state
        if found:
            sections["recall"] = found
        messages = [{"role": "system", "content": system}] + merged
        report = {k: estimate_tokens(v) for k, v in sections.items()}
        report["history"] = sum(estimate_tokens(m["content"]) for m in merged)
        report["history_messages"] = len(merged)
        report["total"] = estimate_tokens(system) + report["history"]
        f = self.factor.get(room["id"], 1.0)
        report["total_real"] = int(report["total"] * f)
        report["over_target"] = report["total_real"] > int(cfg["target_tokens"])
        report["compact_at"] = int(cfg["summarize_tokens"])
        report["recalled"] = found_ids
        if report["total_real"] > int(cfg["summarize_tokens"]) and getattr(self.app, "summaries", None):
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
