"""Numbered marks: things to click found in the pixels, so a worker never has to estimate coordinates (offline)."""
import json
from pathlib import Path
import random
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image, ImageDraw

from snaplab.harnesses import desktop, remote


def screen(seed=1, count=12):
    """A browser-like screen: a tab strip, a canvas with round numbered buttons (pictures, no elements)."""
    random.seed(seed)
    im = Image.new('RGB', (1024, 768), (32, 35, 42))
    d = ImageDraw.Draw(im)
    d.rectangle((0, 0, 1024, 80), fill=(222, 225, 230))
    for i in range(6):
        d.rounded_rectangle((10 + i * 150, 20, 140 + i * 150, 60), 8, outline=(120, 120, 120), fill=(240, 240, 240))
    d.rectangle((32, 90, 992, 690), fill=(244, 241, 232))
    buttons = []
    while len(buttons) < count:
        x, y = random.randint(80, 950), random.randint(150, 650)
        if any((x - a) ** 2 + (y - b) ** 2 < 70 ** 2 for _, a, b in buttons):
            continue
        d.ellipse((x - 20, y - 20, x + 20, y + 20), fill=(59, 111, 216))
        d.text((x - 4, y - 6), str(len(buttons) + 1), fill=(255, 255, 255))
        buttons.append((len(buttons) + 1, x, y))
    return im, buttons


class MarksTest(unittest.TestCase):
    def test_round_buttons_on_a_canvas_are_found_as_one_target_each(self):
        for seed in range(5):
            im, buttons = screen(seed)
            marks = desktop.make_marks(im, [], [], None)
            for n, x, y in buttons:
                best = min(marks, key=lambda m: (m['x'] - x) ** 2 + (m['y'] - y) ** 2)
                self.assertLessEqual(abs(best['x'] - x) + abs(best['y'] - y), 3, (seed, n, best))
                self.assertTrue(34 <= best['w'] <= 48 and 34 <= best['h'] <= 48, best)   # the circle, not its digit
                self.assertEqual(best['color'], '#3b6fd8')
                self.assertEqual(best['source'], 'shape')

    def test_text_is_attached_to_its_button_and_page_elements_win(self):
        im = Image.new('RGB', (1024, 768), (255, 255, 255))
        d = ImageDraw.Draw(im)
        d.rectangle((100, 100, 200, 140), outline=(0, 0, 0), fill=(230, 230, 230))
        d.text((130, 113), 'Save', fill=(0, 0, 0))
        d.rectangle((400, 100, 500, 140), outline=(0, 0, 0), fill=(230, 230, 230))
        lines = [[{'text': 'Save', 'x': 130, 'y': 113, 'w': 26, 'h': 12, 'confidence': 90}],
                 [{'text': 'Hello', 'x': 600, 'y': 300, 'w': 40, 'h': 12, 'confidence': 90}]]
        page = [{'el': 'button#go', 'text': 'Go', 'sx': 450, 'sy': 120, 'w': 100, 'h': 40}]
        marks = desktop.make_marks(im, lines, page, None)
        by_text = {m.get('text'): m for m in marks}
        self.assertEqual(by_text['Save']['source'], 'shape')            # the button, labelled with its text
        self.assertEqual((by_text['Go']['source'], by_text['Go']['x'], by_text['Go']['y']), ('page', 450, 120))
        self.assertEqual(by_text['Hello']['source'], 'text')
        self.assertEqual(len([m for m in marks if abs(m['x'] - 450) < 20 and abs(m['y'] - 120) < 20]), 1)

    def test_a_bar_over_a_page_element_keeps_its_own_button(self):
        # y8.com, 2026-11-16: the cookie bar's "Got it" sat over a game tile and was dropped as "part of the tile".
        im = Image.new('RGB', (1024, 768), (255, 255, 255))
        d = ImageDraw.Draw(im)
        d.rectangle((560, 600, 780, 740), fill=(90, 140, 200))                  # the tile underneath
        d.rectangle((200, 690, 820, 750), fill=(250, 250, 250), outline=(200, 200, 200))
        d.rectangle((700, 702, 760, 734), fill=(210, 0, 0))                     # Got it
        tile = {'el': 'a', 'text': 'Game', 'sx': 670, 'sy': 670, 'w': 220, 'h': 140}
        found = lambda e: [m for m in desktop.make_marks(im, [], [e], None) if m['source'] == 'shape'
                           and abs(m['x'] - 730) <= 3 and abs(m['y'] - 718) <= 3]
        self.assertEqual(found(tile), [])                                      # what happened before
        marks = desktop.make_marks(im, [], [{**tile, 'covered': True}], None)
        self.assertTrue(found({**tile, 'covered': True}))
        self.assertTrue(any(m.get('covered') for m in marks if m['source'] == 'page'))

    def test_fine_mode_splits_a_knob_from_its_track(self):
        im = Image.new('RGB', (1024, 768), (255, 255, 255))
        d = ImageDraw.Draw(im)
        d.rectangle((100, 398, 400, 402), fill=(200, 200, 200))
        d.ellipse((191, 391, 209, 409), fill=(45, 108, 223))
        coarse = desktop.make_marks(im, [], [], None)
        fine = desktop.make_marks(im, [], [], (90, 380, 410, 420), fine=True)
        knob = lambda ms: [m for m in ms if abs(m['x'] - 200) <= 2 and abs(m['y'] - 400) <= 2 and m['w'] <= 24]
        self.assertEqual(knob(coarse), [])                                     # one mark for knob and track
        self.assertTrue(knob(fine))

    def test_region_limits_the_marks(self):
        im, buttons = screen(3)
        n, x, y = buttons[0]
        marks = desktop.make_marks(im, [], [], (x - 40, y - 40, x + 40, y + 40))
        self.assertEqual(len(marks), 1)
        self.assertLessEqual(abs(marks[0]['x'] - x) + abs(marks[0]['y'] - y), 3)

    def test_marks_action_saves_pictures_and_click_mark_uses_its_centre(self):
        im, buttons = screen(4)
        with tempfile.TemporaryDirectory() as tmp, patch.object(desktop, 'STATE', Path(tmp)), \
             patch.dict('os.environ', {'DISPLAY': ':77'}), patch.object(desktop, 'screenshot', return_value=im), \
             patch.object(desktop, 'read_screen', return_value=[]), patch.object(desktop, 'page_elements', return_value=[]), \
             patch.object(desktop, 'inside', side_effect=lambda o, suffix='.png': Path(tmp) / Path(o).name), \
             patch.object(desktop, 'pointer', return_value=None), patch('builtins.print') as printed:
            desktop.control(SimpleNamespace(action='marks', output='desktop.png', no_text=False))
            out = json.loads(printed.call_args.args[0])
            self.assertTrue(Path(out['marks_image']).is_file() and Path(out['marks_sheet']).is_file())
            n, x, y = buttons[0]
            self.assertTrue(Path(out['marks_list']).read_text(encoding='utf-8').startswith('#1 '))
            saved = json.loads((Path(tmp) / 'marks.json').read_text(encoding='utf-8'))['marks']
            mark = min(saved, key=lambda m: (m['x'] - x) ** 2 + (m['y'] - y) ** 2)
            self.assertIn(f"#{mark['n']} shape {mark['x']},{mark['y']}", out['marks'][mark['n'] - 1])
            with patch.object(desktop, 'guard_input'), patch.object(desktop, 'keyboard'), \
                 patch.object(desktop, 'run', return_value=b'') as run, patch.object(desktop, 'settle'), \
                 patch.object(desktop, 'capture', return_value='screen.png'):
                desktop.control(SimpleNamespace(action='click', output='desktop.png', no_text=True, mark=mark['n']))
                self.assertEqual(run.call_args.args[:3], ('xdotool', 'mousemove', str(mark['x'])))
                self.assertEqual(run.call_args.args[3], str(mark['y']))
                with self.assertRaises(ValueError):
                    desktop.control(SimpleNamespace(action='click', output='desktop.png', no_text=True, mark=999))

    def test_worker_is_told_to_use_marks_instead_of_estimating(self):
        self.assertIn('marks [--x X --y Y --width W --height H] [--fine]', remote.INSTRUCTIONS)
        self.assertIn('click --mark N', remote.INSTRUCTIONS)
        self.assertIn('never stop because one way did not find the target', remote.INSTRUCTIONS)


if __name__ == '__main__':
    unittest.main()
