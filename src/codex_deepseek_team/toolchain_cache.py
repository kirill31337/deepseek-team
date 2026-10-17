"""Durable content-addressed storage for declared toolchain sources.

The cache is coordinator-owned private state.  It never becomes a sandbox bind:
workspace preparation copies a verified entry into the active copy, which keeps the
existing read-only ``.deepseek-tools`` mapping and ordinary writable ``--copy``
destinations.  Entries contain ordinary copied files only, never hardlinks,
symlinks, archives or checkout metadata.  Every cache operation validates the
complete manifest again while holding the shared cache lock.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import time
import uuid

from . import toolchains, workspace

CACHE_DIRNAME = 'toolchain-cache'
CACHE_SCHEMA = 1
ENTRIES_DIRNAME = 'entries'
MANIFESTS_DIRNAME = 'manifests'
_LOCK_NAME = 'cache.lock'
_STAGING_DIRNAME = '.staging'
_DIGEST = re.compile(r'[0-9a-f]{64}\Z')
_STAGE_NAME = re.compile(r'\.stage-[0-9a-f]{32}(?:\.json)?\Z')
_PURGE_NAME = re.compile(r'\.purge-[0-9a-f]{32}\Z')
_MAX_MANIFEST_BYTES = 64 << 20
_DEFAULT_MAX_BYTES = 2147483648
_DEFAULT_MAX_INODES = 200000
_O_NOATIME = getattr(os, 'O_NOATIME', 0)
_O_PATH = getattr(os, 'O_PATH', os.O_RDONLY)
_DIRECTORY_FLAGS = (os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
                    | _O_NOATIME)
_DIRECTORY_BASE_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def _directory_flags(info=None) -> int:
    if info is None or info.st_uid == _owner_uid():
        return _DIRECTORY_FLAGS
    return _DIRECTORY_BASE_FLAGS


@dataclass(frozen=True)
class _CacheLayout:
    root: Path
    entries: Path
    manifests: Path
    staging: Path
    root_identity: tuple[int, int]
    entries_identity: tuple[int, int]
    manifests_identity: tuple[int, int]
    staging_identity: tuple[int, int]
    root_mount: int
    entries_mount: int
    manifests_mount: int
    staging_mount: int

    def __iter__(self):
        """Keep the historical ``root, entries, manifests`` unpacking usable."""
        yield self.root
        yield self.entries
        yield self.manifests

    def __fspath__(self):
        return os.fspath(self.root)

    def __truediv__(self, value):
        return self.root / value


def _display(value) -> str:
    return workspace._escape_name(str(value))


def _cache_root(state: Path) -> Path:
    return Path(state).absolute() / CACHE_DIRNAME


def _absolute(path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _identity(info) -> tuple[int, int]:
    return info.st_dev, info.st_ino


def _owner_uid() -> int:
    """Single current-user ownership seam for narrow UID regression simulation."""
    return os.geteuid()


def _deletion_owner_uid() -> int:
    """Narrow UID simulation seam for destructive-ownership regression tests."""
    return os.geteuid()


def _mount_id(fd: int) -> int:
    """Return the mount id for an open descriptor, failing closed if unavailable."""
    path = f'/proc/self/fdinfo/{fd}'
    try:
        with open(path, 'rb') as stream:
            for line in stream:
                if line.startswith(b'mnt_id:'):
                    return int(line.split(b':', 1)[1].strip())
    except (OSError, ValueError):
        pass
    raise toolchains.ToolchainError(
        'The shared toolchain cache cannot prove mount identity; no cache data was changed.')


def _private_directory(info, label: str) -> None:
    if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
            or info.st_uid != _owner_uid() or stat.S_IMODE(info.st_mode) & 0o077):
        raise toolchains.ToolchainError(f'Private toolchain cache directory is unsafe: {_display(label)}.')


def _open_directory_chain(path: Path, *, create: bool = False) -> int:
    """Open an absolute directory path without following any component symlink."""
    absolute = _absolute(path)
    if not absolute.is_absolute():
        raise toolchains.ToolchainError('Toolchain cache paths must be absolute.')
    try:
        fd = os.open('/', _DIRECTORY_BASE_FLAGS)
    except OSError as error:
        raise toolchains.ToolchainError('Cannot inspect the toolchain cache root.') from error
    try:
        for part in absolute.parts[1:]:
            if part in ('', '.', '..'):
                raise toolchains.ToolchainError('Unsafe toolchain cache path component.')
            try:
                before = os.stat(part, dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(part, 0o700, dir_fd=fd)
                except FileExistsError:
                    pass
                before = os.stat(part, dir_fd=fd, follow_symlinks=False)
            except OSError as error:
                raise toolchains.ToolchainError(
                    f'Cannot validate toolchain cache ancestor: {_display(absolute)}.') from error
            if not stat.S_ISDIR(before.st_mode) or stat.S_ISLNK(before.st_mode):
                raise toolchains.ToolchainError(
                    f'Toolchain cache ancestor is not an ordinary directory: {_display(absolute)}.')
            try:
                next_fd = os.open(part, _directory_flags(before), dir_fd=fd)
            except OSError as error:
                raise toolchains.ToolchainError(
                    f'Cannot open toolchain cache ancestor safely: {_display(absolute)}.') from error
            opened = os.fstat(next_fd)
            os.close(fd)
            fd = next_fd
            if _identity(opened) != _identity(before):
                raise toolchains.ToolchainError(
                    f'Toolchain cache ancestor changed while being inspected: {_display(absolute)}.')
            _mount_id(fd)
        return fd
    except BaseException:
        os.close(fd)
        raise


def _open_layout_child(root_fd: int, name: str, *, create: bool = False) -> int:
    try:
        info = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
    except FileNotFoundError:
        if not create:
            raise
        try:
            os.mkdir(name, 0o700, dir_fd=root_fd)
        except FileExistsError:
            pass
        info = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise toolchains.ToolchainError(
            f'Toolchain cache layout entry is not an ordinary directory: {_display(name)}.')
    try:
        fd = os.open(name, _directory_flags(info), dir_fd=root_fd)
    except OSError as error:
        raise toolchains.ToolchainError(
            f'Cannot open toolchain cache layout entry safely: {_display(name)}.') from error
    opened = os.fstat(fd)
    if _identity(opened) != _identity(info):
        os.close(fd)
        raise toolchains.ToolchainError(
            f'Toolchain cache layout entry changed while being inspected: {_display(name)}.')
    return fd


def _layout_from_fds(root: Path, root_fd: int, *, create: bool = False) -> _CacheLayout:
    root_info = os.fstat(root_fd)
    _private_directory(root_info, root)
    root_mount = _mount_id(root_fd)
    children = {}
    for label, name in (('entries', ENTRIES_DIRNAME), ('manifests', MANIFESTS_DIRNAME),
                        ('staging', _STAGING_DIRNAME)):
        try:
            fd = _open_layout_child(root_fd, name, create=create)
        except FileNotFoundError:
            raise toolchains.ToolchainError(
                'Shared toolchain cache layout is incomplete; no cache data was changed.') from None
        try:
            info = os.fstat(fd)
            _private_directory(info, root / name)
            mount = _mount_id(fd)
            if info.st_dev != root_info.st_dev or mount != root_mount:
                raise toolchains.ToolchainError(
                    f'Toolchain cache layout entry crosses a filesystem or mount boundary: '
                    f'{_display(root / name)}.')
            children[label] = (root / name, _identity(info), mount)
        finally:
            os.close(fd)
    return _CacheLayout(
        root=root, entries=children['entries'][0], manifests=children['manifests'][0],
        staging=children['staging'][0], root_identity=_identity(root_info),
        entries_identity=children['entries'][1], manifests_identity=children['manifests'][1],
        staging_identity=children['staging'][1], root_mount=root_mount,
        entries_mount=children['entries'][2], manifests_mount=children['manifests'][2],
        staging_mount=children['staging'][2])


def _ensure_layout(state: Path) -> _CacheLayout:
    try:
        state_path = _absolute(state)
        state_fd = _open_directory_chain(state_path, create=True)
        try:
            _private_directory(os.fstat(state_fd), state_path)
        finally:
            os.close(state_fd)
        root = _cache_root(state_path)
        root_fd = _open_directory_chain(root, create=True)
        try:
            return _layout_from_fds(root, root_fd, create=True)
        finally:
            os.close(root_fd)
    except toolchains.ToolchainError:
        raise
    except (OSError, workspace.WorkspaceError) as error:
        raise toolchains.ToolchainError(
            'Shared toolchain cache state is unavailable or unsafe; no cache entry was used.',
            78) from error


def _inspect_layout(state: Path) -> _CacheLayout | None:
    state_path = _absolute(state)
    try:
        state_fd = _open_directory_chain(state_path)
    except FileNotFoundError:
        return None
    except toolchains.ToolchainError:
        raise
    try:
        _private_directory(os.fstat(state_fd), state_path)
    finally:
        os.close(state_fd)
    root = _cache_root(state_path)
    try:
        root_fd = _open_directory_chain(root)
    except FileNotFoundError:
        return None
    try:
        return _layout_from_fds(root, root_fd)
    finally:
        os.close(root_fd)


def _verify_root_path(layout: _CacheLayout, root_fd: int) -> None:
    info = os.fstat(root_fd)
    if _identity(info) != layout.root_identity or _mount_id(root_fd) != layout.root_mount:
        raise toolchains.ToolchainError(
            'The shared toolchain cache root changed after it was inspected; no cache data was changed.')
    try:
        named = os.stat(layout.root, follow_symlinks=False)
    except OSError as error:
        raise toolchains.ToolchainError(
            'The shared toolchain cache root disappeared while locked; no cache data was changed.') from error
    if _identity(named) != layout.root_identity:
        raise toolchains.ToolchainError(
            'The shared toolchain cache root was replaced; no cache data was changed.')


def _verify_lock(layout: _CacheLayout, root_fd: int, lock_fd: int) -> None:
    info = os.fstat(lock_fd)
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or info.st_uid != _owner_uid() or stat.S_IMODE(info.st_mode) != 0o600):
        raise toolchains.ToolchainError('Shared toolchain cache lock is unsafe.')
    try:
        named = os.stat(_LOCK_NAME, dir_fd=root_fd, follow_symlinks=False)
    except OSError as error:
        raise toolchains.ToolchainError('Shared toolchain cache lock changed identity.') from error
    if _identity(named) != _identity(info):
        raise toolchains.ToolchainError('Shared toolchain cache lock changed identity.')
    _verify_root_path(layout, root_fd)


def _layout_has_entries(layout: _CacheLayout) -> bool:
    try:
        with os.scandir(layout.root) as stream:
            if any(entry.name not in {ENTRIES_DIRNAME, MANIFESTS_DIRNAME, _STAGING_DIRNAME}
                   for entry in stream):
                return True
    except OSError as error:
        raise toolchains.ToolchainError(
            'Shared toolchain cache root became unreadable during inspection.') from error
    for path in (layout.entries, layout.manifests, layout.staging):
        try:
            fd = _open_directory_chain(path)
        except OSError as error:
            raise toolchains.ToolchainError(
                'Shared toolchain cache layout became unreadable during inspection.') from error
        try:
            with os.scandir(fd) as stream:
                if any(True for _ in stream):
                    return True
        except OSError as error:
            raise toolchains.ToolchainError(
                'Shared toolchain cache layout became unreadable during inspection.') from error
        finally:
            os.close(fd)
    return False


@contextmanager
def locked(state: Path, *, exclusive: bool = True):
    """Hold the cache lock while validating or materializing shared entries."""
    layout = _ensure_layout(state)
    root_fd = _open_directory_chain(layout.root)
    try:
        _verify_root_path(layout, root_fd)
        try:
            lock_fd = os.open(_LOCK_NAME,
                              os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW | _O_NOATIME,
                              0o600, dir_fd=root_fd)
        except OSError as error:
            raise toolchains.ToolchainError('Cannot lock the shared toolchain cache safely.') from error
        try:
            _verify_lock(layout, root_fd, lock_fd)
            fcntl.flock(lock_fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
            _verify_lock(layout, root_fd, lock_fd)
            yield layout
        finally:
            os.close(lock_fd)
    finally:
        os.close(root_fd)


@contextmanager
def _inspection(state: Path):
    layout = _inspect_layout(state)
    if layout is None:
        yield None
        return
    root_fd = _open_directory_chain(layout.root)
    try:
        _verify_root_path(layout, root_fd)
        try:
            lock_fd = os.open(_LOCK_NAME, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | _O_NOATIME,
                              dir_fd=root_fd)
        except FileNotFoundError:
            if _layout_has_entries(layout):
                raise toolchains.ToolchainError(
                    'Shared toolchain cache data exists without its safety lock; inspection refused.') from None
            yield layout
            return
        except OSError as error:
            raise toolchains.ToolchainError('Cannot inspect the shared toolchain cache lock safely.') from error
        try:
            _verify_lock(layout, root_fd, lock_fd)
            fcntl.flock(lock_fd, fcntl.LOCK_SH)
            _verify_lock(layout, root_fd, lock_fd)
            yield layout
        finally:
            os.close(lock_fd)
    finally:
        os.close(root_fd)


@contextmanager
def reading(state: Path):
    """Shared reader lock used while an active copy consumes cache entries."""
    with _inspection(state) as layout:
        if layout is None or not os.path.lexists(layout.root / _LOCK_NAME):
            raise toolchains.ToolchainError(
                'A managed cache entry requires an existing shared cache lock.')
        yield layout


def _validate_caps(max_bytes, max_inodes) -> tuple[int, int]:
    for label, value in (('max_bytes', max_bytes), ('max_inodes', max_inodes)):
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise toolchains.ToolchainError(f'{label} must be a non-negative integer.')
    return max_bytes, max_inodes


def _is_under(path: Path, root: Path) -> bool:
    value, base = str(path), str(root)
    return value == base or value.startswith(base + os.sep)


def _credential_path(path: Path) -> bool:
    return workspace._credential_like(path) or workspace._credential_like(Path(os.path.realpath(path)))


def _validate_source(state: Path, source: Path) -> tuple[Path, Path]:
    if isinstance(source, os.PathLike):
        source = os.fspath(source)
    if not isinstance(source, str) or not source or '\0' in source:
        raise toolchains.ToolchainError('A cache source must name an existing ordinary file or directory.')
    raw = Path(source)
    try:
        resolved = raw.resolve(strict=True)
    except (OSError, RuntimeError):
        raise toolchains.ToolchainError(
            f'Cache source does not exist or is not readable: {_display(source)}.', 78) from None
    home = Path.home()
    rejected = set(toolchains._BROAD_ROOTS) | {str(home), str(home / '.local')}
    if str(resolved) in rejected:
        raise toolchains.ToolchainError(
            f'Cache source is a broad or mixed host location and is never copied: {_display(resolved)}.')
    if _credential_path(raw) or _credential_path(resolved):
        raise toolchains.ToolchainError(f'Refusing credential-like cache source: {_display(raw)}.')
    if '.git' in resolved.parts:
        raise toolchains.ToolchainError('A checkout or Git metadata directory is never cached.')
    cache = Path(os.path.realpath(_cache_root(state)))
    if _is_under(resolved, cache) or _is_under(cache, resolved):
        raise toolchains.ToolchainError('The shared toolchain cache cannot be its own source.')
    try:
        source_info = os.lstat(resolved)
    except OSError:
        raise toolchains.ToolchainError(
            f'Cache source is unavailable or unsafe: {_display(source)}.') from None
    if not stat.S_ISDIR(source_info.st_mode) and not stat.S_ISREG(source_info.st_mode):
        raise toolchains.ToolchainError('A cache source must be an ordinary file or directory without links.')
    if resolved.is_dir() and os.path.lexists(resolved / '.git'):
        raise toolchains.ToolchainError('Never cache a Git checkout or its metadata.')
    return raw, resolved


def _open_measurement(path: Path) -> int:
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
    try:
        info = os.lstat(path)
    except OSError as error:
        raise toolchains.ToolchainError(
            f'Cache allocation cannot be measured safely: {_display(path)}.') from error
    if info.st_uid == _owner_uid():
        flags |= _O_NOATIME
    if stat.S_ISDIR(info.st_mode):
        flags |= os.O_DIRECTORY
    try:
        return os.open(path, flags)
    except OSError as error:
        raise toolchains.ToolchainError(
            f'Cache allocation cannot be read safely: {_display(path)}.') from error


def _measure(path: Path, *, owned: bool = False, ordinary: bool = False) -> tuple[int, int]:
    """Actual allocated blocks and inodes, rejecting read or identity failures.

    ``owned`` applies destructive-ownership rules to cache storage.  Source inputs
    deliberately do not use it: a declared system SDK may be root-owned while still
    being readable by the ordinary user running the cache.
    """
    path = _absolute(path)
    fd = _open_measurement(path)
    measured = [0, 0]

    def consume(current_fd: int, current_info, *, base_dev: int, base_mount: int) -> None:
        measured[0] += current_info.st_blocks * 512
        measured[1] += 1
        if owned and current_info.st_uid != _owner_uid():
            raise toolchains.ToolchainError(
                f'Cache allocation is not owned by the current user: {_display(path)}.')
        if current_info.st_dev != base_dev:
            raise toolchains.ToolchainError(
                f'Cache allocation crosses a filesystem boundary: {_display(path)}.')
        if stat.S_ISLNK(current_info.st_mode):
            if ordinary:
                raise toolchains.ToolchainError(
                    f'Cache allocation contains a link rather than ordinary data: {_display(path)}.')
            return
        if stat.S_ISREG(current_info.st_mode):
            if ordinary and current_info.st_nlink != 1:
                raise toolchains.ToolchainError(
                    f'Cache allocation contains a hardlinked file: {_display(path)}.')
            return
        if not stat.S_ISDIR(current_info.st_mode):
            if ordinary:
                raise toolchains.ToolchainError(
                    f'Cache allocation contains a special file: {_display(path)}.')
            return
        try:
            names = sorted(entry.name for entry in os.scandir(current_fd))
        except OSError as error:
            raise toolchains.ToolchainError(
                f'Cache allocation directory is unreadable: {_display(path)}.') from error
        for name in names:
            try:
                child_info = os.stat(name, dir_fd=current_fd, follow_symlinks=False)
            except OSError as error:
                raise toolchains.ToolchainError(
                    f'Cache allocation changed while being measured: {_display(path)}.') from error
            if owned and child_info.st_uid != _owner_uid():
                raise toolchains.ToolchainError(
                    f'Cache allocation is not owned by the current user: {_display(path)}.')
            if child_info.st_dev != base_dev:
                raise toolchains.ToolchainError(
                    f'Cache allocation crosses a filesystem boundary: {_display(path)}.')
            if stat.S_ISDIR(child_info.st_mode):
                try:
                    child_fd = os.open(name, _directory_flags(child_info), dir_fd=current_fd)
                except OSError as error:
                    raise toolchains.ToolchainError(
                        f'Cache allocation directory is unreadable: {_display(path)}.') from error
                try:
                    opened = os.fstat(child_fd)
                    if _identity(opened) != _identity(child_info):
                        raise toolchains.ToolchainError(
                            f'Cache allocation changed while being measured: {_display(path)}.')
                    if _mount_id(child_fd) != base_mount:
                        raise toolchains.ToolchainError(
                            f'Cache allocation crosses a mount boundary: {_display(path)}.')
                    consume(child_fd, child_info, base_dev=base_dev, base_mount=base_mount)
                finally:
                    os.close(child_fd)
            elif stat.S_ISREG(child_info.st_mode) or stat.S_ISLNK(child_info.st_mode):
                measured[0] += child_info.st_blocks * 512
                measured[1] += 1
                link_flags = _O_PATH if stat.S_ISLNK(child_info.st_mode) else 0
                read_noatime = _O_NOATIME if child_info.st_uid == _owner_uid() else 0
                try:
                    child_fd = os.open(
                        name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
                        | read_noatime | link_flags, dir_fd=current_fd)
                except OSError as error:
                    raise toolchains.ToolchainError(
                        f'Cache allocation is unreadable: {_display(path)}.') from error
                try:
                    opened = os.fstat(child_fd)
                    if (_identity(opened) != _identity(child_info)
                            or _mount_id(child_fd) != base_mount):
                        raise toolchains.ToolchainError(
                            f'Cache allocation changed while being measured: {_display(path)}.')
                finally:
                    os.close(child_fd)
            else:
                measured[0] += child_info.st_blocks * 512
                measured[1] += 1
                if ordinary:
                    raise toolchains.ToolchainError(
                        f'Cache allocation contains a special file: {_display(path)}.')

    try:
        info = os.fstat(fd)
        consume(fd, info, base_dev=info.st_dev, base_mount=_mount_id(fd))
    finally:
        os.close(fd)
    if measured[1] < 1:
        raise toolchains.ToolchainError(f'Cache allocation could not be measured: {_display(path)}.')
    return measured[0], measured[1]


def _immutable_mode(mode: int, *, directory: bool) -> int:
    return 0o500 if directory else mode & 0o555


def _canonical_digest(kind: str, entries: list[dict]) -> str:
    material = []
    for entry in entries:
        material.append({'path': entry['path'], 'kind': entry['kind'], 'mode': entry['mode'],
                         'size': entry['size'], 'sha256': entry['sha256']})
    raw = json.dumps({'type': kind, 'entries': material}, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(raw.encode('utf-8', 'surrogateescape')).hexdigest()


def _stage_manifest(stage: Path, kind: str) -> dict:
    entries = []

    def visit(path: Path, relative: str):
        try:
            info = os.lstat(path)
        except OSError:
            raise toolchains.ToolchainError('A staged cache entry disappeared during verification.') from None
        if stat.S_ISLNK(info.st_mode):
            raise toolchains.ToolchainError('Cache entries must contain ordinary materialized files, not symlinks.')
        if stat.S_ISDIR(info.st_mode):
            entry_kind = 'dir'
            digest = None
            size = 0
        elif stat.S_ISREG(info.st_mode):
            entry_kind = 'file'
            size = info.st_size
            digest = _file_digest(path)
        else:
            raise toolchains.ToolchainError('Cache entries may contain only ordinary files and directories.')
        if info.st_uid != _owner_uid():
            raise toolchains.ToolchainError('A cache entry is not owned by the current user.')
        mode = stat.S_IMODE(info.st_mode) & 0o777
        entries.append({'path': relative, 'kind': entry_kind, 'mode': mode,
                        'cached_mode': _immutable_mode(mode, directory=entry_kind == 'dir'),
                        'size': size, 'sha256': digest, 'uid': info.st_uid,
                        'dev': info.st_dev, 'ino': info.st_ino})
        if entry_kind == 'dir':
            try:
                children = sorted(os.scandir(path), key=lambda entry: entry.name)
            except OSError:
                raise toolchains.ToolchainError('A staged cache directory is not readable.') from None
            for child in children:
                child_relative = child.name if relative == '.' else relative + '/' + child.name
                visit(Path(child.path), child_relative)

    visit(stage, '.')
    entries.sort(key=lambda entry: entry['path'])
    return {'schema': CACHE_SCHEMA, 'digest': _canonical_digest(kind, entries), 'type': kind,
            'created_at': time.time(), 'last_used': time.time(), 'entries': entries}


def _file_digest(path: Path, expected: dict | None = None) -> str:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
                     | _O_NOATIME)
    except OSError as error:
        raise toolchains.ToolchainError('A cache file is unavailable or unsafe.') from error
    digest = hashlib.sha256()
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise toolchains.ToolchainError('A cache file is not an ordinary independent file.')
        if expected is not None and ((info.st_dev, info.st_ino) != (expected['dev'], expected['ino'])
                                     or info.st_size != expected['size']
                                     or info.st_uid != expected['uid']):
            raise toolchains.ToolchainError('A cache file changed identity while being verified.')
        for chunk in iter(lambda: stream.read(1 << 20), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _chmod_immutable(root: Path, *, include_root: bool = True) -> None:
    directories = []
    stack = [root]
    while stack:
        path = stack.pop()
        info = os.lstat(path)
        if stat.S_ISDIR(info.st_mode):
            directories.append(path)
            matches = list(os.scandir(path))
            for match in matches:
                stack.append(Path(match.path))
        elif stat.S_ISREG(info.st_mode):
            os.chmod(path, _immutable_mode(stat.S_IMODE(info.st_mode), directory=False), follow_symlinks=False)
        else:
            raise toolchains.ToolchainError('Cache entries may contain only ordinary files and directories.')
    selected = directories if include_root else [item for item in directories if item != root]
    for path in sorted(selected, key=lambda item: len(item.parts), reverse=True):
        os.chmod(path, 0o500, follow_symlinks=False)


def _child_layout(layout: _CacheLayout, kind: str) -> tuple[Path, tuple[int, int], int]:
    if kind == 'entries':
        return layout.entries, layout.entries_identity, layout.entries_mount
    if kind == 'manifests':
        return layout.manifests, layout.manifests_identity, layout.manifests_mount
    if kind == 'staging':
        return layout.staging, layout.staging_identity, layout.staging_mount
    raise toolchains.ToolchainError('Unknown shared toolchain cache layout area.')


@contextmanager
def _verified_parent(layout: _CacheLayout, kind: str):
    root_fd = _open_directory_chain(layout.root)
    try:
        _verify_root_path(layout, root_fd)
        child_fd = _open_layout_child(root_fd, {
            'entries': ENTRIES_DIRNAME,
            'manifests': MANIFESTS_DIRNAME,
            'staging': _STAGING_DIRNAME,
        }[kind])
        try:
            info = os.fstat(child_fd)
            _, expected_identity, expected_mount = _child_layout(layout, kind)
            if _identity(info) != expected_identity or _mount_id(child_fd) != expected_mount:
                raise toolchains.ToolchainError(
                    'A shared toolchain cache layout directory was replaced; no cache data was changed.')
            yield child_fd
        finally:
            os.close(child_fd)
    finally:
        os.close(root_fd)


def _validate_deletion_tree(parent_fd: int, name: str, *, base_dev: int,
                            base_mount: int) -> dict:
    """Validate a complete deletion target before any mode or name is changed."""
    try:
        info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError as error:
        raise toolchains.ToolchainError(
            f'Cache deletion target is unavailable: {_display(name)}.') from error
    if info.st_uid != _deletion_owner_uid():
        raise toolchains.ToolchainError('Cache deletion target is not owned by the current user.')
    if info.st_dev != base_dev:
        raise toolchains.ToolchainError('Cache deletion target crosses a filesystem boundary.')
    if stat.S_ISLNK(info.st_mode):
        raise toolchains.ToolchainError('Cache deletion target is a link and is preserved.')
    if stat.S_ISDIR(info.st_mode):
        try:
            fd = os.open(name, _directory_flags(info), dir_fd=parent_fd)
        except OSError as error:
            raise toolchains.ToolchainError('Cache deletion target directory is unreadable.') from error
        try:
            opened = os.fstat(fd)
            if _identity(opened) != _identity(info):
                raise toolchains.ToolchainError('Cache deletion target changed identity.')
            if _mount_id(fd) != base_mount:
                raise toolchains.ToolchainError(
                    'Cache deletion target crosses a mount boundary and is preserved.')
            try:
                children = []
                for entry in sorted(os.scandir(fd), key=lambda item: item.name):
                    children.append(_validate_deletion_tree(
                        fd, entry.name, base_dev=base_dev, base_mount=base_mount))
            except OSError as error:
                raise toolchains.ToolchainError('Cache deletion target directory is unreadable.') from error
            return {'name': name, 'kind': 'dir', 'identity': _identity(info), 'children': children}
        finally:
            os.close(fd)
    if stat.S_ISREG(info.st_mode):
        if info.st_nlink != 1:
            raise toolchains.ToolchainError('Cache deletion target is hardlinked and is preserved.')
        try:
            fd = os.open(name, _O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent_fd)
        except OSError as error:
            raise toolchains.ToolchainError('Cache deletion target is unreadable.') from error
        try:
            opened = os.fstat(fd)
            if _identity(opened) != _identity(info):
                raise toolchains.ToolchainError('Cache deletion target changed identity.')
            if _mount_id(fd) != base_mount:
                raise toolchains.ToolchainError(
                    'Cache deletion target crosses a mount boundary and is preserved.')
        finally:
            os.close(fd)
        return {'name': name, 'kind': 'file', 'identity': _identity(info), 'children': []}
    raise toolchains.ToolchainError('Cache deletion target is not an ordinary file or directory.')


def _delete_validated_tree(fd: int, plan: dict, *, base_mount: int) -> None:
    for child in plan['children']:
        name = child['name']
        try:
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
        except OSError as error:
            raise toolchains.ToolchainError('Cache deletion target changed during removal.') from error
        if _identity(info) != child['identity']:
            raise toolchains.ToolchainError('Cache deletion target changed identity during removal.')
        if child['kind'] == 'dir':
            try:
                child_fd = os.open(name, _directory_flags(info), dir_fd=fd)
            except OSError as error:
                raise toolchains.ToolchainError('Cache deletion target directory became unreadable.') from error
            try:
                if (_identity(os.fstat(child_fd)) != child['identity']
                        or _mount_id(child_fd) != base_mount):
                    raise toolchains.ToolchainError('Cache deletion target changed identity during removal.')
                os.fchmod(child_fd, 0o700)
                _delete_validated_tree(child_fd, child, base_mount=base_mount)
            finally:
                os.close(child_fd)
            os.rmdir(name, dir_fd=fd)
        else:
            try:
                child_fd = os.open(name, _O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
            except OSError as error:
                raise toolchains.ToolchainError(
                    'Cache deletion target became unreadable during removal.') from error
            try:
                if (_identity(os.fstat(child_fd)) != child['identity']
                        or _mount_id(child_fd) != base_mount):
                    raise toolchains.ToolchainError(
                        'Cache deletion target changed identity during removal.')
            finally:
                os.close(child_fd)
            os.unlink(name, dir_fd=fd)


def _delete_quarantined(parent_fd: int, name: str, plan: dict) -> None:
    info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if _identity(info) != plan['identity']:
        raise toolchains.ToolchainError('Cache deletion quarantine changed identity.')
    if plan['kind'] == 'file':
        os.unlink(name, dir_fd=parent_fd)
    else:
        fd = os.open(name, _directory_flags(info), dir_fd=parent_fd)
        try:
            if _identity(os.fstat(fd)) != plan['identity']:
                raise toolchains.ToolchainError('Cache deletion quarantine changed identity.')
            base_mount = _mount_id(fd)
            os.fchmod(fd, 0o700)
            _delete_validated_tree(fd, plan, base_mount=base_mount)
        finally:
            os.close(fd)
        os.rmdir(name, dir_fd=parent_fd)
    try:
        os.fsync(parent_fd)
    except OSError:
        pass


def _remove_cache_path(path: Path, *, layout: _CacheLayout | None = None,
                       kind: str | None = None,
                       expected_identity: tuple[int, int] | None = None) -> bool:
    """Atomically quarantine and remove one validated, current-user-owned cache node."""
    path = _absolute(path)
    if layout is None:
        root = path.parent.parent
        root_fd = _open_directory_chain(root)
        try:
            layout = _layout_from_fds(root, root_fd)
        finally:
            os.close(root_fd)
    if kind is None:
        kinds = {layout.entries: 'entries', layout.manifests: 'manifests', layout.staging: 'staging'}
        kind = kinds.get(path.parent)
    if kind is None:
        raise toolchains.ToolchainError('Cache deletion target is outside a managed cache area.')
    parent, _, _ = _child_layout(layout, kind)
    if path.parent != parent or path.name in ('', '.', '..') or os.sep in path.name:
        raise toolchains.ToolchainError('Cache deletion target is outside a managed cache area.')
    with _verified_parent(layout, kind) as parent_fd:
        try:
            info = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return False
        except OSError as error:
            raise toolchains.ToolchainError('Cache deletion target is unavailable.') from error
        if expected_identity is not None and _identity(info) != expected_identity:
            raise toolchains.ToolchainError('Cache deletion target changed identity and is preserved.')
        parent_info = os.fstat(parent_fd)
        plan = _validate_deletion_tree(parent_fd, path.name, base_dev=parent_info.st_dev,
                                       base_mount=_mount_id(parent_fd))
        quarantine = '.purge-' + uuid.uuid4().hex
        while True:
            try:
                os.stat(quarantine, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                break
            quarantine = '.purge-' + uuid.uuid4().hex
        from .atomic_rename import move
        move(parent_fd, path.name, parent_fd, quarantine)
        started = False
        try:
            moved = os.stat(quarantine, dir_fd=parent_fd, follow_symlinks=False)
            if _identity(moved) != plan['identity']:
                raise toolchains.ToolchainError(
                    'Cache deletion target changed identity while being quarantined.')
            verified = _validate_deletion_tree(parent_fd, quarantine, base_dev=parent_info.st_dev,
                                               base_mount=_mount_id(parent_fd))
            if verified['identity'] != plan['identity']:
                raise toolchains.ToolchainError('Cache deletion target changed identity before removal.')
            started = True
            _delete_quarantined(parent_fd, quarantine, verified)
            return True
        except BaseException:
            if not started:
                try:
                    move(parent_fd, quarantine, parent_fd, path.name)
                except OSError:
                    pass
            raise


def _manifest_path(manifests: Path, digest: str) -> Path:
    return manifests / (digest + '.json')


def _read_manifest(manifests: Path, digest: str) -> dict:
    path = _manifest_path(manifests, digest)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
                     | _O_NOATIME)
    except FileNotFoundError:
        raise toolchains.ToolchainError('A cache entry has no manifest and is never reused.') from None
    except OSError as error:
        raise toolchains.ToolchainError('A cache manifest is unsafe or unreadable.') from error
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or info.st_uid != _owner_uid() or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_size > _MAX_MANIFEST_BYTES):
            raise toolchains.ToolchainError('A cache manifest is not a private owned ordinary file.')
        raw = stream.read(_MAX_MANIFEST_BYTES + 1)
    try:
        payload = json.loads(raw.decode('utf-8'))
    except (UnicodeError, ValueError):
        raise toolchains.ToolchainError('A cache manifest is malformed.') from None
    expected = {'schema', 'digest', 'type', 'created_at', 'last_used', 'entries'}
    if not isinstance(payload, dict) or set(payload) != expected:
        raise toolchains.ToolchainError('A cache manifest has unknown or missing fields.')
    if (payload.get('schema') != CACHE_SCHEMA or payload.get('digest') != digest
            or payload.get('type') not in ('file', 'dir')
            or not isinstance(payload.get('created_at'), (int, float))
            or isinstance(payload.get('created_at'), bool)
            or not isinstance(payload.get('last_used'), (int, float))
            or isinstance(payload.get('last_used'), bool)
            or not isinstance(payload.get('entries'), list)):
        raise toolchains.ToolchainError('A cache manifest is malformed.')
    return payload


def _valid_relative(relative) -> bool:
    if not isinstance(relative, str) or not relative or '\0' in relative or '\\' in relative:
        return False
    if relative == '.':
        return True
    pure = Path(relative)
    return not pure.is_absolute() and '..' not in pure.parts and pure.as_posix() == relative


def _validate_manifest_tree(entries_root: Path, digest: str, manifest: dict) -> None:
    entry = entries_root / digest
    try:
        root_info = os.lstat(entry)
    except FileNotFoundError:
        raise toolchains.ToolchainError('A cache entry is missing and cannot be reused.') from None
    except OSError as error:
        raise toolchains.ToolchainError('A cache entry is unsafe or unreadable.') from error
    expected = {}
    for item in manifest['entries']:
        if (not isinstance(item, dict)
                or set(item) != {'path', 'kind', 'mode', 'cached_mode', 'size', 'sha256',
                                 'uid', 'dev', 'ino'}
                or not _valid_relative(item.get('path'))
                or item.get('kind') not in ('file', 'dir')
                or not isinstance(item.get('mode'), int) or isinstance(item.get('mode'), bool)
                or not isinstance(item.get('cached_mode'), int) or isinstance(item.get('cached_mode'), bool)
                or not isinstance(item.get('size'), int) or isinstance(item.get('size'), bool)
                or not isinstance(item.get('uid'), int) or isinstance(item.get('uid'), bool)
                or not isinstance(item.get('dev'), int) or isinstance(item.get('dev'), bool)
                or not isinstance(item.get('ino'), int) or isinstance(item.get('ino'), bool)
                or (item['kind'] == 'file' and not isinstance(item.get('sha256'), str))
                or (item['kind'] == 'dir' and item.get('sha256') is not None)):
            raise toolchains.ToolchainError('A cache manifest contains a malformed entry.')
        if item['path'] in expected:
            raise toolchains.ToolchainError('A cache manifest contains duplicate paths.')
        expected[item['path']] = item
    root = expected.get('.')
    if root is None or root['kind'] != manifest['type']:
        raise toolchains.ToolchainError('A cache manifest root does not match its entry type.')
    actual_paths = set()
    for relative, item in expected.items():
        path = entry if relative == '.' else entry.joinpath(*Path(relative).parts)
        try:
            info = os.lstat(path)
        except OSError:
            raise toolchains.ToolchainError('A cache entry changed or disappeared during verification.') from None
        actual_paths.add(relative)
        kind = 'dir' if stat.S_ISDIR(info.st_mode) else 'file' if stat.S_ISREG(info.st_mode) else 'other'
        if kind != item['kind']:
            raise toolchains.ToolchainError('A cache entry has a different file type than its manifest.')
        if info.st_uid != _owner_uid() or info.st_uid != item['uid']:
            raise toolchains.ToolchainError('A cache entry has unsafe ownership.')
        if stat.S_IMODE(info.st_mode) != item['cached_mode']:
            raise toolchains.ToolchainError('A cache entry is writable or has changed permissions.')
        if (info.st_dev, info.st_ino) != (item['dev'], item['ino']):
            raise toolchains.ToolchainError('A cache entry identity changed after it was stored.')
        if kind == 'file':
            if info.st_nlink != 1 or info.st_size != item['size']:
                raise toolchains.ToolchainError('A cache file is hardlinked or has changed size.')
            if _file_digest(path, item) != item['sha256']:
                raise toolchains.ToolchainError('A cache file content does not match its manifest.')
        elif kind == 'dir':
            try:
                children = {child.name for child in os.scandir(path)}
            except OSError:
                raise toolchains.ToolchainError('A cache directory is unreadable.') from None
            expected_children = {Path(other).name for other in expected
                                 if other != relative
                                 and (Path(other).parent.as_posix() == relative
                                      or (relative == '.' and Path(other).parent.as_posix() == '.'))}
            if children != expected_children:
                raise toolchains.ToolchainError('A cache directory contains unrecorded entries.')
    if actual_paths != set(expected):
        raise toolchains.ToolchainError('A cache manifest is incomplete.')
    calculated = _canonical_digest(manifest['type'], manifest['entries'])
    if calculated != digest:
        raise toolchains.ToolchainError('A cache manifest digest does not match its content.')


def _entry_usage(entry: Path, manifest_file: Path) -> tuple[int, int]:
    payload_bytes, payload_inodes = _measure(entry, owned=True, ordinary=True)
    manifest_bytes, manifest_inodes = _measure(manifest_file, owned=True, ordinary=True)
    return payload_bytes + manifest_bytes, payload_inodes + manifest_inodes


def _manifest_requirement(manifest: dict) -> dict:
    """Bound a materialization from manifest apparent sizes, not sparse blocks."""
    total = {'disk_bytes': 0, 'inodes': 0}
    for item in manifest['entries']:
        total['inodes'] += 1
        if item['kind'] == 'dir':
            total['disk_bytes'] += toolchains._ALLOCATION_BLOCK
        else:
            total['disk_bytes'] += toolchains._rounded_allocation(item['size'])
    return total


def _valid_entries(layout: _CacheLayout) -> tuple[list[dict], list[str]]:
    valid = []
    invalid = []
    try:
        names = sorted(path.name for path in os.scandir(layout.entries)
                       if _DIGEST.fullmatch(path.name))
    except OSError:
        raise toolchains.ToolchainError('Shared toolchain cache entries are unreadable.') from None
    for digest in names:
        try:
            manifest = _read_manifest(layout.manifests, digest)
            _validate_manifest_tree(layout.entries, digest, manifest)
            manifest_file = _manifest_path(layout.manifests, digest)
            manifest_identity = _identity(os.lstat(manifest_file))
            used_bytes, used_inodes = _entry_usage(layout.entries / digest, manifest_file)
        except toolchains.ToolchainError:
            invalid.append(digest)
            continue
        valid.append({'digest': digest, 'manifest': manifest, 'bytes': used_bytes,
                      'inodes': used_inodes, 'manifest_identity': manifest_identity,
                      'last_used': float(manifest['last_used']),
                      'created_at': float(manifest['created_at'])})
    try:
        for path in os.scandir(layout.manifests):
            if path.name.endswith('.json') and _DIGEST.fullmatch(path.name[:-5]) \
                    and path.name[:-5] not in names:
                invalid.append(path.name[:-5])
    except OSError:
        raise toolchains.ToolchainError('Shared toolchain cache manifests are unreadable.') from None
    return valid, sorted(set(invalid))


def _usage(layout: _CacheLayout) -> tuple[int, int]:
    """Measure every entry, manifest, staging item and the lock in the cache."""
    total = [0, 0]
    for path in (layout.entries, layout.manifests, layout.staging):
        measured = _measure(path)
        total[0] += measured[0]
        total[1] += measured[1]
    try:
        with os.scandir(layout.root) as stream:
            root_names = sorted(entry.name for entry in stream
                                if entry.name not in {ENTRIES_DIRNAME, MANIFESTS_DIRNAME,
                                                      _STAGING_DIRNAME})
    except OSError as error:
        raise toolchains.ToolchainError('Shared toolchain cache root is unreadable.') from error
    for name in root_names:
        measured = _measure(layout.root / name)
        total[0] += measured[0]
        total[1] += measured[1]
    return total[0], total[1]


def _remove_entry(layout: _CacheLayout, item: dict) -> None:
    digest = item['digest']
    root = next(entry for entry in item['manifest']['entries'] if entry['path'] == '.')
    _remove_cache_path(layout.entries / digest, layout=layout, kind='entries',
                       expected_identity=(root['dev'], root['ino']))
    if item.get('manifest_identity') is not None:
        _remove_cache_path(_manifest_path(layout.manifests, digest), layout=layout, kind='manifests',
                           expected_identity=item['manifest_identity'])


def _empty_report(*, max_bytes: int, max_inodes: int) -> dict:
    return {'schema': CACHE_SCHEMA, 'entries': 0, 'bytes': 0, 'inodes': 0,
            'allocated_bytes': 0, 'allocated_inodes': 0, 'entries_bytes': 0,
            'manifests_bytes': 0, 'staging_bytes': 0, 'lock_bytes': 0,
            'max_bytes': max_bytes, 'max_inodes': max_inodes, 'within_budget': True,
            'invalid_entries': [], 'removed_entries': [], 'preserved_staging': [],
            'preserved_quarantine': []}


def _managed_target_reason(parent_fd: int, name: str, *, expected_mount: int) -> str | None:
    try:
        info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError:
        return 'target became unavailable'
    if stat.S_ISLNK(info.st_mode):
        return 'target is a link'
    if not stat.S_ISDIR(info.st_mode) and not stat.S_ISREG(info.st_mode):
        return 'target is not an ordinary file or directory'
    if info.st_uid != _deletion_owner_uid():
        return 'target is not owned by the current user'
    try:
        fd = os.open(name, _O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent_fd)
    except OSError:
        return 'target cannot be opened safely'
    try:
        moved = os.fstat(fd)
        if _identity(moved) != _identity(info):
            return 'target changed identity'
        if moved.st_dev != os.fstat(parent_fd).st_dev or _mount_id(fd) != expected_mount:
            return 'target crosses a filesystem or mount boundary'
    except toolchains.ToolchainError as error:
        return str(error)
    finally:
        os.close(fd)
    return None


def _scan_managed_names(layout: _CacheLayout, kind: str, patterns, *,
                        remove: bool, report_unknown: bool = False) -> list[dict]:
    preserved = []
    with _verified_parent(layout, kind) as parent_fd:
        mount = _mount_id(parent_fd)
        try:
            names = sorted(entry.name for entry in os.scandir(parent_fd))
        except OSError as error:
            raise toolchains.ToolchainError('Shared toolchain cache staging is unreadable.') from error
        for name in names:
            if not any(pattern.fullmatch(name) for pattern in patterns):
                if report_unknown:
                    preserved.append({'path': name, 'reason': 'unrecognized cache temporary name'})
                continue
            reason = _managed_target_reason(parent_fd, name, expected_mount=mount)
            if reason is not None:
                preserved.append({'path': name, 'reason': reason})
                continue
            if not remove:
                continue
            try:
                info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                _remove_cache_path(_child_layout(layout, kind)[0] / name, layout=layout, kind=kind,
                                   expected_identity=_identity(info))
            except toolchains.ToolchainError as error:
                preserved.append({'path': name, 'reason': str(error)})
    return preserved


def _clean_staging(layout: _CacheLayout, *, remove: bool = True) -> list[dict]:
    if not isinstance(layout, _CacheLayout):
        staging = _absolute(layout)
        root = staging.parent
        root_fd = _open_directory_chain(root)
        try:
            layout = _layout_from_fds(root, root_fd)
        finally:
            os.close(root_fd)
    return _scan_managed_names(layout, 'staging', (_STAGE_NAME, _PURGE_NAME), remove=remove,
                               report_unknown=True)


def _recover_quarantines(layout: _CacheLayout, *, remove: bool = True) -> list[dict]:
    preserved = []
    for kind in ('entries', 'manifests'):
        preserved.extend(_scan_managed_names(layout, kind, (_PURGE_NAME,), remove=remove))
    return preserved


def _report(layout: _CacheLayout, *, max_bytes: int, max_inodes: int, apply: bool) -> dict:
    if apply:
        preserved_quarantine = _recover_quarantines(layout, remove=True)
        preserved_staging = _clean_staging(layout, remove=True)
    else:
        preserved_quarantine = _recover_quarantines(layout, remove=False)
        preserved_staging = _clean_staging(layout, remove=False)
    valid, invalid = _valid_entries(layout)
    total_bytes, total_inodes = _usage(layout)
    removed = []
    if apply:
        for item in sorted(valid, key=lambda row: (row['last_used'], row['created_at'], row['digest'])):
            if total_bytes <= max_bytes and total_inodes <= max_inodes:
                break
            _remove_entry(layout, item)
            total_bytes -= item['bytes']
            total_inodes -= item['inodes']
            removed.append(item['digest'])
        valid, invalid = _valid_entries(layout)
        total_bytes, total_inodes = _usage(layout)
    entries_bytes = _measure(layout.entries)[0]
    manifests_bytes = _measure(layout.manifests)[0]
    staging_bytes = _measure(layout.staging)[0]
    try:
        lock_bytes = _measure(layout.root / _LOCK_NAME)[0]
    except toolchains.ToolchainError:
        if os.path.lexists(layout.root / _LOCK_NAME):
            raise
        lock_bytes = 0
    result = {'schema': CACHE_SCHEMA,
              'entries': len(valid),
              'bytes': total_bytes,
              'inodes': total_inodes,
              'allocated_bytes': total_bytes,
              'allocated_inodes': total_inodes,
              'entries_bytes': entries_bytes,
              'manifests_bytes': manifests_bytes,
              'staging_bytes': staging_bytes,
              'lock_bytes': lock_bytes,
              'max_bytes': max_bytes,
              'max_inodes': max_inodes,
              'within_budget': total_bytes <= max_bytes and total_inodes <= max_inodes,
              'invalid_entries': list(invalid),
              'removed_entries': removed,
              'preserved_staging': preserved_staging,
              'preserved_quarantine': preserved_quarantine}
    return result


def _measure_if_present(path: Path) -> tuple[int, int]:
    try:
        return _measure(path, owned=True, ordinary=True)
    except toolchains.ToolchainError:
        if not os.path.lexists(path):
            return 0, 0
        raise


def _make_room(layout: _CacheLayout, *, required_bytes: int, required_inodes: int,
               max_bytes: int, max_inodes: int, exclude_paths=()) -> None:
    valid, _ = _valid_entries(layout)
    total_bytes, total_inodes = _usage(layout)
    excluded_bytes = excluded_inodes = 0
    for path in exclude_paths:
        measured = _measure_if_present(path)
        excluded_bytes += measured[0]
        excluded_inodes += measured[1]
    total_bytes -= excluded_bytes
    total_inodes -= excluded_inodes
    if required_bytes > max_bytes or required_inodes > max_inodes:
        raise toolchains.ToolchainError(
            'The shared toolchain cache cannot store this source within its finite byte/inode budget. '
            'Choose a smaller declared source or raise the explicit cache limit.')
    removable_bytes = sum(item['bytes'] for item in valid)
    removable_inodes = sum(item['inodes'] for item in valid)
    floor_bytes = max(0, total_bytes - removable_bytes)
    floor_inodes = max(0, total_inodes - removable_inodes)
    if floor_bytes + required_bytes > max_bytes or floor_inodes + required_inodes > max_inodes:
        raise toolchains.ToolchainError(
            'The shared toolchain cache is full and its invalid or foreign entries were preserved for '
            'inspection. Remove stale cache data explicitly or raise the cache limit.')
    for item in sorted(valid, key=lambda row: (row['last_used'], row['created_at'], row['digest'])):
        if total_bytes + required_bytes <= max_bytes and total_inodes + required_inodes <= max_inodes:
            return
        _remove_entry(layout, item)
        total_bytes -= item['bytes']
        total_inodes -= item['inodes']
    if total_bytes + required_bytes > max_bytes or total_inodes + required_inodes > max_inodes:
        raise toolchains.ToolchainError(
            'The shared toolchain cache is full and its invalid or foreign entries were preserved for '
            'inspection. Remove stale cache data explicitly or raise the cache limit.')


def _touch(layout: _CacheLayout, digest: str) -> None:
    payload = _read_manifest(layout.manifests, digest)
    _validate_manifest_tree(layout.entries, digest, payload)
    _measure(layout.entries / digest, owned=True, ordinary=True)
    updated = dict(payload, last_used=time.time())
    toolchains._atomic_private_write(
        _manifest_path(layout.manifests, digest),
        (json.dumps(updated, indent=2, ensure_ascii=True, sort_keys=True) + '\n').encode())


def snapshot(state, source: Path, *, max_bytes: int = _DEFAULT_MAX_BYTES,
             max_inodes: int = _DEFAULT_MAX_INODES, _owned_copy=None) -> Path:
    """Copy one declared ordinary source into a bounded immutable shared entry."""
    max_bytes, max_inodes = _validate_caps(max_bytes, max_inodes)
    _, resolved = _validate_source(state, source)
    with toolchains._borrowed_workspace_source(_owned_copy, resolved):
        return _snapshot_locked_source(state, resolved, max_bytes=max_bytes, max_inodes=max_inodes)


def _snapshot_locked_source(state, source, *, max_bytes, max_inodes):
    max_bytes, max_inodes = _validate_caps(max_bytes, max_inodes)
    _, resolved = _validate_source(state, source)
    java = toolchains._java_tool(resolved)
    requirement = toolchains._materialization_requirement(
        resolved, java=java, label='cache source')
    requirement['disk_bytes'] += max(toolchains._ALLOCATION_BLOCK,
                                     requirement['inodes'] * 512)
    requirement['inodes'] += 1
    with locked(state) as layout:
        source_bytes, source_inodes = _measure(resolved)
        if source_bytes > max_bytes or source_inodes + 1 > max_inodes:
            raise toolchains.ToolchainError(
                'The shared toolchain cache cannot store this source within its finite byte/inode budget. '
                'Choose a smaller declared source or raise the explicit cache limit.')
        with toolchains._reserve_materialization(layout.staging, requirement):
            _recover_quarantines(layout, remove=True)
            _clean_staging(layout, remove=True)
            stage = layout.staging / ('.stage-' + uuid.uuid4().hex)
            manifest_temporary = layout.staging / (stage.name + '.json')
            published = False
            digest = None
            manifest = None
            try:
                if resolved.is_dir():
                    kind = 'dir'
                    toolchains._copy_tree(resolved, stage, java=toolchains._java_tool(resolved),
                                          label='cache source')
                else:
                    kind = 'file'
                    stats = {'files': 0, 'bytes': 0, 'links_skipped': 0}
                    toolchains._copy_file(resolved, stage, stats)
                manifest = _stage_manifest(stage, kind)
                digest = manifest['digest']
                entry = layout.entries / digest
                manifest_path = _manifest_path(layout.manifests, digest)
                if os.path.lexists(entry) or os.path.lexists(manifest_path):
                    _touch(layout, digest)
                    _remove_cache_path(stage, layout=layout, kind='staging')
                    return entry
                _chmod_immutable(stage, include_root=False)
                payload_bytes, payload_inodes = _measure(stage)
                manifest_content = (json.dumps(manifest, indent=2, ensure_ascii=True,
                                               sort_keys=True) + '\n').encode()
                toolchains._atomic_private_write(manifest_temporary, manifest_content)
                manifest_bytes, manifest_inodes = _measure(manifest_temporary)
                _make_room(layout, required_bytes=payload_bytes + manifest_bytes,
                           required_inodes=payload_inodes + manifest_inodes,
                           max_bytes=max_bytes, max_inodes=max_inodes,
                           exclude_paths=(stage, manifest_temporary))
                os.replace(stage, entry)
                published = True
                root_entry = next(item for item in manifest['entries'] if item['path'] == '.')
                root_mode = 0o500 if kind == 'dir' else root_entry['cached_mode']
                os.chmod(entry, root_mode, follow_symlinks=False)
                directory = os.open(layout.entries, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
                os.replace(manifest_temporary, manifest_path)
                directory = os.open(layout.manifests, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
                actual_bytes, actual_inodes = _entry_usage(entry, manifest_path)
                if actual_bytes > max_bytes or actual_inodes > max_inodes:
                    item = {'digest': digest, 'manifest': manifest,
                            'manifest_identity': _identity(os.lstat(manifest_path))}
                    _remove_entry(layout, item)
                    raise toolchains.ToolchainError(
                        'The shared toolchain cache rejected a source that exceeded its finite byte/inode '
                        'budget; no entry was retained.')
                return entry
            except BaseException:
                try:
                    _remove_cache_path(stage, layout=layout, kind='staging')
                    _remove_cache_path(manifest_temporary, layout=layout, kind='staging')
                except (OSError, toolchains.ToolchainError):
                    pass
                if published:
                    try:
                        manifest_path = _manifest_path(layout.manifests, digest)
                        if manifest is not None:
                            manifest_identity = (_identity(os.lstat(manifest_path))
                                                 if os.path.lexists(manifest_path) else None)
                            item = {'digest': digest, 'manifest': manifest,
                                    'manifest_identity': manifest_identity}
                            _remove_entry(layout, item)
                    except (OSError, toolchains.ToolchainError):
                        pass
                raise


def _entry_for_path(state: Path, value: Path) -> tuple[Path, Path, str] | None:
    root = _cache_root(state)
    entries_root = root / ENTRIES_DIRNAME
    path = Path(value)
    if path.parent != entries_root or _DIGEST.fullmatch(path.name) is None:
        return None
    return root, entries_root, path.name


def is_entry_path(state, value) -> bool:
    """True only for a lexical path in the managed entries directory."""
    try:
        return _entry_for_path(Path(state), Path(value)) is not None
    except (TypeError, ValueError):
        return False


def validate_entry(state, value, *, require_directory: bool = False) -> Path:
    """Validate a cached source under a shared lock and return its ordinary path."""
    path = Path(value)
    located = _entry_for_path(Path(state), path)
    if located is None:
        raise toolchains.ToolchainError('A managed cache source must be an entry returned by snapshot().')
    _, _, digest = located
    with reading(state) as layout:
        manifest = _read_manifest(layout.manifests, digest)
        _validate_manifest_tree(layout.entries, digest, manifest)
        _measure(layout.entries / digest, owned=True, ordinary=True)
        if require_directory and manifest['type'] != 'dir':
            raise toolchains.ToolchainError('A prepared tool source must be an ordinary cached directory.')
        return layout.entries / digest


def materialization_requirement(state, cached) -> dict:
    """Return conservative bytes/inodes for copying one managed cache entry."""
    located = _entry_for_path(Path(state), Path(cached))
    if located is None:
        raise toolchains.ToolchainError('Only a managed cache entry may be budgeted.')
    _, _, digest = located
    with reading(state) as layout:
        manifest = _read_manifest(layout.manifests, digest)
        _validate_manifest_tree(layout.entries, digest, manifest)
        return _manifest_requirement(manifest)


def _validate_materialize_destination(destination: Path) -> None:
    parent = destination.parent
    try:
        info = os.lstat(parent)
    except OSError:
        raise toolchains.ToolchainError(
            f'Cache materialization destination parent is unavailable: {_display(parent)}.', 78) from None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise toolchains.ToolchainError(
            f'Cache materialization destination parent is unsafe: {_display(parent)}.', 78)
    if info.st_uid != _owner_uid():
        raise toolchains.ToolchainError(
            f'Cache materialization destination parent is not owned by the current user: '
            f'{_display(parent)}.', 78)
    if workspace._credential_like(destination):
        raise toolchains.ToolchainError(
            f'Refusing credential-like cache materialization destination: {_display(destination)}.')
    if os.path.lexists(destination):
        raise toolchains.ToolchainError(
            f'Cache materialization destination already exists: {_display(destination)}.')


def _restore_modes(destination: Path, manifest: dict) -> None:
    for item in sorted(manifest['entries'], key=lambda row: len(Path(row['path']).parts), reverse=True):
        path = destination if item['path'] == '.' else destination.joinpath(*Path(item['path']).parts)
        os.chmod(path, item['mode'], follow_symlinks=False)


@contextmanager
def _nullcontext():
    yield


def materialize(state, cached: Path, destination: Path, *, java: bool = False,
                label: str = 'cache source', reserve: bool = True) -> dict:
    """Copy one fully verified cache entry independently into an active workspace."""
    located = _entry_for_path(Path(state), Path(cached))
    if located is None:
        raise toolchains.ToolchainError('Only a managed cache entry may be materialized.')
    destination = _absolute(destination)
    _validate_materialize_destination(destination)
    _, _, digest = located
    with reading(state) as layout:
        manifest = _read_manifest(layout.manifests, digest)
        _validate_manifest_tree(layout.entries, digest, manifest)
        entry = layout.entries / digest
        _measure(entry, owned=True, ordinary=True)
        requirement = _manifest_requirement(manifest)
        admission = (toolchains._reserve_materialization(destination.parent, requirement)
                     if reserve else _nullcontext())
        with admission:
            if manifest['type'] == 'dir':
                stats = toolchains._copy_tree(entry, destination, java=java, label=label)
            else:
                stats = {'files': 0, 'bytes': 0, 'links_skipped': 0}
                toolchains._copy_file(entry, destination, stats)
            _restore_modes(destination, manifest)
            return stats


def collect(state, *, apply: bool = False, max_bytes: int = _DEFAULT_MAX_BYTES,
            max_inodes: int = _DEFAULT_MAX_INODES) -> dict:
    """Return compact cache allocation data and optionally evict oldest idle entries."""
    max_bytes, max_inodes = _validate_caps(max_bytes, max_inodes)
    if not isinstance(apply, bool):
        raise toolchains.ToolchainError('apply must be a boolean.')
    if apply:
        with locked(state) as layout:
            return _report(layout, max_bytes=max_bytes, max_inodes=max_inodes, apply=True)
    with _inspection(state) as layout:
        if layout is None:
            return _empty_report(max_bytes=max_bytes, max_inodes=max_inodes)
        return _report(layout, max_bytes=max_bytes, max_inodes=max_inodes, apply=False)
