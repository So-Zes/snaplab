"""Time and provenance rules shared by every recall backend. No model decides validity."""
from __future__ import annotations

import datetime as dt
import hashlib
import json


def instant(value: str | None = None) -> str:
    if value is None:
        d = dt.datetime.now(dt.timezone.utc)
    else:
        d = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        if d.tzinfo is None:
            raise ValueError("timestamps must include a timezone")
    return d.astimezone(dt.timezone.utc).isoformat(timespec="microseconds")


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


class TemporalScope:
    """Adds time constraints before the index's candidate limit, retaining its ACL."""
    def __init__(self, scope, mode="current", as_of=None, known_at=None):
        if mode not in ("current", "historical", "all"):
            raise ValueError("mode must be current, historical or all")
        if mode == "historical" and not as_of:
            raise ValueError("historical search requires as_of with timezone")
        self.scope, self.mode = scope, mode
        self.at = instant(as_of)
        self.known = instant(known_at)

    def sql(self):
        where, args = self.scope.sql()
        where += " AND recorded_at <= ?"
        args = args + [self.known]
        if self.mode != "all":
            where += (" AND (valid_from IS NULL OR valid_from <= ?)"
                      " AND (valid_to IS NULL OR valid_to > ? OR end_recorded_at > ?)"
                      " AND lifecycle != 'suppressed'")
            args += [self.at, self.at, self.known]
        return where, args

    def allows(self, rec):
        if not self.scope.allows(rec):
            return False
        m = rec.get("meta") or {}
        if instant(m.get("recorded_at") or rec["ts"]) > self.known:
            return False
        if self.mode == "all":
            return True
        return (m.get("status") != "suppressed"
                and (not m.get("valid_from") or instant(m["valid_from"]) <= self.at)
                and (not m.get("valid_to") or instant(m["valid_to"]) > self.at
                     or (m.get("end_recorded_at") and instant(m["end_recorded_at"]) > self.known)))
