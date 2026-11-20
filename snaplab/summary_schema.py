"""Fixed summary fields and a deterministic, inspectable coverage manifest."""
import json
import re

FIELDS = ("purpose", "constraints", "decisions", "open_items", "identifiers", "approvals", "failures", "next_actions")
# Rendered with what the bot needs next first: a context budget clips the end, so the long reference lists go last
# (until 2026-11-21 the fixed order put next_actions at the end, where the clip removed it). Empty fields are left out.
RENDER_ORDER = ("purpose", "next_actions", "open_items", "decisions", "constraints", "failures", "approvals", "identifiers")
MAX_IDENTIFIERS = 40


def parse(text):
    text = text.strip()
    if text.startswith('```'):
        match = re.fullmatch(r'```(?:json)?\s*\n(.*?)\n```', text, re.S | re.I)
        if match:
            text = match.group(1).strip()
    try:
        data = json.loads(text)
    except ValueError:
        # Compatibility with older models returning Markdown: preserve it as opaque, unverified material.
        if not text.strip().startswith("-"):
            return None
        data = {k: [] for k in FIELDS}
        data["legacy_text"] = text
    if not isinstance(data, dict) or any(not isinstance(data.get(k), list) for k in FIELDS):
        return None
    if any(not all(isinstance(v, str) for v in data[k]) for k in FIELDS):
        return None
    return {k: data[k] for k in FIELDS} | {"legacy_text": str(data.get("legacy_text") or "")}


def render(data):
    parts = ["Summary snapshot; approvals and work status must be checked against the current ledger."]
    for key in RENDER_ORDER:
        if data.get(key):
            parts.append("## " + key + "\n" + "\n".join("- " + x for x in data[key]))
    if data.get("legacy_text"):
        parts.append("## Unstructured legacy material (unverified)\n" + data["legacy_text"])
    return "\n\n".join(parts)


def keep_identifiers(new_text, model_list, previous_text, body):
    """Identifiers for the next summary, newest first and at most MAX_IDENTIFIERS: those in the new messages (found by
    code), those the model kept, and earlier ones that the summary still mentions. Older ones stay in the archive."""
    earlier = [i for i in identifiers(previous_text) if i in body]
    return list(dict.fromkeys(identifiers(new_text) + list(model_list) + earlier))[:MAX_IDENTIFIERS]


def identifiers(text):
    return sorted(set(re.findall(r"\b[a-z]-[0-9a-f]{8}\b|\b[\w.-]+\.(?:py|md|json|js)\b", text)))
