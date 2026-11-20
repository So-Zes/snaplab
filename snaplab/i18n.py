"""Languages: one file per language in web/i18n/<code>.js holds every string, for the phone and the server.

- The phone gets core.js and every language file joined into one /i18n.js.
- The server reads the same files (one `"key": "value",` per line) for what it writes in the user's language:
  activity lines, notes after an approval, notification texts, the welcome message, the default persona
  prompt and proposal cards. Keys used only by the server start with `srv.`, `act.`, `note.`, `prop.`, `push.`.
- A missing string falls back to English, then to the key itself. See docs/extending.md, "Add a language".
- Activity lines, notes and cards also keep their key and values, so the phone shows them in the language
  it is set to now (a value written as "@some.key" is itself looked up).
"""
from __future__ import annotations

import json
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .config import WEB

DIR = WEB / "i18n"
CODE_RE = re.compile(r"^[a-z]{2,3}(_[a-z0-9]{2,8})?$")      # file names like fr.js or pt_br.js
LINE_RE = re.compile(r'^\s*"([A-Za-z0-9_.\-]+)"\s*:\s*("(?:[^"\\]|\\.)*")\s*,?\s*$', re.M)
VAR_RE = re.compile(r"\{(\w+)\}")
_cache: dict = {"key": None, "langs": [], "js": b"", "strings": {}}


def _files() -> list[Path]:
    files = [p for p in DIR.glob("*.js") if p.stem != "core" and CODE_RE.match(p.stem)]
    order = {"en": 0, "ja": 1}
    return sorted(files, key=lambda p: (order.get(p.stem, 2), p.stem))


def parse(text: str) -> dict[str, str]:
    """The strings of one language file."""
    out = {}
    for m in LINE_RE.finditer(text):
        try:
            out[m.group(1)] = json.loads(m.group(2))
        except ValueError:
            continue
    return out


def _load() -> None:
    files = _files()
    key = tuple((p.name, p.stat().st_mtime_ns, p.stat().st_size) for p in files) + \
        (((DIR / "core.js").stat().st_mtime_ns,) if (DIR / "core.js").exists() else ())
    if key == _cache["key"]:
        return
    langs, parts, strings = [], [], {}
    if (DIR / "core.js").exists():
        parts.append((DIR / "core.js").read_text(encoding="utf-8"))
    for p in files:
        text = p.read_text(encoding="utf-8")
        if f"window.SNAPLAB_I18N.{p.stem} =" not in text:
            continue                                   # the file does not define the language its name says
        s = parse(text)
        strings[p.stem] = s
        langs.append({"code": p.stem, "name": s.get("lang.name") or p.stem, "english": s.get("lang.english") or p.stem})
        parts.append(text)
    _cache.update(key=key, langs=langs, js="\n".join(parts).encode("utf-8"), strings=strings)


def languages() -> list[dict]:
    """[{code, name, english}] for every language file, English first."""
    _load()
    return list(_cache["langs"])


def codes() -> set[str]:
    return {x["code"] for x in languages()}


def valid(code: str | None, default: str = "en") -> str:
    """The code if a language file exists for it, else the default."""
    return code if isinstance(code, str) and code in codes() else default


def english_name(code: str) -> str:
    for x in languages():
        if x["code"] == code:
            return x["english"]
    return "English"


def bundle() -> bytes:
    """core.js + every language file, as one script."""
    _load()
    return _cache["js"]


def has(lang: str, key: str) -> bool:
    _load()
    return key in (_cache["strings"].get(lang) or {})


def raw(lang: str, key: str) -> str:
    _load()
    s = _cache["strings"]
    v = (s.get(lang) or {}).get(key)
    if v is None:
        v = (s.get("en") or {}).get(key)
    return key if v is None else v


# Old events and callers retain their original keys. Resolve the category at display time.
PLURAL_ALIASES = {
    "rc.file1": "rc.files", "projects.session1": "projects.sessions",
    "session.turn1": "session.turns", "work.count1": "work.count",
    "bots.project1": "bots.projects", "chats.questions.n": "chats.questions",
    "ev.files.1": "ev.files.n",
}


def plural_category(lang: str, number) -> str:
    """Cardinal categories for shipped languages (CLDR; decimals use other in Russian)."""
    try:
        n = abs(Decimal(str(number)))
        if not n.is_finite():
            return "other"
    except (InvalidOperation, ValueError, TypeError):
        return "other"
    base = lang.split("_")[0].split("-")[0]
    if base == "ru":
        if n != n.to_integral_value():
            return "other"
        i = int(n)
        if i % 10 == 1 and i % 100 != 11:
            return "one"
        if 2 <= i % 10 <= 4 and not 12 <= i % 100 <= 14:
            return "few"
        return "many"
    return "one" if base == "en" and n == 1 else "other"


def text(lang: str, key: str, **vars) -> str:
    """The string for `key` in `lang` (English when missing) with {placeholders} filled in.
    A value that starts with "@" is a key itself (so a stored line can be shown again in another language)."""
    base = PLURAL_ALIASES.get(key, key)
    n = vars.get("n", 1 if key in PLURAL_ALIASES and key != "chats.questions.n" else None)
    if n is not None and has(lang, base + "." + plural_category(lang, n)):
        key = base + "." + plural_category(lang, n)
        vars = {**vars, "n": n}
    def val(v):
        if isinstance(v, str) and v.startswith("@") and len(v) > 1:
            return raw(lang, v[1:])
        return "" if v is None else str(v)
    return VAR_RE.sub(lambda m: val(vars[m.group(1)]) if m.group(1) in vars else m.group(0), raw(lang, key))


def all_values(key: str) -> set[str]:
    """`key` in every language (to accept an answer typed in any of them)."""
    _load()
    return {s[key] for s in _cache["strings"].values() if key in s}
