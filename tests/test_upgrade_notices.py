"""In-session package-upgrade notices and private receipt semantics."""
import contextlib
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from codex_deepseek_team import (claude_config, config, coordinator_hooks,
                                 upgrade_notices)


OLD_VERSION = '0.11.0'
NEW_VERSION = '0.12.0'


class UpgradeNoticeCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='dst-upgrade-notices-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.home = self.root / 'home'
        self.home.mkdir()
        self.codex_home = self.root / 'codex'
        self.claude_home = self.root / 'claude'
        self.xdg_config = self.root / 'xdg-config'
        self.xdg_state = self.root / 'xdg-state'
        self.state = self.root / 'state'
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        self.entrypoint = self.bin / 'deepseek-team'
        self.entrypoint.write_text('#!/bin/sh\nexit 0\n', encoding='utf-8')
        self.entrypoint.chmod(0o700)
        self.environment = mock.patch.dict(os.environ, {
            'HOME': str(self.home),
            'CODEX_HOME': str(self.codex_home),
            'CLAUDE_CONFIG_DIR': str(self.claude_home),
            'XDG_CONFIG_HOME': str(self.xdg_config),
            'XDG_STATE_HOME': str(self.xdg_state),
            'DEEPSEEK_TEAM_STATE_DIR': str(self.state),
            'DEEPSEEK_TEAM_DISABLED': '',
            'PATH': str(self.bin) + os.pathsep + os.environ.get('PATH', '/usr/bin:/bin'),
            'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'src'),
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.codex_home.mkdir(mode=0o700)
        self.claude_home.mkdir(mode=0o700)
        config.install_codex_hooks(self.codex_home)
        claude_config.install(self.claude_home, executable=self.entrypoint)

    def payload(self, event, runtime, session='session-1', **extra):
        payload = {
            'hook_event_name': event,
            'session_id': session,
            'cwd': str(self.root),
        }
        if runtime == 'codex':
            payload['turn_id'] = 'turn-1'
        payload.update(extra)
        return payload

    def prepare(self, event, runtime, *, version, session='session-1', **extra):
        payload = self.payload(event, runtime, session=session, **extra)
        with mock.patch.object(upgrade_notices, '_package_version', return_value=version):
            return upgrade_notices.prepare_notice(payload, runtime), payload

    def seed(self, event, runtime, *, version=OLD_VERSION, session='session-1', **extra):
        notice, _payload = self.prepare(event, runtime, version=version,
                                        session=session, **extra)
        self.assertIsNone(notice)

    def receipt(self, runtime):
        return upgrade_notices.receipt_path(runtime)

    def assert_notice(self, notice, runtime):
        self.assertIsInstance(notice, dict)
        self.assertIn('deepseek-team hooks refresh --runtime ' + runtime + ' --notify --json',
                      notice['message'])
        self.assertIn('required_actions', notice['message'])
        self.assertIn('pending', notice['message'].lower())

    def test_raw_upgrade_in_same_turn_reaches_pretool_and_stop_for_both_runtimes(self):
        for runtime in ('codex', 'claude'):
            with self.subTest(runtime=runtime, event='PreToolUse'):
                self.seed('SessionStart', runtime, session='upgrade-pretool-' + runtime,
                          source='startup')
                notice, payload = self.prepare(
                    'PreToolUse', runtime, version=NEW_VERSION,
                    session='upgrade-pretool-' + runtime, tool_name='Read',
                    tool_input={'file_path': str(self.root / 'a.py')})
                self.assert_notice(notice, runtime)
                self.assertEqual(notice['previous_version'], OLD_VERSION)
                result = upgrade_notices.annotate_result(
                    {'hookSpecificOutput': {'hookEventName': 'PreToolUse',
                                            'additionalContext': 'existing context'}},
                    payload, runtime, notice)
                self.assertIn('existing context', result['hookSpecificOutput']['additionalContext'])
                self.assertIn(notice['message'], result['hookSpecificOutput']['additionalContext'])
                upgrade_notices.mark_delivered(notice)
                again, _ = self.prepare(
                    'PreToolUse', runtime, version=NEW_VERSION,
                    session='upgrade-pretool-' + runtime, tool_name='Read',
                    tool_input={'file_path': str(self.root / 'b.py')})
                self.assertIsNone(again)

            with self.subTest(runtime=runtime, event='Stop'):
                session = 'upgrade-stop-' + runtime
                self.seed('UserPromptSubmit', runtime, session=session, prompt='upgrade')
                notice, payload = self.prepare('Stop', runtime, version=NEW_VERSION,
                                               session=session)
                self.assert_notice(notice, runtime)
                result = upgrade_notices.annotate_result(
                    {'continue': True}, payload, runtime, notice)
                self.assertEqual(result['decision'], 'block')
                self.assertIn(notice['message'], result['reason'])
                upgrade_notices.mark_delivered(notice)
                repeated, _ = self.prepare('Stop', runtime, version=NEW_VERSION,
                                           session=session, stop_hook_active=True)
                self.assertIsNone(repeated)

    def test_another_session_or_global_observation_does_not_hide_a_warning(self):
        for runtime in ('codex', 'claude'):
            with self.subTest(runtime=runtime):
                old_session = 'old-' + runtime
                new_session = 'new-' + runtime
                direct_session = 'direct-' + runtime
                self.seed('SessionStart', runtime, version=OLD_VERSION,
                          session=old_session, source='startup')
                self.seed('SessionStart', runtime, version=NEW_VERSION,
                          session=new_session, source='startup')

                old_notice, _ = self.prepare('PreToolUse', runtime, version=NEW_VERSION,
                                             session=old_session, tool_name='Read',
                                             tool_input={'file_path': str(self.root / 'old.py')})
                self.assert_notice(old_notice, runtime)
                self.assertEqual(old_notice['previous_version'], OLD_VERSION)
                upgrade_notices.mark_delivered(old_notice)

                direct_notice, _ = self.prepare('PreToolUse', runtime, version=NEW_VERSION,
                                                session=direct_session, tool_name='Read',
                                                tool_input={'file_path': str(self.root / 'new.py')})
                self.assert_notice(direct_notice, runtime)
                self.assertEqual(direct_notice['kind'], 'unknown-session')
                upgrade_notices.mark_delivered(direct_notice)
                repeated, _ = self.prepare('PreToolUse', runtime, version=NEW_VERSION,
                                           session=direct_session, tool_name='Read',
                                           tool_input={'file_path': str(self.root / 'new.py')})
                self.assertIsNone(repeated)

    def test_unchanged_normal_sessions_seed_without_repeated_warnings(self):
        for runtime in ('codex', 'claude'):
            with self.subTest(runtime=runtime):
                for session, event in (('normal-one-' + runtime, 'SessionStart'),
                                       ('normal-two-' + runtime, 'UserPromptSubmit')):
                    self.seed(event, runtime, version=NEW_VERSION, session=session,
                              source='startup', prompt='ordinary work')
                    notice, _ = self.prepare('PreToolUse', runtime, version=NEW_VERSION,
                                             session=session, tool_name='Read',
                                             tool_input={'file_path': str(self.root / 'a.py')})
                    self.assertIsNone(notice)

    def test_missing_receipt_issues_initial_guidance_only_once(self):
        for runtime in ('codex', 'claude'):
            with self.subTest(runtime=runtime):
                session = 'first-' + runtime
                self.assertFalse(self.receipt(runtime).exists())
                notice, payload = self.prepare('PreToolUse', runtime, version=NEW_VERSION,
                                               session=session, tool_name='Read',
                                               tool_input={'file_path': str(self.root / 'a.py')})
                self.assert_notice(notice, runtime)
                self.assertEqual(notice['kind'], 'initial')
                result = upgrade_notices.annotate_result({}, payload, runtime, notice)
                self.assertIn('first', result['hookSpecificOutput']['additionalContext'].lower())
                upgrade_notices.mark_delivered(notice)
                repeated, _ = self.prepare('PreToolUse', runtime, version=NEW_VERSION,
                                           session=session, tool_name='Read',
                                           tool_input={'file_path': str(self.root / 'b.py')})
                self.assertIsNone(repeated)
                self.assertEqual(stat.S_IMODE(self.receipt(runtime).stat().st_mode), 0o600)

    def test_annotation_preserves_gate_denials_stop_reasons_and_repeated_stop(self):
        payload = self.payload('PreToolUse', 'codex')
        notice = {'message': 'notice text', 'runtime': 'codex', 'event': 'PreToolUse'}
        denied = {
            'hookSpecificOutput': {
                'hookEventName': 'PreToolUse', 'permissionDecision': 'deny',
                'permissionDecisionReason': 'original gate reason',
            }
        }
        annotated = upgrade_notices.annotate_result(denied, payload, 'codex', notice)
        self.assertEqual(annotated['hookSpecificOutput']['permissionDecision'], 'deny')
        self.assertIn('original gate reason', annotated['hookSpecificOutput']['permissionDecisionReason'])
        self.assertIn('notice text', annotated['hookSpecificOutput']['permissionDecisionReason'])

        blocked = {'decision': 'block', 'reason': 'ledger still unfinished'}
        annotated = upgrade_notices.annotate_result(
            blocked, self.payload('Stop', 'codex'), 'codex', notice)
        self.assertEqual(annotated['decision'], 'block')
        self.assertIn('ledger still unfinished', annotated['reason'])
        self.assertIn('notice text', annotated['reason'])

        repeated = upgrade_notices.annotate_result(
            {'continue': True, 'systemMessage': 'existing warning'},
            self.payload('Stop', 'codex', stop_hook_active=True), 'codex', notice)
        self.assertNotEqual(repeated.get('decision'), 'block')
        self.assertIn('existing warning', repeated['systemMessage'])
        self.assertIn('notice text', repeated['systemMessage'])

    def test_long_context_and_stop_reasons_keep_the_notice_first(self):
        text = 'required upgrade instruction'
        notice = {'message': text, 'runtime': 'codex', 'event': 'UserPromptSubmit'}
        long_context = 'policy guidance\n' * 15000
        long_reason = 'existing gate reason\n' * 15000

        for event in ('SessionStart', 'UserPromptSubmit'):
            with self.subTest(event=event):
                annotated = upgrade_notices.annotate_result(
                    {'hookSpecificOutput': {'hookEventName': event,
                                            'additionalContext': long_context}},
                    self.payload(event, 'codex'), 'codex', notice)
                context = annotated['hookSpecificOutput']['additionalContext']
                self.assertTrue(context.startswith(text + '\n\n'))
                self.assertTrue(context.endswith(long_context))
                self.assertEqual(context.find(long_context), len(text) + 2)

        denied = upgrade_notices.annotate_result(
            {'hookSpecificOutput': {
                'hookEventName': 'PreToolUse', 'permissionDecision': 'deny',
                'permissionDecisionReason': long_reason,
            }}, self.payload('PreToolUse', 'codex'), 'codex', notice)
        reason = denied['hookSpecificOutput']['permissionDecisionReason']
        self.assertEqual(denied['hookSpecificOutput']['permissionDecision'], 'deny')
        self.assertTrue(reason.startswith(text + '\n\n'))
        self.assertTrue(reason.endswith(long_reason))

        blocked = upgrade_notices.annotate_result(
            {'decision': 'block', 'reason': long_reason},
            self.payload('Stop', 'codex'), 'codex', notice)
        self.assertEqual(blocked['decision'], 'block')
        self.assertTrue(blocked['reason'].startswith(text + '\n\n'))
        self.assertTrue(blocked['reason'].endswith(long_reason))

        repeated = upgrade_notices.annotate_result(
            {'continue': True, 'systemMessage': long_reason},
            self.payload('Stop', 'codex', stop_hook_active=True), 'codex', notice)
        self.assertTrue(repeated['continue'])
        self.assertNotEqual(repeated.get('decision'), 'block')
        self.assertTrue(repeated['systemMessage'].startswith(text + '\n\n'))
        self.assertTrue(repeated['systemMessage'].endswith(long_reason))

    def test_main_keeps_long_existing_gate_text_after_the_notice(self):
        long_context = 'policy guidance\n' * 15000
        long_reason = 'existing gate reason\n' * 15000
        cases = {
            'SessionStart': {'hookSpecificOutput': {
                'hookEventName': 'SessionStart', 'additionalContext': long_context}},
            'UserPromptSubmit': {'hookSpecificOutput': {
                'hookEventName': 'UserPromptSubmit', 'additionalContext': long_context}},
            'PreToolUse': {'hookSpecificOutput': {
                'hookEventName': 'PreToolUse', 'permissionDecision': 'deny',
                'permissionDecisionReason': long_reason}},
            'Stop': {'decision': 'block', 'reason': long_reason},
        }
        for runtime in ('codex', 'claude'):
            for event, original in cases.items():
                with self.subTest(runtime=runtime, event=event):
                    session = 'main-long-' + runtime + '-' + event
                    self.seed('SessionStart', runtime, session=session)
                    payload = self.payload(event, runtime, session=session)
                    if event == 'PreToolUse':
                        payload.update(tool_name='Read', tool_input={'file_path': 'a.py'})
                    stdout, stderr = io.StringIO(), io.StringIO()
                    with mock.patch.object(upgrade_notices, '_package_version',
                                           return_value=NEW_VERSION), \
                            mock.patch.object(coordinator_hooks, 'handle',
                                              return_value=original), \
                            mock.patch('sys.stdin', io.StringIO(json.dumps(payload))), \
                            contextlib.redirect_stdout(stdout), \
                            contextlib.redirect_stderr(stderr):
                        code = coordinator_hooks.main(['--runtime', runtime])

                    self.assertEqual(code, 0)
                    self.assertEqual(stderr.getvalue(), '')
                    body = json.loads(stdout.getvalue())
                    if event == 'Stop':
                        annotated = body['reason']
                        self.assertEqual(body['decision'], 'block')
                    elif event == 'PreToolUse':
                        annotated = body['hookSpecificOutput']['permissionDecisionReason']
                        self.assertEqual(body['hookSpecificOutput']['permissionDecision'],
                                         'deny')
                    else:
                        annotated = body['hookSpecificOutput']['additionalContext']
                    expected = long_reason if event in ('PreToolUse', 'Stop') else long_context
                    self.assertTrue(annotated.startswith(
                        'DeepSeek Team changed from ' + OLD_VERSION + ' to ' + NEW_VERSION))
                    self.assertTrue(annotated.endswith(expected))
                    self.assertEqual(annotated.count(expected), 1)
                    self.assertEqual(stdout.getvalue().count('\n'), 1)

    def test_main_prepare_failure_reports_recovery_without_losing_gate(self):
        for runtime in ('codex', 'claude'):
            with self.subTest(runtime=runtime):
                payload = self.payload('PreToolUse', runtime)
                payload.update(tool_name='Read', tool_input={'file_path': 'a.py'})
                original = {'hookSpecificOutput': {
                    'hookEventName': 'PreToolUse', 'permissionDecision': 'deny',
                    'permissionDecisionReason': 'original denial',
                }}
                secret = 'arbitrary-private-exception-text'
                stdout, stderr = io.StringIO(), io.StringIO()
                with mock.patch.object(coordinator_hooks, 'handle', return_value=original), \
                        mock.patch.object(upgrade_notices, 'prepare_notice',
                                          side_effect=RuntimeError(secret)), \
                        mock.patch.object(upgrade_notices, 'mark_delivered') as delivered, \
                        mock.patch('sys.stdin', io.StringIO(json.dumps(payload))), \
                        contextlib.redirect_stdout(stdout), \
                        contextlib.redirect_stderr(stderr):
                    code = coordinator_hooks.main(['--runtime', runtime])

                self.assertEqual(code, 0)
                self.assertEqual(json.loads(stdout.getvalue()), original)
                self.assertEqual(stdout.getvalue().count('\n'), 1)
                diagnostic = stderr.getvalue()
                self.assertIn('upgrade notice check failed', diagnostic.lower())
                self.assertIn('deepseek-team hooks refresh --runtime ' + runtime
                              + ' --notify --json', diagnostic)
                self.assertIn('required user action', diagnostic.lower())
                self.assertNotIn(secret, diagnostic)
                self.assertNotIn('Traceback', diagnostic)
                self.assertLess(len(diagnostic), 600)
                self.assertEqual(diagnostic.count('\n'), 1)
                delivered.assert_not_called()

    def test_main_annotation_failure_reports_recovery_without_losing_gate(self):
        for runtime in ('codex', 'claude'):
            with self.subTest(runtime=runtime):
                payload = self.payload('PreToolUse', runtime)
                payload.update(tool_name='Read', tool_input={'file_path': 'a.py'})
                original = {'hookSpecificOutput': {
                    'hookEventName': 'PreToolUse', 'permissionDecision': 'deny',
                    'permissionDecisionReason': 'original denial',
                }}
                notice = {'message': 'required upgrade instruction', 'runtime': runtime,
                          'event': 'PreToolUse'}
                secret = 'arbitrary-private-exception-text'
                stdout, stderr = io.StringIO(), io.StringIO()
                with mock.patch.object(coordinator_hooks, 'handle', return_value=original), \
                        mock.patch.object(upgrade_notices, 'prepare_notice',
                                          return_value=notice), \
                        mock.patch.object(upgrade_notices, 'annotate_result',
                                          side_effect=RuntimeError(secret)), \
                        mock.patch.object(upgrade_notices, 'mark_delivered') as delivered, \
                        mock.patch('sys.stdin', io.StringIO(json.dumps(payload))), \
                        contextlib.redirect_stdout(stdout), \
                        contextlib.redirect_stderr(stderr):
                    code = coordinator_hooks.main(['--runtime', runtime])

                self.assertEqual(code, 0)
                self.assertEqual(json.loads(stdout.getvalue()), original)
                self.assertEqual(stdout.getvalue().count('\n'), 1)
                diagnostic = stderr.getvalue()
                self.assertIn('upgrade notice check failed', diagnostic.lower())
                self.assertIn('deepseek-team hooks refresh --runtime ' + runtime
                              + ' --notify --json', diagnostic)
                self.assertIn('required user action', diagnostic.lower())
                self.assertNotIn(secret, diagnostic)
                self.assertNotIn('Traceback', diagnostic)
                self.assertLess(len(diagnostic), 600)
                self.assertEqual(diagnostic.count('\n'), 1)
                delivered.assert_not_called()

    def test_native_subagents_foreign_hooks_and_disabled_hooks_are_skipped(self):
        self.seed('SessionStart', 'codex', session='subagent-codex')
        notice, _ = self.prepare('PreToolUse', 'codex', version=NEW_VERSION,
                                 session='subagent-codex', agent_id='native-child',
                                 tool_name='Read', tool_input={'file_path': str(self.root / 'a.py')})
        self.assertIsNone(notice)

        self.seed('SessionStart', 'claude', session='subagent-claude')
        notice, _ = self.prepare('PreToolUse', 'claude', version=NEW_VERSION,
                                 session='subagent-claude', agent_id='native-child',
                                 tool_name='Read', tool_input={'file_path': str(self.root / 'a.py')})
        self.assertIsNone(notice)

        settings = json.loads((self.claude_home / 'settings.json').read_text())
        settings['disableAllHooks'] = True
        (self.claude_home / 'settings.json').write_text(json.dumps(settings))
        notice, _ = self.prepare('PreToolUse', 'claude', version=NEW_VERSION,
                                 session='disabled-claude', tool_name='Read',
                                 tool_input={'file_path': str(self.root / 'a.py')})
        self.assertIsNone(notice)

        empty = self.root / 'empty-codex'
        empty.mkdir(mode=0o700)
        with mock.patch.dict(os.environ, {'CODEX_HOME': str(empty)}):
            notice, _ = self.prepare('PreToolUse', 'codex', version=NEW_VERSION,
                                     session='foreign-only', tool_name='Read',
                                     tool_input={'file_path': str(self.root / 'a.py')})
        self.assertIsNone(notice)

    def test_malformed_symlink_and_unsafe_receipts_are_preserved(self):
        runtime = 'codex'
        path = self.receipt(runtime)
        self.seed('SessionStart', runtime, session='unsafe-seed')
        malformed = b'{this is not valid json\n'
        path.write_bytes(malformed)
        path.chmod(0o600)
        notice, payload = self.prepare('PreToolUse', runtime, version=NEW_VERSION,
                                       session='unsafe-seed', tool_name='Read',
                                       tool_input={'file_path': str(self.root / 'a.py')})
        self.assertIsInstance(notice, dict)
        result = upgrade_notices.annotate_result({}, payload, runtime, notice)
        self.assertIn('hookSpecificOutput', result)
        with self.assertRaises(upgrade_notices.ReceiptError):
            upgrade_notices.mark_delivered(notice)
        self.assertEqual(path.read_bytes(), malformed)

        path.unlink()
        target = self.root / 'foreign-receipt.json'
        target.write_bytes(b'{"foreign": true}\n')
        path.symlink_to(target)
        notice, _ = self.prepare('PreToolUse', runtime, version=NEW_VERSION,
                                 session='unsafe-seed', tool_name='Read',
                                 tool_input={'file_path': str(self.root / 'a.py')})
        self.assertIsInstance(notice, dict)
        with self.assertRaises(upgrade_notices.ReceiptError):
            upgrade_notices.mark_delivered(notice)
        self.assertTrue(path.is_symlink())
        self.assertEqual(target.read_bytes(), b'{"foreign": true}\n')

        path.unlink()
        unsafe = json.dumps({
            'schema_version': 1, 'global_version': NEW_VERSION,
            'initial_review_delivered': True, 'sessions': {},
        }).encode() + b'\n'
        path.write_bytes(unsafe)
        path.chmod(0o644)
        notice, _ = self.prepare('PreToolUse', runtime, version=NEW_VERSION,
                                 session='unsafe-seed', tool_name='Read',
                                 tool_input={'file_path': str(self.root / 'a.py')})
        self.assertIsInstance(notice, dict)
        with self.assertRaises(upgrade_notices.ReceiptError):
            upgrade_notices.mark_delivered(notice)
        self.assertEqual(path.read_bytes(), unsafe)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o644)

    def test_main_publishes_one_json_then_reports_receipt_failure(self):
        for runtime in ('codex', 'claude'):
            with self.subTest(runtime=runtime):
                session = 'main-' + runtime
                self.seed('SessionStart', runtime, session=session)
                payload = self.payload('PreToolUse', runtime, session=session,
                                       tool_name='Read',
                                       tool_input={'file_path': str(self.root / 'a.py')})
                original = {'hookSpecificOutput': {
                    'hookEventName': 'PreToolUse', 'permissionDecision': 'deny',
                    'permissionDecisionReason': 'original denial',
                }}
                stdout, stderr = io.StringIO(), io.StringIO()
                observed_stdout = []

                def fail_after_print(notice):
                    observed_stdout.append(stdout.getvalue())
                    raise upgrade_notices.ReceiptError('receipt write failed safely')

                with mock.patch.object(upgrade_notices, '_package_version', return_value=NEW_VERSION), \
                        mock.patch.object(coordinator_hooks, 'handle', return_value=original), \
                        mock.patch.object(upgrade_notices, 'mark_delivered',
                                          side_effect=fail_after_print), \
                        mock.patch('sys.stdin', io.StringIO(json.dumps(payload))), \
                        contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    code = coordinator_hooks.main(['--runtime', runtime])

                self.assertEqual(code, 0)
                published = json.loads(stdout.getvalue())
                self.assertEqual(published['hookSpecificOutput']['permissionDecision'], 'deny')
                self.assertIn('original denial',
                              published['hookSpecificOutput']['permissionDecisionReason'])
                self.assertIn('hooks refresh --runtime ' + runtime,
                              published['hookSpecificOutput']['permissionDecisionReason'])
                self.assertEqual(len(observed_stdout), 1)
                self.assertEqual(json.loads(observed_stdout[0])['hookSpecificOutput']
                                 ['permissionDecisionReason'],
                                 published['hookSpecificOutput']['permissionDecisionReason'])
                diagnostic = stderr.getvalue().lower()
                self.assertIn('receipt', diagnostic)
                self.assertIn('retry', diagnostic)
                self.assertNotIn('traceback', diagnostic)
                self.assertEqual(stdout.getvalue().count('\n'), 1)

    def test_cli_publishes_one_notice_then_records_delivery_without_project_state(self):
        for runtime in ('codex', 'claude'):
            with self.subTest(runtime=runtime):
                session = 'cli-' + runtime
                self.seed('SessionStart', runtime, version='0.10.0-cli-control',
                          session=session)
                payload = self.payload('PreToolUse', runtime, session=session,
                                       tool_name='Read',
                                       tool_input={'file_path': str(self.root / 'a.py')})
                command = [sys.executable, '-m', 'codex_deepseek_team',
                           'coordinator-hook', '--runtime', runtime]
                current_version = upgrade_notices._package_version()
                result = subprocess.run(command, input=json.dumps(payload), text=True,
                                        capture_output=True, timeout=15, cwd=self.root)
                self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
                body = json.loads(result.stdout)
                context = body['hookSpecificOutput']['additionalContext']
                self.assertIn('hooks refresh --runtime ' + runtime, context)
                self.assertNotIn('permissionDecision', body['hookSpecificOutput'])

                state = json.loads(self.receipt(runtime).read_text())
                self.assertEqual(state['global_version'], current_version)
                self.assertTrue(all(row['delivered_version'] == current_version
                                    for row in state['sessions'].values()))

    def test_receipt_sessions_are_bounded_and_current_observations_are_retained(self):
        runtime = 'claude'
        for index in range(upgrade_notices.MAX_SESSIONS + 12):
            self.seed('SessionStart', runtime, version=NEW_VERSION,
                      session=f'bounded-{index}', source='startup')
        state = json.loads(self.receipt(runtime).read_text())
        self.assertLessEqual(len(state['sessions']), upgrade_notices.MAX_SESSIONS)
        self.assertEqual(state['global_version'], NEW_VERSION)
        self.assertTrue(all(row['version'] == NEW_VERSION for row in state['sessions'].values()))


if __name__ == '__main__':
    unittest.main()
