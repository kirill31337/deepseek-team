"""Durable logical jobs, distinct from worker process attempts.

The lease lives beside record.json, never inside the disposable execution tree.
All lifecycle participants take the same creation/collection fence before an
owner lock. A process exit leaves review/retry leased to the coordinator.
"""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import json
import math
import os
from pathlib import Path
import re
import stat
import sys
import threading
import time
import uuid

from . import state_storage

_LOCAL = threading.local()
FINAL_PHASES = frozenset(('finalized', 'cancelled'))
PHASES = FINAL_PHASES | {'preparing', 'ready', 'running', 'review', 'retry'}
MAX_LEASE_BYTES = 8 * 1024 * 1024


def error(message, code=78):
    from .workspace import WorkspaceError
    return WorkspaceError(message, code)


def open_lock(parent_fd, name, *, create=True):
    flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
    fd = os.open(name, flags | (os.O_CREAT if create else 0), 0o600, dir_fd=parent_fd)
    info = os.fstat(fd)
    named = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or info.st_uid != os.geteuid() or info.st_mode & 0o077
            or (info.st_dev, info.st_ino) != (named.st_dev, named.st_ino)):
        os.close(fd)
        raise error('Unsafe lifecycle lock; no execution data was changed.')
    return fd


@contextmanager
def fence(state):
    """Pin the private root and serialize creation, ownership and GC rename.

    Reentrant only in this thread; different threads/processes still serialize.
    Never hold this fence through recursive deletion or a worker execution.
    """
    state = state_storage.state_root(state)
    held = getattr(_LOCAL, 'fences', {})
    key = str(state)
    if key in held:
        yield held[key]
        return
    root_fd = copies_fd = lock_fd = None
    try:
        root_fd = state_storage.open_private_directory(state, create=True)
        copies_fd = state_storage.open_private_directory(state / 'workspaces',
                                                         parent=root_fd, create=True)
        lock_fd = open_lock(root_fd, 'lifecycle.lock')
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        held[key] = (root_fd, copies_fd)
        _LOCAL.fences = held
        yield held[key]
    except state_storage.StorageError as cause:
        raise error(str(cause), cause.code) from None
    finally:
        held.pop(key, None)
        for fd in (lock_fd, copies_fd, root_fd):
            if fd is not None:
                os.close(fd)


@contextmanager
def directory(copy):
    """Open each ancestor without following links, pinning the control inode."""
    state = copy.directory.parent.parent
    fds = []
    try:
        root = state_storage.open_private_directory(state)
        fds.append(root)
        copies = state_storage.open_private_directory(state / 'workspaces', parent=root)
        fds.append(copies)
        fd = state_storage.open_private_directory(copy.directory, parent=copies)
        fds.append(fd)
        yield fd
    except state_storage.StorageError as cause:
        raise error(str(cause), cause.code) from None
    finally:
        for fd in reversed(fds):
            os.close(fd)


def read_file(fd, name, *, maximum=MAX_LEASE_BYTES):
    try:
        source = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                         dir_fd=fd)
        try:
            info = os.fstat(source)
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or info.st_uid != os.geteuid() or info.st_size > maximum
                    or info.st_mode & 0o077):
                raise error('Unsafe or oversized lifecycle metadata: ' + name)
            with os.fdopen(source, 'rb', closefd=False) as stream:
                raw = stream.read(maximum + 1)
            if len(raw) > maximum:
                raise error('Oversized lifecycle metadata: ' + name)
            return raw
        finally:
            os.close(source)
    except FileNotFoundError:
        return None
    except OSError:
        raise error('Cannot safely read lifecycle metadata: ' + name) from None


def atomic_file(fd, name, raw):
    temporary = '.lifecycle-' + uuid.uuid4().hex
    target = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=fd)
    try:
        with os.fdopen(target, 'wb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        # Replacement does not follow the old entry; refuse unsafe metadata
        # anyway, so a planted symlink is never silently "repaired".
        previous = os.stat(name, dir_fd=fd, follow_symlinks=False) if _exists(fd, name) else None
        if previous is not None and (not stat.S_ISREG(previous.st_mode)
                or previous.st_uid != os.geteuid() or previous.st_nlink != 1):
            raise error('Lifecycle destination was replaced: ' + name)
        os.replace(temporary, name, src_dir_fd=fd, dst_dir_fd=fd)
        os.fsync(fd)
    except OSError:
        raise error('Lifecycle publication failed; execution data was preserved.') from None
    finally:
        try:
            os.unlink(temporary, dir_fd=fd)
        except FileNotFoundError:
            pass


def _exists(fd, name):
    try:
        os.stat(name, dir_fd=fd, follow_symlinks=False)
        return True
    except FileNotFoundError:
        return False


def write(copy, lease):
    validate(lease, copy.id)
    with directory(copy) as fd:
        atomic_file(fd, 'lease.json', (json.dumps(lease, ensure_ascii=True) + '\n').encode())


def _valid_identity(value):
    return (isinstance(value, dict) and set(value) in (
                {'pid', 'start_time', 'boot_id', 'uid'},
                {'pid', 'start_time', 'boot_id', 'uid', 'pid_namespace'})
            and type(value['pid']) is int and 0 < value['pid'] < 2 ** 31
            and type(value['uid']) is int and 0 <= value['uid'] < 2 ** 32
            and isinstance(value['start_time'], str) and value['start_time'].isdigit()
            and len(value['start_time']) <= 32
            and isinstance(value['boot_id'], str) and 0 < len(value['boot_id']) <= 128
            and ('pid_namespace' not in value or
                 (type(value['pid_namespace']) is int and value['pid_namespace'] > 0)))


def process_identity(pid):
    if type(pid) is not int or pid < 1:
        return None
    try:
        proc = Path('/proc') / str(pid)
        fields = (proc / 'stat').read_text().rsplit(')', 1)[1].split()
        if fields[0] in ('Z', 'X'):
            return None
        return {'pid': pid, 'start_time': fields[19],
                'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
                'uid': proc.stat().st_uid,
                'pid_namespace': (proc / 'ns/pid').stat().st_ino}
    except (OSError, ValueError, IndexError):
        return None


def identity_live(identity):
    return identity_state(identity) == 'live'


def identity_state(identity):
    """Distinguish proven process loss from an unobservable PID namespace."""
    if not _valid_identity(identity) or identity['uid'] != os.geteuid():
        return 'dead'
    try:
        boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        if identity['boot_id'] != boot:
            return 'dead'
        namespace = Path('/proc/self/ns/pid').stat().st_ino
        if identity.get('pid_namespace', namespace) != namespace:
            return 'unknown'
        proc = Path('/proc') / str(identity['pid'])
        fields = (proc / 'stat').read_text().rsplit(')', 1)[1].split()
        if fields[0] in ('Z', 'X'):
            return 'dead'
        if fields[19] != identity['start_time']:
            return 'dead'  # PID reuse is provable even when its namespace is hidden.
        actual = process_identity(identity['pid'])
        if actual is None:
            return 'unknown'
        return 'live' if all(actual.get(key) == value for key, value in identity.items()) else 'dead'
    except FileNotFoundError:
        return 'dead' if Path('/proc/self/stat').exists() else 'unknown'
    except (OSError, ValueError, IndexError):
        return 'unknown'


def owner_identity():
    """Actual coordinator, or the CLI's calling shell across short commands."""
    pid = os.getpid()
    shell = None
    cli = getattr(sys.modules.get('__main__'), '__package__', None) == 'codex_deepseek_team'
    for _ in range(64):
        try:
            proc = Path('/proc') / str(pid)
            command = (proc / 'comm').read_text().strip()
            if pid == os.getpid() and command == 'deepseek-team':
                cli = True
            executable = Path(os.readlink(proc / 'exe')).name
            if (shell is None and pid != os.getpid()
                    and command in ('bash', 'dash', 'sh', 'zsh', 'fish', 'ksh')
                    and executable in ('bash', 'dash', 'sh', 'zsh', 'fish', 'ksh')):
                candidate = process_identity(pid)
                if candidate and candidate['uid'] == os.geteuid():
                    shell = candidate
            if command in ('codex', 'claude') or executable in ('codex', 'claude'):
                identity = process_identity(pid)
                if identity and identity['uid'] == os.geteuid():
                    return identity
            fields = (proc / 'stat').read_text().rsplit(')', 1)[1].split()
            pid = int(fields[1])
            if pid <= 1:
                break
        except (OSError, IndexError, ValueError):
            break
    return (shell if cli else None) or process_identity(os.getpid())


def validate(row, workspace_id):
    if (not isinstance(row, dict) or row.get('schema') != 1
            or row.get('workspace_id') != workspace_id
            or not re.fullmatch('[0-9a-f]{32}', str(row.get('job_id', '')))
            or row.get('phase') not in PHASES
            or type(row.get('generation')) is not int or row['generation'] < 1
            or not _valid_identity(row.get('owner'))
            or type(row.get('work_inode')) is not int
            or type(row.get('work_device')) is not int
            or not isinstance(row.get('attempts'), list)
            or not isinstance(row.get('events'), list)
            or any(type(row.get(k)) not in (int, float) or not math.isfinite(row[k])
                   for k in ('created_at', 'heartbeat'))):
        raise error('Malformed or mismatched logical job lease; copy preserved.')
    ids = set()
    for attempt in row['attempts']:
        if (not isinstance(attempt, dict)
                or not re.fullmatch('[0-9a-f]{32}', str(attempt.get('id', '')))
                or attempt['id'] in ids or attempt.get('status') not in
                ('running', 'succeeded', 'failed', 'interrupted')):
            raise error('Malformed logical job attempt history; copy preserved.')
        ids.add(attempt['id'])
    return row


def read(copy):
    with directory(copy) as fd:
        raw = read_file(fd, 'lease.json')
    if raw is None:
        if copy.metadata.get('job_id'):
            raise error('Logical job lease is missing; execution data preserved.', 73)
        return None
    try:
        return validate(json.loads(raw), copy.id)
    except (ValueError, TypeError, KeyError):
        raise error('Malformed logical job lease; copy preserved.') from None


def initialize(copy, owner=None):
    with fence(copy.directory.parent.parent):
        previous = read(copy)
        if previous is not None:
            if owner is not None and previous['owner'] != owner:
                return renew(copy, owner=owner)
            return previous
        owner = owner or owner_identity()
        if not _valid_identity(owner):
            raise error('Cannot identify the logical job owner.')
        info = copy.path.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
            raise error('Execution directory is unsafe; cannot create a lease.')
        if info.st_ino != copy.metadata.get('work_inode'):
            raise error('Execution inode changed; cannot create a lease.', 73)
        now = time.time()
        row = {'schema': 1, 'job_id': uuid.uuid4().hex, 'workspace_id': copy.id,
               'work_inode': info.st_ino, 'work_device': info.st_dev,
               'generation': 1, 'owner': owner, 'created_at': now, 'heartbeat': now,
               'phase': 'ready', 'attempts': [],
               'events': [{'event': 'created', 'at': now}]}
        write(copy, row)
        return row


def live(row):
    return row is not None and row['phase'] not in FINAL_PHASES and identity_live(row['owner'])


def owner_authorizes_process(identity):
    """A child operation may use its live coordinator's lease without adopting it."""
    if identity_state(identity) != 'live':
        return False
    pid = os.getpid()
    for _ in range(128):
        if pid == identity['pid']:
            actual = process_identity(pid)
            return actual is not None and all(actual.get(k) == v for k, v in identity.items())
        if pid <= 1:
            return False
        try:
            fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
            pid = int(fields[1])
        except (OSError, ValueError, IndexError):
            return False
    return False


def prepare_attempt(copy, task_id, assignment_id, *, replace_pending=False):
    """Reserve the same attempt identity for the ledger and workspace begin."""
    with fence(copy.directory.parent.parent):
        row = read(copy) or initialize(copy)
        if row['phase'] in FINAL_PHASES:
            raise error('Finalized logical jobs require a new workspace.', 73)
        binding = {'task_id': task_id, 'assignment_id': assignment_id}
        if replace_pending and row.get('pending_attempt'):
            pending = row.pop('pending_attempt')
            row['attempts'].append({'id': pending['id'], 'operation': 'execution',
                'status': 'interrupted', 'started_at': None, 'finished_at': time.time(),
                'generation': row['generation']})
        pending = row.get('pending_attempt')
        if pending is None:
            pending = dict(binding, id=uuid.uuid4().hex)
            row['pending_attempt'] = pending
        elif (not isinstance(pending, dict) or set(pending) != {'task_id', 'assignment_id', 'id'}
              or not re.fullmatch('[0-9a-f]{32}', str(pending['id']))
              or any(pending.get(key) != value for key, value in binding.items())):
            raise error('A different job start already reserved this execution copy.', 75)
        row['binding'] = binding
        write(copy, row)
        return row['job_id'], pending['id']


def renew(copy, owner=None, *, recover=False):
    with fence(copy.directory.parent.parent):
        row = read(copy)
        if row is None:
            return initialize(copy, owner=owner)
        if row['phase'] in FINAL_PHASES:
            raise error('Job was finalized; create a new workspace for new execution.', 73)
        owner = owner or owner_identity()
        if not _valid_identity(owner):
            raise error('Cannot identify the logical job owner.')
        if row['owner'] != owner:
            if owner_authorizes_process(row['owner']):
                row['heartbeat'] = time.time()
                write(copy, row)
                return row
            if identity_state(row['owner']) != 'dead':
                raise error('Another live or unobservable coordinator owns this logical job.', 75)
            if not recover:
                raise error('Job owner exited; inspect and explicitly recover before resuming.', 75)
            row['generation'] += 1
            row['events'].append({'event': 'recovered', 'at': time.time(),
                                  'previous_owner': row['owner']})
            row['owner'] = owner
        row['heartbeat'] = time.time()
        write(copy, row)
        return row


def begin(copy, operation):
    with fence(copy.directory.parent.parent):
        row = read(copy) or initialize(copy)
        if row['phase'] in FINAL_PHASES:
            raise error('Finalized logical jobs cannot execute again.', 73)
        if row.get('pending_attempt') and operation != 'execution':
            raise error('An execution attempt is reserved; reconcile it before preparation or import.', 75)
        if row['attempts'] and row['attempts'][-1]['status'] == 'running':
            # Explicit recovery retains the interrupted attempt instead of
            # overwriting it with the next process's outcome.
            row['attempts'][-1].update(status='interrupted', finished_at=time.time())
        pending = row.pop('pending_attempt', None) if operation == 'execution' else None
        attempt = {'id': pending['id'] if pending else uuid.uuid4().hex,
                   'operation': operation, 'status': 'running',
                   'started_at': time.time(), 'generation': row['generation']}
        row['attempts'].append(attempt)
        row.update(phase='running', heartbeat=time.time())
        write(copy, row)
        return attempt['id']


def finish(copy):
    with fence(copy.directory.parent.parent):
        row = read(copy)
        if row is None:
            return None
        if row['phase'] in FINAL_PHASES:
            raise error('Cannot change a finalized logical job.', 73)
        pending = row.pop('pending_attempt', None)
        if pending:
            row['attempts'].append({'id': pending['id'], 'operation': 'execution',
                'status': 'running', 'started_at': None, 'generation': row['generation']})
        if row['attempts'] and row['attempts'][-1]['status'] == 'running':
            row['attempts'][-1].update(
                status=copy.metadata['status'], exit_code=copy.metadata.get('exit_code'),
                finished_at=copy.metadata.get('finished_at', time.time()),
                error_kind=copy.metadata.get('error_kind'))
        row.update(phase='review', heartbeat=time.time())
        write(copy, row)
        return row


def rework(copy):
    with fence(copy.directory.parent.parent):
        row = read(copy) or initialize(copy)
        if row['phase'] in FINAL_PHASES:
            raise error('Corrections require a new job after finalization.', 73)
        row.update(phase='retry', heartbeat=time.time())
        row['events'].append({'event': 'needs-rework', 'at': time.time()})
        write(copy, row)
        return row


def heartbeat_task(root, task, *, recover=False):
    """Renew only this coordinator's current bindings; explicit resume can claim.

    A worker holding owner.lock already protects its active operation. Busy jobs
    are left alone; Stop and compaction never end a lease or start execution.
    """
    from . import workspace, workspace_gc
    state = state_storage.state_root()
    owner = owner_identity()
    seen, problems = set(), []
    for assignment in task.get('assignments', []):
        identifier = assignment.get('workspace_id')
        if (not isinstance(identifier, str) or not re.fullmatch('[0-9a-f]{32}', identifier)
                or identifier in seen):
            continue
        try:
            copy = workspace_gc._load_record(state, identifier)
            row = read(copy)
            if row is None or row['phase'] in FINAL_PHASES or str(copy.source) != str(root):
                continue
            binding = row.get('binding') or {'task_id': copy.metadata.get('coord_task'),
                                            'assignment_id': copy.metadata.get('coord_assignment')}
            if binding != {'task_id': task['id'], 'assignment_id': assignment['id']}:
                continue
            seen.add(identifier)
            if row['owner'] != owner and not recover:
                continue
            with fence(state), workspace_gc._owner(copy) as fd:
                info = os.stat('work', dir_fd=fd, follow_symlinks=False)
                if ((info.st_dev, info.st_ino) != (row['work_device'], row['work_inode'])
                        or info.st_ino != copy.metadata.get('work_inode')
                        or not stat.S_ISDIR(info.st_mode)):
                    raise error('Execution inode changed; coordinator did not adopt it.', 73)
                renew(copy, owner=owner, recover=recover)
        except workspace.WorkspaceError as cause:
            if cause.code != 75:
                problems.append(identifier + ': ' + str(cause))
        except (OSError, state_storage.StorageError):
            problems.append(identifier + ': lease heartbeat unavailable; data preserved')
    return problems
