import io
import tempfile
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch, Mock
from helpers import fake_credentials, make_app, onboard
from snaplab.harnesses import desktop, remote, remote_worker
from snaplab import prompts
from snaplab.proxy import to_chat


class DesktopTest(unittest.TestCase):
    def test_modifier_keys_with_spaces_are_normalized(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(desktop,'STATE',Path(tmp)), \
             patch.dict('os.environ',{'DISPLAY':':77'}), patch.object(desktop,'guard_input'), \
             patch.object(desktop,'read_screen',return_value=[]), \
             patch.object(desktop,'run') as run, patch.object(desktop,'capture',return_value='screen.png'), \
             patch.object(desktop.time,'sleep'), patch.object(desktop,'settle'), patch.object(desktop,'keyboard') as keys:
            desktop.control(SimpleNamespace(action='key',keys='ctrl s',output='screen.png'))
            run.assert_not_called()
            keys.assert_called_once_with([('ctrl',True),('s',True),('s',False),('ctrl',False)])

    def test_launch_request_belongs_to_supervisor(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(desktop,'STATE',Path(tmp)), \
             patch.dict('os.environ',{'DISPLAY':':77'}), patch.object(desktop,'run'), \
             patch.object(desktop,'capture',return_value='screen.png'):
            import json
            seen=[]
            def supervisor_tick(_):
                for request in Path(tmp).glob('launch-*.json'):
                    seen.append(json.loads(request.read_text(encoding='utf-8'))); request.unlink()
            with patch.object(desktop.time,'sleep',side_effect=supervisor_tick):
                desktop.control(SimpleNamespace(action='launch',app='mousepad',url='',output='screen.png'))
            self.assertEqual([request['cmd'] for request in seen],[['mousepad']])

    def test_only_latest_two_screenshots_are_sent(self):
        parts=[{'type':'input_image','image_url':f'data:image/png;base64,{i}'} for i in range(5)]
        chat,_,stats=to_chat({'input':[{'role':'user','content':parts}]},{'image_history_limit':2})
        images=[p for p in chat['messages'][0]['content'] if p['type']=='image_url']
        self.assertEqual([p['image_url']['url'] for p in images],[parts[3]['image_url'],parts[4]['image_url']])
        self.assertEqual(stats['images'],2)
        self.assertEqual(stats['images_omitted'],3)
        self.assertEqual(parts[0]['type'],'input_image')

    def test_tool_image_reaches_chat_as_image(self):
        image={'type':'input_image','image_url':'data:image/png;base64,abcd'}
        body={'input':[{'type':'function_call','name':'view_image','call_id':'a','arguments':'{}'},
                       {'type':'function_call_output','call_id':'a','output':[image]}]}
        chat, _, stats=to_chat(body)
        self.assertEqual(stats['images'],1)
        self.assertEqual(chat['messages'][-1]['content'][1]['image_url']['url'], image['image_url'])

    def test_sensitive_screen_blocks_before_typing_and_remains_blocked(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(desktop,'STATE',Path(folder)), \
             patch.object(desktop,'screenshot',return_value=Mock()), patch.object(desktop,'digest',return_value='x'),              patch.object(desktop,'run',return_value=b'Verify you are human') as run:
            with self.assertRaises(ValueError): desktop.guard_input()
            self.assertTrue((Path(folder)/'blocked').exists())
            run.reset_mock()
            with self.assertRaises(ValueError): desktop.guard_input()
            run.assert_not_called()

    def test_sensitive_languages_and_captcha(self):
        for text in ['CAPTCHA','Verify you are human',"I'm not a robot",'Credit card number','本人確認','カード番号']:
            self.assertIsNotNone(desktop.SENSITIVE.search(text),text)
        # Logins and passwords given by the task are typed; they no longer stop input.
        for text in ['Enter the digits and press the named button','Password','Log in','Sign in','ログイン','パスワード']:
            self.assertIsNone(desktop.SENSITIVE.search(text),text)

    def test_hard_memory_limit_and_job_isolation(self):
        command=remote_worker.limited_command(['bwrap'])
        self.assertIn('MemoryMax=3072M',command)
        self.assertIn('OOMPolicy=continue',command)
        self.assertIn('MemorySwapMax=0',command)
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            args=remote_worker.isolated_command(['true'],root/'job',root/'home',root/'config')
            self.assertIn('--unshare-pid',args)
            self.assertIn('--die-with-parent',args)
            self.assertIn('--tmpfs',args)
            self.assertNotIn('/tmp/.X11-unix',args)
            self.assertNotIn('/run/user',args)

    def test_live_frames_are_bounded_and_not_persisted(self):
        with fake_credentials(), tempfile.TemporaryDirectory() as tmp:
            app,_,_=make_app(tmp=tmp)
            raw=b'\xff\xd8\xfftest\xff\xd9'
            app.sessions.set_screen('s-example',raw)
            app.sessions.set_screen('s-example',raw)
            self.assertEqual(app.sessions.screens['s-example'],(raw,1))
            self.assertFalse(app.store.session_events('s-example'))
            with self.assertRaises(ValueError): app.sessions.set_screen('s-example',b'<script>')
            with self.assertRaises(ValueError): app.sessions.set_screen('s-example',raw*100000)
            app.sessions.clear_screen('s-example')
            self.assertNotIn('s-example',app.sessions.screens)
            app.archive.close()

    def test_local_does_not_offer_computer_use(self):
        local=prompts.house_rules('en',{'codex':{'backend':'local'}})
        ssh=prompts.house_rules('en',{'codex':{'backend':'ssh'}})
        self.assertIn('cannot operate a desktop',local)
        self.assertNotIn('private Linux desktop',local)
        self.assertIn('real Chrome window on a private Linux desktop',ssh)
        self.assertIn('after EVERY action',remote.INSTRUCTIONS)
