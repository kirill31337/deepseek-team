"""Linux inotify hints for worker-slot waiters.

The allocator remains correct without this module: notifications are only
hints, and every wake is followed by the normal validation and admission
probe. The watch is created before probing so a release between the probe and
the wait cannot be lost. Unsupported platforms or inaccessible watches fall
back to the allocator's bounded sleep interval.
"""
from __future__ import annotations

import ctypes
import errno
import os
import re
import select
import struct
import time


IN_CLOSE_WRITE = 0x00000008
IN_MOVED_FROM = 0x00000040
IN_MOVED_TO = 0x00000080
IN_DELETE = 0x00000200
IN_DELETE_SELF = 0x00000400
IN_Q_OVERFLOW = 0x00004000
IN_IGNORED = 0x00008000
IN_ONLYDIR = 0x01000000
IN_NONBLOCK = 0o4000
IN_CLOEXEC = 0o2000000
_WATCH_MASK = (IN_CLOSE_WRITE | IN_MOVED_FROM | IN_MOVED_TO | IN_DELETE
               | IN_DELETE_SELF | IN_ONLYDIR)
_EVENT = struct.Struct('iIII')
_SLOT = re.compile(r'worker-\d+\.lock\Z')
_TICKET = re.compile(r'ticket-\d+\Z')


class NotifierUnavailable(Exception):
    """The kernel notification path cannot be used for this wait."""


def _open_libc():
    try:
        library = ctypes.CDLL(None, use_errno=True)
        init = library.inotify_init1
        add = library.inotify_add_watch
    except (OSError, AttributeError) as error:
        raise NotifierUnavailable('inotify is unavailable') from error
    init.argtypes = [ctypes.c_int]
    init.restype = ctypes.c_int
    add.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
    add.restype = ctypes.c_int
    return init, add


def _system_error(operation):
    code = ctypes.get_errno() or errno.ENOSYS
    return OSError(code, f'{operation}: {os.strerror(code)}')


def _relevant_events(data):
    """Return ``(relevant, broken)`` for one drained inotify buffer."""
    offset = 0
    relevant = False
    broken = False
    while offset + _EVENT.size <= len(data):
        _wd, mask, _cookie, length = _EVENT.unpack_from(data, offset)
        offset += _EVENT.size
        raw = data[offset:offset + length]
        offset += length
        name = raw.rstrip(b'\0').decode('utf-8', 'surrogateescape') if raw else ''
        if mask & IN_Q_OVERFLOW:
            broken = True
            continue
        if mask & IN_IGNORED:
            broken = True
            continue
        if mask & IN_CLOSE_WRITE and _SLOT.fullmatch(name):
            # Read-only probes produce IN_CLOSE_NOWRITE, and allocator/ticket
            # closes are filtered by name, so a waiter's own probe cannot spin.
            relevant = True
        elif mask & (IN_MOVED_FROM | IN_MOVED_TO | IN_DELETE | IN_DELETE_SELF):
            if not name or _SLOT.fullmatch(name) or _TICKET.fullmatch(name):
                relevant = True
    if offset != len(data):
        broken = True
    return relevant, broken


class SlotNotifier:
    """One pinned-directory inotify watch owned until :meth:`close`."""

    def __init__(self, directory: int):
        try:
            init, add = _open_libc()
        except (OSError, AttributeError) as error:
            raise NotifierUnavailable('inotify is unavailable') from error
        fd = init(IN_NONBLOCK | IN_CLOEXEC)
        if fd < 0:
            raise NotifierUnavailable(str(_system_error('inotify_init1')))
        self._fd = fd
        self._usable = False
        try:
            # /proc/self/fd keeps the watch anchored to the already-validated
            # directory descriptor even if the path is renamed concurrently.
            path = f'/proc/self/fd/{directory}'.encode()
            watch = add(fd, path, _WATCH_MASK)
            if watch < 0:
                raise _system_error('inotify_add_watch')
        except OSError as error:
            self.close()
            raise NotifierUnavailable(str(error)) from error
        self._usable = True

    @property
    def usable(self) -> bool:
        return self._usable and self._fd is not None

    def wait(self, timeout: float) -> bool:
        """Wait for a relevant hint, returning False on fallback timeout."""
        if not self.usable:
            return False
        deadline = time.monotonic() + max(0.0, float(timeout))
        while True:
            remaining = max(0.0, deadline - time.monotonic())
            try:
                readable = select.select([self._fd], [], [], remaining)[0]
            except InterruptedError:
                continue
            except OSError:
                self._usable = False
                return False
            if not readable:
                return False
            try:
                data = os.read(self._fd, 65536)
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    return False
                continue
            except OSError:
                self._usable = False
                return False
            if not data:
                self._usable = False
                return False
            relevant, broken = _relevant_events(data)
            if broken:
                # Overflow or an invalidated watch means hints were lost.
                # Revalidate instead of trusting an empty queue.
                self._usable = False
                return True
            if relevant:
                return True
            if time.monotonic() >= deadline:
                return False

    def close(self):
        fd, self._fd = self._fd, None
        self._usable = False
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()
        return False


def open_notifier(directory: int) -> SlotNotifier:
    """Create a watch for an already-open private directory descriptor."""
    try:
        return SlotNotifier(directory)
    except NotifierUnavailable:
        raise
    except (OSError, ValueError) as error:
        raise NotifierUnavailable(str(error)) from error
