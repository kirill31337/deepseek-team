"""Private machine-local storage choice, separate from project policy and keys."""
from contextlib import contextmanager
import errno
import fcntl
import json
import os
from pathlib import Path
import stat
import uuid

from .state_storage import StorageError, open_private_directory


def _directory():
    base = Path(os.environ.get('XDG_CONFIG_HOME') or Path.home() / '.config')
    if not base.is_absolute() or '..' in base.parts:
        raise StorageError('XDG_CONFIG_HOME must be an absolute canonical path.')
    return base / 'deepseek-team/storage'


def _file(directory, name, *, create=False):
    flags = os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
    flags |= os.O_RDWR | os.O_CREAT if create else os.O_RDONLY
    fd = os.open(name, flags, 0o600, dir_fd=directory)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600):
            raise StorageError('Saved storage selection requires private owned ordinary files (600).')
        return fd
    except BaseException:
        os.close(fd)
        raise


def read_root(directory=None):
    opened = None
    try:
        if directory is None:
            try:
                opened = open_private_directory(_directory(), label='Storage selection directory')
            except StorageError as error:
                if error.errno == errno.ENOENT:
                    return None
                raise
            directory = opened
        try:
            fd = _file(directory, 'selection.json')
        except FileNotFoundError:
            return None
        with os.fdopen(fd, 'rb') as stream:
            raw = stream.read(8193)
        if len(raw) > 8192:
            raise ValueError('oversized')
        value = json.loads(raw)
        if (not isinstance(value, dict) or set(value) != {'schema', 'root', 'previous'}
                or type(value['schema']) is not int or value['schema'] != 1):
            raise ValueError('schema')
        for field in ('root', 'previous'):
            if not isinstance(value[field], str) or not value[field] or '\0' in value[field]:
                raise ValueError('path')
            path = Path(value[field])
            if not path.is_absolute() or '..' in path.parts:
                raise ValueError('path')
        return Path(value['root'])
    except (OSError, ValueError, UnicodeError):
        raise StorageError(f'Cannot safely read saved storage selection: {_directory() / "selection.json"}. '
                           'The file and retained data were preserved.') from None
    finally:
        if opened is not None:
            os.close(opened)


@contextmanager
def locked():
    directory = lock = None
    try:
        directory = open_private_directory(_directory(), create=True,
                                           label='Storage selection directory')
        lock = _file(directory, 'selection.lock', create=True)
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield directory
    except OSError:
        raise StorageError(f'Cannot lock private storage selection: {_directory()}.') from None
    finally:
        if lock is not None:
            os.close(lock)
        if directory is not None:
            os.close(directory)


def publish(directory, root, previous):
    temporary = '.selection-' + uuid.uuid4().hex
    created = False
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=directory)
        created = True
        with os.fdopen(fd, 'wb') as stream:
            raw = json.dumps(dict(schema=1, root=str(root), previous=str(previous))).encode() + b'\n'
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, 'selection.json', src_dir_fd=directory, dst_dir_fd=directory)
        created = False
        os.fsync(directory)
    except OSError:
        raise StorageError('Could not durably save the automatic storage selection. '
                           'Inspect the local storage selection before starting workers.') from None
    finally:
        if created:
            os.unlink(temporary, dir_fd=directory)
