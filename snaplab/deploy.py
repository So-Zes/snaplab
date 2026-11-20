"""Restarts made by an updater, so an update does not need everything stopped first.

Before it starts the new version, the updater writes data/deploy.json: {"hold": true, "resume": true, "written": <unix
time>}. While held, SnapLab answers requests but leaves the data alone: the replies and sessions the restart cut off are
not touched yet, and the background work (scheduled tasks, room summaries, memory tidy) has not started. The updater can
then check that the new version's start-up changed no user data. When the check passes, the updater sets "hold" to
false. SnapLab then picks the cut-off work up again (resume: a reply is written again, a session is told to go on) and
starts the background work. If the updater never answers (it crashed), the hold ends by itself after HOLD_S.

A marker older than FRESH_S is a leftover (a deploy that died, then a reboot hours later): it is removed and nothing
is resumed.
"""
from __future__ import annotations

import json
import threading
import time

HOLD_S = 600
FRESH_S = 1800


def read(app) -> dict:
    path = app.store.path("deploy.json")
    try:
        marker = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(marker, dict) and time.time() - float(marker.get("written") or 0) < FRESH_S:
            return marker
    except (OSError, ValueError, TypeError):
        return {}
    clear(app)
    return {}


def clear(app) -> None:
    try:
        app.store.path("deploy.json").unlink()
    except OSError:
        pass


def held(app) -> bool:
    marker = read(app)
    return bool(marker.get("hold")) and time.time() - float(marker.get("written") or 0) < HOLD_S


def begin(app, stopping: threading.Event, start) -> None:
    """Start the background work (start()) now, or once the updater ends the hold. App.__init__ already recovered
    the cut-off work when there was no hold."""
    if not held(app):
        clear(app)
        start()
        return

    def wait():
        while held(app) and not stopping.is_set():
            time.sleep(0.5)
        if stopping.is_set():
            return
        resume = bool(read(app).get("resume", True))
        app.log("deploy hold ended" + ("; picking up the work the restart cut off" if resume else ""))
        try:
            app.recover(resume=resume)
        finally:
            clear(app)
            start()
    threading.Thread(target=wait, daemon=True, name="deploy-hold").start()
