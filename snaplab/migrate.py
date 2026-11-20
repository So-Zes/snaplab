"""Moving an existing data folder to the current shape, without losing anything.

data/schema.json holds the version of the data folder. On start, SnapLab runs the steps from that version up to
SCHEMA. Before a step changes a file, the file is copied to data/backup/migrate-<version>-<time>/. A step only
adds or moves information; it never deletes a message, a memory or a record. A folder that was never set up
(no settings.json) just gets the current version.

Version 1: up to round 5.
Version 2: the raw archive is filled once. Round 10 preserves all old project assignments.
Version 3 remains readable unchanged: missing shared_with, bot_only and bots_paused fields use safe defaults.
"""
from __future__ import annotations

import datetime as dt
import shutil

SCHEMA = 3


def version(store) -> int:
    v = store.read_json("schema.json", None)
    if v is None:
        return 1 if store.path("settings.json").exists() else SCHEMA
    return int(v.get("version") or 1)


def _backup(store, ver: int, names: list[str]) -> str:
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = store.path("backup", f"migrate-{ver}-{stamp}")
    dest.mkdir(parents=True, exist_ok=True)
    for n in names:
        src = store.path(n)
        if src.is_file():
            shutil.copy2(src, dest / n.replace("/", "_"))
    return str(dest)


def to_2(app) -> list[str]:
    st = app.store
    notes = []
    archive = getattr(app, "archive", None)
    if archive is not None:
        n = archive.backfill()
        notes.append(f"the raw archive was filled with {n} records from the existing data")
    return notes


def to_3(app) -> list[str]:
    """Add projections, retaining every canonical file and old archive byte."""
    _backup(app.store, 3, ["schema.json"])
    app.ledger.sync()
    for b in app.store.bots():
        if app.store.memory(b["id"]).strip():
            app.archive_memory(b["id"], source_type="legacy")
    if app.store.user_note().strip():
        app.archive_memory(None, source_type="legacy")
    return ["added versioned state and memory projections; legacy approvals remain unbound"]


STEPS = {2: to_2, 3: to_3}


def run(app) -> list[str]:
    """Bring the data folder up to SCHEMA. Returns short notes of what changed (also written to the log)."""
    st = app.store
    cur = version(st)
    notes: list[str] = []
    while cur < SCHEMA:
        step = STEPS.get(cur + 1)
        if step:
            for n in step(app) or []:
                notes.append(n)
                app.log(f"migrate to {cur + 1}: {n}")
        cur += 1
        st.write_json("schema.json", {"version": cur, "updated": dt.datetime.now().astimezone().isoformat(timespec="seconds")})
    return notes
