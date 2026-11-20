"""Russian UI, stored plural events, Unicode names and language enforcement. No external API."""
import json
from pathlib import Path
import re
import unittest
from unittest.mock import patch

from helpers import make_app, onboard
from snaplab import i18n, prompts, dialogue
from snaplab.tools import Toolbox

NUMBERS = [0, 1, 2, 4, 5, 11, 14, 21, 22, 25, 101, 1.5]
CATEGORIES = ['many', 'one', 'few', 'few', 'many', 'many', 'many', 'one', 'few', 'many', 'one', 'other']


class RussianTest(unittest.TestCase):
    def test_catalog_complete_unique_and_placeholders_preserved(self):
        en = i18n.parse((i18n.DIR/'en.js').read_text(encoding='utf-8'))
        ru_text = (i18n.DIR/'ru.js').read_text(encoding='utf-8')
        ru = i18n.parse(ru_text)
        self.assertEqual(len(ru), len(list(i18n.LINE_RE.finditer(ru_text))))
        self.assertFalse(set(en)-set(ru))
        for key, value in en.items():
            self.assertEqual(set(i18n.VAR_RE.findall(value)), set(i18n.VAR_RE.findall(ru[key])), key)
            self.assertNotRegex(ru[key], r'[\u3040-\u30ff\u3400-\u9fff]', key)
        bases = {k.rsplit('.', 1)[0] for k in ru if k.endswith('.one') and k not in en}
        for base in bases:
            for cat in ('one', 'few', 'many', 'other'):
                self.assertIn(base+'.'+cat, ru)

    def test_plural_categories_and_old_events(self):
        words = ['файлов','файл','файла','файла','файлов','файлов','файлов','файл','файла','файлов','файл','файла']
        for n, cat, word in zip(NUMBERS,CATEGORIES,words):
            self.assertEqual(i18n.plural_category('ru',n),cat)
            self.assertEqual(i18n.text('ru','rc.files',n=n),f'{n} {word}')
            for key in ('ev.files.1','ev.files.n'):
                value=i18n.text('ru',key,n=str(n),plus=3,minus=2)
                self.assertIn(f'{n} {word}',value)
                self.assertIn('(+3 −2)',value)
        self.assertEqual(i18n.text('ru','projects.session1'),'1 задача SnapCode')
        self.assertEqual(i18n.text('en','rc.file1',n=1),'1 file')
        self.assertEqual(i18n.text('en','rc.files',n=22),'22 files')
        self.assertEqual(i18n.text('ja','rc.files',n=22), i18n.raw('ja','rc.files').replace('{n}','22'))

    def test_stop_and_non_stop(self):
        for value in ('стоп','Стоп!','остановись','остановитесь','Остановите разговор, пожалуйста.',
                      'пожалуйста, прекратите разговор ботов','@Марлин стоп'):
            self.assertTrue(dialogue.stop_requested(value),value)
        for value in ('не останавливайтесь','не стоп','не прекращайте разговор','Слово «стоп» означает остановку.',
                      'Если я скажу стоп, остановитесь','продолжайте разговор'):
            self.assertFalse(dialogue.stop_requested(value),value)

    def test_cyrillic_language_detection(self):
        for text in ('I have completed your project successfully.', 'プロジェクトの作業が完了しました。'):
            self.assertTrue(prompts.wrong_language(text,'ru','Создайте проект и напишите отчёт.'))
        for text in ('Проект готов. Все тесты пройдены.', 'Используйте Python и SnapCode.',
                     'Готово.\n```python\nprint("Hello world")\n```','README.md','SnapCode'):
            self.assertFalse(prompts.wrong_language(text,'ru','Создайте проект.'))
        self.assertFalse(prompts.wrong_language('The work is complete.','ru','Please answer in English.'))
        self.assertTrue(prompts.wrong_language('This is an English task report.','ru',''))

    def test_tool_cards_plans_and_instructions(self):
        app,_,_=make_app()
        result=onboard(app,lang='ru',name='Александра')
        bot=app.store.orchestrator()
        box=Toolbox(app,bot,lang='ru')
        cases={'start_session':{'prompt':'Create a small project and test it.'},
               'ask_user':{'question':'What should I do next with this project?'},
               'update_dashboard':{'widgets':[{'id':'keep_identifier','title':'This is a long English title'}]},
               'update_plan':{'steps':[{'text':'Implement a small Python application','done':False}]}}
        for name,args in cases.items():
            self.assertIsNotNone(box.wrong_language_arg(name,args),name)
        self.assertIsNone(box.wrong_language_arg('update_plan',{'steps':[{'text':'Создать приложение','done':False}]}))
        self.assertEqual(box.wrong_language_arg('ask_user',{'question':'Какой отчёт вам нужен?',
                         'options':['Please provide a detailed report']}), 'options')
        self.assertIn('на «вы»',bot['system_prompt'])
        self.assertNotIn('Russian',bot['system_prompt'])
        self.assertIn('Russian',prompts.house_rules('ru'))
        for h in app.sessions.harnesses.values():
            self.assertIn('ru',h.label)
        self.assertEqual(app.store.room(result['room']['id'])['name'],'Александра')

    def test_cyrillic_file_and_search(self):
        app,_,_=make_app()
        onboard(app,lang='ru',name='Александра')
        with patch('snaplab.gitops.available',return_value=False):
            project=app.create_project(name='Проверка файлов',description='Описание проекта',by='Вы')
        folder=Path(app.cfg['projects_dir'])/project['slug']
        app.sessions._write_state_file(project)
        state=(folder/'STATE.md').read_text(encoding='utf-8-sig')
        self.assertIn('Состояние проекта',state)
        self.assertIn('Задач пока нет',state)
        self.assertNotIn('Updated by SnapLab',state)
        self.assertEqual(app.archive.where({'project':project['id']}),'Проект «Проверка файлов»')
        box=Toolbox(app,app.store.orchestrator(),lang='ru',session={'id':'unicode-test','project':project['id'],'_dir':str(folder)})
        box.t_write_file('результат.txt','Проверка кириллицы завершена.')
        self.assertEqual((folder/'результат.txt').read_text(encoding='utf-8'),'Проверка кириллицы завершена.')
        # Exercise the actual Unicode FTS index and scope through the public recall tool.
        app.archive.add('message','Проверка кириллицы завершена.',bot=app.store.orchestrator()['id'],key='r17-unicode')
        result=box.t_recall(query='кириллицы')
        self.assertIn('кириллицы',result.content)


if __name__=='__main__': unittest.main()
