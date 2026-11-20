"""Generate the deterministic synthetic P0 corpus; no application data or model calls."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FAMILIES = ("current", "historical", "late_record", "cross_room", "alias", "short_ja",
            "same_name", "approval", "test_version", "external_claim", "denied", "summary_ids")


def build():
    scenes = []
    for n in range(11):
        for j, family in enumerate(FAMILIES):
            serial = n * 12 + j + 1
            entity = f"p-{serial:08x}"
            old_id, new_id = f"a-{serial * 10:08x}", f"a-{serial * 10 + 1:08x}"
            old_time, new_time = "2026-10-01T00:00:00+00:00", "2026-10-20T00:00:00+00:00"
            at, known = "2026-10-25T00:00:00+00:00", "2026-10-25T00:00:00+00:00"
            name = ["灯台", "航路", "風見", "帆", "港", "月", "岬", "波", "島", "丘", "空"][n]
            field, old, new = "owner", f"Bot-A{n}", f"Bot-B{n}"
            query, mode, ask = entity, "current", False
            if family == "historical":
                mode, at = "historical", "2026-10-10T00:00:00+00:00"
            elif family == "late_record":
                known, at = "2026-10-15T00:00:00+00:00", "2026-10-12T00:00:00+00:00"
            elif family == "cross_room":
                field, old, new = "test_command", "pytest", "python -m unittest"
            elif family == "alias":
                query = f"旧称{name}"
            elif family == "short_ja":
                query = "帆"
            elif family == "approval":
                field, old, new, ask = "authorization", "pending", "consumed", True
            elif family == "test_version":
                field, old, new = "test_status", "passed:rev1", "unverified:rev2"
            elif family == "external_claim":
                field, old, new = "owner", "Bot-web", f"Bot-user{n}"
            elif family == "summary_ids":
                field, old, new = "open_work", "none", f"s-{serial:08x}:PLAN.md"

            def record(rid, value, stamp, previous=None):
                return {"id": rid, "ts": stamp, "kind": "memory", "project": entity, "bot": None,
                        "room": None, "session": None, "shared": False, "title": f"{name} {entity}",
                        "key": f"fact:{entity}:{field}", "hash": rid, "ref": f"#/project/{entity}",
                        "text": json.dumps({"entity": entity, field: value}, ensure_ascii=False),
                        "meta": {"fact": True, "entity": entity, "entity_name": name, "aliases": [f"旧称{name}", "帆"],
                                 "field": field, "source_type": "user", "status": "verified", "valid_from": stamp,
                                 "recorded_at": stamp, "source_version": rid,
                                 "supersedes": [previous] if previous else []}}
            records = [record(old_id, old, old_time), record(new_id, new, new_time, old_id)]
            if family == "late_record":
                records[-1]["meta"]["valid_from"] = "2026-10-11T00:00:00+00:00"
            if family == "external_claim":
                records[0]["meta"].update(source_type="external", status="unverified", untrusted=True)
            scope = {"everything": True}
            if family in ("same_name", "denied"):
                scope = {"bot": "b-00000001", "projects": [entity] if family == "same_name" else []}
                other = record(f"a-{serial * 10 + 2:08x}", "forbidden", new_time)
                other.update(project="p-ffffffff", title=f"{name} {entity}")
                records.append(other)
            if family == "cross_room":
                for r in records:
                    r["room"] = "r-00000002"
            value, ids = (old, [old_id]) if family in ("historical", "late_record") else (new, [new_id])
            if family == "denied":
                value, ids = "unknown", []
            question = f"{name}（{entity}）の{field}を、指定の時点と閲覧範囲で答えてください。"
            scenes.append({"id": f"{family}-{n:02}", "split": "tune" if n == 0 else "eval", "family": family,
                           "question": question, "entity": entity, "field": field, "query": query,
                           "mode": mode, "as_of": at, "known_at": known, "scope": scope, "records": records,
                           "gold": {"value": value, "sources": ids, "ask": ask, "action": "request_approval" if ask else "none",
                                    "stale_values": [old] if value != old else [new]}})
    return {"schema": 1, "version": "round8-v1", "notes": "12 families, 12 tuning scenes and 120 parameterized evaluation scenes; all synthetic.",
            "scenes": scenes}


if __name__ == "__main__":
    (ROOT / "tests/fixtures/context_eval.json").write_text(json.dumps(build(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
