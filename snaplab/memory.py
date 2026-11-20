"""Long-term memory (F14, plan A of the research): plain Markdown files the user can read and change.

- data/bots/<id>/memory.md   one bot's notes ("- [date] text" lines), up to `memory_chars` (4000). Only that bot
                             writes it (and the user, on the bot's page). Loaded into the bot's context.
- data/shared/user.md        what every bot should know about the user, up to `user_note_chars` (1400). The lead
                             bot writes it (about="user"); the user edits it in Settings. Loaded for every bot.
- data/bots/<id>/daily/YYYY-MM-DD.md   an append-only log of every memory change. Never loaded into a model call;
                             it is there so nothing a bot forgets is lost for the user.

"Remember ..." appends, "forget ..." removes the lines that contain the text, and a write past the limit is
refused with a message that asks the bot to tidy up (rewrite shorter with mode=replace) first.
"""
from __future__ import annotations

import datetime as dt


class MemoryFull(ValueError):
    pass


def lines(text: str) -> list[str]:
    return [ln for ln in (text or "").splitlines() if ln.strip()]


def forget(text: str, needle: str) -> tuple[str, int]:
    """Remove the lines that contain needle (case-insensitive)."""
    n = needle.strip().lower()
    keep, gone = [], 0
    for ln in lines(text):
        if n and n in ln.lower():
            gone += 1
        else:
            keep.append(ln)
    return ("\n".join(keep) + "\n") if keep else "", gone


def stamp_line(text: str) -> str:
    return f"- [{dt.datetime.now():%Y-%m-%d}] {' '.join(text.split())}"
