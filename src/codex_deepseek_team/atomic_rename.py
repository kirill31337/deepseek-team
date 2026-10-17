"""Linux descriptor-relative moves that never replace a concurrent destination."""
import ctypes
import errno
import os


def move(source_fd, source, destination_fd, destination):
    """Atomically move one basename, preserving both entries on collision.

    Cleanup requires Linux already. An unavailable kernel/libc operation keeps
    the quarantine intact; falling back to rename() would permit overwriting.
    """
    names = [os.fsencode(name) for name in (source, destination)]
    if any(not name or name in (b'.', b'..') or b'/' in name or b'\0' in name for name in names):
        raise ValueError('Atomic cleanup moves require basenames.')
    operation = getattr(ctypes.CDLL(None, use_errno=True), 'renameat2', None)
    if operation is None:
        raise OSError(errno.ENOSYS, 'Atomic no-replace cleanup rename is unavailable')
    operation.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int,
                         ctypes.c_char_p, ctypes.c_uint)
    operation.restype = ctypes.c_int
    if operation(source_fd, names[0], destination_fd, names[1], 1):
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code), os.fsdecode(names[1]))
