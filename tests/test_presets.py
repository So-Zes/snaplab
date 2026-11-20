"""Provider presets: one place for the Snapscribe address, changeable by config.json or environment."""
import os
import unittest
from unittest import mock

from snaplab import presets


class PresetTest(unittest.TestCase):
    def test_defaults_name_snapscribe_1(self):
        p = presets.presets()
        self.assertEqual(p["snapscribe"]["label"], "Snapscribe 1")
        self.assertTrue(p["snapscribe"]["fixed_model"])
        self.assertTrue(p["snapscribe"]["base_url"].startswith("https://"))
        self.assertEqual(set(p), {"snapscribe", "openrouter", "opencode", "custom"})

    def test_config_overrides_known_fields_only(self):
        cfg = {"presets": {"snapscribe": {"base_url": "https://new.example/v1", "secret": "x"}, "unknown": {"label": "y"}}}
        p = presets.presets(cfg)
        self.assertEqual(p["snapscribe"]["base_url"], "https://new.example/v1")
        self.assertNotIn("secret", p["snapscribe"])
        self.assertNotIn("unknown", p)
        self.assertNotEqual(presets.BASE["snapscribe"]["base_url"], "https://new.example/v1")

    def test_environment_wins(self):
        env = {"SNAPLAB_SNAPSCRIBE_URL": " https://env.example/v1 ", "SNAPLAB_SNAPSCRIBE_MODEL": "model-2"}
        with mock.patch.dict(os.environ, env):
            p = presets.snapscribe({"presets": {"snapscribe": {"base_url": "https://cfg.example/v1"}}})
        self.assertEqual(p["base_url"], "https://env.example/v1")
        self.assertEqual(p["model"], "model-2")

    def test_label_is_shared_with_models(self):
        from snaplab import models
        self.assertEqual(models.SNAPSCRIBE_LABEL, presets.SNAPSCRIBE_LABEL)



class DeriveTest(unittest.TestCase):
    def test_setup_written_model_is_not_listed_twice(self):
        from snaplab.models import derive
        url = presets.snapscribe()["base_url"]
        cfg = {"api": {"base_url": url, "model": "qwen3.8-27b", "api_key_env": "K"},
               "providers": {"snapscribe-1a2b": {"base_url": url, "api_key_env": "K", "kind": "snapscribe"}},
               "models": {"snapscribe-1a2b": {"provider": "snapscribe-1a2b", "model": "qwen3.8-27b"}},
               "routing": {}}
        derive(cfg)
        self.assertEqual(list(cfg["models"]), ["snapscribe-1a2b"])
        self.assertEqual(cfg["routing"]["chat"], "snapscribe-1a2b")


if __name__ == "__main__":
    unittest.main()
