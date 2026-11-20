"""The interface icons: one SVG file per icon in web/icons/ui/, put into the page as one hidden sprite.

To replace the icons, put new files with the same names into that folder (24 x 24 viewBox; colour them with
"currentColor" so they follow the theme) and reload the app. A file may set its own fill, stroke and stroke-width
on the <svg> element; they are kept. Nothing else in the code needs to change.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT_ATTRS = ("fill", "stroke", "stroke-width", "stroke-linecap", "stroke-linejoin")
_CACHE: dict = {"key": None, "value": ""}
NAME_RE = re.compile(r"^[a-z0-9-]+$")


def _symbol(name: str, svg: str) -> str:
    m = re.search(r"<svg\b([^>]*)>(.*)</svg>", svg, re.S | re.I)
    if not m:
        return ""
    attrs, inner = m.group(1), m.group(2)
    if re.search(r"<script|\son\w+\s*=|javascript:", inner, re.I):      # icons are drawings, nothing else
        return ""
    vb = re.search(r'viewBox\s*=\s*"([^"]+)"', attrs)
    keep = " ".join(f'{a}="{v}"' for a, v in re.findall(r'([\w-]+)\s*=\s*"([^"]*)"', attrs) if a in ROOT_ATTRS)
    return (f'<symbol id="i-{name}" viewBox="{vb.group(1) if vb else "0 0 24 24"}">'
            + (f"<g {keep}>" if keep else "") + inner.strip() + ("</g>" if keep else "") + "</symbol>")


def sprite(folder: Path) -> str:
    files = sorted(f for f in Path(folder).glob("*.svg") if NAME_RE.match(f.stem))
    key = tuple((f.name, f.stat().st_mtime_ns, f.stat().st_size) for f in files)
    if key != _CACHE["key"]:
        parts = [_symbol(f.stem, f.read_text(encoding="utf-8", errors="replace")) for f in files]
        _CACHE.update(key=key, value='<svg xmlns="http://www.w3.org/2000/svg" width="0" height="0" style="position:absolute;width:0;height:0;overflow:hidden" aria-hidden="true" focusable="false">'
                      + "".join(p for p in parts if p) + "</svg>")
    return _CACHE["value"]


def names(folder: Path) -> list[str]:
    return sorted(f.stem for f in Path(folder).glob("*.svg") if NAME_RE.match(f.stem))
