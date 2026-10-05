"""Shared state selection and provider-free private storage readiness.

Never resolve a symbolic link into an accepted storage directory, chmod an
existing entry, or relocate retained workspaces and coordination records.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import uuid


class StorageError(Exception):
    def __init__(self, message: str, code: int = 78, *, errno=None):
        self.message, self.code, self.errno = message, code, errno
        super().__init__(message)


def state_root(explicit=None) -> Path:
    """Command override > live environment > saved recovery > user default.

    Explicit command paths retain support for relative paths. Environment
    overrides must be absolute, without parent traversal, as required by the
    routing and lessons stores. Normalization deliberately does not follow links.
    """
    if explicit is not None:
        return Path(explicit).absolute()
    configured = os.environ.get('DEEPSEEK_TEAM_STATE_DIR')
    if configured:
        path = Path(configured)
        if not path.is_absolute() or '..' in path.parts:
            raise StorageError('DEEPSEEK_TEAM_STATE_DIR must be an absolute canonical path.')
        return path
    from . import storage_selection
    return storage_selection.read_root() or Path.home() / '.local/state/codex-deepseek'


def _problem(path, reason, label):
    displayed = json.dumps(str(path), ensure_ascii=True)
    return StorageError(
        f'{label} {reason}: {displayed}. Use an ordinary current-user-owned '
        'state root (700) with --state-dir, or set DEEPSEEK_TEAM_STATE_DIR '
        'consistently for the coordinator and commands. Existing entries and '
        'retained data were not changed or relocated.')


def _validate(info, path, label):
    if stat.S_ISLNK(info.st_mode):
        raise _problem(path, 'is a symbolic link', label)
    if not stat.S_ISDIR(info.st_mode):
        raise _problem(path, 'is not an ordinary directory', label)
    if info.st_uid != os.geteuid():
        raise _problem(path, f'has a different owner (uid {info.st_uid}; expected {os.geteuid()})', label)
    if stat.S_IMODE(info.st_mode) != 0o700:
        raise _problem(path, f'has unsafe permissions ({stat.S_IMODE(info.st_mode):o}; expected 700)', label)


def _open_component(parent, name, path, *, create, private, label):
    try:
        if create:
            try:
                os.mkdir(name, mode=0o700, dir_fd=parent)
            except FileExistsError:
                pass  # Inspect the concurrent entry; never adopt a link.
        info = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if private:
            _validate(info, path, label)
        elif stat.S_ISLNK(info.st_mode):
            raise _problem(path, 'is a symbolic link', label)
        elif not stat.S_ISDIR(info.st_mode):
            raise _problem(path, 'is not an ordinary directory', label)
        fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                     dir_fd=parent)
        try:
            opened = os.fstat(fd)
            if private:
                _validate(opened, path, label)
            if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                raise _problem(path, 'changed during its readiness check', label)
        except BaseException:
            os.close(fd)
            raise
        return fd
    except OSError as cause:
        error = _problem(path, 'is missing or inaccessible', label)
        error.errno = cause.errno
        raise error from None


def _open_directory(path: Path, *, create=False, parent=None, private=True,
                    label='Workspace control directory') -> int:
    """Return an owned descriptor without traversing links in any component.

    Only the final directory requires ownership and mode 700; public system
    ancestors are allowed. ``parent`` pins the root for its workspaces child.
    The caller owns the returned descriptor and must close it.
    """
    path = Path(path).absolute()
    if parent is not None:
        return _open_component(parent, path.name, path, create=create, private=True,
                               label=label)
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        current = Path('/')
        for index, part in enumerate(path.parts[1:], start=1):
            current = current / part
            child = _open_component(fd, part, current, create=create,
                                    private=private and index == len(path.parts) - 1, label=label)
            os.close(fd)
            fd = child
        if not private and os.fstat(fd).st_uid != os.geteuid():
            raise _problem(path, 'has a different owner', label)
        if path == Path('/') and private:
            _validate(os.fstat(fd), path, label)
        result, fd = fd, None
        return result
    finally:
        if fd is not None:
            os.close(fd)


def open_private_directory(path: Path, *, create=False, parent=None,
                           label='Workspace control directory') -> int:
    return _open_directory(path, create=create, parent=parent, label=label)


def open_owned_directory_for_inspection(path: Path) -> int:
    """Inspect locks in a rejected owned root without adopting its permissions."""
    return _open_directory(path, private=False, label='Previous worker state root')


def check_private_directory(path: Path, *, create=False) -> None:
    fd = open_private_directory(path, create=create)
    os.close(fd)


def _prepare_exact(root: Path) -> Path:
    """Create missing private directories and prove local allocation can work.

    Probes only a uniquely named temporary directory and file, opened relative
    to validated descriptors. No workspace, lock slot, ledger, key or provider
    is opened; existing directory permissions are never repaired implicitly.
    """
    state_fd = open_private_directory(root, create=True, label='Worker state root')
    copies_fd = probe_fd = None
    name = '.storage-check-' + uuid.uuid4().hex
    created = False
    try:
        copies_fd = open_private_directory(root / 'workspaces', create=True,
                                           parent=state_fd)
        os.mkdir(name, mode=0o700, dir_fd=copies_fd)
        created = True
        probe_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                           dir_fd=copies_fd)
        fd = os.open('allocation', os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=probe_fd)
        os.close(fd)
        return root
    except OSError:
        raise _problem(root / 'workspaces', 'cannot allocate private working data',
                       'Workspace control directory') from None
    finally:
        try:
            if probe_fd is not None:
                try:
                    os.unlink('allocation', dir_fd=probe_fd)
                except FileNotFoundError:
                    pass
                finally:
                    os.close(probe_fd)
            if created:
                os.rmdir(name, dir_fd=copies_fd)
        except OSError:
            raise _problem(root / 'workspaces', 'could not clean up its allocation probe',
                           'Workspace control directory') from None
        finally:
            if copies_fd is not None:
                os.close(copies_fd)
            os.close(state_fd)


def prepare_storage(explicit=None) -> Path:
    """Prepare explicit/saved storage, or recover an unusable idle default once."""
    from . import storage_selection, worker_slots
    root = state_root(explicit)
    try:
        return _prepare_exact(root)
    except StorageError:
        if explicit is not None or os.environ.get('DEEPSEEK_TEAM_STATE_DIR'):
            raise
        selected = storage_selection.read_root()
        if selected is not None:
            if selected == root:
                raise  # Never silently discard an existing saved choice/history.
            return _prepare_exact(selected)
    with storage_selection.locked() as selection:
        selected = storage_selection.read_root(selection)
        if selected is not None:
            return _prepare_exact(selected)
        # Recheck after serializing concurrent bootstraps; another process may
        # have repaired the default without selecting a replacement.
        try:
            return _prepare_exact(root)
        except StorageError:
            pass
        try:
            with worker_slots.idle_state(root):
                replacement = Path.home() / ('.deepseek-team-state-' + uuid.uuid4().hex)
                _prepare_exact(replacement)
                storage_selection.publish(selection, replacement, root)
                return replacement
        except worker_slots.SlotError as error:
            raise StorageError('Automatic storage recovery could not verify that the previous '
                               'root has no active or queued workers: ' + str(error), error.code) from None
