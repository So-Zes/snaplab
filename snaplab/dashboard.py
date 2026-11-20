"""Dashboard widgets that bots can write.

Bots never write HTML or JavaScript. They send data in a fixed shape, this module checks every field,
and the phone draws it with its own templates (text only). Unknown fields, unknown widget types, wrong
types and over-long strings are rejected with a clear message so the bot can fix its call.

Widget types (all have: id, type, title, optional size "half" | "full"):
  stat      value (text or number), unit?, note?, tone?
  progress  value 0..100, note?, tone?
  list      items: [{text, meta?, tone?}] (up to 12)
  note      text (up to 600 characters)
  table     columns: [text] (up to 5), rows: [[text]] (up to 10)
  bars      items: [{label, value >= 0}] (up to 10), unit?
tone is one of neutral | good | warn | bad | accent.
"""
from __future__ import annotations

import re

MAX_WIDGETS = 16
TONES = {"neutral", "good", "warn", "bad", "accent"}
ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

TYPES = {
    "stat": {"value": "value", "unit": ("str", 12), "note": ("str", 80), "tone": "tone"},
    "progress": {"value": "percent", "note": ("str", 80), "tone": "tone"},
    "list": {"items": "list_items"},
    "note": {"text": ("str", 600)},
    "table": {"columns": "columns", "rows": "rows"},
    "bars": {"items": "bar_items", "unit": ("str", 12)},
}
REQUIRED = {"stat": {"value"}, "progress": {"value"}, "list": {"items"}, "note": {"text"},
            "table": {"columns", "rows"}, "bars": {"items"}}
COMMON = {"id", "type", "title", "size"}


class WidgetError(ValueError):
    pass


def _str(v, limit: int, where: str, required: bool = True) -> str:
    if v is None and not required:
        return ""
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        v = str(v)
    if not isinstance(v, str):
        raise WidgetError(f"{where} must be text")
    v = CTRL_RE.sub("", v).strip()
    if len(v) > limit:
        raise WidgetError(f"{where} is longer than {limit} characters")
    return v


def _num(v, where: str, lo=None, hi=None) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise WidgetError(f"{where} must be a number")
    if v != v or v in (float("inf"), float("-inf")):
        raise WidgetError(f"{where} must be a finite number")
    if lo is not None and v < lo or hi is not None and v > hi:
        raise WidgetError(f"{where} must be between {lo} and {hi}")
    return int(v) if float(v).is_integer() else round(float(v), 3)


def _tone(v, where: str) -> str:
    if v is None:
        return "neutral"
    if v not in TONES:
        raise WidgetError(f"{where} must be one of {sorted(TONES)}")
    return v


def _check_keys(obj: dict, allowed: set, where: str) -> None:
    extra = set(obj) - allowed
    if extra:
        raise WidgetError(f"{where} has unknown fields: {sorted(extra)}")


def validate_widget(w) -> dict:
    if not isinstance(w, dict):
        raise WidgetError("each widget must be an object")
    t = w.get("type")
    if t not in TYPES:
        raise WidgetError(f"unknown widget type {t!r}; use one of {sorted(TYPES)}")
    spec = TYPES[t]
    _check_keys(w, COMMON | set(spec), f"widget {w.get('id')!r}")
    wid = w.get("id")
    if not isinstance(wid, str) or not ID_RE.match(wid):
        raise WidgetError("id must be 1-40 lowercase letters, digits, '-' or '_'")
    missing = REQUIRED[t] - set(w)
    if missing:
        raise WidgetError(f"widget {wid!r} is missing {sorted(missing)}")
    out = {"id": wid, "type": t, "title": _str(w.get("title"), 60, f"{wid}.title", required=False)}
    size = w.get("size", "half")
    if size not in ("half", "full"):
        raise WidgetError(f"{wid}.size must be 'half' or 'full'")
    out["size"] = size
    for key, kind in spec.items():
        if key not in w:
            continue
        v, where = w[key], f"{wid}.{key}"
        if kind == "value":
            out[key] = _str(v, 24, where)
        elif kind == "percent":
            out[key] = _num(v, where, 0, 100)
        elif kind == "tone":
            out[key] = _tone(v, where)
        elif isinstance(kind, tuple):
            out[key] = _str(v, kind[1], where, required=False)
        elif kind == "list_items":
            if not isinstance(v, list) or len(v) > 12:
                raise WidgetError(f"{where} must be a list of up to 12 items")
            items = []
            for i, it in enumerate(v):
                if isinstance(it, str):
                    it = {"text": it}
                if not isinstance(it, dict):
                    raise WidgetError(f"{where}[{i}] must be an object with text")
                _check_keys(it, {"text", "meta", "tone"}, f"{where}[{i}]")
                items.append({"text": _str(it.get("text"), 120, f"{where}[{i}].text"),
                              "meta": _str(it.get("meta"), 40, f"{where}[{i}].meta", required=False),
                              "tone": _tone(it.get("tone"), f"{where}[{i}].tone")})
            out[key] = items
        elif kind == "columns":
            if not isinstance(v, list) or not 1 <= len(v) <= 5:
                raise WidgetError(f"{where} must be a list of 1 to 5 column names")
            out[key] = [_str(c, 24, f"{where}[{i}]") for i, c in enumerate(v)]
        elif kind == "rows":
            if not isinstance(v, list) or len(v) > 10:
                raise WidgetError(f"{where} must be a list of up to 10 rows")
            rows = []
            for i, row in enumerate(v):
                if not isinstance(row, list) or len(row) > 5:
                    raise WidgetError(f"{where}[{i}] must be a list of up to 5 cells")
                rows.append([_str(c, 40, f"{where}[{i}][{j}]") for j, c in enumerate(row)])
            out[key] = rows
        elif kind == "bar_items":
            if not isinstance(v, list) or len(v) > 10:
                raise WidgetError(f"{where} must be a list of up to 10 items")
            bars = []
            for i, it in enumerate(v):
                if not isinstance(it, dict):
                    raise WidgetError(f"{where}[{i}] must be an object with label and value")
                _check_keys(it, {"label", "value"}, f"{where}[{i}]")
                bars.append({"label": _str(it.get("label"), 24, f"{where}[{i}].label"),
                             "value": _num(it.get("value"), f"{where}[{i}].value", 0, None)})
            out[key] = bars
    if t == "table":
        n = len(out["columns"])
        if any(len(r) != n for r in out["rows"]):
            raise WidgetError(f"every row of {wid} must have {n} cells")
    return out


def apply(current: list[dict], op: str, widgets: list | None = None, ids: list | None = None) -> list[dict]:
    """Return the new widget list after an operation: set (replace all), upsert (by id), remove (by id)."""
    if op not in ("set", "upsert", "remove"):
        raise WidgetError("op must be 'set', 'upsert' or 'remove'")
    if op == "remove":
        if not isinstance(ids, list) or not ids:
            raise WidgetError("remove needs ids: a list of widget ids")
        return [w for w in current if w["id"] not in set(ids)]
    if not isinstance(widgets, list) or not widgets:
        raise WidgetError(f"{op} needs widgets: a non-empty list")
    clean = [validate_widget(w) for w in widgets]
    if len({w["id"] for w in clean}) != len(clean):
        raise WidgetError("widget ids must be unique")
    if op == "set":
        result = clean
    else:
        by_id = {w["id"]: w for w in clean}
        result = [by_id.pop(w["id"], w) for w in current] + list(by_id.values())
    if len(result) > MAX_WIDGETS:
        raise WidgetError(f"the dashboard holds at most {MAX_WIDGETS} widgets")
    return result


SCHEMA_HELP = (
    "Widgets are data, never HTML. Each widget: {id, type, title, size?:'half'|'full', ...}. Types: "
    "stat{value, unit?, note?, tone?}; progress{value 0-100, note?, tone?}; list{items:[{text, meta?, tone?}] max 12}; "
    "note{text max 600}; table{columns max 5, rows max 10}; bars{items:[{label, value>=0}] max 10, unit?}. "
    "tone: neutral|good|warn|bad|accent. At most 16 widgets."
)
