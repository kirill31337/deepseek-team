"""Storage readiness at public entry points, without a model or credential."""
import contextlib
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from codex_deepseek_team import (activation, cli, config, coordination,
                                 coordinator_hooks, doctor, project, sandbox,
                                 settings, state_storage, worker, worker_slots, workspace)


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='dst-storage-test-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.state = self.root / 'state'
        self.home = self.root / 'home'
        self.home.mkdir()
        self.codex = self.root / 'codex'
        self.environment = mock.patch.dict(os.environ, {
            'HOME': str(self.home), 'CODEX_HOME': str(self.codex),
            'CLAUDE_CONFIG_DIR': str(self.root / 'claude'),
            'XDG_CONFIG_HOME': str(self.root / 'config'),
            'DEEPSEEK_TEAM_STATE_DIR': str(self.state),
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)
        for name in ('DEEPSEEK_TEAM_DISABLED', 'CODEX_DEEPSEEK_DISABLED'):
            os.environ.pop(name, None)
        self.previous = Path.cwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, self.previous)

    def repository(self):
        source = self.root / 'source'
        source.mkdir()
        subprocess.run(['git', 'init', '-q', str(source)], check=True)
        subprocess.run(['git', '-C', str(source), '-c', 'user.name=Storage',
                        '-c', 'user.email=storage@example.invalid', 'commit',
                        '--allow-empty', '-qm', 'base'], check=True)
        return source

    def link_storage(self):
        self.state.mkdir(mode=0o700)
        target = self.root / 'retained-workspaces'
        target.mkdir(mode=0o700)
        (target / 'retained-data').write_text('preserve existing copies')
        (self.state / 'workspaces').symlink_to(target, target_is_directory=True)
        return target

    def doctor(self, *extra, live=False):
        config.configure(self.codex)
        out, err = io.StringIO(), io.StringIO()
        backend = sandbox.SandboxBackend(('/usr/bin/bwrap',), '/usr/bin/bwrap', 'direct')
        with mock.patch.object(sandbox, 'probe_backend', return_value=backend), \
             mock.patch('subprocess.check_output', side_effect=[
                 'codex-cli fixture',
                 '--strict-config --ephemeral --json --sandbox --ignore-rules']), \
             mock.patch.object(worker, 'load_api_key', side_effect=AssertionError('credential read')), \
             mock.patch('urllib.request.build_opener', side_effect=AssertionError('network request')), \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = doctor.main(['--live' if live else '--offline', '--runtime', 'codex',
                                '--access', 'read-only', *extra])
        return code, out.getvalue() + err.getvalue()

    def test_read_only_doctor_rejects_symlink_to_owned_700_workspaces(self):
        target = self.link_storage()
        code, output = self.doctor()
        self.assertEqual(code, 78, output)
        self.assertIn('symbolic link', output)
        self.assertIn(str(self.state / 'workspaces'), output)
        self.assertIn('--state-dir', output)
        self.assertTrue((self.state / 'workspaces').is_symlink())
        self.assertEqual(target.stat().st_mode & 0o777, 0o700)
        self.assertEqual((target / 'retained-data').read_text(), 'preserve existing copies')
        self.assertEqual(sorted(p.name for p in target.iterdir()), ['retained-data'])

    def test_doctor_rejects_public_workspaces_without_chmod(self):
        self.state.mkdir(mode=0o700)
        copies = self.state / 'workspaces'
        copies.mkdir(mode=0o755)
        copies.chmod(0o755)
        code, output = self.doctor()
        self.assertEqual(code, 78, output)
        self.assertIn('permissions', output)
        self.assertIn('755', output)
        self.assertEqual(copies.stat().st_mode & 0o777, 0o755)

    def test_doctor_override_checks_selected_root_and_preserves_old_link(self):
        target = self.link_storage()
        selected = self.root / 'selected-state'
        code, output = self.doctor('--state-dir', str(selected))
        self.assertEqual(code, 0, output)
        self.assertIn(str(selected), output)
        self.assertEqual(selected.stat().st_mode & 0o777, 0o700)
        self.assertEqual((selected / 'workspaces').stat().st_mode & 0o777, 0o700)
        self.assertEqual(list((selected / 'workspaces').iterdir()), [])
        self.assertTrue((self.state / 'workspaces').is_symlink())
        self.assertTrue((target / 'retained-data').is_file())

    def test_worker_default_uses_live_environment_state_root(self):
        for selected in (self.state, self.root / 'second-state'):
            with self.subTest(selected=selected), \
                 mock.patch.dict(os.environ, {'DEEPSEEK_TEAM_STATE_DIR': str(selected)}), \
                 mock.patch.object(sys, 'argv', ['worker', 'inspect source']):
                self.assertEqual(worker.parse_args().state_dir, selected)

    def test_worker_command_override_has_priority_over_environment(self):
        selected = self.root / 'explicit-state'
        with mock.patch.object(sys, 'argv', ['worker', '--state-dir', str(selected), 'inspect']):
            self.assertEqual(worker.parse_args().state_dir, selected)

    def test_live_doctor_passes_checked_storage_to_worker_smoke(self):
        selected = self.root / 'explicit-state'
        def smoke(*args, **kwargs):
            self.assertEqual(kwargs.get('state_dir'), selected)
            return 0
        with mock.patch.object(doctor, 'live_tests', side_effect=smoke):
            code, output = self.doctor('--state-dir', str(selected), live=True)
        self.assertEqual(code, 0, output)

    def test_synthetic_worker_receives_explicit_storage_argument(self):
        selected = self.root / 'explicit-state'
        def run(arguments, **kwargs):
            self.assertIn('--state-dir', arguments)
            self.assertEqual(arguments[arguments.index('--state-dir') + 1], str(selected))
            return subprocess.CompletedProcess(arguments, 0, '', '')
        with mock.patch.object(subprocess, 'run', side_effect=run):
            doctor.call('synthetic task', state_dir=selected)

    def test_storage_rejects_linked_ancestor_without_allocating_through_it(self):
        target = self.root / 'target'
        target.mkdir(mode=0o700)
        alias = self.root / 'alias'
        alias.symlink_to(target, target_is_directory=True)
        selected = alias / 'state'
        with self.assertRaises(state_storage.StorageError) as caught:
            state_storage.prepare_storage(selected)
        self.assertIn('symbolic link', str(caught.exception))
        self.assertTrue(alias.is_symlink())
        self.assertEqual(list(target.iterdir()), [])

    def test_slot_allocator_reports_foreign_directory_owner(self):
        self.state.mkdir(mode=0o700)
        with mock.patch.object(os, 'geteuid', return_value=self.state.stat().st_uid + 1), \
             self.assertRaises(worker_slots.SlotError) as caught:
            worker_slots.acquire(self.state, wait=False)
        self.assertIn('owner', str(caught.exception))
        self.assertIn('uid', str(caught.exception))
        self.assertEqual(list(self.state.iterdir()), [])

    def test_workspace_default_uses_environment_state_root(self):
        source = self.repository()
        copy = workspace.create(source)
        self.assertEqual(copy.directory.parent, self.state / 'workspaces')
        self.assertFalse((self.home / '.local/state/codex-deepseek').exists())

    def test_workspace_cli_default_uses_environment_state_root(self):
        source = self.repository()
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(['workspace', 'create', str(source), '--json'])
        self.assertEqual(code, 0, err.getvalue())
        self.assertTrue((self.state / 'workspaces').is_dir())
        self.assertIn(str(self.state / 'workspaces'), out.getvalue())

    def test_workspace_error_distinguishes_link_from_private_target(self):
        source = self.repository()
        self.link_storage()
        with self.assertRaises(workspace.WorkspaceError) as caught:
            workspace.create(source, self.state)
        self.assertIn('symbolic link', str(caught.exception))

    def test_bootstrap_reports_link_before_creating_coordination_state(self):
        source = self.repository()
        project.attach(source, coordinator='both')
        target = self.link_storage()
        for runtime in ('codex', 'claude'):
            with self.subTest(runtime=runtime):
                result = coordinator_hooks.handle(dict(
                    hook_event_name='UserPromptSubmit', cwd=str(source),
                    session_id='storage-' + runtime, turn_id='first',
                    prompt='Inspect source'), runtime=runtime)
                text = result['hookSpecificOutput']['additionalContext']
                self.assertIn('symbolic link', text)
                self.assertIn('--state-dir', text)
                self.assertNotIn('coordination task task-', text)
                self.assertFalse((self.state / 'coordination').exists())
                self.assertEqual((target / 'retained-data').read_text(), 'preserve existing copies')

    def test_bootstrap_initializes_missing_private_storage(self):
        source = self.repository()
        project.attach(source, coordinator='codex')
        coordinator_hooks.handle(dict(hook_event_name='SessionStart', cwd=str(source),
                                      session_id='storage-fresh', source='startup'))
        self.assertEqual((self.state / 'workspaces').stat().st_mode & 0o777, 0o700)
        self.assertEqual(list((self.state / 'workspaces').iterdir()), [])

    def assert_failed_storage_lifecycle(self, source, reason):
        for runtime in ('codex', 'claude'):
            for event in ('UserPromptSubmit', 'PreToolUse', 'Stop'):
                with self.subTest(runtime=runtime, event=event):
                    result = coordinator_hooks.handle(dict(
                        hook_event_name=event, cwd=str(source),
                        session_id='storage-stale-' + runtime, turn_id='next',
                        prompt='Inspect source', tool_name='Write',
                        tool_input={'file_path': str(source / 'a.py'), 'content': 'x'}),
                        runtime=runtime)
                    output = result['hookSpecificOutput']
                    self.assertIn(reason, output['additionalContext'])
                    self.assertIn('Continue locally', output['additionalContext'])
                    self.assertNotIn('permissionDecision', output)
                    self.assertNotIn('decision', result)

    def test_failed_bootstrap_tool_and_stop_preserve_public_root(self):
        source = self.repository()
        project.attach(source, coordinator='both')
        self.state.mkdir(mode=0o755)
        self.state.chmod(0o755)
        self.assert_failed_storage_lifecycle(source, 'permissions')
        with self.assertRaises(coordination.CoordinationError):
            coordination.latest_task(source, 'storage-stale-codex')
        self.assertEqual(self.state.stat().st_mode & 0o777, 0o755)
        self.assertEqual(list(self.state.iterdir()), [])

    def test_failed_bootstrap_tool_and_stop_preserve_linked_root_target(self):
        source = self.repository()
        project.attach(source, coordinator='both')
        target = self.root / 'retained-state'
        target.mkdir(mode=0o755)
        target.chmod(0o755)
        (target / 'retained').write_text('keep')
        self.state.symlink_to(target, target_is_directory=True)
        self.assert_failed_storage_lifecycle(source, 'symbolic link')
        with self.assertRaises(coordination.CoordinationError):
            coordination.latest_task(source, 'storage-stale-codex')
        self.assertTrue(self.state.is_symlink())
        self.assertEqual(target.stat().st_mode & 0o777, 0o755)
        self.assertEqual(sorted(p.name for p in target.iterdir()), ['retained'])
        self.assertEqual((target / 'retained').read_text(), 'keep')

    def test_failed_bootstrap_tool_and_stop_preserve_linked_ancestor(self):
        source = self.repository()
        project.attach(source, coordinator='both')
        target = self.root / 'retained-parent'
        target.mkdir(mode=0o700)
        alias = self.root / 'alias'
        alias.symlink_to(target, target_is_directory=True)
        os.environ['DEEPSEEK_TEAM_STATE_DIR'] = str(alias / 'state')
        self.assert_failed_storage_lifecycle(source, 'symbolic link')
        with self.assertRaises(coordination.CoordinationError):
            coordination.latest_task(source, 'storage-stale-codex')
        self.assertTrue(alias.is_symlink())
        self.assertEqual(list(target.iterdir()), [])

    def test_failed_bootstrap_tool_and_stop_leave_old_tasks_untouched(self):
        source = self.repository()
        project.attach(source, coordinator='both')
        target = self.link_storage()
        for runtime in ('codex', 'claude'):
            coordination.begin_turn(source, session_id='storage-stale-' + runtime,
                                    turn_id='old', prompt='Old unfinished work',
                                    policy=settings.resolve(source), runtime=runtime)
        ledger = self.state / 'coordination'
        snapshot = {p.relative_to(ledger): p.read_bytes()
                    for p in ledger.rglob('*') if p.is_file()}
        self.assert_failed_storage_lifecycle(source, 'symbolic link')
        self.assertEqual(snapshot, {p.relative_to(ledger): p.read_bytes()
                                    for p in ledger.rglob('*') if p.is_file()})
        self.assertEqual((target / 'retained-data').read_text(), 'preserve existing copies')

    def test_disabled_bootstrap_does_not_initialize_storage(self):
        source = self.repository()
        activation.set_enabled(source, False)
        project.attach(source, coordinator='codex')
        coordinator_hooks.handle(dict(hook_event_name='SessionStart', cwd=str(source),
                                      session_id='storage-disabled', source='startup'))
        self.assertFalse(self.state.exists())

    def test_setup_reports_link_before_changing_runtime_configuration(self):
        target = self.link_storage()
        backend = sandbox.SandboxBackend(('/usr/bin/bwrap',), '/usr/bin/bwrap', 'direct')
        out, err = io.StringIO(), io.StringIO()
        with mock.patch('shutil.which', side_effect=lambda name: '/test/bin/' + name), \
             mock.patch.object(sandbox, 'probe_backend', return_value=backend), \
             mock.patch.object(doctor, 'main', return_value=0), \
             mock.patch.object(worker, 'load_api_key', side_effect=AssertionError('credential read')), \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(['setup', '--runtime', 'codex', '--no-key'])
        self.assertEqual(code, 78, out.getvalue() + err.getvalue())
        self.assertIn('symbolic link', out.getvalue() + err.getvalue())
        self.assertFalse(self.codex.exists())
        self.assertEqual((target / 'retained-data').read_text(), 'preserve existing copies')


if __name__ == '__main__':
    unittest.main()
