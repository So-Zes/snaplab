"""The overall map (docs/MAP.md) and the code map (docs/CODEMAP.md) must keep up with the code.

When this fails: mention the new file in docs/MAP.md (in the part it belongs to), run `python tools/codemap.py`,
and add a line to docs/CHANGELOG.md.
"""
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]


def sources():
    out = [p for p in (ROOT / "snaplab").glob("*.py") if p.name != "__init__.py"]
    out += [p for p in (ROOT / "snaplab" / "harnesses").glob("*.py") if p.name != "__init__.py"]
    out += list((ROOT / "snaplab" / "harnesses").glob("*.mjs"))
    return sorted(out)


class DocsMapTest(unittest.TestCase):
    def test_every_module_is_on_the_overall_map(self):
        text = (ROOT / "docs" / "MAP.md").read_text(encoding="utf-8")
        missing = [p.name for p in sources() if p.name not in text and f"{p.stem}.*" not in text]
        self.assertEqual(missing, [], "add these files to docs/MAP.md")

    def test_code_map_lists_exactly_the_current_files(self):
        text = (ROOT / "docs" / "CODEMAP.md").read_text(encoding="utf-8")
        listed = set(re.findall(r"^- `([^`]+)`", text, re.M))
        expected = {p.name for p in sources()} | {p.name for p in (ROOT / "web").glob("*.js")}
        self.assertEqual(sorted(expected - listed), [], "run python tools/codemap.py")
        gone = sorted(n for n in listed if n.endswith((".py", ".mjs", ".js")) and not any(ROOT.rglob(n)))
        self.assertEqual(gone, [], "run python tools/codemap.py")

    def test_changelog_has_dated_entries(self):
        text = (ROOT / "docs" / "CHANGELOG.md").read_text(encoding="utf-8")
        self.assertRegex(text, r"(?m)^## \d{4}-\d{2}-\d{2}")


if __name__ == "__main__":
    unittest.main()
