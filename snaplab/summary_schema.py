"""Fixed summary fields and a deterministic, inspectable coverage manifest."""
import json
import re

FIELDS = ("purpose", "constraints", "decisions", "open_items", "identifiers", "approvals", "failures", "next_actions")


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
    for key in FIELDS:
        parts.append("## " + key + "\n" + ("\n".join("- " + x for x in data[key]) or "- Unknown / none recorded"))
    if data.get("legacy_text"):
        parts.append("## Unstructured legacy material (unverified)\n" + data["legacy_text"])
    return "\n\n".join(parts)


def identifiers(text):
    return sorted(set(re.findall(r"\b[a-z]-[0-9a-f]{8}\b|\b[\w.-]+\.(?:py|md|json|js)\b", text)))
