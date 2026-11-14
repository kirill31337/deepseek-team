"""Provider-free finalization and recoverable collection of owned execution trees.

Historical records remain beside compact result artifacts. The quarantine is a
short deletion stage on the same mount, never a checkout archive. No Git process
runs as root: result capture uses a sparse, private-network user namespace.
"""
from __future__ import annotations

from contextlib import contextmanager, ExitStack, nullcontext
import errno
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import subprocess
import time
import uuid

from . import state_storage, workspace_lifecycle as lifecycle
from .atomic_rename import move
from .workspace import WorkspaceError

MAX_PATCH_BYTES = 64 * 1024 * 1024
MAX_RESULT_BYTES = 2 * 1024 * 1024
MAX_CONTROL_BYTES = 4096
DISCARD_REASONS = frozenset((
    'git_integrity_mismatch', 'capture_too_large',
    'capture_unavailable', 'capture_failed'))


class CaptureFailure(WorkspaceError):
    """A capture refusal in one of the closed, verified failure branches.

    Only this typed exception authorizes the destructive ``discard_result``
    path.  Unknown exceptions, signals, malformed protocol responses and
    publication failures deliberately do not become ``CaptureFailure``.
    """

    def __init__(self, reason_code, message, *, code=None):
        if reason_code not in DISCARD_REASONS:
            raise ValueError('Unsupported capture failure code.')
        super().__init__(message, 73 if reason_code == 'git_integrity_mismatch' else 78)
        self.reason_code = reason_code


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
import errno, hashlib, json, os, subprocess, sys

LIMIT = 64 * 1024 * 1024
CONTROL_MAX = 4096

class Failure(Exception):
    def __init__(self, code, step):
        self.code = code
        self.step = step

def report(payload):
    raw = json.dumps(payload, separators=(',', ':'), sort_keys=True).encode('ascii')
    if len(raw) > CONTROL_MAX:
        raise RuntimeError('control response exceeds its bound')
    os.write(2, raw)

base = ['git', '--no-pager', '--git-dir=/work/.git', '--work-tree=/work',
        '-c', 'safe.directory=/work', '-c', 'core.fsmonitor=false',
        '-c', 'core.hooksPath=/dev/null', '-c', 'core.attributesFile=/dev/null',
        '-c', 'core.filemode=true', '-c', 'core.ignoreStat=false',
        '-c', 'diff.autoRefreshIndex=false']
def run(args, ok=(0,), env=None, step='git'):
    try:
        process = subprocess.Popen(base + args, env=env, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL)
    except OSError as error:
        if error.errno in (errno.ENOENT, errno.EACCES, errno.EPERM):
            raise Failure('capture_unavailable', step)
        raise
    chunks, length = [], 0
    try:
        while True:
            block = process.stdout.read(65536)
            if not block:
                break
            length += len(block)
            if length > LIMIT:
                raise Failure('capture_too_large', step)
            chunks.append(block)
        code = process.wait()
    finally:
        process.stdout.close()
        if process.poll() is None:
            process.kill()
            process.wait()
    if code < 0:
        raise RuntimeError('capture command ended by signal')
    if code not in ok:
        raise Failure('capture_failed', step)
    return b''.join(chunks)

def capture():
    head = run(['rev-parse', '--verify', 'HEAD'], step='head').strip().decode('ascii')
    if head != sys.argv[1]:
        raise Failure('git_integrity_mismatch', 'head')
    patch = run(['diff', '--binary', '--full-index', '--no-ext-diff', '--no-textconv',
                 '--no-renames', sys.argv[1], '--'], step='diff')
    for raw in run(['ls-files', '--others', '--exclude-standard', '-z'],
                   step='ls-files').split(b'\0'):
        if raw:
            patch += run(['diff', '--no-index', '--binary', '--full-index',
                          '--no-ext-diff', '--no-textconv', '--', '/dev/null',
                          os.fsdecode(raw)], (0, 1), step='untracked')
            if len(patch) > LIMIT:
                raise Failure('capture_too_large', 'patch')
    sys.stdout.buffer.write(patch)
    sys.stdout.buffer.flush()
    report({'status': 'ok', 'bytes': len(patch),
            'sha256': hashlib.sha256(patch).hexdigest()})

try:
    capture()
except Failure as failure:
    report({'status': 'error', 'code': failure.code, 'step': failure.step})
    raise SystemExit(1)
except BaseException:
    report({'status': 'error', 'code': 'internal'})
    raise SystemExit(1)
'''

def _capture(copy):
    """Run the bounded capture protocol and return patch bytes.

    Only a verified structural mismatch, the encoded-size bound, a diagnosed
    mandatory-sandbox/launch refusal, or a controlled capture failure raises
    ``CaptureFailure``.  Every unknown signal, protocol, exception or output
    state leaves the copy untouched by remaining an ordinary error.
    """
    from . import git_integrity, sandbox

    if not copy.metadata.get('base_head'):
        # An interrupted allocation before fetching any source has no trusted
        # baseline. Preserve only an empty/non-project tree automatically.
        if {entry.name for entry in copy.path.iterdir()} - {'.git'}:
            raise CaptureFailure(
                'capture_unavailable',
                'Incomplete source baseline needs explicit recovery before cleanup.')
        return b''
    evidence = copy.metadata.get('git_integrity')
    try:
        if evidence is not None:
            git_integrity.verify(copy.path, evidence)
        elif copy.metadata.get('git_digest'):
            if not git_integrity.legacy_matches(
                    copy.path, copy.metadata['git_digest']):
                raise git_integrity.GitIntegrityError(
                    'Git administrative state changed; result capture refused.')
        else:
            raise CaptureFailure(
                'capture_unavailable',
                'Git integrity evidence is unavailable; result capture refused.')
    except git_integrity.GitIntegrityError as error:
        if getattr(error, 'kind', None) == 'unsupported':
            raise CaptureFailure('capture_unavailable', str(error)) from None
        if getattr(error, 'kind', None) == 'integrity_mismatch':
            raise CaptureFailure('git_integrity_mismatch', str(error)) from None
        raise lifecycle.error('Git integrity could not be inspected; copy preserved.', 78) from None

    try:
        backend = sandbox.probe_backend()
    except sandbox.SandboxError as error:
        raise CaptureFailure('capture_unavailable', error.message) from None
    uid = os.geteuid() or 65534
    gid = os.getegid() if os.geteuid() else 65534
    object_format = (evidence.get('object_format')
                     if isinstance(evidence, dict) else None)
    try:
        # C1 owns private-index creation and seeding. The verified index is
        # mounted read-only; Git never reads the worker-controlled index.
        with git_integrity.temporary_index(
                copy.path, copy.metadata['base_head'],
                object_format=object_format) as index_env:
            index_file = Path(index_env.get('GIT_INDEX_FILE', ''))
            if not index_file.is_absolute() or not index_file.name:
                raise lifecycle.error('Private capture index is unavailable; copy preserved.', 78)
            try:
                index_fd = os.open(index_file.parent,
                                   os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
            except OSError as error:
                if error.errno in (errno.ENOENT, errno.EACCES, errno.EPERM):
                    raise CaptureFailure(
                        'capture_unavailable',
                        'The verified capture index is unavailable; copy preserved.') from None
                raise
            try:
                args = [*backend.prefix, '--die-with-parent', '--new-session', '--unshare-user',
                        '--uid', str(uid), '--gid', str(gid), '--unshare-pid', '--unshare-ipc',
                        '--unshare-uts', '--unshare-net', '--disable-userns', '--cap-drop', 'ALL']
                for path in ('/usr', '/bin', '/lib', '/lib64'):
                    if Path(path).exists():
                        args += ['--ro-bind', path, path]
                args += ['--proc', '/proc', '--dev', '/dev', '--tmpfs', '/tmp',
                         '--ro-bind-fd', str(index_fd), '/capture-index',
                         '--ro-bind', str(copy.path), '/work', '--chdir', '/work', '--',
                         '/usr/bin/python3', '-c', _CAPTURE, copy.metadata['base_head']]
                env = {'PATH': '/usr/bin:/bin', 'HOME': '/tmp', 'LC_ALL': 'C',
                       'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': '/dev/null',
                       'GIT_OPTIONAL_LOCKS': '0', 'GIT_TERMINAL_PROMPT': '0',
                       'GIT_INDEX_FILE': '/capture-index/' + index_file.name}
                try:
                    result = subprocess.run(args, env=env, capture_output=True, timeout=120,
                                            check=False, pass_fds=(index_fd,))
                except subprocess.TimeoutExpired:
                    raise CaptureFailure(
                        'capture_failed',
                        'Result capture exceeded its controlled time limit; copy preserved.') from None
                except OSError as error:
                    if error.errno in (errno.ENOENT, errno.EACCES, errno.EPERM):
                        raise CaptureFailure(
                            'capture_unavailable',
                            'Cannot launch the required unprivileged capture; copy preserved.') from None
                    raise
                except sandbox.SandboxError as error:
                    raise CaptureFailure('capture_unavailable', error.message) from None
            finally:
                os.close(index_fd)
    except git_integrity.GitIntegrityError as error:
        if getattr(error, 'kind', None) == 'unsupported':
            raise CaptureFailure('capture_unavailable', str(error)) from None
        if getattr(error, 'kind', None) == 'integrity_mismatch':
            raise CaptureFailure('git_integrity_mismatch', str(error)) from None
        raise lifecycle.error('Git integrity could not be inspected; copy preserved.', 78) from None

    stdout = getattr(result, 'stdout', None)
    stderr = getattr(result, 'stderr', None)
    if (type(result.returncode) is not int or not isinstance(stdout, bytes)
            or not isinstance(stderr, bytes)):
        raise lifecycle.error('Result capture returned an unknown output shape; copy preserved.', 78)
    # A signal is not a structured capture refusal. The same is true for a
    # missing, oversized or malformed response; neither may authorize deletion.
    if result.returncode < 0:
        raise lifecycle.error('Result capture ended by an unknown signal; copy preserved.', 78)
    control = _capture_control(stderr)
    status = control.get('status')
    if status == 'ok':
        if (result.returncode != 0 or set(control) != {'status', 'bytes', 'sha256'}
                or type(control.get('bytes')) is not int or control['bytes'] != len(stdout)
                or not isinstance(control.get('sha256'), str)
                or control['sha256'] != hashlib.sha256(stdout).hexdigest()):
            raise lifecycle.error('Result capture protocol response is inconsistent; copy preserved.', 78)
        if len(stdout) > MAX_PATCH_BYTES:
            raise CaptureFailure(
                'capture_too_large', 'Result patch exceeds the compact artifact bound.')
        return stdout
    if status == 'error':
        code = control.get('code')
        step = control.get('step')
        steps = {
            'git_integrity_mismatch': ('head',),
            'capture_too_large': ('head', 'diff', 'ls-files', 'untracked', 'patch'),
            'capture_unavailable': ('head', 'diff', 'ls-files', 'untracked'),
            'capture_failed': ('head', 'diff', 'ls-files', 'untracked'),
        }
        if (result.returncode != 1 or not isinstance(code, str)
                or code not in DISCARD_REASONS
                or set(control) != {'status', 'code', 'step'}
                or not isinstance(step, str) or step not in steps[code]):
            raise lifecycle.error('Result capture protocol response is unknown; copy preserved.', 78)
        messages = {
            'git_integrity_mismatch': 'Verified Git baseline mismatch; result capture refused.',
            'capture_too_large': 'Result patch exceeds the compact artifact bound; copy preserved.',
            'capture_unavailable': 'The required capture boundary is unavailable; copy preserved.',
            'capture_failed': 'A structured capture command failed; copy preserved.',
        }
        raise CaptureFailure(code, messages[code])
    raise lifecycle.error('Result capture protocol response is missing or malformed; copy preserved.', 78)


def _capture_control(raw):
    if not raw or len(raw) > MAX_CONTROL_BYTES:
        raise lifecycle.error('Result capture protocol response is missing or oversized; copy preserved.', 78)
    try:
        value = json.loads(raw)
    except (ValueError, TypeError, UnicodeError):
        raise lifecycle.error('Result capture protocol response is malformed; copy preserved.', 78) from None
    if not isinstance(value, dict) or not isinstance(value.get('status'), str):
        raise lifecycle.error('Result capture protocol response is malformed; copy preserved.', 78)
    return value


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



def _artifact_digest(fd, name):
    """Stream one owned historical artifact without loading it into memory."""
    if name != 'changes.patch':
        raise lifecycle.error('Unknown historical result artifact; history preserved.', 73)
    try:
        source = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                         dir_fd=fd)
    except FileNotFoundError:
        return None
    except OSError:
        raise lifecycle.error('Cannot read historical result artifact; history preserved.', 73) from None
    try:
        info = os.fstat(source)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or info.st_uid != os.geteuid()):
            raise lifecycle.error('Historical result artifact is unsafe; history preserved.', 73)
        digest, size = hashlib.sha256(), 0
        while True:
            chunk = os.read(source, 1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            digest.update(chunk)
        return size, digest.hexdigest()
    except OSError:
        raise lifecycle.error('Cannot read historical result artifact; history preserved.', 73) from None
    finally:
        os.close(source)


def _valid_attempt_id(value):
    return value is None or (isinstance(value, str)
                             and re.fullmatch('[0-9a-f]{32}', value) is not None)


def _validate_finalization_block(value):
    if value is None:
        return
    if (not isinstance(value, dict) or set(value) != {
            'request_id', 'at', 'cancelled', 'discard_result', 'reason'}
            or not re.fullmatch('[0-9a-f]{32}', str(value.get('request_id', '')))
            or type(value.get('at')) not in (int, float) or not math.isfinite(value['at'])
            or type(value.get('cancelled')) is not bool
            or type(value.get('discard_result')) is not bool
            or value['discard_result'] and not value['cancelled']
            or not isinstance(value.get('reason'), str)
            or not value['reason'].strip() or len(value['reason']) > 1000):
        raise lifecycle.error('Result finalization identity is malformed; history preserved.', 73)


def _validate_result_document(copy, result, fd, lease=None):
    """Validate one closed result schema and its retained bytes."""
    if not isinstance(result, dict) or type(result.get('schema')) is not int:
        raise lifecycle.error('Unknown or malformed durable result schema; history preserved.', 73)
    if result.get('workspace_id') != copy.id or result.get('source') != str(copy.source):
        raise lifecycle.error('Durable result belongs to another workspace/source.', 73)
    if lease is not None and 'job_id' in result and result['job_id'] != lease['job_id']:
        raise lifecycle.error('Durable result belongs to another logical job.', 73)
    if 'job_id' in result and not re.fullmatch('[0-9a-f]{32}', str(result['job_id'])):
        raise lifecycle.error('Durable result job identity is malformed.', 73)
    if 'attempt_id' in result and not _valid_attempt_id(result['attempt_id']):
        raise lifecycle.error('Durable result attempt identity is malformed.', 73)
    _validate_finalization_block(result.get('finalization'))
    finalization = result.get('finalization')
    lease_finalization = (lease or {}).get('finalization')
    if lease is not None:
        if isinstance(lease_finalization, dict):
            if finalization is None:
                if ('result_schema' in lease_finalization
                        or (lease.get('cleanup') or {}).get('state') == 'cleaned'):
                    raise lifecycle.error(
                        'Retained result has no matching finalization identity.', 73)
            elif not _result_matches_request(result, lease_finalization):
                raise lifecycle.error(
                    'Retained result does not match its finalization request.', 73)
            if ('result_schema' in lease_finalization
                    and lease_finalization['result_schema'] != result['schema']):
                raise lifecycle.error(
                    'Retained result schema conflicts with its finalization request.', 73)
        elif finalization is not None:
            raise lifecycle.error(
                'Retained result claims finalization without a durable request.', 73)
    schema = result['schema']
    if schema == 1:
        allowed = {'schema', 'workspace_id', 'source', 'base_head', 'patch',
                   'job_id', 'attempt_id', 'finalization'}
        expected_head = (copy.metadata.get('base_head')
                         or copy.metadata.get('source_head'))
        patch = result.get('patch')
        if (set(result) - allowed or result.get('base_head') != expected_head
                or not isinstance(patch, dict)
                or set(patch) != {'file', 'bytes', 'sha256'}
                or patch.get('file') != 'changes.patch'
                or type(patch.get('bytes')) is not int or patch['bytes'] < 0
                or patch['bytes'] > MAX_PATCH_BYTES
                or not isinstance(patch.get('sha256'), str)
                or re.fullmatch('[0-9a-f]{64}', patch['sha256']) is None):
            raise lifecycle.error('Result schema 1 is malformed or mismatched; history preserved.', 73)
        artifact = _artifact_digest(fd, 'changes.patch')
        if (artifact is None or artifact[0] != patch['bytes']
                or artifact[1] != patch['sha256']):
            raise lifecycle.error('Result schema 1 patch failed its integrity check.', 73)
        if isinstance(lease_finalization, dict) and 'capture' in lease_finalization:
            capture = lease_finalization['capture']
            if (capture['bytes'] != patch['bytes']
                    or capture['sha256'] != patch['sha256']
                    or capture.get('attempt_id') != result.get('attempt_id')):
                raise lifecycle.error(
                    'Retained capture evidence does not match the result patch.', 73)
        return result
    if schema == 2:
        history = lease if lease is not None else lifecycle.read(copy)
        if not history or result.get('job_id') != history['job_id']:
            raise lifecycle.error('Discarded result has no matching durable job history.', 73)
        attempt_ids = {attempt['id'] for attempt in history.get('attempts', [])}
        if result.get('attempt_id') is not None and result['attempt_id'] not in attempt_ids:
            raise lifecycle.error('Discarded result attempt is absent from durable job history.', 73)
        allowed = {'schema', 'workspace_id', 'source', 'base_head', 'patch',
                   'job_id', 'attempt_id', 'capture', 'historical_artifacts',
                   'finalization'}
        capture = result.get('capture')
        artifacts = result.get('historical_artifacts')
        if (set(result) - allowed or result.get('base_head') != copy.metadata.get('base_head')
                or 'patch' not in result or result.get('patch') is not None
                or not isinstance(capture, dict)
                or set(capture) != {'status', 'reason_code', 'reason', 'baseline_status'}
                or capture.get('status') != 'discarded'
                or capture.get('reason_code') not in DISCARD_REASONS
                or not isinstance(capture.get('reason'), str)
                or not capture['reason'].strip() or len(capture['reason']) > 1000
                or capture.get('baseline_status') != (
                    'recorded' if result.get('base_head') else 'unavailable')
                or not isinstance(finalization, dict)
                or finalization.get('discard_result') is not True
                or finalization.get('cancelled') is not True
                or capture.get('reason') != finalization.get('reason')
                or not isinstance(artifacts, list) or len(artifacts) > 16):
            raise lifecycle.error('Result schema 2 is malformed or mismatched; history preserved.', 73)
        if (isinstance(lease_finalization, dict)
                and lease_finalization.get('reason_code') != capture['reason_code']):
            raise lifecycle.error(
                'Retained discard reason conflicts with its finalization request.', 73)
        seen = set()
        for artifact in artifacts:
            if (not isinstance(artifact, dict)
                    or set(artifact) != {'file', 'bytes', 'sha256', 'attempt_id', 'provenance'}
                    or artifact.get('file') != 'changes.patch'
                    or artifact['file'] in seen
                    or type(artifact.get('bytes')) is not int or artifact['bytes'] < 0
                    or not isinstance(artifact.get('sha256'), str)
                    or re.fullmatch('[0-9a-f]{64}', artifact['sha256']) is None
                    or artifact.get('provenance') not in
                       ('verified_attempt', 'legacy_unattributed')
                    or not _valid_attempt_id(artifact.get('attempt_id'))
                    or (artifact['provenance'] == 'verified_attempt')
                       != (artifact.get('attempt_id') is not None)):
                raise lifecycle.error('Historical result artifact is malformed; history preserved.', 73)
            actual = _artifact_digest(fd, artifact['file'])
            if (actual is None or actual[0] != artifact['bytes']
                    or actual[1] != artifact['sha256']):
                raise lifecycle.error('Historical result artifact failed its integrity check.', 73)
            if (artifact['provenance'] == 'verified_attempt'
                    and artifact['attempt_id'] not in attempt_ids):
                raise lifecycle.error('Historical artifact attempt is absent from durable job history.', 73)
            seen.add(artifact['file'])
        if _artifact_digest(fd, 'changes.patch') is not None and 'changes.patch' not in seen:
            raise lifecycle.error('Retained historical patch is missing from the result manifest.', 73)
        return result
    raise lifecycle.error('Unknown durable result schema; history preserved.', 73)


def _read_result_document(fd):
    raw = lifecycle.read_file(fd, 'result.json', maximum=MAX_RESULT_BYTES)
    if raw is None:
        return None
    try:
        result = json.loads(raw)
    except (ValueError, TypeError, UnicodeError):
        raise lifecycle.error('Malformed durable result manifest; history preserved.', 73) from None
    if not isinstance(result, dict):
        raise lifecycle.error('Malformed durable result manifest; history preserved.', 73)
    return result


def _stale_uncommitted_result(copy, result, *, fingerprint, lease):
    """Recognize a schema-1 historical manifest already superseded on disk.

    A crash may publish new ``changes.patch`` bytes after capture but before
    the terminal lease and manifest. The old manifest then describes bytes that
    no longer exist. Only that exact state is recognized here: every identity,
    baseline and patch field must still be valid, no finalization identity may
    be present, and the retained artifact must be present but provably
    different. The caller recaptures from the owned work tree or resumes from
    committed capture evidence; it never adopts this manifest.
    """
    if (not isinstance(result, dict) or type(result.get('schema')) is not int
            or result['schema'] != 1 or result.get('finalization') is not None):
        return False
    if set(result) - {'schema', 'workspace_id', 'source', 'base_head', 'patch',
                      'job_id', 'attempt_id', 'finalization'}:
        return False
    expected_head = (copy.metadata.get('base_head')
                     or copy.metadata.get('source_head'))
    patch = result.get('patch')
    if (result.get('workspace_id') != copy.id
            or result.get('source') != str(copy.source)
            or result.get('base_head') != expected_head
            or not isinstance(patch, dict)
            or set(patch) != {'file', 'bytes', 'sha256'}
            or patch.get('file') != 'changes.patch'
            or type(patch.get('bytes')) is not int or patch['bytes'] < 0
            or patch['bytes'] > MAX_PATCH_BYTES
            or not isinstance(patch.get('sha256'), str)
            or re.fullmatch('[0-9a-f]{64}', patch['sha256']) is None):
        return False
    if 'attempt_id' in result and not _valid_attempt_id(result['attempt_id']):
        return False
    if 'job_id' in result:
        if re.fullmatch('[0-9a-f]{32}', str(result['job_id'])) is None:
            return False
        if lease is not None and result['job_id'] != lease['job_id']:
            return False
    # Missing bytes are a different lost-artifact state and stay fail-closed.
    # The content digest must differ; a wrong length with the current digest is
    # malformed evidence, not an interrupted capture, and also stays closed.
    return fingerprint is not None and fingerprint[1] != patch['sha256']


def read_result(copy, *, fd=None, lease=None):
    """Read and validate schema 1/2, or return ``None`` when absent.

    A new reader must never reinterpret an unknown schema.  Older binaries are
    not required to understand schema 2; this release fails closed instead of
    guessing from a filename or a stray ``changes.patch``.
    """
    if fd is None:
        with lifecycle.directory(copy) as directory_fd:
            return read_result(copy, fd=directory_fd, lease=lease)
    result = _read_result_document(fd)
    if result is None:
        return None
    if lease is None and isinstance(result, dict) and result.get('schema') == 2:
        lease = lifecycle.read(copy)
    return _validate_result_document(copy, result, fd, lease=lease)

def inspect_workspace(state, identifier):
    """History/result inspection works after the disposable checkout is gone."""
    state = state_storage.state_root(state)
    copy = _load_record(state, identifier)
    lease = lifecycle.read(copy)
    with lifecycle.directory(copy) as fd:
        present = lifecycle._exists(fd, 'work')
        result = read_result(copy, fd=fd, lease=lease)
    return {'id': copy.id, 'path': str(copy.path), 'metadata': copy.metadata,
            'lease': lease, 'result': result, 'execution_present': present}

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
    report = {'id': copy.id, 'job_id': lease['job_id'], 'action': 'cleaned',
              'reason': lease.get('final_reason', 'finalized'),
              'allocated_bytes': pending.get('allocated_bytes', 0),
              'inodes': pending.get('inodes', 0)}
    report.update(_finalization_report(lease))
    return report


def _finalization_report(lease):
    finalization = (lease or {}).get('finalization')
    if not isinstance(finalization, dict):
        return {}
    return {'discarded': bool(finalization.get('discarded')),
            'reason_code': finalization.get('reason_code'),
            'result_schema': finalization.get('result_schema')}


def _extra_name(name):
    return name == 'preparation-home' or re.fullmatch(r'(?:runtime|session)-[A-Za-z0-9_-]+', name)


def _valid_extra(entry):
    return (isinstance(entry, dict) and isinstance(entry.get('source'), str)
            and _extra_name(entry['source'])
            and re.fullmatch('[0-9a-f]{32}', str(entry.get('name', '')))
            and type(entry.get('device')) is int and type(entry.get('inode')) is int)


def _historical_artifacts(fd, copy, prior):
    """List retained pre-discard bytes without claiming a missing current patch."""
    artifact = _artifact_digest(fd, 'changes.patch')
    if artifact is None:
        return []
    size, digest = artifact
    attempt_id, provenance = None, 'legacy_unattributed'
    if isinstance(prior, dict) and prior.get('schema') == 1:
        patch = prior.get('patch')
        candidate = prior.get('attempt_id')
        if (prior.get('workspace_id') == copy.id
                and isinstance(patch, dict) and patch.get('file') == 'changes.patch'
                and patch.get('bytes') == size and patch.get('sha256') == digest
                and isinstance(candidate, str)
                and re.fullmatch('[0-9a-f]{32}', candidate) is not None
                and candidate in {attempt['id'] for attempt in
                                  (lifecycle.read(copy) or {}).get('attempts', [])}):
            attempt_id, provenance = candidate, 'verified_attempt'
    return [{'file': 'changes.patch', 'bytes': size, 'sha256': digest,
             'attempt_id': attempt_id, 'provenance': provenance}]


def _validate_capture_evidence(value):
    if not isinstance(value, dict) or set(value) - {'bytes', 'sha256', 'attempt_id'}:
        return False
    if (type(value.get('bytes')) is not int or value['bytes'] < 0
            or value['bytes'] > MAX_PATCH_BYTES
            or not isinstance(value.get('sha256'), str)
            or re.fullmatch('[0-9a-f]{64}', value['sha256']) is None
            or not _valid_attempt_id(value.get('attempt_id'))):
        return False
    return True


def _result_from_lease(copy, lease, fd, prior, request):
    """Rebuild a manifest committed to the terminal lease before publication.

    The capture identity is written before ``result.json``. A process crash in
    that narrow gap therefore resumes publication without running capture or a
    model again. Any byte mismatch fails closed; this never guesses a patch.
    """
    finalization = lease.get('finalization')
    if not isinstance(finalization, dict) or 'result_schema' not in finalization:
        return None
    schema = finalization['result_schema']
    common = {'workspace_id': copy.id, 'source': str(copy.source),
              'finalization': dict((key, request[key]) for key in (
                  'request_id', 'at', 'cancelled', 'discard_result', 'reason'))}
    if 'job_id' in lease:
        common['job_id'] = lease['job_id']
    if schema == 1:
        capture = finalization.get('capture')
        if not _validate_capture_evidence(capture):
            raise lifecycle.error(
                'Durable capture evidence is malformed; history preserved.', 73)
        actual = _artifact_digest(fd, 'changes.patch')
        if (actual is None or actual[0] != capture['bytes']
                or actual[1] != capture['sha256']):
            raise lifecycle.error(
                'Durable capture bytes do not match their fingerprint; copy preserved.', 73)
        result = dict(common, schema=1,
                      base_head=(copy.metadata.get('base_head')
                                 or copy.metadata.get('source_head')),
                      patch={'file': 'changes.patch', 'bytes': capture['bytes'],
                             'sha256': capture['sha256']})
        attempt_id = capture.get('attempt_id')
        if attempt_id is None:
            candidate = copy.metadata.get('attempt_id')
            attempt_id = (candidate if isinstance(candidate, str)
                          and re.fullmatch('[0-9a-f]{32}', candidate) else None)
        if attempt_id is not None:
            result['attempt_id'] = attempt_id
        return result
    if schema == 2:
        if (finalization.get('discarded') is not True
                or finalization.get('discard_result') is not True
                or finalization.get('cancelled') is not True
                or finalization.get('reason_code') not in DISCARD_REASONS):
            raise lifecycle.error(
                'Durable discard evidence is incomplete; history preserved.', 73)
        result = dict(common, schema=2,
                      base_head=copy.metadata.get('base_head'), patch=None,
                      capture={'status': 'discarded',
                               'reason_code': finalization['reason_code'],
                               'reason': request['reason'],
                               'baseline_status': ('recorded'
                                   if copy.metadata.get('base_head') else 'unavailable')},
                      historical_artifacts=_historical_artifacts(fd, copy, prior))
        attempt_id = copy.metadata.get('attempt_id')
        if isinstance(attempt_id, str) and re.fullmatch('[0-9a-f]{32}', attempt_id):
            result['attempt_id'] = attempt_id
        return result
    raise lifecycle.error('Durable result schema is unsupported; history preserved.', 73)


def _validate_lease_finalization(value):
    if value is None:
        return
    allowed = {'request_id', 'at', 'cancelled', 'discard_result', 'reason',
               'result_schema', 'reason_code', 'discarded', 'capture'}
    if (not isinstance(value, dict) or set(value) - allowed
            or not re.fullmatch('[0-9a-f]{32}', str(value.get('request_id', '')))
            or type(value.get('at')) not in (int, float) or not math.isfinite(value['at'])
            or type(value.get('cancelled')) is not bool
            or type(value.get('discard_result')) is not bool
            or value['discard_result'] and not value['cancelled']
            or not isinstance(value.get('reason'), str)
            or not value['reason'].strip() or len(value['reason']) > 1000
            or ('result_schema' in value
                and (type(value['result_schema']) is not int
                     or value['result_schema'] not in (1, 2)))
            or ('discarded' in value and type(value['discarded']) is not bool)
            or ('discarded' in value and 'result_schema' not in value)
            or ('reason_code' in value and value['reason_code'] not in DISCARD_REASONS)
            or ('reason_code' in value and 'result_schema' not in value)
            or (value.get('result_schema') == 2 and value.get('discarded') is not True)
            or (value.get('result_schema') == 1 and value.get('discarded') is not False)
            or (value.get('result_schema') == 1 and 'reason_code' in value)
            or (value.get('result_schema') == 2
                and value.get('reason_code') not in DISCARD_REASONS)
            or (value.get('result_schema') == 2 and value.get('discard_result') is not True)
            or ('capture' in value and not _validate_capture_evidence(value['capture']))
            or ('capture' in value and value.get('result_schema') != 1)):
        raise lifecycle.error('Logical job finalization request is malformed; copy preserved.', 73)


def _ensure_finalization_request(lease, *, reason, cancelled, discard_result,
                                 result_present):
    existing = lease.get('finalization')
    _validate_lease_finalization(existing)
    if existing is None:
        return ({'request_id': uuid.uuid4().hex, 'at': time.time(),
                 'cancelled': bool(cancelled), 'discard_result': bool(discard_result),
                 'reason': reason}, True)
    if existing['cancelled'] != cancelled:
        raise lifecycle.error('Finalization cannot change the original cancellation decision.', 73)
    if 'result_schema' in existing:
        # The result identity is already committed to the terminal lease. A
        # repeated call cannot rewrite its schema or destructive decision.
        if discard_result and existing.get('discard_result') is not True:
            raise lifecycle.error(
                'A committed captured result cannot be replaced by an explicit discard.', 73)
        return existing, False
    if discard_result and not existing['discard_result']:
        if result_present:
            raise lifecycle.error('A published result cannot be replaced by an explicit discard.', 73)
        return (dict(existing, discard_result=True, reason=reason, at=time.time()), True)
    # A durable destructive request remains authoritative across recovery. A
    # caller repeating cancellation does not have to pass the flag again.
    return existing, False


def _result_matches_request(result, request):
    finalization = (result or {}).get('finalization')
    return (isinstance(finalization, dict) and isinstance(request, dict)
            and all(finalization.get(key) == request.get(key)
                    for key in ('request_id', 'at', 'cancelled',
                                'discard_result', 'reason')))


def _prior_is_finalized(prior, lease):
    """Classify a manifest already present before this finalization attempt.

    A schema 1 manifest without a finalization identity is historical evidence
    (the completed execution's patch), not proof that a newer destructive
    transaction has already published. A manifest carrying an identity must
    match the durable lease request exactly; schema 2 is only meaningful after
    such a request.
    """
    if prior is None:
        return False
    finalization = (lease or {}).get('finalization')
    prior_finalization = prior.get('finalization')
    if prior_finalization is not None:
        if not _result_matches_request(prior, finalization):
            raise lifecycle.error(
                'An existing durable result has no matching finalization request; copy preserved.', 73)
        return True
    if prior.get('schema') == 1:
        return False
    raise lifecycle.error(
        'Discarded history has no matching finalization request; copy preserved.', 73)


def finalize(copy, *, reason='accepted', cancelled=False, orphan=False,
             before_finalize=None, discard_result=False):
    # Keep the owner lock through the entire (possibly interrupted) rmtree,
    # while releasing the creation fence before recursive deletion.
    with ExitStack() as owned:
        with lifecycle.fence(copy.directory.parent.parent):
            fd = owned.enter_context(_owner(copy))
        return _finalize(copy, reason=reason, cancelled=cancelled, orphan=orphan,
                         owner_fd=fd, before_finalize=before_finalize,
                         discard_result=discard_result)


def _finalize(copy, *, reason, cancelled, orphan, owner_fd, before_finalize,
              discard_result=False):
    """Publish the result, end this job lease, then remove its execution tree.

    Caller owns final integration/review. Failure before publication preserves
    the execution tree; interruption after publication or rename leaves durable
    identities so a repeat repairs cleanup without recapturing or rewriting
    confirmed history. ``discard_result`` is valid only for cancellation, and
    only a typed ``CaptureFailure`` reaches that branch.
    """
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 1000:
        raise lifecycle.error('Finalization requires a bounded reason.', 64)
    if type(discard_result) is not bool:
        raise lifecycle.error('discard_result must be a boolean.', 64)
    if discard_result and not cancelled:
        raise lifecycle.error('--discard-result is valid only for cancellation.', 64)
    if discard_result and reason.strip().lower() == 'cancelled':
        raise lifecycle.error('Explicit discard requires a non-default reason.', 64)
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
            bound = (isinstance(lease, dict)
                     and isinstance(lease.get('binding'), dict))
            linked_metadata = bool(copy.metadata.get('coord_task')
                                   and copy.metadata.get('coord_assignment'))
            if (cancelled and before_finalize is None
                    and (bound or (discard_result and linked_metadata))):
                raise lifecycle.error(
                    'Linked cancellation must use coordination finalize --cancel; '
                    'copy preserved.', 64)
            if lease and lease.get('cleanup', {}).get('state') == 'cleaned':
                if lifecycle._exists(fd, 'work'):
                    raise lifecycle.error('Cleaned job has a replacement execution tree; data preserved.', 73)
                finalization = lease.get('finalization')
                if discard_result and (not isinstance(finalization, dict)
                        or finalization.get('discard_result') is not True):
                    raise lifecycle.error(
                        'A completed job cannot be reopened or replaced by an explicit discard.', 73)
                if before_finalize is not None:
                    before_finalize()
                report = {'id': copy.id, 'action': 'already-cleaned',
                          'reason': lease.get('final_reason', reason),
                          'allocated_bytes': 0, 'inodes': 0}
                report.update(_finalization_report(lease))
                return report
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
                lease = lease or lifecycle.initialize(copy)
                raw_prior = _read_result_document(fd)
                prior = None
                if raw_prior is not None:
                    try:
                        prior_lease = (lease if isinstance(raw_prior, dict)
                                       and raw_prior.get('schema') == 2 else None)
                        prior = _validate_result_document(copy, raw_prior, fd, lease=prior_lease)
                    except WorkspaceError:
                        committed = lease.get('finalization')
                        capture = (committed.get('capture')
                                   if isinstance(committed, dict) else None)
                        fingerprint = _artifact_digest(fd, 'changes.patch')
                        stale = _stale_uncommitted_result(
                            copy, raw_prior, fingerprint=fingerprint, lease=lease)
                        # After a terminal capture, the lease fingerprint is the
                        # only authority. Before that, a stale uncommitted
                        # manifest is rebuilt by a fresh capture from the owned
                        # tree. No other validation failure is tolerated.
                        resumed = (
                            stale
                            and isinstance(committed, dict)
                            and committed.get('result_schema') == 1
                            and _validate_capture_evidence(capture)
                            and fingerprint == (capture['bytes'], capture['sha256']))
                        uncommitted = stale and (
                            committed is None
                            or (isinstance(committed, dict)
                                and 'result_schema' not in committed))
                        if not (resumed or uncommitted):
                            raise
                        prior = None
                prior_finalized = _prior_is_finalized(prior, lease)
                request, changed = _ensure_finalization_request(
                    lease, reason=reason, cancelled=cancelled,
                    discard_result=discard_result, result_present=prior_finalized)
                if before_finalize is not None:
                    # Attestation writes the durable ledger intent. It runs before
                    # this copy's lease changes so a refused review leaves both
                    # the ledger and the copy byte-identical.
                    before_finalize()
                    lease = lifecycle.read(copy)
                    refreshed, _ = _ensure_finalization_request(
                        lease, reason=reason, cancelled=cancelled,
                        discard_result=discard_result, result_present=prior_finalized)
                    if (isinstance(lease.get('finalization'), dict)
                            and lease['finalization'].get('request_id') != request['request_id']):
                        raise lifecycle.error('Finalization request changed during attestation; copy preserved.', 73)
                    request = refreshed
                if changed or not isinstance(lease.get('finalization'), dict):
                    lease['finalization'] = request
                    lifecycle.write(copy, lease)
                effective_discard = request['discard_result'] is True
                committed = lease.get('finalization')
                committed_schema = (committed.get('result_schema')
                                    if isinstance(committed, dict) else None)
                if prior_finalized:
                    if (committed_schema is not None
                            and prior['schema'] != committed_schema):
                        raise lifecycle.error(
                            'Retained result schema conflicts with its terminal lease.', 73)
                    if (isinstance(committed, dict)
                            and committed.get('discarded') is not None
                            and bool(committed['discarded']) != (prior['schema'] == 2)):
                        raise lifecycle.error(
                            'Retained discard state conflicts with its terminal lease.', 73)
                    result = prior
                elif committed_schema is not None:
                    # Capture identity is durable in the terminal lease. Resume
                    # publication without another model run or capture.
                    if committed_schema == 2 and not effective_discard:
                        raise lifecycle.error(
                            'A committed discarded result cannot be replaced by a capture.', 73)
                    result = _result_from_lease(copy, lease, fd, prior, request)
                else:
                    try:
                        patch = _capture(copy)
                    except CaptureFailure as failure:
                        if not effective_discard:
                            raise
                        result = {'schema': 2, 'workspace_id': copy.id,
                                  'source': str(copy.source),
                                  'base_head': copy.metadata.get('base_head'),
                                  'patch': None,
                                  'capture': {
                                      'status': 'discarded',
                                      'reason_code': failure.reason_code,
                                      'reason': request['reason'],
                                      'baseline_status': ('recorded'
                                          if copy.metadata.get('base_head') else 'unavailable')},
                                  'historical_artifacts': _historical_artifacts(fd, copy, prior),
                                  'finalization': dict((key, request[key]) for key in (
                                      'request_id', 'at', 'cancelled', 'discard_result', 'reason'))}
                    else:
                        lifecycle.atomic_file(fd, 'changes.patch', patch)
                        result = {'schema': 1, 'workspace_id': copy.id,
                                  'source': str(copy.source),
                                  'base_head': (copy.metadata.get('base_head')
                                                or copy.metadata.get('source_head')),
                                  'patch': {'file': 'changes.patch', 'bytes': len(patch),
                                            'sha256': hashlib.sha256(patch).hexdigest()},
                                  'finalization': dict((key, request[key]) for key in (
                                      'request_id', 'at', 'cancelled', 'discard_result', 'reason'))}
                    if lease is not None:
                        result['job_id'] = lease['job_id']
                    attempt_id = copy.metadata.get('attempt_id')
                    if isinstance(attempt_id, str) and re.fullmatch('[0-9a-f]{32}', attempt_id):
                        result['attempt_id'] = attempt_id
                # The manifest and retained bytes are validated before the
                # terminal lease makes the loss or capture durable.
                _validate_result_document(copy, result, fd)
                if lease['phase'] not in lifecycle.FINAL_PHASES:
                    lease['phase'] = 'cancelled' if cancelled else 'finalized'
                    lease['final_reason'] = request['reason']
                    lease['events'].append({'event': lease['phase'], 'at': time.time(),
                                            'reason': request['reason']})
                # Idempotent recovery reuses its original quarantine name/inode.
                pending = lease.get('cleanup') or {'name': uuid.uuid4().hex,
                    'device': identity[0], 'inode': identity[1],
                    'allocated_bytes': usage[0] + sum(e['allocated_bytes'] for e in extras),
                    'inodes': usage[1] + sum(e['inodes'] for e in extras),
                    'extras': extras, 'state': 'pending'}
                if (pending['device'], pending['inode']) != identity:
                    raise lifecycle.error('Cleanup intent does not match execution inode.', 73)
                lease['cleanup'] = pending
                terminal = dict(lease['finalization'],
                                result_schema=result['schema'],
                                discarded=result['schema'] == 2)
                if result['schema'] == 1:
                    patch = result['patch']
                    terminal['capture'] = {
                        'bytes': patch['bytes'], 'sha256': patch['sha256'],
                        'attempt_id': result.get('attempt_id')}
                    terminal.pop('reason_code', None)
                else:
                    terminal['reason_code'] = result['capture']['reason_code']
                    terminal.pop('capture', None)
                lease['finalization'] = terminal
                # Durable request, terminal lease and cleanup intent precede
                # manifest publication and deletion.
                lifecycle.write(copy, lease)
                if not prior_finalized:
                    lifecycle.atomic_file(fd, 'result.json',
                                          (json.dumps(result) + '\n').encode())
                read_result(copy, fd=fd, lease=lease)
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
                result = read_result(copy, fd=fd, lease=lease)
                if (result is None and isinstance(lease.get('finalization'), dict)
                        and 'result_schema' in lease['finalization']):
                    raise lifecycle.error(
                        'Terminal result manifest is missing; history preserved.', 73)
                if (result is not None and isinstance(lease.get('finalization'), dict)
                        and not _result_matches_request(result, lease['finalization'])):
                    raise lifecycle.error('Retained result does not match its finalization request.', 73)
                quarantine_fd = _quarantine(root_fd, state)
        # The global admission fence is intentionally not held during deletion.
        return _finish_cleanup(copy, lease, quarantine_fd)
    finally:
        if quarantine_fd is not None:
            os.close(quarantine_fd)


def cancel(copy, *, reason='cancelled', discard_result=False):
    return finalize(copy, reason=reason, cancelled=True, discard_result=discard_result)

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
