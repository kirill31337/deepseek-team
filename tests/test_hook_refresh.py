"""Focused contracts for refreshing already configured coordinator hooks."""
import contextlib
import io
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from codex_deepseek_team import (claude_config, cli, config, coordinator_hooks,
                                 hook_refresh)


class HookRefreshCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='dst-hook-refresh-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.home = self.root / 'home'
        self.home.mkdir()
        self.codex = self.root / 'codex'
        self.claude = self.root / 'claude'
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        self.launcher = self.bin / 'deepseek-team'
        self.launcher.write_text(
            '#!/bin/sh\nexec ' + shlex.quote(sys.executable) +
            ' -m codex_deepseek_team "$@"\n', encoding='utf-8')
        self.launcher.chmod(0o700)
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        subprocess.run(['git', 'init', '-q', str(self.repo)], check=True)
        environment = mock.patch.dict(os.environ, {
            'HOME': str(self.home),
            'DSH_HOME': str(self.root / 'dsh-home'),
            'DEEPSEEK_TEAM_DSH_PROFILE': '',
            'CODEX_HOME': str(self.codex),
            'CLAUDE_CONFIG_DIR': str(self.claude),
            'XDG_CONFIG_HOME': str(self.root / 'xdg-config'),
            'XDG_STATE_HOME': str(self.root / 'xdg-state'),
            'DEEPSEEK_TEAM_STATE_DIR': str(self.root / 'state'),
            'PATH': str(self.bin) + os.pathsep + '/usr/bin:/bin',
            'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'src'),
        })
        environment.start()
        self.addCleanup(environment.stop)
        self.addCleanup(os.environ.pop, 'DEEPSEEK_TEAM_DISABLED', None)
        os.environ.pop('DEEPSEEK_TEAM_DISABLED', None)

    def call(self, function, argv):
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = function(list(argv))
        return code, output.getvalue(), errors.getvalue()

    def codex_path(self):
        return self.codex / 'hooks.json'

    def claude_path(self):
        return self.claude / 'settings.json'

    def install_stale_codex(self):
        self.assertTrue(config.install_codex_hooks(self.codex))
        data = json.loads(self.codex_path().read_text())
        data['user_setting'] = 'preserve-me'
        data['hooks'].setdefault('PostToolUse', []).append(
            {'hooks': [{'type': 'command', 'command': 'foreign-post'}]})
        for group in data['hooks']['PreToolUse']:
            if any(handler.get('command') == config.CODEX_HOOK_COMMAND
                   for handler in group.get('hooks', [])):
                group['matcher'] = 'Bash|Edit'
        self.codex_path().write_text(json.dumps(data, indent=2, sort_keys=True) + '\n')

    def install_stale_claude(self):
        self.assertTrue(claude_config.install(self.claude, executable=self.launcher))
        data = json.loads(self.claude_path().read_text())
        data['model'] = 'preserve-me'
        data['hooks'].setdefault('PostToolUse', []).append(
            {'hooks': [{'type': 'command', 'command': 'foreign-post'}]})
        data['hooks']['PreToolUse'][0]['matcher'] = 'Read'
        self.claude_path().write_text(json.dumps(data, indent=2))

    def rows(self, report):
        return {row['runtime']: row for row in report['hooks']}

    def codes(self, report):
        return [row['code'] for row in report['required_actions']]


class OwnershipAndStatusTests(HookRefreshCase):
    def test_auto_missing_configs_are_quiet_and_create_nothing(self):
        report = hook_refresh.refresh_installed('auto', notify=True)
        self.assertEqual(report['schema_version'], 1)
        self.assertEqual(report['status'], 'completed')
        self.assertEqual(report['hooks'], [])
        self.assertEqual(report['required_actions'], [])
        self.assertFalse(report['requires_user_action'])
        self.assertFalse(self.codex.exists())
        self.assertFalse(self.claude.exists())

    def test_auto_repairs_stale_owned_codex_hooks_and_is_idempotent(self):
        self.install_stale_codex()
        report = hook_refresh.refresh_installed('auto')
        row = self.rows(report)['codex']
        self.assertEqual(row['status'], 'refreshed')
        self.assertTrue(row['changed'])
        self.assertEqual(report['status'], 'action_required')
        self.assertIn('review_native_hooks', self.codes(report))
        repaired = json.loads(self.codex_path().read_text())
        self.assertEqual(repaired['user_setting'], 'preserve-me')
        self.assertEqual(repaired['hooks']['PostToolUse'][0]['hooks'][0]['command'],
                         'foreign-post')
        self.assertTrue(config.codex_hooks_status(self.codex))

        repeated = hook_refresh.refresh_installed('auto')
        self.assertEqual(self.rows(repeated)['codex']['status'], 'current')
        self.assertFalse(self.rows(repeated)['codex']['changed'])
        self.assertEqual(repeated['status'], 'completed')
        self.assertFalse(repeated['requires_user_action'])

    def test_auto_repairs_stale_absolute_claude_hooks_without_path_discovery(self):
        self.install_stale_claude()
        with mock.patch.object(claude_config, 'resolve_entrypoint',
                               side_effect=AssertionError('PATH discovery used')), \
             mock.patch.object(shutil, 'which',
                               side_effect=AssertionError('PATH discovery used')) as which, \
             mock.patch.dict(os.environ, {'PATH': ''}):
            report = hook_refresh.refresh_installed('auto')
        which.assert_not_called()
        row = self.rows(report)['claude']
        self.assertEqual(row['status'], 'refreshed')
        self.assertTrue(row['changed'])
        refreshed = json.loads(self.claude_path().read_text())
        self.assertEqual(refreshed['model'], 'preserve-me')
        self.assertEqual(refreshed['hooks']['PostToolUse'][0]['hooks'][0]['command'],
                         'foreign-post')
        self.assertEqual(refreshed['hooks']['PreToolUse'][0]['matcher'],
                         claude_config.HOOK_EVENTS['PreToolUse'])
        self.assertEqual(refreshed['hooks']['PreToolUse'][0]['hooks'][0]['command'],
                         claude_config.hook_command(self.launcher))

    def test_both_refreshes_stale_runtimes_then_reports_both_current(self):
        self.install_stale_codex()
        self.install_stale_claude()

        first = hook_refresh.refresh_installed('both')
        self.assertEqual(set(self.rows(first)), {'codex', 'claude'})
        self.assertTrue(all(row['changed'] for row in first['hooks']))
        self.assertEqual(first['status'], 'action_required')
        self.assertEqual([action['runtime'] for action in first['required_actions']
                          if action['code'] == 'review_native_hooks'], ['codex', 'claude'])
        self.assertTrue(config.codex_hooks_status(self.codex))
        self.assertTrue(claude_config.status(self.claude, executable=self.launcher))

        second = hook_refresh.refresh_installed('both')
        self.assertEqual(set(self.rows(second)), {'codex', 'claude'})
        self.assertTrue(all(row['status'] == 'current' and not row['changed']
                            for row in second['hooks']))
        self.assertEqual(second['status'], 'completed')
        self.assertEqual(second['required_actions'], [])
        self.assertFalse(second['requires_user_action'])

    def test_null_codex_hooks_are_not_owned_and_are_not_rewritten(self):
        self.codex.mkdir()
        before = b'{"foreign": "preserve-me", "hooks": null}\n'
        self.codex_path().write_bytes(before)

        report = hook_refresh.refresh_installed('auto')
        self.assertEqual(report['status'], 'completed')
        self.assertEqual(report['hooks'], [])
        self.assertEqual(report['required_actions'], [])
        self.assertEqual(self.codex_path().read_bytes(), before)

    def test_auto_foreign_only_configuration_is_preserved_and_not_reported_missing(self):
        self.codex.mkdir()
        self.claude.mkdir()
        codex_before = json.dumps({'hooks': {'Stop': [
            {'hooks': [{'type': 'command', 'command': 'foreign-stop'}]}]}}, indent=2) + '\n'
        claude_before = json.dumps({'model': 'mine', 'hooks': {'Stop': [
            {'hooks': [{'type': 'command', 'command': 'foreign-stop'}]}]}}) + '\n'
        self.codex_path().write_text(codex_before)
        self.claude_path().write_text(claude_before)

        report = hook_refresh.refresh_installed('auto')
        self.assertEqual(report['status'], 'completed')
        self.assertEqual(report['hooks'], [])
        self.assertEqual(report['required_actions'], [])
        self.assertEqual(self.codex_path().read_text(), codex_before)
        self.assertEqual(self.claude_path().read_text(), claude_before)

    def test_explicit_missing_integration_returns_setup_guidance_without_creation(self):
        for runtime in ('codex', 'claude'):
            with self.subTest(runtime=runtime):
                code, output, errors = self.call(
                    hook_refresh.main, ['--runtime', runtime, '--json'])
                self.assertEqual(code, 3, errors)
                self.assertEqual(errors, '')
                report = json.loads(output)
                self.assertEqual(report['status'], 'action_required')
                self.assertEqual(self.rows(report)[runtime]['status'], 'missing')
                self.assertIn('install_hooks', self.codes(report))
                self.assertIn('hooks install --runtime ' + runtime,
                              report['required_actions'][0]['command'])
                self.assertFalse(self.codex.exists())
                self.assertFalse(self.claude.exists())

    def test_disabled_claude_hooks_remain_disabled_and_require_user_action(self):
        self.assertTrue(claude_config.install(self.claude, executable=self.launcher))
        data = json.loads(self.claude_path().read_text())
        data['disableAllHooks'] = True
        data['model'] = 'preserve-me'
        self.claude_path().write_text(json.dumps(data, indent=2) + '\n')
        before = self.claude_path().read_bytes()

        code, output, errors = self.call(
            hook_refresh.main, ['--runtime', 'claude', '--json'])
        self.assertEqual(code, 3, errors)
        report = json.loads(output)
        self.assertEqual(self.rows(report)['claude']['status'], 'disabled')
        self.assertFalse(self.rows(report)['claude']['changed'])
        self.assertIn('enable_disabled_hooks', self.codes(report))
        self.assertIn('will not re-enable', report['required_actions'][0]['message'])
        self.assertEqual(self.claude_path().read_bytes(), before)
        preserved = json.loads(self.claude_path().read_text())
        self.assertTrue(preserved['disableAllHooks'])
        self.assertEqual(preserved['model'], 'preserve-me')


class NoticeAndProtocolTests(HookRefreshCase):
    def test_notify_actions_reach_redirected_text_and_json(self):
        self.assertTrue(config.install_codex_hooks(self.codex))
        code, output, errors = self.call(
            hook_refresh.main, ['--runtime', 'codex', '--notify'])
        self.assertEqual(code, 3, errors)
        self.assertIn('new Codex session', output)
        self.assertIn('/hooks', output)
        self.assertIn('User confirmation remains pending', output)

        result = subprocess.run(
            [sys.executable, '-m', 'codex_deepseek_team.hook_refresh',
             '--runtime', 'codex', '--notify', '--json'],
            text=True, capture_output=True, timeout=15, env=dict(os.environ))
        self.assertEqual(result.returncode, 3, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report['status'], 'action_required')
        self.assertIn('review_native_hooks', self.codes(report))
        self.assertNotIn('new Codex session', result.stdout.split('"message":', 1)[0])

    def test_lifecycle_refresh_actions_reach_hook_additional_context(self):
        self.install_stale_codex()
        result = coordinator_hooks.handle({
            'hook_event_name': 'SessionStart', 'source': 'startup',
            'session_id': 'session-1', 'cwd': str(self.repo),
        }, runtime='codex')
        context = result['hookSpecificOutput']['additionalContext']
        self.assertIn('new Codex session', context)
        self.assertIn('/hooks', context)
        self.assertIn('User confirmation remains pending', context)

    def test_only_lifecycle_main_thread_events_refresh_hooks(self):
        completed = {'schema_version': 1, 'version': 'test', 'status': 'completed',
                     'hooks': [], 'required_actions': [], 'requires_user_action': False}
        with mock.patch.object(hook_refresh, 'refresh_installed',
                               return_value=completed) as refresh:
            for event in ('PreToolUse', 'Stop', 'SubagentStop'):
                coordinator_hooks.handle({
                    'hook_event_name': event, 'session_id': 's',
                    'cwd': str(self.repo), 'tool_name': 'Read',
                }, runtime='codex')
            refresh.assert_not_called()
            for event in ('SessionStart', 'UserPromptSubmit'):
                coordinator_hooks.handle({
                    'hook_event_name': event, 'session_id': 's',
                    'cwd': str(self.repo), 'prompt': 'status',
                }, runtime='claude')
            self.assertEqual(refresh.call_count, 2)
            refresh.reset_mock()
            coordinator_hooks.handle({
                'hook_event_name': 'SessionStart', 'session_id': 's',
                'cwd': str(self.repo), 'agent_id': 'native-child',
            }, runtime='claude')
            refresh.assert_not_called()

    def test_hook_output_remains_one_valid_json_object_with_actions(self):
        self.install_stale_codex()
        result = subprocess.run(
            [sys.executable, '-m', 'codex_deepseek_team', 'coordinator-hook',
             '--runtime', 'codex'],
            input=json.dumps({'hook_event_name': 'UserPromptSubmit',
                              'session_id': 'session-1', 'cwd': str(self.repo),
                              'prompt': 'status'}),
            text=True, capture_output=True, timeout=15, env=dict(os.environ))
        self.assertEqual(result.returncode, 0, result.stderr)
        body = json.loads(result.stdout)
        self.assertIn('new Codex session',
                      body['hookSpecificOutput']['additionalContext'])
        self.assertEqual(result.stderr, '')

    def test_automatic_cli_refresh_uses_stderr_without_changing_exit_status(self):
        self.install_stale_codex()
        code, output, errors = self.call(cli.main, ['status', str(self.repo)])
        self.assertEqual(code, 0, errors)
        self.assertIn('DeepSeek Team:', output)
        self.assertIn('new Codex session', errors)
        self.assertNotIn('new Codex session', output)
        self.assertTrue(config.codex_hooks_status(self.codex))

    def test_automatic_cli_refresh_skips_help_version_management_worker_and_auth(self):
        report = {'schema_version': 1, 'version': 'test', 'status': 'completed',
                  'hooks': [], 'required_actions': [], 'requires_user_action': False}
        with mock.patch.object(hook_refresh, 'refresh_installed',
                               return_value=report) as refresh:
            for argv in ([], ['--help'], ['--version'], ['hooks', 'status'],
                         ['hooks', 'refresh'], ['worker'], ['auth', 'status'],
                         ['setup'], ['reset'], ['init'], ['detach'], ['update'],
                         ['doctor', '--help'], ['unknown-command'], ['help']):
                hook_refresh.refresh_for_cli(argv)
            refresh.assert_not_called()

    def test_hooks_refresh_cli_delegates_exit_contract_and_keeps_stdout_json(self):
        code, output, errors = self.call(
            cli.main, ['hooks', 'refresh', '--runtime', 'codex', '--json'])
        self.assertEqual(code, 3, errors)
        self.assertIsInstance(json.loads(output), dict)
        self.assertTrue(config.install_codex_hooks(self.codex))
        code, output, errors = self.call(
            cli.main, ['hooks', 'refresh', '--runtime', 'codex', '--json'])
        self.assertEqual(code, 0, errors)
        self.assertEqual(json.loads(output)['status'], 'completed')


class ControlledFailureTests(HookRefreshCase):
    def test_malformed_and_symlinked_configurations_fail_without_replacement(self):
        self.codex.mkdir()
        self.codex_path().write_bytes(b'{broken')
        before = self.codex_path().read_bytes()
        code, output, errors = self.call(
            hook_refresh.main, ['--runtime', 'codex', '--json'])
        self.assertEqual(code, 78, errors)
        report = json.loads(output)
        self.assertEqual(report['status'], 'failed')
        self.assertEqual(self.rows(report)['codex']['status'], 'failed')
        self.assertIn('repair_hooks', self.codes(report))
        self.assertEqual(self.codex_path().read_bytes(), before)

        self.codex_path().unlink()
        target = self.root / 'outside-hooks.json'
        target.write_bytes(b'{"hooks": {}}')
        self.codex_path().symlink_to(target)
        code, output, errors = self.call(
            hook_refresh.main, ['--runtime', 'codex', '--json'])
        self.assertEqual(code, 78, errors)
        self.assertEqual(json.loads(output)['status'], 'failed')
        self.assertTrue(self.codex_path().is_symlink())
        self.assertEqual(target.read_bytes(), b'{"hooks": {}}')

    def test_malformed_claude_settings_fail_closed(self):
        self.claude.mkdir()
        self.claude_path().write_bytes(b'{"hooks": []}')
        before = self.claude_path().read_bytes()
        code, output, errors = self.call(
            hook_refresh.main, ['--runtime', 'claude', '--json'])
        self.assertEqual(code, 78, errors)
        self.assertEqual(json.loads(output)['status'], 'failed')
        self.assertEqual(self.claude_path().read_bytes(), before)

    def test_atomic_replacement_failure_reports_failure_and_preserves_configuration(self):
        self.install_stale_codex()
        before = self.codex_path().read_bytes()
        with mock.patch.object(config, 'atomic_write',
                               side_effect=config.ConfigError(
                                   'File changed concurrently; no update was applied.')):
            code, output, errors = self.call(
                hook_refresh.main, ['--runtime', 'codex', '--json'])
        self.assertEqual(code, 78, errors)
        report = json.loads(output)
        self.assertEqual(report['status'], 'failed')
        self.assertIn('changed concurrently',
                      report['required_actions'][0]['message'])
        self.assertEqual(self.codex_path().read_bytes(), before)

    def test_failed_hook_refresh_does_not_break_lifecycle_protocol(self):
        self.codex.mkdir()
        self.codex_path().write_bytes(b'{broken')
        result = coordinator_hooks.handle({
            'hook_event_name': 'SessionStart', 'source': 'startup',
            'session_id': 'session-1', 'cwd': str(self.repo),
        }, runtime='codex')
        context = result['hookSpecificOutput']['additionalContext']
        self.assertIn('could not refresh', context)
        self.assertEqual(self.codex_path().read_bytes(), b'{broken')



class RefreshPreservationRegressionTests(unittest.TestCase):
    def test_gc_dry_run_does_not_refresh_configuration(self):
        with mock.patch.object(hook_refresh, 'refresh_installed',
                               return_value={'required_actions': []}) as refresh:
            hook_refresh.refresh_for_cli(['gc', '--dry-run'])
            hook_refresh.refresh_for_cli(['gc'])
        refresh.assert_not_called()

    def test_malformed_falsy_codex_hook_sections_are_preserved_and_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            path = home / 'hooks.json'
            for value in ([], False, 0, ''):
                with self.subTest(value=value):
                    raw = json.dumps({'hooks': value}).encode()
                    path.write_bytes(raw)
                    with self.assertRaises(config.ConfigError):
                        config.codex_hooks_owned(home)
                    self.assertEqual(path.read_bytes(), raw)


class NotifiedFailureRegressionTests(HookRefreshCase):
    def test_failed_notify_retains_native_review_and_notify_recovery(self):
        self.install_stale_codex()
        self.install_stale_claude()
        codex_before = self.codex_path().read_bytes()
        claude_before = self.claude_path().read_bytes()
        failure = config.ConfigError('Controlled configuration replacement failure.')
        with mock.patch.object(config, 'atomic_write', side_effect=failure), \
             mock.patch.object(claude_config, 'atomic_write', side_effect=failure):
            code, output, errors = self.call(
                hook_refresh.main, ['--runtime', 'both', '--notify', '--json'])
        self.assertEqual(code, 78, errors)
        report = json.loads(output)
        self.assertEqual(report['status'], 'failed')
        self.assertTrue(report['requires_user_action'])
        self.assertEqual(len(report['required_actions']), 2)
        for action in report['required_actions']:
            self.assertIn('--notify', action['command'])
            self.assertIn('/hooks', action['message'])
            self.assertIn('session', action['message'])
        self.assertEqual(self.codex_path().read_bytes(), codex_before)
        self.assertEqual(self.claude_path().read_bytes(), claude_before)

if __name__ == '__main__':
    unittest.main()
