"""Kernel-notified FIFO slot waits with a bounded fallback."""
import os
import shutil
import signal
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from codex_deepseek_team import slot_notifications, worker_slots as slots


CTX = None


def _hold(state, ready, release):
    try:
        fd = slots.acquire(Path(state), limit=1, wait=False)
    except BaseException:
        ready.set()
        raise
    ready.set()
    release.wait(20)
    os.close(fd)


class WaitNotificationTests(unittest.TestCase):
    def setUp(self):
        global CTX
        import multiprocessing
        CTX = multiprocessing.get_context('fork')
        self.tmp = Path(tempfile.mkdtemp(prefix='dst-wait-'))
        os.chmod(self.tmp, 0o700)
        self.state = self.tmp / 'state'
        self.state.mkdir(mode=0o700)
        self._fds = set()
        self.addCleanup(self._close_all)

    def _close_all(self):
        for fd in list(self._fds):
            try:
                os.close(fd)
            except OSError:
                pass
            self._fds.discard(fd)
        shutil.rmtree(self.tmp, True)

    def track(self, fd):
        self._fds.add(fd)
        return fd

    def release(self, fd):
        self._fds.discard(fd)
        os.close(fd)

    def hold(self):
        return self.track(slots.acquire(self.state, limit=1, wait=False))

    def holder_process(self):
        ready, release = CTX.Event(), CTX.Event()
        process = CTX.Process(target=_hold, args=(str(self.state), ready, release))
        process.start()
        self.addCleanup(self._reap, process)
        self.assertTrue(ready.wait(10), 'holder never acquired its slot')
        return process, release

    def _reap(self, process):
        if process.is_alive():
            process.kill()
        process.join(10)

    def start_waiter(self, *, timeout=5, validate=None):
        result, waiting, acquired = {}, threading.Event(), threading.Event()

        def run():
            try:
                result['fd'] = slots.acquire(
                    self.state, limit=1, timeout=timeout,
                    on_wait=waiting.set, validate=validate)
            except BaseException as exc:  # noqa: BLE001 - asserted by the caller
                result['error'] = exc
            finally:
                acquired.set()

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        self.assertTrue(waiting.wait(10), 'waiter never reported waiting')
        return thread, result, acquired

    def test_direct_notifier_wakes_on_release_before_fallback(self):
        holder = self.hold()
        directory = os.open(self.state, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            notifier = slot_notifications.open_notifier(directory)
            try:
                timer = threading.Timer(0.05, self.release, args=(holder,))
                timer.start()
                started = time.monotonic()
                self.assertTrue(notifier.wait(2))
                elapsed = time.monotonic() - started
            finally:
                notifier.close()
        finally:
            os.close(directory)
        self.assertLess(elapsed, 0.9, 'release hint did not wake the notifier')

    def test_release_hint_is_retained_before_wait_starts(self):
        holder = self.hold()
        directory = os.open(self.state, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            notifier = slot_notifications.open_notifier(directory)
            try:
                # The watch exists before the probe, so an event raised before
                # the caller reaches wait() must remain queued for it.
                self.release(holder)
                started = time.monotonic()
                self.assertTrue(notifier.wait(1.0))
                self.assertLess(time.monotonic() - started, 0.9)
            finally:
                notifier.close()
        finally:
            os.close(directory)

    def test_waiter_owned_close_events_do_not_wake_or_spin(self):
        self.hold()
        directory = os.open(self.state, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            notifier = slot_notifications.open_notifier(directory)
            try:
                for name in ('allocator.lock', 'ticket-00000000000000000001'):
                    fd = os.open(name, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC,
                                 0o600, dir_fd=directory)
                    os.close(fd)
                probe = os.open('worker-0.lock', os.O_RDONLY | os.O_CLOEXEC,
                                dir_fd=directory)
                os.close(probe)
                started = time.monotonic()
                self.assertFalse(notifier.wait(0.2))
                self.assertGreaterEqual(time.monotonic() - started, 0.15)
            finally:
                notifier.close()
        finally:
            os.close(directory)

    def test_ticket_unlinked_between_open_and_validation_is_ignored(self):
        self.hold()
        name = 'ticket-00000000000000000001'
        path = self.state / name
        path.touch(mode=0o600)
        directory = os.open(self.state, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        real_fstat = os.fstat

        def remove_then_stat(fd):
            path.unlink(missing_ok=True)
            return real_fstat(fd)

        try:
            with mock.patch.object(slots.os, 'fstat', side_effect=remove_then_stat):
                slots._reclaim_stale_tickets(directory)
        finally:
            os.close(directory)
        self.assertFalse(path.exists())

    def test_waiter_is_woken_by_slot_release(self):
        holder = self.hold()
        thread, result, acquired = self.start_waiter()
        started = time.monotonic()
        self.release(holder)
        self.assertTrue(acquired.wait(5), 'release never woke the waiter')
        elapsed = time.monotonic() - started
        thread.join(5)
        self.assertNotIn('error', result)
        self.release(result['fd'])
        self.assertLess(elapsed, 1.5)

    def test_waiter_is_woken_by_abrupt_worker_death(self):
        process, release = self.holder_process()
        thread, result, acquired = self.start_waiter()
        started = time.monotonic()
        os.kill(process.pid, signal.SIGKILL)
        process.join(10)
        self.assertTrue(acquired.wait(5), 'worker death never woke the waiter')
        elapsed = time.monotonic() - started
        thread.join(5)
        self.assertNotIn('error', result)
        self.release(result['fd'])
        self.assertLess(elapsed, 2.5)

    def test_waiters_keep_fifo_order_after_notified_release(self):
        holder = self.hold()
        first_thread, first_result, first_acquired = self.start_waiter()
        second_thread, second_result, second_acquired = self.start_waiter()
        self.release(holder)
        self.assertTrue(first_acquired.wait(5), 'first waiter was not admitted')
        self.assertNotIn('error', first_result)
        self.assertFalse(second_acquired.is_set(), 'second waiter jumped the FIFO queue')
        self.release(first_result['fd'])
        self.assertTrue(second_acquired.wait(5), 'second waiter was not admitted after release')
        self.assertNotIn('error', second_result)
        self.release(second_result['fd'])
        first_thread.join(5)
        second_thread.join(5)

    def test_repeated_fifo_release_cycles_do_not_lose_hints(self):
        for cycle in range(5):
            with self.subTest(cycle=cycle):
                holder = self.hold()
                first_thread, first_result, first_acquired = self.start_waiter()
                second_thread, second_result, second_acquired = self.start_waiter()
                self.release(holder)
                self.assertTrue(first_acquired.wait(5), 'first waiter was not admitted')
                self.assertFalse(second_acquired.is_set(),
                                 'second waiter jumped the FIFO queue')
                self.release(first_result['fd'])
                self.assertTrue(second_acquired.wait(5),
                                'second waiter missed the next release hint')
                self.assertNotIn('error', first_result)
                self.assertNotIn('error', second_result)
                self.release(second_result['fd'])
                first_thread.join(5)
                second_thread.join(5)

    def test_stable_wait_revalidates_only_at_a_bounded_rate(self):
        self.hold()
        calls = []
        started = time.monotonic()
        with self.assertRaises(slots.SlotError) as caught:
            slots.acquire(self.state, limit=1, timeout=0.45,
                          validate=lambda: calls.append(1))
        elapsed = time.monotonic() - started
        self.assertEqual(caught.exception.code, 75)
        self.assertGreaterEqual(elapsed, 0.35)
        self.assertLess(elapsed, 2.0)
        self.assertLessEqual(len(calls), 8,
                             f'stable wait performed {len(calls)} validations')

    def test_finite_deadline_is_honored_and_ticket_is_removed(self):
        self.hold()
        started = time.monotonic()
        with self.assertRaises(slots.SlotError) as caught:
            slots.acquire(self.state, limit=1, timeout=0.2)
        elapsed = time.monotonic() - started
        self.assertEqual(caught.exception.code, 75)
        self.assertGreaterEqual(elapsed, 0.15)
        self.assertLess(elapsed, 1.5)
        self.assertEqual(list(self.state.glob('ticket-*')), [])

    def test_forced_fallback_still_wakes_and_obeys_the_same_contract(self):
        holder = self.hold()
        calls = []
        with (
            mock.patch.object(slot_notifications, 'open_notifier',
                              side_effect=slot_notifications.NotifierUnavailable('unavailable')),
            mock.patch.object(slots, '_REVALIDATE_INTERVAL', 0.05),
        ):
            thread, result, acquired = self.start_waiter(
                timeout=5, validate=lambda: calls.append(1))
            time.sleep(0.08)
            self.release(holder)
            self.assertTrue(acquired.wait(3), 'fallback waiter never acquired the slot')
        thread.join(5)
        self.assertNotIn('error', result)
        self.release(result['fd'])
        self.assertGreaterEqual(len(calls), 2)
        self.assertLessEqual(len(calls), 200)

    def test_wait_paths_close_notification_lock_and_ticket_descriptors(self):
        self.hold()
        baseline = len(os.listdir('/proc/self/fd'))
        for _ in range(2):
            with self.assertRaises(slots.SlotError):
                slots.acquire(self.state, limit=1, timeout=0.05)
            self.assertEqual(len(os.listdir('/proc/self/fd')), baseline)

        def broken_validate():
            raise RuntimeError('cancel')

        with self.assertRaises(RuntimeError):
            slots.acquire(self.state, limit=1, timeout=0.05, validate=broken_validate)
        self.assertEqual(len(os.listdir('/proc/self/fd')), baseline)
        self.assertEqual(list(self.state.glob('ticket-*')), [])

        directory = os.open(self.state, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            notifier = slot_notifications.open_notifier(directory)
            self.assertEqual(len(os.listdir('/proc/self/fd')), baseline + 2)
            notifier.close()
            self.assertEqual(len(os.listdir('/proc/self/fd')), baseline + 1)
        finally:
            os.close(directory)
        self.assertEqual(len(os.listdir('/proc/self/fd')), baseline)


if __name__ == '__main__':
    unittest.main()
