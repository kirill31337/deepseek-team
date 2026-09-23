"""Shared FIFO worker-slot allocator for the DeepSeek team runtime.

The allocator hands out exclusively file-locked ``worker-<n>.lock`` slots from a
private per-state directory.  :func:`acquire` returns an open, flock-held file
descriptor that the caller releases with :func:`os.close`; the OS drops the lock
when the descriptor closes (or the process dies), so a crashed worker can never
strand a slot.

Fairness is provided by a per-directory queue of ticket files.  Every waiting
caller owns a ticket flock for the whole time it waits; a ticket whose lock can
be taken by somebody else belongs to a dead process and is reclaimed without
trusting process ids.  Admission, ticket creation and slot probing all happen
under a short-lived private allocator flock, while polling happens outside it.

Error codes reported through :class:`SlotError`:

* ``64`` - invalid arguments (limit, timeout, wait flag or callbacks).
* ``75`` - no slot is available and the caller would not wait, or waiting timed out.
* ``78`` - the state directory or a runtime file is unsafe (symlink, foreign
  owner, group/other permission bits, non-regular or multiply-linked file).
"""
from __future__ import annotations

import fcntl
import math
import os
from pathlib import Path
import re
import stat
import time
from typing import Callable, Optional


DEFAULT_MAX_WORKERS = 8
MAX_MAX_WORKERS = 64

ALLOCATOR_LOCK = 'allocator.lock'
_POLL_INTERVAL = 0.05
_SLOT_NAME = 'worker-{number}.lock'
_TICKET_RE = re.compile(r'^ticket-(\d+)$')
_UNSAFE = ('Worker state requires a private user-owned directory (700) and '
           'regular lock files (600), without symlinks or hard links.')
_BAD_LIMIT = f'limit must be an integer between 1 and {MAX_MAX_WORKERS}.'
_BAD_TIMEOUT = ('timeout must be a non-negative finite number of seconds '
                '(0 means wait without a deadline).')


class SlotError(Exception):
    """Raised for invalid arguments (64), busy/timeout (75) or unsafe state (78)."""

    def __init__(self, code: int, message: str):
        self.code, self.message = code, message
        super().__init__(message)


def acquire(state: Path, *, limit: int = DEFAULT_MAX_WORKERS, timeout: float = 0,
            wait: bool = True, on_wait: Optional[Callable[[], object]] = None,
            validate: Optional[Callable[[], object]] = None) -> int:
    """Acquire a flock-held worker slot and return its file descriptor.

    ``limit`` caps how many of the 64 slots may run concurrently for this caller.
    ``timeout`` of ``0`` waits indefinitely, a positive value bounds the total
    time spent queueing.  ``on_wait`` is invoked at most once, and only when the
    caller cannot be admitted immediately.  ``validate`` is invoked while waiting
    and once more immediately before the slot is allocated; any exception it (or
    ``on_wait``) raises propagates after the wait state is cleaned up.
    """
    limit = _check_limit(limit)
    timeout = _check_timeout(timeout)
    if not isinstance(wait, bool):
        raise SlotError(64, 'wait must be a boolean.')
    if on_wait is not None and not callable(on_wait):
        raise SlotError(64, 'on_wait must be callable or None.')
    if validate is not None and not callable(validate):
        raise SlotError(64, 'validate must be callable or None.')
    state = Path(state)

    directory = _open_state(state)
    ticket_fd = ticket_name = None
    on_wait_fired = False
    deadline = None if timeout == 0 else time.monotonic() + timeout
    try:
        sequence = None
        while True:
            if deadline is not None and time.monotonic() >= deadline:
                raise SlotError(75, f'Timed out after {timeout:g}s waiting for a worker slot.')
            _call(validate)
            try:
                if wait and ticket_fd is None:
                    ticket_fd, ticket_name, sequence = _enqueue(directory)
                slot = _attempt(directory, limit, sequence, validate)
            except BlockingIOError:
                # Contention on the allocator is queue wait too: it must obey
                # the same cancellation/deadline/no-wait policy as busy slots.
                slot = None
            if slot is not None:
                return slot
            if not wait:
                raise SlotError(75, f'All {limit} worker slots are busy and waiting is disabled.')
            if on_wait is not None and not on_wait_fired:
                on_wait()
            on_wait_fired = True
            if deadline is None:
                delay = _POLL_INTERVAL
            else:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise SlotError(
                        75, f'Timed out after {timeout:g}s waiting for a worker slot.')
                delay = min(_POLL_INTERVAL, remaining)
            if delay > 0:
                time.sleep(delay)
    finally:
        if ticket_fd is not None:
            try:
                os.unlink(ticket_name, dir_fd=directory)
            except OSError:
                pass
            os.close(ticket_fd)
        os.close(directory)


def _check_limit(limit: object) -> int:
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise SlotError(64, _BAD_LIMIT)
    if not 1 <= limit <= MAX_MAX_WORKERS:
        raise SlotError(64, _BAD_LIMIT)
    return limit


def _check_timeout(timeout: object) -> float:
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        raise SlotError(64, _BAD_TIMEOUT)
    try:
        value = float(timeout)
    except OverflowError:
        raise SlotError(64, _BAD_TIMEOUT) from None
    if not math.isfinite(value) or value < 0:
        raise SlotError(64, _BAD_TIMEOUT)
    return value


def _call(callback: Optional[Callable[[], object]]) -> None:
    if callback is not None:
        callback()


def _open_state(state: Path) -> int:
    try:
        state.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError:
        raise SlotError(78, _UNSAFE) from None
    try:
        directory = os.open(
            state, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        raise SlotError(78, _UNSAFE) from None
    try:
        info = os.fstat(directory)
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_mode & 0o077):
            raise SlotError(78, _UNSAFE)
    except BaseException:
        os.close(directory)
        raise
    return directory


def _open_file(directory: int, name: str, *, create: bool) -> int:
    flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
    if create:
        flags |= os.O_CREAT
    try:
        fd = os.open(name, flags, 0o600, dir_fd=directory)
    except FileNotFoundError:
        if create:
            raise SlotError(78, _UNSAFE) from None
        raise
    except OSError:
        raise SlotError(78, _UNSAFE) from None
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or info.st_uid != os.geteuid() or info.st_mode & 0o077):
            raise SlotError(78, _UNSAFE)
    except BaseException:
        os.close(fd)
        raise
    return fd


def _allocator(directory: int) -> int:
    fd = _open_file(directory, ALLOCATOR_LOCK, create=True)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        raise
    except BaseException:
        os.close(fd)
        raise
    return fd


def _tickets(directory: int) -> list[tuple[int, str]]:
    found = []
    for name in os.listdir(directory):
        match = _TICKET_RE.match(name)
        if match:
            found.append((int(match.group(1)), name))
    found.sort()
    return found


def _reclaim_stale_tickets(directory: int) -> None:
    """Drop tickets whose owner died (their ticket flock is now free)."""
    for _, name in _tickets(directory):
        try:
            fd = _open_file(directory, name, create=False)
        except FileNotFoundError:
            continue
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                continue
            try:
                os.unlink(name, dir_fd=directory)
            except FileNotFoundError:
                pass
        finally:
            os.close(fd)


def _pending_before(directory: int, sequence: Optional[int]) -> int:
    return sum(1 for number, _ in _tickets(directory)
               if sequence is None or number < sequence)


def _enqueue(directory: int) -> tuple[int, str, int]:
    allocator = _allocator(directory)
    try:
        _reclaim_stale_tickets(directory)
        highest = max((number for number, _ in _tickets(directory)), default=0)
        sequence = max(highest + 1, time.monotonic_ns())
        name = f'ticket-{sequence:020d}'
        fd = _open_file(directory, name, create=True)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            raise SlotError(78, _UNSAFE) from None
        return fd, name, sequence
    finally:
        os.close(allocator)


def _probe_slots(directory: int) -> tuple[int, list[int]]:
    """Count occupied slots and list the free ones across all 64 names."""
    names = set(os.listdir(directory))
    occupied = 0
    free = []
    for number in range(MAX_MAX_WORKERS):
        name = _SLOT_NAME.format(number=number)
        if name not in names:
            free.append(number)
            continue
        try:
            fd = _open_file(directory, name, create=False)
        except FileNotFoundError:
            free.append(number)
            continue
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                occupied += 1
                continue
        finally:
            os.close(fd)
        free.append(number)
    return occupied, free


def _allocate_slot(directory: int, free: list[int]) -> int:
    for number in free:
        fd = _open_file(directory, _SLOT_NAME.format(number=number), create=True)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            continue
        except OSError:
            os.close(fd)
            raise SlotError(78, _UNSAFE) from None
        return fd
    raise SlotError(75, 'No worker slot could be allocated.')


def _attempt(directory: int, limit: int, sequence: Optional[int],
             validate: Optional[Callable[[], object]]) -> Optional[int]:
    """Admit at most one caller while holding the private allocator lock."""
    allocator = _allocator(directory)
    try:
        _reclaim_stale_tickets(directory)
        occupied, free = _probe_slots(directory)
        if occupied >= limit:
            return None
        if _pending_before(directory, sequence):
            return None
        _call(validate)
        return _allocate_slot(directory, free)
    finally:
        os.close(allocator)
