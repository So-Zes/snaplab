"""Round 6 coverage, updated for round 10's multiple project ownership."""
import json
import re
import tempfile
import unittest
from pathlib import Path

from helpers import make_app, onboard

ROOT = Path(__file__).resolve().parent.parent


class ProjectOwnershipTest(unittest.TestCase):
    def test_a_teammate_keeps_all_assigned_projects(self):
        app, _, _ = make_app()
        onboard(app)
        a = app.create_project(name="Alpha")
        b = app.create_project(name="Beta")
        ken = app.create_bot(name="Ken", role="tester", projects=[a["id"], b["id"]])
        self.assertEqual(ken["projects"], [a["id"], b["id"]])
        self.assertEqual(app.store.project(a["id"])["bot"], ken["id"])
        app.assign_project(b["id"], ken["id"])  # assigning again preserves both projects
        ken = app.store.bot(ken["id"])
        self.assertEqual(ken["projects"], [a["id"], b["id"]])
        self.assertEqual(app.store.project(a["id"])["bot"], ken["id"])
        self.assertEqual(app.store.project(b["id"])["bot"], ken["id"])

    def test_owner_change_from_the_project_and_from_the_bot(self):
        app, _, _ = make_app()
        onboard(app)
        a = app.create_project(name="Alpha")
        ken = app.create_bot(name="Ken")
        sam = app.create_bot(name="Sam", projects=[a["id"]])
        app.update_project(a["id"], {"bot": ken["id"]})
        self.assertEqual(app.store.bot(ken["id"])["projects"], [a["id"]])
        self.assertEqual(app.store.bot(sam["id"])["projects"], [])
        app.update_bot(ken["id"], {"projects": []})
        self.assertEqual(app.store.bot(ken["id"])["projects"], [])
        self.assertEqual(app.store.project(a["id"])["bot"], app.store.orchestrator()["id"])

    def test_new_bot_comes_with_its_chat(self):
        app, _, _ = make_app()
        onboard(app)
        b = app.create_bot(name="Ken")
        self.assertIsNotNone(app.store.dm_room(b["id"]))


class MigrationTest(unittest.TestCase):
    def test_old_folder_retains_all_project_assignments(self):
        tmp = tempfile.mkdtemp(prefix="snaplab-mig-")
        app, _, _ = make_app(tmp=tmp)
        onboard(app)
        a = app.create_project(name="Alpha")
        b = app.create_project(name="Beta")
        ken = app.create_bot(name="Ken", projects=[a["id"]])
        # an older SnapLab let a teammate own two projects
        bots = app.store.bots()
        for x in bots:
            if x["id"] == ken["id"]:
                x["projects"] = [a["id"], b["id"]]
        app.store.write_json("bots.json", bots)
        pb = app.store.project(b["id"])
        pb["bot"] = ken["id"]
        app.store.save_project(pb)
        app.store.path("schema.json").unlink()
        app2, _, _ = make_app(tmp=tmp)
        k = app2.store.bot(ken["id"])
        self.assertEqual(k["projects"], [a["id"], b["id"]])
        self.assertNotIn("former_projects", k)
        self.assertEqual(app2.store.project(b["id"])["bot"], k["id"])
        from snaplab.migrate import SCHEMA
        self.assertEqual(app2.store.read_json("schema.json", {})["version"], SCHEMA)
        backups = list(app2.store.path("backup").glob("migrate-2-*/bots.json"))
        self.assertEqual(len(backups), 0)  # no ownership files need changing

    def test_a_fresh_folder_stays_untouched(self):
        tmp = tempfile.mkdtemp(prefix="snaplab-fresh-")
        app, _, _ = make_app(tmp=tmp)
        self.assertFalse(app.store.path("schema.json").exists())
        self.assertFalse(app.store.path("settings.json").exists())


class IconsTest(unittest.TestCase):
    def test_every_icon_the_app_uses_has_a_file(self):
        js = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
        used = set(re.findall(r'icon\("([a-z0-9-]+)"', js))
        used |= set(re.findall(r'\["[a-z]+", "([a-z0-9-]+)", "nav\.', js))
        from snaplab import icons
        have = set(icons.names(ROOT / "web" / "icons" / "ui"))
        self.assertFalse(used - have, f"missing icon files: {sorted(used - have)}")
        self.assertNotIn("bot", have)          # no robot faces

    def test_sprite_keeps_drawings_only(self):
        from snaplab import icons
        d = Path(tempfile.mkdtemp())
        (d / "ok.svg").write_text('<svg viewBox="0 0 24 24" stroke="currentColor"><path d="M1 1h2"/></svg>', encoding="utf-8")
        (d / "bad.svg").write_text('<svg viewBox="0 0 24 24"><script>alert(1)</script></svg>', encoding="utf-8")
        sp = icons.sprite(d)
        self.assertIn('id="i-ok"', sp)
        self.assertNotIn("script", sp)


if __name__ == "__main__":
    unittest.main()
