"""Experimental (2026-11-22): a decision model watches work that may be stuck and wakes a bot.

SnapLab no longer cuts waiting work by time (2026-11-21): a model that never answers, a command that never ends or
a page that never loads is waited for until someone stops it. While this experiment is on, a watcher looks every
`every_s` seconds at running SnapCode sessions and at bot replies that are being written. Work that has shown no
progress for `quiet_s` seconds is described to a decision model (default liquid/d1.2-ultra through OpenRouter's
decisions endpoint, key OPENROUTER_API_KEY) with one yes/no question: is it stuck in a way that should be looked at
now? When the answer's probability reaches `wake_at`, a bot is woken like a wake-up it set itself:
- a quiet SnapCode session wakes the bot that started it, in the room it was started from;
- a quiet bot reply wakes the lead bot in its own chat (that bot's turn is the one that is stuck, so it cannot be
  woken itself); if the lead bot is the stuck one, the user gets a push notification instead.
Nothing is stopped or cut; the woken bot decides (wait, look with get_session, message, stop or start again).
The same work is not described again within `rewake_s` seconds of a wake-up.

On and off: Settings > Advanced > Experiments, or decision_watch.enabled in config.json (the setting wins). On by
default since 1.1.6; without the key nothing is checked or sent.
Separate by design: the whole feature is this module, one start() in server.background(), one view in the
bootstrap, one settings route and one switch in the app. Turning it off stops the next check and any call that is
still waiting.
What is sent: the kind of work, its title, how long it has run and been quiet, and the last few events with a
short excerpt, cleaned by the same redactor as model calls (home folder, user and PC names, tailnet) and by the
API key guard. OpenRouter's decision models may keep what they receive.
Every decision is appended to data/decision-watch.jsonl.
"""
from __future__ import annotations

import datetime as dt
import json
import threading
import time
import urllib.error
import urllib.request

DEFAULTS = {"enabled": True, "every_s": 120, "quiet_s": 600, "wake_at": 0.6, "rewake_s": 1800,
            "model": "liquid/d1.2-ultra", "url": "https://openrouter.ai/api/alpha/decisions",
            "key_env": "OPENROUTER_API_KEY"}
QUESTION = {"stuck": {"type": "noul", "instructions": (
    "Is this work stuck, hung or failing in a way that the bot responsible for it should look at now? Answer yes "
    "only when waiting longer is unlikely to help, for example when nothing has happened for a long time while it "
    "waits on a command, a page or a model that may never answer. A long build, install, download or test that is "
    "still plausibly working is not stuck.")}}
STATE_HEAD = ("SnapLab runs AI work on the user's PC: SnapCode sessions (a coding agent in a Linux workspace that runs "
              "commands, edits files and uses a browser) and bot replies (a chat model that calls tools). Nothing is cut "
              "off by time, so work that hangs waits until someone stops it. Events: 'start' means a command or tool call "
              "began and is still running unless a matching 'command' event follows; 'command' means it finished, with "
              "its exit code; 'message' is the agent's text; 'tool' a tool call.\n\n")


def _ts(value) -> float | None:
    try:
        return dt.datetime.fromisoformat(str(value)).timestamp()
    except (TypeError, ValueError):
        return None


def _clip(text, n: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[:n - 1] + "…"


class DecisionWatch:
    def __init__(self, app, decide=None):
        self.app = app
        self.cfg = {**DEFAULTS, **(app.cfg.get("decision_watch") or {})}
        self._decide = decide or self.call_model
        self.progress: dict[str, tuple] = {}
        self.woken: dict[str, float] = {}
        self.stats = {"checks": 0, "asked": 0, "woke": 0, "cost_usd": 0.0, "last_check": None, "last_error": ""}
        self.stop_ev = threading.Event()
        self.thread: threading.Thread | None = None

    # ------------------------------------------------------------------ on and off
    def enabled(self) -> bool:
        chosen = self.app.store.settings().get("decision_watch")
        return bool(self.cfg["enabled"]) if chosen is None else bool(chosen)

    def set_enabled(self, on: bool) -> dict:
        self.app.store.update_settings(decision_watch=bool(on))
        if on:
            self.start()
        return self.view()

    def key_set(self) -> bool:
        from .llm import get_api_key
        return bool(get_api_key(self.cfg["key_env"]))

    def view(self) -> dict:
        return {"enabled": self.enabled(), "model": self.cfg["model"], "key_env": self.cfg["key_env"],
                "key_set": self.key_set(), "quiet_minutes": round(float(self.cfg["quiet_s"]) / 60, 1),
                "wake_at": self.cfg["wake_at"], **self.stats}

    def start(self) -> None:
        if self.thread and self.thread.is_alive():
            return

        def loop():
            while not self.stop_ev.wait(float(self.cfg["every_s"])):
                if not self.enabled():
                    continue
                try:
                    self.check()
                except Exception as e:  # noqa: BLE001 - an experiment must never disturb SnapLab
                    self.stats["last_error"] = f"{type(e).__name__}: {e}"[:300]
                    self.app.log(f"decision watch: {e!r}")
        self.thread = threading.Thread(target=loop, daemon=True, name="decision-watch")
        self.thread.start()

    # ------------------------------------------------------------------ what is running
    def candidates(self, now: float) -> list[dict]:
        st, out, seen = self.app.store, [], set()
        for s in st.sessions():
            if s.get("status") != "running":
                continue
            events = st.session_events(s["id"], limit=8)
            last = _ts(events[-1].get("ts")) if events else None
            began = _ts(s.get("started") or s.get("created")) or now
            last = last or _ts(s.get("updated")) or began
            recent = [f"{e.get('kind')} ({_clip(e.get('ts'), 25)[11:19]}): {_clip(e.get('text'), 160)}"
                      + (f" -> exit {e.get('exit_code')}" if e.get("kind") == "command" else "") for e in events[-5:]]
            out.append({"key": "s:" + s["id"], "kind": "SnapCode session", "id": s["id"],
                        "title": s.get("title") or s.get("goal") or "", "bot": s.get("bot"), "room": s.get("room"),
                        "running_s": now - began, "quiet_s": now - last, "recent": recent,
                        "detail": f"harness {s.get('harness')}, turn {s.get('turns') or 1}"})
        for m in list(getattr(self.app.chat, "live", {}).values()):
            key = "m:" + m["id"]
            seen.add(key)
            tools = m.get("tools") or []
            sig = (len(m.get("text") or ""), len(m.get("thinking") or ""), len(tools),
                   tuple(t.get("status") for t in tools))
            prev = self.progress.get(key)
            since = prev[1] if prev and prev[0] == sig else now
            self.progress[key] = (sig, since)
            bot = st.bot(m.get("bot") or "") or {}
            recent = [f"tool {t.get('name')} [{t.get('status')}]: {_clip(t.get('label'), 120)}" for t in tools[-5:]]
            if m.get("text"):
                recent.append("reply so far: " + _clip(m["text"], 200))
            out.append({"key": key, "kind": "bot reply", "id": m["id"], "title": f"reply of bot {bot.get('name') or ''}",
                        "bot": m.get("bot"), "room": m.get("room"), "running_s": now - (_ts(m.get("ts")) or now),
                        "quiet_s": now - since, "recent": recent, "detail": f"{len(tools)} tool calls so far"})
        for key in [k for k in self.progress if k not in seen]:
            self.progress.pop(key, None)
        return out

    def describe(self, c: dict) -> str:
        clean = lambda s: self.app.guard.clean(self.app.redactor.clean(s))  # noqa: E731
        lines = [f"Work: {c['kind']} \"{_clip(c['title'], 200)}\" ({c['detail']}).",
                 f"Running for {round(c['running_s'] / 60, 1)} minutes; no progress for {round(c['quiet_s'] / 60, 1)} minutes.",
                 "Last events (oldest first):", *([f"- {x}" for x in c["recent"]] or ["- none"])]
        return STATE_HEAD + clean("\n".join(lines))

    # ------------------------------------------------------------------ one check
    def check(self, now: float | None = None) -> list[dict]:
        now = time.time() if now is None else now
        if self._decide == self.call_model and not self.key_set():
            return []                                # on by default: without the key nothing is described or sent
        self.stats["checks"] += 1
        self.stats["last_check"] = dt.datetime.fromtimestamp(now).astimezone().isoformat(timespec="seconds")
        done = []
        for c in self.candidates(now):
            if c["quiet_s"] < float(self.cfg["quiet_s"]) or now - self.woken.get(c["key"], 0) < float(self.cfg["rewake_s"]):
                continue
            state = self.describe(c)
            try:
                p, cost = self._decide(state)
            except Exception as e:  # noqa: BLE001 - recorded and shown in Settings
                self.stats["last_error"] = f"{type(e).__name__}: {e}"[:300]
                self.record(c, None, False, error=self.stats["last_error"])
                continue
            self.stats["asked"] += 1
            self.stats["cost_usd"] = round(self.stats["cost_usd"] + float(cost or 0), 8)
            woke = p is not None and p >= float(self.cfg["wake_at"])
            if woke:
                self.wake(c, p)
                self.woken[c["key"]] = now
                self.stats["woke"] += 1
            self.record(c, p, woke, cost=cost)
            done.append({"key": c["key"], "p": p, "woke": woke})
        return done

    def record(self, c: dict, p, woke: bool, cost=None, error: str = "") -> None:
        row = {"ts": dt.datetime.now().astimezone().isoformat(timespec="seconds"), "work": c["key"], "kind": c["kind"],
               "quiet_s": round(c["quiet_s"]), "running_s": round(c["running_s"]), "p_stuck": p, "woke": woke,
               "cost_usd": cost, "model": self.cfg["model"]}
        if error:
            row["error"] = error
        try:
            self.app.store.append_jsonl(self.app.store.path("decision-watch.jsonl"), row)
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------ the decision model
    def call_model(self, state: str) -> tuple[float | None, float]:
        from .llm import get_api_key
        from .provider_protocol import open_request
        key = get_api_key(self.cfg["key_env"])
        if not key:
            raise ValueError(f"{self.cfg['key_env']} is not set")
        body = json.dumps({"model": self.cfg["model"], "state": state, "questions": QUESTION}).encode("utf-8")
        req = urllib.request.Request(self.cfg["url"], data=body, method="POST",
                                     headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}",
                                              "X-OpenRouter-Title": "SnapLab decision watch"})
        self.app.guard.add(key)
        try:
            resp = open_request(req, cancel=lambda: self.stop_ev.is_set() or not self.enabled())
            with resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            text = e.read().decode("utf-8", "replace")[:300] if e.fp else ""
            raise ValueError(f"HTTP {e.code} {self.app.guard.clean(text)}") from None
        answer = ((data.get("answers") or {}).get("stuck") or {})
        p = answer.get("noul")
        usage = data.get("usage") or {}
        cost = usage.get("cost") or ((data.get("meta") or {}).get("usage") or {}).get("usd_spent")
        if cost is None:
            cost = (usage.get("input_tokens") or usage.get("prompt_tokens") or 0) * 0.042e-6
        return (float(p) if isinstance(p, (int, float)) else None), float(cost or 0)

    # ------------------------------------------------------------------ waking a bot
    def wake(self, c: dict, p: float) -> None:
        st, chat = self.app.store, self.app.chat
        minutes = round(c["quiet_s"] / 60)
        if c["kind"] == "SnapCode session":
            bot = st.bot(c.get("bot") or "")
            room = st.room(c.get("room") or "") if bot else None
            if bot and (not room or bot["id"] not in (room.get("bots") or [])):
                room = st.dm_room(bot["id"]) or st.ensure_dm(bot)
            text = (f"[SnapLab watch] SnapCode session {c['id']} \"{_clip(c['title'], 120)}\" has shown no progress for "
                    f"about {minutes} minutes; a decision model judged it likely stuck (p={p:.2f}). Look at it "
                    "(get_session shows its last steps) and decide: wait, send it a message, stop it or start it again. "
                    "If nothing needs doing, reply exactly [SILENT].")
        else:
            stuck = c.get("bot")
            bot = next((b for b in st.bots() if b.get("orchestrator") and b["id"] != stuck), None)
            room = (st.dm_room(bot["id"]) or st.ensure_dm(bot)) if bot else None
            name = (st.bot(stuck or "") or {}).get("name") or stuck
            text = (f"[SnapLab watch] The reply that bot {name} is writing in room {c.get('room')} has shown no progress "
                    f"for about {minutes} minutes; a decision model judged it likely stuck (p={p:.2f}). Its last steps: "
                    f"{'; '.join(c['recent'][-3:]) or 'none'}. Decide whether to tell the user, message that bot, or wait. "
                    "If nothing needs doing, reply exactly [SILENT].")
            if not bot or not room:
                try:
                    self.app.push.notify("needs_you", "SnapLab", self.app.guard.clean(
                        f"A reply of {name} has shown no progress for about {minutes} minutes."), dedupe=c["key"])
                except Exception:  # noqa: BLE001
                    pass
                return
        if not bot or not room:
            return
        text = self.app.guard.clean(text)
        m = st.add_message(room["id"], {"role": "system", "text": text, "key": "srv.watch.wake",
                                        "vars": {"minutes": str(minutes)}})
        self.app.bus.publish("message.new", {"room": room["id"], "message": m})
        self.app.bus.publish("room.updated", {"room": st.room(room["id"])})
        st.event("watch.wake", actor="snaplab", where=bot["id"], data={"work": c["key"], "p": p, "message": m["id"]})
        chat.task_origins.add(m["id"])
        chat._enqueue(room["id"], bot["id"], m["id"], 1)
