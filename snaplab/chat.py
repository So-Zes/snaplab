"""Chat rooms: who answers, building the model context, streaming replies to phones, question cards.

- One-to-one rooms: the room's bot answers.
- Group rooms: mentions take priority; all members can contribute or stay silent. Autonomous replies and
  public bot-to-bot deliveries share persistent limits across rooms.
- Each room handles one turn at a time; different rooms run in parallel (the API slots cap the total).
- Replies stream: thinking and text deltas go out as events (batched about every 80 ms). The reply is saved
  when it starts (status "streaming"), as a draft every few seconds, and again when it ends, so a restart
  leaves the text that had arrived with an "interrupted" mark instead of nothing (QA B-2, QA2 L-1). Errors carry a short code the phone turns into a plain sentence (QA B-3).
- Messages sent while a bot is busy are answered together in its next turn, not once each (QA B-5).
"""
from __future__ import annotations

import queue
import re
import threading
import time
import unicodedata

from . import context as context_mod
from . import prompts
from . import mcp_client
from .agent import run_loop, step_cap
from .llm import ApiError, Cancelled, error_code
from .store import new_id, now_iso
from . import tools as tools_mod
from .tools import Toolbox
from .dialogue import DialogueLimits, stop_requested

APPROVAL_OPTIONS = ("Approve", "Decline")


QUESTION_END = re.compile(r"([?？]|決めて(ください)?[。.!！]?|教えて(ください)?[。.!！]?|どうしますか[。]?|いかがですか[。]?)\s*$")


def asks_user_in_text(text: str, call: str) -> bool:
    """A group-room message that names the user and ends with a question or a request to decide."""
    t = (text or "").strip()
    if not t or not call:
        return False
    tail = t.splitlines()[-1] if t.splitlines() else t
    base = call.strip()
    names = {base, base + "さん", base + "様"}
    return any(n and n in t for n in names) and bool(QUESTION_END.search(tail))


def waits_for_user_in_text(text: str, *calls: str) -> bool:
    """Require a direct address and a concrete reply/approval wait in that sentence."""
    names = {unicodedata.normalize("NFKC", call).strip() for call in calls if call and call.strip()}
    names = {re.sub(r"\s*(?:さん|様)$", "", name) or name for name in names}
    if not names:
        return False
    address = re.compile(r"^(?:[-*]\s+)?@?(?:" + "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True))
                         + r")(?:\s*(?:さん|様))?\s*[,、:：]\s*(.+)$", re.I)
    waiting = re.compile(
        r"(?:返事|返信|回答|判断|承認|指示|合図|go|ok).{0,50}(?:待って(?:います|おります|ます)|待ち(?:です|ます)|お待ちして(?:います|おります)|待機して(?:います|おります))"
        r"|(?:ok|go|承認|許可|合図)\s*(?:を|が)\s*(?:もらえたら|いただけたら|いただいたら|出たら)"
        r"|進めて(?:も)?(?:よければ|良ければ|よろしければ)"
        r"|\b(?:wait(?:ing)?|await(?:ing)?)\s+(?:for\s+|on\s+)?your\s+(?:reply|response|answer|decision|approval|permission|confirmation|signal|go(?:-ahead)?|ok)\b"
        r"|\b(?:once|when|after|if)\s+you\s+(?:approve|confirm|agree|decide|reply|respond|give\s+(?:the\s+)?(?:go-ahead|ok)|say\s+(?:go|ok|yes))\b"
        r"|\blet\s+(?:me|us)\s+know\s+(?:when|whether|if)\s+(?:to\s+proceed|we\s+can\s+proceed|I\s+can\s+proceed)\b", re.I)
    fenced = False
    for line in unicodedata.normalize("NFKC", text or "").splitlines():
        line = line.strip()
        if line.startswith(("```", "~~~")):
            fenced = not fenced
        if fenced or line.startswith((">", "```", "~~~")):
            continue
        line = re.sub(r"\*\*|__", "", line)
        for sentence in re.split(r"[。.!?！？]", line):
            match = address.match(sentence.strip())
            if not match:
                continue
            # Quoted UI labels or examples are not the speaker's own wait. Short
            # quoted approval words ("GO", "OK") still carry their normal meaning.
            body = re.sub(r'「[^」]*」|“[^”]*”|"[^"]*"|`[^`]*`',
                          lambda m: m[0][1:-1] if m[0][1:-1].casefold() in ("go", "ok", "yes") else "", match[1])
            if re.search(r"\b(?:not|never|no longer|don't|do not|without)\s+(?:need to\s+)?(?:wait|waiting|await|awaiting)\b", body, re.I):
                continue
            if waiting.search(body):
                return True
    return False


def is_approval(text: str) -> bool:
    """An approve answer, in any language the files know (plus a few plain words)."""
    from . import i18n
    t = (text or "").strip().lower()
    return t in {"approve", "approved", "yes", "ok", "はい"} or t in {v.lower() for v in i18n.all_values("q.approve")}


SILENT = "[SILENT]"


DRAFT_EVERY_S = 3.0


class _Batcher:
    """Collects streamed pieces and publishes them at most every `every` seconds."""

    def __init__(self, publish, every: float = 0.08):
        self.publish = publish
        self.every = every
        self.text = ""
        self.thinking = ""
        self.t = 0.0
        self.lock = threading.Lock()

    def add(self, text: str = "", thinking: str = "") -> None:
        with self.lock:
            self.text += text
            self.thinking += thinking
            if time.perf_counter() - self.t >= self.every:
                self._flush()

    def flush(self) -> None:
        with self.lock:
            self._flush()

    def _flush(self) -> None:
        if self.text or self.thinking:
            self.publish(self.text, self.thinking)
            self.text = self.thinking = ""
        self.t = time.perf_counter()


class ChatManager:
    def __init__(self, app):
        self.app = app
        self.store = app.store
        self.queues: dict[str, queue.Queue] = {}
        self.workers: dict[str, threading.Thread] = {}
        self.live: dict[str, dict] = {}          # message id -> message being written
        self.active_bots: dict[str, str] = {}
        self.cancel: dict[str, threading.Event] = {}   # room id -> cancel flag of the current turn
        self.lock = threading.Lock()
        self.answer_lock = threading.RLock()  # answers, room changes and approval actions share this order
        self.task_origins: set[str] = set()         # notes of scheduled tasks: their turns wait like SnapCode
        self.context = context_mod.load(app)
        self.dialogue = DialogueLimits(app)

    # ------------------------------------------------------------ posting

    def post(self, room_id: str, text: str, to_bot: str | None = None, device: str | None = None,
             extra: dict | None = None, carry_from: str | None = None, files: list | None = None) -> dict:
        room = self.store.room(room_id)
        if not room:
            raise LookupError("no such room")
        text = (text or "").strip()
        from . import uploads
        attached = uploads.claim(self.app, room_id, files) if files else []      # photos and files the user sent
        if not text and not attached:
            raise ValueError("empty message")
        if len(text) > 8000:
            raise ValueError("the message is too long (8000 characters at most)")
        if attached:
            extra = {**(extra or {}), "attachments": attached}
            if not text:
                extra["preview"] = ", ".join(a["name"] for a in attached)[:120]
        msg = self.store.add_message(room_id, {"role": "user", "text": text, **(extra or {})})
        self.store.event("message", actor="user", where=room_id,
                         data={"message": msg["id"], "chars": len(text), "files": len(attached)})
        self.app.bus.publish("message.new", {"room": room_id, "message": msg})
        with self.store.lock:
            current = self.store.room(room_id)
            if current:
                current["bot_only"] = False
                if not stop_requested(text):
                    current["bots_paused"] = False
                self.store.save_room(current)
                self.app.bus.publish("room.updated", {"room": current})
        if stop_requested(text):
            self.stop(room_id)
            self._dialogue_note(room_id, "stopped")
            return msg
        self.dialogue.register(msg["id"], room_id)
        for read in self.dialogue.web_reads(carry_from) if carry_from else []:   # an answer keeps them
            self.dialogue.mark_web_read(msg["id"], room_id, read)
        first = [to_bot] if to_bot else self.responders(room, text)
        for bot_id in first:
            self._enqueue(room_id, bot_id, msg["id"], 1)
        return msg

    def _dialogue_note(self, room_id, reason):
        # Order notes with deletion; never resurrect a deleted room with a late event.
        with self.answer_lock, self.store.lock:
            if self.store.room(room_id):
                self._save_dialogue_note(room_id, reason)

    def _save_dialogue_note(self, room_id, reason):
        key = "dialogue." + reason
        previous = self.store.messages(room_id, 1)
        if previous and previous[-1].get("key") == key:
            return
        msg = self.store.add_message(room_id, {"role": "system", "text": self.app.t(key), "key": key,
                                               "hint": True})
        self.app.bus.publish("message.new", {"room": room_id, "message": msg})
        self.app.bus.publish("room.updated", {"room": self.store.room(room_id)})

    def validate_bot_message(self, sender, recipients, text, room_id=""):
        if not sender or not self.store.bot(sender["id"]):
            raise ValueError("no such sender")
        if not recipients or any(not self.store.bot(b) or b == sender["id"] for b in recipients):
            raise ValueError("choose other existing bots")
        if not isinstance(text, str) or not text.strip() or len(text) > 8000:
            raise ValueError("a message needs 1 to 8000 characters")
        if room_id:
            room = self.store.room(room_id)
            if (not room or room.get("kind") != "group"
                    or not set([sender["id"], *recipients]).issubset(room.get("bots") or [])):
                raise ValueError("sender and recipients must belong to the group")
            if room.get("bots_paused"):
                raise ValueError("the user stopped bots in this room")

    def message_bot(self, sender, recipients, text, *, room_id="", name="", origin=None,
                    source_room=None, new_group=False):
        """Public delivery only. All rooms in a conversation share its original human budget."""
        with self.answer_lock, self.store.lock:
            self.validate_bot_message(sender, recipients, text, room_id)
            source = self.store.room(source_room) if source_room else self.store.dm_room(sender["id"])
            if not source or sender["id"] not in source.get("bots", []):
                raise ValueError("sender is no longer in the source room")
            origin = origin or new_id("o")
            reason = self.dialogue.reserve(origin, source["id"], sender["id"])
            if reason:
                self._dialogue_note(source["id"], reason)
                raise ValueError(self.app.t("dialogue." + reason))
            text = self.app.guard.clean(text.strip())
            if not self.dialogue.accept_text(origin, text):
                self._dialogue_note(source["id"], "repeat")
                raise ValueError(self.app.t("dialogue.repeat"))
            members = list(dict.fromkeys([sender["id"], *recipients]))
            room = self.store.room(room_id) if room_id else None
            if not room and not new_group:
                room = next((r for r in self.store.rooms() if r.get("bot_pair") == sorted(members)
                             and set(r.get("bots", [])) == set(members)), None)
                if room and room.get("bots_paused"):
                    raise ValueError("the user stopped bots in this room")
            if not room:
                room = self.app.create_group(name, members)
                room.update(bot_only=True, created_by=sender["id"])
                if not new_group:
                    room["bot_pair"] = sorted(members)
                self.store.save_room(room)
            self.dialogue.register(origin, room["id"])
            msg = self.store.add_message(room["id"], {"role": "bot", "bot": sender["id"], "text": text,
                "status": "done", "to_bots": recipients, "bot_conversation": True, "origin": origin})
            self.app.bus.publish("rooms.updated", {"rooms": self.store.rooms()})
            self.app.bus.publish("message.new", {"room": room["id"], "message": msg})
            self.app.bus.publish("room.updated", {"room": self.store.room(room["id"])})
            # Make the destination discoverable even when contact began in a one-to-one chat.
            if source["id"] != room["id"]:
                note = self.store.add_message(source["id"], {"role": "system", "key": "note.bot_contact",
                    "vars": {"name": room["name"]}, "text": self.app.t("note.bot_contact", name=room["name"]),
                    "linked_room": room["id"], "hint": True})
                self.app.bus.publish("message.new", {"room": source["id"], "message": note})
            targets = recipients + [b for b in room["bots"] if b not in recipients and b != sender["id"]]
            for bid in targets:
                self._enqueue(room["id"], bid, origin, 2, retry=True)
            return {"room": room["id"], "message": msg["id"], "visible_to_user": True}

    def mentions(self, room: dict, text: str, exclude: str | None = None) -> list[str]:
        """Bots of the room mentioned as @Name in the text, in order of appearance."""
        bots = [b for b in (self.store.bot(i) for i in room.get("bots", [])) if b and b["id"] != exclude]
        found = []
        for b in sorted(bots, key=lambda b: len(b["name"]), reverse=True):
            tail = r"(?:さん|くん|ちゃん|様|さま|-san)?" + (r"(?![\w])" if re.match(r"[\x00-\x7f]", b["name"][-1]) else "")
            for m in re.finditer(r"@" + re.escape(b["name"]) + tail, text or "", re.I):
                if not any(s <= m.start() < e for s, e, _ in found):
                    found.append((m.start(), m.end(), b["id"]))
        out = []
        for _, _, bid in sorted(found):
            if bid not in out:
                out.append(bid)
        return out

    def responders(self, room: dict, text: str) -> list[str]:
        bots = [b for b in (self.store.bot(i) for i in room.get("bots", [])) if b]
        if not bots:
            return []
        if room.get("kind") != "group":
            return [bots[0]["id"]]
        called = [bid for bid in self.mentions(room, text) if any(b['id'] == bid for b in bots)]
        # Mentioned bots go first; every member may contribute or choose [SILENT].
        return called + [b["id"] for b in sorted(bots, key=lambda b: 0 if b.get("orchestrator") else 1)
                         if b["id"] not in called]

    def _enqueue(self, room_id: str, bot_id: str, origin: str, round_: int, retry: bool = False) -> None:
        with self.lock:
            q = self.queues.setdefault(room_id, queue.Queue())
            q.put((bot_id, origin, round_, retry))
            w = self.workers.get(room_id)
            if not w or not w.is_alive():
                w = threading.Thread(target=self._worker, args=(room_id,), daemon=True, name=f"room-{room_id}")
                self.workers[room_id] = w
                w.start()

    def _worker(self, room_id: str) -> None:
        q = self.queues[room_id]
        while True:
            try:
                bot_id, origin, round_, retry = q.get(timeout=30)
            except queue.Empty:
                with self.lock:
                    if q.empty():
                        self.workers.pop(room_id, None)
                        return
                continue
            try:
                if round_ <= 1 and not retry and self.already_answered(room_id, bot_id):
                    continue                     # an earlier turn already answered these messages (QA B-5)
                if not self.dialogue.active(origin, room_id) or self.dialogue.waiting_for_user(origin):
                    continue
                saved = self.run_turn(room_id, bot_id, round_, scheduled=origin in self.task_origins, origin=origin)
                self.task_origins.discard(origin)
                if saved:
                    self._after_reply(room_id, saved, origin, round_)
                    summaries = getattr(self.app, "summaries", None)
                    if summaries:
                        summaries.maybe_schedule(room_id)
            except Exception as e:  # noqa: BLE001 - a broken turn must not kill the room
                self.app.log(f"turn failed in {room_id}: {e!r}")
            finally:
                q.task_done()

    def _after_reply(self, room_id: str, saved: dict, origin: str, round_: int) -> None:
        """Invite relevant contributions; reservations cap the whole conversation across rooms."""
        room = self.store.room(room_id)
        if room and saved.get("needs_you"):
            self.dialogue.wait_for_user(origin, room_id)
        if (not room or room.get("kind") != "group" or saved.get("status") not in ("done", "silent")
                or not self.dialogue.active(origin, room_id) or self.dialogue.waiting_for_user(origin)
                or saved.get("question_id") or saved.get("needs_you")):
            return
        if saved.get("status") == "silent" and round_ <= 1 and self._all_silent(room_id, origin):
            b = self.store.bot(saved.get("bot") or "") or {"name": "Bot"}
            vars = {"bot": b["name"]}
            note = self.store.add_message(room_id, {"role": "system", "text": self.app.t("note.all_silent", **vars),
                                                    "key": "note.all_silent", "vars": vars, "hint": True})
            self.app.bus.publish("message.new", {"room": room_id, "message": note})
        if saved.get("status") == "silent":
            return
        for bid in self.responders(room, saved.get("text") or ""):
            if bid == saved.get("bot"):
                continue
            with self.lock:
                q = self.queues.get(room_id)
                if q and any(item[0] == bid and item[1] == origin for item in list(q.queue)):
                    continue
            self._enqueue(room_id, bid, origin, round_ + 1)

    def _all_silent(self, room_id: str, origin: str) -> bool:
        """True when every reply to this user message so far stayed silent and nothing else is queued for it."""
        msgs = self.store.messages(room_id, 40)
        ids = [m.get("id") for m in msgs]
        if origin not in ids:
            return False
        after = msgs[ids.index(origin) + 1:]
        queued = any(item[1] == origin for item in list(self.queues[room_id].queue))
        return not queued and after and all(m.get("role") == "bot" and m.get("status") == "silent" for m in after)

    def already_answered(self, room_id: str, bot_id: str) -> bool:
        """True when this bot's latest finished reply comes after every user message and note in the room."""
        last_input = last_reply = -1
        for i, m in enumerate(self.store.messages(room_id, 60)):
            if m.get("role") == "user" or (m.get("role") == "system" and not m.get("work_notice")):
                last_input = i                   # a SnapCode start note is not new input for the bots
            elif m.get("role") == "bot" and m.get("bot") == bot_id and m.get("status") in ("done", "silent"):
                last_reply = i
        return last_reply >= 0 and last_reply > last_input

    def retry(self, room_id: str, message_id: str) -> dict:
        """Run the turn again for a reply that failed or was stopped (the "Try again" button, QA B-3)."""
        room = self.store.room(room_id)
        if not room:
            raise LookupError("no such room")
        m = next((x for x in self.store.messages(room_id) if x.get("id") == message_id), None)
        if not m or m.get("role") != "bot":
            raise LookupError("no such reply")
        if m.get("status") not in ("error", "stopped"):
            raise ValueError("only a failed or stopped reply can be tried again")
        if m.get("retried"):
            raise ValueError("this reply was already tried again")
        if m.get("bot") not in room.get("bots", []):
            raise ValueError("this bot is no longer in the room")
        if self.busy(room_id):
            raise ValueError("a reply is being written in this room; try again when it is done")
        m = {**m, "retried": True}
        self.store.add_message(room_id, m, touch_room=False)
        self.app.bus.publish("message.done", {"room": room_id, "message": m})
        with self.store.lock:
            current = self.store.room(room_id)
            current["bots_paused"] = False
            self.store.save_room(current)
        # Retrying is not a fresh human instruction. Keep external-read provenance even
        # though the explicit retry starts a new budget and leaves stopped chains stopped.
        origin = new_id("o")
        self.dialogue.register(origin, room_id)
        for read in self.dialogue.web_reads(m.get("origin")):
            self.dialogue.mark_web_read(origin, room_id, read)
        self._enqueue(room_id, m["bot"], origin, 1, retry=True)
        return m

    def recover(self, resume: bool = False) -> None:
        """After a restart, replies that were being written cannot continue: mark them interrupted (QA B-2).
        resume (a restart by an update): then write each one again, as the "Try again" button does."""
        cut = []
        for room in self.store.rooms():
            for m in self.store.messages(room["id"], 50):
                if m.get("status") == "streaming" and m.get("id") not in self.live:
                    d = self.store.draft(m["id"]) or {}
                    self.store.add_message(room["id"], {**m, "text": d.get("text") or m.get("text") or "",
                                                        "thinking": d.get("thinking") or m.get("thinking") or "",
                                                        "status": "error", "error_code": "restart",
                                                        "error": "SnapLab restarted while this reply was being written"},
                                           touch_room=False)
                    self.store.drop_draft(m["id"])
                    cut.append((room["id"], m["id"]))
        for room_id, message_id in cut if resume else []:
            try:
                self.retry(room_id, message_id)
            except (LookupError, ValueError) as e:     # e.g. the bot left the room, or another reply there came first
                self.app.log(f"could not write reply {message_id} again after the restart: {e}")

    def stop_bot(self, room_id: str, bot_id: str) -> None:
        # Queued turns are rejected by _worker; interrupt only this bot's active turn.
        with self.store.lock:
            if self.active_bots.get(room_id) == bot_id:
                event = self.cancel.get(room_id)
                if event:
                    event.set()

    def stop(self, room_id: str) -> bool:
        stopped = False
        with self.store.lock:
            for rid in self.dialogue.stop(room_id):
                ev = self.cancel.get(rid)
                q = self.queues.get(rid)
                if q:
                    while not q.empty():
                        try:
                            q.get_nowait()
                            q.task_done()
                        except queue.Empty:
                            break
                if ev:
                    ev.set()
                    stopped = True
        return stopped

    def busy(self, room_id: str) -> bool:
        return room_id in self.cancel

    def live_messages(self, room_id: str) -> list[dict]:
        return [self._public(m) for m in list(self.live.values()) if m["room"] == room_id]

    @staticmethod
    def _public(m: dict) -> dict:
        return {k: v for k, v in m.items() if not k.startswith("_")}

    # ------------------------------------------------------------ one bot turn

    def build_messages(self, room: dict, bot: dict, lang: str) -> list[dict]:
        return self.context.build(room, bot, lang)[0]

    def run_turn(self, room_id: str, bot_id: str, round_: int = 1, scheduled: bool = False,
                 origin: str | None = None) -> dict | None:
        app, st = self.app, self.store
        with self.answer_lock, st.lock:
            room, bot = st.room(room_id), st.bot(bot_id)
            if not room or not bot or bot_id not in room.get("bots", []):
                return None
            origin = origin or next((m["id"] for m in reversed(st.messages(room_id, 60))
                                     if m.get("role") == "user"), new_id("o"))
            self.dialogue.register(origin, room_id)
            if not self.dialogue.active(origin, room_id):
                return None
            if room.get("kind") == "group" or round_ > 1:
                reason = self.dialogue.reserve(origin, room_id, bot_id)
                if reason:
                    self._dialogue_note(room_id, reason)
                    return None
            cancel = threading.Event()
            self.cancel[room_id] = cancel
            self.active_bots[room_id] = bot_id
        lang = st.settings().get("lang") or "en"
        msg = {"id": new_id("m"), "room": room_id, "role": "bot", "bot": bot_id, "ts": now_iso(), "text": "",
               "thinking": "", "tools": [], "status": "streaming", "_t0": time.perf_counter(), "_sep": False}
        msg["origin"] = origin
        msg["bot_conversation"] = round_ > 1 or room.get("bot_only", False)
        self.live[msg["id"]] = msg
        st.add_message(room_id, {k: v for k, v in self._public(msg).items() if k != "room"}, touch_room=False)
        app.bus.publish("message.start", {"room": room_id, "message": self._public(msg)})

        msg["_saved"] = time.perf_counter()

        def publish(text, thinking):
            app.bus.publish("message.delta", {"room": room_id, "id": msg["id"], "text": text, "thinking": thinking})
            if time.perf_counter() - msg["_saved"] >= DRAFT_EVERY_S:      # keep what arrived if SnapLab restarts (L-1)
                msg["_saved"] = time.perf_counter()
                try:
                    st.save_draft(msg["id"], msg["text"], msg["thinking"])
                except OSError:
                    pass
        batch = _Batcher(publish)

        def on_content(piece):
            if "thinking_s" not in msg and piece.strip():     # thinking time starts when the model got a slot (B-1)
                msg["thinking_s"] = round(time.perf_counter() - msg.get("_t_ready", msg["_t0"]), 1)
            if msg["_sep"] and piece.strip():
                if msg["text"].strip():
                    piece = "\n\n" + piece.lstrip()
                msg["_sep"] = False
            msg["text"] += piece
            if room.get("kind") != "group" and not msg["bot_conversation"]:
                batch.add(text=piece)

        def on_reasoning(piece):
            if "thinking_started" not in msg:
                msg["thinking_started"] = now_iso()
            msg["thinking"] += piece
            batch.add(thinking=piece)

        def on_step(i):
            if i > 0:
                msg["_sep"] = True

        def on_chip(chip):
            batch.flush()
            for i, c in enumerate(msg["tools"]):
                if c["id"] == chip["id"]:
                    msg["tools"][i] = dict(chip)
                    break
            else:
                msg["tools"].append(dict(chip))
            app.bus.publish("message.tool", {"room": room_id, "id": msg["id"], "tool": chip})
            if chip.get("status") != "running":
                st.event("tool", actor=bot_id, where=room_id, visible_to=bot_id,
                         data={"name": chip.get("name"), "label": chip.get("label"), "status": chip.get("status")})

        def on_wait(position):
            if position == 0:
                msg["_t_ready"] = time.perf_counter()
                msg["waited_s"] = round(msg["_t_ready"] - msg["_t0"], 1)
            app.bus.publish("message.wait", {"room": room_id, "id": msg["id"], "position": position})

        box = Toolbox(app, bot, room=room, lang=lang, on_chip=on_chip, origin=origin, turn=msg["id"],
                      cancel=lambda: cancel.is_set() or not self.dialogue.active(origin, room_id))
        messages, report = self.context.build(room, bot, lang)
        if box.report_only:
            limit = ' You have continued this session without the user as many times as allowed; report what remains and wait for the user.' if box.result_session and app.sessions.auto_continue_limit() else ''
            messages[-1] = {**messages[-1], 'content': messages[-1]['content'] + '\n\n[SnapLab result report] Explain only the result, tests, attachments and remaining issues in the room language. Do not run tools, start work, repeat verification or name the execution harness unless the user asked about it.' + limit}
        elif box.result_session:
            messages[-1] = {**messages[-1], 'content': messages[-1]['content'] + (
                '\n\n[SnapLab result report] This result is for work you started. If the outcome the user asked for is complete, '
                'report the result, tests, attachments and remaining issues briefly and stop. If it is not complete and the next '
                'step is clear, continue now without asking the user: call send_to_session on session ' + box.result_session +
                ' with a concrete next instruction (finish the remaining part, or fix the reported failure), or start other '
                'sessions when that fits better (work in parallel, or a fresh session with from_session=' + box.result_session +
                ' for an independent review). Then say in one or two lines what you did. Checks that already passed need not be '
                'repeated. Use ask_user only for a decision only the user can make. If the same failure came back after your last '
                'instruction, change the approach instead of resending it. Write in the room language. '
                + (f'Continuations left without the user: {int(box.auto_left)}.' if box.auto_left != float('inf') else ''))}
        # Files the user sent (uploads.py): images as images, text as text. Last, so the notes above join plain text.
        from . import uploads
        ccfg_files = app.cfg.get("context") or {}
        uploads.expand(app, messages, vision=uploads.can_see(app, bot), images=int(ccfg_files.get("file_images", 6)),
                       text_tokens=int(ccfg_files.get("file_tokens", 6000)))
        last_user = next((m.get("text") or "" for m in reversed(st.messages(room_id, 30)) if m.get("role") == "user"), "")

        def language_check(text: str):
            if prompts.wrong_language(text, lang, last_user):
                name = prompts.lang_name(lang)
                msg["_rewritten"] = True
                return (f"[SnapLab check] This room speaks {name} and the user wrote in {name}, but your reply is in "
                        f"another language. Write your whole reply again in {name}.")
            return None
        st.save_view(msg["id"], {"bot": bot_id, "room": room_id, "ts": msg["ts"],
                                 "sections": report.pop("sections", []), "history": report.pop("history_view", []),
                                 "report": dict(report)})
        msg["context"] = report
        cfg = app.cfg
        try:
            ccfg = {**context_mod.DEFAULTS, **(cfg.get("context") or {})}
            limit = int(app.models.spec(bot.get("model"), "chat").get("context_limit") or 131072)
            schemas = box.schemas()
            if limit <= tools_mod.SMALL_WINDOW:
                schemas = tools_mod.compact_schemas(schemas)
            res = run_loop(app.models.get(bot.get("model"), "chat"), messages, schemas, box, effort=bot.get("first_effort", cfg["api"]["chat_effort"]),
                           max_steps=step_cap(cfg["chat"].get("max_steps")), max_tokens=int(cfg["chat"]["max_tokens"]),
                           source="chat", caller_id=room_id, cancel=cancel.is_set, on_reasoning=on_reasoning, on_content=on_content,
                           on_step=on_step, kind="session" if scheduled else
                           ("followup" if msg["bot_conversation"] else "chat"),
                           on_wait=on_wait,
                           effort_after_tools=bot.get("after_tools_effort", cfg["api"].get("after_tools_effort")) or None, check_claims=True,
                           extra_claims=mcp_client.claim_rules(getattr(app, "plugin_tools", None)),
                           compact_at=int(report.get("compact_at") or ccfg["summarize_tokens"]), final_check=language_check)
            if hasattr(self.context, "learn"):
                self.context.learn(room_id, report.get("total", 0), res.first_input_tokens)
            for c_ in res.claims:
                st.event("claim.unbacked", actor=bot_id, where=room_id, data={"tool": c_, "message": msg["id"]})
            batch.flush()
            msg["status"] = "done"
            msg["text"] = (res.text if msg.get("_rewritten") else (msg["text"] or res.text)).strip()
            # short remarks between tool calls ("Let me check first.") go to the thinking, the last step is the answer
            steps = [s_.strip() for s_ in res.step_texts]
            if len(steps) > 1 and steps[-1] and len(steps[-1]) >= 20:
                between = [s_ for s_ in steps[:-1] if s_]
                if between and all(len(s_) < 240 for s_ in between):
                    msg["thinking"] = (msg["thinking"] + "\n\n" + "\n\n".join(between)).strip()
                    msg["text"] = steps[-1]
            msg["usage"] = {"input_tokens": res.input_tokens, "output_tokens": res.output_tokens, "calls": res.calls}
        except Cancelled:
            batch.flush()
            msg["status"] = "stopped"
        except ApiError as e:                       # includes StreamCut: the text that arrived is kept (B-4)
            batch.flush()
            msg["status"] = "error"
            msg["error_code"] = error_code(e)
            msg["error"] = e.short()
            app.note_model_error(msg["error_code"])
            app.act("error", "act.reply_failed", ref=room_id, bot=bot["name"], reason="@err.code." + msg["error_code"])
        except Exception as e:  # noqa: BLE001
            batch.flush()
            msg["status"] = "error"
            msg["error_code"] = "internal"
            msg["error"] = f"{type(e).__name__}: {e}"[:300]
            app.log(f"turn error: {e!r}")
        finally:
            self.cancel.pop(room_id, None)
            self.active_bots.pop(room_id, None)
        msg["thinking"] = msg["thinking"][-20000:]
        msg["elapsed_s"] = round(time.perf_counter() - msg["_t0"], 1)
        final = {k: v for k, v in msg.items() if not k.startswith("_")}
        final.pop("room", None)
        if cancel.is_set() or not self.dialogue.active(origin, room_id):
            final["status"] = "stopped"
        if (room.get("kind") == "group" or msg["bot_conversation"]) and final["status"] == "done":
            text = final.get("text") or ""
            if text.strip() == SILENT or (text.strip().startswith(SILENT) and len(text.strip()) < len(SILENT) + 3):
                final["status"] = "silent"          # the bot chose to stay quiet; the phone hides it
            elif not box.asked and text.strip() and not self.dialogue.accept_text(origin, text):
                final["status"] = "silent"
                final["suppressed"] = "repeat"
                self._dialogue_note(room_id, "repeat")
            elif re.search(r"@(user|you)(?![\w])", text, re.I) or (bot.get("call_user") and re.search(
                    r"@" + re.escape(bot["call_user"]) + r"(?![\w])", text, re.I)):
                final["needs_you"] = True
            elif not box.asked and asks_user_in_text(text, bot.get("call_user") or st.settings().get("user_name") or ""):
                final["needs_you"] = True          # measured: bots sometimes ask the user in the text instead of a card
                st.event("question.in_text", actor=bot_id, where=room_id, data={"message": final["id"]})
            elif not box.asked and waits_for_user_in_text(text, bot.get("call_user"), st.settings().get("user_name")):
                final["needs_you"] = True
                st.event("waiting.in_text", actor=bot_id, where=room_id, data={"message": final["id"]})
        if final["status"] == "error" and not (final.get("text") or "").strip():
            final["preview"] = app.t("err.code." + (final.get("error_code") or "internal"))
        # Keep the latest card snapshot and its notification in the same order as
        # answers and membership changes. A cancelled card must never reopen on the phone.
        with self.answer_lock, st.lock:
            if box.asked:
                current = st.question(box.asked["id"])
                if current:
                    current["message_id"] = final["id"]
                    st.save_question(current)
                    final["question_id"] = current["id"]
                    final["question"] = self.question_view(current)
                    final["pending_ok"] = current.get("kind") == "approval" and current.get("status") == "open"
                    if final["pending_ok"]:
                        prop = current.get("prop") or {}
                        why = app.t("prop.why." + (prop.get("why") or "policy"), **(prop.get("why_vars") or {}))
                        final["text"] = (final.get("text", "").strip() + "\n\n" + why).strip()
            saved = st.add_message(room_id, final)
            st.drop_draft(saved["id"])
            st.event("message", actor=bot_id, where=room_id,
                     data={"message": saved["id"], "status": saved["status"], "context": report,
                           "usage": saved.get("usage"), "tools": [c.get("name") for c in saved.get("tools") or []]})
            self.live.pop(msg["id"], None)
            app.bus.publish("message.done", {"room": room_id, "message": saved})
            # A deletion must not publish its room list between this read and event.
            current_room = st.room(room_id)
            if current_room:
                app.bus.publish("room.updated", {"room": current_room})
        if current_room and saved.get("needs_you"):
            app.push.notify("needs_you", bot["name"], saved["text"][:160], url=f"/#/room/{room_id}", tag=f"room-{room_id}",
                            dedupe=f"needs:{saved['id']}")
        return saved

    # ------------------------------------------------------------ questions

    @staticmethod
    def question_view(q: dict) -> dict:
        return {k: q.get(k) for k in ("id", "kind", "question", "options", "status", "answer", "bot", "room", "detail",
                                      "prop", "created", "answered_at")}

    def create_question(self, room: dict, bot: dict, *, question: str, options: list | None = None,
                        kind: str = "question", action: dict | None = None, detail: str = "",
                        prop: dict | None = None) -> dict:
        # The lock covers saving AND publishing: removal cannot publish cancellation first.
        with self.answer_lock:
            return self._create_question(room, bot, question=question, options=options, kind=kind,
                                         action=action, detail=detail, prop=prop)

    def _create_question(self, room: dict, bot: dict, *, question: str, options: list | None = None,
                         kind: str = "question", action: dict | None = None, detail: str = "",
                         prop: dict | None = None) -> dict:
        """A question card, or (kind "approval") a proposal card. A proposal carries `prop`: the title's key and
        values, labelled fields and why it was asked, so the phone shows it in its own language."""
        opts = [" ".join(str(o).split())[:60] for o in (options or []) if str(o).strip()][:5]
        if kind == "approval":
            opts = list(APPROVAL_OPTIONS)          # canonical answers; the phone shows them translated
        q = {"id": new_id("q"), "kind": kind, "room": room["id"], "bot": bot["id"], "question": question.strip()[:600],
             "options": opts, "status": "open", "answer": None, "created": now_iso()}
        if action:
            q["action"] = action
            if kind == "approval":
                self.app.ledger.bind_approval(q)
        if prop:
            q["prop"] = prop
        if detail and detail.strip() and " ".join(detail.split()) not in " ".join(q["question"].split()):
            q["detail"] = detail.strip()[:8000]        # the full instruction, shown on the card before approving
        with self.store.lock:
            current = self.store.room(room["id"])
            if not current or bot["id"] not in current.get("bots", []):
                raise ValueError("this bot is no longer in an active room")
            self.store.save_question(q)
        app = self.app
        if kind == "approval":
            what = " · ".join(str(v) for _, v in (prop or {}).get("fields", [])[:2] if v) or q["question"]
            app.act("question", "act.approval", ref=q["id"], bot=bot["name"], what=what[:120])
            title, body = app.t("push.approval", bot=bot["name"]), (q["question"] + (" · " + what if what != q["question"] else ""))
            reason = app.t("prop.why." + ((prop or {}).get("why") or "policy"), **((prop or {}).get("why_vars") or {}))
            body = reason + " " + body
        else:
            app.act("question", "act.question", ref=q["id"], bot=bot["name"], question=q["question"][:120])
            title, body = app.t("push.asks", bot=bot["name"]), q["question"]
        app.bus.publish("question.new", {"question": self.question_view(q)})
        self.store.event("card.open", actor=bot["id"], where=room["id"], data={"card": q["id"], "kind": kind})
        app.push.notify("needs_you", title, body[:160], url=f"/#/room/{room['id']}", tag=q["id"], dedupe=q["id"])
        return q

    def _settle_chip(self, q: dict, text: str) -> None:
        """The tool chip that opened a card turns into a done (or declined) chip once the card is answered."""
        mid = q.get("message_id")
        if not mid:
            return
        m = next((x for x in self.store.messages(q["room"], 100) if x.get("id") == mid), None)
        if not m or not m.get("tools"):
            return
        approved = q.get("kind") != "approval" or is_approval(text)
        changed = False
        for c in m["tools"]:
            if c.get("status") == "waiting":
                c["status"] = "ok" if approved else "declined"
                changed = True
        if changed:
            m["question"] = self.question_view(q)
            m["pending_ok"] = False
            self.store.add_message(q["room"], m, touch_room=False)
            self.app.bus.publish("message.done", {"room": q["room"], "message": m})

    def answer(self, qid: str, text: str, carry_from: str | None = None) -> dict:
        text = (text or "").strip()
        if not text:
            raise ValueError("empty answer")
        with self.answer_lock:
            return self._answer(qid, text, carry_from)

    def _answer(self, qid: str, text: str, carry_from: str | None = None) -> dict:
        with self.store.lock:                     # a card is answered exactly once, even if two answers race
            q = self.store.question(qid)
            if not q:
                raise LookupError("no such question")
            if q.get("status") != "open":
                raise LookupError("this question was already answered")
            room = self.store.room(q.get("room"))
            if not room or q.get("bot") not in room.get("bots", []):
                raise LookupError("this bot is no longer in an active room")
            q.update(status="answered", answer=text[:1000], answered_at=now_iso())
            self.store.save_question(q)
        self.app.bus.publish("question.updated", {"question": self.question_view(q)})
        self._settle_chip(q, text)
        if q.get("kind") == "approval":
            # the card itself shows the answer, so no extra bubble; the note says what happened (in any language)
            key, vars = self.app.run_approval(q, is_approval(text))
            note = {"role": "system", "text": self.app.t(key, **vars), "key": key, "vars": vars, "card": q["id"]}
            if vars.get("session"):
                note["session"] = vars["session"]
            m = self.store.add_message(q["room"], note)
            self.app.bus.publish("message.new", {"room": q["room"], "message": m})
            room = self.store.room(q["room"])
            if room:
                self.app.bus.publish("room.updated", {"room": room})
            return q
        self.post(q["room"], text, to_bot=q["bot"], extra={"answer_to": (q.get("question") or "")[:300]},
                  carry_from=carry_from)
        return q
