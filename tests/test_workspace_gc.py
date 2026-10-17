"""Collection deletes execution data only after durable result publication."""
import json
import os
import socket
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest import mock

from codex_deepseek_team import workspace
from tests.test_workspace_lifecycle import LifecycleFixture


class WorkspaceCollectionTests(LifecycleFixture):
    def test_unrelated_uninspectable_process_does_not_veto_every_owned_copy(self):
        gc = self.api('workspace_gc')
        proc = self.root / 'fake-proc'
        process = proc / '999999999'
        process.mkdir(parents=True)
        (process / 'cmdline').write_bytes(b'keyring-service\0')
        with mock.patch.object(gc, 'Path', return_value=proc), \
                mock.patch.object(gc.os, 'readlink', side_effect=PermissionError('ptrace restricted')):
            self.assertIsNone(gc._references([self.root / 'owned-work']))
        (process / 'cmdline').write_bytes(os.fsencode(self.root / 'owned-work') + b'\0')
        with mock.patch.object(gc, 'Path', return_value=proc), \
                mock.patch.object(gc.os, 'readlink', side_effect=PermissionError('ptrace restricted')):
            self.assertEqual(gc._references([self.root / 'owned-work'])['pid'], 999999999)

    def test_same_device_file_bind_mount_is_detected_before_quarantine(self):
        from tests import test_gc_adversarial as adversarial
        adversarial._skip_without_namespace(self, 'mount')
        copy = self.completed()
        source = self.root / 'mounted-canary'
        source.write_text('outside file must remain')
        lease = self.api().read(copy)
        lease['owner'] = {'pid': 999999999, 'start_time':'0', 'boot_id':'old', 'uid':os.geteuid()}
        self.api().write(copy, lease)
        script = '''
import ctypes, json, os, sys
from pathlib import Path
from codex_deepseek_team import workspace_gc
root, identifier = Path(sys.argv[1]), sys.argv[2]
source = root / 'mounted-canary'
target = root / 'state/workspaces' / identifier / 'work/app.txt'
libc = ctypes.CDLL(None, use_errno=True)
if libc.mount(os.fsencode(source), os.fsencode(target), None, 4096, None):
    raise SystemExit('mount errno ' + str(ctypes.get_errno()))
report = workspace_gc.collect(root / 'state', apply=False)
item = next(r for r in report['workspaces'] if r['id'] == identifier)
assert item['action'] == 'kept' and 'mount' in item['reason'].lower(), item
assert target.exists()
print('file bind mount preserved before quarantine')
'''
        command = [*adversarial._NAMESPACE_PREFIX['mount'], '--die-with-parent',
            '--unshare-user', '--unshare-pid', '--unshare-net', '--cap-add', 'CAP_SYS_ADMIN',
            '--ro-bind', '/', '/', '--proc', '/proc', '--dev', '/dev', '--tmpfs', '/tmp',
            '--bind', str(self.root), str(self.root), '--', '/usr/bin/python3', '-c', script,
            str(self.root), copy.id]
        result = subprocess.run(command, capture_output=True, text=True, timeout=30,
            env=dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / 'src')))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(copy.path.exists())
        self.assertEqual(source.read_text(), 'outside file must remain')

    def test_caller_open_execution_file_defers_cleanup(self):
        copy = self.completed()
        with (copy.path / 'app.txt').open('rb'):
            with self.assertRaises(workspace.WorkspaceError):
                self.api('workspace_gc').finalize(copy)
            self.assertTrue(copy.path.exists())

    def test_nonroot_capture_keeps_the_callers_mapped_nonzero_identity(self):
        gc = self.api('workspace_gc')
        copy = self.completed()
        with mock.patch('codex_deepseek_team.sandbox.probe_backend',
                        return_value=SimpleNamespace(prefix=('bwrap',))), \
                mock.patch.object(gc.os, 'geteuid', return_value=1001), \
                mock.patch.object(gc.os, 'getegid', return_value=1002), \
                mock.patch.object(workspace, '_git_digest', return_value=copy.metadata['git_digest']), \
                mock.patch.object(gc.subprocess, 'run',
                        return_value=SimpleNamespace(returncode=0, stdout=b'')) as run:
            gc._capture(copy)
        command = run.call_args.args[0]
        self.assertEqual(command[command.index('--uid') + 1], '1001')
        self.assertEqual(command[command.index('--gid') + 1], '1002')

    def test_stopped_runtime_socket_left_by_crash_does_not_prevent_cleanup(self):
        copy = self.completed()
        runtime = copy.directory / 'runtime-interrupted'
        runtime.mkdir(mode=0o700)
        fd = os.open(runtime, os.O_RDONLY | os.O_DIRECTORY)
        try:
            with socket.socket(socket.AF_UNIX) as connection:
                connection.bind(f'/proc/self/fd/{fd}/provider.sock')
        finally:
            os.close(fd)
        self.api('workspace_gc').finalize(copy)
        self.assertFalse(runtime.exists())
        self.assertFalse(copy.path.exists())

    def completed(self):
        self.require_capture()
        copy = self.copy()
        (copy.path / 'app.txt').write_text('accepted fix\n')
        (copy.path / 'data.bin').write_bytes(b'\x00\xfe\x81new\x00')
        (copy.path / 'new.bin').write_bytes(b'\x00untracked\xff')
        with copy.lock():
            copy.begin('execution')
            copy.finish('succeeded')
        return copy

    def test_finalize_deletes_immediately_and_binary_patch_applies(self):
        gc = self.api('workspace_gc')
        copy = self.completed()
        inode = copy.metadata['work_inode']
        before = dict(copy.metadata)
        report = gc.finalize(copy)
        self.assertFalse(copy.path.exists())
        self.assertEqual(report['action'], 'cleaned')
        stored = json.loads((copy.directory / 'record.json').read_text())
        self.assertEqual(stored['work_inode'], inode)
        for key in ('status', 'exit_code', 'finished_at', 'changed_files'):
            self.assertEqual(stored[key], before[key])
        patch = copy.directory / 'changes.patch'
        subprocess.run(['git', '-C', str(self.source), 'apply', '--binary', str(patch)], check=True)
        self.assertEqual((self.source / 'data.bin').read_bytes(), b'\x00\xfe\x81new\x00')
        self.assertEqual((self.source / 'new.bin').read_bytes(), b'\x00untracked\xff')
        self.assertEqual((self.source / 'app.txt').read_text(), 'accepted fix\n')
        self.assertTrue((copy.directory / 'lease.json').is_file())
        self.assertEqual(gc.finalize(copy)['action'], 'already-cleaned')

    def test_live_review_and_retry_survive_gc_without_owner_lock(self):
        gc, lifecycle = self.api('workspace_gc'), self.api()
        copy = self.completed()
        lifecycle.initialize(copy)
        lifecycle.rework(copy)
        before = copy.path.stat().st_ino
        rows = gc.collect(self.state, apply=True)['workspaces']
        self.assertEqual(next(r for r in rows if r['id'] == copy.id)['action'], 'kept')
        self.assertEqual(copy.path.stat().st_ino, before)

    def test_dry_run_has_no_filesystem_side_effects(self):
        gc = self.api('workspace_gc')
        copy = self.completed()
        # A legacy finished copy has no durable lease.
        (copy.directory / 'lease.json').unlink(missing_ok=True)
        copy.metadata.pop('job_id', None)
        copy.metadata.pop('attempt_id', None)
        copy.save()
        before = {str(p.relative_to(self.state)): p.lstat() for p in self.state.rglob('*')}
        report = gc.collect(self.state)
        after = {str(p.relative_to(self.state)): p.lstat() for p in self.state.rglob('*')}
        self.assertEqual(before.keys(), after.keys())
        self.assertEqual({p: (s.st_ino, s.st_mtime_ns, s.st_size) for p, s in before.items()},
                         {p: (s.st_ino, s.st_mtime_ns, s.st_size) for p, s in after.items()})
        row = next(r for r in report['workspaces'] if r['id'] == copy.id)
        self.assertEqual(row['action'], 'would-clean')
        self.assertGreater(row['allocated_bytes'], 0)
        self.assertGreater(row['inodes'], 0)

    def test_owner_lock_blocks_finalize(self):
        gc = self.api('workspace_gc')
        copy = self.completed()
        with copy.lock():
            with self.assertRaises(workspace.WorkspaceError) as caught:
                gc.finalize(copy)
            self.assertEqual(caught.exception.code, 75)
            self.assertTrue(copy.path.exists())

    def test_legacy_orphan_parent_status_does_not_keep_copy(self):
        self.require_capture()
        gc = self.api('workspace_gc')
        for status in ('succeeded', 'failed', 'ready', 'preparing', 'running'):
            copy = self.copy()
            copy.metadata['status'] = status
            copy.metadata['coord_task'] = 'old-active-task'
            copy.save()
            (copy.directory / 'lease.json').unlink(missing_ok=True)
            copy.metadata.pop('job_id', None)
            copy.metadata.pop('attempt_id', None)
            copy.save()
        report = gc.collect(self.state, apply=True)
        self.assertTrue(all(r['action'] == 'cleaned' for r in report['workspaces']))
        self.assertFalse(any((p / 'work').exists() for p in (self.state / 'workspaces').iterdir()))

    def test_work_symlink_and_swapped_inode_preserve_outside_canary(self):
        gc = self.api('workspace_gc')
        copy = self.completed()
        original = copy.directory / 'original'
        copy.path.rename(original)
        outside = self.root / 'outside'
        outside.mkdir()
        (outside / 'canary').write_text('preserve')
        copy.path.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(workspace.WorkspaceError):
            gc.finalize(copy)
        self.assertEqual((outside / 'canary').read_text(), 'preserve')
        copy.path.unlink()
        copy.path.mkdir(mode=0o700)
        with self.assertRaises(workspace.WorkspaceError):
            gc.finalize(copy)
        self.assertTrue(original.exists())

    def test_cancel_does_not_rewrite_succeeded_history(self):
        gc = self.api('workspace_gc')
        copy = self.completed()
        before = (copy.directory / 'record.json').read_bytes()
        gc.cancel(copy, reason='explicit rejection')
        self.assertFalse(copy.path.exists())
        self.assertEqual((copy.directory / 'record.json').read_bytes(), before)
        self.assertEqual(self.api().read(copy)['phase'], 'cancelled')

    def test_collection_never_executes_git_as_root(self):
        gc = self.api('workspace_gc')
        copy = self.completed()
        # A finished copy already has a verified result patch. GC must consume
        # that durable result rather than run programs from its Git metadata.
        with mock.patch.object(workspace, 'git', side_effect=AssertionError('GC invoked Git')):
            gc.finalize(copy)
        self.assertFalse(copy.path.exists())

    def test_inspection_reads_cleaned_history_and_result_without_work(self):
        gc = self.api('workspace_gc')
        copy = self.completed()
        gc.finalize(copy)
        self.assertTrue(hasattr(gc, 'inspect_workspace'), 'inspection API missing')
        report = gc.inspect_workspace(self.state, copy.id)
        self.assertEqual(report['metadata']['status'], 'succeeded')
        self.assertEqual(report['lease']['phase'], 'finalized')
        self.assertFalse(report['execution_present'])
        self.assertGreater(report['result']['patch']['bytes'], 0)

    def test_finalization_removes_owned_reproducible_environments(self):
        gc = self.api('workspace_gc')
        copy = self.completed()
        for name in ('preparation-home', 'runtime-fixture'):
            directory = copy.directory / name
            directory.mkdir(mode=0o700)
            (directory / 'reproducible-runtime').write_bytes(b'0' * 4096)
        gc.finalize(copy)
        self.assertFalse((copy.directory / 'preparation-home').exists())
        self.assertFalse((copy.directory / 'runtime-fixture').exists())

    def test_partial_deletion_recovers_without_rewriting_history(self):
        gc = self.api('workspace_gc')
        copy = self.completed()
        original_record = (copy.directory / 'record.json').read_bytes()
        with mock.patch.object(gc, '_remove_tree', side_effect=OSError('fixture interrupted rmtree')):
            with self.assertRaises(OSError):
                gc.finalize(copy)
        self.assertFalse(copy.path.exists())
        self.assertTrue(any((self.state / 'quarantine').iterdir()))
        gc.finalize(copy)
        self.assertFalse(any((self.state / 'quarantine').iterdir()))
        self.assertEqual((copy.directory / 'record.json').read_bytes(), original_record)

    def test_owner_lock_survives_quarantine_while_creation_fence_is_released(self):
        gc = self.api('workspace_gc')
        copy = self.completed()
        remove = gc._remove_tree
        observed = []
        def deleting(parent, name, expected):
            if not observed:
                with self.assertRaises(workspace.WorkspaceError) as caught:
                    with gc._owner(copy):
                        pass
                self.assertEqual(caught.exception.code, 75)
                another = self.copy()  # Creation must not wait for full rmtree.
                observed.append(another.id)
            return remove(parent, name, expected)
        with mock.patch.object(gc, '_remove_tree', side_effect=deleting):
            gc.finalize(copy)
        self.assertEqual(len(observed), 1)

    def test_real_open_file_in_another_process_defers_finalization(self):
        gc = self.api('workspace_gc')
        copy = self.completed()
        process = subprocess.Popen([sys.executable, '-c',
            'import sys; f=open(sys.argv[1]); print("ready", flush=True); sys.stdin.read()',
            str(copy.path / 'app.txt')], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            text=True)
        try:
            self.assertEqual(process.stdout.readline().strip(), 'ready')
            with self.assertRaises(workspace.WorkspaceError) as caught:
                gc.finalize(copy)
            self.assertEqual(caught.exception.code, 75)
            self.assertTrue(copy.path.exists())
        finally:
            process.communicate(timeout=10)
        gc.finalize(copy)
        self.assertFalse(copy.path.exists())

    def test_preparing_copy_with_only_reproducible_git_data_can_be_cancelled(self):
        gc = self.api('workspace_gc')
        git = workspace.git
        def stop_fetch(root, *args, **kwargs):
            if args and args[0] == 'fetch':
                raise workspace.WorkspaceError('fixture interrupted source fetch')
            return git(root, *args, **kwargs)
        with mock.patch.object(workspace, 'git', side_effect=stop_fetch):
            with self.assertRaises(workspace.WorkspaceError) as caught:
                self.copy()
        copy = gc._load_record(self.state, caught.exception.workspace_id)
        self.assertEqual({p.name for p in copy.path.iterdir()}, {'.git'})
        before = (copy.directory / 'record.json').read_bytes()
        gc.cancel(copy)
        self.assertFalse(copy.path.exists())
        self.assertEqual((copy.directory / 'record.json').read_bytes(), before)
        self.assertEqual((copy.directory / 'changes.patch').read_bytes(), b'')

    def test_owner_in_another_pid_namespace_is_uncertain_and_preserved(self):
        gc, lifecycle = self.api('workspace_gc'), self.api()
        copy = self.completed()
        row = lifecycle.read(copy)
        self.assertIn('pid_namespace', row['owner'], 'owner PID namespace identity is missing')
        row['owner']['pid_namespace'] += 1
        lifecycle.write(copy, row)
        report = gc.collect(self.state, apply=True)
        item = next(item for item in report['workspaces'] if item['id'] == copy.id)
        self.assertEqual(item['action'], 'kept')
        self.assertTrue(copy.path.exists())
