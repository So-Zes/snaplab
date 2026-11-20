"""The browser panel: a SnapCode session's browser, shown live in SnapLab and shared by the agent and the user.

The browser always runs in the session's job (the WSL workspace, the local `snaplab` account or the Mac VM), never
on this PC. While a turn runs, SnapLab attaches to the agent's own Chrome; between turns it runs Chrome alone in
the same sandbox with the same profile ("host"), so a sign-in made in the panel is there in the agent's next turn.

The DevTools protocol travels as JSON lines: this module <-> SSH <-> remote_worker.live <-> the job's
.snaplab/live.sock <-> browser.mjs --live <-> Chrome. The debugging port never leaves the job. Frames come from
Page.startScreencast (JPEG, only while someone watches) and reach the page as server-sent events; taps, keys and
text go back as Input.* commands, accepted only while the user holds control (always between turns; during a turn
after "Take over"). browser.mjs and desktop.py wait while the user holds control. Typed text is never logged,
archived or put on the event bus.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import secrets
import subprocess
import threading
import time
from pathlib import Path

IDLE_CLOSE_S = 120          # the browser closes this long after the last viewer left
FRAME_MAX = 1280            # largest frame side in pixels
FRAME_GAP_S = 0.08          # at most about 12 frames a second (each frame is acknowledged no sooner)
MAX_LINE = 32 * 1024 * 1024
HELPERS = ("capture.py", "capture.mjs", "browser.mjs", "keep.py", "desktop.py")
MOUSE = {"down": "mousePressed", "up": "mouseReleased", "move": "mouseMoved"}
BUTTONS = ("none", "left", "middle", "right")
SCHEMES = ("http", "https", "about")


class BrowserError(ValueError):
    pass


def _num(v, lo=0.0, hi=1.0) -> float:
    if not isinstance(v, (int, float)) or isinstance(v, bool) or v != v:
        raise BrowserError("invalid number")
    return min(hi, max(lo, float(v)))


def _mods(v) -> int:
    return int(v) & 15 if isinstance(v, int) and not isinstance(v, bool) else 0


def page_url(text: str) -> str:
    """What the address bar accepts: http(s) and about: addresses; a bare host gets https://."""
    url = str(text or "").strip()
    if not url or len(url) > 4000 or any(ord(c) < 32 for c in url):
        raise BrowserError("enter an address")
    if not re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", url):
        url = "https://" + url
    if url.split(":", 1)[0].lower() not in SCHEMES:
        raise BrowserError("only http, https and about: addresses")
    return url


class LiveBrowser:
    def __init__(self, manager, sid: str):
        self.m, self.app, self.sid = manager, manager.app, sid
        self.lock = threading.RLock()
        self.cond = threading.Condition(self.lock)
        self.write_lock = threading.Lock()
        self.proc = None
        self.mode = None                  # "host" | "turn"
        self.status = "idle"              # idle | starting | waiting | ready | error
        self.error = None
        self.viewers = 0
        self.left_at = time.monotonic()
        self.thread = None
        self.pending: dict[int, list] = {}
        self.next_id = 0
        self.targets: dict[str, dict] = {}
        self.active = None
        self.session = None
        self.casting = False
        self.frame = None                 # (seq, base64 jpeg, {"w", "h"} in CSS pixels)
        self.seq = 0
        self.version = 0
        self.holder = "agent"
        self.dialog = None
        self.last_ack = 0.0
        self.ending = False
        self.host_until = 0.0             # the workspace said no turn runs: host the browser for now
        self.phones: list[tuple[int, int]] = []    # CSS sizes of phone viewers: pages are laid out for a phone
        self.emulated = None
        self.host_done = threading.Event()         # the host job ended (Chrome has written its profile)

    # ------------------------------------------------------------ state for the page

    def view(self) -> dict:
        with self.lock:
            tabs = [{"id": tid, "url": t.get("url", ""), "title": t.get("title", ""), "active": tid == self.active}
                    for tid, t in self.targets.items()]
            cur = self.targets.get(self.active) or {}
            return {"status": self.status, "mode": self.mode, "holder": self.holder, "tabs": tabs,
                    "url": cur.get("url", ""), "title": cur.get("title", ""), "dialog": self.dialog,
                    "error": self.error, "version": self.version, "turn": self.m.turn_running(self.sid),
                    "can_input": self.status == "ready" and self.holder == "user"}

    def _changed(self) -> None:
        with self.lock:
            self.version += 1
            self.cond.notify_all()
        try:
            self.app.bus.publish("browser.state", {"session": self.sid, "status": self.status, "mode": self.mode,
                                                   "holder": self.holder})
        except Exception:  # noqa: BLE001 - the panel's own stream carries the full state
            pass

    # ------------------------------------------------------------ viewers

    def add_viewer(self, phone: tuple[int, int] | None = None) -> None:
        with self.lock:
            self.viewers += 1
            if phone:
                self.phones.append(phone)
            first = self.viewers == 1
            if self.thread is None:
                self.thread = threading.Thread(target=self._run, name="browser-" + self.sid, daemon=True)
                self.thread.start()
        if first and self.status == "ready":
            threading.Thread(target=self._cast, args=(True,), daemon=True).start()

    def remove_viewer(self, phone: tuple[int, int] | None = None) -> None:
        with self.lock:
            self.viewers = max(0, self.viewers - 1)
            if phone in self.phones:
                self.phones.remove(phone)
        threading.Thread(target=self._fit, daemon=True).start()
        with self.lock:
            self.left_at = time.monotonic()
            last = self.viewers == 0
        if last and self.status == "ready":
            threading.Thread(target=self._cast, args=(False,), daemon=True).start()

    def wait(self, seq: int, version: int, timeout: float):
        """Until a newer frame or state than (seq, version), or the timeout. Returns (frame or None, changed)."""
        with self.lock:
            self.cond.wait_for(lambda: self.seq > seq or self.version > version, timeout)
            return (self.frame if self.frame and self.frame[0] > seq else None), self.version > version

    # ------------------------------------------------------------ connection

    def _wanted(self) -> bool:
        with self.lock:
            return self.viewers > 0 or time.monotonic() - self.left_at < IDLE_CLOSE_S

    def _run(self) -> None:
        failures = 0
        try:
            # Connect only while someone watches; a connection itself stays IDLE_CLOSE_S after they leave.
            while self.viewers > 0 and not self.m.stopping:
                started = time.monotonic()
                try:
                    self._connect()
                except Exception as e:  # noqa: BLE001
                    self.app.log(f"browser {self.sid}: {type(e).__name__}")
                    with self.lock:
                        self.status, self.error = "error", "the workspace could not be reached"
                    self._changed()
                failures = failures + 1 if time.monotonic() - started < 10 else 0
                if self.viewers == 0:
                    break
                time.sleep(min(30, 1.5 * (failures + 1)))
        finally:
            with self.lock:
                self.thread = None
                self.status, self.mode, self.session, self.casting = "idle", None, None, False
                self.targets, self.active, self.dialog = {}, None, None
                again = self.viewers > 0 and not self.m.stopping     # someone opened the panel as this loop ended
                if again:
                    self.thread = threading.Thread(target=self._run, name="browser-" + self.sid, daemon=True)
                    self.thread.start()
            self._changed()

    def _connect(self) -> None:
        turn = self.m.turn_running(self.sid) and time.monotonic() > self.host_until
        with contextlib.ExitStack() as stack:
            cfg, local_port, remote_port, token = self.m.connect_config(self.sid, stack, host=not turn)
            from .harnesses import remote
            req = {"sid": self.sid, "mode": "live", "host": not turn, "wait_s": 120,
                   "screen": str(cfg.get("desktop_size") or ""), "token": token, "relay_port": remote_port,
                   "host_addresses": cfg.get("host_addresses", []),
                   "helpers": {n: (Path(remote.__file__).with_name(n)).read_text(encoding="utf-8") for n in HELPERS}}
            env = {k: v for k, v in os.environ.items() if k.upper() in
                   {"PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "USERPROFILE", "HOME", "TEMP", "TMP", "PROGRAMDATA"}}
            proc = subprocess.Popen(remote.ssh_command(cfg, local_port, remote_port), stdin=subprocess.PIPE,
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env,
                                    creationflags=0x08000000 if os.name == "nt" else 0)
            with self.lock:
                self.proc, self.mode, self.status, self.error = proc, "turn" if turn else "host", "starting", None
                self.holder = "agent" if turn else "user"
                self.ending = False
                self.host_done = threading.Event()
            self._changed()
            stack.callback(self._drop, proc)
            proc.stdin.write((json.dumps(req) + "\n").encode("utf-8"))
            proc.stdin.flush()
            idle_check = threading.Thread(target=self._watch_idle, args=(proc,), daemon=True)
            idle_check.start()
            for line in iter(lambda: proc.stdout.readline(MAX_LINE), b""):
                try:
                    item = json.loads(line)
                except ValueError:
                    continue
                kind = item.get("kind")
                if kind == "cdp":
                    self._on_cdp(item.get("m") or {})
                elif kind == "connected":
                    threading.Thread(target=self._ready, daemon=True).start()
                elif kind == "waiting":
                    with self.lock:
                        self.status = "waiting"
                    self._changed()
                elif kind == "busy":
                    self.m.turn_started_elsewhere(self.sid)
                elif kind == "noturn":
                    with self.lock:
                        self.host_until = time.monotonic() + 60
                    self.m.busy.pop(self.sid, None)
                elif kind == "host_ended":
                    self.host_done.set()
                elif kind == "disconnected":            # the browser side closed: nothing will answer any more
                    with self.lock:
                        for waiter in self.pending.values():
                            waiter[2] = "the browser closed"
                            waiter[0].set()
                elif kind in ("nobrowser", "nojob", "host_failed"):
                    with self.lock:
                        self.error = {"nobrowser": "the browser did not start", "nojob": "this session has no workspace yet",
                                      "host_failed": "the browser could not start in the workspace"}[kind]
                    self._changed()
            proc.wait(timeout=30)

    def _drop(self, proc) -> None:
        for stream in (proc.stdin, proc.stdout):
            try:
                stream.close()
            except OSError:
                pass
        if proc.poll() is None:
            try:
                proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=10)
        with self.lock:
            if self.proc is proc:
                self.proc = None
            for waiter in self.pending.values():
                waiter[2] = "closed"
                waiter[0].set()
            self.pending.clear()
            self.status, self.session, self.casting, self.dialog = "starting", None, False, None

    def _watch_idle(self, proc) -> None:
        """Close the connection (and a host browser) when nobody has watched for IDLE_CLOSE_S."""
        while proc.poll() is None and not self.m.stopping:
            time.sleep(1)
            # A turn needs the job: the browser the panel runs between turns gives way (the turn waits for it).
            if not self._wanted() or (self.mode == "host" and self.m.turn_running(self.sid) and time.monotonic() > self.host_until):
                self.end(wait=False)
                return

    def end(self, wait: bool = True) -> None:
        """Close this panel's connection. A host browser is closed with Browser.close so it saves cookies and tabs."""
        with self.lock:
            proc, mode = self.proc, self.mode
            self.ending = True
            if wait:
                self.left_at = 0.0
        if proc is None:
            return
        if mode == "host" and self.status == "ready":
            try:
                self.cmd("Browser.close", timeout=10)
            except Exception:  # noqa: BLE001
                pass
            # Chrome writes cookies and tabs as it exits, and the host job ends once it has. Closing the connection
            # earlier would stop the job (and Chrome with it) before that.
            self.host_done.wait(30)
        try:
            proc.stdin.close()
        except OSError:
            pass
        if wait:
            try:
                proc.wait(timeout=60)
            except subprocess.TimeoutExpired:
                proc.kill()

    # ------------------------------------------------------------ DevTools

    def _send(self, msg: dict) -> None:
        with self.lock:
            proc = self.proc
        if proc is None:
            raise BrowserError("the browser is not connected")
        data = (json.dumps(msg, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        with self.write_lock:
            try:
                proc.stdin.write(data)
                proc.stdin.flush()
            except (OSError, ValueError):
                raise BrowserError("the browser is not connected") from None

    def cmd(self, method: str, params: dict | None = None, session: str | None = None, timeout: float = 15):
        with self.lock:
            self.next_id += 1
            mid = self.next_id
            waiter = [threading.Event(), None, None]
            self.pending[mid] = waiter
        msg = {"id": mid, "method": method, "params": params or {}}
        if session:
            msg["sessionId"] = session
        try:
            self._send(msg)
            if not waiter[0].wait(timeout):
                raise BrowserError(method + " did not answer")
        finally:
            with self.lock:
                self.pending.pop(mid, None)
        if waiter[2]:
            raise BrowserError(str(waiter[2])[:200])
        return waiter[1] or {}

    def _on_cdp(self, m: dict) -> None:
        if "id" in m:
            with self.lock:
                waiter = self.pending.get(m.get("id"))
            if waiter:
                waiter[1] = m.get("result")
                waiter[2] = (m.get("error") or {}).get("message") if m.get("error") else None
                waiter[0].set()
            return
        method, p, where = m.get("method"), m.get("params") or {}, m.get("sessionId")
        if method == "Page.screencastFrame" and where and where == self.session:
            meta = p.get("metadata") or {}
            with self.lock:
                self.seq += 1
                self.frame = (self.seq, p.get("data") or "", {"w": meta.get("deviceWidth"), "h": meta.get("deviceHeight")})
                self.cond.notify_all()
            self._ack(where, p.get("sessionId"))
        elif method in ("Target.targetCreated", "Target.targetInfoChanged"):
            info = p.get("targetInfo") or {}
            if info.get("type") == "page" and not str(info.get("url", "")).startswith(("devtools:", "chrome-extension:")):
                with self.lock:
                    new = info["targetId"] not in self.targets
                    self.targets[info["targetId"]] = {"url": info.get("url", ""), "title": info.get("title", "")}
                    follow = new and method == "Target.targetCreated" and self.holder == "user" and info.get("openerId")
                self._changed()
                if follow:               # a link the user opened in a new tab or a popup: show it
                    threading.Thread(target=self._attach, args=(info["targetId"],), daemon=True).start()
        elif method == "Target.targetDestroyed":
            tid = p.get("targetId")
            with self.lock:
                gone = self.targets.pop(tid, None) is not None
                was_active = tid == self.active
            if gone:
                self._changed()
            if was_active:
                threading.Thread(target=self._pick, daemon=True).start()
        elif method == "Target.detachedFromTarget" and p.get("sessionId") == self.session:
            threading.Thread(target=self._pick, daemon=True).start()
        elif method == "Page.javascriptDialogOpening" and where == self.session:
            with self.lock:
                self.dialog = {"type": p.get("type"), "message": str(p.get("message", ""))[:2000],
                               "default": str(p.get("defaultPrompt", ""))[:2000]}
            self._changed()
        elif method == "Page.javascriptDialogClosed" and where == self.session:
            with self.lock:
                self.dialog = None
            self._changed()
        elif method == "Page.screencastVisibilityChanged" and where == self.session and p.get("visible") is False:
            threading.Thread(target=self._pick, args=(True,), daemon=True).start()

    def _ack(self, session: str, frame_id) -> None:
        def go():
            try:
                self._send({"id": 0, "method": "Page.screencastFrameAck", "params": {"sessionId": frame_id},
                            "sessionId": session})
            except BrowserError:
                pass
        wait = FRAME_GAP_S - (time.monotonic() - self.last_ack)
        self.last_ack = time.monotonic() + max(0.0, wait)
        if wait > 0:
            threading.Timer(wait, go).start()
        else:
            go()

    def _ready(self) -> None:
        try:
            self.cmd("Target.setDiscoverTargets", {"discover": True})
            infos = self.cmd("Target.getTargets").get("targetInfos") or []
            with self.lock:
                for info in infos:
                    if info.get("type") == "page" and not str(info.get("url", "")).startswith(("devtools:", "chrome-extension:")):
                        self.targets[info["targetId"]] = {"url": info.get("url", ""), "title": info.get("title", "")}
            self._pick(True)
            with self.lock:
                self.status = "ready"
            self._changed()
            if self.viewers:
                self._cast(True)
        except BrowserError as e:
            self.app.log(f"browser {self.sid}: not ready ({e})")

    def _visible(self, tid: str) -> bool:
        session = self.cmd("Target.attachToTarget", {"targetId": tid, "flatten": True}).get("sessionId")
        try:
            r = self.cmd("Runtime.evaluate", {"expression": "document.visibilityState", "returnByValue": True}, session, 5)
            return (r.get("result") or {}).get("value") == "visible"
        finally:
            try:
                self.cmd("Target.detachFromTarget", {"sessionId": session}, timeout=5)
            except BrowserError:
                pass

    def _pick(self, by_visibility: bool = False) -> None:
        """Show the tab the browser shows (the visible one), or the first tab; open one if there is none."""
        with self.lock:
            ids = list(self.targets)
            current = self.active if self.active in self.targets else None
        chosen = None
        if by_visibility and len(ids) > 1:
            for tid in ids:
                try:
                    if self._visible(tid):
                        chosen = tid
                        break
                except BrowserError:
                    continue
        chosen = chosen or current or (ids[0] if ids else None)
        if chosen is None:
            try:
                chosen = self.cmd("Target.createTarget", {"url": "about:blank"}).get("targetId")
            except BrowserError:
                return
        if chosen != self.active or self.session is None:
            self._attach(chosen)

    def _attach(self, tid: str) -> None:
        try:
            old = self.session
            if old:
                try:
                    self.cmd("Target.detachFromTarget", {"sessionId": old}, timeout=5)
                except BrowserError:
                    pass
            session = self.cmd("Target.attachToTarget", {"targetId": tid, "flatten": True}).get("sessionId")
            with self.lock:
                self.active, self.session, self.casting, self.dialog = tid, session, False, None
            self.cmd("Page.enable", session=session)
            with self.lock:
                self.emulated = None
            self._fit()
            self._still_frame()
            self._changed()
            if self.viewers:
                self._cast(True)
        except BrowserError as e:
            self.app.log(f"browser {self.sid}: attach failed ({e})")

    def _still_frame(self) -> None:
        """One picture at once, so a viewer sees the page before the next change (screencast sends only changes)."""
        session = self.session
        if not session:
            return
        try:
            m = self.cmd("Page.getLayoutMetrics", session=session, timeout=10)
            vp = m.get("cssVisualViewport") or m.get("layoutViewport") or {}
            w, h = vp.get("clientWidth") or 1024, vp.get("clientHeight") or 768
            scale = min(1.0, FRAME_MAX / max(w, h))
            shot = self.cmd("Page.captureScreenshot", {"format": "jpeg", "quality": 60, "clip": {
                "x": vp.get("pageX", 0), "y": vp.get("pageY", 0), "width": w, "height": h, "scale": scale}}, session, 20)
            with self.lock:
                self.seq += 1
                self.frame = (self.seq, shot.get("data") or "", {"w": w, "h": h})
                self.cond.notify_all()
        except BrowserError:
            pass

    def _fit(self) -> None:
        """While the user holds the browser from a phone, lay pages out for that phone (cleared on hand back, and
        when the phone leaves). The agent always gets the window's own size back."""
        session = self.session
        with self.lock:
            want = self.phones[-1] if self.phones and self.holder == "user" and self.status in ("ready", "starting") else None
            if not session or want == self.emulated:
                return
            self.emulated = want
        try:
            if want:
                self.cmd("Emulation.setDeviceMetricsOverride", {"width": want[0], "height": want[1], "deviceScaleFactor": 2,
                                                                 "mobile": True}, session)
            else:
                self.cmd("Emulation.clearDeviceMetricsOverride", session=session)
        except BrowserError:
            pass

    def _cast(self, on: bool) -> None:
        session = self.session
        if not session:
            return
        try:
            if on and not self.casting:
                with self.lock:
                    self.casting = True
                self._still_frame()             # first, so a newer screencast frame is never replaced by it
                self.cmd("Page.startScreencast", {"format": "jpeg", "quality": 60, "maxWidth": FRAME_MAX,
                                                  "maxHeight": FRAME_MAX, "everyNthFrame": 1}, session)
            elif not on and self.casting:
                with self.lock:
                    self.casting = False
                self.cmd("Page.stopScreencast", session=session)
        except BrowserError:
            pass

    # ------------------------------------------------------------ the user's actions

    def _need_input(self) -> str:
        with self.lock:
            if self.status != "ready" or not self.session:
                raise BrowserError("the browser is not ready yet")
            if self.holder != "user":
                raise BrowserError("take over the browser first")
            return self.session

    def input(self, events) -> int:
        if not isinstance(events, list) or len(events) > 200:
            raise BrowserError("invalid input")
        session = self._need_input()
        with self.lock:
            size = (self.frame or (0, "", {}))[2]
        w, h = float(size.get("w") or 1024), float(size.get("h") or 768)
        done = 0
        for ev in events:
            if not isinstance(ev, dict):
                raise BrowserError("invalid input")
            t = ev.get("t")
            if t in ("down", "up", "move", "tap", "wheel"):
                x, y = _num(ev.get("x")) * w, _num(ev.get("y")) * h
                base = {"x": x, "y": y, "modifiers": _mods(ev.get("mods"))}
                button = ev.get("button") if ev.get("button") in BUTTONS else "left"
                if t == "wheel":
                    self.cmd("Input.dispatchMouseEvent", {**base, "type": "mouseWheel", "button": "none",
                             "deltaX": _num(ev.get("dx", 0), -5000, 5000), "deltaY": _num(ev.get("dy", 0), -5000, 5000)}, session)
                elif t == "tap":
                    count = int(_num(ev.get("count", 1), 1, 3))
                    self.cmd("Input.dispatchMouseEvent", {**base, "type": "mouseMoved", "button": "none"}, session)
                    for i in range(1, count + 1):
                        self.cmd("Input.dispatchMouseEvent", {**base, "type": "mousePressed", "button": button, "buttons": 1, "clickCount": i}, session)
                        self.cmd("Input.dispatchMouseEvent", {**base, "type": "mouseReleased", "button": button, "buttons": 0, "clickCount": i}, session)
                else:
                    held = int(_num(ev.get("buttons", 0), 0, 7))
                    self.cmd("Input.dispatchMouseEvent", {**base, "type": MOUSE[t], "button": button if t != "move" else ("left" if held & 1 else "none"),
                             "buttons": held, "clickCount": int(_num(ev.get("count", 1), 0, 3)) if t != "move" else 0}, session)
            elif t == "key":
                kind = ev.get("type")
                key, code = str(ev.get("key") or "")[:32], str(ev.get("code") or "")[:32]
                text = str(ev.get("text") or "")[:4]
                mods = _mods(ev.get("mods"))
                vk = int(_num(ev.get("vk", 0), 0, 255))
                if kind == "down":
                    typing = text and not (mods & (2 | 4))
                    self.cmd("Input.dispatchKeyEvent", {"type": "keyDown" if typing else "rawKeyDown", "key": key, "code": code,
                             "windowsVirtualKeyCode": vk, "modifiers": mods, **({"text": text, "unmodifiedText": text} if typing else {})}, session)
                elif kind == "up":
                    self.cmd("Input.dispatchKeyEvent", {"type": "keyUp", "key": key, "code": code, "windowsVirtualKeyCode": vk,
                                                        "modifiers": mods}, session)
                else:
                    raise BrowserError("invalid key")
            elif t == "text":
                text = str(ev.get("text") or "")
                if not text or len(text) > 4000:
                    raise BrowserError("invalid text")
                self.cmd("Input.insertText", {"text": text}, session)
            elif t == "ime":
                text = str(ev.get("text") or "")[:400]
                end = len(text)
                self.cmd("Input.imeSetComposition", {"text": text, "selectionStart": end, "selectionEnd": end}, session)
            else:
                raise BrowserError("invalid input")
            done += 1
        return done

    def navigate(self, action: str, url: str = "") -> None:
        session = self._need_input()
        if action == "go":
            r = self.cmd("Page.navigate", {"url": page_url(url)}, session, 30)
            if r.get("errorText"):
                raise BrowserError("could not open the page: " + str(r["errorText"])[:120])
        elif action in ("back", "forward"):
            h = self.cmd("Page.getNavigationHistory", session=session)
            i = int(h.get("currentIndex", 0)) + (-1 if action == "back" else 1)
            entries = h.get("entries") or []
            if 0 <= i < len(entries):
                self.cmd("Page.navigateToHistoryEntry", {"entryId": entries[i]["id"]}, session)
        elif action == "reload":
            self.cmd("Page.reload", session=session)
        elif action == "stop":
            self.cmd("Page.stopLoading", session=session)
        else:
            raise BrowserError("unknown action")

    def tab(self, action: str, tid: str = "", url: str = "") -> None:
        self._need_input()
        if action == "new":
            created = self.cmd("Target.createTarget", {"url": page_url(url) if url else "about:blank"}).get("targetId")
            if created:
                self._attach(created)
        elif action in ("show", "close"):
            with self.lock:
                if tid not in self.targets:
                    raise BrowserError("no such tab")
            if action == "show":
                self.cmd("Target.activateTarget", {"targetId": tid})
                self._attach(tid)
            else:
                self.cmd("Target.closeTarget", {"targetId": tid})
        else:
            raise BrowserError("unknown action")

    def control(self, holder: str) -> None:
        if holder not in ("user", "agent"):
            raise BrowserError("invalid holder")
        with self.lock:
            if self.status != "ready":
                raise BrowserError("the browser is not ready yet")
            if self.mode == "host":
                return                          # no agent between turns: the user always has it
        self._send({"snaplab": "control", "holder": holder})
        with self.lock:
            self.holder = holder
        self._fit()
        self._changed()

    def answer_dialog(self, accept: bool, text: str = "") -> None:
        session = self._need_input()
        params = {"accept": bool(accept)}
        if text:
            params["promptText"] = str(text)[:2000]
        self.cmd("Page.handleJavaScriptDialog", params, session)


class LiveBrowsers:
    """One LiveBrowser per session that someone opened (app.browsers)."""

    def __init__(self, app):
        self.app = app
        self.lock = threading.Lock()
        self.items: dict[str, LiveBrowser] = {}
        self.turns: set[str] = set()
        self.busy: dict[str, float] = {}
        self.stopping = False

    def available(self, sid: str) -> tuple[bool, str]:
        session = self.app.store.session(sid)
        if not session:
            return False, "no such session"
        from .harnesses import remote
        if remote.backend(self.app, session) not in ("wsl", "ssh") or session.get("harness") not in ("codex", "pi"):
            return False, "this session has no Linux workspace"
        return True, ""

    def get(self, sid: str) -> LiveBrowser:
        ok, why = self.available(sid)
        if not ok:
            raise BrowserError(why)
        with self.lock:
            if sid not in self.items:
                self.items[sid] = LiveBrowser(self, sid)
            return self.items[sid]

    def peek(self, sid: str) -> LiveBrowser | None:
        with self.lock:
            return self.items.get(sid)

    def turn_running(self, sid: str) -> bool:
        with self.lock:
            return sid in self.turns or self.busy.get(sid, 0) > time.monotonic()

    def turn_started_elsewhere(self, sid: str) -> None:
        """The job lock was held (a turn is starting): attach to it for the next minute."""
        with self.lock:
            self.busy[sid] = time.monotonic() + 60

    def before_turn(self, sid: str) -> None:
        """A turn of this session starts: a host browser closes first (the turn needs the job), then the panel
        attaches to the agent's browser."""
        with self.lock:
            self.turns.add(sid)
            lb = self.items.get(sid)
        if lb is not None:
            with lb.lock:
                lb.host_until = 0.0
            if lb.mode == "host":
                lb.end(wait=True)

    def after_turn(self, sid: str) -> None:
        with self.lock:
            self.turns.discard(sid)

    def connect_config(self, sid: str, stack: contextlib.ExitStack, *, host: bool):
        """(SSH settings, local port, remote port, token) for one panel connection. The WSL workspace stays up while
        the connection lasts. A host browser on WSL reaches the Internet through a translator like a turn's."""
        from .harnesses import remote
        session = self.app.store.session(sid) or {}
        if remote.backend(self.app, session) == "wsl":
            from .harnesses import wsl
            cfg = stack.enter_context(wsl.worker())
        else:
            cfg = remote.settings(self.app)
        if not (host and cfg.get("wsl")):
            return cfg, None, None, None
        from .harnesses.wsl import host_addresses
        from .harnesses.wsl_network import connect_public
        from .proxy import Translator
        token = secrets.token_urlsafe(32)
        self.app.guard.add(token)
        model = self.app.models.spec(None, "codex")["name"]
        proxy = Translator(port=0, api_cfg=self.app.cfg["api"], slots=self.app.slots, key_fn=lambda: None, token=token,
                           log_path=self.app.store.path("logs", "proxy.log"), redactor=self.app.redactor,
                           resolve=lambda _requested: self.app._translator_target(model), max_calls=1,
                           egress=lambda host_, port: connect_public(host_, port, host_addresses, reject_self=True))
        if not proxy.start():
            raise RuntimeError("could not start the browser's Internet relay")
        stack.callback(proxy.stop)
        return cfg, proxy.port, 30000 + secrets.randbelow(30000), token

    def close_all(self) -> None:
        self.stopping = True
        with self.lock:
            items = list(self.items.values())
        for lb in items:
            try:
                lb.end(wait=True)
            except Exception:  # noqa: BLE001
                pass
