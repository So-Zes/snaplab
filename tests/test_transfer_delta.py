"""Only changed files move between SnapLab and the worker, and the limits that ended long turns are gone (2026-11-17)."""
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import select
import socket
import sys
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from helpers import fake_credentials, make_app, onboard
from snaplab import evidence, gitops, sessions
from snaplab.guard import Guard, MARK
from snaplab.harnesses import adapters, remote
from snaplab.harnesses import remote_worker as files
from snaplab.harnesses import wsl, wsl_network


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


class WorkerSyncTest(unittest.TestCase):
    """The worker side of a turn, run on real folders: the job folder is kept, the manifest decides what moves."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        (self.home / 'jobs').mkdir()

    def sync(self, sid, pc_files, uploads=None, seeds=()):
        """Run files.sync with the home PC's files; answer the worker's request from pc_files."""
        folder = self.home / 'jobs' / sid
        folder.mkdir(exist_ok=True)
        events = []
        request = {'sid': sid, 'manifest': {n: digest(r) for n, r in pc_files.items()}, 'seeds': list(seeds)}

        class Wire:
            def __init__(self):
                self.lines = None

            def readline(self, limit=-1):
                if self.lines is None:            # the worker has said what it needs by now
                    need = next(e for e in events if e['kind'] == 'need')['paths']
                    sent = uploads if uploads is not None else {n: pc_files[n] for n in need}
                    self.lines = [json.dumps({'path': n, 'data': base64.b64encode(r).decode()}).encode() + b'\n'
                                  for n, r in sent.items()] + [b'{"kind":"start"}\n']
                return self.lines.pop(0)

        with patch.object(files.sys, 'stdin', SimpleNamespace(buffer=Wire())), \
             patch.object(files, 'emit', side_effect=events.append):
            files.sync(request, folder, self.home)
        return folder, events

    def need(self, events):
        return next(e for e in events if e['kind'] == 'need')['paths']

    def test_first_turn_asks_for_everything_and_the_next_for_nothing(self):
        pc = {'a.txt': b'one', 'src/b.js': b'two', '.snaplab/PLAN.md': b'- [ ] plan'}
        folder, events = self.sync('s-1', pc)
        self.assertEqual(sorted(self.need(events)), sorted(pc))
        self.assertEqual((folder / 'src' / 'b.js').read_bytes(), b'two')
        _, events = self.sync('s-1', pc)
        self.assertEqual(self.need(events), [])
        pc['a.txt'] = b'changed on the PC'
        _, events = self.sync('s-1', pc)
        self.assertEqual(self.need(events), ['a.txt'])
        self.assertEqual((folder / 'a.txt').read_bytes(), b'changed on the PC')

    def test_files_the_pc_removed_are_removed_with_their_empty_folders(self):
        folder, _ = self.sync('s-1', {'keep.txt': b'k', 'old/deep/gone.txt': b'g'})
        self.sync('s-1', {'keep.txt': b'k'})
        self.assertFalse((folder / 'old').exists())
        self.assertTrue((folder / 'keep.txt').exists())

    def test_a_new_session_copies_files_from_an_earlier_job_of_the_project(self):
        pc = {f'asset/{i}.png': bytes([i]) * 100 for i in range(20)}
        self.sync('s-old', pc)
        pc['new.txt'] = b'only on the PC'
        folder, events = self.sync('s-new', pc, seeds=['s-old'])
        self.assertEqual(self.need(events), ['new.txt'])
        self.assertEqual(next(e for e in events if e['kind'] == 'need')['local'], 20)
        self.assertEqual((folder / 'asset' / '7.png').read_bytes(), bytes([7]) * 100)

    def test_an_earlier_job_without_a_cache_is_read_once(self):
        pc = {'a.txt': b'aaa', 'b.txt': b'bbb'}
        self.sync('s-old', pc)
        (self.home / files.CACHE_DIR / 's-old.json').unlink()      # a job from before 2026-11-17 has no cache
        _, events = self.sync('s-new', pc, seeds=['s-old'])
        self.assertEqual(self.need(events), [])

    def test_a_changed_seed_file_is_not_trusted(self):
        pc = {'a.txt': b'original'}
        old, _ = self.sync('s-old', pc)
        (old / 'a.txt').write_bytes(b'changed later')       # the cache now describes other content
        _, events = self.sync('s-new', pc, seeds=['s-old'])
        self.assertEqual(self.need(events), ['a.txt'])

    def test_work_that_never_reached_the_pc_is_kept_aside(self):
        folder, _ = self.sync('s-1', {'a.txt': b'a', 'b.txt': b'b'})
        (folder / 'result.txt').write_bytes(b'made by a stopped turn')
        _, events = self.sync('s-1', {'a.txt': b'a', 'b.txt': b'b'})
        recovery = next(e for e in events if e['kind'] == 'recovery')['name']
        saved = sorted(p.relative_to(self.home / 'jobs' / recovery).as_posix()
                       for p in (self.home / 'jobs' / recovery).rglob('*') if p.is_file())
        self.assertEqual(saved, ['result.txt'])                 # only what differs, not the whole project
        self.assertFalse((folder / 'result.txt').exists())

    def test_uploads_must_match_the_request_and_the_manifest(self):
        with self.assertRaises(ValueError):
            self.sync('s-1', {'a.txt': b'a'}, uploads={'a.txt': b'other content'})
        with self.assertRaises(ValueError):
            self.sync('s-2', {'a.txt': b'a'}, uploads={'a.txt': b'a', 'extra.txt': b'x'})

    def test_the_turn_returns_only_changes_and_deletions(self):
        pc = {'same.txt': b'same', 'edit.txt': b'before', 'drop.txt': b'drop'}
        folder = self.home / 'jobs' / 's-1'
        folder.mkdir()
        request = {'sid': 's-1', 'manifest': {n: digest(r) for n, r in pc.items()}, 'token': 't', 'config': '',
                   'instructions': '', 'codex_bin': '.local/bin/codex', 'prompt': 'work', 'timeout': 30,
                   'test_timeout': 10, 'test_command': '',
                   'helpers': {'adapters.py': Path(remote.__file__).with_name('adapters.py').read_text(encoding='utf-8')}}
        wire = io.BytesIO(b''.join(json.dumps({'path': n, 'data': base64.b64encode(r).decode()}).encode() + b'\n'
                                   for n, r in pc.items()) + b'{"kind":"start"}\n')

        def work(cmd, folder, env, prompt, cancel, timeout, **kw):
            (folder / 'edit.txt').write_bytes(b'after')
            (folder / 'drop.txt').unlink()
            (folder / 'new.txt').write_bytes(b'new')
            return 0, ''

        events = []
        with patch.object(files.sys, 'stdin', SimpleNamespace(buffer=wire)), \
             patch.object(files, 'execute', side_effect=work), patch.object(files.threading, 'Thread'), \
             patch.object(files, 'emit', side_effect=events.append):
            self.assertEqual(files.run(request, folder, self.home), 0)
        returned = {e['path']: base64.b64decode(e['data']) for e in events if e['kind'] == 'file'}
        self.assertEqual(returned, {'edit.txt': b'after', 'new.txt': b'new'})
        self.assertEqual(next(e for e in events if e['kind'] == 'deleted')['paths'], ['drop.txt'])
        self.assertEqual(events[-1], {'kind': 'complete', 'exit_code': 0})


class HomeSideTest(unittest.TestCase):
    def setUp(self):
        self.creds = fake_credentials()
        self.creds.__enter__()
        self.addCleanup(self.creds.__exit__, None, None, None)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.app, _, _ = make_app(tmp=self.tmp.name, codex={"backend": "ssh", "remote": {"host": "agent.invalid"}})
        self.addCleanup(self.app.archive.close)
        self.folder = Path(self.tmp.name) / 'work'
        self.folder.mkdir()

    def test_manifest_reads_only_changed_files(self):
        (self.folder / 'a.txt').write_bytes(b'a')
        (self.folder / 'b.txt').write_bytes(b'b')
        old = time.time() - 60                     # changed long before it is read: the cache may trust it
        for name in ('a.txt', 'b.txt'):
            os.utime(self.folder / name, (old, old))
        first = remote.local_manifest(self.app, 's-12345678', self.folder)
        self.assertEqual(first, {'a.txt': digest(b'a'), 'b.txt': digest(b'b')})
        time.sleep(0.01)
        (self.folder / 'b.txt').write_bytes(b'BB')
        opened = []
        real_open = Path.open

        def counting(path, *args, **kwargs):
            opened.append(path.name)
            return real_open(path, *args, **kwargs)
        with patch.object(Path, 'open', counting):
            second = remote.local_manifest(self.app, 's-12345678', self.folder)
        self.assertEqual(second['b.txt'], digest(b'BB'))
        self.assertEqual([n for n in opened if n.endswith('.txt')], ['b.txt'])

    def test_a_fresh_worktree_reuses_digests_git_already_knows(self):
        repo = Path(self.tmp.name) / 'project'
        repo.mkdir()
        gitops.run(repo, 'init', '-q', '-b', 'main')
        gitops.run(repo, 'config', 'user.name', 'SnapLab')
        gitops.run(repo, 'config', 'user.email', 'snaplab@localhost')
        gitops.run(repo, 'config', 'core.autocrlf', 'false')
        for i in range(6):
            (repo / f'asset{i}.json').write_text('{"n": %d}' % i)
        gitops.run(repo, 'add', '-A')
        gitops.run(repo, 'commit', '-q', '-m', 'start')
        first = remote.local_manifest(self.app, 's-00000001', repo)         # reads and learns blob -> SHA-256
        self.assertTrue((repo / '.git' / 'snaplab-sha256.json').exists())
        fresh = repo / '.worktrees' / 's-2'
        gitops.run(repo, 'worktree', 'add', '-q', '-b', 'snaplab/s-2', str(fresh), 'HEAD')
        (fresh / 'asset5.json').write_text('{"n": 55}')                    # changed in the new worktree
        opened = []
        real_open = Path.open

        def counting(path, *args, **kwargs):
            if path.suffix == '.json' and path.name.startswith('asset'):
                opened.append(path.name)
            return real_open(path, *args, **kwargs)
        with patch.object(Path, 'open', counting):
            second = remote.local_manifest(self.app, 's-00000002', fresh)
        self.assertEqual(opened, ['asset5.json'])
        self.assertEqual({n: d for n, d in second.items() if n != 'asset5.json'},
                         {n: d for n, d in first.items() if n != 'asset5.json'})
        self.assertEqual(second['asset5.json'], digest(b'{"n": 55}'))

    def test_a_private_file_name_stops_the_job_before_anything_is_sent(self):
        self.app.redactor.add_term('private-owner-canary')
        (self.folder / 'notes-private-owner-canary.txt').write_bytes(b'x')
        with self.assertRaises(ValueError):
            remote.local_manifest(self.app, 's-12345678', self.folder)

    def test_download_applies_changes_and_only_deletes_files_that_were_sent(self):
        (self.folder / 'keep.txt').write_bytes(b'k')
        (self.folder / 'gone.txt').write_bytes(b'g')
        sent = {'keep.txt': digest(b'k'), 'gone.txt': digest(b'g')}
        remote.apply_download(self.folder, {'new.txt': b'n'}, ['gone.txt'], sent)
        self.assertEqual(sorted(p.name for p in self.folder.iterdir()), ['keep.txt', 'new.txt'])
        (self.folder / 'local-only.txt').write_bytes(b'made on the PC during the turn')
        with self.assertRaises(ValueError):
            remote.apply_download(self.folder, {}, ['local-only.txt'], sent)
        self.assertTrue((self.folder / 'local-only.txt').exists())

    def test_seeds_are_earlier_jobs_of_the_same_project_on_the_same_worker(self):
        store = self.app.store
        for sid, project, where, updated in (('s-00000001', 'p-1', 'wsl', '2026-11-17T10:00'),
                                             ('s-00000002', 'p-1', 'wsl', '2026-11-17T12:00'),
                                             ('s-00000003', 'p-1', 'ssh', '2026-11-17T13:00'),
                                             ('s-00000004', 'p-2', 'wsl', '2026-11-17T14:00')):
            store.save_session({'id': sid, 'project': project, 'execution_backend': where, 'updated': updated})
        seeds = remote.seed_sessions(self.app, {'id': 's-00000009', 'project': 'p-1', 'execution_backend': 'wsl',
                                                'from_session': 's-00000001'})
        self.assertEqual(seeds, ['s-00000001', 's-00000002'])


class NoArbitraryLimitsTest(unittest.TestCase):
    def test_relay_keeps_a_quiet_connection_open(self):
        calls = []
        real = select.select

        def recording(*args):
            if threading.current_thread().name == 'relay-under-test':
                calls.append(len(args))
            return real(*args)
        a, b = socket.socketpair()
        c, d = socket.socketpair()
        a.settimeout(5)
        with patch.object(wsl_network.select, 'select', recording):
            t = threading.Thread(target=wsl_network.pump, args=(b, c), daemon=True, name='relay-under-test')
            t.start()
            time.sleep(0.3)
            a.sendall(b'late answer')
            d.settimeout(5)
            self.assertEqual(d.recv(100), b'late answer')
            a.close()
            t.join(5)
        self.assertTrue(calls)
        self.assertTrue(all(n == 3 for n in calls))              # select without a timeout
        for s in (b, c, d):
            s.close()

    def test_relay_has_no_connection_cap(self):
        self.assertNotIn('process_request', vars(wsl_network.Broker))      # it refused the 33rd connection
        self.assertNotIn('slots', wsl_network.Broker.__init__.__code__.co_names)

    def test_pi_keeps_retrying_and_has_no_idle_cut(self):
        with tempfile.TemporaryDirectory() as tmp:
            req = {'harness': 'pi', 'pi_config': {}, 'mcp_url': 'http://127.0.0.1:1/mcp/s', 'pi_bin': '/bin/pi',
                   'model': 'synthetic', 'effort': 'medium', 'instructions': ''}
            adapters.launch(req, Path(tmp), Path(tmp), tmp)
            settings = json.loads(Path(tmp, 'settings.json').read_text(encoding='utf-8'))
        self.assertGreaterEqual(settings['retry']['maxRetries'], 10000)
        self.assertEqual(settings['httpIdleTimeoutMs'], 0)

    def test_git_has_no_time_limit(self):
        self.assertIsNone(gitops.run.__kwdefaults__['timeout'])

    def test_snapshot_lists_every_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            for i in range(5100):
                Path(tmp, f'{i}.txt').write_bytes(b'')
            self.assertEqual(len(sessions.snapshot(Path(tmp))), 5100)

    def test_large_files_are_checked_for_secrets(self):
        guard = Guard(values=['synthetic-secret-value'])
        with tempfile.TemporaryDirectory() as tmp:
            big = Path(tmp, 'big.bin')
            big.write_bytes(b'x' * 21_000_000 + b'synthetic-secret-value')
            self.assertEqual(guard.clean_paths(Path(tmp), ['big.bin']), ['big.bin'])
            self.assertTrue(big.read_bytes().endswith(MARK.encode()))

    def test_the_folder_fingerprint_rereads_only_changed_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = time.time() - 60
            for i in range(5):
                Path(tmp, f'{i}.txt').write_text(str(i))
                os.utime(Path(tmp, f'{i}.txt'), (old, old))
            first = evidence.artifact_version(Path(tmp))
            time.sleep(0.01)
            Path(tmp, '3.txt').write_text('changed')
            opened = []
            real_open = open
            with patch('snaplab.evidence.open', create=True,
                       side_effect=lambda p, *a, **k: opened.append(os.path.basename(p)) or real_open(p, *a, **k)):
                second = evidence.artifact_version(Path(tmp))
            self.assertNotEqual(first, second)
            self.assertEqual(opened, ['3.txt'])
            evidence._DIGESTS.clear()
            self.assertEqual(evidence.artifact_version(Path(tmp)), second)

    def test_a_file_rewritten_within_one_clock_step_is_read_again(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, 'main.py')
            path.write_text('one')
            stamp = path.stat().st_mtime_ns
            first = evidence.artifact_version(Path(tmp))
            path.write_text('two')                                  # same size
            os.utime(path, ns=(stamp, stamp))                       # and the same time, as within one clock step
            self.assertNotEqual(evidence.artifact_version(Path(tmp)), first)

    @unittest.skipUnless(os.name == 'nt', 'wsl.exe helpers run on Windows')
    def test_starting_the_worker_waits_but_obeys_stop(self):
        started = time.monotonic()
        with self.assertRaises(wsl.Cancelled):
            wsl._run([sys.executable, '-c', 'import time; time.sleep(30)'], cancelled=lambda: time.monotonic() - started > 0.5)
        self.assertLess(time.monotonic() - started, 10)
        code, out = wsl._run([sys.executable, '-c', 'print("verified")'])
        self.assertEqual((code, out.strip()), (0, b'verified'))


if __name__ == '__main__':
    unittest.main()
