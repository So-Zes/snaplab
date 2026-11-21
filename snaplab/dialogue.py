"""Bot conversations across rooms: the user's stop, optional limits (none by default) and external-read provenance."""
from __future__ import annotations

import re
import time

DEFAULTS = {"max_consecutive": 0, "max_hourly": 0}   # 0 = no limit (config "bot_conversations" may set one)


def session_origin(sid: str) -> str:
    """Session context survives follow-ups; keep its reads separate from human chat origins."""
    return "session:" + sid


def stop_requested(text):
    text = re.sub(r"^@\S+\s+", "", text.strip()).strip(" 。.!！?？")
    english = r"(?:(?:please|can you|could you)\s+)?(?:stop|pause)(?:\s+(?:the\s+)?(?:bots|conversation|chat|talking|it|now|please))*"
    japanese = r"(?:(?:もう|いったん|一旦|ちょっと|ここで|みんな|この会話を|会話を|Bot の会話を)[、,\s]*)*(?:止めて|やめて|中止して|ストップ)(?:ください|下さい|ほしい|ね)?"
    russian = (r"(?:пожалуйста[,\s]+)?(?:стоп|остановись|остановитесь|остановите|прекратите|приостановите)"
               r"(?:\s+(?:разговор|беседу|общение|чат|ботов|разговор\s+ботов))?"
               r"(?:[,\s]+пожалуйста)?")
    return bool(re.fullmatch(english, text, re.I) or re.fullmatch(japanese, text)
                or re.fullmatch(russian, text, re.I))


class DialogueLimits:
    def __init__(self, app):
        self.app, self.store = app, app.store

    def settings(self):
        cfg = self.app.cfg.get("bot_conversations") or {}
        from .config import limit
        return {k: limit(cfg.get(k, v)) for k, v in DEFAULTS.items()}

    def _load(self):
        data = self.store.read_json("bot-conversations.json", {"chains": {}, "hour": []})
        data["hour"] = [x for x in data["hour"] if x["ts"] > time.time() - 3600]
        return data

    def _save(self, data):
        self.store.write_json("bot-conversations.json", data)

    def register(self, origin, room):
        with self.store.lock:
            data = self._load()
            chain = data["chains"].setdefault(origin, {"count": 0, "rooms": [], "stopped": False})
            if room and room not in chain["rooms"]:
                chain["rooms"].append(room)
            self._save(data)

    def active(self, origin, room):
        r = self.store.room(room)
        chain = self._load()["chains"].get(origin) or {}
        return bool(r and not r.get("bots_paused") and not chain.get("stopped"))

    def web_reads(self, origin):
        """Only the requested conversation or session contributes; old chains have no reads."""
        with self.store.lock:
            return self._load()["chains"].get(origin, {}).get("web_reads", [])

    def mark_web_read(self, origin, room, read):
        with self.store.lock:
            self.register(origin, room)
            data = self._load()
            reads = data["chains"][origin].setdefault("web_reads", [])
            if read not in reads:
                reads.append(dict(read))
                self._save(data)

    def waiting_for_user(self, origin):
        with self.store.lock:
            return bool(self._load()["chains"].get(origin, {}).get("waiting_for_user"))

    def wait_for_user(self, origin, room):
        with self.store.lock:
            self.register(origin, room)
            data = self._load()
            data["chains"][origin]["waiting_for_user"] = True
            self._save(data)

    def reserve(self, origin, room, bot):
        """Count before a model call or outbound message, including silent/error attempts."""
        with self.store.lock:
            self.register(origin, room)
            data, cfg = self._load(), self.settings()
            chain = data["chains"][origin]
            if not self.active(origin, room):
                return "stopped"
            if chain["count"] >= cfg["max_consecutive"]:
                return "consecutive"
            if sum(x["bot"] == bot for x in data["hour"]) >= cfg["max_hourly"]:
                return "hourly"
            chain["count"] += 1
            data["hour"].append({"ts": time.time(), "bot": bot})
            self._save(data)
            return None

    def accept_text(self, origin, text):
        """False only when the user stopped this conversation. A text that repeats an earlier one is accepted
        (until 2026-11-21 the same text as one of the last 20 was refused)."""
        with self.store.lock:
            chain = self._load()["chains"].get(origin)
            return bool(chain) and not chain.get("stopped")

    def reserve_session(self, bot):
        """Session starts and follow-ups share the bot's durable hourly activity limit."""
        with self.store.lock:
            data = self._load()
            if sum(x["bot"] == bot for x in data["hour"]) >= self.settings()["max_hourly"]:
                raise ValueError("Bot hourly activity limit reached; wait before starting more work")
            data["hour"].append({"ts": time.time(), "bot": bot, "kind": "session"})
            self._save(data)

    def stop(self, room):
        with self.store.lock:
            data = self._load()
            affected = {room}
            for chain in data["chains"].values():
                if room in chain["rooms"]:
                    chain["stopped"] = True
                    affected.update(chain["rooms"])
            r = self.store.room(room)
            if r:
                r["bots_paused"] = True
                self.store.save_room(r)
            self._save(data)
            return affected
