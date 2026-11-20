import errno
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch


def load(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).resolve().parents[1] / 'tools' / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


probe = load('check_wsl_isolation')
windows = load('check_wsl_windows_exec')


class DirectWindowsExecutionTests(unittest.TestCase):
    def result(self, code, stderr=b'', stdout=b'', timeout=False):
        child = Mock(pid=123, returncode=code)
        child.communicate.side_effect = ([subprocess.TimeoutExpired('probe', 1), (b'', b'')]
                                         if timeout else [(stdout, stderr), (stdout, stderr)])
        with patch.object(probe.subprocess, 'Popen', return_value=child), \
                patch.object(probe.os, 'killpg', create=True) as kill, \
                patch.object(probe.signal, 'SIGKILL', 9, create=True):
            result = probe.probe_executable(['/synthetic/probe.exe'])
            kill.assert_called_once_with(123, 9)
        return result

    def test_timeout_and_exit_zero_never_pass(self):
        self.assertFalse(self.result(0)['denied'])
        self.assertFalse(self.result(1, timeout=True)['denied'])
        error = b'<3>WSL (3 - ) ERROR: UtilBindVsockAnyPort:309: socket failed 1\n'
        self.assertFalse(self.result(0, error)['denied'])

    def test_unexplained_failure_and_accept_timeout_never_pass(self):
        for error in (b'', b'failed', b'<3>WSL (3 - ) ERROR: UtilAcceptVsock:273: accept4 failed 110\n',
                      b'<3>WSL (3 - ) ERROR: UtilBindVsockAnyPort:309: socket failed 110\n'):
            self.assertFalse(self.result(1, error)['denied'])

    def test_known_denials_are_explicit(self):
        self.assertTrue(self.result(1, b'<3>WSL (3 - ) ERROR: UtilBindVsockAnyPort:309: socket failed 1\n')['vsock_permission_denied'])
        self.assertTrue(self.result(1, b'<3>WSL (11 - ) ERROR: UtilGetPpid:1330: Failed to parse: /proc/1/stat, content: 1 (bwrap) S 0 1\n')['private_parent_denied'])

    def test_unexpected_os_errors_never_pass(self):
        for number in (errno.ENOEXEC, errno.EPERM, errno.EIO, errno.EMFILE):
            with patch.object(probe.subprocess, 'Popen', side_effect=OSError(number, 'synthetic')):
                self.assertEqual(probe.probe_executable(['probe'])['denied'], number in (errno.ENOEXEC, errno.EPERM))

    def test_mz_is_executable_and_removed(self):
        paths = []
        def execute(argv):
            p = Path(argv[0]); paths.append(p)
            self.assertEqual(p.read_bytes(), b'MZ')
            return {'denied': True}
        with tempfile.TemporaryDirectory() as tmp, patch.object(probe, 'probe_executable', side_effect=execute):
            self.assertTrue(probe.probe_mz(tmp)['denied'])
            self.assertFalse(paths[0].exists())

    def test_observer_detects_write_during_teardown_and_cleans_it(self):
        report = {}
        with patch.object(windows.time, 'sleep'), self.assertRaisesRegex(RuntimeError, 'Delayed'):
            with windows.windows_observer(report) as marker:
                marker.write_text('synthetic', encoding='utf-8')
        self.assertTrue(report['late_windows_marker_created'])
        self.assertFalse(marker.parent.exists())

    def test_observer_stays_active_when_probe_raises(self):
        report = {}
        with patch.object(windows.time, 'sleep'), self.assertRaisesRegex(ValueError, 'synthetic'):
            with windows.windows_observer(report) as marker:
                raise ValueError('synthetic')
        self.assertFalse(report['late_windows_marker_created'])
        self.assertFalse(marker.parent.exists())


if __name__ == '__main__':
    unittest.main()
