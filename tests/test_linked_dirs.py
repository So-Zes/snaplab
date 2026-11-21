"""A linked folder in a SnapCode workspace is skipped, not a reason to stop the turn (2026-11-21).

A bot cloned a repository with one symlinked folder (packages/contracts-test/contracts -> ../contracts/contracts).
inventory() raised "linked workspace directory" on every turn, so the session could never continue."""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from snaplab.harnesses import remote_worker as files


def make_link(link: Path, target: Path) -> bool:
    try:
        os.symlink(target, link, target_is_directory=True)
        return True
    except OSError:
        if os.name != "nt":
            raise
    r = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True)
    return r.returncode == 0


class LinkedFolderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "job"
        (self.root / "packages" / "contracts" / "contracts").mkdir(parents=True)
        (self.root / "packages" / "contracts" / "contracts" / "Token.sol").write_text("contract Token {}")
        (self.root / "README.md").write_text("readme")
        (self.root / "packages" / "contracts-test").mkdir()
        self.link = self.root / "packages" / "contracts-test" / "contracts"
        if not make_link(self.link, self.root / "packages" / "contracts" / "contracts"):
            self.skipTest("cannot create a symlink or junction here")

    def tearDown(self):
        if self.link.is_symlink():
            self.link.unlink()
        elif self.link.exists():
            os.rmdir(self.link)                      # only the junction, never its target
        self.tmp.cleanup()

    def test_inventory_skips_the_link_and_reports_it(self):
        linked = []
        names = files.inventory(self.root, linked)
        self.assertIn("README.md", names)
        self.assertIn("packages/contracts/contracts/Token.sol", names)
        self.assertFalse(any(n.startswith("packages/contracts-test/") for n in names))
        self.assertEqual(linked, ["packages/contracts-test/contracts"])
        self.assertEqual(files.inventory(self.root), names)        # no list given: still no error

    def test_manifest_works_and_nothing_is_written_through_the_link(self):
        linked = []
        digests, _ = files.manifest(self.root, {}, linked)
        self.assertIn("README.md", digests)
        self.assertEqual(linked, ["packages/contracts-test/contracts"])
        with self.assertRaises(ValueError):
            files.write_own(self.root, "packages/contracts-test/contracts/Evil.sol", b"x", set(), linked)
        self.assertFalse((self.root / "packages" / "contracts" / "contracts" / "Evil.sol").exists())
        files.write_own(self.root, "packages/new.txt", b"ok", set(), linked)
        self.assertEqual((self.root / "packages" / "new.txt").read_bytes(), b"ok")

    def test_through_link(self):
        self.assertTrue(files.through_link("a/b", ["a/b"]))
        self.assertTrue(files.through_link("a/b/c.txt", ["a/b"]))
        self.assertFalse(files.through_link("a/bc.txt", ["a/b"]))


if __name__ == "__main__":
    unittest.main()
