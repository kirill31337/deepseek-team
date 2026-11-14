import contextlib
import fcntl
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

from codex_deepseek_team import cli, updates


class _Distribution:
    def __init__(self, version='0.11.0', *, direct_url=None, located_file=None,
                 installer='pip\n'):
        self.version = version
        self._direct_url = direct_url
        self._located_file = located_file
        self.installer = installer

    def locate_file(self, name):
        if self._located_file is not None and str(name) in (
                'codex_deepseek_team/__init__.py', 'codex_deepseek_team'):
            return Path(self._located_file)
        return Path('/not-the-running-package') / str(name)

    def read_text(self, name):
        if name == 'direct_url.json':
            return self._direct_url
        if name == 'INSTALLER':
            return self.installer
        return None


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        self.venv = self.root / 'venv'
        (self.venv / 'bin').mkdir(parents=True)
        self.interpreter = self.venv / 'bin/python'
        self.manager = self.bin / 'pipx'
        self.uv_manager = self.bin / 'uv'
        for manager in (self.manager, self.uv_manager):
            manager.write_text('#!/bin/sh\nexit 0\n')
            manager.chmod(0o755)
        self.package_file = Path(updates.__file__).resolve().with_name('__init__.py')
        self.state_dir = self.root / 'state'
        self.environment = {
            'PATH': str(self.bin) + os.pathsep + os.environ.get('PATH', ''),
            'HOME': str(self.root / 'home'),
            'CODEX_HOME': str(self.root / 'codex'),
            'CLAUDE_CONFIG_DIR': str(self.root / 'claude'),
            'XDG_CONFIG_HOME': str(self.root / 'xdg'),
            'DEEPSEEK_TEAM_STATE_DIR': str(self.state_dir),
        }

    def _detect(self, distribution=None, *, receipt=None, pipx=None):
        distribution = distribution or _Distribution(located_file=self.package_file)
        if receipt is not None:
            (self.venv / 'uv-receipt.toml').write_text(receipt)
        if pipx is not None:
            (self.venv / 'pipx_metadata.json').write_text(json.dumps(pipx))
        with mock.patch.object(updates.metadata, 'distribution', return_value=distribution), \
             mock.patch.object(updates.sys, 'prefix', str(self.venv)), \
             mock.patch.object(updates.sys, 'base_prefix', '/usr'), \
             mock.patch.object(updates.sys, 'executable', str(self.interpreter)):
            return updates.detect_installation('auto', environ=self.environment)

    def _installation(self, manager='pip'):
        if manager == 'pip':
            executable = self.interpreter
        elif manager == 'pipx':
            executable = self.manager
        else:
            executable = self.uv_manager
        return updates.Installation(
            manager=manager,
            executable=str(executable),
            interpreter=str(self.interpreter),
            previous_version='0.11.0',
        )

    def _invoke(self, arguments, *, detect=None, run=None):
        output, errors = io.StringIO(), io.StringIO()
        with mock.patch.object(updates, 'detect_installation',
                               return_value=detect or self._installation()), \
             mock.patch.object(updates.subprocess, 'run', side_effect=run) as runner, \
             mock.patch.dict(os.environ, self.environment), \
             contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = updates.main(arguments)
        return code, output.getvalue(), errors.getvalue(), runner

    def _invoke_detected(self, arguments, *, distribution=None, run=None):
        output, errors = io.StringIO(), io.StringIO()
        distribution = distribution or _Distribution(located_file=self.package_file)
        with mock.patch.object(updates.metadata, 'distribution',
                               return_value=distribution), \
             mock.patch.object(updates.sys, 'prefix', str(self.venv)), \
             mock.patch.object(updates.sys, 'base_prefix', '/usr'), \
             mock.patch.object(updates.sys, 'executable', str(self.interpreter)), \
             mock.patch.object(updates.subprocess, 'run', side_effect=run) as runner, \
             mock.patch.dict(os.environ, self.environment), \
             contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = updates.main(arguments)
        return code, output.getvalue(), errors.getvalue(), runner

    def _lock_path(self, installation=None):
        with mock.patch.dict(os.environ, self.environment):
            return updates._update_lock_path(installation or self._installation())

    def _assert_lock_released(self, path):
        descriptor = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)

    def test_cli_dispatches_update_before_loading_unrelated_commands(self):
        with mock.patch.object(updates, 'main', return_value=3) as updater:
            self.assertEqual(cli.main(['update', '--json']), 3)
        updater.assert_called_once_with(['--json'])

    def test_invalid_version_is_rejected_before_detection_or_mutation(self):
        with mock.patch.object(updates, 'detect_installation') as detect, \
             mock.patch.object(updates.subprocess, 'run') as runner, \
             contextlib.redirect_stdout(io.StringIO()) as output:
            code = updates.main(['--version', '--index-url', '--json'])
        self.assertEqual(code, 78)
        detect.assert_not_called()
        runner.assert_not_called()
        report = json.loads(output.getvalue())
        self.assertEqual(report['status'], 'failed')
        self.assertEqual(report['package_update']['status'], 'not_attempted')

    def test_invalid_manager_value_is_not_echoed_in_diagnostics(self):
        with mock.patch.object(updates, 'detect_installation') as detect, \
             mock.patch.object(updates.subprocess, 'run') as runner, \
             contextlib.redirect_stdout(io.StringIO()) as output:
            code = updates.main([
                '--manager', 'https://user:super-secret@example.invalid',
                '--json',
            ])
        self.assertEqual(code, 78)
        detect.assert_not_called()
        runner.assert_not_called()
        self.assertNotIn('super-secret', output.getvalue())
        report = json.loads(output.getvalue())
        self.assertIsNone(report['manager'])

    def test_version_validation_rejects_option_and_command_syntax(self):
        for value in ('--index-url', '-1', '1.0; id', '1.0 && id', '1.0/../x',
                      '1.0 @ https://example.invalid'):
            with self.subTest(value=value):
                with self.assertRaises(updates.UpdateError):
                    updates.validate_release(value)
        self.assertEqual(updates.validate_release('0.12.0'), '0.12.0')

    def test_global_pip_and_source_installs_are_rejected(self):
        with mock.patch.object(updates.sys, 'prefix', '/usr'), \
             mock.patch.object(updates.sys, 'base_prefix', '/usr'):
            with self.assertRaisesRegex(updates.UpdateError, 'virtual environment'):
                updates.detect_installation('auto', environ=self.environment)

        distribution = _Distribution(located_file='/somewhere/else/__init__.py')
        with mock.patch.object(updates.metadata, 'distribution', return_value=distribution), \
             mock.patch.object(updates.sys, 'prefix', str(self.venv)), \
             mock.patch.object(updates.sys, 'base_prefix', '/usr'), \
             mock.patch.object(updates.sys, 'executable', str(self.interpreter)):
            with self.assertRaisesRegex(updates.UpdateError, 'does not belong'):
                updates.detect_installation('auto', environ=self.environment)

    def test_editable_and_local_source_installs_are_rejected(self):
        for direct_url in (
                json.dumps({'url': 'file:///src/deepseek-team',
                            'dir_info': {'editable': True}}),
                json.dumps({'url': 'file:///src/deepseek-team',
                            'dir_info': {}}),
                json.dumps({'url': 'https://example.invalid/repo.git',
                            'vcs_info': {'vcs': 'git'}})):
            with self.subTest(direct_url=direct_url):
                with self.assertRaisesRegex(updates.UpdateError, 'editable|source'):
                    self._detect(_Distribution(direct_url=direct_url))

    def test_pipx_and_uv_provenance_require_matching_receipts(self):
        pipx = {
            'main_package': {
                'package': 'deepseek-team',
                'package_or_url': 'deepseek-team',
            },
            'pipx_metadata_version': '0.1',
        }
        installation = self._detect(pipx=pipx)
        self.assertEqual(installation.manager, 'pipx')
        self.assertEqual(installation.executable, str(self.manager))

        with self.assertRaisesRegex(updates.UpdateError, '[Cc]onflicting manager'):
            with mock.patch.object(updates.metadata, 'distribution',
                                   return_value=_Distribution(located_file=self.package_file)), \
                 mock.patch.object(updates.sys, 'prefix', str(self.venv)), \
                 mock.patch.object(updates.sys, 'base_prefix', '/usr'), \
                 mock.patch.object(updates.sys, 'executable', str(self.interpreter)):
                updates.detect_installation('pip', environ=self.environment)

        (self.venv / 'pipx_metadata.json').unlink()
        uv = self._detect(receipt='[tool]\nrequirements = ["deepseek-team"]\n')
        self.assertEqual(uv.manager, 'uv')
        self.assertEqual(uv.executable, str(self.uv_manager))

    def test_source_locations_in_manager_receipts_are_rejected(self):
        pipx = {'main_package': {'package': 'deepseek-team',
                                 'package_or_url': '/tmp/deepseek-team'}}
        with self.assertRaisesRegex(updates.UpdateError, 'editable|local-source'):
            self._detect(pipx=pipx)

        (self.venv / 'pipx_metadata.json').unlink()
        receipt = ('[tool]\nrequirements = [{ name = "deepseek-team", editable = true, '
                   'path = "/tmp/deepseek-team" }]\n')
        with self.assertRaisesRegex(updates.UpdateError, 'editable|local-source'):
            self._detect(receipt=receipt)

    def test_plain_pip_main_rejects_missing_or_unsupported_installer_provenance(self):
        for installer in (None, '', 'unknown\n', 'pipx\n', 'uv\n'):
            with self.subTest(installer=installer):
                distribution = _Distribution(located_file=self.package_file,
                                             installer=installer)
                code, output, errors, runner = self._invoke_detected(
                    ['--json'], distribution=distribution)
                self.assertEqual(code, 78, errors)
                runner.assert_not_called()
                report = json.loads(output)
                self.assertEqual(report['package_update']['status'], 'not_attempted')
                self.assertFalse(report['package_update']['changed'])
                self.assertIn('INSTALLER', report['error'])

        supported = _Distribution(located_file=self.package_file, installer='pip\n')
        with mock.patch.object(updates.metadata, 'distribution', return_value=supported), \
             mock.patch.object(updates.sys, 'prefix', str(self.venv)), \
             mock.patch.object(updates.sys, 'base_prefix', '/usr'), \
             mock.patch.object(updates.sys, 'executable', str(self.interpreter)):
            self.assertEqual(
                updates.detect_installation('auto', environ=self.environment).manager, 'pip')

    def test_pipx_and_uv_receipts_remain_valid_without_a_pip_installer_receipt(self):
        distribution = _Distribution(located_file=self.package_file, installer=None)
        pipx = {'main_package': {'package': 'deepseek-team',
                                 'package_or_url': 'deepseek-team'}}
        self.assertEqual(self._detect(distribution, pipx=pipx).manager, 'pipx')

        (self.venv / 'pipx_metadata.json').unlink()
        uv = self._detect(distribution, receipt='[tool]\nrequirements = ["deepseek-team"]\n')
        self.assertEqual(uv.manager, 'uv')

    def test_manager_commands_keep_the_installation_channel(self):
        cases = {
            'pip': ([str(self.interpreter), '-m', 'pip', 'install', '--upgrade',
                     'deepseek-team'], [str(self.interpreter), '-m', 'pip', 'install',
                                        '--upgrade', 'deepseek-team==0.12.0']),
            'pipx': ([str(self.manager), 'upgrade', 'deepseek-team'],
                     [str(self.manager), 'install', '--force', 'deepseek-team==0.12.0']),
            'uv': ([str(self.uv_manager), 'tool', 'upgrade', 'deepseek-team'],
                   [str(self.uv_manager), 'tool', 'install', '--force',
                    'deepseek-team==0.12.0']),
        }
        for manager, (ordinary, explicit) in cases.items():
            with self.subTest(manager=manager):
                installation = self._installation(manager)
                self.assertEqual(updates.manager_command(installation), ordinary)
                self.assertEqual(updates.manager_command(installation, '0.12.0'), explicit)

    def test_hook_refresh_uses_fresh_lexical_interpreter_and_clean_environment(self):
        completed = [
            subprocess.CompletedProcess([], 0, 'manager output\n', 'manager note\n'),
            subprocess.CompletedProcess([], 0, json.dumps({
                'schema_version': 1,
                'version': '0.12.0',
                'status': 'completed',
                'hooks': [{'runtime': 'codex', 'status': 'completed', 'changed': True}],
                'required_actions': [],
                'requires_user_action': False,
            }), ''),
        ]
        with mock.patch.dict(os.environ, dict(self.environment, PYTHONPATH='/old/src',
                                              PYTHONHOME='/old/home'), clear=True), \
             mock.patch.object(updates.sys, 'executable', str(self.interpreter)):
            code, output, errors, runner = self._invoke(
                ['--manager', 'pipx', '--runtime', 'both', '--json'],
                detect=self._installation('pipx'), run=completed)

        self.assertEqual(code, 0, errors)
        report = json.loads(output)
        self.assertEqual(report['manager'], 'pipx')
        self.assertEqual(report['previous_version'], '0.11.0')
        self.assertEqual(report['current_version'], '0.12.0')
        self.assertEqual(report['package_update']['status'], 'updated')
        self.assertEqual(report['hooks'], [
            {'runtime': 'codex', 'status': 'completed', 'changed': True}])
        self.assertEqual(runner.call_count, 2)
        manager_call, refresh_call = runner.call_args_list
        self.assertEqual(manager_call.args[0], [
            str(self.manager), 'upgrade', 'deepseek-team'])
        self.assertEqual(refresh_call.args[0], [
            str(self.interpreter), '-m', 'codex_deepseek_team.hook_refresh',
            '--runtime', 'both', '--notify', '--json'])
        self.assertNotIn('PYTHONPATH', refresh_call.kwargs['env'])
        self.assertNotIn('PYTHONHOME', refresh_call.kwargs['env'])
        self.assertFalse(manager_call.kwargs['shell'])
        self.assertFalse(refresh_call.kwargs['shell'])
        self.assertIn('manager note', errors)
        self.assertNotIn('manager note', output)

    def test_action_required_preserves_every_action_and_returns_three(self):
        actions = [
            {'code': 'review_native_trust', 'runtime': 'codex',
             'message': 'Start a new Codex session and review DeepSeek Team in /hooks.',
             'command': '/usr/bin/codex'},
            {'code': 'review_native_trust', 'runtime': 'claude',
             'message': 'Start a new Claude session and review DeepSeek Team in /hooks.',
             'command': '/usr/bin/claude'},
        ]
        completed = [
            subprocess.CompletedProcess([], 0, '', ''),
            subprocess.CompletedProcess([], 3, json.dumps({
                'schema_version': 1,
                'version': '0.12.0',
                'status': 'action_required',
                'hooks': [],
                'required_actions': actions,
                'requires_user_action': True,
            }), ''),
        ]
        code, output, errors, _runner = self._invoke(['--json'], run=completed)
        self.assertEqual(code, 3, errors)
        report = json.loads(output)
        self.assertEqual(report['status'], 'action_required')
        self.assertEqual(report['required_actions'], actions)
        self.assertTrue(report['requires_user_action'])

    def test_text_mode_exposes_actions_and_pending_confirmation(self):
        action = {'code': 'restart_session', 'runtime': 'codex',
                  'message': 'Start a new session and review DeepSeek Team in /hooks.',
                  'command': 'codex'}
        completed = [
            subprocess.CompletedProcess([], 0, '', 'manager note\n'),
            subprocess.CompletedProcess([], 3, json.dumps({
                'schema_version': 1, 'version': '0.12.0', 'status': 'action_required',
                'hooks': [], 'required_actions': [action], 'requires_user_action': True,
            }), ''),
        ]
        code, output, errors, _runner = self._invoke([], run=completed)
        self.assertEqual(code, 3)
        self.assertIn('Start a new session', output)
        self.assertIn('Run: codex', output)
        self.assertIn('AI agents must relay every required action', output)
        self.assertIn('manager note', errors)
        self.assertNotIn('manager note', output)

    def test_unchanged_version_is_reported_without_user_action(self):
        completed = [
            subprocess.CompletedProcess([], 0, '', ''),
            subprocess.CompletedProcess([], 0, json.dumps({
                'schema_version': 1, 'version': '0.11.0', 'status': 'completed',
                'hooks': [], 'required_actions': [], 'requires_user_action': False,
            }), ''),
        ]
        code, output, errors, _runner = self._invoke(['--json'], run=completed)
        self.assertEqual(code, 0, errors)
        report = json.loads(output)
        self.assertEqual(report['package_update']['status'], 'unchanged')
        self.assertFalse(report['package_update']['changed'])
        self.assertEqual(report['status'], 'completed')

    def test_manager_failure_is_sanitized_and_skips_hook_refresh(self):
        completed = subprocess.CompletedProcess(
            [], 1, 'using https://user:password@example.invalid/simple\n'
            'Authorization: Bearer super-secret-token\n', '')
        code, output, errors, runner = self._invoke(['--json'], run=[completed])
        self.assertEqual(code, 78)
        self.assertEqual(runner.call_count, 1)
        report = json.loads(output)
        self.assertEqual(report['status'], 'failed')
        self.assertEqual(report['package_update']['status'], 'failed')
        self.assertIsNone(report['package_update']['changed'])
        self.assertEqual(report['package_update']['manager_exit_code'], 1)
        self.assertEqual(report['required_actions'], [])
        self.assertIn('partially', report['error'])
        self.assertTrue(report['recovery_commands'])
        self.assertNotIn('password', output + errors)
        self.assertNotIn('super-secret-token', output + errors)
        self.assertIn('[redacted]', errors)

    def test_manager_launch_failure_is_reported_as_a_verified_no_change(self):
        def run(_command, **_kwargs):
            raise OSError('synthetic manager launch failure')

        code, output, errors, runner = self._invoke(['--json'], run=run)
        self.assertEqual(code, 78, errors)
        self.assertEqual(runner.call_count, 1)
        report = json.loads(output)
        self.assertEqual(report['status'], 'failed')
        self.assertEqual(report['package_update']['status'], 'failed')
        self.assertFalse(report['package_update']['changed'])
        self.assertIsNone(report['package_update']['manager_exit_code'])
        self.assertEqual(report['current_version'], '0.11.0')
        self.assertIn('could not be started', report['error'])
        self.assertIn('was not changed', report['error'])
        self.assertNotIn('synthetic manager launch failure', output + errors)

    def test_overlapping_same_installation_update_returns_busy_before_manager(self):
        first_started = threading.Event()
        release_first = threading.Event()
        calls = []
        outcomes = {}
        reports = {}
        call_guard = threading.Lock()
        report_guard = threading.Lock()

        def run(command, **_kwargs):
            with call_guard:
                calls.append(list(command))
            first_started.set()
            if not release_first.wait(5):
                raise AssertionError('first manager did not receive release')
            return subprocess.CompletedProcess(command, 1, '', 'manager stopped\n')

        def emit(report, json_mode):
            with report_guard:
                reports[threading.current_thread().name] = (report, json_mode)

        def invoke(name):
            outcomes[name] = updates.main(['--json'])

        first = threading.Thread(target=invoke, args=('first',), name='first')
        second = threading.Thread(target=invoke, args=('second',), name='second')
        with mock.patch.object(updates, 'detect_installation',
                               return_value=self._installation()), \
             mock.patch.object(updates.subprocess, 'run', side_effect=run), \
             mock.patch.object(updates, '_emit', side_effect=emit), \
             mock.patch.dict(os.environ, self.environment):
            try:
                first.start()
                self.assertTrue(first_started.wait(5))
                second.start()
                second.join(2)
                second_was_blocked = not second.is_alive()
            finally:
                release_first.set()
                first.join(5)
                second.join(5)

        self.assertTrue(second_was_blocked, 'second update waited for the manager')
        self.assertEqual(outcomes, {'first': 78, 'second': 78})
        self.assertEqual(len(calls), 1)
        self.assertIn('already running', reports['second'][0]['error'])
        self.assertEqual(reports['second'][0]['package_update']['status'], 'not_attempted')
        self.assertIsNone(reports['first'][0]['package_update']['changed'])

    def test_unsafe_lock_entries_are_preserved_and_never_launch_manager(self):
        lock_path = self._lock_path()
        lock_path.parent.mkdir(mode=0o700, parents=True)
        target = self.root / 'lock-target'
        target.write_text('preserve me')
        lock_path.symlink_to(target)

        code, output, errors, runner = self._invoke(['--json'], run=[])
        self.assertEqual(code, 78, errors)
        runner.assert_not_called()
        self.assertTrue(lock_path.is_symlink())
        self.assertEqual(target.read_text(), 'preserve me')
        self.assertIn('lock', json.loads(output)['error'].lower())

        lock_path.unlink()
        lock_path.write_text('existing lock')
        lock_path.chmod(0o644)
        code, output, _errors, runner = self._invoke(['--json'], run=[])
        self.assertEqual(code, 78)
        runner.assert_not_called()
        self.assertEqual(lock_path.read_text(), 'existing lock')
        self.assertEqual(lock_path.stat().st_mode & 0o777, 0o644)
        self.assertIn('unsafe', json.loads(output)['error'].lower())

    def test_update_lock_is_outside_venv_and_released_after_report(self):
        lock_path = self._lock_path()
        self.assertFalse(lock_path.is_relative_to(self.venv))

        completed = [
            subprocess.CompletedProcess([], 0, '', ''),
            subprocess.CompletedProcess([], 0, json.dumps({
                'schema_version': 1, 'version': '0.11.0', 'status': 'completed',
                'hooks': [], 'required_actions': [], 'requires_user_action': False,
            }), ''),
        ]
        code, _output, errors, _runner = self._invoke(['--json'], run=completed)
        self.assertEqual(code, 0, errors)
        self._assert_lock_released(lock_path)

        def launch_failure(_command, **_kwargs):
            raise OSError('synthetic manager launch failure')

        code, _output, errors, _runner = self._invoke(['--json'], run=launch_failure)
        self.assertEqual(code, 78, errors)
        self._assert_lock_released(lock_path)

    def test_post_upgrade_refresh_failure_reports_package_change_and_recovery(self):
        completed = [
            subprocess.CompletedProcess([], 0, '', ''),
            subprocess.CompletedProcess([], 78, 'not json', 'refresh failed'),
        ]
        code, output, errors, _runner = self._invoke(
            ['--runtime', 'both', '--json'], run=completed)
        self.assertEqual(code, 78, errors)
        report = json.loads(output)
        self.assertEqual(report['status'], 'failed')
        self.assertEqual(report['package_update']['status'], 'succeeded')
        self.assertTrue(report['package_update']['changed'])
        self.assertIn('package', report['error'].lower())
        self.assertIn('incomplete', report['error'].lower())
        self.assertEqual(len(report['required_actions']), 1)
        action = report['required_actions'][0]
        self.assertEqual(action['runtime'], 'both')
        self.assertEqual(action['command'],
                         'deepseek-team hooks refresh --runtime both --notify --json')
        self.assertIn(action['command'], report['recovery_commands'])

    def test_hook_launch_error_is_controlled(self):
        def run(command, **_kwargs):
            if command[1:3] == ['-m', 'pip']:
                return subprocess.CompletedProcess(command, 0, '', '')
            raise OSError('synthetic launch failure')

        code, output, errors, _runner = self._invoke(['--json'], run=run)
        self.assertEqual(code, 78, errors)
        report = json.loads(output)
        self.assertEqual(report['package_update']['status'], 'succeeded')
        self.assertIn('hooks refresh', report['error'])
        self.assertIn('hooks refresh', report['recovery_commands'][0])
        self.assertNotIn('synthetic launch failure', report['error'])


if __name__ == '__main__':
    unittest.main()
