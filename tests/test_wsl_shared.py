"""Several SnapCode jobs share one running WSL worker; the last one to finish stops it (2026-11-16)."""
from contextlib import contextmanager
import threading
import unittest
from unittest.mock import patch

from snaplab.harnesses import wsl


class SharedWorkerTest(unittest.TestCase):
    def test_parallel_jobs_start_the_worker_once_and_stop_it_after_the_last(self):
        events = []

        @contextmanager
        def boot(**kw):
            events.append('start')
            yield {'host': '127.0.0.1', 'ssh_port': 2222}
            events.append('stop')

        @contextmanager
        def exclusive(cancelled=None):
            events.append('lock')
            yield
            events.append('unlock')

        inside = threading.Barrier(2)
        leave_first = threading.Event()
        seen = []

        def job(name):
            with wsl.worker() as cfg:
                seen.append((name, cfg['ssh_port'], events.count('start')))
                inside.wait(timeout=5)
                if name == 'a':
                    leave_first.set()
                else:
                    leave_first.wait(timeout=5)
                    self.assertNotIn('stop', events)        # "a" finished; "b" still holds the worker

        with patch.object(wsl, '_boot', side_effect=boot), patch.object(wsl, 'exclusive', side_effect=exclusive), \
             patch.object(wsl, 'host_addresses', return_value=['192.0.2.1']):
            threads = [threading.Thread(target=job, args=(n,)) for n in ('a', 'b')]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=10)
        self.assertEqual(sorted(s[0] for s in seen), ['a', 'b'])
        self.assertEqual(events, ['lock', 'start', 'stop', 'unlock'])
        self.assertEqual(wsl._shared['jobs'], 0)

    def test_setup_keeps_its_private_start(self):
        calls = []

        @contextmanager
        def boot(**kw):
            calls.append(kw)
            yield {'host': '127.0.0.1'}

        with patch.object(wsl, '_boot', side_effect=boot):
            with wsl.worker(_record={'ssh_port': 1}, _locked=True):
                pass
        self.assertEqual(calls, [{'_record': {'ssh_port': 1}, '_locked': True}])


if __name__ == '__main__':
    unittest.main()
