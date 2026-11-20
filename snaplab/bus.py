"""In-process event bus. The web server streams these events to phones over Server-Sent Events."""
from __future__ import annotations

import queue
import threading


class Bus:
    def __init__(self, max_queue: int = 2000):
        self._subs: set[queue.Queue] = set()
        self._lock = threading.Lock()
        self.max_queue = max_queue
        self.seq = 0

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=self.max_queue)
        with self._lock:
            self._subs.add(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            self._subs.discard(q)

    def publish(self, kind: str, data: dict) -> None:
        with self._lock:
            self.seq += 1
            ev = {"type": kind, "seq": self.seq, **data}
            dead = []
            for q in self._subs:
                try:
                    q.put_nowait(ev)
                except queue.Full:
                    dead.append(q)      # a stuck client: drop it, the browser reconnects and reloads state
            for q in dead:
                self._subs.discard(q)

    @property
    def listeners(self) -> int:
        with self._lock:
            return len(self._subs)
