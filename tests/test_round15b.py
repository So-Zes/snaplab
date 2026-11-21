import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from snaplab.harnesses import desktop
from snaplab.app import brief_text


def word(text, x=10, confidence=90):
    return dict(text=text, x=x, y=30, w=40, h=20, confidence=confidence)


class TextDesktopTest(unittest.TestCase):
    def test_remote_brief_describes_available_network(self):
        for lang, yes, no in [('en', 'internet access', 'There is no internet access'),
                              ('ja', 'インターネットを使えます', 'インターネットにはつながりません'),
                              ('ru', 'доступен интернет', 'Доступа к интернету нет')]:
            self.assertIn(yes, brief_text('Test', 'Desktop task', '', lang, ssh=True))
            self.assertNotIn(no, brief_text('Test', 'Desktop task', '', lang, ssh=True))
            self.assertNotIn(no, brief_text('Test', 'Desktop task', '', lang))      # no such rule since 2026-11-21

    def test_phrase_center_and_unicode(self):
        self.assertEqual(desktop.text_point([[word('Learn'), word('more', 60)]], 'Learn more'), (30, 40))
        self.assertEqual(desktop.text_point([[word('保'), word('存', 60)]], '保存'), (30, 40))
        self.assertEqual(desktop.text_point([[word('ＳＡＶＥ')]], 'save'), (30, 40))

    def test_refuse_ambiguous_missing_low_confidence_and_partial_words(self):
        for lines, text in [([[word('Save')], [word('Save', 90)]], 'Save'),
                            ([[word('Save')]], 'Sav'), ([[word('Save', confidence=20)]], 'Save'),
                            ([[word('Save', x=2000)]], 'Save'), ([], '')]:
            with self.subTest(text=text, lines=lines), self.assertRaises(ValueError):
                desktop.text_point(lines, text)

    def test_click_text_guard_precedes_any_input(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(desktop, 'STATE', Path(tmp)), \
             patch.dict('os.environ', {'DISPLAY': ':77'}), \
             patch.object(desktop, 'guard_input', side_effect=ValueError('blocked')), \
             patch.object(desktop, 'read_screen') as read, patch.object(desktop, 'run') as run:
            with self.assertRaises(ValueError):
                desktop.control(SimpleNamespace(action='click-text', text='Sign in', output='desktop.png'))
            read.assert_not_called(); run.assert_not_called()

    def test_ocr_failure_prevents_input(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(desktop, 'STATE', Path(tmp)), \
             patch.dict('os.environ', {'DISPLAY': ':77'}), \
             patch.object(desktop, 'screenshot', return_value=Mock()), \
             patch.object(desktop, 'run', side_effect=RuntimeError('OCR failed')) as run:
            with self.assertRaises(RuntimeError):
                desktop.control(SimpleNamespace(action='type', text='secret', output='desktop.png'))
            self.assertEqual(run.call_count, 1)
            self.assertEqual(run.call_args.args[0], 'tesseract')

    def test_tsv_grouping_ignores_empty_lines(self):
        tsv = ('level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n'
               '5\t1\t1\t1\t1\t1\t10\t30\t40\t20\t90\tLearn\n'
               '5\t1\t1\t1\t1\t2\t60\t30\t40\t20\t91\tmore\n')
        from PIL import Image          # OCR now scales a real image and caches its reading (2026-11-15)
        with tempfile.TemporaryDirectory() as tmp, patch.object(desktop, 'STATE', Path(tmp)), \
             patch.object(desktop, 'screenshot', return_value=Image.new('RGB', (64, 48))), \
             patch.object(desktop, 'run', return_value=tsv.encode()):
            self.assertEqual(desktop.text_point(desktop.read_screen(), 'Learn more'), (15, 20))
