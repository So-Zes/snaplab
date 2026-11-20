"""Concurrency slots for the model API, shared across processes through lock files, with a priority queue.

Each slot is a file `slot<N>.lock` in one folder. Taking a slot means holding an OS byte-range lock on the
first byte of that file (msvcrt on Windows, flock elsewhere). The OS drops the lock when a process dies,
so a crash never leaks a slot. Any other tool that uses the same folder and the same protocol shares the
same limit (on the author's PC SnapLab points at the research folder's lock folder so that the total stays
at 3 across everything).

Inside SnapLab, waiting callers form a priority queue: when a slot frees up, the most urgent waiter takes it.
    0  the user talking to a bot          (chat)
    1  answers to cards, bot hand-offs     (follow-up)
    2  session work (Codex, "bot only")   (session)
    3  background jobs                     (background)
Session work may also be capped below the total (`session_max`, default 2 of 3), so a chat never waits
behind a row of long coding sessions. Other programs sharing the lock folder are not part of the queue,
but the total across everything never goes above `count`.
"""
from __future__ import annotations

import itertools
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path

PRIORITY = {"chat": 0, "followup": 1, "session": 2, "background": 3}


def _try_lock(f) -> bool:
    f.seek(0)
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _unlock(f) -> None:
    f.seek(0)
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass


class SlotsBusy(RuntimeError):
    pass


class Slots:
    def __init__(self, folder: str | Path, count: int = 3, session_max: int | None = None, background_max: int = 1):
        self.dir = Path(folder)
        self.count = max(1, int(count))
        self.session_max = max(1, min(int(session_max or self.count), self.count))
        self.background_max = max(1, min(int(background_max or 1), self.session_max))
        self._mine = 0
        self._mine_by_kind: dict[str, int] = {}
        self._lock = threading.Lock()
        self._waiters: list[tuple[int, int, str, str]] = []   # (priority, order, kind, label)
        self._order = itertools.count()

    @property
    def waiting(self) -> int:
        with self._lock:
            return len(self._waiters)

    def queue(self) -> list[dict]:
        with self._lock:
            return [{"kind": k, "label": lb} for _, _, k, lb in sorted(self._waiters)]

    def _first_in_line(self, me: tuple) -> bool:
        with self._lock:
            return bool(self._waiters) and min(self._waiters) == me

    def position(self, me: tuple) -> int:
        with self._lock:
            return sorted(self._waiters).index(me) + 1 if me in self._waiters else 0

    def _kind_allowed(self, kind: str) -> bool:
        if kind not in ("session", "background"):
            return True
        with self._lock:
            bg = self._mine_by_kind.get("background", 0)
            busy = self._mine_by_kind.get("session", 0) + bg
        if kind == "background" and bg >= self.background_max:
            return False                  # summaries and memory tidying: at most background_max at a time
        return busy < self.session_max

    @contextmanager
    def acquire(self, wait: bool = True, cancel=None, poll_s: float = 0.25, kind: str = "chat", label: str = "",
                on_wait=None):
        """Hold one slot for the duration of the block.

        kind: chat | followup | session | background (sets the priority). on_wait(position) is called while
        waiting (1 = next in line). cancel(): returns True to give up waiting (raises SlotsBusy).
        """
        self.dir.mkdir(parents=True, exist_ok=True)
        me = (PRIORITY.get(kind, 2), next(self._order), kind, label)
        with self._lock:
            self._waiters.append(me)
        held = None
        reported = None
        try:
            while held is None:
                if self._first_in_line(me) and self._kind_allowed(kind):
                    for i in range(self.count):
                        f = open(self.dir / f"slot{i}.lock", "a+")
                        if _try_lock(f):
                            held = (i, f)
                            break
                        f.close()
                if held is not None:
                    break
                if not wait:
                    raise SlotsBusy(f"all {self.count} API slots are in use")
                if cancel and cancel():
                    raise SlotsBusy("cancelled while waiting for an API slot")
                pos = self.position(me)
                if on_wait and pos != reported:
                    reported = pos
                    try:
                        on_wait(pos)
                    except Exception:  # noqa: BLE001
                        pass
                time.sleep(poll_s)
        finally:
            with self._lock:
                if me in self._waiters:
                    self._waiters.remove(me)
        i, f = held
        with self._lock:
            self._mine += 1
            self._mine_by_kind[kind] = self._mine_by_kind.get(kind, 0) + 1
        try:
            yield i
        finally:
            with self._lock:
                self._mine -= 1
                self._mine_by_kind[kind] -= 1
            _unlock(f)
            f.close()

    def in_use(self) -> int:
        """How many slots are held right now (by anyone sharing the folder). Probes each lock for an instant."""
        if not self.dir.exists():
            return 0
        busy = 0
        for i in range(self.count):
            p = self.dir / f"slot{i}.lock"
            if not p.exists():
                continue
            try:
                f = open(p, "a+")
            except OSError:
                continue
            try:
                if _try_lock(f):
                    _unlock(f)
                else:
                    busy += 1
            finally:
                f.close()
        return busy

    @property
    def mine(self) -> int:
        return self._mine
