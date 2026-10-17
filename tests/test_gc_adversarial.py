"""Adversarial lifecycle/collection acceptance tests.

These cases intentionally use real processes, descriptor-relative filesystem
checks and durable crash-state fixtures. Namespace-dependent cases skip only
when the hosting worker boundary cannot create the required namespaces; the
skip text names the coordinator rerun command.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import re
import select
import stat
import subprocess
import sys
import unittest
import uuid
from unittest import mock

from codex_deepseek_team import workspace
from tests import test_workspace_lifecycle as lifecycle_fixture


STRESS_ENV = 'DEEPSEEK_TEAM_GC_STRESS_JOBS'
STRESS_RUN = ('PYTHONPATH=src DEEPSEEK_TEAM_GC_STRESS_JOBS=100 '
              'python3 -m unittest tests.test_gc_adversarial.GCStressAcceptanceTests -v')
ADVERSARIAL_RUN = 'PYTHONPATH=src python3 -m unittest tests.test_gc_adversarial -v'
RUNTIME_MARKER = b'gc-adversarial-reproducible-runtime\n'

_NAMESPACE_RESULTS = {}
_NAMESPACE_PREFIX = {}


def _dead_identity(uid):
    return {'pid': 999999999, 'start_time': '0', 'boot_id': 'old-boot',
            'uid': uid}


def _tree_usage(root):
    """Allocated bytes and inodes without following links or crossing roots."""
    total = inodes = 0

    def visit(path):
        nonlocal total, inodes
        info = os.lstat(path)
        inodes += 1
        total += info.st_blocks * 512
        if stat.S_ISDIR(info.st_mode):
            with os.scandir(path) as entries:
                for entry in entries:
                    visit(entry.path)

    if not os.path.lexists(root):
        return 0, 0
    visit(os.fspath(root))
    return total, inodes


def _create_paused(source, state, ready, release, result):
    """Pause direct library creation inside its creation fence and Git init."""
    from codex_deepseek_team import workspace as workspace_module

    original = workspace_module.git
    paused = False

    def git(root, *args, **kwargs):
        nonlocal paused
        if not paused and args and args[0] == 'init':
            paused = True
            ready.set()
            if not release.wait(60):
                raise RuntimeError('test did not release paused Git init')
        return original(root, *args, **kwargs)

    workspace_module.git = git
    try:
        copy = workspace_module.create(Path(source), Path(state))
        result.put(('ok', copy.id, str(copy.path), copy.path.stat().st_ino))
    except BaseException as error:
        result.put(('error', type(error).__name__, str(error)))


def _collect_child(state, apply, result):
    from codex_deepseek_team import workspace_gc

    try:
        result.put(('ok', workspace_gc.collect(Path(state), apply=apply)))
    except BaseException as error:
        result.put(('error', type(error).__name__, str(error)))


def _operation_child(operation, state, copy_id, ready, release, result):
    """Hold a real owner lock while testing GC's operation boundary."""
    from codex_deepseek_team import workspace as workspace_module
    from codex_deepseek_team import workspace_lifecycle as lifecycle

    copy = workspace_module.load(Path(state), copy_id)

    def stale_owner():
        lease = lifecycle.read(copy)
        lease['owner'] = _dead_identity(os.geteuid())
        lifecycle.write(copy, lease)

    try:
        if operation == 'import':
            original = workspace_module._import_file
            paused_once = False

            def paused(*args, **kwargs):
                nonlocal paused_once
                if not paused_once:
                    paused_once = True
                    stale_owner()
                    ready.set()
                    if not release.wait(60):
                        raise RuntimeError('test did not release import')
                return original(*args, **kwargs)

            workspace_module._import_file = paused
            imported = workspace_module.import_paths(copy, ['app.txt'])
            result.put(('ok', imported, (copy.path / 'app.txt').read_text()))
        elif operation == 'review':
            with copy.lock(recover=True):
                stale_owner()
                ready.set()
                if not release.wait(60):
                    raise RuntimeError('test did not release review')
                changed, ignored = copy.changes()
            result.put(('ok', changed, ignored))
        else:
            raise ValueError('unknown operation ' + operation)
    except BaseException as error:
        result.put(('error', type(error).__name__, str(error)))


def _prepare_child(state, copy_id, ready_path, release_path, result):
    """Run real prepare while its command holds the owner lock and reports readiness."""
    from codex_deepseek_team import workspace as workspace_module

    package = str(Path(__file__).resolve().parents[1] / 'src')
    script = '''
import os, sys
package, state, identifier, ready_path, release_path = sys.argv[1:6]
sys.path.insert(0, package)
from codex_deepseek_team import workspace, workspace_lifecycle as lifecycle
copy = workspace.load(state, identifier)
lease = lifecycle.read(copy)
lease["owner"] = {"pid": 999999999, "start_time": "0",
                  "boot_id": "old-boot", "uid": os.geteuid()}
lifecycle.write(copy, lease)
with open(ready_path, "wb", buffering=0) as stream:
    stream.write(b"1")
with open(release_path, "rb", buffering=0) as stream:
    stream.read(1)
'''
    copy = workspace_module.load(Path(state), copy_id)
    try:
        code = workspace_module.prepare(
            copy, [sys.executable, '-c', script, package, str(state), copy_id,
                   str(ready_path), str(release_path)])
        result.put(('ok', code, (copy.directory / 'preparation-home').is_dir()))
    except BaseException as error:
        result.put(('error', type(error).__name__, str(error)))


def _namespace_key(kind):
    return kind


def _namespace_capability(kind):
    if kind in _NAMESPACE_RESULTS:
        return _NAMESPACE_RESULTS[kind]
    from codex_deepseek_team import sandbox
    try:
        backend = sandbox.probe_backend()
    except sandbox.SandboxError as error:
        result = (False, error.message)
        _NAMESPACE_RESULTS[kind] = result
        return result
    _NAMESPACE_PREFIX[kind] = backend.prefix
    common = [*backend.prefix, '--die-with-parent', '--new-session', '--unshare-user',
              '--unshare-pid', '--unshare-ipc', '--unshare-uts', '--unshare-net']
    roots = [path for path in ('/usr', '/bin', '/lib', '/lib64') if Path(path).exists()]
    for path in roots:
        common += ['--ro-bind', path, path]
    env = {'PATH': '/usr/bin:/bin', 'HOME': '/tmp', 'LC_ALL': 'C',
           'PYTHONDONTWRITEBYTECODE': '1'}
    if kind == 'capture':
        capture_uid = os.geteuid() or 65534
        capture_gid = os.getegid() if os.geteuid() else 65534
        probe = common + ['--uid', str(capture_uid), '--gid', str(capture_gid), '--disable-userns',
                          '--cap-drop', 'ALL', '--proc', '/proc', '--dev', '/dev',
                          '--tmpfs', '/tmp', '--', '/usr/bin/python3', '-c',
                          'import os; print(os.geteuid())']
    elif kind == 'mount':
        script = (
            'import ctypes, os\n'
            'os.makedirs("/tmp/source")\n'
            'os.makedirs("/tmp/target")\n'
            'open("/tmp/source/canary", "w").write("ok")\n'
            'libc = ctypes.CDLL(None, use_errno=True)\n'
            'rc = libc.mount(b"/tmp/source", b"/tmp/target", None, 4096, None)\n'
            'if rc: raise SystemExit("mount errno %d" % ctypes.get_errno())\n'
            'assert open("/tmp/target/canary").read() == "ok"\n'
            'assert os.stat("/tmp/source").st_dev == os.stat("/tmp/target").st_dev\n'
        )
        probe = common + ['--cap-add', 'CAP_SYS_ADMIN', '--proc', '/proc',
                          '--dev', '/dev', '--tmpfs', '/tmp', '--', '/usr/bin/python3',
                          '-c', script]
    else:
        raise AssertionError('unknown namespace probe')
    try:
        checked = subprocess.run(probe, env=env, capture_output=True, text=True,
                                 timeout=30, check=False)
        if checked.returncode == 0:
            if kind != 'capture' or checked.stdout.strip() == str(capture_uid):
                result = (True, 'available')
            else:
                result = (False, 'capture probe ran as uid ' + checked.stdout.strip())
        else:
            detail = (checked.stderr or checked.stdout).strip().splitlines()
            result = (False, detail[-1] if detail else 'probe exit ' + str(checked.returncode))
    except (OSError, subprocess.SubprocessError) as error:
        result = (False, type(error).__name__ + ': ' + str(error))
    _NAMESPACE_RESULTS[kind] = result
    return result


def _skip_without_namespace(case, kind):
    available, reason = _namespace_capability(_namespace_key(kind))
    if not available:
        case.skipTest(
            f'{kind} namespace unavailable in this worker boundary: {reason}. '
            f'Coordinator rerun required: {ADVERSARIAL_RUN}')


class GCAdversarialFixture(lifecycle_fixture.LifecycleFixture):
    def setUp(self):
        super().setUp()
        self._cwd = os.getcwd()
        self.addCleanup(self._restore_cwd)
        self.context = multiprocessing.get_context('fork')

    def _restore_cwd(self):
        try:
            os.chdir(self._cwd)
        except OSError:
            pass

    def child_env(self, **extra):
        env = {
            'PATH': os.environ.get('PATH', '/usr/bin:/bin'),
            'HOME': str(self.root / 'home'),
            'CODEX_HOME': str(self.root / 'codex'),
            'CLAUDE_CONFIG_DIR': str(self.root / 'claude'),
            'XDG_CONFIG_HOME': str(self.root / 'config'),
            'DEEPSEEK_TEAM_STATE_DIR': str(self.state),
            'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'src'),
            'PYTHONDONTWRITEBYTECODE': '1',
            'LC_ALL': 'C.UTF-8',
        }
        env.update(extra)
        return env

    def completed(self, content='accepted fix\n'):
        copy = self.copy()
        (copy.path / 'app.txt').write_text(content)
        with copy.lock():
            copy.begin('execution')
            copy.finish('succeeded', exit_code=0)
        return copy

    def start_holder(self, path):
        script = ('import os, sys\n'
                  'fd = os.open(sys.argv[1], os.O_RDONLY)\n'
                  'print("ready", flush=True)\n'
                  'sys.stdin.readline()\n'
                  'os.close(fd)\n')
        process = subprocess.Popen(
            [sys.executable, '-c', script, str(path)], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env=self.child_env())

        def stop():
            if process.poll() is None:
                try:
                    process.stdin.write('release\n')
                    process.stdin.flush()
                    process.wait(timeout=10)
                except (BrokenPipeError, subprocess.TimeoutExpired):
                    process.kill()
                    process.wait(timeout=10)
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None and not stream.closed:
                    stream.close()

        self.addCleanup(stop)
        line = process.stdout.readline().strip()
        if line != 'ready':
            error = process.stderr.read()
            self.fail('holder did not open execution state: ' + error)
        return process

    def pending_cleanup(self, copy):
        gc = self.api('workspace_gc')
        lifecycle = self.api()
        record_before = (copy.directory / 'record.json').read_bytes()
        lease = lifecycle.read(copy)
        work_fd = os.open(copy.path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            usage = gc._tree_usage(work_fd)
            info = os.fstat(work_fd)
        finally:
            os.close(work_fd)
        name = uuid.uuid4().hex
        lease['phase'] = 'finalized'
        lease['final_reason'] = 'adversarial interruption'
        lease['events'].append({'event': 'finalized', 'at': 1.0,
                                'reason': lease['final_reason']})
        lease['cleanup'] = {
            'name': name,
            'device': info.st_dev,
            'inode': info.st_ino,
            'allocated_bytes': usage[0],
            'inodes': usage[1],
            'extras': [],
            'state': 'pending',
        }
        lifecycle.write(copy, lease)
        quarantine = self.state / 'quarantine'
        quarantine.mkdir(mode=0o700)
        destination = quarantine / name
        os.rename(copy.path, destination)
        return lease, destination, record_before, (info.st_dev, info.st_ino)

    def assert_clean_state(self, count):
        workspaces = self.state / 'workspaces'
        entries = [entry for entry in workspaces.iterdir() if entry.is_dir()]
        self.assertEqual(len(entries), count)
        for entry in entries:
            self.assertFalse((entry / 'work').exists() or (entry / 'work').is_symlink())
        for path in self.state.rglob('*'):
            self.assertNotEqual(path.name, 'work')
            self.assertNotEqual(path.name, 'preparation-home')
            self.assertIsNone(re.match(r'(?:runtime|session)-[A-Za-z0-9_-]+$', path.name),
                              'temporary environment tree survived: ' + str(path))
            self.assertFalse(path.name.endswith(('.tar', '.tar.gz', '.tgz', '.zip', '.7z')),
                             'archive survived collection: ' + str(path))
        quarantine = self.state / 'quarantine'
        if quarantine.exists():
            self.assertEqual(list(quarantine.iterdir()), [])


class GCConcurrencyTests(GCAdversarialFixture):
    def test_create_during_early_git_init_keeps_fence_and_live_lease(self):
        ready, release = self.context.Event(), self.context.Event()
        created = self.context.Queue()
        creator = self.context.Process(
            target=_create_paused,
            args=(self.source, self.state, ready, release, created))
        creator.start()
        self.addCleanup(lambda: creator.terminate() if creator.is_alive() else None)
        self.assertTrue(ready.wait(30), 'creation did not reach the Git-init pause')
        self.assertTrue(creator.is_alive())

        lock_fd = os.open(self.state / 'lifecycle.lock',
                          os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            with self.assertRaises(BlockingIOError):
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(lock_fd)

        entries = list((self.state / 'workspaces').iterdir())
        self.assertEqual(len(entries), 1)
        identifier = entries[0].name
        collected = self.context.Queue()
        collector = self.context.Process(
            target=_collect_child, args=(self.state, True, collected))
        collector.start()
        try:
            kind, *payload = collected.get(timeout=30)
            collector.join(timeout=30)
            self.assertFalse(collector.is_alive(), 'GC did not finish while create was paused')
        finally:
            if collector.is_alive():
                collector.terminate()
                collector.join(timeout=10)
        self.assertEqual(kind, 'ok', payload)
        report = payload[0]
        row = next(item for item in report['workspaces'] if item['id'] == identifier)
        self.assertEqual(row['action'], 'kept')
        self.assertIn('active logical job lease', row['reason'])
        self.assertFalse((self.state / 'quarantine').exists())
        self.assertTrue((entries[0] / 'work').is_dir())

        release.set()
        result = created.get(timeout=60)
        creator.join(timeout=30)
        self.assertFalse(creator.is_alive())
        self.assertEqual(result[0], 'ok', result)
        self.assertEqual(result[1], identifier)
        self.assertTrue(Path(result[2]).is_dir())
        self.assertEqual(Path(result[2]).stat().st_ino, result[3])

    def _assert_owner_lock_defers_gc(self, gc, state, copy):
        report = gc.collect(state, apply=True)
        row = next(item for item in report['workspaces'] if item['id'] == copy.id)
        self.assertEqual(row['action'], 'kept')
        self.assertEqual(row['reason'], 'active workspace operation')
        self.assertTrue(copy.path.is_dir())
        self.assertFalse((state / 'quarantine').exists())
        self.assertFalse((copy.directory / 'changes.patch').exists())

    def test_import_and_review_owner_locks_defer_gc(self):
        gc = self.api('workspace_gc')
        for operation in ('import', 'review'):
            with self.subTest(operation=operation):
                state = self.root / ('state-' + operation)
                copy = workspace.create(self.source, state)
                ready, release = self.context.Event(), self.context.Event()
                result_queue = self.context.Queue()
                child = self.context.Process(
                    target=_operation_child,
                    args=(operation, state, copy.id, ready, release, result_queue))
                child.start()
                try:
                    self.assertTrue(ready.wait(30), operation + ' did not acquire its owner lock')
                    self.assertTrue(child.is_alive())
                    self._assert_owner_lock_defers_gc(gc, state, copy)
                finally:
                    release.set()
                    child.join(timeout=60)
                    if child.is_alive():
                        child.terminate()
                        child.join(timeout=10)
                self.assertEqual(child.exitcode, 0)
                result = result_queue.get(timeout=10)
                self.assertEqual(result[0], 'ok', result)

    def test_prepare_owner_lock_defers_gc(self):
        gc = self.api('workspace_gc')
        state = self.root / 'state-prepare'
        copy = workspace.create(self.source, state)
        ready_path = self.root / 'prepare.ready'
        release_path = self.root / 'prepare.release'
        os.mkfifo(ready_path, 0o600)
        os.mkfifo(release_path, 0o600)
        ready_fd = os.open(ready_path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
        result_queue = self.context.Queue()
        child = self.context.Process(
            target=_prepare_child,
            args=(state, copy.id, ready_path, release_path, result_queue))
        child.start()
        try:
            readable = select.select([ready_fd], [], [], 30)[0]
            self.assertTrue(readable, 'prepare command did not report readiness')
            self.assertEqual(os.read(ready_fd, 1), b'1')
            self._assert_owner_lock_defers_gc(gc, state, copy)
        finally:
            release_fd = os.open(release_path, os.O_RDWR | os.O_NONBLOCK | os.O_CLOEXEC)
            try:
                os.write(release_fd, b'1')
            finally:
                os.close(release_fd)
                os.close(ready_fd)
            child.join(timeout=60)
            if child.is_alive():
                child.terminate()
                child.join(timeout=10)
        self.assertEqual(child.exitcode, 0)
        result = result_queue.get(timeout=10)
        self.assertEqual(result[0], 'ok', result)
        self.assertEqual(result[1], 0)
        self.assertTrue(result[2])


class GCFilesystemAttackTests(GCAdversarialFixture):
    def test_real_namespace_nested_mount_is_refused_without_host_mutation(self):
        _skip_without_namespace(self, 'mount')
        prefix = _NAMESPACE_PREFIX.get('mount')
        self.assertIsNotNone(prefix, 'mount probe succeeded without recording its backend')
        gc = self.api('workspace_gc')
        copy = self.completed()
        inode = copy.metadata['work_inode']
        patch_before = (copy.directory / 'changes.patch').read_bytes()
        canary = self.root / 'outside-canary'
        canary.mkdir(mode=0o700)
        marker = canary / 'must-survive.txt'
        marker.write_text('outside payload\n')
        package = Path(__file__).resolve().parents[1] / 'src'
        roots = [path for path in ('/usr', '/bin', '/lib', '/lib64') if Path(path).exists()]
        args = [*prefix, '--die-with-parent', '--new-session', '--unshare-user',
                '--unshare-pid', '--unshare-ipc', '--unshare-uts', '--unshare-net',
                '--cap-add', 'CAP_SYS_ADMIN']
        for path in roots:
            args += ['--ro-bind', path, path]
        args += ['--proc', '/proc', '--dev', '/dev', '--tmpfs', '/tmp',
                 '--bind', str(self.state), '/sandbox-state',
                 '--ro-bind', str(package), '/sandbox-package',
                 '--bind', str(canary), '/canary',
                 '--chdir', '/sandbox-package', '--', '/usr/bin/python3', '-c', '''
import ctypes, json, os, sys
from codex_deepseek_team import workspace, workspace_gc, workspace_lifecycle
state, identifier, canary = sys.argv[1:4]
copy = workspace.load(state, identifier)
# This private fixture intentionally moves the job into this PID namespace.
# Keep the owner observable so the test reaches the nested-mount boundary.
lease = workspace_lifecycle.read(copy)
lease['owner'] = workspace_lifecycle.process_identity(os.getpid())
workspace_lifecycle.write(copy, lease)
nested = copy.path / "nested"
nested.mkdir(mode=0o700)
libc = ctypes.CDLL(None, use_errno=True)
if libc.mount(canary.encode(), os.fsencode(nested), None, 4096, None):
    print(json.dumps({"mount_errno": ctypes.get_errno()}))
    raise SystemExit(2)
assert os.stat(nested).st_dev == os.stat(copy.path).st_dev
try:
    workspace_gc.finalize(copy)
except workspace.WorkspaceError as error:
    print(json.dumps({"code": error.code, "message": error.message}))
else:
    print(json.dumps({"unexpected": "finalized"}))
    raise SystemExit(3)
''', '/sandbox-state', copy.id, '/canary']
        checked = subprocess.run(args, env=self.child_env(
                                 PYTHONPATH='/sandbox-package',
                                 DEEPSEEK_TEAM_STATE_DIR='/sandbox-state'),
                                 capture_output=True, text=True, timeout=120, check=False)
        self.assertEqual(checked.returncode, 0,
                         checked.stderr + checked.stdout)
        payload = json.loads(checked.stdout.strip().splitlines()[-1])
        self.assertIn('code', payload, payload)
        self.assertIn('nested mount', payload['message'])
        self.assertEqual(copy.path.stat().st_ino, inode)
        self.assertEqual(marker.read_text(), 'outside payload\n')
        self.assertEqual((copy.directory / 'changes.patch').read_bytes(), patch_before)
        self.assertFalse((copy.directory / 'result.json').exists())
        self.assertEqual(self.api().read(copy)['phase'], 'review')
        after_mounts = Path('/proc/self/mountinfo').read_text()
        self.assertNotIn(str(copy.path), after_mounts)
        self.assertNotIn('/canary', after_mounts)

    def test_state_root_and_workspaces_ancestor_symlink_swaps_preserve_canary(self):
        gc = self.api('workspace_gc')
        copy = self.completed()
        outside = self.root / 'outside-root'
        outside_workspaces = outside / 'workspaces'
        outside_workspaces.mkdir(parents=True, mode=0o700)
        os.chmod(outside, 0o700)
        trap = outside_workspaces / copy.id / 'work'
        trap.mkdir(parents=True, mode=0o700)
        os.chmod(outside_workspaces, 0o700)
        os.chmod(outside_workspaces / copy.id, 0o700)
        canary = trap / 'outside-canary'
        canary.write_text('preserve me\n')

        actual_state = self.root / 'state-ordinary'
        self.state.rename(actual_state)
        self.state.symlink_to(outside, target_is_directory=True)
        try:
            with self.assertRaises(workspace.WorkspaceError):
                gc.finalize(copy)
            self.assertEqual(canary.read_text(), 'preserve me\n')
        finally:
            self.state.unlink()
            actual_state.rename(self.state)

        ordinary_workspaces = self.state / 'workspaces'
        replaced_workspaces = self.state / 'workspaces-ordinary'
        ordinary_workspaces.rename(replaced_workspaces)
        ordinary_workspaces.symlink_to(outside_workspaces, target_is_directory=True)
        try:
            with self.assertRaises(workspace.WorkspaceError):
                gc.finalize(copy)
            self.assertEqual(canary.read_text(), 'preserve me\n')
        finally:
            ordinary_workspaces.unlink()
            replaced_workspaces.rename(ordinary_workspaces)

    def test_foreign_owner_refuses_collection_before_capture(self):
        gc = self.api('workspace_gc')
        copy = self.completed()
        patch_before = (copy.directory / 'changes.patch').read_bytes()
        foreign = copy.path / 'foreign.bin'
        foreign.write_text('foreign owner\n')
        target = 65534 if os.geteuid() != 65534 else 65533
        try:
            os.chown(foreign, target, -1)
        except OSError:
            self.skipTest('actual foreign-UID chown is unavailable in this environment; '
                          'coordinator rerun required: ' + ADVERSARIAL_RUN)
        self.assertNotEqual(foreign.lstat().st_uid, os.geteuid())
        with self.assertRaises(workspace.WorkspaceError) as caught:
            gc.finalize(copy)
        self.assertEqual(caught.exception.code, 73)
        self.assertTrue(foreign.is_file())
        self.assertTrue((copy.directory / 'work').is_dir())
        self.assertEqual((copy.directory / 'changes.patch').read_bytes(), patch_before)

    def test_malformed_record_lease_and_cleanup_preserve_execution_state(self):
        gc = self.api('workspace_gc')
        bad_record = self.copy()
        (bad_record.directory / 'record.json').write_bytes(b'{not-json')
        report = gc.collect(self.state, apply=True)
        row = next(item for item in report['workspaces'] if item['id'] == bad_record.id)
        self.assertEqual(row['action'], 'kept')
        self.assertTrue((bad_record.directory / 'work').is_dir())

        bad_lease = self.copy()
        outside = self.root / 'lease-canary'
        outside.write_text('lease canary\n')
        (bad_lease.directory / 'lease.json').unlink()
        (bad_lease.directory / 'lease.json').symlink_to(outside)
        report = gc.collect(self.state, apply=True)
        row = next(item for item in report['workspaces'] if item['id'] == bad_lease.id)
        self.assertEqual(row['action'], 'kept')
        self.assertTrue((bad_lease.directory / 'work').is_dir())
        self.assertEqual(outside.read_text(), 'lease canary\n')

        pending = self.completed()
        lease, destination, record_before, _identity = self.pending_cleanup(pending)
        lease['cleanup']['name'] = '../escape'
        self.api().write(pending, lease)
        with self.assertRaises(workspace.WorkspaceError):
            gc.finalize(pending)
        self.assertTrue(destination.is_dir())
        self.assertFalse((self.state / 'escape').exists())
        self.assertEqual((pending.directory / 'record.json').read_bytes(), record_before)

    def test_deletion_does_not_follow_symlinks_outside_canary(self):
        gc = self.api('workspace_gc')
        outside = self.root / 'outside-files'
        outside.mkdir(mode=0o700)
        (outside / 'canary.txt').write_text('untouched\n')
        (outside / 'nested').mkdir(mode=0o700)
        (outside / 'nested' / 'payload.bin').write_bytes(b'outside\x00data')
        self.state.mkdir(mode=0o700)
        quarantine = self.state / 'quarantine'
        quarantine.mkdir(mode=0o700)
        tree = quarantine / uuid.uuid4().hex
        tree.mkdir(mode=0o700)
        (tree / 'ordinary.txt').write_text('delete me\n')
        (tree / 'file-link').symlink_to(outside / 'canary.txt')
        (tree / 'directory-link').symlink_to(outside / 'nested', target_is_directory=True)
        info = tree.stat()
        qfd = os.open(quarantine, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            gc._remove_tree(qfd, tree.name, (info.st_dev, info.st_ino))
        finally:
            os.close(qfd)
        self.assertFalse(tree.exists() or tree.is_symlink())
        self.assertEqual((outside / 'canary.txt').read_text(), 'untouched\n')
        self.assertEqual((outside / 'nested' / 'payload.bin').read_bytes(), b'outside\x00data')


class GCCleanupRecoveryTests(GCAdversarialFixture):
    def test_post_rename_open_activity_restores_the_same_inode(self):
        gc = self.api('workspace_gc')
        copy = self.completed('original inode content\n')
        lease, destination, record_before, identity = self.pending_cleanup(copy)
        holder = self.start_holder(destination / 'app.txt')
        try:
            with self.assertRaises(workspace.WorkspaceError) as caught:
                gc.finalize(copy)
            self.assertEqual(caught.exception.code, 75)
            self.assertTrue(copy.path.is_dir())
            self.assertFalse(destination.exists())
            self.assertEqual((copy.path.stat().st_dev, copy.path.stat().st_ino), identity)
            self.assertEqual((copy.path / 'app.txt').read_text(), 'original inode content\n')
            restored = self.api().read(copy)
            self.assertTrue(restored['cleanup']['restored'])
            self.assertEqual(restored['phase'], 'finalized')
            self.assertEqual((copy.directory / 'record.json').read_bytes(), record_before)
        finally:
            holder.stdin.write('release\n')
            holder.stdin.flush()
            holder.wait(timeout=10)

    def test_restore_never_overwrites_an_existing_destination(self):
        gc = self.api('workspace_gc')
        copy = self.completed('quarantined original\n')
        lease, destination, _record_before, original_identity = self.pending_cleanup(copy)
        copy.path.mkdir(mode=0o700)
        destination_file = copy.path / 'new-destination.txt'
        destination_file.write_text('new destination\n')
        destination_identity = (copy.path.stat().st_dev, copy.path.stat().st_ino)
        holder = self.start_holder(destination / 'app.txt')
        quarantine = self.state / 'quarantine'
        qfd = os.open(quarantine, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            with self.assertRaises(workspace.WorkspaceError) as caught:
                gc._finish_cleanup(copy, lease, qfd)
            self.assertEqual(caught.exception.code, 75)
            self.assertEqual((copy.path.stat().st_dev, copy.path.stat().st_ino),
                             destination_identity)
            self.assertEqual(destination_file.read_text(), 'new destination\n')
            self.assertEqual((destination.stat().st_dev, destination.stat().st_ino),
                             original_identity)
            cleanup = self.api().read(copy)['cleanup']
            self.assertNotEqual(cleanup.get('restored'), True)
            self.assertEqual(cleanup['state'], 'pending')
            with self.assertRaises(workspace.WorkspaceError):
                gc.finalize(copy)
            self.assertEqual(destination_file.read_text(), 'new destination\n')
            self.assertEqual((destination.stat().st_dev, destination.stat().st_ino),
                             original_identity)
        finally:
            os.close(qfd)
            holder.stdin.write('release\n')
            holder.stdin.flush()
            holder.wait(timeout=10)

    def test_interrupted_quarantine_rmtree_retries_idempotently(self):
        gc = self.api('workspace_gc')
        lifecycle = self.api()
        copy = self.completed('interrupted cleanup\n')
        lease_before = lifecycle.read(copy)
        lease, destination, record_before, _identity = self.pending_cleanup(copy)
        evidence_before = {
            'work_inode': copy.metadata['work_inode'],
            'status': copy.metadata['status'],
            'exit_code': copy.metadata['exit_code'],
            'attempts': [dict(row) for row in lease['attempts']],
        }
        real_remove = gc._remove_tree
        calls = []

        def interrupted(parent, name, expected):
            if not calls:
                fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                             dir_fd=parent)
                try:
                    for child in os.listdir(fd):
                        child_info = os.stat(child, dir_fd=fd, follow_symlinks=False)
                        if stat.S_ISREG(child_info.st_mode):
                            os.unlink(child, dir_fd=fd)
                            break
                finally:
                    os.close(fd)
                calls.append(name)
                raise OSError('injected interruption after quarantine deletion began')
            return real_remove(parent, name, expected)

        with mock.patch.object(gc, '_remove_tree', side_effect=interrupted):
            with self.assertRaises(OSError):
                gc.finalize(copy)
        self.assertTrue(destination.is_dir())
        self.assertFalse((copy.directory / 'work').exists())
        self.assertEqual((copy.directory / 'record.json').read_bytes(), record_before)
        interrupted_lease = lifecycle.read(copy)
        self.assertEqual(interrupted_lease['cleanup']['state'], 'pending')
        self.assertEqual(interrupted_lease['work_inode'], evidence_before['work_inode'])

        report = gc.finalize(copy)
        self.assertEqual(report['action'], 'cleaned')
        self.assertFalse(destination.exists())
        self.assertFalse((copy.directory / 'work').exists())
        self.assertEqual((copy.directory / 'record.json').read_bytes(), record_before)
        final_lease = lifecycle.read(copy)
        self.assertEqual(final_lease['cleanup']['state'], 'cleaned')
        self.assertEqual(final_lease['work_inode'], evidence_before['work_inode'])
        self.assertEqual(final_lease['events'][-1]['event'], 'cleaned')
        self.assertEqual(sum(event.get('event') == 'cleaned' for event in final_lease['events']), 1)
        self.assertEqual(final_lease['attempts'][0]['status'], evidence_before['status'])
        self.assertEqual(final_lease['attempts'][0]['exit_code'], evidence_before['exit_code'])
        self.assertEqual(gc.finalize(copy)['action'], 'already-cleaned')
        self.assertEqual(lifecycle.read(copy)['events'], final_lease['events'])
        self.assertEqual(lease_before['job_id'], final_lease['job_id'])


class GCLeaseIdentityTests(GCAdversarialFixture):
    def test_live_review_and_retry_leases_survive_gc_without_worker_pid(self):
        gc = self.api('workspace_gc')
        lifecycle = self.api()
        copies = []
        for phase in ('review', 'retry'):
            copy = self.completed(phase + ' content\n')
            if phase == 'retry':
                lifecycle.rework(copy)
            current = lifecycle.read(copy)
            self.assertEqual(current['phase'], phase)
            self.assertNotIn('worker_pid', current)
            self.assertTrue(lifecycle.live(current))
            copy.metadata.update(status='running', coord_task='legacy-active-parent',
                                 planned=True, worker_pid=999999999)
            copy.save()
            copies.append(copy)
        report = gc.collect(self.state, apply=True)
        for copy in copies:
            row = next(item for item in report['workspaces'] if item['id'] == copy.id)
            self.assertEqual(row['action'], 'kept')
            self.assertIn('active logical job lease', row['reason'])
            self.assertTrue(copy.path.is_dir())
        self.assertFalse((self.state / 'quarantine').exists())

    def test_pid_and_boot_reuse_does_not_create_a_live_lease(self):
        gc = self.api('workspace_gc')
        lifecycle = self.api()
        copy = self.completed('stale owner\n')
        lease = lifecycle.read(copy)
        current = lifecycle.owner_identity()
        for mutation in ({'start_time': '0'}, {'boot_id': 'previous-boot'},
                         {'pid': 1}, {'pid': 999999999}):
            candidate = dict(current, **mutation)
            lease['owner'] = candidate
            lifecycle.write(copy, lease)
            self.assertFalse(lifecycle.live(lease))
            report = gc.collect(self.state, apply=False)
            row = next(item for item in report['workspaces'] if item['id'] == copy.id)
            self.assertEqual(row['action'], 'would-clean')
        self.assertTrue(copy.path.is_dir())

    def test_legacy_planned_history_is_not_a_lease(self):
        gc = self.api('workspace_gc')
        copy = self.completed('legacy history\n')
        (copy.directory / 'lease.json').unlink()
        copy.metadata.pop('job_id', None)
        copy.metadata.pop('attempt_id', None)
        copy.metadata.update(status='running', coord_task='old-parent',
                             planned=True, active=True, worker_pid=os.getpid())
        copy.save()
        self.assertIsNone(self.api().read(copy))
        report = gc.collect(self.state, apply=False)
        row = next(item for item in report['workspaces'] if item['id'] == copy.id)
        self.assertEqual(row['action'], 'would-clean')
        self.assertEqual(row['reason'], 'legacy unused copy')
        self.assertTrue(copy.path.is_dir())


class GCNamespaceAcceptanceTests(GCAdversarialFixture):
    def test_unprivileged_capture_publishes_patch_and_removes_environment_trees(self):
        _skip_without_namespace(self, 'capture')
        gc = self.api('workspace_gc')
        copy = self.completed('accepted in namespace\n')
        (copy.path / 'data.bin').write_bytes(b'\x00\xfe\x81namespace\x00')
        with copy.lock():
            copy.begin('execution')
            copy.finish('succeeded', exit_code=0)
        record_before = (copy.directory / 'record.json').read_bytes()
        for name in ('preparation-home', 'runtime-fixture'):
            directory = copy.directory / name
            directory.mkdir(mode=0o700)
            (directory / 'reproducible.bin').write_bytes(RUNTIME_MARKER)
        inode = copy.metadata['work_inode']
        report = gc.finalize(copy)
        self.assertEqual(report['action'], 'cleaned')
        self.assertFalse(copy.path.exists())
        self.assertFalse((copy.directory / 'preparation-home').exists())
        self.assertFalse((copy.directory / 'runtime-fixture').exists())
        self.assertEqual((copy.directory / 'record.json').read_bytes(), record_before)
        self.assertEqual(json.loads(record_before)['work_inode'], inode)
        result = json.loads((copy.directory / 'result.json').read_text())
        patch = (copy.directory / 'changes.patch').read_bytes()
        self.assertEqual(result['patch']['bytes'], len(patch))
        self.assertEqual(result['patch']['sha256'], hashlib.sha256(patch).hexdigest())
        inspected = gc.inspect_workspace(self.state, copy.id)
        self.assertEqual(inspected['lease']['phase'], 'finalized')
        self.assertFalse(inspected['execution_present'])
        subprocess.run(['git', '-C', str(self.source), 'apply', '--binary',
                        str(copy.directory / 'changes.patch')], check=True)
        self.assertEqual((self.source / 'data.bin').read_bytes(), b'\x00\xfe\x81namespace\x00')
        quarantine = self.state / 'quarantine'
        self.assertFalse(quarantine.exists() and any(quarantine.iterdir()))


class GCStressAcceptanceTests(GCAdversarialFixture):
    def test_100_completed_jobs_final_acceptance_loop_is_bounded(self):
        raw = os.environ.get(STRESS_ENV)
        if raw is None:
            self.skipTest('explicit stress run not requested; run: ' + STRESS_RUN)
        try:
            count = int(raw)
        except ValueError:
            self.fail(STRESS_ENV + ' must be an integer >= 100')
        if count < 100:
            self.fail(STRESS_ENV + ' must be >= 100 for final acceptance')
        _skip_without_namespace(self, 'capture')

        gc = self.api('workspace_gc')
        before_bytes, before_inodes = _tree_usage(self.state)
        completed = []
        for number in range(count):
            copy = self.copy()
            (copy.path / 'app.txt').write_text(f'accepted job {number}\n')
            (copy.path / '.gitignore').write_text('ignored/\n')
            ignored = copy.path / 'ignored'
            ignored.mkdir(mode=0o700)
            (ignored / 'payload.bin').write_bytes(RUNTIME_MARKER)
            for name in ('preparation-home', 'runtime-fixture'):
                directory = copy.directory / name
                directory.mkdir(mode=0o700)
                (directory / 'reproducible.bin').write_bytes(RUNTIME_MARKER)
            with copy.lock():
                copy.begin('execution')
                copy.finish('succeeded', exit_code=0)
            report = gc.finalize(copy)
            self.assertEqual(report['action'], 'cleaned')
            self.assertFalse(copy.path.exists())
            directory_bytes, directory_inodes = _tree_usage(copy.directory)
            self.assertLess(directory_bytes, 128 * 1024,
                            f'workspace {copy.id} retained {directory_bytes} allocated bytes')
            self.assertLessEqual(directory_inodes, 12,
                                 f'workspace {copy.id} retained {directory_inodes} inodes')
            completed.append((copy, directory_bytes, directory_inodes))

        after_bytes, after_inodes = _tree_usage(self.state)
        print(f'GC adversarial stress measured: jobs={count} '
              f'allocated_bytes={before_bytes}->{after_bytes} '
              f'inodes={before_inodes}->{after_inodes}', flush=True)
        self.assertLess(after_bytes, 16 * 1024 * 1024)
        self.assertLessEqual(after_inodes, before_inodes + count * 16)
        self.assert_clean_state(count)

        for copy, _directory_bytes, _directory_inodes in completed:
            report = gc.inspect_workspace(self.state, copy.id)
            self.assertEqual(report['lease']['phase'], 'finalized')
            self.assertFalse(report['execution_present'])
            patch = (copy.directory / 'changes.patch').read_bytes()
            result = json.loads((copy.directory / 'result.json').read_text())
            self.assertEqual(result['patch']['sha256'], hashlib.sha256(patch).hexdigest())
            self.assertLessEqual(len(patch), 64 * 1024)
            subprocess.run(['git', '-C', str(self.source), 'apply', '--check',
                            str(copy.directory / 'changes.patch')], check=True)

        for path in self.state.rglob('*'):
            if path.is_file() and not path.is_symlink():
                self.assertNotIn(RUNTIME_MARKER, path.read_bytes(),
                                 'ignored runtime payload was archived in ' + str(path))


if __name__ == '__main__':
    unittest.main()
