"""Logical leases survive execution gaps; process identity cannot be inherited."""
import importlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from codex_deepseek_team import workspace


class LifecycleFixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='dst-job-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / 'source'
        self.source.mkdir()
        self.state = self.root / 'state'
        self.patch = mock.patch.dict(os.environ, {
            'HOME': str(self.root / 'home'),
            'CODEX_HOME': str(self.root / 'codex'),
            'CLAUDE_CONFIG_DIR': str(self.root / 'claude'),
            'XDG_CONFIG_HOME': str(self.root / 'config'),
            'DEEPSEEK_TEAM_STATE_DIR': str(self.state),
        })
        self.patch.start()
        self.addCleanup(self.patch.stop)
        subprocess.run(['git', 'init', '-q', '-b', 'main', str(self.source)], check=True)
        (self.source / 'app.txt').write_text('base\n')
        (self.source / 'data.bin').write_bytes(b'\x00\x01\xff')
        subprocess.run(['git', '-C', str(self.source), 'add', '.'], check=True)
        subprocess.run(['git', '-C', str(self.source), '-c', 'user.name=Fixture',
                        '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'base'],
                       check=True)

    def api(self, name='workspace_lifecycle'):
        spec = importlib.util.find_spec('codex_deepseek_team.' + name)
        self.assertIsNotNone(spec, 'missing logical workspace lifecycle implementation')
        return importlib.import_module('codex_deepseek_team.' + name)

    def copy(self):
        return workspace.create(self.source, self.state)

    def require_capture(self):
        from tests import test_gc_adversarial
        test_gc_adversarial._skip_without_namespace(self, 'capture')


class JobLeaseTests(LifecycleFixture):
    def test_standalone_cli_lease_survives_its_short_lived_command_in_the_same_shell(self):
        api = self.api()
        current = api.process_identity(os.getpid())
        rows = {'/proc/10/comm':'deepseek-team', '/proc/10/stat':'10 (deepseek-team) S 20',
                '/proc/20/comm':'bash', '/proc/20/stat':'20 (bash) S 1'}
        def read(path, *args, **kwargs):
            return rows[str(path)]
        with mock.patch.object(api.os, 'getpid', return_value=10), \
                mock.patch.object(Path, 'read_text', autospec=True, side_effect=read), \
                mock.patch.object(api.os, 'readlink', side_effect=lambda p: '/bin/bash' if str(p)=='/proc/20/exe' else '/usr/bin/python3'), \
                mock.patch.object(api, 'process_identity', side_effect=lambda pid: dict(current, pid=pid)):
            self.assertEqual(api.owner_identity()['pid'], 20)

    def test_missing_new_lease_is_not_reinterpreted_as_a_legacy_orphan(self):
        copy = self.copy()
        (copy.directory / 'lease.json').unlink()
        report = self.api('workspace_gc').collect(self.state, apply=True)
        self.assertEqual(report['workspaces'][0]['action'], 'kept')
        self.assertTrue(copy.path.exists())
        with self.assertRaises(workspace.WorkspaceError):
            self.api().initialize(copy)

    def test_three_attempts_keep_job_workspace_inode_and_old_failure(self):
        api = self.api()
        copy = self.copy()
        api.initialize(copy)
        initial = api.read(copy)
        inode = copy.path.stat().st_ino
        for number, status in enumerate(('failed', 'failed', 'succeeded'), 1):
            with copy.lock(recover=True):
                # Automatic begin/finish integration may already be present.
                copy.begin('execution')
                if len(api.read(copy)['attempts']) < number:
                    api.begin(copy, 'execution')
                (copy.path / 'app.txt').write_text(f'fix {number}\n')
                copy.finish(status, exit_code=9 if status == 'failed' else 0)
                api.finish(copy)
            current = api.read(copy)
            self.assertEqual(current['job_id'], initial['job_id'])
            self.assertEqual(current['workspace_id'], copy.id)
            self.assertEqual(copy.path.stat().st_ino, inode)
            self.assertEqual(current['phase'], 'review')
            self.assertTrue(api.live(current))
            if status == 'failed':
                api.rework(copy)
                self.assertEqual(api.read(copy)['phase'], 'retry')
        attempts = api.read(copy)['attempts']
        self.assertEqual(len({a['id'] for a in attempts}), 3)
        self.assertEqual([a['status'] for a in attempts], ['failed', 'failed', 'succeeded'])
        self.assertEqual([a['exit_code'] for a in attempts], [9, 9, 0])
        self.assertEqual((copy.path / 'app.txt').read_text(), 'fix 3\n')

    def test_pid_boot_and_start_time_are_all_required(self):
        api = self.api()
        identity = api.process_identity(os.getpid())
        self.assertTrue(api.identity_live(identity))
        for changed in ({'boot_id': 'different-boot'}, {'start_time': '0'},
                        {'pid': 999999999}, {'uid': os.geteuid() + 1}):
            self.assertFalse(api.identity_live(dict(identity, **changed)))
        for invalid in (None, {}, {'pid': True}, {'pid': '1'}):
            self.assertFalse(api.identity_live(invalid))

    def test_review_gap_is_leased_without_worker_process(self):
        api = self.api()
        copy = self.copy()
        api.initialize(copy)
        with copy.lock():
            copy.begin('execution')
            if not api.read(copy)['attempts']:
                api.begin(copy, 'execution')
            copy.finish('failed', exit_code=5)
            api.finish(copy)
        api.rework(copy)
        row = api.read(copy)
        self.assertTrue(api.live(row))
        self.assertEqual(row['phase'], 'retry')
        self.assertNotIn('worker_pid', row)

    def test_recover_changes_owner_generation_preserves_attempts(self):
        api = self.api()
        copy = self.copy()
        before = api.read(copy)
        before['owner'] = {'pid': 999999999, 'start_time': '0',
                           'boot_id': 'old', 'uid': os.geteuid()}
        api.write(copy, before)  # Simulate the persisted identity of a dead host.
        before = api.read(copy)
        api.renew(copy, owner=api.process_identity(os.getpid()), recover=True)
        after = api.read(copy)
        self.assertEqual(after['generation'], before['generation'] + 1)
        self.assertEqual(after['job_id'], before['job_id'])
        self.assertEqual(after['attempts'], before['attempts'])
        self.assertTrue(api.live(after))

    def test_lease_metadata_symlink_never_reads_external_target(self):
        api = self.api()
        copy = self.copy()
        api.initialize(copy)
        sentinel = self.root / 'canary'
        sentinel.write_text('private canary')
        (copy.directory / 'lease.json').unlink()
        (copy.directory / 'lease.json').symlink_to(sentinel)
        with self.assertRaises(workspace.WorkspaceError):
            api.read(copy)
        self.assertEqual(sentinel.read_text(), 'private canary')

    def test_bad_lease_cannot_be_adopted_as_a_new_job(self):
        api = self.api()
        copy = self.copy()
        api.initialize(copy)
        lease = copy.directory / 'lease.json'
        bad = json.loads(lease.read_text())
        bad['workspace_id'] = 'other'
        lease.write_text(json.dumps(bad))
        with self.assertRaises(workspace.WorkspaceError):
            api.initialize(copy)
        self.assertEqual(json.loads(lease.read_text())['workspace_id'], 'other')


if __name__ == '__main__':
    unittest.main()
