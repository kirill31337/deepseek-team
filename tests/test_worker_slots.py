"""Behavioral tests for the shared FIFO worker slot allocator."""
import fcntl
import multiprocessing
import os
import shutil
import stat
import tempfile
import time
import unittest
from pathlib import Path

from codex_deepseek_team import worker_slots as slots


CTX = multiprocessing.get_context('fork')


def _hold_allocator(state, ready, release):
    fd = os.open(Path(state) / 'allocator.lock', os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    ready.set()
    release.wait(10)
    os.close(fd)


def _waiter(state, tag, *, limit=8, timeout=0, wait=True, order=None, waited=None,
            acquired=None, release=None, validate_raises=0):
    calls = {'count': 0}
    validate = None
    if validate_raises:
        def validate():
            calls['count'] += 1
            if calls['count'] >= validate_raises:
                raise RuntimeError('validate-stop')
    on_wait = waited.set if waited is not None else None
    try:
        fd = slots.acquire(Path(state), limit=limit, timeout=timeout, wait=wait,
                           on_wait=on_wait, validate=validate)
    except slots.SlotError as exc:
        if order is not None:
            order.put(('slot-error', tag, exc.code))
        return
    except BaseException as exc:  # noqa: BLE001 - surfaced to the test parent
        if order is not None:
            order.put(('raised', tag, type(exc).__name__))
        return
    if order is not None:
        order.put(('acquired', tag))
    if acquired is not None:
        acquired.set()
    try:
        if release is not None:
            release.wait(20)
    finally:
        os.close(fd)


def _holder(state, limit, ready, release, result):
    """Hold one slot in its own process so forked waiters never inherit it."""
    try:
        fd = slots.acquire(Path(state), limit=limit, wait=True)
    except slots.SlotError as exc:
        result.put(('slot-error', exc.code))
        ready.set()
        return
    result.put(('held', os.getpid()))
    ready.set()
    release.wait(30)
    os.close(fd)


class SlotTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        os.chmod(self.tmp, 0o700)
        self.state = self.tmp / 'state'
        self.state.mkdir(mode=0o700)
        self._fds = set()
        self.addCleanup(self._close_all)

    def _close_all(self):
        while self._fds:
            try:
                os.close(self._fds.pop())
            except OSError:
                pass
        shutil.rmtree(self.tmp, True)

    def track(self, fd):
        self._fds.add(fd)
        return fd

    def release(self, fd):
        self._fds.discard(fd)
        try:
            os.close(fd)
        except OSError:
            pass

    def acquire(self, *args, **kwargs):
        return self.track(slots.acquire(*args, **kwargs))

    def hold(self, *, limit=1, wait=False):
        return self.acquire(self.state, limit=limit, wait=wait)

    def process(self, target, *args, **kwargs):
        process = CTX.Process(target=target, args=args, kwargs=kwargs)
        process.start()
        self.addCleanup(self._reap, process)
        return process

    def wait_for_holder(self):
        ready, release, result = CTX.Event(), CTX.Event(), CTX.Queue()
        self.process(_holder, str(self.state), 1, ready, release, result)
        self.assertTrue(ready.wait(10), 'slot holder never started')
        outcome = result.get(timeout=10)
        self.assertEqual(outcome[0], 'held', outcome)
        return release

    def _reap(self, process):
        if process.is_alive():
            process.terminate()
        process.join(10)


class ContractTests(SlotTestBase):
    def test_allocator_contention_observes_timeout_and_nonwaiting_mode(self):
        ready, release = CTX.Event(), CTX.Event()
        self.process(_hold_allocator, str(self.state), ready, release)
        self.assertTrue(ready.wait(2))
        try:
            for wait in (True, False):
                with self.subTest(wait=wait):
                    results = CTX.Queue()
                    self.process(_waiter, str(self.state), 'contended',
                                 timeout=.2, wait=wait, order=results)
                    self.assertEqual(results.get(timeout=1), ('slot-error', 'contended', 75))
        finally:
            release.set()

    def test_huge_timeout_is_a_clean_validation_error(self):
        with self.assertRaises(slots.SlotError) as caught:
            slots.acquire(self.state, timeout=10**400)
        self.assertEqual(caught.exception.code, 64)

    def test_public_constants(self):
        self.assertEqual(slots.DEFAULT_MAX_WORKERS, 8)
        self.assertEqual(slots.MAX_MAX_WORKERS, 64)

    def test_slot_error_exposes_code_and_message(self):
        error = slots.SlotError(75, 'busy')
        self.assertEqual((error.code, error.message), (75, 'busy'))
        self.assertIsInstance(error, Exception)

    def test_default_limit_is_eight(self):
        for _ in range(8):
            self.acquire(self.state)
        with self.assertRaises(slots.SlotError) as caught:
            slots.acquire(self.state, wait=False)
        self.assertEqual(caught.exception.code, 75)

    def test_capacity_is_configurable(self):
        self.acquire(self.state, limit=2)
        self.acquire(self.state, limit=2)
        with self.assertRaises(slots.SlotError) as caught:
            slots.acquire(self.state, limit=2, wait=False)
        self.assertEqual(caught.exception.code, 75)

    def test_returned_fd_is_flocked_and_released_on_close(self):
        fd = slots.acquire(self.state, limit=1)
        probe = self.track(os.open(self.state / 'worker-0.lock', os.O_RDWR))
        with self.assertRaises(BlockingIOError):
            fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.close(fd)
        fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_state_directory_created_privately(self):
        fresh = self.tmp / 'fresh'
        self.release(self.acquire(fresh, limit=1))
        info = os.stat(fresh)
        self.assertTrue(stat.S_ISDIR(info.st_mode))
        self.assertEqual(info.st_mode & 0o777, 0o700)

    def test_slot_files_are_neither_truncated_nor_removed(self):
        existing = self.state / 'worker-0.lock'
        existing.write_bytes(b'keep-payload')
        os.chmod(existing, 0o600)
        fd = self.hold(limit=8)
        self.assertEqual(os.fstat(fd).st_ino, existing.stat().st_ino)
        self.assertEqual(existing.read_bytes(), b'keep-payload')
        self.release(fd)
        self.assertTrue(existing.exists())
        self.assertEqual(existing.read_bytes(), b'keep-payload')


class WaitingTests(SlotTestBase):
    def test_waiter_blocks_until_slot_released(self):
        release_holder = self.wait_for_holder()
        order = CTX.Queue()
        waited, acquired, release = CTX.Event(), CTX.Event(), CTX.Event()
        process = self.process(_waiter, str(self.state), 'waiter', limit=1,
                               timeout=0, order=order, waited=waited,
                               acquired=acquired, release=release)
        self.assertTrue(waited.wait(10), 'waiter never reported waiting')
        self.assertFalse(acquired.is_set())
        release_holder.set()
        self.assertTrue(acquired.wait(10), 'waiter never acquired the freed slot')
        self.assertEqual(order.get(timeout=10), ('acquired', 'waiter'))
        release.set()
        process.join(10)
        self.assertEqual(process.exitcode, 0)

    def test_finite_timeout_reports_75_and_cleans_up(self):
        self.hold(limit=1)
        started = time.monotonic()
        with self.assertRaises(slots.SlotError) as caught:
            slots.acquire(self.state, limit=1, timeout=0.3)
        elapsed = time.monotonic() - started
        self.assertEqual(caught.exception.code, 75)
        self.assertGreaterEqual(elapsed, 0.2)
        self.assertLess(elapsed, 5.0)
        self.assertEqual(list(self.state.glob('ticket-*')), [])

    def test_finite_timeout_succeeds_when_a_slot_frees(self):
        release_holder = self.wait_for_holder()
        order = CTX.Queue()
        waited, acquired, release = CTX.Event(), CTX.Event(), CTX.Event()
        process = self.process(_waiter, str(self.state), 'timed', limit=1,
                               timeout=5, order=order, waited=waited,
                               acquired=acquired, release=release)
        self.assertTrue(waited.wait(10))
        release_holder.set()
        self.assertTrue(acquired.wait(10))
        self.assertEqual(order.get(timeout=10), ('acquired', 'timed'))
        release.set()
        process.join(10)

    def test_full_nonwaiting_acquisition_reports_75(self):
        self.hold(limit=1)
        with self.assertRaises(slots.SlotError) as caught:
            slots.acquire(self.state, limit=1, wait=False)
        self.assertEqual(caught.exception.code, 75)

    def test_waiters_are_admitted_in_fifo_order(self):
        release_holder = self.wait_for_holder()
        order = CTX.Queue()
        waited1, waited2 = CTX.Event(), CTX.Event()
        acquired1, acquired2 = CTX.Event(), CTX.Event()
        release = CTX.Event()
        first = self.process(_waiter, str(self.state), 'first', limit=1,
                             order=order, waited=waited1, acquired=acquired1,
                             release=release)
        self.assertTrue(waited1.wait(10))
        second = self.process(_waiter, str(self.state), 'second', limit=1,
                              order=order, waited=waited2, acquired=acquired2,
                              release=release)
        self.assertTrue(waited2.wait(10))
        release_holder.set()
        self.assertTrue(acquired1.wait(10))
        self.assertEqual(order.get(timeout=10), ('acquired', 'first'))
        self.assertFalse(acquired2.is_set())
        release.set()
        self.assertTrue(acquired2.wait(10))
        self.assertEqual(order.get(timeout=10), ('acquired', 'second'))
        first.join(10)
        second.join(10)
        self.assertEqual((first.exitcode, second.exitcode), (0, 0))

    def test_dead_waiter_ticket_is_reclaimed(self):
        release_holder = self.wait_for_holder()
        waited = CTX.Event()
        process = self.process(_waiter, str(self.state), 'doomed', limit=1,
                               waited=waited)
        self.assertTrue(waited.wait(10))
        process.terminate()
        process.join(10)
        self.assertTrue(list(self.state.glob('ticket-*')),
                        'waiter ticket should remain after abrupt death')
        release_holder.set()
        self.release(self.acquire(self.state, limit=1, timeout=10))
        self.assertEqual(list(self.state.glob('ticket-*')), [])

    def test_on_wait_fires_once_only_when_waiting_is_required(self):
        immediate = []
        self.release(self.acquire(self.state, limit=1,
                                   on_wait=lambda: immediate.append(1)))
        self.assertEqual(immediate, [])
        self.hold(limit=1)
        calls = []
        with self.assertRaises(slots.SlotError):
            slots.acquire(self.state, limit=1, timeout=0.3,
                          on_wait=lambda: calls.append(1))
        self.assertEqual(calls, [1])


class CallbackTests(SlotTestBase):
    def test_validate_runs_immediately_before_allocation(self):
        calls = []
        self.release(self.acquire(self.state, limit=1,
                                   validate=lambda: calls.append(1)))
        self.assertGreaterEqual(len(calls), 1)
        self.assertEqual(len(list(self.state.glob('worker-*.lock'))), 1)

    def test_validate_failure_cleans_the_wait_state(self):
        calls = []

        def validate():
            calls.append(1)
            if len(calls) >= 2:
                raise RuntimeError('stop-before-allocation')

        with self.assertRaises(RuntimeError):
            slots.acquire(self.state, limit=1, validate=validate)
        self.assertGreaterEqual(len(calls), 2)
        self.assertEqual(list(self.state.glob('ticket-*')), [])
        self.assertEqual(list(self.state.glob('worker-*.lock')), [])

    def test_keyboard_interrupt_from_on_wait_propagates_and_cleans_up(self):
        self.hold(limit=1)

        def on_wait():
            raise KeyboardInterrupt()

        with self.assertRaises(KeyboardInterrupt):
            slots.acquire(self.state, limit=1, on_wait=on_wait)
        self.assertEqual(list(self.state.glob('ticket-*')), [])

    def test_invalid_callbacks_are_rejected(self):
        for kwargs in ({'on_wait': 5}, {'validate': 'nope'}):
            with self.subTest(kwargs=kwargs), self.assertRaises(slots.SlotError) as caught:
                slots.acquire(self.state, **kwargs)
            self.assertEqual(caught.exception.code, 64)


class ArgumentTests(SlotTestBase):
    def test_invalid_limits_are_rejected(self):
        for bad in (True, False, 0, -3, 65, 1.5, '8', None):
            with self.subTest(limit=bad), self.assertRaises(slots.SlotError) as caught:
                slots.acquire(self.state, limit=bad)
            self.assertEqual(caught.exception.code, 64)

    def test_invalid_timeouts_are_rejected(self):
        for bad in (True, -1, -0.5, float('nan'), float('inf'), 'x', None):
            with self.subTest(timeout=bad), self.assertRaises(slots.SlotError) as caught:
                slots.acquire(self.state, timeout=bad)
            self.assertEqual(caught.exception.code, 64)

    def test_non_boolean_wait_is_rejected(self):
        with self.assertRaises(slots.SlotError) as caught:
            slots.acquire(self.state, wait=1)
        self.assertEqual(caught.exception.code, 64)


class UnsafeStateTests(SlotTestBase):
    def test_public_state_directory_is_rejected_without_chmod(self):
        public = self.tmp / 'public'
        public.mkdir()
        os.chmod(public, 0o755)
        with self.assertRaises(slots.SlotError) as caught:
            slots.acquire(public, limit=1)
        self.assertEqual(caught.exception.code, 78)
        self.assertEqual(os.stat(public).st_mode & 0o777, 0o755)

    def test_symlinked_state_directory_is_rejected(self):
        target = self.tmp / 'real'
        target.mkdir(mode=0o700)
        link = self.tmp / 'link'
        link.symlink_to(target, target_is_directory=True)
        with self.assertRaises(slots.SlotError) as caught:
            slots.acquire(link, limit=1)
        self.assertEqual(caught.exception.code, 78)
        self.assertEqual(list(target.iterdir()), [])

    def test_public_slot_file_is_rejected_without_chmod(self):
        lock = self.state / 'worker-0.lock'
        lock.write_text('')
        os.chmod(lock, 0o644)
        with self.assertRaises(slots.SlotError) as caught:
            slots.acquire(self.state, limit=1)
        self.assertEqual(caught.exception.code, 78)
        self.assertEqual(os.stat(lock).st_mode & 0o777, 0o644)

    def test_fifo_slot_file_is_rejected(self):
        os.mkfifo(self.state / 'worker-0.lock', 0o600)
        with self.assertRaises(slots.SlotError) as caught:
            slots.acquire(self.state, limit=1)
        self.assertEqual(caught.exception.code, 78)

    def test_hardlinked_slot_file_is_rejected(self):
        target = self.tmp / 'payload'
        target.write_text('preserve-me')
        os.chmod(target, 0o600)
        os.link(target, self.state / 'worker-0.lock')
        with self.assertRaises(slots.SlotError) as caught:
            slots.acquire(self.state, limit=1)
        self.assertEqual(caught.exception.code, 78)
        self.assertEqual(target.read_text(), 'preserve-me')

    def test_symlinked_slot_file_is_rejected(self):
        outside = self.tmp / 'outside'
        outside.write_text('preserve-me')
        os.chmod(outside, 0o600)
        os.symlink(outside, self.state / 'worker-0.lock')
        with self.assertRaises(slots.SlotError) as caught:
            slots.acquire(self.state, limit=1)
        self.assertEqual(caught.exception.code, 78)
        self.assertEqual(outside.read_text(), 'preserve-me')


class MixedLimitTests(SlotTestBase):
    def occupy(self, number, mode=0o600):
        fd = os.open(self.state / f'worker-{number}.lock',
                     os.O_CREAT | os.O_RDWR, mode)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return self.track(fd)

    def test_existing_high_numbered_slot_counts_when_limit_is_lowered(self):
        self.occupy(5)
        with self.assertRaises(slots.SlotError) as caught:
            slots.acquire(self.state, limit=1, wait=False)
        self.assertEqual(caught.exception.code, 75)
        fd = self.acquire(self.state, limit=2, wait=False)
        self.assertEqual(os.fstat(fd).st_ino,
                         os.stat(self.state / 'worker-0.lock').st_ino)

    def test_lowest_free_slot_is_reused_before_creating_new_files(self):
        self.occupy(0)
        self.occupy(1)
        fd = self.acquire(self.state, limit=8, wait=False)
        self.assertEqual(os.fstat(fd).st_ino,
                         os.stat(self.state / 'worker-2.lock').st_ino)
        fd2 = self.acquire(self.state, limit=8, wait=False)
        self.assertEqual(os.fstat(fd2).st_ino,
                         os.stat(self.state / 'worker-3.lock').st_ino)


if __name__ == '__main__':
    unittest.main()
