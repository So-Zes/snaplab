"""Round 13 regression tests, using synthetic files and credentials only."""
import json
import time
import unittest
from unittest.mock import patch

import test_round11
from snaplab.tools import Toolbox, approval_policy
from snaplab.testparse import parse_tests
from snaplab.harnesses.base import TurnResult
from snaplab.sessions import Control


class Round13Test(unittest.TestCase):
    setUp = test_round11.DeliveryTest.setUp
    produced = test_round11.DeliveryTest.produced

    def test_japanese_started_claim_requires_start_tool(self):
        from snaplab.agent import claim_gaps
        self.assertTrue(claim_gaps('コーディングセッションを起動しました。', {'create_project', 'write_brief'}, {'start_session'}))

    def test_test_process_launch_failure_is_a_test_result(self):
        from snaplab.harnesses.codex import sandbox_run
        with patch('snaplab.harnesses.codex.find_codex', return_value='synthetic'), \
             patch('snaplab.harnesses.codex.write_session_home'), \
             patch('snaplab.harnesses.codex.prepare_workspace'), \
             patch('snaplab.harnesses.codex.child_env', return_value={}), \
             patch('snaplab.harnesses.codex.subprocess.Popen', side_effect=OSError('synthetic missing binary')):
            (self.folder / '.snaplab').mkdir(exist_ok=True)
            code, out = sandbox_run(self.app, self.session['id'], self.folder, 'python -m unittest')
        self.assertIsNone(code)
        self.assertEqual(parse_tests(code, out)['status'], 'error')

    def test_local_screenshot_does_not_run_tests(self):
        self.project['test_command'] = 'python -m unittest'
        self.app.store.save_project(self.project)
        self.session['harness'] = 'codex'
        self.app.store.save_session(self.session)
        def screenshot(*args):
            (self.folder / 'screen.png').write_bytes(b'\x89PNG\r\n\x1a\nsynthetic')
            return TurnResult(ok=True, last_message='Screenshot created.')
        with patch.object(self.app.sessions.harnesses['codex'], 'run_turn', side_effect=screenshot), \
             patch('snaplab.sessions.sandbox_run') as run:
            self.app.sessions._turn(self.session['id'], 'Screenshot', False, Control(self.app.sessions, self.session['id']))
        run.assert_not_called()
        self.assertEqual(self.app.store.session(self.session['id'])['tests']['status'], 'unchanged')

    def test_changed_code_runs_tests(self):
        self.project['test_command'] = 'python -m unittest'
        self.app.store.save_project(self.project)
        self.session['harness'] = 'codex'
        self.app.store.save_session(self.session)
        def changed(*args):
            (self.folder / 'index.html').write_text('<p>Changed</p>', encoding='utf-8')
            return TurnResult(ok=True, last_message='Changed.')
        with patch.object(self.app.sessions.harnesses['codex'], 'run_turn', side_effect=changed), \
             patch('snaplab.sessions.sandbox_run', return_value=(124, 'timed out')) as run:
            self.app.sessions._turn(self.session['id'], 'Change', False, Control(self.app.sessions, self.session['id']))
        run.assert_called_once()
        self.assertEqual(self.app.store.session(self.session['id'])['tests']['status'], 'timeout')
        self.assertEqual(self.app.store.session(self.session['id'])['status'], 'failed')

    def test_ssh_dashboard_remove_and_explicit_override(self):
        self.app.cfg['codex']['backend'] = 'ssh'
        self.app.store.write_json('dashboard.json', {'widgets': [{'id': 'sample', 'type': 'note', 'title': 'Sample', 'text': 'Synthetic'}]})
        self.assertFalse(self.box.t_update_dashboard('remove', ids=['sample']).stop)
        self.app.cfg['approvals'] = {'update_dashboard': 'always'}
        self.assertTrue(self.box.t_update_dashboard('upsert', widgets=[{'id': 'sample', 'type': 'note', 'title': 'Sample', 'text': 'Synthetic'}]).stop)
    def test_any_test_command_is_accepted(self):
        # Since 2026-11-21 server and watch commands and long commands are accepted as the project's test command.
        long = 'python -m unittest ' + ' '.join(f'tests.test_{i}' for i in range(60))
        self.assertGreater(len(long), 300)
        for i, command in enumerate(('python -m http.server 8080', 'npm run dev', 'pytest --watch', long)):
            with self.subTest(command=command[:40]):
                p = self.app.create_project(name=f'any-command-{i}', test_command=command)
                self.assertEqual(p['test_command'], command)
        self.app.update_project(self.project['id'], {'test_command': 'npm run dev'})
        self.assertEqual(self.app.store.project(self.project['id'])['test_command'], 'npm run dev')

    def test_test_outcomes_are_distinct(self):
        self.assertEqual(parse_tests(None, 'stopped', timed_out=True)['status'], 'timeout')
        self.assertEqual(parse_tests(None, 'cannot start')['status'], 'error')
        self.assertEqual(parse_tests(1, '1 failed')['status'], 'failed')
        self.assertEqual(parse_tests(127, 'command not found')['status'], 'error')

    def test_remote_launch_failure(self):
        from snaplab.harnesses import remote_worker
        with patch.object(remote_worker.subprocess, 'Popen', side_effect=OSError('synthetic')):
            code, out = remote_worker.execute(['missing'], self.folder, {}, '', None, 1)
        self.assertIsNone(code)

    def test_prompts_and_tool_descriptions_match_ssh(self):
        from snaplab import prompts
        self.app.cfg['codex']['backend'] = 'ssh'
        self.assertNotIn('no internet', prompts.tools_hint('en', self.app.cfg))
        descriptions = {t['function']['name']: t['function']['description'] for t in self.box.schemas()}
        self.assertIn('autonomously', descriptions['start_session'])
        self.assertNotIn('only when the user asked', descriptions['send_to_session'])
        self.assertIn('from_session', descriptions['send_to_session'])     # a fresh session is the way to review
        self.app.cfg['codex']['backend'] = 'local'
        descriptions = {t['function']['name']: t['function']['description'] for t in self.box.schemas()}
        self.assertIn('its own task history', descriptions['send_to_session'])

    def test_no_card_by_default_on_any_backend_and_overrides(self):
        # Since 2026-11-21 no action shows a card unless config "approvals" asks, on every backend.
        for backend_cfg in ('ssh', 'local'):
            self.app.cfg['codex']['backend'] = backend_cfg
            for group in (False, True):
                self.room['kind'] = 'group' if group else 'dm'
                box = Toolbox(self.app, self.bot, room=self.room)
                box.read_web = True
                for action in ('start_session', 'create_project', 'update_dashboard', 'write_brief', 'create_bot',
                               'schedule_task', 'write_memory', 'message_bot', 'create_group', 'assign_project',
                               'share_project', 'some_future_action'):
                    self.assertIsNone(box._needs_approval({'type': action}, action), action)
                for backend in ('ssh', 'local', None):
                    self.session['execution_backend'] = backend
                    self.app.store.save_session(self.session)
                    self.assertIsNone(box._needs_approval({'type': 'send_to_session', 'session': self.session['id']}, 'send'))
        self.app.cfg['approvals'] = {'start_session': 'always', 'create_project': 'after_web'}
        self.assertEqual(approval_policy(self.app.cfg, 'start_session'), 'always')
        self.assertEqual(approval_policy(self.app.cfg, 'create_project'), 'after_web')
        self.assertEqual(approval_policy(self.app.cfg, 'write_brief'), 'never')

    def test_group_followup_has_no_card_on_any_backend(self):
        self.app.cfg['codex']['backend'] = 'ssh'
        self.room['kind'] = 'group'
        self.box = Toolbox(self.app, self.bot, room=self.room)
        self.box.read_web = True
        for backend in ('ssh', 'local'):
            self.session['execution_backend'] = backend
            self.app.store.save_session(self.session)
            with patch.object(self.app.sessions, 'send') as send:
                result = self.box.t_send_to_session(self.session['id'], 'Continue')
                self.assertFalse(result.stop)                       # a local session no longer forces a card either
                self.assertTrue(send.called)

    def test_attachment_failure_names_file_and_reason(self):
        self.produced('screenshot.png', b'\x89PNG\r\n\x1a\nsynthetic')
        (self.folder / 'index.html').write_text('<p>older unrecorded file</p>', encoding='utf-8')
        self.app.attachments.queue(self.folder, 'index.html')
        self.app.attachments.queue(self.folder, 'screenshot.png')
        attached, errors = self.app.attachments.collect(self.session, self.folder)
        self.assertEqual(len(attached), 1)
        self.assertEqual(errors[0]['path'], 'index.html')
        self.assertIn('produced', errors[0]['reason'])

    def test_bot_work_finishes_without_review_notification(self):
        self.session.update(started_by=self.bot['id'], room=self.room['id'])
        self.app.store.save_session(self.session)
        with patch.object(self.app.sessions.harnesses['bot'], 'run_turn', return_value=TurnResult(ok=True, last_message='Finished.')):
            self.app.sessions._turn(self.session['id'], 'Finish', True, Control(self.app.sessions, self.session['id']))
        s = self.app.store.session(self.session['id'])
        self.assertEqual(s['status'], 'done')
        self.app.cfg['sessions']['report_by_bot'] = True
        with patch.object(self.app.push, 'notify') as notify, patch.object(self.app.chat, '_enqueue') as enqueue:
            self.app.sessions._announce(s, self.project, 'done')
        notify.assert_not_called()
        enqueue.assert_called_once()

    def test_session_start_honours_hourly_limit(self):
        self.app.cfg['codex']['backend'] = 'ssh'
        self.app.cfg.setdefault('bot_conversations', {})['max_hourly'] = 60    # off by default; a set cap still applies
        self.app.store.write_json('bot-conversations.json', {'chains': {}, 'hour': [
            {'bot': self.bot['id'], 'ts': time.time()} for _ in range(60)]})
        with patch.object(self.app.sessions, '_start') as start, self.assertRaisesRegex(ValueError, 'hourly'):
            self.app.sessions.create(self.project, 'bot', 'Build', started_by=self.bot['id'], bot=self.bot['id'], room=self.room['id'])
        start.assert_not_called()
