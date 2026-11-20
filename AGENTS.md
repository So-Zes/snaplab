# Working on SnapLab

1. Read `docs/MAP.md` first (how the parts fit together). Find files with `docs/CODEMAP.md`; past changes are in
   `docs/CHANGELOG.md`.
2. With every change, update in the same commit:
   - `docs/CHANGELOG.md`: the date, what changed and why
   - `docs/MAP.md`: when the structure or a flow changes
   - `docs/CODEMAP.md`: when files are added or removed (`python tools/codemap.py`)
3. Files use LF line endings. When rewriting a file from Python, use `open(..., newline='')`.
4. Tests: `python -m unittest discover -s tests` (no API calls) and `node tests/<name>_ui.mjs` for the UI.
5. Principle: give bots and SnapCode as much freedom as possible. Do not add prohibitions or caps to their
   instructions unless they are really needed (see "Principles" in `docs/MAP.md`).
