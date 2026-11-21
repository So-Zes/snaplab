"""Settings > Models: make a model the bots' default, remove a model added in Settings (synthetic keys only)."""
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from helpers import make_app, fake_credentials
from snaplab import config, setup
from tools.round19_provider import make_server

KEY = "synthetic-models-admin-credential"


class ModelsAdminTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.fake = fake_credentials()
        self.fake.__enter__()
        self.addCleanup(self.fake.__exit__, None, None, None)
        self.app, _, _ = make_app(tmp=self.temp.name, local_mode=True)
        self.addCleanup(self.app.archive.close)
        self.srv = make_server()
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)
        url = f"http://127.0.0.1:{self.srv.server_port}/v1"
        self.first = set(self.app.cfg["models"])
        with patch("snaplab.credentials.write_credential"):
            setup.configure(self.app, {"provider": "openrouter", "base_url": url, "model": "ordinary", "key": KEY}, add=True)
            setup.configure(self.app, {"provider": "custom", "base_url": url, "model": "other", "key": KEY + "2"}, add=True)
        self.a, self.b = [n for n in self.app.cfg["models"] if n not in self.first]

    def reload(self):
        return config.load(Path(self.temp.name) / "absent.json", overrides={"data_dir": self.app.cfg["data_dir"]})

    def test_default_changes_only_the_bots_route_and_persists(self):
        before = dict(self.app.cfg["routing"])
        out = setup.set_default(self.app, self.a)
        self.assertEqual(self.app.cfg["routing"]["chat"], self.a)
        self.assertEqual({k: v for k, v in self.app.cfg["routing"].items() if k != "chat"},
                         {k: v for k, v in before.items() if k != "chat"})
        self.assertIn("chat", next(m for m in out["models"] if m["name"] == self.a)["default_for"])
        self.assertEqual(self.reload()["routing"]["chat"], self.a)
        self.assertEqual(self.app.models.name_for(None, "chat"), self.a)
        with self.assertRaises(LookupError):
            setup.set_default(self.app, "missing")

    def test_remove_resets_bots_and_deletes_the_unused_key(self):
        bot = (self.app.store.bots() or [self.app.create_bot(name="Rin")])[0]
        self.app.update_bot(bot["id"], {"model": self.b})
        env = self.app.cfg["providers"][self.app.cfg["models"][self.b]["provider"]]["api_key_env"]
        with patch("snaplab.credentials.delete_credential") as delete:
            setup.remove_model(self.app, self.b)
        delete.assert_called_once_with(env)
        self.assertNotIn(self.b, self.app.cfg["models"])
        self.assertIsNone(self.app.store.bot(bot["id"]).get("model"))
        self.assertNotIn(self.b, self.reload()["models"])
        self.assertIn(self.a, self.reload()["models"])

    def test_defaults_and_config_models_cannot_be_removed(self):
        setup.set_default(self.app, self.a)
        with self.assertRaises(ValueError):
            setup.remove_model(self.app, self.a)
        for name in self.first:
            with self.assertRaises(ValueError):
                setup.remove_model(self.app, name)
        with self.assertRaises(LookupError):
            setup.remove_model(self.app, "missing")
        view = {m["name"]: m for m in self.app.models.view()}
        self.assertTrue(view[self.b]["removable"])
        self.assertFalse(any(view[n]["removable"] for n in self.first if n in view))
        reloaded = self.reload()                                       # after a restart, added ones stay removable
        self.assertIn(self.b, reloaded["models"])
        self.assertNotIn(self.b, reloaded["config_models"])


if __name__ == "__main__":
    unittest.main()
