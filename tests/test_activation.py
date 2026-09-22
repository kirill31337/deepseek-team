"""Persistent project activation through CLI, hooks and worker entry points."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from codex_deepseek_team import activation, codex_hooks, config, coordination, doctor, project, settings, worker, workspace


class ActivationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        subprocess.run(['git', 'init', '-q', str(self.repo)], check=True)
        (self.repo / 'a.py').write_text('VALUE = 1\n')
        subprocess.run(['git', '-C', str(self.repo), 'add', '.'], check=True)
        subprocess.run(['git', '-C', str(self.repo), '-c', 'user.name=Test', '-c',
                        'user.email=test@example.invalid', 'commit', '-qm', 'base'], check=True)
        self.env = mock.patch.dict(os.environ, {
            'HOME': str(self.root / 'home'), 'XDG_CONFIG_HOME': str(self.root / 'config'),
            'DEEPSEEK_TEAM_STATE_DIR': str(self.root / 'state'),
            'DEEPSEEK_TEAM_DISABLED': '', 'CODEX_DEEPSEEK_DISABLED': '',
            'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'src'),
        })
        self.env.start()
        self.addCleanup(self.env.stop)

    def call(self, *args, cwd=None):
        return subprocess.run([sys.executable, '-m', 'codex_deepseek_team', *args],
                              cwd=cwd or self.repo, text=True, capture_output=True, timeout=15)

    def switch(self, name):
        result = self.call(name)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def status(self, cwd=None):
        result = self.call('status', '--json', cwd=cwd)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_default_persistence_subdirectory_and_project_isolation(self):
        self.assertTrue(self.status()['enabled'])
        self.switch('off')
        nested = self.repo / 'nested'
        nested.mkdir()
        self.assertFalse(self.status(nested)['enabled'])
        other = self.root / 'other'
        other.mkdir()
        subprocess.run(['git', 'init', '-q', str(other)], check=True)
        self.assertTrue(self.status(other)['enabled'])
        self.switch('on')
        self.assertTrue(self.status()['enabled'])

    def test_switch_preserves_project_instructions_settings_and_key(self):
        project.attach(self.repo, 'both')
        settings.set_values(self.repo / settings.PROJECT_FILE, delegation_level=75, effort='high')
        config.save_key('synthetic-test-credential')
        key = self.root / 'home/.config/codex-deepseek/api-key'
        key_before = key.read_bytes()
        before = {p.name: p.read_bytes() for p in self.repo.iterdir() if p.is_file()}
        self.switch('off')
        self.switch('off')
        policy = settings.resolve(self.repo)
        self.assertFalse(policy.enabled)
        self.assertEqual((policy.delegation_level, policy.effort), (75, 'high'))
        self.switch('on')
        self.assertEqual(key.read_bytes(), key_before)
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.repo.iterdir() if p.is_file()})

    def test_environment_disable_has_priority_over_saved_on(self):
        for name in ('DEEPSEEK_TEAM_DISABLED', 'CODEX_DEEPSEEK_DISABLED'):
            with self.subTest(name=name), mock.patch.dict(os.environ, {name: '1'}):
                result = self.switch('on')
                status = self.status()
                self.assertFalse(status['enabled'])
                self.assertTrue(status['saved_enabled'])
                self.assertIn(name, status['source'])
                self.assertIn(name, result.stdout)
        self.assertTrue(self.status()['enabled'])

    def test_config_instructions_disable_both_coordinators(self):
        self.switch('off')
        for runtime in ('codex', 'claude'):
            result = self.call('config', 'show', '--effective', '--instructions', '--runtime', runtime)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('disabled', result.stdout.lower())
            self.assertNotIn('Delegate bounded', result.stdout)
            self.assertNotIn('submit a concrete JSON distribution', result.stdout)
        data = json.loads(self.call('config', 'show', '--json').stdout)
        self.assertFalse(data['enabled'])

    def test_disabled_worker_stops_before_runtime_sandbox_or_credentials(self):
        self.switch('off')
        for runtime in ('codex', 'claude'):
            for access in ('read-only', 'full-access'):
                args = SimpleNamespace(runtime=runtime, access=access, state_dir=self.root / 'workers')
                with self.subTest(runtime=runtime, access=access), \
                     mock.patch.object(Path, 'cwd', return_value=self.repo), \
                     mock.patch.object(worker, 'resolve_runtime', side_effect=AssertionError('runtime touched')), \
                     mock.patch.object(worker, 'load_api_key', side_effect=AssertionError('key read')):
                    with self.assertRaises(worker.WorkerError) as error:
                        worker.run(args)
                    self.assertEqual(error.exception.code, 69)
        # Re-enabling reaches the normal worker path again.
        self.switch('on')
        with mock.patch.object(Path, 'cwd', return_value=self.repo), \
             mock.patch.object(worker, 'run_worker', return_value=0) as run, \
             contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(worker.run(SimpleNamespace(access='read-only')), 0)
            run.assert_called_once()

    def test_workspace_reuse_checks_source_project_not_launch_directory(self):
        copy = workspace.create(self.repo, self.root / 'workers')
        self.switch('off')
        args = SimpleNamespace(workspace=copy.id, state_dir=self.root / 'workers')
        with mock.patch.object(Path, 'cwd', return_value=self.root):
            with self.assertRaises(worker.WorkerError) as error:
                worker.run(args)
        self.assertEqual(error.exception.code, 69)

    def test_disabled_hooks_bypass_pending_gate_without_modifying_ledger(self):
        project.attach(self.repo, 'codex')
        payload = {'cwd': str(self.repo), 'session_id': 's', 'turn_id': 't', 'prompt': 'task'}
        codex_hooks.handle(dict(payload, hook_event_name='UserPromptSubmit'))
        task = coordination.latest_task(self.repo, 's')
        self.switch('off')
        for event in ('SessionStart', 'UserPromptSubmit', 'PreToolUse', 'Stop'):
            result = codex_hooks.handle(dict(payload, hook_event_name=event,
                                            tool_name='Write', tool_input={'file_path': 'a.py'}))
            self.assertNotIn('decision', result)
            self.assertNotIn('permissionDecision', result.get('hookSpecificOutput', {}))
            self.assertIsNot(result.get('continue'), False)
            if event in ('SessionStart', 'UserPromptSubmit'):
                self.assertIn('disabled', result['hookSpecificOutput']['additionalContext'].lower())
        self.assertEqual(task, coordination.latest_task(self.repo, 's'))
        self.switch('on')
        result = codex_hooks.handle(dict(payload, hook_event_name='PreToolUse',
                                        tool_name='Write', tool_input={'file_path': 'a.py'}))
        self.assertEqual(result['hookSpecificOutput']['permissionDecision'], 'deny')

    def test_environment_disable_also_bypasses_hook_gates(self):
        project.attach(self.repo, 'codex')
        with mock.patch.dict(os.environ, {'DEEPSEEK_TEAM_DISABLED': '1'}):
            result = codex_hooks.handle({'cwd': str(self.repo), 'session_id': 's',
                                        'hook_event_name': 'UserPromptSubmit'})
        self.assertIn('disabled', result['hookSpecificOutput']['additionalContext'].lower())
        self.assertIsNone(coordination.latest_task(self.repo, 's'))

    def test_disabled_live_diagnostics_do_not_read_key(self):
        self.switch('off')
        with mock.patch.object(Path, 'cwd', return_value=self.repo), \
             mock.patch.object(doctor.worker, 'load_api_key', side_effect=AssertionError('key read')), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(doctor.api_probe(), 69)
            self.assertEqual(doctor.live_tests(), 69)

    def test_outside_repository_rejected_without_writes(self):
        for command in ('on', 'off', 'status'):
            result = self.call(command, cwd=self.root)
            self.assertEqual(result.returncode, 78, result.stderr)
        self.assertFalse((self.root / 'config').exists())

    def test_status_is_read_only_and_paths_are_canonical(self):
        self.assertTrue(self.status()['enabled'])
        self.assertFalse((self.root / 'config').exists())
        alias = self.root / 'alias'
        alias.symlink_to(self.repo, target_is_directory=True)
        result = self.call('off', str(alias), cwd=self.root)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.status()['enabled'])
        result = self.call('status', str(alias), '--json', cwd=self.root)
        self.assertEqual(json.loads(result.stdout)['project'], str(self.repo))

    def test_malformed_or_unsafe_state_never_enables_workers(self):
        self.switch('off')
        path = activation.state_file(self.repo)
        for content in ('garbage', '{"enabled": "false"}', '{"enabled": 0}', '{}', '[]'):
            with self.subTest(content=content):
                path.write_text(content)
                result = self.call('worker', '--runtime', 'claude', 'task')
                self.assertEqual(result.returncode, 78, result.stderr)
                self.assertIn('Invalid activation', result.stderr)
                result = self.call('on')
                self.assertEqual(result.returncode, 78, result.stderr)
                self.assertEqual(path.read_text(), content)
        path.unlink()
        target = self.root / 'unrelated.json'
        target.write_text('{"enabled": false}')
        path.symlink_to(target)
        result = self.call('on')
        self.assertEqual(result.returncode, 78, result.stderr)
        self.assertEqual(target.read_text(), '{"enabled": false}')

    def test_activation_directory_symlink_is_rejected(self):
        package = self.root / 'config/deepseek-team'
        package.mkdir(parents=True)
        target = self.root / 'unrelated'
        target.mkdir()
        (package / 'activation').symlink_to(target, target_is_directory=True)
        result = self.call('off')
        self.assertEqual(result.returncode, 78, result.stderr)
        self.assertEqual(list(target.iterdir()), [])

    def test_hook_can_run_off_command_while_distribution_is_blocked(self):
        project.attach(self.repo, 'codex')
        payload = {'cwd': str(self.repo), 'session_id': 's', 'turn_id': 't', 'prompt': 'task'}
        codex_hooks.handle(dict(payload, hook_event_name='UserPromptSubmit'))
        for command in ('deepseek-team off', 'deepseek-team off /tmp/project'):
            result = codex_hooks.handle(dict(payload, hook_event_name='PreToolUse',
                                            tool_name='Bash', tool_input={'command': command}))
            self.assertEqual(result, {})

    def test_missing_workspace_source_cannot_fall_back_to_launch_project(self):
        copy = workspace.create(self.repo, self.root / 'workers')
        self.switch('off')
        self.repo.rename(self.root / 'moved')
        args = SimpleNamespace(workspace=copy.id, state_dir=self.root / 'workers')
        from codex_deepseek_team import managed
        with mock.patch.object(Path, 'cwd', return_value=self.root), \
             mock.patch.object(managed, 'run', return_value=0) as run:
            with self.assertRaises(worker.WorkerError) as error:
                worker.run(args)
            self.assertEqual(error.exception.code, 78)
            run.assert_not_called()

    def test_corrupt_state_has_clean_direct_diagnostic_error(self):
        self.switch('off')
        activation.state_file(self.repo).write_text('{broken')
        result = subprocess.run([sys.executable, str(Path(doctor.__file__)), '--api-probe'],
                                cwd=self.repo, text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 78, result.stderr)
        self.assertNotIn('Traceback', result.stderr)
        self.assertIn('Invalid activation', result.stderr)
