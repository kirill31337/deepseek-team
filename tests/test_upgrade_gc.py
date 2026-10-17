"""Package-update recovery is bounded to validated state roots and keeps live jobs."""
import contextlib
import errno
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from codex_deepseek_team import (cli, gc_cli, onboarding, sandbox, settings,
                                 state_storage, storage_selection, workspace,
                                 workspace_gc, workspace_lifecycle)


class UpgradeGcCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='dst-upgrade-gc-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.home = self.root / 'home'
        self.home.mkdir()
        self.state = self.root / 'state'
        self.source = self.root / 'source-a'
        self.other = self.root / 'source-b'
        for repository in (self.source, self.other):
            repository.mkdir()
            subprocess.run(['git', 'init', '-q', '-b', 'main', str(repository)],
                           check=True)
            (repository / 'app.txt').write_text('base\n')
            subprocess.run(['git', '-C', str(repository), 'add', '.'], check=True)
            subprocess.run(['git', '-C', str(repository), '-c', 'user.name=Fixture',
                            '-c', 'user.email=fixture@example.invalid',
                            'commit', '-qm', 'base'], check=True)
        environment = mock.patch.dict(os.environ, {
            'HOME': str(self.home),
            'CODEX_HOME': str(self.root / 'codex'),
            'CLAUDE_CONFIG_DIR': str(self.root / 'claude'),
            'XDG_CONFIG_HOME': str(self.root / 'config'),
            'DEEPSEEK_TEAM_STATE_DIR': str(self.state),
        })
        environment.start()
        self.addCleanup(environment.stop)
        capture = mock.patch.object(
            workspace_gc, '_capture',
            side_effect=lambda copy: (copy.directory / 'changes.patch').read_bytes())
        capture.start()
        self.addCleanup(capture.stop)

    def completed(self, source=None, state=None):
        copy = workspace.create(source or self.source, state or self.state)
        with copy.lock():
            copy.begin('execution')
            (copy.path / 'app.txt').write_text('accepted fix\n')
            copy.finish('succeeded')
        # Upgrade fixtures represent 0.8.x records, which had no job lease.
        (copy.directory / 'lease.json').unlink(missing_ok=True)
        copy.metadata.pop('job_id', None)
        copy.metadata.pop('attempt_id', None)
        copy.save()
        return copy

    def run_cli(self, *args, cwd=None):
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.chdir(cwd or self.root), contextlib.redirect_stdout(output), \
             contextlib.redirect_stderr(errors):
            code = cli.main([str(value) for value in args])
        return subprocess.CompletedProcess(args, code, output.getvalue(), errors.getvalue())


class KnownStateRootTests(UpgradeGcCase):
    def test_roots_include_current_default_and_validated_saved_selection(self):
        selected = self.root / 'selected-state'
        previous = self.root / 'previous-state'
        state_storage.check_private_directory(selected, create=True)
        state_storage.check_private_directory(previous, create=True)
        with storage_selection.locked() as directory:
            storage_selection.publish(directory, selected, previous)
        roots = gc_cli.known_state_roots()
        self.assertEqual(roots, [self.state, self.home / '.local/state/codex-deepseek',
                                 selected, previous])
        self.assertNotIn(self.root / 'unrelated', roots)

        explicit = self.root / 'explicit-state'
        self.assertEqual(gc_cli.known_state_roots(explicit), [explicit])

    def test_unsafe_saved_root_is_not_adopted(self):
        selected = self.root / 'selected-state'
        unsafe = self.root / 'unsafe-state'
        state_storage.check_private_directory(selected, create=True)
        state_storage.check_private_directory(unsafe, create=True)
        unsafe.chmod(0o755)
        with storage_selection.locked() as directory:
            storage_selection.publish(directory, selected, unsafe)
        roots = gc_cli.known_state_roots()
        self.assertIn(selected, roots)
        self.assertNotIn(unsafe, roots)

        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop('DEEPSEEK_TEAM_STATE_DIR', None)
            roots = gc_cli.known_state_roots()
        self.assertNotIn(unsafe, roots)
        self.assertIn(selected, roots)


class UpgradeRecoveryTests(UpgradeGcCase):
    def test_gc_all_roots_cleans_the_default_root_only(self):
        default_state = self.home / '.local/state/codex-deepseek'
        stale = self.completed(state=default_state)
        outside = self.root / 'unrelated-state'
        outside.mkdir()
        canary = outside / 'canary'
        canary.write_text('preserve\n')
        result = self.run_cli('gc', '--all-roots', '--apply', '--json')
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        row = next(row for row in report['workspaces'] if row['id'] == stale.id)
        self.assertEqual(row['action'], 'cleaned')
        self.assertEqual(row['state_root'], str(default_state))
        self.assertFalse(stale.path.exists())
        self.assertEqual(canary.read_text(), 'preserve\n')

    def test_init_upgrades_old_owned_copy_from_another_project_and_keeps_retry(self):
        stale = self.completed()
        retry = self.completed()
        workspace_lifecycle.initialize(retry)
        workspace_lifecycle.rework(retry)
        external_home = self.root / 'inherited-home'
        external_state = external_home / '.local/state/codex-deepseek'
        with mock.patch.dict(os.environ, {'HOME': str(external_home)}, clear=False):
            (external_state / 'workspaces').mkdir(parents=True)
            canary = external_state / 'workspaces' / 'external-canary'
            canary.write_text('never touch\n')

        result = self.run_cli('init', '--coordinator', 'codex', str(self.other),
                              cwd=self.other)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(stale.path.exists())
        self.assertTrue(retry.path.is_dir())
        self.assertEqual(workspace_lifecycle.read(retry)['phase'], 'retry')
        self.assertEqual(canary.read_text(), 'never touch\n')
        self.assertTrue((self.other / 'AGENTS.md').is_file())

    def test_upgrade_cleanup_scans_validated_saved_previous_root(self):
        selected = self.root / 'selected-state'
        previous = self.root / 'previous-state'
        state_storage.check_private_directory(selected, create=True)
        state_storage.check_private_directory(previous, create=True)
        stale = self.completed(state=previous)
        with storage_selection.locked() as directory:
            storage_selection.publish(directory, selected, previous)
        report = gc_cli.upgrade_cleanup()
        row = next(row for row in report['workspaces'] if row['id'] == stale.id)
        self.assertEqual(row['action'], 'cleaned')
        self.assertEqual(row['state_root'], str(previous))
        self.assertFalse(stale.path.exists())

    def test_setup_upgrades_copy_without_reading_credentials_or_changing_settings(self):
        stale = self.completed()
        global_file = settings.global_file()
        settings.set_values(global_file, delegation_level=75, access='full-access',
                            effort='max', max_workers=3)
        settings_before = global_file.read_bytes()
        backend = sandbox.SandboxBackend(('/test/bwrap',), '/test/bwrap', 'direct')
        output, errors = io.StringIO(), io.StringIO()
        with mock.patch('shutil.which', side_effect=lambda name: '/test/bin/' + name), \
             mock.patch.object(sandbox, 'probe_backend', return_value=backend), \
             mock.patch.object(onboarding.doctor, 'main', return_value=0), \
             mock.patch.object(onboarding.config, 'codex_hooks_status', return_value=True), \
             mock.patch('codex_deepseek_team.worker.load_api_key',
                        side_effect=AssertionError('setup read a credential')), \
             contextlib.chdir(self.other), contextlib.redirect_stdout(output), \
             contextlib.redirect_stderr(errors):
            code = cli.main(['setup', '--runtime', 'codex', '--no-key'])
        self.assertEqual(code, 0, errors.getvalue())
        self.assertFalse(stale.path.exists())
        self.assertEqual(global_file.read_bytes(), settings_before)

    def test_upgrade_cleanup_reports_unsafe_copy_without_deleting_it(self):
        copy = self.completed()
        outside = self.root / 'outside'
        outside.mkdir()
        canary = outside / 'canary.txt'
        canary.write_text('preserve\n')
        original = copy.directory / 'original-work'
        copy.path.rename(original)
        copy.path.symlink_to(outside, target_is_directory=True)
        report = gc_cli.upgrade_cleanup()
        row = next(row for row in report['workspaces'] if row['id'] == copy.id)
        self.assertEqual(row['action'], 'kept')
        self.assertIn('preserved', row['reason'].lower())
        self.assertEqual(canary.read_text(), 'preserve\n')
        self.assertTrue(original.is_dir())

    def test_upgrade_cleanup_returns_root_errors_without_deleting_data(self):
        unsafe = self.root / 'unsafe-root'
        unsafe.mkdir(mode=0o755)
        canary = unsafe / 'canary'
        canary.write_text('preserve\n')
        report = gc_cli.upgrade_cleanup(unsafe)
        self.assertFalse(report['workspaces'])
        self.assertTrue(report['errors'])
        self.assertEqual(canary.read_text(), 'preserve\n')


if __name__ == '__main__':
    unittest.main()
