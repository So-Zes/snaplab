"""Source-backed memory entries. Originals and supersession edges are append-only."""
from __future__ import annotations

from .archive import Scope
from .temporal import instant


class MemoryFacts:
    def __init__(self, app):
        self.app = app

    def add(self, *, bot, entity, field, text, sources, valid_from=None, valid_to=None,
            supersedes=None, aliases=None, status="unverified", source_type="bot", shared=False):
        scope = Scope.for_bot(self.app, bot)
        if not entity or not field or not sources:
            raise ValueError("a fact requires entity, field and source record IDs")
        if (len(entity) > 160 or len(field) > 80 or len(text) > 1500 or len(sources) > 16
                or len(supersedes or []) > 16 or len(aliases or []) > 16
                or any(not isinstance(a, str) or len(a) > 160 for a in aliases or [])):
            raise ValueError("fact text, names or links exceed their limits")
        if status not in ("verified", "unverified", "conflict", "suppressed"):
            raise ValueError("unknown memory status")
        if source_type not in ("user", "runtime", "bot", "external", "legacy"):
            raise ValueError("unknown source type")
        records = [self.app.archive.get(rid, scope) for rid in sources]
        if not all(records):
            raise ValueError("a memory source is unavailable in this bot's scope")
        # Derived memory must not widen the audience of any original.
        tags = {k: records[0].get(k) for k in ("bot", "project", "room", "shared")}
        if any(any(r.get(k) != v for k, v in tags.items()) for r in records):
            raise ValueError("memory sources must share the same permissions")
        if shared and not tags.get("shared"):
            raise ValueError("private evidence cannot become shared memory")
        start = instant(valid_from) if valid_from else None
        end = instant(valid_to) if valid_to else None
        if start and end and start >= end:
            raise ValueError("valid_to must be after valid_from")
        trusted = all((r.get("meta") or {}).get("source_type") in ("user", "runtime")
                      or (r.get("meta") or {}).get("role") == "user" for r in records)
        if status == "verified" and not trusted:
            raise ValueError("untrusted sources cannot establish a verified fact")
        supersedes = list(supersedes or [])
        for rid in supersedes:
            old = self.app.archive.get(rid, scope)
            m = (old or {}).get("meta") or {}
            if (not old or not m.get("fact") or m.get("entity") != entity or m.get("field") != field
                    or any(old.get(k) != v for k, v in tags.items())):
                raise ValueError("replacement must describe the same field and permissions")
            if not start or (m.get("valid_from") and start < instant(m["valid_from"])):
                raise ValueError("replacement requires a nondecreasing effective date")
            if m.get("status") == "verified" and (not trusted or status != "verified"):
                raise ValueError("a conjecture cannot replace a verified fact")
        untrusted = any(r["kind"] in ("page", "tool", "work") or
                        (r.get("meta") or {}).get("untrusted") for r in records)
        return self.app.archive.add("memory", text, title=f"{entity} / {field}", **tags,
            meta={"fact": True, "entity": entity, "entity_name": entity, "field": field,
                  "sources": [{"id": r["id"], "version": (r.get("meta") or {}).get("source_version"),
                               "range": [0, len(r["text"])]} for r in records],
                  "source_type": source_type, "status": status, "valid_from": start, "valid_to": end,
                  "supersedes": supersedes, "aliases": list(aliases or []), "untrusted": bool(untrusted),
                  "web_material": any(r["kind"] == "page" or (r.get("meta") or {}).get("web_material")
                                      or (r.get("meta") or {}).get("tool") in ("web_search", "read_page") for r in records),
                  "method": "explicit_fact_v1"})

