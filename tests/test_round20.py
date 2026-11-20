"""Routing, completion and per-job safety regressions (synthetic credentials only)."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from helpers import fake_credentials, make_app, onboard
from snaplab.harnesses import adapters
from snaplab.harnesses.base import TurnResult
from snaplab.harnesses.selection import select, completion_error
from snaplab.sessions import Control
from snaplab.tools import Toolbox
from snaplab.proxy import Translator
import test_proxy_namespace as proxy_tests
from test_proxy_namespace import request_body, sse


class Round20Test(unittest.TestCase):
    def setUp(self):
        self.creds = fake_credentials()
        self.creds.__enter__()
        self.addCleanup(self.creds.__exit__, None, None, None)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.app, _, _ = make_app(tmp=self.tmp.name, codex={'backend': 'ssh', 'remote': {'host': 'agent.invalid'}},
                                  sessions={'report_by_bot': False})
        self.addCleanup(self.app.archive.close)
        self.bot = onboard(self.app)['bot']
        with patch('snaplab.gitops.available', return_value=False):
            self.project = self.app.create_project(name='Example', bot=self.bot['id'])

    def test_long_work_caps_are_off_by_default(self):
        from snaplab.agent import step_cap
        from snaplab.harnesses.remote import call_cap
        from snaplab.sessions import remaining_calls
        self.assertEqual(call_cap(self.app, {}), 0)                       # 0 = the translator refuses nothing
        self.assertEqual(step_cap(self.app.cfg['sessions'].get('max_steps')), 10**9)
        self.assertEqual(step_cap(self.app.cfg['chat'].get('max_steps')), 10**9)
        self.assertEqual(self.app.sessions.auto_continue_limit(), float('inf'))
        self.assertEqual(self.app.chat.dialogue.settings()['max_hourly'], float('inf'))
        self.assertEqual(remaining_calls(self.app, TurnResult(ok=False, extra={'proxy_calls': 500})), 0)
        self.app.cfg['sessions']['max_api_calls'] = 80
        self.assertEqual(call_cap(self.app, {}), 80)
        self.assertEqual(remaining_calls(self.app, TurnResult(ok=False, extra={'proxy_calls': 30})), 50)

    def test_capture_accepts_query_and_fragment_but_only_as_a_suffix(self):
        from snaplab.harnesses.capture import split_page
        self.assertEqual(split_page('index.html?cam=1'), ('index.html', '?cam=1'))
        self.assertEqual(split_page('a/b.html#top'), ('a/b.html', '#top'))
        self.assertEqual(split_page('index.html'), ('index.html', ''))
        for bad in ('index.html?x="y"', 'index.html?a b', 'index.html?a\\b', 'index.html#\n'):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                split_page(bad)

    def test_failed_remote_turn_says_why(self):
        from types import SimpleNamespace
        from snaplab.harnesses.remote import failure_reason
        from snaplab.proxy import _short_error
        quiet = SimpleNamespace(call_limit_reached=False, last_error='', incomplete=False, max_calls=0)
        self.assertIn('time limit (30 min)', failure_reason(124, False, quiet, 1800))
        self.assertIn('stopped without saying', failure_reason(0, False, quiet, float('inf')))
        busy = SimpleNamespace(**{**vars(quiet), 'last_error': 'model server answered 503: ' + _short_error(
            '{"error":{"message":"The GPU box is not answering right now.","code":"yami_503"}}')})
        reason = failure_reason(0, False, busy, float('inf'))
        self.assertIn('503: The GPU box is not answering right now.', reason)
        self.assertIn('continuing usually works', reason)
        self.assertEqual(_short_error('<!DOCTYPE html><html>cloudflare</html>'), '')
        capped = SimpleNamespace(**{**vars(quiet), 'call_limit_reached': True, 'max_calls': 60})
        self.assertIn('model call limit (60)', failure_reason(0, True, capped, float('inf')))

    def test_routing_and_local_compatibility(self):
        self.assertEqual(select(self.app, 'Fix the Python bug'), 'pi')
        self.assertEqual(select(self.app, 'Create a Web app'), 'pi')
        self.assertEqual(select(self.app, '画面を操作してください'), 'codex')
        self.assertEqual(select(self.app, 'Use Codex for this code'), 'codex')
        self.assertEqual(select(self.app, 'Read these documents', kind='research'), 'pi')
        self.app.cfg['codex']['backend'] = 'local'
        self.assertEqual(select(self.app, 'Fix the Python bug'), 'codex')

    def test_optional_harness_and_no_harness_in_chat_card(self):
        from snaplab.sessions import result_card_text
        box = Toolbox(self.app, self.bot)
        schema = next(x['function'] for x in box.schemas() if x['function']['name'] == 'start_session')
        self.assertNotIn('harness', schema['parameters']['required'])
        text = result_card_text({'session': 's-one', 'harness': 'pi', 'status': 'done', 'turn': 1, 'duration_s': 1}, 'en')
        self.assertNotIn('(pi)', text)

    def test_pi_launch_does_not_trust_project_or_persist_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            req = {'harness': 'pi', 'mcp_url': 'http://127.0.0.1:35000/mcp/s-one',
                   'model': 'qwen', 'effort': 'medium', 'instructions': 'Do the task.',
                   'pi_config': adapters.pi_config('qwen', 35000, 131072, 20000)}
            cmd = adapters.launch(req, Path('/home/snaplab'), Path('/home/snaplab/jobs/s-one'), tmp)
            for flag in ('--no-approve', '--no-extensions', '--no-session', '--offline'):
                self.assertIn(flag, cmd)
            self.assertIn('read,bash,edit,write,mcp__snaplab_web__*', cmd)
            safety = Path(tmp, 'pi-safety.mjs').read_text(encoding='utf-8')
            self.assertIn('spawnHook', safety)
            self.assertIn('KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL', safety)
            self.assertIn('snaplab.safety_ready', safety)

    def test_pi_length_even_exit_zero_never_completes(self):
        events = adapters.events('pi', {'type': 'message_end', 'message': {
            'role': 'assistant', 'stopReason': 'length', 'content': [{'type': 'text', 'text': 'Done'}]}})
        self.assertIn({'kind': 'failure'}, events)
        self.assertNotIn({'kind': 'done'}, events)

    def test_completion_requires_files_tests_and_explanation(self):
        folder = Path(self.tmp.name)
        result = TurnResult(ok=True, last_message='Done')
        self.assertIsNotNone(completion_error(result, folder, ['result.txt']))
        (folder / 'result.txt').write_text('partial')
        self.assertIsNotNone(completion_error(result, folder, ['result.txt'], True))
        result.extra['tests'] = {'status': 'passed'}
        self.assertIsNone(completion_error(result, folder, ['result.txt'], True))
        for status in ('no tests', 'unchanged'):
            result.extra['tests'] = {'status': status}
            self.assertIsNone(completion_error(result, folder, ['result.txt'], True))
        for status in ('failed', 'error', 'timeout'):
            result.extra['tests'] = {'status': status}
            self.assertIsNotNone(completion_error(result, folder, ['result.txt'], True))
        result.extra['tests'] = {'status': 'passed'}
        result.last_message = ''
        self.assertIsNotNone(completion_error(result, folder, ['result.txt'], True))

    def test_fallback_once_and_passes_failure_files_and_budget(self):
        self.app.cfg['sessions']['max_api_calls'] = 60      # no cap by default; a set cap is shared with the fallback
        with patch.object(self.app.sessions, '_start'), patch('snaplab.harnesses.remote.available', return_value=(True, '')):
            session = self.app.sessions.create(self.project, prompt='Fix code', started_by=self.bot['id'])
        folder = self.app.sessions.workdir(session, self.project)
        def finish(s, f, prompt, ctl, first):
            self.assertEqual(s['remaining_api_calls'], 58)
            self.assertIn('startup failed', prompt)
            (f / 'done.txt').write_text('done')
            return TurnResult(ok=True, last_message='Created done.txt')
        failed = TurnResult(ok=False, error='startup failed', extra={'safe_fallback': True, 'no_progress': True, 'proxy_calls': 2})
        with patch.object(self.app.sessions.harnesses['pi'], 'run_turn', return_value=failed), \
             patch.object(self.app.sessions.harnesses['codex'], 'run_turn', side_effect=finish) as fallback:
            self.app.sessions._turn(session['id'], 'Fix code', True, Control(self.app.sessions, session['id']))
        saved = self.app.store.session(session['id'])
        self.assertTrue(saved['fallback_used'])
        self.assertEqual(saved['harness'], 'codex')
        self.assertEqual(saved['status'], 'done')
        self.assertEqual(fallback.call_count, 1)

    def test_scaffold_alone_is_not_an_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / 'AGENTS.md').write_text('Instructions')
            (folder / 'BRIEF.md').write_text('Request')
            result = TurnResult(ok=True, last_message='Done')
            self.assertIsNotNone(completion_error(result, folder, require_artifacts=True))
            (folder / 'app.py').write_text('print(1)')
            self.assertIsNone(completion_error(result, folder, require_artifacts=True))

    def test_uncertain_failure_or_progress_does_not_fallback(self):
        for extra in ({'safe_fallback': False, 'no_progress': True}, {'safe_fallback': True, 'no_progress': False}):
            with patch.object(self.app.sessions, '_start'), patch('snaplab.harnesses.remote.available', return_value=(True, '')):
                session = self.app.sessions.create(self.project, prompt='Fix code')
            with patch.object(self.app.sessions.harnesses['pi'], 'run_turn', return_value=TurnResult(ok=False, extra=extra)), \
                 patch.object(self.app.sessions.harnesses['codex'], 'run_turn') as fallback:
                self.app.sessions._turn(session['id'], 'Fix code', True, Control(self.app.sessions, session['id']))
            fallback.assert_not_called()

    def test_result_turn_may_start_new_work(self):
        # 2026-11-15: a result card no longer forbids new sessions (parallel work, a fresh reviewer with from_session).
        room = self.app.store.dm_room(self.bot['id'])
        msg = self.app.store.add_message(room['id'], {'role': 'system', 'session': 's-one', 'result': {'status': 'done'}, 'text': 'Done'})
        box = Toolbox(self.app, self.bot, room=room, origin=msg['id'])
        self.assertFalse(box.report_only)
        self.assertIn('send_to_session', [t['function']['name'] for t in box.schemas()])
        with patch.object(self.app.sessions, '_start'), patch('snaplab.harnesses.remote.available', return_value=(True, '')):
            out = box.t_start_session(self.project['id'], 'Review it with fresh eyes')
        self.assertIn('"started": true', out.content)
        self.assertEqual(box.auto_left, float('inf'))      # no cap on continuations by default

    def _result_turn(self, s, room):
        msg = self.app.store.add_message(room['id'], {'role': 'system', 'session': s['id'], 'result': {'status': 'done'}, 'text': 'Done'})
        return Toolbox(self.app, self.bot, room=room, origin=msg['id'])

    def test_result_turn_continues_until_the_limit_and_the_user_resets_it(self):
        self.app.cfg['sessions']['auto_continue'] = 2
        room = self.app.store.dm_room(self.bot['id'])
        with patch.object(self.app.sessions, '_start'), patch('snaplab.harnesses.remote.available', return_value=(True, '')):
            s = self.app.sessions.create(self.project, prompt='Build a game', started_by=self.bot['id'], room=room['id'])
        self.app.store.save_session({**self.app.store.session(s['id']), 'status': 'review'})
        with patch.object(self.app.sessions, '_start'):
            for expected in (1, 2):
                box = self._result_turn(s, room)
                self.assertEqual(box.auto_left, 3 - expected)
                box.t_send_to_session(s['id'], 'Finish the remaining levels')
                self.assertEqual(self.app.store.session(s['id'])['auto_continues'], expected)
            box = self._result_turn(s, room)
            self.assertTrue(box.report_only)
            self.assertEqual(box.schemas(), [])
            with self.assertRaises(ValueError):
                box.t_send_to_session(s['id'], 'Keep going')
            # A follow-up from an ordinary turn (the user asked) starts a new count.
            Toolbox(self.app, self.bot, room=room).t_send_to_session(s['id'], 'Keep going')
        self.assertEqual(self.app.store.session(s['id'])['auto_continues'], 0)
        self.assertFalse(self._result_turn(s, room).report_only)

    def test_real_starts_and_instructions_leave_a_snaplab_note(self):
        room = self.app.store.dm_room(self.bot['id'])
        with patch.object(self.app.sessions, '_start'), patch('snaplab.harnesses.remote.available', return_value=(True, '')):
            s = self.app.sessions.create(self.project, prompt='Build a page', started_by=self.bot['id'], room=room['id'])
        note = self.app.store.messages(room['id'])[-1]
        self.assertEqual((note['role'], note['key'], note['session'], note['work_notice']), ('system', 'note.work_started', s['id'], True))
        self.assertIn(s['id'], note['text'])
        self.assertIn('Build a page', note['text'])
        self.app.store.save_session({**self.app.store.session(s['id']), 'status': 'review'})
        self.app.store.add_message(room['id'], {'role': 'bot', 'bot': self.bot['id'], 'status': 'done', 'text': 'Sent'})
        with patch.object(self.app.sessions, '_start'):
            Toolbox(self.app, self.bot, room=room).t_send_to_session(s['id'], '@x Finish it')
        note = self.app.store.messages(room['id'])[-1]
        self.assertEqual(note['key'], 'note.work_sent')
        self.assertIn('@x Finish it', note['text'])
        # The note is SnapLab's record, not new input that the bot still has to answer.
        self.assertTrue(self.app.chat.already_answered(room['id'], self.bot['id']))

    def test_result_turn_does_not_continue_a_stopped_session(self):
        room = self.app.store.dm_room(self.bot['id'])
        with patch.object(self.app.sessions, '_start'), patch('snaplab.harnesses.remote.available', return_value=(True, '')):
            s = self.app.sessions.create(self.project, prompt='Build a game', started_by=self.bot['id'], room=room['id'])
            other = self.app.sessions.create(self.project, prompt='Other', started_by='user', room=room['id'])
        self.app.store.save_session({**self.app.store.session(other['id']), 'status': 'stopped'})
        with self.assertRaises(ValueError), patch.object(self.app.sessions, '_start'):
            self._result_turn(s, room).t_send_to_session(other['id'], 'Continue')

    def test_bot_report_keeps_result_context_and_uses_notification(self):
        room = self.app.store.dm_room(self.bot['id'])
        with patch.object(self.app.sessions, '_start'), patch('snaplab.harnesses.remote.available', return_value=(True, '')):
            s = self.app.sessions.create(self.project, prompt='Build a page', started_by=self.bot['id'], room=room['id'])
        s['result'] = {'session': s['id'], 'turn': 1, 'duration_s': 1, 'status': 'done',
                       'attachments': [{'id': 'f-photo', 'kind': 'image', 'name': 'screenshot.png'}]}
        for enabled in (True, False):
            self.app.cfg['sessions']['report_by_bot'] = enabled
            with patch.object(self.app.chat, '_enqueue') as enqueue:
                self.app.sessions._announce(s, self.project, 'done')
            msg = self.app.store.messages(room['id'])[-1]
            self.assertEqual(msg['result_notification'], enabled)
            self.assertEqual(msg['attachments'], s['result']['attachments'])
            self.assertEqual(msg['result']['status'], 'done')
            self.assertEqual(enqueue.called, enabled)

    def test_followup_can_correct_test_runner_configuration(self):
        with patch.object(self.app.sessions, '_start'), patch('snaplab.harnesses.remote.available', return_value=(True, '')):
            s = self.app.sessions.create(self.project, prompt='Fix code', started_by=self.bot['id'])
        box = Toolbox(self.app, self.bot)
        with patch.object(self.app.sessions, 'send') as send:
            box.t_send_to_session(s['id'], 'Verify the saved files', test_command='python3 -m unittest -v')
        send.assert_called_once()
        self.assertEqual(self.app.store.project(self.project['id'])['test_command'], 'python3 -m unittest -v')

    def test_japanese_rejects_russian_caption_but_russian_is_supported(self):
        from snaplab.prompts import wrong_language, house_rules
        self.assertTrue(wrong_language('保存した写真 Сохранено', 'ja', '写真を送ってください'))
        self.assertFalse(wrong_language('Сохранённый снимок экрана', 'ru', 'Покажите снимок'))
        self.assertNotIn('polite вы', house_rules('ja'))

    def test_proxy_cap_deduplication_and_output_limit(self):
        owner = Translator(port=0, api_cfg=self.app.cfg['api'], slots=self.app.slots,
                           key_fn=lambda: 'synthetic-upstream-key', token='test-token',
                           log_path=Path(self.tmp.name)/'proxy.log', max_calls=1, output_limit=20000, effort='low')
        body = request_body()
        body['stream'] = False
        with patch('snaplab.proxy.open_request', return_value=sse([{'choices': [{'delta': {'content': 'Done'}, 'finish_reason': 'stop'}]}])) as upstream:
            header, _ = proxy_tests.NamespaceProxyTest.http_request(self, owner, body)
            self.assertIn('200', header)
            sent = json.loads(upstream.call_args.args[0].data)
            self.assertEqual(sent['max_tokens'], 20000)
            self.assertEqual(sent['chat_template_kwargs']['reasoning_effort'], 'low')
            header, _ = proxy_tests.NamespaceProxyTest.http_request(self, owner, body)
            self.assertIn('409', header)
            self.assertEqual(upstream.call_count, 1)
