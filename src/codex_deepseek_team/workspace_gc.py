"""Provider-free finalization and recoverable collection of owned execution trees.

Historical records remain beside compact result artifacts. The quarantine is a
short deletion stage on the same mount, never a checkout archive. No Git process
runs as root: result capture uses a sparse, private-network user namespace.
"""
from __future__ import annotations

from contextlib import contextmanager, ExitStack, nullcontext
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import time
import uuid

from . import state_storage, workspace_lifecycle as lifecycle
from .atomic_rename import move

MAX_PATCH_BYTES = 64 * 1024 * 1024


def _identity(info):
    return info.st_dev, info.st_ino


def _mount_id(fd):
    try:
        for line in Path(f'/proc/self/fdinfo/{fd}').read_text().splitlines():
            if line.startswith('mnt_id:'):
                return int(line.split(':', 1)[1])
    except (OSError, ValueError):
        pass
    raise lifecycle.error('Cannot verify execution mount identity; copy preserved.')


def _open_dir(parent, name, expected=None):
    info = os.stat(name, dir_fd=parent, follow_symlinks=False)
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
            or (expected is not None and _identity(info) != tuple(expected))):
        raise lifecycle.error('Execution directory inode/type/owner changed; copy preserved.', 73)
    fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                 dir_fd=parent)
    try:
        if _identity(os.fstat(fd)) != _identity(info) or _mount_id(fd) != _mount_id(parent):
            raise lifecycle.error('Replaced directory or nested mount; copy preserved.', 73)
        return fd
    except BaseException:
        os.close(fd)
        raise


def _tree_usage(fd, *, mount=None, seen=None, allow_ipc=False):
    mount = _mount_id(fd) if mount is None else mount
    seen = set() if seen is None else seen
    info = os.fstat(fd)
    if info.st_uid != os.geteuid() or _mount_id(fd) != mount:
        raise lifecycle.error('Foreign owner or nested mount in execution data.', 73)
    total, inodes = info.st_blocks * 512, 1
    seen.add(_identity(info))
    for name in os.listdir(fd):
        info = os.stat(name, dir_fd=fd, follow_symlinks=False)
        if info.st_uid != os.geteuid():
            raise lifecycle.error('Foreign owner in execution data; copy preserved.', 73)
        if stat.S_ISDIR(info.st_mode):
            child = _open_dir(fd, name, _identity(info))
            try:
                size, count = _tree_usage(child, mount=mount, seen=seen, allow_ipc=allow_ipc)
            finally:
                os.close(child)
            total += size
            inodes += count
        elif (stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)
              or (allow_ipc and (stat.S_ISSOCK(info.st_mode) or stat.S_ISFIFO(info.st_mode)))):
            child = os.open(name, os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
            try:
                if _identity(os.fstat(child)) != _identity(info) or _mount_id(child) != mount:
                    raise lifecycle.error('Replaced file or nested file mount; copy preserved.', 73)
            finally:
                os.close(child)
            if _identity(info) not in seen:
                total += info.st_blocks * 512
                inodes += 1
                seen.add(_identity(info))
        else:
            raise lifecycle.error('Special file in execution tree; stop its service before cleanup.', 75)
    return total, inodes


def _remove_tree(parent, name, expected):
    """Descriptor-relative deletion: no path traversal and no crossing mounts."""
    fd = _open_dir(parent, name, expected)
    try:
        for child_name in os.listdir(fd):
            info = os.stat(child_name, dir_fd=fd, follow_symlinks=False)
            if info.st_uid != os.geteuid():
                raise lifecycle.error('Execution entry changed owner during deletion.', 73)
            if stat.S_ISDIR(info.st_mode):
                _remove_tree(fd, child_name, _identity(info))
            elif (stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)
                  or stat.S_ISSOCK(info.st_mode) or stat.S_ISFIFO(info.st_mode)):
                # unlink never follows the entry; an outside link target is safe.
                current = os.stat(child_name, dir_fd=fd, follow_symlinks=False)
                if _identity(current) != _identity(info):
                    raise lifecycle.error('Execution entry changed during deletion.', 73)
                os.unlink(child_name, dir_fd=fd)
            else:
                raise lifecycle.error('Live/special execution entry prevents deletion.', 75)
        current = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if _identity(current) != tuple(expected):
            raise lifecycle.error('Quarantine inode changed during deletion.', 73)
        os.rmdir(name, dir_fd=parent)
        os.fsync(parent)
    finally:
        os.close(fd)


def _references(paths):
    """Find real same-user open state; never publish argv/maps/file content."""
    prefixes = tuple(str(path).rstrip('/') for path in paths)
    identifiers = {Path(path).parent.name for path in paths
                   if Path(path).name == 'work' and re.fullmatch('[0-9a-f]{32}', Path(path).parent.name)}

    def inside(value):
        value = value.removesuffix(' (deleted)')
        return any(value == root or value.startswith(root + '/') for root in prefixes)

    for proc in Path('/proc').iterdir():
        if not proc.name.isdigit():
            continue
        try:
            if proc.stat().st_uid != os.geteuid():
                continue
            identity = lifecycle.process_identity(int(proc.name)) or {'pid': int(proc.name)}
            try:
                argv = (proc / 'cmdline').read_bytes().split(b'\0')
                for arg in argv:
                    value = os.fsdecode(arg)
                    candidate = value.split('=', 1)[-1]
                    if (inside(value) or inside(candidate)
                            or (int(proc.name) != os.getpid() and candidate in identifiers)):
                        return identity
            except (PermissionError, FileNotFoundError, ProcessLookupError):
                pass
            for link in ('cwd', 'exe', 'root'):
                try:
                    if inside(os.readlink(proc / link)):
                        return identity
                except (PermissionError, FileNotFoundError):
                    pass
            try:
                descriptors = list((proc / 'fd').iterdir())
            except PermissionError:
                descriptors = []
            for entry in descriptors:
                try:
                    if inside(os.readlink(entry)):
                        return identity
                except (PermissionError, FileNotFoundError):
                    pass
            # An mmap can outlive the descriptor that originally opened it.
            with (proc / 'maps').open() as stream:
                for line in stream:
                    fields = line.split(None, 5)
                    if len(fields) == 6 and inside(fields[5].rstrip()):
                        return identity
        except (FileNotFoundError, ProcessLookupError):
            continue
        except PermissionError:
            # Linux may hide unrelated setuid/keyring/runner processes. Their
            # existence is not evidence of a reference to every private copy.
            # Recorded unobservable owners are protected by the lease checks;
            # argv/cwd/fd/maps observations here provide additional positive
            # evidence without overriding those authoritative owner locks.
            continue
    return None


_CAPTURE = r'''
import os, subprocess, sys
if os.geteuid() == 0:
    raise SystemExit(78)
limit = 64 * 1024 * 1024
base = ['git', '--no-pager', '--git-dir=/work/.git', '--work-tree=/work',
        '-c', 'safe.directory=/work', '-c', 'core.fsmonitor=false',
        '-c', 'core.hooksPath=/dev/null', '-c', 'core.attributesFile=/dev/null',
        '-c', 'core.filemode=true', '-c', 'core.ignoreStat=false',
        '-c', 'diff.autoRefreshIndex=false']
def run(args, ok=(0,)):
    p = subprocess.Popen(base + args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    chunks, length = [], 0
    try:
        while True:
            block = p.stdout.read(65536)
            if not block: break
            length += len(block)
            if length > limit: raise RuntimeError('result patch exceeds compact artifact bound')
            chunks.append(block)
        if p.wait() not in ok: raise RuntimeError('cannot capture verified result')
        return b''.join(chunks)
    finally:
        p.stdout.close()
        if p.poll() is None: p.kill()
        p.wait()
head = run(['rev-parse', '--verify', 'HEAD']).strip().decode('ascii')
if head != sys.argv[1]: raise SystemExit(73)
patch = run(['diff', '--binary', '--full-index', '--no-ext-diff', '--no-textconv',
             '--no-renames', 'HEAD', '--'])
for raw in run(['ls-files', '--others', '--exclude-standard', '-z']).split(b'\0'):
    if raw:
        patch += run(['diff', '--no-index', '--binary', '--full-index', '--no-ext-diff',
                      '--no-textconv', '--', '/dev/null', os.fsdecode(raw)], (0, 1))
        if len(patch) > limit: raise RuntimeError('result patch exceeds compact artifact bound')
sys.stdout.buffer.write(patch)
'''


def _capture(copy):
    from . import sandbox, workspace
    if not copy.metadata.get('base_head'):
        # An interrupted allocation before fetching any source has no trusted
        # baseline. Preserve only an empty/non-project tree automatically.
        if {entry.name for entry in copy.path.iterdir()} - {'.git'}:
            raise lifecycle.error('Incomplete source baseline needs explicit recovery before cleanup.', 73)
        return b''
    if workspace._git_digest(copy.path) != copy.metadata.get('git_digest'):
        raise lifecycle.error('Git administrative state changed; result capture refused.', 73)
    try:
        backend = sandbox.probe_backend()
        uid = os.geteuid() or 65534
        gid = os.getegid() if os.geteuid() else 65534
        args = [*backend.prefix, '--die-with-parent', '--new-session', '--unshare-user',
                '--uid', str(uid), '--gid', str(gid), '--unshare-pid', '--unshare-ipc',
                '--unshare-uts', '--unshare-net', '--disable-userns', '--cap-drop', 'ALL']
        for path in ('/usr', '/bin', '/lib', '/lib64'):
            if Path(path).exists():
                args += ['--ro-bind', path, path]
        args += ['--proc', '/proc', '--dev', '/dev', '--tmpfs', '/tmp',
                 '--ro-bind', str(copy.path), '/work', '--chdir', '/work', '--',
                 '/usr/bin/python3', '-c', _CAPTURE, copy.metadata['base_head']]
        env = {'PATH': '/usr/bin:/bin', 'HOME': '/tmp', 'LC_ALL': 'C',
               'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': '/dev/null',
               'GIT_OPTIONAL_LOCKS': '0', 'GIT_TERMINAL_PROMPT': '0'}
        result = subprocess.run(args, env=env, capture_output=True, timeout=120, check=False)
    except (OSError, subprocess.SubprocessError, sandbox.SandboxError):
        raise lifecycle.error('Cannot capture result in the required unprivileged namespace; copy preserved.') from None
    if result.returncode or len(result.stdout) > MAX_PATCH_BYTES:
        raise lifecycle.error('Unprivileged result capture failed; execution data was preserved.',
                              73 if result.returncode == 73 else 78)
    return result.stdout


def _load_record(state, identifier):
    from .workspace import Workspace, SCHEMA
    if not isinstance(identifier, str) or not re.fullmatch('[0-9a-f]{32}', identifier):
        raise lifecycle.error('Expected an owned workspace ID, never a path.', 64)
    copy = Workspace(Path(state) / 'workspaces' / identifier, {'source': '/'})
    with lifecycle.directory(copy) as fd:
        raw = lifecycle.read_file(fd, 'record.json')
    try:
        row = json.loads(raw)
        if (row['schema'] != SCHEMA or not isinstance(row['source'], str)
                or not Path(row['source']).is_absolute() or type(row['work_inode']) is not int):
            raise ValueError()
        copy.metadata, copy.source = row, Path(row['source'])
        return copy
    except (ValueError, TypeError, KeyError):
        raise lifecycle.error('Malformed workspace history; execution data preserved.') from None


def inspect_workspace(state, identifier):
    """History/result inspection works after the disposable checkout is gone."""
    state = state_storage.state_root(state)
    copy = _load_record(state, identifier)
    with lifecycle.directory(copy) as fd:
        raw = lifecycle.read_file(fd, 'result.json')
        present = lifecycle._exists(fd, 'work')
    try:
        result = None if raw is None else json.loads(raw)
    except (ValueError, TypeError):
        raise lifecycle.error('Malformed durable result manifest; history preserved.') from None
    return {'id': copy.id, 'path': str(copy.path), 'metadata': copy.metadata,
            'lease': lifecycle.read(copy), 'result': result, 'execution_present': present}


@contextmanager
def _owner(copy):
    with lifecycle.directory(copy) as fd:
        lock = lifecycle.open_lock(fd, 'owner.lock')
        try:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise lifecycle.error('Workspace has an active operation; cleanup deferred.', 75) from None
            yield fd
        finally:
            os.close(lock)


def _check_work(copy, fd, lease):
    info = os.stat('work', dir_fd=fd, follow_symlinks=False)
    if info.st_ino != copy.metadata.get('work_inode'):
        raise lifecycle.error('Execution inode changed; copy preserved.', 73)
    if lease and (info.st_ino != lease['work_inode'] or info.st_dev != lease['work_device']):
        raise lifecycle.error('Execution device/inode differs from lease; copy preserved.', 73)
    work_fd = _open_dir(fd, 'work', _identity(info))
    try:
        return _identity(info), _tree_usage(work_fd)
    finally:
        os.close(work_fd)


def _quarantine(root_fd, state):
    return state_storage.open_private_directory(Path(state) / 'quarantine', parent=root_fd, create=True)


def _finish_cleanup(copy, lease, quarantine_fd):
    pending = lease.get('cleanup')
    if not isinstance(pending, dict) or not re.fullmatch('[0-9a-f]{32}', pending.get('name', '')):
        raise lifecycle.error('Malformed cleanup intent; quarantine preserved.')
    name = pending['name']
    state = copy.directory.parent.parent
    destination = state / 'quarantine' / name
    extra = pending.get('extras', [])
    if not isinstance(extra, list) or any(not _valid_extra(entry) for entry in extra):
        raise lifecycle.error('Malformed execution environment cleanup intent.')
    # Complete an interrupted sequence of renames before starting deletion.
    with lifecycle.fence(state), lifecycle.directory(copy) as fd:
        for entry in extra:
            if lifecycle._exists(fd, entry['source']):
                child = _open_dir(fd, entry['source'], (entry['device'], entry['inode']))
                os.close(child)
                if lifecycle._exists(quarantine_fd, entry['name']):
                    raise lifecycle.error('Quarantine destination collision; no overwrite.', 73)
                move(fd, entry['source'], quarantine_fd, entry['name'])
                os.fsync(fd)
                os.fsync(quarantine_fd)
    paths = [destination, copy.path]
    paths += [state / 'quarantine' / entry['name'] for entry in extra]
    paths += [copy.directory / entry['source'] for entry in extra]
    if any(lifecycle._exists(quarantine_fd, entry['name']) for entry in [pending, *extra]):
        if _references(paths):
            # Keep the original inode recoverable; never overwrite a new path.
            restored = False
            with lifecycle.fence(state), lifecycle.directory(copy) as fd:
                if not lifecycle._exists(fd, 'work'):
                    if lifecycle._exists(quarantine_fd, name):
                        move(quarantine_fd, name, fd, 'work')
                    for entry in extra:
                        if (not lifecycle._exists(fd, entry['source'])
                                and lifecycle._exists(quarantine_fd, entry['name'])):
                            move(quarantine_fd, entry['name'], fd, entry['source'])
                    os.fsync(fd)
                    os.fsync(quarantine_fd)
                    lease['cleanup']['restored'] = True
                    lifecycle.write(copy, lease)
                    restored = True
            message = ('same inode restored' if restored else 'quarantine preserved because the original path is occupied')
            raise lifecycle.error('Open execution state appeared after quarantine; ' + message + '.', 75)
        for entry in [pending, *extra]:
            if lifecycle._exists(quarantine_fd, entry['name']):
                _remove_tree(quarantine_fd, entry['name'], (entry['device'], entry['inode']))
    # Recover a crash after restoring a busy tree, or before the rename.
    if copy.path.exists() or copy.path.is_symlink():
        raise lifecycle.error('Cleanup has an execution tree awaiting recovery.', 75)
    with lifecycle.fence(state):
        lease['cleanup']['state'] = 'cleaned'
        if not any(event.get('event') == 'cleaned' for event in lease['events']):
            lease['events'].append({'event': 'cleaned', 'at': time.time()})
        lifecycle.write(copy, lease)
    return {'id': copy.id, 'job_id': lease['job_id'], 'action': 'cleaned',
            'reason': lease.get('final_reason', 'finalized'),
            'allocated_bytes': pending.get('allocated_bytes', 0),
            'inodes': pending.get('inodes', 0)}


def _extra_name(name):
    return name == 'preparation-home' or re.fullmatch(r'(?:runtime|session)-[A-Za-z0-9_-]+', name)


def _valid_extra(entry):
    return (isinstance(entry, dict) and isinstance(entry.get('source'), str)
            and _extra_name(entry['source'])
            and re.fullmatch('[0-9a-f]{32}', str(entry.get('name', '')))
            and type(entry.get('device')) is int and type(entry.get('inode')) is int)


def finalize(copy, *, reason='accepted', cancelled=False, orphan=False, before_finalize=None):
    # Keep the owner lock through the entire (possibly interrupted) rmtree,
    # while releasing the creation fence before recursive deletion.
    with ExitStack() as owned:
        with lifecycle.fence(copy.directory.parent.parent):
            fd = owned.enter_context(_owner(copy))
        return _finalize(copy, reason=reason, cancelled=cancelled, orphan=orphan,
                         owner_fd=fd, before_finalize=before_finalize)


def _finalize(copy, *, reason, cancelled, orphan, owner_fd, before_finalize):
    """Publish the result, end this job lease, then remove its execution tree.

    Caller owns final integration/review. Failure before publication preserves
    the execution tree; interruption after rename leaves durable cleanup intent.
    Historical record.json is never rewritten to disguise cleanup.
    """
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 1000:
        raise lifecycle.error('Finalization requires a bounded reason.', 64)
    state = copy.directory.parent.parent
    quarantine_fd = None
    try:
        with lifecycle.fence(state) as (root_fd, _copies_fd), nullcontext(owner_fd) as fd:
            fresh = _load_record(state, copy.id)
            copy.metadata, copy.source = fresh.metadata, fresh.source
            lease = lifecycle.read(copy)
            if orphan and lifecycle.live(lease):
                raise lifecycle.error('A live logical job lease protects this workspace.', 75)
            if orphan and _pending_assignment(copy, lease):
                raise lifecycle.error('A current review/retry queue protects this logical job.', 75)
            lifecycle.authorize_finalization(lease)
            if lease and lease.get('cleanup', {}).get('state') == 'cleaned':
                if lifecycle._exists(fd, 'work'):
                    raise lifecycle.error('Cleaned job has a replacement execution tree; data preserved.', 73)
                if before_finalize is not None:
                    before_finalize()
                return {'id': copy.id, 'action': 'already-cleaned', 'reason': reason,
                        'allocated_bytes': 0, 'inodes': 0}
            if lifecycle._exists(fd, 'work'):
                identity, usage = _check_work(copy, fd, lease)
                extras = []
                for entry_name in os.listdir(fd):
                    if _extra_name(entry_name):
                        child = _open_dir(fd, entry_name)
                        try:
                            extra_info = os.fstat(child)
                            extra_usage = _tree_usage(child, allow_ipc=True)
                        finally:
                            os.close(child)
                        extras.append({'name': uuid.uuid4().hex, 'source': entry_name,
                                       'device': extra_info.st_dev, 'inode': extra_info.st_ino,
                                       'allocated_bytes': extra_usage[0], 'inodes': extra_usage[1]})
                if _references([copy.path, *(copy.directory / entry['source'] for entry in extras)]):
                    raise lifecycle.error('A process has open execution state; cleanup deferred.', 75)
                if before_finalize is not None:
                    before_finalize()
                    # Attestation can repair association; keep its durable lease.
                    lease = lifecycle.read(copy)
                patch = _capture(copy)
                lifecycle.atomic_file(fd, 'changes.patch', patch)
                result = {'schema': 1, 'workspace_id': copy.id,
                          'source': str(copy.source),
                          'base_head': copy.metadata.get('base_head') or copy.metadata.get('source_head'),
                          'patch': {'file': 'changes.patch', 'bytes': len(patch),
                                    'sha256': hashlib.sha256(patch).hexdigest()}}
                if lease is not None:
                    result['job_id'] = lease['job_id']
                lifecycle.atomic_file(fd, 'result.json', (json.dumps(result) + '\n').encode())
                lease = lease or lifecycle.initialize(copy)
                if lease['phase'] not in lifecycle.FINAL_PHASES:
                    lease['phase'] = 'cancelled' if cancelled else 'finalized'
                    lease['final_reason'] = reason
                    lease['events'].append({'event': lease['phase'], 'at': time.time(), 'reason': reason})
                # Idempotent recovery reuses its original quarantine name/inode.
                pending = lease.get('cleanup') or {'name': uuid.uuid4().hex,
                    'device': identity[0], 'inode': identity[1],
                    'allocated_bytes': usage[0] + sum(e['allocated_bytes'] for e in extras),
                    'inodes': usage[1] + sum(e['inodes'] for e in extras),
                    'extras': extras, 'state': 'pending'}
                if (pending['device'], pending['inode']) != identity:
                    raise lifecycle.error('Cleanup intent does not match execution inode.', 73)
                lease['cleanup'] = pending
                lifecycle.write(copy, lease)
                quarantine_fd = _quarantine(root_fd, state)
                if lifecycle._exists(quarantine_fd, pending['name']):
                    raise lifecycle.error('Quarantine destination already exists; no overwrite.', 73)
                move(fd, 'work', quarantine_fd, pending['name'])
                os.fsync(fd)
                os.fsync(quarantine_fd)
            else:
                if lease is None or not lease.get('cleanup'):
                    raise lifecycle.error('Missing execution tree has no cleanup intent; history preserved.', 73)
                if before_finalize is not None:
                    before_finalize()
                    lease = lifecycle.read(copy)
                quarantine_fd = _quarantine(root_fd, state)
        # The global admission fence is intentionally not held during deletion.
        return _finish_cleanup(copy, lease, quarantine_fd)
    finally:
        if quarantine_fd is not None:
            os.close(quarantine_fd)


def cancel(copy, *, reason='cancelled'):
    return finalize(copy, reason=reason, cancelled=True)


def recover(copy, *, owner=None):
    lease = lifecycle.read(copy)
    if lease and lease['phase'] in lifecycle.FINAL_PHASES:
        return finalize(copy, reason=lease.get('final_reason', 'recovered cleanup'),
                        cancelled=lease['phase'] == 'cancelled')
    with lifecycle.fence(copy.directory.parent.parent), _owner(copy):
        lifecycle.renew(copy, owner=owner, recover=True)
    return {'id': copy.id, 'action': 'recovered', 'reason': 'explicit ownership transfer'}


def _legacy_admission_active(root_fd):
    for name in os.listdir(root_fd):
        if not re.fullmatch(r'(?:worker-\d+\.lock|ticket-\d+)', name):
            continue
        lock = lifecycle.open_lock(root_fd, name, create=False)
        try:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
        finally:
            os.close(lock)
    return False


def _pending_assignment(copy, lease):
    """Read the exact *current* queue dependency without Git or history scans.

    A parent task's status and old attempt snapshots never authorize retention.
    Explicit needs-rework is a durable retry queue even between processes or
    after a reboot. A new lease's review phase also represents a pending review.
    """
    binding = (lease or {}).get('binding') or {
        'task_id': copy.metadata.get('coord_task'),
        'assignment_id': copy.metadata.get('coord_assignment')}
    task_id, assignment_id = binding.get('task_id'), binding.get('assignment_id')
    if (not isinstance(task_id, str) or not re.fullmatch('task-[0-9a-f]{20}', task_id)
            or not isinstance(assignment_id, str)):
        if lease and copy.metadata.get('standalone_job') and lease['phase'] in ('review', 'retry'):
            return 'pending standalone job review/retry; explicit finalize or cancel required'
        return None
    state = copy.directory.parent.parent
    key = hashlib.sha256(os.fsencode(str(copy.source))).hexdigest()[:24]
    directory = state / 'coordination' / key / 'tasks'
    try:
        fd = state_storage.open_private_directory(directory)
    except state_storage.StorageError as cause:
        if cause.errno == 2:
            return None
        raise lifecycle.error('Cannot safely verify the current job queue; copy preserved.') from None
    try:
        raw = lifecycle.read_file(fd, task_id + '.json')
    finally:
        os.close(fd)
    if raw is None:
        return None
    try:
        task = json.loads(raw)
        if (task.get('id') != task_id or task.get('project') != str(copy.source)
                or not isinstance(task.get('assignments'), list)
                or not isinstance(task.get('deliverables'), list)):
            raise ValueError()
        row = next((r for r in task['assignments'] if r.get('id') == assignment_id), None)
        current = {item['id'] for item in task['deliverables']}
        if (not row or row.get('workspace_id') != copy.id
                or row.get('deliverable_id') not in current or row.get('job_finalization')):
            return None
        if lease and row.get('job_id') and row['job_id'] != lease['job_id']:
            raise ValueError()
        if not lease and task.get('status') in ('completed', 'closed'):
            return None
        pending = lease.get('pending_attempt') if lease else None
        if (pending and row.get('status') == 'running'
                and pending.get('task_id') == task_id and pending.get('assignment_id') == assignment_id):
            return 'reserved execution awaits recovery; explicit recover/finalize/cancel required'
        decision = (row.get('disposition') or {}).get('kind')
        if row.get('status') in ('succeeded', 'failed') and decision == 'needs-rework':
            return 'pending retry in current assignment; explicit recover/finalize/cancel required'
        if (row.get('status') in ('succeeded', 'failed')
                and ((lease and lease['phase'] == 'review') or (not lease and decision is None))):
            return 'current review awaits explicit finalization; recover/finalize/cancel required'
        return None
    except (ValueError, TypeError, KeyError, AttributeError):
        raise lifecycle.error('Malformed current job queue; copy preserved.') from None


def collect(state=None, *, apply=False):
    """Explain every owned copy; dry-run does not even create a lock file."""
    state = state_storage.state_root(state)
    report = {'schema': 1, 'state_root': str(state), 'dry_run': not apply,
              'workspaces': [], 'reclaimed': {'allocated_bytes': 0, 'inodes': 0}}
    if not state.exists():
        return report
    root_fd = copies_fd = None
    try:
        root_fd = state_storage.open_private_directory(state)
        try:
            copies_fd = state_storage.open_private_directory(state / 'workspaces', parent=root_fd)
        except state_storage.StorageError:
            if not (state / 'workspaces').exists():
                return report
            raise
        legacy_busy = _legacy_admission_active(root_fd)
        for identifier in sorted(os.listdir(copies_fd)):
            row = {'id': identifier, 'action': 'kept', 'allocated_bytes': 0, 'inodes': 0}
            try:
                if not re.fullmatch('[0-9a-f]{32}', identifier):
                    raise lifecycle.error('Unknown control entry; preserved.')
                copy = _load_record(state, identifier)
                lease = lifecycle.read(copy)
                pending = _pending_assignment(copy, lease)
                if lease and lease.get('cleanup', {}).get('state') == 'cleaned':
                    row.update(action='already-cleaned', reason='finalized history only')
                elif lifecycle.live(lease):
                    row['reason'] = 'active logical job lease (' + lease['phase'] + ')'
                elif pending:
                    row['reason'] = pending
                elif (lease and lease['phase'] not in lifecycle.FINAL_PHASES
                      and lifecycle.identity_state(lease['owner']) == 'unknown'):
                    row['reason'] = 'unobservable job owner namespace; copy preserved'
                elif lease is None and legacy_busy:
                    row['reason'] = 'legacy worker admission is active; creation gap protected'
                else:
                    with lifecycle.directory(copy) as fd:
                        if lifecycle._exists(fd, 'owner.lock'):
                            lock = lifecycle.open_lock(fd, 'owner.lock', create=False)
                            try:
                                try:
                                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                                except BlockingIOError:
                                    raise lifecycle.error('active workspace operation', 75) from None
                            finally:
                                os.close(lock)
                        if lifecycle._exists(fd, 'work'):
                            _identity_value, usage = _check_work(copy, fd, lease)
                            row.update(allocated_bytes=usage[0], inodes=usage[1])
                    if _references((copy.path,)):
                        raise lifecycle.error('process has open execution state', 75)
                    row.update(action='would-clean', reason='orphaned lease' if lease else 'legacy unused copy')
                    if apply:
                        row.update(finalize(copy, reason=row['reason'], cancelled=True, orphan=True))
                        if row['action'] == 'cleaned':
                            for key in report['reclaimed']:
                                report['reclaimed'][key] += row[key]
            except (OSError, ValueError, state_storage.StorageError) as cause:
                row.update(action='kept', reason='unsafe or unreadable control state: ' + type(cause).__name__)
            except Exception as cause:
                from .workspace import WorkspaceError
                if not isinstance(cause, WorkspaceError):
                    raise
                row.update(action='kept', reason=str(cause))
            report['workspaces'].append(row)
        return report
    finally:
        if copies_fd is not None:
            os.close(copies_fd)
        if root_fd is not None:
            os.close(root_fd)
