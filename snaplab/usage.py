"""API usage for the phone's usage screen: every recorded day, and one period broken down.

The numbers come from Store.record_usage (usage.json and usage/<day>.json). Older days lack some fields
(tokens per use, hours, callers); they are read as missing, never as an error. No model is called here.
"""
from __future__ import annotations

import datetime as dt

FIELDS = ("calls", "input_tokens", "output_tokens", "errors")


def _num(v) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _counts(b) -> dict:
    """calls / input / output / errors from a bucket; an older bucket may be a bare call count."""
    if isinstance(b, dict):
        return {k: _num(b.get(k)) for k in FIELDS}
    return {"calls": _num(b), "input_tokens": 0, "output_tokens": 0, "errors": 0}


def _add(into: dict, b) -> dict:
    c = _counts(b)
    for k in FIELDS:
        into[k] = into.get(k, 0) + c[k]
    return into


def _day(text) -> dt.date | None:
    try:
        return dt.date.fromisoformat(str(text))
    except ValueError:
        return None


def history(store) -> dict:
    """One small row per recorded day (for the day, week and month charts) and the total since the first day."""
    u = store.usage()
    rows = [{"day": d, **_counts(v)} for d, v in sorted(u["days"].items()) if _day(d)]
    detailed = store.usage_detail_days()
    return {"days": rows, "total": _counts(u["total"]), "first_day": rows[0]["day"] if rows else None,
            "detail_since": detailed[0] if detailed else None, "today": dt.date.today().isoformat(),
            "last_call": u.get("last_call")}


def detail(app, start: str, end: str) -> dict:
    """Totals, models, uses, callers (rooms and sessions with their bot) and hours for the days start..end."""
    a, b = _day(start), _day(end)
    if not a or not b:
        raise ValueError("from and to must be dates (YYYY-MM-DD)")
    if b < a:
        a, b = b, a
    store = app.store
    u = store.usage()
    total: dict = {}
    models: dict = {}
    sources: dict = {}
    calls_only: set = set()
    callers: dict = {}
    hours = [dict.fromkeys(FIELDS, 0) for _ in range(24)]
    days = with_detail = 0
    d = a
    while d <= b:
        key = d.isoformat()
        days += 1
        rec = u["days"].get(key) or {}
        _add(total, rec)
        for name, m in (rec.get("by_model") or {}).items():
            _add(models.setdefault(name, {}), m)
        if isinstance(rec.get("sources"), dict):
            for name, s in rec["sources"].items():
                _add(sources.setdefault(name, {}), s)
        else:                                   # days before tokens per use were kept: calls only
            for name, n in (rec.get("by_source") or {}).items():
                _add(sources.setdefault(name, {}), n)
                calls_only.add(name)
        extra = store.usage_detail(key)
        if extra:
            with_detail += 1
            for hh, c in (extra.get("hours") or {}).items():
                if str(hh).isdigit() and 0 <= int(hh) < 24:
                    _add(hours[int(hh)], c)
            for cid, c in (extra.get("callers") or {}).items():
                _add(callers.setdefault(cid, {}), c)
        d += dt.timedelta(days=1)
    for k in FIELDS:
        total.setdefault(k, 0)
    return {"from": a.isoformat(), "to": b.isoformat(), "days": days, "days_with_detail": with_detail, "sum": total,
            "models": _ranked(models), "sources": [{**s, "calls_only": s["id"] in calls_only} for s in _ranked(sources)],
            "callers": [{**c, **_who(app, c["id"])} for c in _ranked(callers)], "hours": hours}


def _ranked(groups: dict) -> list[dict]:
    rows = [{"id": k, **_counts(v)} for k, v in groups.items()]
    return sorted(rows, key=lambda r: (-(r["input_tokens"] + r["output_tokens"]), -r["calls"], r["id"]))


def _who(app, cid: str) -> dict:
    """What a caller id is, in words the phone can show: a room (and the bot of a one-to-one chat) or a session."""
    store = app.store
    if not cid:
        return {"kind": "other", "name": "", "bot": None}
    if cid.startswith("r-"):
        room = next((r for r in store.rooms(include_deleted=True) if r.get("id") == cid), None)
        if not room:
            return {"kind": "room", "name": cid, "bot": None}
        bots = room.get("bots") or []
        return {"kind": "room", "name": room.get("name") or cid, "bot": bots[0] if room.get("kind") == "dm" and bots else None,
                "group": room.get("kind") == "group"}
    if cid.startswith("s-"):
        s = store.session(cid)
        if not s:
            return {"kind": "session", "name": cid, "bot": None}
        project = store.project(s.get("project") or "") or {}
        starter = s.get("started_by") if s.get("started_by") not in (None, "", "user") else None
        return {"kind": "session", "name": s.get("title") or cid, "project": project.get("name"),
                "bot": s.get("bot") or starter or project.get("bot"), "status": s.get("status")}
    return {"kind": "other", "name": cid, "bot": None}
