"""The desktop helper's real-input commands, the worker's memory admission and the worker's operation list (offline)."""
import os
from pathlib import Path
import sys
import tempfile
import threading
import types
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from snaplab.harnesses import desktop, remote, remote_worker


def act(**kw):
    return SimpleNamespace(output='screen.png', **kw)


class DesktopInputTest(unittest.TestCase):
    def control(self, args, run_value=b''):
        self.keys = []
        with tempfile.TemporaryDirectory() as tmp, patch.object(desktop, 'STATE', Path(tmp)), \
             patch.object(desktop, 'keyboard', side_effect=lambda events, gap=0: self.keys.extend(events)), \
             patch.dict('os.environ', {'DISPLAY': ':77'}), patch.object(desktop, 'guard_input'), \
             patch.object(desktop, 'read_screen', return_value=[]), \
             patch.object(desktop, 'run', return_value=run_value) as run, \
             patch.object(desktop, 'capture', return_value='screen.png'), patch.object(desktop.time, 'sleep'),              patch.object(desktop, 'settle'):
            desktop.control(args)
            return [c.args for c in run.call_args_list]

    def test_key_names_models_use_become_x_keysyms(self):
        self.assertEqual(desktop.key_tokens('ctrl s'), ['ctrl+s'])
        self.assertEqual(desktop.key_tokens('Tab Tab Enter'), ['Tab', 'Tab', 'Return'])
        self.assertEqual(desktop.key_tokens('ctrl+shift+i'), ['ctrl+shift+i'])
        self.assertEqual(desktop.key_tokens('F12 ArrowLeft Escape'), ['F12', 'Left', 'Escape'])
        self.assertEqual(desktop.key_tokens('Control+/'), ['ctrl+slash'])
        with self.assertRaises(ValueError):
            desktop.key_tokens('ctrl+$(rm)')

    def test_right_click_with_modifiers_is_one_real_x_input(self):
        calls = self.control(act(action='right-click', x=300, y=200, modifiers='ctrl'))
        self.assertEqual(calls, [('xdotool', 'mousemove', '300', '200', 'click', '--repeat', '1', '--delay', '90', '3')])
        self.assertEqual(self.keys, [('ctrl', True), ('ctrl', False)])
        calls = self.control(act(action='triple-click', x=10, y=20))
        self.assertEqual(calls[0][-1], '1')
        self.assertIn('3', calls[0])

    def test_points_outside_the_screen_are_refused(self):
        with self.assertRaises(ValueError):
            self.control(act(action='click', x=5000, y=10))

    def test_ascii_is_typed_as_keys_and_japanese_is_pasted(self):
        calls = self.control(act(action='type', text='miner-2026'))
        self.assertEqual(calls[0][:2], ('xdotool', 'type'))
        self.assertEqual(calls[0][-1], 'miner-2026')
        with patch.object(desktop.subprocess, 'run') as paste:
            calls = self.control(act(action='type', text='こんにちは'))
        self.assertEqual(paste.call_args_list[0].args[0], ['xclip', '-selection', 'clipboard'])
        self.assertEqual(self.keys, [('ctrl', True), ('v', True), ('v', False), ('ctrl', False)])
        calls = self.control(act(action='type', text='ab\ncd'))
        self.assertEqual([c[-1] for c in calls], ['ab', 'cd'])
        self.assertEqual(self.keys, [('Return', True), ('Return', False)])

    def test_hold_releases_the_key_and_scroll_moves_first(self):
        calls = self.control(act(action='hold', keys='shift+Right', seconds=0.2))
        self.assertEqual(calls, [])
        self.assertEqual(self.keys, [('shift', True), ('Right', True), ('Right', False), ('shift', False)])
        self.control(act(action='key', keys='F12', repeat=2))
        self.assertEqual(self.keys, [('F12', True), ('F12', False)] * 2)
        calls = self.control(act(action='scroll', direction='left', amount=2, x=100, y=100))
        self.assertEqual(calls[0][:5], ('xdotool', 'mousemove', '100', '100', 'click'))
        self.assertEqual(calls[0][-1], '6')

    def test_click_text_falls_back_to_the_page_elements(self):
        with patch.object(desktop, 'text_point', side_effect=ValueError('no match')), \
             patch.object(desktop, 'browser_point', return_value=(240, 512)):
            calls = self.control(act(action='click-text', text='Level up'))
        self.assertEqual(calls[0][:4], ('xdotool', 'mousemove', '240', '512'))

    def test_zoom_enlarges_a_region(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as tmp:
            cwd = os.getcwd(); os.chdir(tmp)
            try:
                with patch.object(desktop, 'screenshot', return_value=Image.new('RGB', desktop.SIZE, 'white')):
                    out = desktop.zoom(SimpleNamespace(action='zoom', x=100, y=50, width=200, height=100, scale=0,
                                                       output='desktop.png', grid=False))
                    self.assertEqual(Image.open(out['zoom']).size, (800, 400))
                    self.assertEqual(out['region'], [100, 50, 300, 150])
                    # With --grid the lines sit on screen coordinates: x=200 is 100 px into the region, 400 px at scale 4.
                    out = desktop.zoom(SimpleNamespace(action='zoom', x=100, y=50, width=200, height=100, scale=0,
                                                       output='grid.png', grid=True))
                    picture = Image.open(out['zoom']).convert('RGB')
                    self.assertTrue(all(abs(a - b) <= 4 for a, b in zip(picture.convert('RGB').getpixel((400, 300)), (255, 0, 255))))   # 256-colour PNG
                    self.assertTrue(all(abs(a - b) <= 4 for a, b in zip(picture.convert('RGB').getpixel((410, 300)), (255, 255, 255))))   # 256-colour PNG
            finally:
                os.chdir(cwd)

    def test_screen_size_comes_from_the_job(self):
        with patch.dict('os.environ', {'SNAPLAB_SCREEN': '1280x800'}):
            self.assertEqual(desktop.screen_size(), (1280, 800))
        with patch.dict('os.environ', {'SNAPLAB_SCREEN': '5120x2880'}):      # any size since 2026-11-21
            self.assertEqual(desktop.screen_size(), (5120, 2880))
        for bad in ('0x768', 'big', ''):
            with patch.dict('os.environ', {'SNAPLAB_SCREEN': bad}):
                self.assertEqual(desktop.screen_size(), (1024, 768))

    def test_password_and_captcha_screens_do_not_stop_input_but_card_entry_does(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(desktop, 'STATE', Path(tmp)), \
             patch.object(desktop, 'screenshot'), patch.object(desktop, 'digest', return_value='a'):
            with patch.object(desktop, 'run', return_value=b'Developer login Password Enter'):
                desktop.guard_input()
            with patch.object(desktop, 'run', return_value=b"I'm not a robot reCAPTCHA Verify your identity"):
                desktop.guard_input()
            with patch.object(desktop, 'run', return_value=b"Credit card number Security code"):
                with self.assertRaises(ValueError):
                    desktop.guard_input()
            self.assertTrue((Path(tmp) / 'blocked').exists())


class FakeFcntl(types.ModuleType):
    """flock for tests on any OS: a lock belongs to an open file object until it is closed."""
    LOCK_EX, LOCK_NB = 2, 4

    def __init__(self):
        super().__init__('fcntl')
        self.held = {}

    def flock(self, f, op):
        key = os.path.realpath(f.name)
        holder = self.held.get(key)
        if holder is not None and holder is not f and not holder.closed:
            if op & self.LOCK_NB:
                raise BlockingIOError
            raise AssertionError('would block')
        self.held[key] = f


class AdmissionTest(unittest.TestCase):
    def setUp(self):
        patcher = patch.object(remote_worker, 'tasks_free', return_value=None)
        patcher.start(); self.addCleanup(patcher.stop)
        self.fake = FakeFcntl()
        patcher = patch.dict(sys.modules, {'fcntl': self.fake})
        patcher.start(); self.addCleanup(patcher.stop)
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)

    def test_there_is_no_fixed_number_of_desktops(self):
        with patch.object(remote_worker, 'memory_free_mb', return_value=100000):
            slots = [remote_worker.desktop_slot(self.home, threading.Event()) for _ in range(6)]
            for slot in slots:
                slot.__enter__()
            for slot in slots:
                slot.__exit__(None, None, None)
        self.assertEqual(list((self.home / '.local/state/snaplab-locks').glob('desktop-*')), [])

    def test_recent_jobs_count_with_their_expected_size(self):
        free = remote_worker.JOB_START_MB * 2 + 10
        cancel = threading.Event()
        with patch.object(remote_worker, 'memory_free_mb', return_value=free):
            with remote_worker.desktop_slot(self.home, cancel):
                notes = []
                timer = threading.Timer(0.5, cancel.set); timer.start()
                with self.assertRaises(RuntimeError):     # the second would leave too little: it waits, then is cancelled
                    with remote_worker.desktop_slot(self.home, cancel, need_mb=free - 100, notify=lambda *a: notes.append(a)):
                        pass
                self.assertEqual(len(notes), 1)

    def test_a_waiting_job_starts_when_memory_frees_up(self):
        readings = iter([100, 100, 5000])
        with patch.object(remote_worker, 'memory_free_mb', side_effect=lambda: next(readings, 5000)), \
             patch.object(threading.Event, 'wait', lambda self, t=None: None):
            notes = []
            with remote_worker.desktop_slot(self.home, threading.Event(), notify=lambda *a: notes.append(a)):
                pass
        self.assertEqual(notes, [(100, remote_worker.JOB_START_MB, None)])

    def test_a_job_also_waits_for_task_slots(self):
        cancel = threading.Event()
        notes = []
        with patch.object(remote_worker, 'memory_free_mb', return_value=100000),              patch.object(remote_worker, 'tasks_free', return_value=remote_worker.JOB_START_TASKS - 1):
            threading.Timer(0.3, cancel.set).start()
            with self.assertRaises(RuntimeError):
                with remote_worker.desktop_slot(self.home, cancel, notify=lambda *a: notes.append(a)):
                    pass
        self.assertEqual(notes[0][2], remote_worker.JOB_START_TASKS - 1)

    def test_one_killed_process_does_not_stop_the_whole_job(self):
        command = remote_worker.limited_command(['bwrap'])
        self.assertIn('OOMPolicy=continue', command)
        self.assertIn('TasksMax=1024', command)


class OperationListTest(unittest.TestCase):
    def test_one_list_has_every_operation_and_the_password_rule(self):
        text = remote.instructions(True, '1280x800')
        for needed in ('desktop.py launch --app chrome', 'right-click', 'drag --x', 'scroll --direction', 'hold --keys',
                       'key --keys', 'zoom --x', 'F12', 'console', 'network', 'storage', 'elements', 'a11y', 'eval:JS',
                       '1280x800', 'view_image', 'read (Pi)', 'python3 .snaplab/desktop.py', 'node .snaplab/browser.mjs'):
            self.assertIn(needed, text)
        self.assertIn('type the credentials', text)
        self.assertNotIn('CAPTCHA', text)             # only the payment stop is kept (2026-11-21)
        self.assertIn('payment', text)
        self.assertNotIn('If login, password', text)
        headless = remote.instructions(False)
        self.assertNotIn('desktop.py', headless)
        self.assertIn('browser.mjs', headless)
        self.assertNotIn('rightclick', remote.DELIVERY)          # the list lives in one place only


if __name__ == '__main__':
    unittest.main()
