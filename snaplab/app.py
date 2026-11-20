"""The SnapLab application object: wires settings, storage, the model client, tools, chat, sessions,
notifications and the Codex translator together. The web server (server.py) is a thin layer on top.
"""
from __future__ import annotations

import json

import datetime as dt
import secrets
import threading
from pathlib import Path

from . import VERSION, auth, i18n, prompts
from . import dashboard as dashmod
from . import avatars
from .bus import Bus
from .guard import Guard
from .chat import ChatManager
from . import gitops
from .llm import Usage
from .models import ModelRegistry
from .privacy import Redactor
from .proxy import Translator
from .push import Push
from .sessions import SessionManager
from .slots import Slots
from .store import Store, new_id, now_iso, slugify
from .websearch import Searcher, read_page

PALETTE = ["#E5162B", "#2F6FED", "#12A37F", "#8B5CF6", "#F59E0B", "#0EA5E9", "#EC4899", "#64748B"]
NAME_LIMIT = 24


def initials_of(name: str) -> str:
    parts = [p for p in (name or "").replace("_", " ").replace("-", " ").split() if p]
    if not parts:
        return "?"
    if len(parts) == 1:
        w = parts[0]
        return w[:1].upper()
    return (parts[0][:1] + parts[1][:1]).upper()


def clean_name(name: str) -> str:
    name = " ".join((name or "").replace("@", "").split())[:NAME_LIMIT]
    if not name:
        raise ValueError("a name is required")
    return name


def clean_color(color: str, fallback: str) -> str:
    c = (color or "").strip()
    if len(c) == 7 and c.startswith("#") and all(ch in "0123456789abcdefABCDEF" for ch in c[1:]):
        return c.upper()
    return fallback


def brief_text(name: str, desc: str, tests: str, lang: str = "en", *, ssh: bool = False) -> str:
    """BRIEF.md: the project's goal and rules, in the app language. Agents read it; the user (or the lead bot) edits it."""
    t = lambda k, **v: i18n.text(lang, k, **v)  # noqa: E731
    done = "- " + (t("srv.brief.tests_pass", cmd=tests) if tests else t("srv.brief.user_confirms"))
    lines = [f"# {name}", "", "## " + t("srv.brief.goal"), desc or t("srv.brief.goal_ph"), "", "## " + t("srv.brief.done"),
             done, "", "## " + t("srv.brief.rules"), "- " + t("srv.brief.rule1"), "- " + t("srv.brief.rule2_ssh" if ssh else "srv.brief.rule2"),
             # The standard-library rule only follows from having no network; the Linux worker can install packages.
             *([] if ssh else ["- " + t("srv.brief.rule3")]), "- " + t("srv.brief.rule4"), ""]
    return "\n".join(lines)


def agents_text(name: str) -> str:
    lines = [f"# {name}", "", "Read BRIEF.md first (goal, done-when, rules). SnapLab keeps the current state in",
             ".snaplab/STATE.md inside each session folder. Do not run git commit; SnapLab records changes.",
             "The files are UTF-8. In Windows PowerShell read them with: Get-Content -Encoding UTF8 BRIEF.md", ""]
    return "\n".join(lines)


def bot_cap(cfg: dict) -> int | None:
    """The configured most bots, or None when there is no limit (the default)."""
    v = cfg.get("max_bots")
    return int(v) if v else None


class App:
    def __init__(self, cfg: dict, *, llm=None, searcher=None, page_reader=None, push_sender=None,
                 start_translator: bool = True, owner: str | None = None):
        self.cfg = cfg
        self.version = VERSION
        self.store = Store(cfg["data_dir"])
        self.guard = Guard(extra_env=list((cfg.get("guard") or {}).get("secret_env") or []))
        self.store.guard = self.guard
        self.bus = Bus()
        self.slots = Slots(cfg["slots"]["dir"], cfg["slots"]["count"], cfg["slots"].get("session_max"),
                           cfg["slots"].get("background_max") or 1)
        self.redactor = Redactor(extra_terms=list(cfg.get("private_terms") or []), guard=self.guard)
        self.usage = Usage(on_record=self._on_usage)
        from .qwen_log import QwenLog
        self.qwen_log = QwenLog(cfg["data_dir"], self.guard, (cfg.get("qwen_log") or {}).get("enabled", True))
        self.models = ModelRegistry(cfg, self.slots, self.redactor, self.usage, override=llm)
        self.guard.extra_env = sorted(set(self.guard.extra_env) | set(self.models.key_envs()) | {"BRAVE_API_KEY", "TAVILY_API_KEY"})
        self.searcher = searcher or Searcher(cfg.get("search") or {}, self.redactor)
        self._page_reader = page_reader
        self.push = Push(self.store, (cfg.get("push") or {}).get("subject") or "mailto:snaplab@example.com",
                         sender=push_sender, guard=self.guard)
        self.auth = auth.AuthStore(self.store.path("auth.json"))
        self.owner = auth.Owner(owner or cfg.get("owner_login"), cfg.get("tailscale_exe") or "tailscale")
        self.public_url = (cfg.get("public_url") or "").rstrip("/")
        self.proxy_token = secrets.token_urlsafe(32)
        from .web_mcp import WebMCP
        self.web_mcp = WebMCP(self)
        from .phone import Phone
        self.phone = Phone(self)
        self.translator = None
        self.health: dict = {"model_ok": None, "model_error": None}
        self._status_stop = threading.Event()
        self._log_lock = threading.Lock()
        self.log_path = self.store.path("logs", "server.log")
        from . import plugins as plugmod
        from .tools import builtin_tool_names
        self.plugin_tools = plugmod.load(cfg, builtin_tool_names(), log=self.log)
        from . import mcp_client
        self.plugin_tools.update(mcp_client.load(cfg, builtin_tool_names() | set(self.plugin_tools), log=self.log))
        self.store.on_activity = lambda rec: self.bus.publish("activity.new", {"activity": rec})
        from .archive import Archive
        self.archive = Archive(self)
        self.store.on_message = self.archive.on_message
        self.chat = ChatManager(self)
        from .background import Housekeeper, MemoryTidy, RoomSummaries
        self.summaries = RoomSummaries(self)
        self.tidy = MemoryTidy(self)
        self.housekeeper = Housekeeper(self)
        self.sessions = SessionManager(self)
        from .attachments import Attachments
        self.attachments = Attachments(self)
        from .tasks import Scheduler
        self.tasks = Scheduler(self)
        from .ledger import StateLedger
        from .facts import MemoryFacts
        self.ledger = StateLedger(self)
        self.facts = MemoryFacts(self)
        self.store.on_state = self.ledger.record
        from . import migrate
        self.migration_notes = migrate.run(self)
        from . import deploy
        if not deploy.held(self):        # during a deploy check this waits until the check passes (deploy.py)
            self.recover(resume=bool(deploy.read(self).get("resume")))
            deploy.clear(self)
        Path(cfg["projects_dir"]).mkdir(parents=True, exist_ok=True)
        if start_translator:
            self.start_translator()

    def recover(self, resume: bool = False) -> None:
        """After a restart: mark the replies and sessions it cut off. resume (a restart by an update): also
        write those replies again and tell those sessions to go on, so a deploy needs nothing stopped first."""
        self.sessions.recover(resume)
        self.chat.recover(resume)

    # ------------------------------------------------------------ plumbing

    def log(self, line: str) -> None:
        line = self.redact_log(line)
        with self._log_lock:
            try:
                if self.log_path.exists() and self.log_path.stat().st_size > 5_000_000:
                    self.log_path.replace(self.log_path.with_suffix(".old.log"))
                with open(self.log_path, "a", encoding="utf-8") as f:
                    f.write(f"{dt.datetime.now():%Y-%m-%d %H:%M:%S} {line}\n")
            except OSError:
                pass

    def redact_log(self, text: str) -> str:
        import re
        text = re.sub(r"/pair/[A-Za-z0-9_-]+", "/pair/***", text)
        return self.guard.clean(text)

    def _on_usage(self, **kw) -> None:
        self.store.record_usage(**kw)
        if kw.get("ok"):                     # failures are noted with their reason by note_model_error()
            self.health["model_ok"] = now_iso()
            self.health["model_error"] = None
        self.bus.publish("usage.updated", {"usage": self.usage_view()})

    def note_model_error(self, code: str) -> None:
        """Remember the last model error for the connection band (F18). Stopping is not an error."""
        if code and code not in ("restart",):
            self.health["model_error"] = {"code": code, "ts": now_iso()}

    def background_view(self) -> dict:
        from .background import SUMMARY_DEFAULTS, TIDY_DEFAULTS
        s = {**SUMMARY_DEFAULTS, **(self.cfg.get("summary") or {})}
        m = {**TIDY_DEFAULTS, **(self.cfg.get("memory_tidy") or {})}
        return {"summary": {k: s[k] for k in ("enabled", "every_messages", "recent_tokens")},
                "memory_tidy": {k: m[k] for k in ("enabled", "hour", "every_days")},
                "last_tidy": self.tidy.state().get("last_run")}

    def status(self) -> dict:
        """The real state of the parts the phone depends on, for the connection band (F18).
        level: ok | warn | bad. Nothing here calls the model: the model state comes from the last real call."""
        items = []
        key_set = self.models.has_key(None, "chat")
        sp = self.models.spec(None, "chat")
        err = self.health.get("model_error")
        if not key_set:
            items.append({"id": "model", "state": "bad", "code": "nokey", "label": sp["label"]})
        elif err and err.get("code") in ("key", "unreachable", "config", "nokey", "denied"):
            items.append({"id": "model", "state": "bad", "code": err["code"], "since": err["ts"], "label": sp["label"]})
        elif err:
            items.append({"id": "model", "state": "warn", "code": err["code"], "since": err["ts"], "label": sp["label"]})
        else:
            items.append({"id": "model", "state": "ok", "code": "ok" if self.health.get("model_ok") else "idle",
                          "since": self.health.get("model_ok"), "label": sp["label"]})
        waiting, in_use = self.slots.waiting, self.slots.in_use()
        full = in_use >= self.slots.count
        items.append({"id": "slots", "state": "warn" if (waiting or full) else "ok", "code": "full" if full else None,
                      "in_use": in_use, "count": self.slots.count, "waiting": waiting})
        if "codex" in getattr(self.sessions, "harnesses", {}) and self.sessions.harnesses["codex"].available()[0]:
            if self.translator is None:
                items.append({"id": "translator", "state": "warn", "code": "off"})
            elif self.translator.server is None:
                items.append({"id": "translator", "state": "bad", "code": "error"})
            else:
                items.append({"id": "translator", "state": "ok", "code": "ok"})
        paused = {k: v for k, v in (getattr(self.searcher, "paused_until", None) or {}).items() if v > __import__("time").time()}
        items.append({"id": "search", "state": "warn" if paused else "ok", "code": "paused" if paused else "ok",
                      "minutes": int((max(paused.values()) - __import__("time").time()) / 60) + 1 if paused else 0})
        running = sum(1 for s in self.store.sessions() if s.get("status") == "running")
        items.append({"id": "sessions", "state": "ok", "running": running})
        level = "bad" if any(i["state"] == "bad" for i in items) else "warn" if any(i["state"] == "warn" for i in items) else "ok"
        return {"level": level, "items": items, "checked": now_iso()}

    def start_status_watch(self, every_s: float = 3.0) -> None:
        """Publish status.updated whenever the state changes (checked every few seconds; F18 wants < 10 s)."""
        def loop():
            last = None
            while not self._status_stop.wait(every_s):
                try:
                    st = self.status()
                    key = [(i["id"], i["state"], i.get("code"), i.get("waiting"), i.get("in_use"), i.get("running"))
                           for i in st["items"]]
                    if key != last:
                        last = key
                        self.bus.publish("status.updated", {"status": st})
                except Exception as e:  # noqa: BLE001
                    self.log(f"status watch: {e!r}")
        threading.Thread(target=loop, daemon=True, name="status-watch").start()

    def start_translator(self) -> None:
        self.translator = Translator(port=self.cfg["proxy"]["port"], api_cfg=self.cfg["api"], slots=self.slots,
                                     key_fn=self.llm.key if hasattr(self.llm, "key") else (lambda: None),
                                     resolve=self._translator_target,
                                     token=self.proxy_token, log_path=self.store.path("logs", "proxy.log"),
                                     redactor=self.redactor, on_usage=self._on_usage, web_mcp=self.web_mcp,
                                     qwen_log=self.qwen_log,
                                     output_limit=int(self.cfg["api"].get("work_max_tokens", 20000)))
        if not self.translator.start():
            self.log(f"translator did not start: {self.translator.error}")
        elif self.cfg.get("local_mode"):
            self.cfg["proxy"]["port"] = self.translator.port

    @property
    def llm(self):
        """The default model's client (routing "default")."""
        return self.models.get(None, "chat")

    def _translator_target(self, model_ref: str | None) -> dict:
        name = self.models.find(model_ref)
        sp = self.models.spec(name)
        from .llm import get_api_key
        return {**sp, "key": get_api_key(sp["api_key_env"]), "slots": self.models.slots_for(name), "error_language": self.lang}

    def proxy_ready(self) -> bool:
        return bool(self.translator and self.translator.server)

    def read_page(self, url: str) -> dict:
        self.guard.reject_web_args({"url": url})
        if self._page_reader:
            return self._page_reader(url)
        return read_page(url, redactor=self.redactor)

    @property
    def lang(self) -> str:
        return self.store.settings().get("lang") or "en"

    def t(self, key: str, **vars) -> str:
        """A string from the language files in the app's language (English when missing)."""
        return i18n.text(self.lang, key, **vars)

    def who(self, by: str) -> str:
        """For string values: "@srv.you" is shown as You / あなた / ... in whatever language reads it."""
        return "@srv.you" if by in ("you", "user", "") else by

    def memory_write(self, bot_id: str, text: str, mode: str = "append", scope: str = "self", by: str | None = None,
                     source_type: str = "bot", sources: list | None = None) -> str:
        """Write a bot's memory or the shared notes about the user (see memory.py). Raises MemoryFull."""
        from . import memory as mem
        bot = self.store.bot(bot_id) or {"id": bot_id, "name": by or "Bot"}
        user = scope == "user"
        limit = int(self.cfg.get("user_note_chars" if user else "memory_chars", 1400 if user else 4000))
        cur = self.store.user_note() if user else self.store.memory(bot_id)
        if mode == "forget":
            new, gone = mem.forget(cur, text)
            if not gone:
                raise ValueError("no note contains that text; nothing was removed")
        elif mode == "replace":
            new = text.strip() + "\n" if text.strip() else ""
        else:
            new = (cur.rstrip() + "\n" if cur.strip() else "") + mem.stamp_line(text) + "\n"
        if len(new) > limit:
            raise mem.MemoryFull(f"{'The notes about the user are' if user else 'Memory is'} full ({len(cur)}/{limit} characters). "
                                 "Nothing was saved. Rewrite them shorter with mode=\"replace\" (merge or drop old notes), "
                                 "then add the new note.")
        if user:
            self.store.save_note_file(("shared", "user.md"), new)
        else:
            self.store.write_memory(bot_id, new, "replace", limit=limit)
        self.store.memory_log(bot_id if not user else "_user", f"{bot['name']} {mode}{' (about the user)' if user else ''}: {' '.join(text.split())[:400]}")
        self.archive_memory(None if user else bot_id, source_type=source_type, sources=sources)
        self.store.event("memory.write", actor=bot_id, where="user" if user else bot_id, visible_to=None if user else bot_id,
                         data={"mode": mode, "chars": len(text), "scope": scope})
        key = "act.user_note" if user else ("act.memory_forgot" if mode == "forget" else "act.memory")
        self.act("memory", key, ref=bot_id, bot=bot["name"])
        return new

    def archive_memory(self, bot_id: str | None, *, source_type: str = "user", sources: list | None = None) -> None:
        """Every version of a memory goes to the raw archive, so a deleted note can still be found (round 6)."""
        if bot_id:
            b = self.store.bot(bot_id) or {"name": "bot"}
            self.archive.add("memory", self.store.memory(bot_id) or "(empty)", key=f"memory:{bot_id}", bot=bot_id,
                             title=b["name"], ref=f"#/bot/{bot_id}",
                             meta={"source_type": source_type, "sources": sources or [], "method": "markdown_snapshot",
                                   "source_file": f"bots/{bot_id}/memory.md", "status": "unverified"})
        else:
            self.archive.add("memory", self.store.user_note() or "(empty)", key="memory:user", shared=True,
                             title="about the user", ref="#/settings",
                             meta={"source_type": source_type, "sources": sources or [], "method": "markdown_snapshot",
                                   "source_file": "shared/user.md", "status": "unverified"})

    def act(self, kind: str, key: str, ref: str | None = None, extra: dict | None = None, **vars) -> dict:
        """An activity line: the text in the app's language plus the key and values, so the phone can show it in
        the language it uses now."""
        vars = {k: (v if isinstance(v, str) else str(v)) for k, v in vars.items()}
        return self.store.activity(kind, self.t(key, **vars), ref=ref, key=key, vars=vars, **(extra or {}))

    def learn_private_terms(self, *terms: str) -> None:
        for t in terms:
            if t:
                self.redactor.add_term(t)

    # ------------------------------------------------------------ onboarding and bots

    def preview_prompt(self, *, lang: str, name: str, tone: str, tone_custom: str, call_user: str) -> str:
        return prompts.persona_prompt(name=clean_name(name), tone=tone, tone_custom=tone_custom, call_user=call_user,
                                      lang=i18n.valid(lang), orchestrator=True)

    def onboard(self, *, lang: str, name: str, color: str, tone: str, tone_custom: str, call_user: str,
                system_prompt: str | None = None, avatar: str | None = None) -> dict:
        lang = i18n.valid(lang)
        name = clean_name(name)
        call_user = " ".join((call_user or "").split())[:32]
        if not call_user:
            raise ValueError("tell the bot what to call you")
        with self.store.lock:
            if self.store.settings().get("onboarded"):
                raise ValueError("SnapLab is already set up")
            prompt = (system_prompt or "").strip() or self.preview_prompt(lang=lang, name=name, tone=tone,
                                                                          tone_custom=tone_custom, call_user=call_user)
            bot = {"id": new_id("b"), "name": name, "color": clean_color(color, PALETTE[0]), "initials": initials_of(name),
                   "tone": tone if tone in (*prompts.TONES, "custom") else "friendly", "tone_custom": tone_custom[:200],
                   "call_user": call_user, "role": "orchestrator", "orchestrator": True, "projects": [],
                   "system_prompt": prompt[:8000],
                   "avatar": avatars.validate_choice(avatar if avatar is not None else avatars.default_face(lead=True), self.store)}
            self.store.save_bot(bot)
            room = self.store.ensure_dm(bot)
            self.store.add_message(room["id"], {"role": "bot", "bot": bot["id"], "status": "done", "welcome": True,
                                                "text": prompts.welcome_message(name=name, call_user=call_user, lang=lang)})
            self.store.update_settings(lang=lang, onboarded=True, user_name=call_user, onboarded_at=now_iso())
            from . import migrate
            self.store.write_json("schema.json", {"version": migrate.SCHEMA, "updated": now_iso()})
            if not self.store.dashboard()["widgets"]:
                self.store.save_dashboard([{
                    "id": "welcome", "type": "note", "size": "full",
                    "title": i18n.text(lang, "srv.board.title"), "text": i18n.text(lang, "srv.board.text")}], by="SnapLab")
            self.act("setup", "act.setup", ref=bot["id"], name=name)
        if call_user and not self.store.user_note().strip():
            self.store.save_note_file(("shared", "user.md"), i18n.text(lang, "srv.user.call", call=call_user) + "\n")
        self.bus.publish("state.reload", {})
        return {"bot": bot, "room": room}

    def create_bot(self, *, name: str, role: str = "", color: str = "", tone: str = "", tone_custom: str = "",
                   call_user: str = "", projects: list | None = None, system_prompt: str = "", by: str = "you",
                   avatar: str | None = None) -> dict:
        name = clean_name(name)
        if self.store.bot_by_name(name):
            raise ValueError(f"a bot named {name!r} already exists")
        cap = bot_cap(self.cfg)
        if cap is not None and len(self.store.bots()) >= cap:
            raise ValueError(f"SnapLab allows {cap} bots; remove one or give the work to an existing bot")
        settings = self.store.settings()
        lang = settings.get("lang") or "en"
        orch = self.store.orchestrator() or {}
        call_user = (call_user or orch.get("call_user") or settings.get("user_name") or "").strip()[:32]
        tone = tone if tone in (*prompts.TONES, "custom") else (orch.get("tone") or "friendly")
        pids = []
        for ref in projects or []:
            p = self.store.project(str(ref))
            if p and p["id"] not in pids:
                pids.append(p["id"])
        used = {b["color"] for b in self.store.bots()}
        fallback = next((c for c in PALETTE if c not in used), PALETTE[len(used) % len(PALETTE)])
        prompt = (system_prompt or "").strip() or prompts.persona_prompt(
            name=name, tone=tone, tone_custom=tone_custom, call_user=call_user, lang=lang, orchestrator=False,
            role=role.strip()[:300])
        bot = {"id": new_id("b"), "name": name, "color": clean_color(color, fallback), "initials": initials_of(name),
               "tone": tone, "tone_custom": tone_custom[:200], "call_user": call_user, "role": role.strip()[:300],
               "orchestrator": False, "projects": [], "system_prompt": prompt[:8000],
               "avatar": avatars.validate_choice(avatar if avatar is not None else avatars.default_face(
                   role, [b.get("avatar") for b in self.store.bots()]), self.store)}
        self.store.save_bot(bot)
        self.store.ensure_dm(bot)
        if pids:
            for pid in pids:
                self.set_owner(pid, bot["id"], by=by, quiet=True)
            bot = self.store.bot(bot["id"])
        self.act("bot", "act.bot_added", ref=bot["id"], who=self.who(by), name=name)
        self.bus.publish("bots.updated", {"bots": self.store.bots()})
        self.bus.publish("rooms.updated", {"rooms": self.store.rooms()})
        return bot

    def update_bot(self, bot_id: str, fields: dict) -> dict:
        bot = self.store.bot(bot_id)
        if not bot:
            raise LookupError("no such bot")
        if "name" in fields:
            name = clean_name(fields["name"])
            other = self.store.bot_by_name(name)
            if other and other["id"] != bot_id:
                raise ValueError(f"a bot named {name!r} already exists")
            bot["name"] = name
            if not fields.get("initials"):
                bot["initials"] = initials_of(name)
        if fields.get("initials"):
            bot["initials"] = str(fields["initials"]).strip()[:2].upper() or bot["initials"]
        if "color" in fields:
            bot["color"] = clean_color(fields["color"], bot["color"])
        if "avatar" in fields:
            bot["avatar"] = avatars.validate_choice(fields["avatar"], self.store)
        if "role" in fields and not bot.get("orchestrator"):
            bot["role"] = str(fields["role"]).strip()[:300]
        if "system_prompt" in fields:
            sp = str(fields["system_prompt"]).strip()
            if not sp:
                raise ValueError("the system prompt cannot be empty")
            bot["system_prompt"] = sp[:8000]
        if "call_user" in fields:
            bot["call_user"] = str(fields["call_user"]).strip()[:32]
        if "model" in fields:
            m = str(fields["model"] or "")
            if m and m not in self.cfg["models"]:
                raise ValueError(f"unknown model {m!r}")
            bot["model"] = m or None
        new_projects = None
        if "projects" in fields and isinstance(fields["projects"], list) and not bot.get("orchestrator"):
            found = [p["id"] for p in (self.store.project(str(x)) for x in fields["projects"]) if p]
            new_projects = list(dict.fromkeys(found))
        if "project" in fields and not bot.get("orchestrator"):
            p = self.store.project(str(fields["project"] or ""))
            new_projects = [p["id"]] if p else []
        self.store.save_bot(bot)
        if new_projects is not None:
            for pid in bot.get("projects") or []:
                if pid not in new_projects:
                    self.set_owner(pid, None)
            for pid in new_projects:
                self.set_owner(pid, bot_id)
            bot = self.store.bot(bot_id)
        room = self.store.dm_room(bot_id)
        if room and room.get("name") != bot["name"]:
            room["name"] = bot["name"]
            self.store.save_room(room)
        self.bus.publish("bots.updated", {"bots": self.store.bots()})
        self.bus.publish("rooms.updated", {"rooms": self.store.rooms()})
        return bot

    def delete_bot(self, bot_id: str, confirmed: bool = False, by: str = "you") -> dict:
        """Remove a bot from the team (2026-11-16). Its running SnapCode and replies stop, its scheduled tasks and open
        cards go, its projects return to the lead, it leaves its groups and its own chat is hidden. Its memory, messages
        and the raw record stay, so recall still finds what it did."""
        bot = next((b for b in self.store.bots() if b["id"] == bot_id), None)
        if not bot:
            raise LookupError("no such bot")
        if bot.get("orchestrator"):
            raise ValueError("the lead bot cannot be deleted")
        if confirmed is not True:
            raise ValueError("confirm the deletion first")
        stopped = [s["id"] for s in self.store.sessions()
                   if s.get("started_by") == bot_id and s.get("status") in ("running", "queued")]
        for sid in stopped:
            self.sessions.stop(sid)
        tasks = [t["id"] for t in self.tasks.all() if t.get("bot") == bot_id]
        for tid in tasks:
            self.tasks.change(tid, "delete")
        self.release_projects(bot_id)
        with self.chat.answer_lock, self.store.lock:
            for p in self.store.projects():
                if bot_id in (p.get("shared_with") or []):
                    p["shared_with"] = [x for x in p["shared_with"] if x != bot_id]
                    self.store.save_project(p)
            for room in self.store.rooms():
                if bot_id not in (room.get("bots") or []):
                    continue
                self.chat.stop_bot(room["id"], bot_id)
                if room.get("kind") == "group" and len(room["bots"]) > 1:
                    room["bots"] = [x for x in room["bots"] if x != bot_id]
                else:                                   # its own chat, or a group it was alone in
                    room.update(deleted=now_iso(), pinned=False)
                self.store.save_room(room)
            for q in self.store.questions():
                pending = q.get("status") == "open" or (q.get("authorization") or {}).get("state") == "pending"
                if q.get("bot") == bot_id and pending:
                    q["status"] = "cancelled"
                    if q.get("authorization"):
                        q["authorization"]["state"] = "cancelled"
                    self.store.save_question(q)
                    self.bus.publish("question.updated", {"question": self.chat.question_view(q)})
            self.store.remove_bot(bot_id)
            self.archive.add("bot", json.dumps({**bot, "deleted": now_iso()}, ensure_ascii=False), bot=bot_id,
                             title=bot["name"], meta={"source_type": "user", "deleted": True})
        self.act("bot", "act.bot_removed", ref=bot_id, who=self.who(by), name=bot["name"])
        self.store.event("bot.delete", actor=by, where=bot_id, data={"stopped_sessions": stopped, "deleted_tasks": tasks})
        self.bus.publish("bots.updated", {"bots": self.store.bots()})
        self.bus.publish("rooms.updated", {"rooms": self.store.rooms()})
        self.bus.publish("projects.updated", {"projects": self.projects_view()})
        return {"deleted": bot_id, "stopped_sessions": len(stopped), "deleted_tasks": len(tasks)}

    def archive_bot(self, bot_id: str, confirmed: bool = False, by: str = "you") -> dict:
        """Archive a bot (2026-11-17; the user wanted archiving rather than deleting). It leaves the team for now: its
        running SnapCode and replies stop, its scheduled tasks pause, its projects go to the lead, it leaves its groups
        and its own chat is hidden. Nothing is deleted, and restore_bot puts all of it back."""
        bot = next((b for b in self.store.bots() if b["id"] == bot_id), None)
        if not bot:
            raise LookupError("no such bot")
        if bot.get("orchestrator"):
            raise ValueError("the lead bot cannot be archived")
        if confirmed is not True:
            raise ValueError("confirm archiving first")
        stopped = [s["id"] for s in self.store.sessions()
                   if s.get("started_by") == bot_id and s.get("status") in ("running", "queued")]
        for sid in stopped:
            self.sessions.stop(sid)
        paused = [t["id"] for t in self.tasks.all() if t.get("bot") == bot_id and t.get("status") == "active"]
        for tid in paused:
            self.tasks.change(tid, "pause")
        owned = [p["id"] for p in self.store.projects() if p.get("bot") == bot_id]
        self.release_projects(bot_id)
        at = now_iso()
        shared, groups, hidden = [], [], []
        with self.chat.answer_lock, self.store.lock:
            for p in self.store.projects():
                if bot_id in (p.get("shared_with") or []):
                    p["shared_with"] = [x for x in p["shared_with"] if x != bot_id]
                    self.store.save_project(p)
                    shared.append(p["id"])
            for room in self.store.rooms():
                if bot_id not in (room.get("bots") or []):
                    continue
                self.chat.stop_bot(room["id"], bot_id)
                if room.get("kind") == "group" and len(room["bots"]) > 1:
                    room["bots"] = [x for x in room["bots"] if x != bot_id]
                    groups.append(room["id"])
                else:                                   # its own chat, or a group it was alone in
                    hidden.append({"id": room["id"], "pinned": bool(room.get("pinned"))})
                    room.update(deleted=at, pinned=False)
                self.store.save_room(room)
            for q in self.store.questions():
                pending = q.get("status") == "open" or (q.get("authorization") or {}).get("state") == "pending"
                if q.get("bot") == bot_id and pending:
                    q["status"] = "cancelled"
                    if q.get("authorization"):
                        q["authorization"]["state"] = "cancelled"
                    self.store.save_question(q)
                    self.bus.publish("question.updated", {"question": self.chat.question_view(q)})
            index = next((i for i, b in enumerate(self.store.bots()) if b["id"] == bot_id), 0)
            self.store.archive_bot(bot_id, {"at": at, "by": by, "index": index, "stopped_sessions": stopped,
                                            "paused_tasks": paused, "projects": owned, "shared": shared,
                                            "groups": groups, "rooms": hidden})
        self.act("bot", "act.bot_archived", ref=bot_id, who=self.who(by), name=bot["name"])
        self.store.event("bot.archive", actor=by, where=bot_id, data={"stopped_sessions": stopped, "paused_tasks": paused})
        self._publish_team()
        return {"archived": bot_id, "stopped_sessions": len(stopped), "paused_tasks": len(paused)}

    def restore_bot(self, bot_id: str, by: str = "you") -> dict:
        """Bring an archived bot back (2026-11-17): its own chat, its groups, its projects and the tasks archiving paused.
        A one-time task whose time passed meanwhile stays paused, so an old wake-up does not fire at once."""
        with self.chat.answer_lock, self.store.lock:
            waiting = next((b for b in self.store.archived_bots() if b["id"] == bot_id), None)
            if waiting and self.store.bot_by_name(waiting["name"]):
                raise ValueError(f"a bot named {waiting['name']!r} is on the team now; rename one of them first")
            got = self.store.unarchive_bot(bot_id)
            if not got:
                raise LookupError("no archived bot with that id")
            bot, rec = got
            for rid in rec.get("groups") or []:
                room = self.store.room(rid)
                if room and room.get("kind") == "group" and bot_id not in room.get("bots", []):
                    room["bots"] = [*room["bots"], bot_id]
                    self.store.save_room(room)
            for hidden in rec.get("rooms") or []:
                room = next((r for r in self.store.rooms(include_deleted=True) if r["id"] == hidden["id"]), None)
                if room and room.get("deleted") == rec.get("at"):
                    room.pop("deleted", None)
                    room["pinned"] = bool(hidden.get("pinned"))
                    self.store.save_room(room)
            for pid in rec.get("shared") or []:
                p = self.store.project(pid)
                if p and p.get("bot") != bot_id and bot_id not in (p.get("shared_with") or []):
                    p["shared_with"] = [*(p.get("shared_with") or []), bot_id]
                    self.store.save_project(p)
            lead = self.store.orchestrator()
            for pid in rec.get("projects") or []:
                p = self.store.project(pid)
                if p and p.get("bot") in (None, (lead or {}).get("id")):     # nobody took it over meanwhile
                    self.set_owner(pid, bot_id, quiet=True)
        resumed = []
        now = dt.datetime.now().astimezone()
        for t in self.tasks.all():
            if t["id"] not in (rec.get("paused_tasks") or []) or t.get("status") != "paused":
                continue
            sch = t.get("schedule") or {}
            if sch.get("kind") == "once" and dt.datetime.fromisoformat(sch["at"]) <= now:
                continue
            self.tasks.change(t["id"], "resume")
            resumed.append(t["id"])
        self.act("bot", "act.bot_restored", ref=bot_id, who=self.who(by), name=bot["name"])
        self.store.event("bot.restore", actor=by, where=bot_id, data={"resumed_tasks": resumed})
        self._publish_team()
        return {"restored": bot_id, "bot": self.store.bot(bot_id), "resumed_tasks": len(resumed)}

    def archived_bots_view(self) -> list[dict]:
        return [{**{k: b.get(k) for k in ("id", "name", "color", "initials", "avatar", "role")},
                 "archived_at": (b.get("archived") or {}).get("at")} for b in self.store.archived_bots()]

    def _publish_team(self) -> None:
        self.bus.publish("bots.updated", {"bots": self.store.bots(), "archived_bots": self.archived_bots_view()})
        self.bus.publish("rooms.updated", {"rooms": self.store.rooms()})
        self.bus.publish("projects.updated", {"projects": self.projects_view()})

    def start_fresh(self, room_id: str, bot_id: str | None = None, note: str = "", by: str = "you",
                    undo: bool = False) -> dict:
        """Start a chat afresh (2026-11-16), when the user wants a bot to keep only what matters now. The messages so
        far and the room summary stop going to the room's bots, and the bot's records of finished work stop going to
        it; `note` (what still matters) goes to them instead. Nothing is deleted: recall finds the old messages, and
        undo brings everything back."""
        from .background import RoomSummaries
        with self.store.lock:
            room = self.store.room(room_id)
            if not room:
                raise LookupError("no such room")
            team = {b["id"] for b in self.store.bots()}
            if undo:
                rec = room.get("fresh_start")
                if not rec:
                    raise ValueError("this chat was not started afresh")
                RoomSummaries.restore_before_fresh(self, room_id, rec["at"])
            else:
                if bot_id and bot_id not in (room.get("bots") or []) and bot_id in team:
                    raise ValueError("that bot is not in this chat")
                bots = [bot_id] if bot_id in team else [b for b in room.get("bots", []) if b in team]
                old = room.get("fresh_start")
                RoomSummaries.keep_before_fresh(self, room_id, old["at"] if old else None)
            who = self.who(by) if by in ("you", "user", "") else (self.store.bot(by) or {}).get("name", by)
            m = self.store.add_message(room_id, {"role": "system", "work_notice": True, "vars": {"who": who},
                                                 "key": "srv.fresh_undone" if undo else "srv.fresh_start",
                                                 "text": "This chat is no longer started afresh: earlier messages go to the bots again."
                                                 if undo else "This chat was started afresh: earlier messages no longer go to the bots "
                                                 "(they stay in the record).", "fresh_start": not undo},
                                       touch_room=False)
            room = self.store.room(room_id)
            if undo:
                room.pop("fresh_start", None)
            else:                                       # milliseconds: work that ended a moment before stays out
                at = dt.datetime.now().astimezone().isoformat(timespec="milliseconds")
                rec = {"at": at, "through": m["id"], "note": self.guard.clean((note or "").strip())[:4000],
                       "by": by, "bots": bots}
                room["fresh_start"] = rec
            self.store.save_room(room)
            for bid in rec.get("bots") or []:
                b = next((x for x in self.store.bots() if x["id"] == bid), None)
                if not b:
                    continue
                if undo:
                    if b.get("context_since") == rec["at"]:
                        b.pop("context_since", None)
                else:
                    b["context_since"] = rec["at"]
                self.store.save_bot(b)
        self.bus.publish("message.new", {"room": room_id, "message": m})
        self.store.event("room.fresh_start", actor=by, where=room_id, data={"undo": undo, "bots": rec.get("bots"),
                                                                            "note_chars": len(rec.get("note") or "")})
        self.archive.add("context", json.dumps({"fresh_start": rec, "undo": undo}, ensure_ascii=False), room=room_id,
                         title=room.get("name") or room_id, meta={"source_type": "user" if by in ("you", "user", "") else "bot"})
        self.bus.publish("rooms.updated", {"rooms": self.store.rooms()})
        self.bus.publish("bots.updated", {"bots": self.store.bots()})
        return rec

    def create_group(self, name: str, bot_ids: list) -> dict:
        bot_ids = self._group_members(bot_ids)
        bots = [self.store.bot(i) for i in bot_ids]
        if len(bots) < 1:
            raise ValueError("pick at least one bot")
        name = " ".join((name or "").split())[:40] or ", ".join(b["name"] for b in bots)[:40]
        room = self.store.save_room({"id": new_id("r"), "kind": "group", "name": name, "bots": [b["id"] for b in bots],
                                     "last_ts": now_iso(), "last_text": ""})
        self.bus.publish("rooms.updated", {"rooms": self.store.rooms()})
        return room

    def _group_members(self, ids) -> list[str]:
        if not isinstance(ids, list) or not ids or len(ids) > len(self.store.bots()):
            raise ValueError("pick at least one existing bot")
        if any(not isinstance(i, str) or not self.store.bot(i) for i in ids) or len(set(ids)) != len(ids):
            raise ValueError("invalid or duplicate bot")
        return ids

    def update_room(self, rid: str, changes: dict) -> dict:
        """Optional v3 fields; deletion hides a room but retains messages and originals."""
        with self.chat.answer_lock, self.store.lock:
            room = self.store.room(rid)
            if not room:
                raise LookupError("no such room")
            if set(changes) - {"name", "bots", "pinned", "action", "confirmed"}:
                raise ValueError("unknown room field")
            if "pinned" in changes and not isinstance(changes["pinned"], bool):
                raise ValueError("pinned must be true or false")
            deleting = changes.get("action") == "delete"
            if changes.get("action") not in (None, "delete"):
                raise ValueError("unknown room action")
            if room["kind"] != "group" and (deleting or "name" in changes or "bots" in changes):
                raise ValueError("edit the bot profile to rename a direct chat")
            if deleting and changes.get("confirmed") is not True:
                raise ValueError("confirm group deletion first")
            if "name" in changes:
                name = changes["name"]
                if not isinstance(name, str) or not name.strip() or len(name.strip()) > 40:
                    raise ValueError("group name must contain 1 to 40 characters")
                room["name"] = " ".join(name.split())
            previous_bots = set(room.get("bots", []))
            if "bots" in changes:
                room["bots"] = self._group_members(changes["bots"])
            members_changed = previous_bots != set(room.get("bots", []))
            removed_bots = previous_bots - set(room.get("bots", []))
            if deleting or members_changed:
                self.chat.stop(rid)
            if "pinned" in changes:
                room["pinned"] = changes["pinned"]
            if deleting:
                room.update(deleted=now_iso(), pinned=False)
            if deleting or removed_bots:
                for q in self.store.questions():
                    pending = (q.get("status") == "open" or
                               (q.get("authorization") or {}).get("state") == "pending")
                    if q.get("room") == rid and (deleting or q.get("bot") in removed_bots) and pending:
                        q["status"] = "cancelled"
                        if q.get("authorization"):
                            q["authorization"]["state"] = "cancelled"
                        self.store.save_question(q)
                        self.bus.publish("question.updated", {"question": self.chat.question_view(q)})
            room = self.store.save_room(room)
            self.archive.add("room", json.dumps(room, ensure_ascii=False), room=rid,
                             title=room["name"], meta={"source_type": "user", "deleted": deleting})
        self.bus.publish("rooms.updated", {"rooms": self.store.rooms()})
        return room

    # ------------------------------------------------------------ projects and dashboard

    def create_project(self, *, name: str, description: str = "", test_command: str = "", bot: str | None = None,
                       by: str = "you") -> dict:
        from .testparse import validate_test_command
        tests = validate_test_command(test_command)
        name = " ".join((name or "").split())[:60]
        if not name:
            raise ValueError("a project needs a name")
        if self.store.project(name):
            raise ValueError(f"a project named {name!r} already exists")
        slug = slugify(name)
        taken = {p["slug"] for p in self.store.projects()}
        base, n = slug, 2
        root = Path(self.cfg["projects_dir"])
        while slug in taken or (root / slug).exists():
            slug = f"{base}-{n}"
            n += 1
        folder = root / slug
        folder.mkdir(parents=True, exist_ok=False)
        desc = " ".join((description or "").split())[:600]
        commit = gitops.init_repo(folder, {"BRIEF.md": self.guard.clean(brief_text(name, desc, tests, self.lang, ssh=self.cfg.get('codex', {}).get('backend') == 'ssh')),
                                           "AGENTS.md": self.guard.clean(agents_text(name))})
        p = {"id": new_id("p"), "name": name, "slug": slug, "description": desc, "test_command": tests, "bot": bot,
             "git": bool(commit), "first_commit": commit}
        self.store.save_project(p)
        if bot:
            b = self.store.bot(bot)
            if b:
                self.set_owner(p["id"], b["id"], by=by)
                p = self.store.project(p["id"])
        self.act("project", "act.project_created", ref=p["id"], who=self.who(by), name=name)
        self.bus.publish("projects.updated", {"projects": self.projects_view()})
        return p

    def update_project(self, pid: str, fields: dict) -> dict:
        p = self.store.project(pid)
        if not p:
            raise LookupError("no such project")
        if "description" in fields:
            p["description"] = " ".join(str(fields["description"]).split())[:600]
        if "test_command" in fields:
            from .testparse import validate_test_command
            tc = validate_test_command(str(fields["test_command"]))
            p["test_command"] = tc
        owner = None
        if "bot" in fields:
            b = self.store.bot(str(fields["bot"] or ""))
            owner = b["id"] if b else ""
        self.store.save_project(p)
        if owner is not None and owner != (p.get("bot") or ""):
            self.set_owner(p["id"], owner or None)
            p = self.store.project(p["id"])
        self.bus.publish("projects.updated", {"projects": self.projects_view()})
        return p

    # ------------------------------------------------------------ who looks after which project

    def set_owner(self, pid: str, bot_id: str | None, by: str = "you", quiet: bool = False) -> dict:
        """Change this project's owner, retaining the bot's other projects."""
        with self.store.lock:
            p = self.store.project(pid)
            if not p:
                raise LookupError("no such project")
            lead = self.store.orchestrator()
            b = self.store.bot(bot_id) if bot_id else lead
            if bot_id and not b:
                raise LookupError("no such bot")
            moved = None
            for other in self.store.bots():
                if pid in (other.get("projects") or []) and (not b or other["id"] != b["id"]):
                    other["projects"] = [x for x in other["projects"] if x != pid]
                    self.store.save_bot(other)
            if b:
                b["projects"] = list(dict.fromkeys([*(b.get("projects") or []), pid]))
                self.store.save_bot(b)
            p["bot"] = b["id"] if b else (lead["id"] if lead else None)
            self.store.save_project(p)
        if moved and not quiet:
            self.act("bot", "act.project_moved", ref=b["id"], bot=b["name"], project=moved["name"])
        self.bus.publish("projects.updated", {"projects": self.projects_view()})
        self.bus.publish("bots.updated", {"bots": self.store.bots()})
        return {"project": p, "moved": moved}

    def release_projects(self, bot_id: str) -> None:
        """Return all of a teammate's projects to the lead without affecting anyone else's work."""
        with self.store.lock:
            b = self.store.bot(bot_id)
            if not b or b.get("orchestrator"):
                return
            for p in self.store.projects():
                if p.get("bot") == bot_id:
                    self.set_owner(p["id"], None)
        self.bus.publish("projects.updated", {"projects": self.projects_view()})
        self.bus.publish("bots.updated", {"bots": self.store.bots()})

    def share_project(self, pid: str, bot_id: str, *, allow: bool, actor: dict) -> dict:
        from .access import check_project
        with self.store.lock:
            check_project(self, actor, pid, manage=True)
            p = self.store.project(pid)
            if not self.store.bot(bot_id):
                raise LookupError("no such bot")
            shared = set(p.get("shared_with") or [])
            shared.add(bot_id) if allow else shared.discard(bot_id)
            p["shared_with"] = sorted(shared)
            self.store.save_project(p)
            self.store.event("project.share", actor=actor["id"], where=pid,
                             data={"bot": bot_id, "allow": allow})
        self.bus.publish("projects.updated", {"projects": self.projects_view()})
        return p

    def write_brief(self, pid: str, text: str, by: str = "you") -> dict:
        """Replace a project's BRIEF.md (in the project folder, committed on main when the project has git)."""
        p = self.store.project(pid)
        if not p:
            raise LookupError("no such project")
        text = self.guard.clean((text or "").strip()[:8000])
        if not text:
            raise ValueError("the brief is empty")
        folder = Path(self.cfg["projects_dir"]) / p["slug"]
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "BRIEF.md").write_text(text + "\n", encoding="utf-8-sig")      # a BOM: PowerShell 5.1 reads it right
        if p.get("git") and gitops.available():
            gitops.run(folder, "add", "BRIEF.md")
            if gitops.run(folder, "diff", "--cached", "--quiet").returncode != 0:
                gitops.run(folder, "commit", "-q", "-m", f"Update BRIEF.md ({by})")
        self.archive.add_doc(p, "BRIEF.md", text)
        from .temporal import digest
        p["brief_version"] = digest(text)
        self.store.save_project(p)
        self.act("project", "act.brief", ref=p["id"], who=self.who(by), project=p["name"])
        self.store.event("project.brief", actor=by, where=p["id"], data={"chars": len(text)})
        self.bus.publish("projects.updated", {"projects": self.projects_view()})
        return p

    def read_brief(self, pid: str) -> str:
        p = self.store.project(pid)
        if not p:
            raise LookupError("no such project")
        try:
            return (Path(self.cfg["projects_dir"]) / p["slug"] / "BRIEF.md").read_text(encoding="utf-8-sig")
        except OSError:
            return ""

    def assign_project(self, pid: str, bot_id: str, note: str = "", by: str = "you",
                       origin: str | None = None, source_room: str | None = None) -> dict:
        """Hand a project to a teammate: owner of the project, BRIEF in its context, a note in its chat, a reply."""
        p, b = self.store.project(pid), self.store.bot(bot_id)
        if not p or not b:
            raise LookupError("no such project or bot")
        self.set_owner(pid, b["id"], by=by)
        p, b = self.store.project(pid), self.store.bot(bot_id)
        room = self.store.ensure_dm(b)
        note = " ".join((note or "").split())[:600]
        vars = {"who": self.who(by), "project": p["name"]}
        text = self.t("srv.assign", **vars) + ("\n" + self.t("srv.assign.note", note=note) if note else "")
        msg = {"role": "system", "text": self.guard.clean(text), "assignment": p["id"], "key": "srv.assign", "vars": vars}
        if note:
            msg["note"] = self.guard.clean(note)
        m = self.store.add_message(room["id"], msg)
        self.bus.publish("message.new", {"room": room["id"], "message": m})
        self.bus.publish("room.updated", {"room": self.store.room(room["id"])})
        self.act("bot", "act.assigned", ref=b["id"], who=self.who(by), project=p["name"], bot=b["name"])
        self.store.event("project.assign", actor=by, where=p["id"], data={"bot": b["id"]})
        self.bus.publish("projects.updated", {"projects": self.projects_view()})
        self.bus.publish("bots.updated", {"bots": self.store.bots()})
        if origin:
            if source_room:
                self.chat.dialogue.register(origin, source_room)
            self.chat.dialogue.register(origin, room["id"])
        self.chat._enqueue(room["id"], b["id"], origin or m["id"], 2 if origin else 1)
        return {"project": p, "bot": b, "room": room}

    def update_dashboard(self, op: str, widgets, ids, by: str) -> dict:
        with self.store.lock:
            cur = self.store.dashboard()["widgets"]
            new = dashmod.apply(cur, op, widgets, ids)
            d = self.store.save_dashboard(new, by)
        self.act("dashboard", "act.dashboard", ref="dashboard", who=self.who(by))
        self.store.event("dashboard.update", actor=by, where="dashboard", data={"op": op, "widgets": [w["id"] for w in d["widgets"]]})
        self.bus.publish("dashboard.updated", {"dashboard": d})
        return d

    def run_approval(self, q: dict, approved: bool) -> tuple[str, dict]:
        """Run an approved proposal. Returns the note for the room as (key, values) from the language files."""
        # Keep room changes ordered through the action, without holding the store lock
        # while invoking sessions, task scheduling or plugin code.
        with self.chat.answer_lock:
            return self._run_approval(q, approved)

    def _run_approval(self, q: dict, approved: bool) -> tuple[str, dict]:
        a = q.get("action") or {}
        try:
            self.ledger.consume_approval(q, approved)
        except ValueError:
            return "note.approval_invalid", {}
        if not approved:
            return "note.declined", {}
        try:
            proposer = (self.store.bot(a.get("bot") or "") or {}).get("name", "Bot")
            if a.get("type") in ("message_bot", "create_group"):
                b = self.store.bot(a["bot"])
                result = self.chat.message_bot(b, [a["to"]] if a["type"] == "message_bot" else a["bots"],
                    a["text"], room_id=a.get("room") or "", name=a.get("name") or "",
                    origin=a.get("origin"), source_room=a.get("source_room"), new_group=a["type"] == "create_group")
                return "note.bot_message", {"room": result["room"]}
            if a.get("type") == "share_project":
                p = self.share_project(a["project"], a["to"], allow=a["allow"], actor=self.store.bot(a["bot"]))
                return "note.project_shared", {"project": p["name"]}
            if a.get("type") == "start_session":
                p = self.store.project(a["project"])
                s = self.sessions.create(p, a["harness"], a["prompt"], title=a.get("title", ""), started_by=a.get("bot", "user"),
                                         room=q.get("room"), bot=a.get("bot"), required_files=a.get("required_files"), task_type=a.get("task_type"),
                                         from_session=a.get("from_session"), files=a.get("files"))
                return "note.session_started", {"title": s.get("title") or s["id"], "session": s["id"]}
            if a.get("type") == "send_to_session":
                s = self.store.session(a["session"]) or {}
                if 'test_command' in a:
                    self.update_project(s['project'], {'test_command': a['test_command']})
                self.sessions.send(a["session"], a["prompt"], requested_by=a.get("bot"), files=a.get("files"))
                return "note.sent", {"title": s.get("title") or a["session"], "session": a["session"]}
            if a.get("type") == "create_project":
                p = self.create_project(name=a["name"], description=a.get("description", ""),
                                        test_command=a.get("test_command", ""), bot=a.get("bot"), by=proposer)
                return "note.project_created", {"project": p["name"]}
            if a.get("type") == "update_dashboard":
                self.update_dashboard(a["op"], a.get("widgets"), a.get("ids"), by=proposer)
                return "note.dashboard", {}
            if a.get("type") == "write_brief":
                p = self.write_brief(a["project"], a.get("text", ""), by=proposer)
                return "note.brief", {"project": p["name"]}
            if a.get("type") == "assign_project":
                r = self.assign_project(a["project"], a["to"], note=a.get("note", ""), by=proposer,
                                        origin=a.get("origin"), source_room=q.get("room"))
                return "note.assigned", {"project": r["project"]["name"], "bot": r["bot"]["name"]}
            if a.get("type") == "write_memory":
                self.memory_write(a["bot"], a["text"], a.get("mode") or "append", scope=a.get("scope") or "self")
                return "note.memory", {}
            if a.get("type") == "plugin":
                from .plugins import PluginContext
                p = self.plugin_tools.get(a.get("tool"))
                if not p:
                    return "note.failed", {"error": "unknown tool"}
                bot = self.store.bot(a.get("bot") or "") or {"id": "", "name": "Bot"}
                out = p.run(PluginContext(app=self, bot=bot, room=self.store.room(q.get("room") or ""), session=None,
                                          folder=None, lang=self.lang, read_web=True), **(a.get("args") or {}))
                text = out if isinstance(out, str) else __import__("json").dumps(out, ensure_ascii=False)
                label = p.label.get(self.lang) or p.label.get("en") or a["tool"]
                return "note.plugin", {"tool": label, "result": text[:600]}
            if a.get("type") == "schedule_task":
                t = self.tasks.create(bot_id=a["bot"], title=a.get("title", ""), prompt=a.get("prompt", ""),
                                      schedule=a.get("schedule") or {}, by=proposer)
                return "note.task_added", {"title": t["title"]}
            if a.get("type") == "create_bot":
                b = self.create_bot(name=a["name"], role=a.get("role", ""), color=a.get("color", ""),
                                    projects=a.get("projects") or [], by=proposer)
                return "note.bot_added", {"name": b["name"]}
        except (ValueError, LookupError, TypeError) as e:
            return "note.failed", {"error": str(e)[:300]}
        return "note.failed", {"error": "unknown action"}

    # ------------------------------------------------------------ views for the phone

    def projects_view(self) -> list[dict]:
        out = []
        sessions = self.store.sessions()
        for p in self.store.projects():
            ss = [s for s in sessions if s["project"] == p["id"]]
            from . import plan as planmod
            open_ = [s for s in ss if s.get("status") not in ("idle",) and not s.get("merged")]
            out.append({**p, "sessions": len(ss), "running": sum(1 for s in ss if s["status"] in ("running", "queued")),
                        "last": ss[0]["updated"] if ss else p.get("created"),
                        "progress": planmod.total([s.get("plan") for s in open_])})
        return out

    def inside_view(self, bot_id: str, room_id: str = "") -> dict:
        """A bot's inside screen (round 6): what it would see now, its project(s), its SnapCode, its tasks."""
        from .archive import Scope
        st = self.store
        b = st.bot(bot_id)
        if not b:
            raise LookupError("no such bot")
        room = st.room(room_id) if room_id else None
        if not room or b["id"] not in (room.get("bots") or []):
            room = st.dm_room(b["id"]) or st.ensure_dm(b)
        lang = st.settings().get("lang") or "en"
        _, report = self.chat.context.build(room, b, lang)
        view = {"sections": report.pop("sections", []), "history": report.pop("history_view", []), "report": report,
                "room": room["id"], "room_name": room.get("name"), "room_kind": room.get("kind"),
                "fresh_start": room.get("fresh_start"), "context_since": b.get("context_since")}
        from .access import project_ids
        mine = project_ids(self, b)
        projects = [p for p in self.projects_view() if b.get("orchestrator") or p["id"] in mine]
        ss = st.sessions()
        if b.get("orchestrator"):
            ss = [s for s in ss if s.get("status") in ("running", "queued", "waiting", "review", "failed")
                  or s.get("started_by") == b["id"] or s.get("bot") == b["id"]]
        else:
            ss = [s for s in ss if s.get("project") in mine]
        return {"bot": b["id"], "view": view, "projects": projects, "sessions": [self.sessions.summary(s) for s in ss[:8]],
                "tasks": [self.tasks.view(t) for t in self.tasks.for_bot(b["id"])],
                "archive": self.archive.stats(Scope.for_bot(self, b))}

    def usage_view(self) -> dict:
        u = self.store.usage()
        today = u["days"].get(dt.date.today().isoformat(), {})
        exp = self.models.spec(None).get("key_expires") or self.cfg["api"].get("key_expires")
        days_left = None
        if exp:
            try:
                days_left = (dt.date.fromisoformat(str(exp)) - dt.date.today()).days
            except ValueError:
                days_left = None
        week = sorted(u["days"].items())[-7:]
        return {"today": today, "total": u["total"], "last_call": u.get("last_call"),
                "slots": {"in_use": self.slots.in_use(), "count": self.slots.count, "mine": self.slots.mine,
                          "waiting": self.slots.waiting, "shared": not str(self.cfg["slots"]["dir"]).startswith(self.cfg["data_dir"])},
                "key": {"set": self.llm.has_key(), "expires": exp, "days_left": days_left},
                "models": self.models.view(), "by_model": today.get("by_model") or {},
                "model": self.cfg["api"]["model"], "week": [{"day": d, **v} for d, v in week]}

    # The long texts of a session are read when its page opens (GET /api/sessions/<id>); the lists only need these.
    BRIEF_DROP = ("last_prompt", "last_message", "previous_message", "changed_files", "produced_files", "result", "goal")

    def session_brief(self, s: dict, project_name: str | None = None) -> dict:
        """A session summary for lists (Home, Dashboard, the inbox): the same fields, without the long texts.
        A finished session's brief is kept until its file or project name changes (the phone asks for all of them
        on every start; the whole response is still cleaned of secret values when it is sent)."""
        light = {k: v for k, v in s.items() if k not in self.BRIEF_DROP}
        if isinstance(light.get("tests"), dict):
            light["tests"] = {k: v for k, v in light["tests"].items() if k != "output"}
        live = s.get("status") in ("running", "queued")
        key = (json.dumps(light, sort_keys=True, default=str), len(s.get("changed_files") or []), project_name)
        cache = self.__dict__.setdefault("_brief_cache", {})
        hit = cache.get(s.get("id"))
        if hit and hit[0] == key and not live:
            return dict(hit[1])
        out = self.sessions.summary(light)
        out["changed_count"] = len(s.get("changed_files") or [])
        for k in ("last_prompt", "last_message", "changed_files", "result"):
            out.pop(k, None)
        if not live:
            cache[s.get("id")] = (key, dict(out))
        return out

    def bootstrap(self, device: dict | None = None) -> dict:
        st = self.store
        rooms = st.rooms()
        names = {p.get("id"): p.get("name") for p in st.projects()}
        return {
            "version": self.version, "settings": st.settings(), "bots": st.bots(), "rooms": rooms,
            "former_bots": [{k: b.get(k) for k in ("id", "name", "color", "initials", "avatar")} for b in st.former_bots()],
            "archived_bots": self.archived_bots_view(),
            "busy_rooms": [r["id"] for r in rooms if self.chat.busy(r["id"])],
            "projects": self.projects_view(),
            "sessions": [self.session_brief(s, names.get(s.get("project"))) for s in st.sessions()],
            "questions": [self.chat.question_view(q) for q in st.questions() if q.get("status") == "open"],
            "harnesses": self.sessions.harness_info(), "dashboard": st.dashboard(), "usage": self.usage_view(),
            "activity": st.recent_activity(25), "tasks": [self.tasks.view(t) for t in self.tasks.all()], "status": self.status(), "background": self.background_view(), "tones": list(prompts.TONES) + ["custom"], "palette": PALETTE,
            "models": self.models.view(), "routing": self.cfg["routing"],
            "tool_labels": {n: p.label for n, p in self.plugin_tools.items() if p.label},
            "push": {"public_key": self.push.public_key_b64(), "devices": len(self.push.subscriptions())},
            "device": {"id": (device or {}).get("id"), "label": (device or {}).get("label")},
        }
