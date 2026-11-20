"""Linux and macOS pieces that can be checked on any OS: the secrets file, the local SSH worker settings."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from snaplab import credentials
from snaplab.harnesses import remote


class SecretsFileTest(unittest.TestCase):
    def test_linux_without_secret_service_uses_an_owner_only_file(self):
        with tempfile.TemporaryDirectory() as tmp, \
                patch.dict(os.environ, {"SNAPLAB_SECRETS_FILE": str(Path(tmp) / "secrets.json")}), \
                patch.object(credentials, "_windows", return_value=False), patch.object(credentials.sys, "platform", "linux"), \
                patch.object(credentials, "_system_keyring", side_effect=OSError("no Secret Service")):
            credentials.write_credential("SNAPLAB_TEST_A", "value-one")
            credentials.write_credential("SNAPLAB_TEST_B", "value-two")
            self.assertEqual(credentials.read_system_credential("SNAPLAB_TEST_A"), "value-one")
            credentials.delete_credential("SNAPLAB_TEST_A")
            self.assertIsNone(credentials.read_system_credential("SNAPLAB_TEST_A"))
            self.assertEqual(json.loads((Path(tmp) / "secrets.json").read_text()), {"SNAPLAB_TEST_B": "value-two"})

    def test_macos_never_falls_back_to_a_file(self):
        with tempfile.TemporaryDirectory() as tmp, \
                patch.dict(os.environ, {"SNAPLAB_SECRETS_FILE": str(Path(tmp) / "secrets.json")}), \
                patch.object(credentials, "_windows", return_value=False), patch.object(credentials.sys, "platform", "darwin"), \
                patch.object(credentials, "_system_keyring", side_effect=OSError("locked")):
            with self.assertRaises(ValueError):
                credentials.write_credential("SNAPLAB_TEST_A", "value-one")
            self.assertFalse((Path(tmp) / "secrets.json").exists())


class LocalWorkerTest(unittest.TestCase):
    def test_ssh_uses_snaplabs_own_key_and_pinned_host_when_given(self):
        cfg = {"ssh_exe": "ssh", "host": "127.0.0.1", "user": "snaplab", "ssh_port": 22,
               "identity": "/home/me/.snaplab/linux-worker/identity", "known_hosts": "/home/me/.snaplab/linux-worker/known_hosts"}
        cmd = remote.ssh_command(cfg, 1000, 2000)
        self.assertIn("-i", cmd)
        self.assertIn("UserKnownHostsFile=/home/me/.snaplab/linux-worker/known_hosts", cmd)
        self.assertNotIn("-i", remote.ssh_command({k: v for k, v in cfg.items() if k != "identity"}, 1000, 2000))

    def test_launcher_picks_the_local_worker_on_linux(self):
        from tools import launch
        with tempfile.TemporaryDirectory() as tmp, patch.object(launch, "windows", return_value=False), \
                patch.object(launch.Path, "home", return_value=Path(tmp)):
            self.assertEqual(launch.local_worker(), {"backend": "none"})
            root = Path(tmp) / ".snaplab" / "linux-worker"
            root.mkdir(parents=True)
            (root / "ready.json").write_text('{"ssh_port": 22}')
            worker = launch.local_worker()
            self.assertEqual(worker["backend"], "ssh")
            self.assertEqual((worker["remote"]["host"], worker["remote"]["user"]), ("127.0.0.1", "snaplab"))


if __name__ == "__main__":
    unittest.main()
