"""Workspace lifecycle commands remain safe after the execution tree is gone."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from codex_deepseek_team import cli, workspace, workspace_gc, workspace_lifecycle


class LifecycleCliCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='dst-lifecycle-cli-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / 'source'
        self.source.mkdir()
        self.state = self.root / 'state'
        environment = mock.patch.dict(os.environ, {
            'HOME': str(self.root / 'home'),
            'CODEX_HOME': str(self.root / 'codex'),
            'CLAUDE_CONFIG_DIR': str(self.root / 'claude'),
            'XDG_CONFIG_HOME': str(self.root / 'config'),
            'DEEPSEEK_TEAM_STATE_DIR': str(self.state),
        })
        environment.start()
        self.addCleanup(environment.stop)
        subprocess.run(['git', 'init', '-q', '-b', 'main', str(self.source)], check=True)
        (self.source / 'app.txt').write_text('base\n')
        subprocess.run(['git', '-C', str(self.source), 'add', '.'], check=True)
        subprocess.run(['git', '-C', str(self.source), '-c', 'user.name=Fixture',
                        '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'base'],
                       check=True)
        # The required unprivileged result-capture namespace is unavailable in
        # this test host. Reuse the real patch produced by Workspace.finish; the
        # quarantine, deletion, lease and inspection paths remain unmodified.
        self.capture = mock.patch.object(
            workspace_gc, '_capture',
            side_effect=lambda copy: (copy.directory / 'changes.patch').read_bytes())
        self.capture.start()
        self.addCleanup(self.capture.stop)

    def run_cli(self, *args):
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = cli.main([str(value) for value in args])
        return subprocess.CompletedProcess(args, code, output.getvalue(), errors.getvalue())

    def completed(self):
        copy = workspace.create(self.source, self.state)
        with copy.lock():
            copy.begin('execution')
            (copy.path / 'app.txt').write_text('accepted fix\n')
            copy.finish('succeeded')
        return copy

    def snapshot(self):
        return {str(path.relative_to(self.state)):
                (path.lstat().st_ino, path.lstat().st_mtime_ns,
                 path.lstat().st_size, path.lstat().st_mode)
                for path in self.state.rglob('*')}

    def legacy(self, copy):
        """Model an actual pre-lease installation rather than a live new job."""
        (copy.directory / 'lease.json').unlink(missing_ok=True)
        copy.metadata.pop('job_id', None)
        copy.metadata.pop('attempt_id', None)
        copy.save()
        return copy


class LifecycleInspectionTests(LifecycleCliCase):
    def test_show_list_history_and_diff_read_cleaned_history(self):
        copy = self.completed()
        result = self.run_cli('workspace', 'finalize', copy.id,
                              '--state-dir', self.state, '--reason', 'accepted review')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(copy.path.exists())

        shown = self.run_cli('workspace', 'show', copy.id,
                             '--state-dir', self.state, '--json')
        self.assertEqual(shown.returncode, 0, shown.stderr)
        inspection = json.loads(shown.stdout)
        self.assertEqual(inspection['id'], copy.id)
        self.assertFalse(inspection['execution_present'])
        self.assertEqual(inspection['metadata']['status'], 'succeeded')
        self.assertEqual(inspection['lease']['phase'], 'finalized')
        self.assertGreater(inspection['result']['patch']['bytes'], 0)

        listed = self.run_cli('workspace', 'list', '--state-dir', self.state, '--json')
        self.assertEqual(listed.returncode, 0, listed.stderr)
        rows = json.loads(listed.stdout)
        self.assertEqual([row['id'] for row in rows], [copy.id])
        self.assertFalse(rows[0]['execution_present'])

        history = self.run_cli('workspace', 'history', copy.id,
                               '--state-dir', self.state, '--json')
        self.assertEqual(history.returncode, 0, history.stderr)
        events = json.loads(history.stdout)['lease']['events']
        self.assertIn('finalized', [event['event'] for event in events])
        self.assertIn('cleaned', [event['event'] for event in events])

        diff = self.run_cli('workspace', 'diff', copy.id, '--state-dir', self.state)
        self.assertEqual(diff.returncode, 0, diff.stderr)
        self.assertIn('accepted fix', diff.stdout)
        self.assertIn('app.txt', diff.stdout)

    def test_cancel_preserves_original_execution_status(self):
        copy = self.completed()
        before = (copy.directory / 'record.json').read_bytes()
        result = self.run_cli('workspace', 'cancel', copy.id,
                              '--state-dir', self.state, '--reason', 'explicit rejection')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(copy.path.exists())
        self.assertEqual((copy.directory / 'record.json').read_bytes(), before)
        shown = self.run_cli('workspace', 'show', copy.id,
                             '--state-dir', self.state, '--json')
        self.assertEqual(shown.returncode, 0, shown.stderr)
        row = json.loads(shown.stdout)
        self.assertEqual(row['metadata']['status'], 'succeeded')
        self.assertEqual(row['lease']['phase'], 'cancelled')
        self.assertEqual(row['lease']['final_reason'], 'explicit rejection')

    def test_resume_reclaims_ownership_without_starting_execution(self):
        copy = self.completed()
        dead = {'pid': 999999999, 'start_time': '0', 'boot_id': 'old',
                'uid': os.geteuid()}
        lease = workspace_lifecycle.read(copy)
        lease['owner'] = dead
        workspace_lifecycle.write(copy, lease)
        before = workspace_lifecycle.read(copy)
        result = self.run_cli('workspace', 'resume', copy.id, '--state-dir', self.state)
        self.assertEqual(result.returncode, 0, result.stderr)
        after = workspace_lifecycle.read(copy)
        self.assertEqual(after['generation'], before['generation'] + 1)
        self.assertEqual(after['owner'], workspace_lifecycle.owner_identity())
        self.assertEqual(after['attempts'], before['attempts'])
        self.assertEqual(copy.metadata['status'], 'succeeded')
        self.assertTrue(copy.path.is_dir())

    def test_resume_rejects_a_finalized_copy(self):
        copy = self.completed()
        self.assertEqual(self.run_cli('workspace', 'finalize', copy.id,
                                      '--state-dir', self.state).returncode, 0)
        result = self.run_cli('workspace', 'resume', copy.id, '--state-dir', self.state)
        self.assertEqual(result.returncode, 73)
        self.assertIn('finalized', result.stderr.lower())


class CollectionCliTests(LifecycleCliCase):
    def test_gc_dry_run_has_no_filesystem_side_effects(self):
        copy = self.legacy(self.completed())
        before = self.snapshot()
        result = self.run_cli('gc', '--state-dir', self.state, '--json')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.snapshot(), before)
        report = json.loads(result.stdout)
        self.assertTrue(report['dry_run'])
        row = next(row for row in report['workspaces'] if row['id'] == copy.id)
        self.assertEqual(row['action'], 'would-clean')
        self.assertIn('reason', row)
        self.assertGreater(row['allocated_bytes'], 0)
        self.assertGreater(row['inodes'], 0)
        self.assertEqual(report['reclaimed'], {'allocated_bytes': 0, 'inodes': 0})

    def test_gc_apply_preserves_active_retry(self):
        copy = self.completed()
        workspace_lifecycle.initialize(copy)
        workspace_lifecycle.rework(copy)
        inode = copy.path.stat().st_ino
        result = self.run_cli('gc', '--apply', '--state-dir', self.state, '--json')
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        row = next(row for row in report['workspaces'] if row['id'] == copy.id)
        self.assertEqual(row['action'], 'kept')
        self.assertIn('active logical job lease', row['reason'])
        self.assertEqual(copy.path.stat().st_ino, inode)
        self.assertEqual(workspace_lifecycle.read(copy)['phase'], 'retry')

    def test_legacy_parent_status_does_not_keep_orphaned_copy(self):
        copy = self.legacy(self.completed())
        copy.metadata.update(status='running', coord_task='legacy-active-task')
        copy.save()
        (copy.directory / 'lease.json').unlink(missing_ok=True)
        result = self.run_cli('gc', '--apply', '--state-dir', self.state, '--json')
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        row = next(row for row in report['workspaces'] if row['id'] == copy.id)
        self.assertEqual(row['action'], 'cleaned')
        self.assertFalse(copy.path.exists())
        self.assertEqual(row['allocated_bytes'], report['reclaimed']['allocated_bytes'])


if __name__ == '__main__':
    unittest.main()
