"""Provider-free storage measurement and unmaterialized allocation reservations.

The basic ``reserve(state, ...)`` call keeps its lock and records in the state
directory for compatibility.  Callers that manage multiple state roots on one
filesystem should pass an existing ``allocation_path`` plus a common
``pool_dir``.  The allocation target is pinned and measured with ``fstatvfs``;
records are tagged with that descriptor's real ``st_dev`` and only matching
devices consume one another's capacity.  ``shared_pool_dir`` returns a private
XDG/HOME-config pool for a target filesystem.  This module does not modify
worker slots and cannot enforce admission by itself: the coordinator must hold
a reservation around checkout, toolchain and temporary allocations before they
materialize.
"""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import re
import shlex
import stat
import uuid

from .state_storage import StorageError, open_private_directory


__all__ = ('BudgetError', 'Reservation', 'measure', 'reserve', 'shared_pool_dir',
           'check_pool_readiness', 'MINIMUM_FREE_BYTES',
           'MINIMUM_FREE_INODES')

MINIMUM_FREE_BYTES = 256 * 1024 * 1024
MINIMUM_FREE_INODES = 1024
BUDGET_LOCK = 'budget.lock'
RECORD_PREFIX = 'budget-'
RECORD_SUFFIX = '.reservation'

_MAX_QUANTITY = (1 << 63) - 1
_RECORD_LIMIT = 4096
_RECORD_SCHEMA = 2
_RECORD_NAME = re.compile(r'^budget-([0-9a-f]{32})\.reservation$')
_MOUNT_ESCAPE = re.compile(r'\\([0-7]{3})')
_PRIVATE_RECORD = 'Reservation records require private owned ordinary files (600).'
_POOL_PARTS = ('deepseek-team', 'storage-budget')


class BudgetError(Exception):
    """Invalid arguments (64), unavailable budget (75) or unsafe storage (78)."""

    def __init__(self, message: str, code: int = 78, *, errno=None):
        self.message, self.code, self.errno = message, code, errno
        super().__init__(message)


def _quantity(name: str, value, *, code: int = 64) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise BudgetError(f'{name} must be a non-negative finite integer.', code)
    if value < 0:
        raise BudgetError(f'{name} must be a non-negative finite integer.', code)
    if value > _MAX_QUANTITY:
        raise BudgetError(f'{name} is too large to reserve safely.', code)
    return value


def _display(path) -> str:
    return json.dumps(str(path), ensure_ascii=True)


def _unsafe(path, reason: str) -> BudgetError:
    return BudgetError(f'Unsafe storage path {reason}: {_display(path)}. '
                       'Existing entries were not changed.', 78)


_POOL_FOREIGN_REMEDY = (
    'Configure XDG_CONFIG_HOME so the pool is current-user-owned and its '
    'ancestry is current-user-owned or root-owned and not writable by others. '
    'An administrator may correct a foreign-owned entry; it was not changed.')
_POOL_SYMLINK_REMEDY = (
    'Inspect the symbolic link and remove or replace it with an ordinary '
    'current-user-owned directory only if that is intended, or configure '
    'XDG_CONFIG_HOME to another current-user-owned path. The link was not '
    'followed or changed.')
_POOL_NONDIRECTORY_REMEDY = (
    'Inspect the non-directory entry and move or replace it only if that is '
    'intended, or configure XDG_CONFIG_HOME to another current-user-owned path. '
    'The entry was not changed.')
_POOL_INACCESSIBLE_REMEDY = (
    'Check traversal/search access on the existing ancestors or configure '
    'XDG_CONFIG_HOME to another current-user-owned path. No entry was changed.')
_POOL_UNVERIFIED_REMEDY = (
    'Inspect the existing pool path and XDG_CONFIG_HOME, or configure another '
    'current-user-owned path. No entry was changed.')
_POOL_CREATE_REMEDY = (
    'Configure XDG_CONFIG_HOME to a safe, writable current-user-owned path '
    'with search access. No entry was changed.')


def _pool_unsafe(path, reason: str, remedy: str) -> BudgetError:
    return BudgetError(f'Unsafe storage budget pool {reason}: {_display(path)}. '
                       f'{remedy}', 78)


def _pool_permission_remedy(path, *, final: bool) -> str:
    command = 'chmod 700 ' if final else 'chmod go-w '
    return ('Fix the current-user-owned path only after review with a '
            f'nonrecursive command: {command}{shlex.quote(str(path))}. '
            'Existing entries were not changed.')


def _pool_effective_mode_bits(info) -> int:
    """Return permission bits that apply to the effective user for a directory."""
    mode = stat.S_IMODE(info.st_mode)
    if info.st_uid == os.geteuid():
        return (mode >> 6) & 0o7
    try:
        groups = set(os.getgroups())
    except OSError:
        groups = set()
    groups.add(os.getegid())
    if info.st_gid in groups:
        return (mode >> 3) & 0o7
    return mode & 0o7


def _pool_create_error(path, pool, reason: str, *, existing: bool) -> BudgetError:
    if existing:
        detail = f'cannot create lock or reservation entries in {_display(path)}'
    else:
        detail = (f'cannot create missing pool {_display(pool)} below its nearest '
                  f'existing ancestor {_display(path)}')
    return BudgetError(f'Unsafe storage budget pool {detail}: {reason}. '
                       f'{_POOL_CREATE_REMEDY}', 78)


def _check_pool_create_ready(fd: int, path, pool, *, existing: bool) -> None:
    """Prove a pinned directory can create entries without probing or repair."""
    try:
        info = os.fstat(fd)
    except OSError:
        raise _pool_create_error(
            path, pool, 'directory readiness could not be verified',
            existing=existing) from None
    if not stat.S_ISDIR(info.st_mode):
        raise _pool_create_error(
            path, pool, 'the entry is not an ordinary directory',
            existing=existing)
    mode = _pool_effective_mode_bits(info)
    if mode & 0o3 != 0o3:
        raise _pool_create_error(
            path, pool,
            f'effective directory mode {mode:o} lacks write and search access',
            existing=existing)
    try:
        flags = int(os.fstatvfs(fd).f_flag)
    except (AttributeError, OSError, OverflowError, TypeError, ValueError):
        raise _pool_create_error(
            path, pool, 'filesystem write readiness could not be verified',
            existing=existing) from None
    if flags & getattr(os, 'ST_RDONLY', 1):
        raise _pool_create_error(
            path, pool, 'the filesystem is mounted read-only', existing=existing)
    try:
        accessible = os.access(
            '.', os.W_OK | os.X_OK, dir_fd=fd, effective_ids=True,
            follow_symlinks=False)
    except (NotImplementedError, OSError, TypeError, ValueError):
        accessible = False
    if not accessible:
        raise _pool_create_error(
            path, pool,
            'the effective user lacks actual write and search access',
            existing=existing)


def _identity(info) -> tuple[int, int]:
    return info.st_dev, info.st_ino


def _check_private_file(info, label: str) -> None:
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o600):
        raise BudgetError(f'{label}: {_PRIVATE_RECORD}', 78)


def _safe_ancestor(path: Path, info, *, pool: bool = False) -> None:
    if not stat.S_ISDIR(info.st_mode):
        if pool:
            raise _pool_unsafe(path, 'has a non-directory ancestor',
                               _POOL_NONDIRECTORY_REMEDY)
        raise _unsafe(path, 'has a non-directory ancestor')
    if info.st_uid not in (0, os.geteuid()):
        if pool:
            raise _pool_unsafe(
                path, f'has a foreign-owned ancestor (uid {info.st_uid})',
                _POOL_FOREIGN_REMEDY)
        raise _unsafe(path, f'has a foreign-owned ancestor (uid {info.st_uid})')
    if info.st_mode & 0o022:
        if info.st_uid == 0 and info.st_mode & stat.S_ISVTX:
            return
        if pool:
            if info.st_uid == os.geteuid():
                raise _pool_unsafe(
                    path, 'has a writable ancestor',
                    _pool_permission_remedy(path, final=False))
            raise _pool_unsafe(
                path, 'has a writable ancestor',
                'Use another current-user-owned XDG_CONFIG_HOME. An entry '
                'owned by another user was not changed and its permissions '
                'must not be altered here.')
        raise _unsafe(path, 'has a writable ancestor')


def _check_pool_ancestors(path: Path):
    """Validate pinned pool ancestry and prove a missing tail is creatable.

    Return the existing final entry's ``lstat`` result, or ``None`` after
    proving the nearest existing ancestor can create the missing components.
    """
    parent = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    parent_path = Path('/')
    try:
        parts = path.parts[1:]
        if not parts:
            return os.fstat(parent)
        for part in parts[:-1]:
            current = parent_path / part
            try:
                info = os.stat(part, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                _check_pool_create_ready(
                    parent, parent_path, path, existing=False)
                return None
            except (OSError, ValueError):
                raise _pool_unsafe(
                    current, 'has an inaccessible ancestor',
                    f'Pool path: {_display(path)}. '
                    + _POOL_INACCESSIBLE_REMEDY) from None
            if stat.S_ISLNK(info.st_mode):
                raise _pool_unsafe(
                    current, 'has a symbolic link in its ancestry',
                    f'Pool path: {_display(path)}. ' + _POOL_SYMLINK_REMEDY)
            try:
                _safe_ancestor(current, info, pool=True)
            except BudgetError as error:
                raise BudgetError(
                    f'{error.message} Pool path: {_display(path)}.',
                    error.code) from None
            child, opened = _open_measured_directory(parent, part, current)
            if _identity(opened) != _identity(info):
                os.close(child)
                problem = _pool_unsafe(
                    current, 'changed during its safety check',
                    _POOL_UNVERIFIED_REMEDY)
                raise BudgetError(
                    f'{problem.message} Pool path: {_display(path)}.',
                    problem.code) from None
            os.close(parent)
            parent = child
            parent_path = current

        final = parts[-1]
        final_path = parent_path / final
        try:
            return os.stat(final, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            _check_pool_create_ready(parent, parent_path, path, existing=False)
            return None
        except OSError as error:
            problem = _pool_unsafe(
                final_path, 'is missing or inaccessible',
                f'Pool path: {_display(path)}. ' + _POOL_UNVERIFIED_REMEDY)
            problem.errno = error.errno
            raise problem from None
    finally:
        os.close(parent)


def _pool_info_problem(path: Path, info) -> BudgetError | None:
    if stat.S_ISLNK(info.st_mode):
        return _pool_unsafe(path, 'is a symbolic link', _POOL_SYMLINK_REMEDY)
    if not stat.S_ISDIR(info.st_mode):
        return _pool_unsafe(path, 'is not an ordinary directory',
                            _POOL_NONDIRECTORY_REMEDY)
    if info.st_uid != os.geteuid():
        return _pool_unsafe(
            path, f'has a foreign owner (uid {info.st_uid})',
            _POOL_FOREIGN_REMEDY)
    if stat.S_IMODE(info.st_mode) != 0o700:
        return _pool_unsafe(
            path, f'has unsafe permissions ({stat.S_IMODE(info.st_mode):o}; '
            'expected 700)', _pool_permission_remedy(path, final=True))
    return None


def _pool_existing_error(path: Path) -> BudgetError:
    try:
        info = os.stat(path, follow_symlinks=False)
    except FileNotFoundError:
        return _pool_unsafe(path, 'is missing or inaccessible',
                            _POOL_UNVERIFIED_REMEDY)
    except OSError as error:
        problem = _pool_unsafe(path, 'is missing or inaccessible',
                               _POOL_INACCESSIBLE_REMEDY)
        problem.errno = error.errno
        return problem
    problem = _pool_info_problem(path, info)
    return problem or _pool_unsafe(path, 'could not be validated safely',
                                   _POOL_UNVERIFIED_REMEDY)


def _mountpoint_below(target: Path) -> bool:
    """Detect bind mounts too, which do not change ``st_dev``."""
    try:
        with open('/proc/self/mountinfo', encoding='utf-8', errors='replace') as stream:
            lines = stream.readlines()
    except OSError as error:
        raise BudgetError('Cannot verify nested mount identity because '
                          '/proc/self/mountinfo is unavailable; refusing to measure '
                          'a directory without same-device bind-mount safety.', 78,
                          errno=error.errno) from None
    for line in lines:
        fields = line.split()
        if len(fields) < 5:
            continue
        mountpoint = _MOUNT_ESCAPE.sub(
            lambda match: chr(int(match.group(1), 8)), fields[4])
        try:
            relative = Path(mountpoint).relative_to(target)
        except ValueError:
            continue
        if relative.parts:
            return True
    return False


def _open_measured_directory(parent: int, name: str, path: Path, *,
                             require_current_owner: bool = False):
    try:
        child = os.open(name,
                        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                        dir_fd=parent)
    except (OSError, ValueError):
        raise _unsafe(path, 'is missing or inaccessible') from None
    try:
        opened = os.fstat(child)
        try:
            listed = os.stat(name, dir_fd=parent, follow_symlinks=False)
        except (OSError, ValueError):
            raise _unsafe(path, 'changed during its safety check') from None
        if _identity(opened) != _identity(listed):
            raise _unsafe(path, 'changed during its safety check')
        if require_current_owner and opened.st_uid != os.geteuid():
            raise _unsafe(path, f'has a foreign owner (uid {opened.st_uid})')
        return child, opened
    except BaseException:
        os.close(child)
        raise


def measure(path: Path) -> dict[str, int]:
    """Return actual allocated bytes and unique inodes for an ordinary tree.

    Every entry is inspected with ``lstat`` and symbolic links are counted but
    never followed.  A hard-linked inode is counted once.  Directory traversal
    is descriptor-relative and stops with :class:`BudgetError` rather than
    crossing a device boundary, accepting a foreign owner, or traversing a
    symbolic or writable path component.
    """
    try:
        target = Path(path).absolute()
    except (TypeError, ValueError, OSError):
        raise BudgetError('path must be a filesystem path.', 64) from None
    if '..' in target.parts:
        raise _unsafe(target, 'contains parent traversal')

    parent = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    final_dir = None
    try:
        parts = target.parts[1:]
        if not parts:
            root_info = os.fstat(parent)
        else:
            current = Path('/')
            root_info = None
            for index, part in enumerate(parts):
                current = current / part
                try:
                    info = os.stat(part, dir_fd=parent, follow_symlinks=False)
                except (OSError, ValueError):
                    raise _unsafe(current, 'is missing or inaccessible') from None
                if stat.S_ISLNK(info.st_mode):
                    raise _unsafe(current, 'contains a symbolic link in its ancestry')
                last = index == len(parts) - 1
                if not last:
                    _safe_ancestor(current, info)
                    child, opened = _open_measured_directory(parent, part, current)
                    os.close(parent)
                    parent, info = child, opened
                    continue
                if info.st_uid != os.geteuid():
                    raise _unsafe(current, f'has a foreign owner (uid {info.st_uid})')
                if stat.S_ISDIR(info.st_mode):
                    if info.st_mode & 0o022 and not (
                            info.st_uid == 0 and info.st_mode & stat.S_ISVTX):
                        raise _unsafe(current, 'is a writable measured directory')
                    final_dir, root_info = _open_measured_directory(
                        parent, part, current, require_current_owner=True)
                else:
                    root_info = info

        seen: set[tuple[int, int]] = set()
        allocated = 0
        inodes = 0

        def account(info) -> bool:
            nonlocal allocated, inodes
            key = _identity(info)
            if key in seen:
                return False
            seen.add(key)
            inodes += 1
            allocated += int(getattr(info, 'st_blocks', 0)) * 512
            return True

        account(root_info)
        if stat.S_ISDIR(root_info.st_mode):
            if _mountpoint_below(target):
                raise _unsafe(target, 'contains a nested mount')
            if final_dir is None:
                final_dir = parent
                parent = None
            pending = [final_dir]
            final_dir = None
            try:
                while pending:
                    directory = pending.pop()
                    try:
                        try:
                            entries = os.scandir(directory)
                        except OSError:
                            raise _unsafe(target, 'could not be scanned safely') from None
                        with entries:
                            for entry in entries:
                                try:
                                    info = entry.stat(follow_symlinks=False)
                                except OSError:
                                    raise _unsafe(target / entry.name,
                                                  'changed during measurement') from None
                                if info.st_dev != root_info.st_dev:
                                    raise _unsafe(target / entry.name,
                                                  'contains a nested mount')
                                if info.st_uid != os.geteuid():
                                    raise _unsafe(
                                        target / entry.name,
                                        f'has a foreign owner (uid {info.st_uid})')
                                account(info)
                                if stat.S_ISDIR(info.st_mode):
                                    try:
                                        child = os.open(
                                            entry.name,
                                            os.O_RDONLY | os.O_DIRECTORY
                                            | os.O_NOFOLLOW | os.O_CLOEXEC,
                                            dir_fd=directory)
                                    except OSError:
                                        raise _unsafe(target / entry.name,
                                                      'changed during traversal') from None
                                    try:
                                        opened = os.fstat(child)
                                        if (_identity(opened) != _identity(info)
                                                or opened.st_dev != root_info.st_dev
                                                or opened.st_uid != os.geteuid()):
                                            raise _unsafe(target / entry.name,
                                                          'changed during traversal')
                                    except BaseException:
                                        os.close(child)
                                        raise
                                    pending.append(child)
                    finally:
                        os.close(directory)
            finally:
                for directory in pending:
                    os.close(directory)
        return {'allocated_bytes': allocated, 'inodes': inodes}
    finally:
        if final_dir is not None:
            os.close(final_dir)
        if parent is not None:
            os.close(parent)


def _absolute_path(name: str, value) -> Path:
    try:
        path = Path(value).absolute()
    except (TypeError, ValueError, OSError):
        raise BudgetError(f'{name} must be a filesystem path.', 64) from None
    if '..' in path.parts:
        raise BudgetError(f'{name} must not contain parent traversal.', 64)
    return path


def _open_private_path(path: Path, *, create: bool, label: str,
                       check_ancestors: bool = False,
                       pool: bool = False) -> int:
    expected = None
    if check_ancestors:
        expected = _check_pool_ancestors(path)
    try:
        fd = open_private_directory(path, create=create, label=label)
    except StorageError as error:
        if pool:
            raise _pool_existing_error(path) from None
        raise BudgetError(str(error), error.code or 78) from None
    except (TypeError, ValueError) as error:
        raise BudgetError(f'Invalid {label.lower()} path {_display(path)}.', 64) from None
    except OSError as error:
        raise BudgetError(f'Cannot safely open {label.lower()} {_display(path)}.',
                          78, errno=error.errno) from None
    if pool:
        try:
            opened = os.fstat(fd)
            if expected is not None and _identity(opened) != _identity(expected):
                raise _pool_unsafe(path, 'changed during its readiness check',
                                   _POOL_UNVERIFIED_REMEDY)
            _check_pool_create_ready(fd, path, path, existing=True)
        except BaseException:
            os.close(fd)
            raise
    return fd


def _open_state(state: Path) -> int:
    return _open_private_path(state, create=True, label='Storage budget state')


def _open_allocation_target(path: Path) -> int:
    return _open_private_path(path, create=False, label='Storage allocation target')


def _open_pool(path: Path) -> int:
    return _open_private_path(path, create=True, label='Storage budget pool',
                              check_ancestors=True, pool=True)


def _open_budget_lock(state_fd: int, *, create: bool) -> int:
    flags = os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC
    if create:
        flags |= os.O_CREAT
    try:
        fd = os.open(BUDGET_LOCK, flags, 0o600, dir_fd=state_fd)
    except FileNotFoundError:
        raise BudgetError(f'Storage budget lock {BUDGET_LOCK} is missing; refusing to '
                          'change reservation state.', 78) from None
    except OSError as error:
        raise BudgetError(f'Cannot safely open storage budget lock {BUDGET_LOCK}.',
                          78, errno=error.errno) from None
    try:
        opened = os.fstat(fd)
        listed = os.stat(BUDGET_LOCK, dir_fd=state_fd, follow_symlinks=False)
        if _identity(opened) != _identity(listed):
            raise BudgetError('Storage budget lock changed during its safety check.', 78)
        _check_private_file(opened, 'Storage budget lock')
        return fd
    except BaseException:
        os.close(fd)
        raise


def _acquire_budget_lock(state_fd: int) -> tuple[int, tuple[int, int]]:
    fd = _open_budget_lock(state_fd, create=True)
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                break
            except InterruptedError:
                continue
            except OSError as error:
                raise BudgetError('Cannot serialize storage budget admission.',
                                  78, errno=error.errno) from None
        opened = os.fstat(fd)
        listed = os.stat(BUDGET_LOCK, dir_fd=state_fd, follow_symlinks=False)
        if _identity(opened) != _identity(listed):
            raise BudgetError('Storage budget lock was replaced during admission.', 78)
        return fd, _identity(opened)
    except BaseException:
        os.close(fd)
        raise


def _verify_budget_lock(state_fd: int, expected: tuple[int, int]) -> None:
    try:
        listed = os.stat(BUDGET_LOCK, dir_fd=state_fd, follow_symlinks=False)
    except OSError as error:
        raise BudgetError('Storage budget lock changed identity during admission.',
                          78, errno=error.errno) from None
    if (_identity(listed) != expected or not stat.S_ISREG(listed.st_mode)
            or listed.st_nlink != 1 or listed.st_uid != os.geteuid()
            or stat.S_IMODE(listed.st_mode) != 0o600):
        raise BudgetError('Storage budget lock changed identity during admission.', 78)


def _read_reservation(fd: int, name: str, path: Path, *,
                      expected_device: int,
                      allow_legacy_device: bool) -> tuple[int, int, int]:
    try:
        raw = os.pread(fd, _RECORD_LIMIT + 1, 0)
    except OSError as error:
        raise BudgetError(f'Cannot read reservation metadata {_display(path)}.',
                          78, errno=error.errno) from None
    if len(raw) > _RECORD_LIMIT:
        raise BudgetError(f'Reservation metadata is oversized: {_display(path)}.', 78)
    try:
        value = json.loads(raw.decode('utf-8'))
    except (UnicodeError, ValueError):
        raise BudgetError(f'Reservation metadata is corrupt: {_display(path)}.', 78) from None
    match = _RECORD_NAME.fullmatch(name)
    if (not isinstance(value, dict) or match is None
            or type(value.get('schema')) is not int):
        raise BudgetError(f'Reservation metadata is unknown or corrupt: {_display(path)}.', 78)
    if value['schema'] == 1:
        fields = {'schema', 'token', 'disk_bytes', 'inodes', 'pid'}
        if not allow_legacy_device:
            raise BudgetError(f'Reservation metadata has no filesystem device: '
                              f'{_display(path)}.', 78)
        device = expected_device
    elif value['schema'] == _RECORD_SCHEMA:
        fields = {'schema', 'token', 'disk_bytes', 'inodes', 'pid', 'device'}
    else:
        raise BudgetError(f'Reservation metadata has an unknown schema: '
                          f'{_display(path)}.', 78)
    if set(value) != fields or value['token'] != match.group(1):
        raise BudgetError(f'Reservation metadata is unknown or corrupt: {_display(path)}.', 78)
    if (isinstance(value['pid'], bool) or not isinstance(value['pid'], int)
            or value['pid'] <= 0):
        raise BudgetError(f'Reservation metadata has an invalid pid: {_display(path)}.', 78)
    if value['schema'] == _RECORD_SCHEMA:
        device = value['device']
        if isinstance(device, bool) or not isinstance(device, int) or device < 0:
            raise BudgetError(f'Reservation metadata has an invalid device: '
                              f'{_display(path)}.', 78)
    try:
        disk_bytes = _quantity('disk_bytes', value['disk_bytes'], code=78)
        inodes = _quantity('inodes', value['inodes'], code=78)
    except BudgetError:
        raise BudgetError(f'Reservation metadata has invalid quantities: {_display(path)}.',
                          78) from None
    return disk_bytes, inodes, device


def _unlink_pinned(state_fd: int, name: str, expected: tuple[int, int],
                   path: Path, *, require_private: bool = True) -> None:
    try:
        current = os.stat(name, dir_fd=state_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    except OSError as error:
        raise BudgetError(f'Cannot verify reservation {_display(path)} before removal.',
                          78, errno=error.errno) from None
    if (_identity(current) != expected or not stat.S_ISREG(current.st_mode)
            or current.st_nlink != 1):
        raise BudgetError(f'Reservation changed identity; refusing to remove '
                          f'{_display(path)}.', 78)
    if current.st_uid != os.geteuid():
        raise BudgetError(f'Reservation has a foreign owner; refusing to remove '
                          f'{_display(path)}.', 78)
    if require_private:
        _check_private_file(current, f'Reservation {_display(path)}')
    try:
        os.unlink(name, dir_fd=state_fd)
        os.fsync(state_fd)
    except OSError as error:
        raise BudgetError(f'Cannot remove reservation record {_display(path)}.',
                          78, errno=error.errno) from None


def _live_usage(pool_fd: int, pool: Path, *, expected_device: int,
                allow_legacy_device: bool) -> tuple[int, int]:
    try:
        names = os.listdir(pool_fd)
    except OSError as error:
        raise BudgetError('Cannot inspect storage reservation records.', 78,
                          errno=error.errno) from None
    disk_bytes = 0
    inodes = 0
    for name in names:
        if not name.startswith(RECORD_PREFIX) or not name.endswith(RECORD_SUFFIX):
            continue
        path = pool / name
        try:
            info = os.stat(name, dir_fd=pool_fd, follow_symlinks=False)
        except FileNotFoundError:
            continue
        except OSError as error:
            raise BudgetError(f'Cannot inspect reservation {_display(path)}.', 78,
                              errno=error.errno) from None
        if stat.S_ISLNK(info.st_mode):
            raise BudgetError(f'Reservation entry is a symbolic link; refusing to '
                              f'follow or delete it: {_display(path)}.', 78)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or info.st_uid != os.geteuid()):
            raise BudgetError(f'Reservation entry is not a private owned ordinary '
                              f'file: {_display(path)}.', 78)
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                         dir_fd=pool_fd)
        except FileNotFoundError:
            continue
        except OSError as error:
            raise BudgetError(f'Cannot safely open reservation {_display(path)}.', 78,
                              errno=error.errno) from None
        try:
            opened = os.fstat(fd)
            if _identity(opened) != _identity(info):
                raise BudgetError(f'Reservation changed identity: {_display(path)}.', 78)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                _check_private_file(opened, f'Reservation {_display(path)}')
                record_bytes, record_inodes, device = _read_reservation(
                    fd, name, path, expected_device=expected_device,
                    allow_legacy_device=allow_legacy_device)
                if device == expected_device:
                    disk_bytes += record_bytes
                    inodes += record_inodes
            except OSError as error:
                raise BudgetError(f'Cannot lock reservation {_display(path)}.', 78,
                                  errno=error.errno) from None
            else:
                _unlink_pinned(pool_fd, name, _identity(opened), path,
                               require_private=False)
        finally:
            os.close(fd)
    return disk_bytes, inodes


def _write_all(fd: int, raw: bytes) -> None:
    view = memoryview(raw)
    while view:
        try:
            written = os.write(fd, view)
        except InterruptedError:
            continue
        if written <= 0:
            raise OSError('short write')
        view = view[written:]


def _create_record(pool_fd: int, disk_bytes: int, inodes: int, device: int):
    fd = None
    name = None
    token = None

    def discard():
        if fd is None:
            return
        try:
            opened = os.fstat(fd)
            current = os.stat(name, dir_fd=pool_fd, follow_symlinks=False)
            if (_identity(current) == _identity(opened)
                    and stat.S_ISREG(current.st_mode) and current.st_nlink == 1):
                os.unlink(name, dir_fd=pool_fd)
        except OSError:
            pass
        finally:
            os.close(fd)

    try:
        for _ in range(16):
            token = uuid.uuid4().hex
            name = f'{RECORD_PREFIX}{token}{RECORD_SUFFIX}'
            try:
                fd = os.open(name,
                             os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
                             | os.O_CLOEXEC,
                             0o600, dir_fd=pool_fd)
                break
            except FileExistsError:
                continue
        if fd is None:
            raise BudgetError('Cannot allocate a unique reservation token.', 78)
        opened = os.fstat(fd)
        _check_private_file(opened, 'New reservation record')
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        raw = (json.dumps({'schema': _RECORD_SCHEMA, 'token': token,
                           'disk_bytes': disk_bytes, 'inodes': inodes,
                           'pid': os.getpid(), 'device': device},
                          sort_keys=True, separators=(',', ':')).encode('ascii') + b'\n')
        _write_all(fd, raw)
        os.fsync(fd)
        listed = os.stat(name, dir_fd=pool_fd, follow_symlinks=False)
        if _identity(listed) != _identity(opened) or not stat.S_ISREG(listed.st_mode):
            raise BudgetError('New reservation changed during creation.', 78)
        return fd, name, token, _identity(opened)
    except OSError as error:
        discard()
        raise BudgetError('Cannot create a private reservation record.', 78,
                          errno=error.errno) from None
    except BaseException:
        discard()
        raise


def _filesystem_limits(allocation_fd: int) -> tuple[int, int, int]:
    """Measure the pinned allocation descriptor, never the pool descriptor."""
    try:
        opened = os.fstat(allocation_fd)
        values = os.fstatvfs(allocation_fd)
    except OSError as error:
        raise BudgetError('Cannot measure the pinned storage allocation target.', 78,
                          errno=error.errno) from None
    if not stat.S_ISDIR(opened.st_mode):
        raise BudgetError('Storage allocation target is not an ordinary directory.', 78)
    device = int(opened.st_dev)
    if device < 0:
        raise BudgetError('Storage allocation target reports an invalid device.', 78)
    try:
        free_bytes = int(values.f_bavail) * int(values.f_frsize)
        available = getattr(values, 'f_favail', None)
        if available is None:
            raise BudgetError('Filesystem does not report available inodes (f_favail); '
                              'refusing to assume inode capacity is unlimited.', 75)
        free_inodes = int(available)
    except (AttributeError, TypeError, ValueError, OverflowError):
        raise BudgetError('Filesystem capacity statistics are unavailable or invalid.',
                          78) from None
    if free_inodes <= 0:
        raise BudgetError('Filesystem reports no available inodes (f_favail=0); '
                          'refusing to assume inode capacity is unlimited.', 75)
    return free_bytes, free_inodes, device


def _close_quietly(fd) -> None:
    if fd is None:
        return
    try:
        os.close(fd)
    except OSError:
        pass


class Reservation(str):
    """A unique token with an open flock-held reservation record."""

    def __new__(cls, token: str, *, pool_fd: int, pool: Path, record_fd: int,
                record_name: str,
                record_identity: tuple[int, int], lock_identity: tuple[int, int],
                disk_bytes: int, inodes: int, device: int, lock_anchor_fd: int):
        value = super().__new__(cls, token)
        value._pool_fd = pool_fd
        value._pool = pool
        value._record_fd = record_fd
        value._record_name = record_name
        value._record_identity = record_identity
        value._lock_identity = lock_identity
        value._lock_anchor_fd = lock_anchor_fd
        value.disk_bytes = disk_bytes
        value.inodes = inodes
        value.device = device
        value._closed = False
        return value

    @property
    def token(self) -> str:
        return str(self)

    @property
    def closed(self) -> bool:
        return self._closed

    def close(self) -> None:
        if self._closed:
            return
        lock_fd = None
        try:
            lock_fd = _open_budget_lock(self._pool_fd, create=False)
            if _identity(os.fstat(lock_fd)) != self._lock_identity:
                raise BudgetError('Storage budget lock changed identity; reservation '
                                  'record was preserved for inspection.', 78)
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            if _identity(os.fstat(lock_fd)) != self._lock_identity:
                raise BudgetError('Storage budget lock changed identity; reservation '
                                  'record was preserved for inspection.', 78)
            _unlink_pinned(self._pool_fd, self._record_name, self._record_identity,
                           self._pool / self._record_name, require_private=False)
        except OSError as error:
            raise BudgetError('Cannot safely close the storage reservation.', 78,
                              errno=error.errno) from None
        finally:
            _close_quietly(lock_fd)
            _close_quietly(self._record_fd)
            _close_quietly(self._pool_fd)
            _close_quietly(self._lock_anchor_fd)
            self._record_fd = None
            self._pool_fd = None
            self._lock_anchor_fd = None
            self._closed = True

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()
        return False


class _ReservationContext:
    def __init__(self, state: Path, disk_bytes: int, inodes: int,
                 minimum_free_bytes: int, minimum_free_inodes: int,
                 allocation_path: Path | None, pool_dir: Path | None):
        self._state = state
        self._allocation_path = allocation_path
        self._pool_dir = pool_dir
        self._disk_bytes = disk_bytes
        self._inodes = inodes
        self._minimum_free_bytes = minimum_free_bytes
        self._minimum_free_inodes = minimum_free_inodes
        self._entered = False
        self._reservation = None

    def __enter__(self):
        if self._entered:
            raise BudgetError('A storage reservation context cannot be entered twice.', 64)
        self._entered = True
        allocation_owned = False
        allocation_fd = None
        pool_fd = None
        lock_fd = None
        try:
            pool_path = self._pool_dir or self._state
            if self._allocation_path is None and self._pool_dir is None:
                pool_fd = _open_state(pool_path)
                allocation_fd = pool_fd
            else:
                pool_fd = (_open_pool(pool_path) if self._pool_dir is not None
                           else _open_state(pool_path))
                allocation_path = self._allocation_path or self._state
                allocation_fd = (_open_allocation_target(allocation_path)
                                 if self._allocation_path is not None
                                 else _open_state(allocation_path))
                allocation_owned = True
            lock_fd, lock_identity = _acquire_budget_lock(pool_fd)
            free_bytes, free_inodes, device = _filesystem_limits(allocation_fd)
            allow_legacy_device = (self._allocation_path is None
                                   and self._pool_dir is None)
            held_bytes, held_inodes = _live_usage(
                pool_fd, pool_path, expected_device=device,
                allow_legacy_device=allow_legacy_device)

            byte_limit = free_bytes - self._minimum_free_bytes
            if (byte_limit < 0 or held_bytes > byte_limit
                    or self._disk_bytes > byte_limit - held_bytes):
                raise BudgetError(
                    f'Insufficient free bytes for this reservation: requested '
                    f'{self._disk_bytes}, live reservations {held_bytes}, available '
                    f'{free_bytes}, minimum free {self._minimum_free_bytes}.', 75)
            inode_limit = free_inodes - self._minimum_free_inodes
            if (inode_limit < 0 or held_inodes > inode_limit
                    or self._inodes > inode_limit - held_inodes):
                raise BudgetError(
                    f'Insufficient free inodes for this reservation: requested '
                    f'{self._inodes}, live reservations {held_inodes}, available '
                    f'{free_inodes}, minimum free {self._minimum_free_inodes}.', 75)

            record_fd, record_name, token, record_identity = _create_record(
                pool_fd, self._disk_bytes, self._inodes, device)
            try:
                _verify_budget_lock(pool_fd, lock_identity)
                self._reservation = Reservation(
                    token, pool_fd=pool_fd, pool=pool_path, record_fd=record_fd,
                    record_name=record_name, record_identity=record_identity,
                    lock_identity=lock_identity, disk_bytes=self._disk_bytes,
                    inodes=self._inodes, device=device, lock_anchor_fd=lock_fd)
            except BaseException:
                try:
                    _unlink_pinned(pool_fd, record_name, record_identity,
                                   pool_path / record_name)
                finally:
                    os.close(record_fd)
                raise
            pool_fd = None
            record_fd = None
            # Keep the inode pinned without holding the allocator mutex. If
            # its pathname is replaced, ext4 cannot recycle this live inode
            # and make a replacement look like our original lock at close.
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            lock_fd = None
            return self._reservation
        finally:
            _close_quietly(lock_fd)
            if allocation_owned:
                _close_quietly(allocation_fd)
            _close_quietly(pool_fd)

    def __exit__(self, exc_type, exc, traceback):
        reservation, self._reservation = self._reservation, None
        if reservation is not None:
            reservation.close()
        return False


def reserve(state: Path, *, disk_bytes: int, inodes: int,
            minimum_free_bytes: int = MINIMUM_FREE_BYTES,
            minimum_free_inodes: int = MINIMUM_FREE_INODES,
            allocation_path: Path | None = None,
            pool_dir: Path | None = None):
    """Reserve unmaterialized bytes and inodes for an allocation filesystem.

    Admission blocks on an exclusive no-follow ``budget.lock`` and then sums
    only reservation records whose owner still holds their flock.  A stopped or
    crashed owner therefore releases its reservation through the operating
    system.  The context yields a :class:`Reservation` (a string token with
    ``close``); leaving the context removes only the caller's identity-pinned
    record.  There is deliberately no wait timeout.

    The basic call with only ``state`` keeps that directory as both the pool and
    allocation target for compatibility.  To coordinate several state roots on
    one filesystem, pass the existing private ``allocation_path`` that will grow
    and a common private ``pool_dir``.  The pool may live on another filesystem:
    capacity always comes from the pinned allocation descriptor.  New records
    carry the canonical ``st_dev`` of that descriptor and records for other
    devices do not consume this target's capacity.  Legacy per-state records are
    accepted only for the basic default call.
    """
    disk_bytes = _quantity('disk_bytes', disk_bytes)
    inodes = _quantity('inodes', inodes)
    minimum_free_bytes = _quantity('minimum_free_bytes', minimum_free_bytes)
    minimum_free_inodes = _quantity('minimum_free_inodes', minimum_free_inodes)
    state_path = _absolute_path('state', state)
    allocation = (None if allocation_path is None
                  else _absolute_path('allocation_path', allocation_path))
    pool = None if pool_dir is None else _absolute_path('pool_dir', pool_dir)
    return _ReservationContext(state_path, disk_bytes, inodes,
                               minimum_free_bytes, minimum_free_inodes,
                               allocation, pool)


def _user_config_base() -> Path:
    configured = os.environ.get('XDG_CONFIG_HOME')
    if configured:
        base = Path(configured)
        if not base.is_absolute() or '..' in base.parts:
            raise BudgetError('XDG_CONFIG_HOME must be an absolute canonical path.', 64)
    else:
        try:
            base = Path.home() / '.config'
        except (RuntimeError, OSError):
            raise BudgetError('Cannot resolve a private user config directory.', 64) from None
        if not base.is_absolute() or '..' in base.parts:
            raise BudgetError('The user home directory must be an absolute canonical path.',
                              64)
    return base


def shared_pool_dir(allocation_path: Path) -> Path:
    """Return the XDG/HOME-config pool for the target's canonical filesystem.

    The returned path is not created here; ``reserve`` validates and creates it
    as a private 700 directory.  Callers may pass the result as ``pool_dir`` for
    every state root allocating on that device.
    """
    target = _absolute_path('allocation_path', allocation_path)
    allocation_fd = _open_allocation_target(target)
    try:
        device = int(os.fstat(allocation_fd).st_dev)
    finally:
        os.close(allocation_fd)
    if device < 0:
        raise BudgetError('Storage allocation target reports an invalid device.', 78)
    return _user_config_base().joinpath(*_POOL_PARTS, f'dev-{device}')


def check_pool_readiness(allocation_path: Path, *,
                         pool_dir: Path | None = None) -> dict:
    """Check the shared budget pool without reserving, creating or repairing it.

    The returned mapping names the effective ``path`` and whether it already
    ``exists``.  A missing pool is ``safe_to_create`` only after every existing
    ancestor has passed the same descriptor-relative ownership, mode and
    symbolic-link checks used by reservation, and the nearest existing ancestor
    is proven to have effective and actual write/search access on a writable
    filesystem.  An existing final pool is also validated as an ordinary
    current-user-owned mode-700 directory that can create its lock and
    reservation records.  This provider-free check never creates the pool, a
    lock, a probe or a reservation record, and ``reserve`` remains authoritative
    at allocation time.

    Raises :class:`BudgetError` with code 78 for an unsafe path.  The message
    names the precise offending entry and avoids suggesting that changing the
    worker state root repairs an XDG-backed pool.
    """
    target = _absolute_path('allocation_path', allocation_path)
    if pool_dir is None:
        pool = shared_pool_dir(target)
    else:
        allocation_fd = _open_allocation_target(target)
        os.close(allocation_fd)
        pool = _absolute_path('pool_dir', pool_dir)

    info = _check_pool_ancestors(pool)
    if info is None:
        return {'path': pool, 'exists': False, 'safe_to_create': True}

    problem = _pool_info_problem(pool, info)
    if problem is not None:
        raise problem
    try:
        fd = open_private_directory(pool, create=False,
                                    label='Storage budget pool')
    except StorageError:
        raise _pool_existing_error(pool) from None
    try:
        if _identity(os.fstat(fd)) != _identity(info):
            raise _pool_unsafe(pool, 'changed during its readiness check',
                               _POOL_UNVERIFIED_REMEDY)
        _check_pool_create_ready(fd, pool, pool, existing=True)
    finally:
        os.close(fd)
    return {'path': pool, 'exists': True, 'safe_to_create': False}
