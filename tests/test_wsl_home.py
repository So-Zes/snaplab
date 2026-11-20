"""The WSL worker's keys and records live outside AppData (2026-11-16).

The Claude desktop app is installed as an app package, so files that setup wrote to AppData from inside it went to the
package's private copy. The SnapLab logon task could not see them and reported wsl_not_ready although setup had passed."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from snaplab.harnesses import wsl


class WslHomeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name, 'home')
        self.appdata = Path(self.tmp.name, 'appdata')
        self.home.mkdir()
        self.appdata.mkdir()
        for p in (patch.object(wsl.Path, 'home', return_value=self.home),
                  patch.dict(os.environ, {'LOCALAPPDATA': str(self.appdata)}),
                  patch.object(wsl, '_adopted', False)):
            p.start()
            self.addCleanup(p.stop)
        self.protected = []
        p = patch.object(wsl, 'protect_key', side_effect=self.protected.append)
        p.start()
        self.addCleanup(p.stop)

    def legacy(self, files):
        old = self.appdata / 'SnapLab' / 'wsl'
        (old / 'distribution').mkdir(parents=True)
        for name, text in files.items():
            (old / name).write_text(text, encoding='utf-8')
        return old

    def test_the_folder_is_in_the_home_folder_not_appdata(self):
        root = wsl.directory()
        self.assertEqual(root, self.home / '.snaplab' / 'wsl')
        self.assertNotIn(str(self.appdata), str(root))

    def test_an_earlier_setup_is_carried_over_once_and_the_key_is_protected(self):
        self.legacy({'ready.json': '{"version": "v", "isolation_ok": true}', 'identity': 'KEY',
                     'setup-result.json': '{}', 'known_hosts': 'host'})
        root = wsl.directory()
        self.assertEqual((root / 'ready.json').read_text(encoding='utf-8'), '{"version": "v", "isolation_ok": true}')
        self.assertEqual((root / 'identity').read_text(encoding='utf-8'), 'KEY')
        self.assertEqual(self.protected, [root / 'identity'])
        self.assertFalse((root / 'distribution').exists())        # the disk stays where WSL registered it
        with patch.object(wsl, 'version', return_value='v'), patch.object(wsl.os, 'name', 'nt'):
            self.assertTrue(wsl.ready())

    def test_a_setup_in_the_new_place_is_never_overwritten(self):
        root = self.home / '.snaplab' / 'wsl'
        root.mkdir(parents=True)
        (root / 'setup-result.json').write_text('{"new": true}', encoding='utf-8')
        (root / 'ready.json').write_text('new', encoding='utf-8')
        self.legacy({'ready.json': 'old', 'setup-result.json': '{}'})
        wsl.directory()
        self.assertEqual((root / 'ready.json').read_text(encoding='utf-8'), 'new')
        self.assertEqual(self.protected, [])

    def test_no_earlier_setup_means_nothing_to_carry(self):
        root = wsl.directory()
        self.assertFalse(root.exists())


if __name__ == '__main__':
    unittest.main()
