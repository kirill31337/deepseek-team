"""Behavioral tests for private storage measurement and reservation accounting."""
import errno
import fcntl
import json
import multiprocessing
import os
from pathlib import Path
import re
import stat
import tempfile
import unittest
from unittest import mock

from codex_deepseek_team import storage_budget


CTX = multiprocessing.get_context('fork')


def _controlled_statvfs(free_bytes, free_inodes, block_size=4096):
    if free_bytes % block_size:
        raise ValueError('controlled free bytes must align to the block size')
    blocks = free_bytes // block_size
    return os.statvfs_result((
        block_size, block_size, blocks, blocks, blocks,
        free_inodes, free_inodes, free_inodes, 0, 255))


def _mountpoint(raw):
    return re.sub(r'\\([0-7]{3})', lambda match: chr(int(match.group(1), 8)), raw)


def _real_nested_same_device_mount(root):
    root = Path(root)
    device = root.stat().st_dev
    try:
        with open('/proc/self/mountinfo', encoding='utf-8', errors='replace') as stream:
            lines = stream.readlines()
    except OSError:
        return None
    for line in lines:
        fields = line.split()
        if len(fields) < 5:
            continue
        mountpoint = Path(_mountpoint(fields[4]))
        try:
            relative = mountpoint.relative_to(root)
        except ValueError:
            continue
        if not relative.parts:
            continue
        try:
            if mountpoint.stat().st_dev == device:
                return mountpoint
        except OSError:
            continue
    return None


def _hold_budget_lock(state, ready, release):
    fd = os.open(Path(state) / 'budget.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,
                 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        ready.set()
        release.wait(20)
    finally:
        os.close(fd)


def _reserve_once(state, started, acquired, result):
    started.set()
    try:
        with storage_budget.reserve(Path(state), disk_bytes=0, inodes=0,
                                    minimum_free_bytes=0, minimum_free_inodes=0):
            acquired.set()
        result.put(('ok',))
    except BaseException as error:  # noqa: BLE001 - surfaced to the test parent
        result.put(('error', type(error).__name__, str(error)))


def _reserve_then_exit(state, entered):
    with storage_budget.reserve(Path(state), disk_bytes=1, inodes=1,
                                minimum_free_bytes=0, minimum_free_inodes=0):
        entered.set()
        os._exit(0)


def _expected_measure(root):
    seen = set()
    allocated = 0
    inodes = 0

    def add(info):
        nonlocal allocated, inodes
        key = (info.st_dev, info.st_ino)
        if key in seen:
            return
        seen.add(key)
        inodes += 1
        allocated += info.st_blocks * 512

    def walk(directory):
        add(os.lstat(directory))
        for name in os.listdir(directory):
            path = directory / name
            info = os.lstat(path)
            add(info)
            if stat.S_ISDIR(info.st_mode):
                walk(path)

    walk(Path(root))
    return {'allocated_bytes': allocated, 'inodes': inodes}


class StorageBudgetTestBase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='dst-storage-budget-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.home = self.root / 'home'
        self.home.mkdir(mode=0o700)
        self.config = self.root / 'config'
        self.config.mkdir(mode=0o700)
        environment = mock.patch.dict(os.environ, {
            'HOME': str(self.home), 'XDG_CONFIG_HOME': str(self.config)})
        environment.start()
        self.addCleanup(environment.stop)
        self.state = self.root / 'state'
        self.state.mkdir(mode=0o700)

    def process(self, target, *args):
        process = CTX.Process(target=target, args=args)
        process.start()
        self.addCleanup(self._reap, process)
        return process

    @staticmethod
    def _reap(process):
        if process.is_alive():
            process.terminate()
        process.join(10)

    def records(self, state=None):
        return sorted((state or self.state).glob('budget-*.reservation'))

    def fd_count(self):
        try:
            return len(os.listdir('/proc/self/fd'))
        except OSError:
            self.skipTest('/proc/self/fd is unavailable')

    def other_filesystem_directory(self):
        source_device = self.root.stat().st_dev
        for base in (Path('/dev/shm'), Path('/run/shm'), Path.home()):
            try:
                if not base.is_dir():
                    continue
                temporary = tempfile.TemporaryDirectory(
                    prefix='dst-storage-budget-other-', dir=base)
            except OSError:
                continue
            path = Path(temporary.name)
            if path.stat().st_dev != source_device:
                path.chmod(0o700)
                self.addCleanup(temporary.cleanup)
                return path
            temporary.cleanup()
        self.skipTest('no second filesystem is available for cross-device coverage')


class MeasurementTests(StorageBudgetTestBase):
    def test_allocated_blocks_and_unique_inodes_ignore_symlink_targets(self):
        tree = self.root / 'tree'
        tree.mkdir(mode=0o700)
        nested = tree / 'nested'
        nested.mkdir(mode=0o700)
        payload = tree / 'payload'
        payload.write_bytes(b'x' * 16384)
        sparse = tree / 'sparse'
        with sparse.open('wb') as stream:
            stream.seek(1024 * 1024)
            stream.write(b'z')
        os.link(payload, tree / 'payload-hardlink')
        outside = self.root / 'outside'
        outside.write_bytes(b'y' * (2 * 1024 * 1024))
        (tree / 'outside-link').symlink_to(outside)
        (nested / 'small').write_bytes(b'q' * 512)

        expected = _expected_measure(tree)
        self.assertEqual(storage_budget.measure(tree), expected)
        self.assertGreater(expected['allocated_bytes'], 0)
        self.assertEqual(expected['inodes'], 6)
        self.assertNotIn(os.lstat(outside).st_ino, {
            os.lstat(path).st_ino for path in (tree, payload, nested, sparse,
                                                tree / 'payload-hardlink',
                                                tree / 'outside-link', nested / 'small')})

        single = storage_budget.measure(payload)
        self.assertEqual(single, {'allocated_bytes': os.lstat(payload).st_blocks * 512,
                                  'inodes': 1})

    def test_symlinked_ancestry_is_rejected_without_reading_the_target(self):
        target = self.root / 'target'
        target.mkdir(mode=0o700)
        (target / 'payload').write_bytes(b'preserve')
        alias = self.root / 'alias'
        alias.symlink_to(target, target_is_directory=True)

        with self.assertRaises(storage_budget.BudgetError) as caught:
            storage_budget.measure(alias / 'payload')
        self.assertEqual(caught.exception.code, 78)
        self.assertIn('symbolic link', str(caught.exception).lower())
        self.assertEqual((target / 'payload').read_bytes(), b'preserve')

    def test_nested_bind_mount_is_rejected_even_when_the_device_matches(self):
        tree = self.root / 'tree'
        tree.mkdir(mode=0o700)
        (tree / 'nested').mkdir(mode=0o700)
        mountinfo = f'1 2 0:1 / {tree}/nested rw - tmpfs tmpfs rw\n'
        with mock.patch('builtins.open', mock.mock_open(read_data=mountinfo)):
            with self.assertRaises(storage_budget.BudgetError) as caught:
                storage_budget.measure(tree)
        self.assertEqual(caught.exception.code, 78)
        self.assertIn('mount', str(caught.exception).lower())

    def test_unavailable_mount_identity_fails_closed(self):
        tree = self.root / 'tree'
        tree.mkdir(mode=0o700)
        with mock.patch('builtins.open',
                        side_effect=OSError(errno.ENOENT, 'mountinfo unavailable')):
            with self.assertRaises(storage_budget.BudgetError) as caught:
                storage_budget.measure(tree)
        self.assertEqual(caught.exception.code, 78)
        self.assertIn('mount', str(caught.exception).lower())

    def test_real_same_device_bind_mount_is_rejected_when_present(self):
        candidate = Path(__file__).resolve().parents[1]
        info = candidate.stat()
        if info.st_uid != os.geteuid():
            self.skipTest('workspace is not owned by the test user')
        nested = _real_nested_same_device_mount(candidate)
        if nested is None:
            self.skipTest('the real mount namespace has no same-device nested mount')
        with self.assertRaises(storage_budget.BudgetError) as caught:
            storage_budget.measure(candidate)
        self.assertEqual(caught.exception.code, 78)
        self.assertIn('mount', str(caught.exception).lower())

    def test_foreign_owner_is_rejected(self):
        tree = self.root / 'tree'
        tree.mkdir(mode=0o700)
        foreign = tree / 'foreign'
        foreign.write_bytes(b'x')
        fake_uid = os.geteuid() + 1
        with mock.patch.object(storage_budget.os, 'geteuid', return_value=fake_uid):
            with self.assertRaises(storage_budget.BudgetError) as caught:
                storage_budget.measure(tree)
        self.assertEqual(caught.exception.code, 78)
        self.assertIn('owner', str(caught.exception).lower())


class ArgumentTests(StorageBudgetTestBase):
    def test_budget_error_exposes_code_and_message(self):
        error = storage_budget.BudgetError('busy', code=75)
        self.assertEqual((error.message, error.code), ('busy', 75))
        self.assertIsInstance(error, Exception)

    def test_invalid_quantities_are_rejected_before_state_access(self):
        values = (True, -1, float('inf'), float('nan'), 1.5, '1', None, 1 << 100)
        for field in ('disk_bytes', 'inodes', 'minimum_free_bytes',
                      'minimum_free_inodes'):
            for value in values:
                arguments = {'disk_bytes': 0, 'inodes': 0,
                             'minimum_free_bytes': 0, 'minimum_free_inodes': 0}
                arguments[field] = value
                with self.subTest(field=field, value=value):
                    with self.assertRaises(storage_budget.BudgetError) as caught:
                        storage_budget.reserve(self.root / 'missing-state', **arguments)
                    self.assertEqual(caught.exception.code, 64)
        self.assertFalse((self.root / 'missing-state').exists())

    def test_public_and_symlinked_state_are_rejected_without_chmod(self):
        public = self.root / 'public'
        public.mkdir(mode=0o755)
        public.chmod(0o755)
        with self.assertRaises(storage_budget.BudgetError) as caught:
            with storage_budget.reserve(public, disk_bytes=0, inodes=0,
                                        minimum_free_bytes=0, minimum_free_inodes=0):
                pass
        self.assertEqual(caught.exception.code, 78)
        self.assertEqual(public.stat().st_mode & 0o777, 0o755)

        target = self.root / 'target-state'
        target.mkdir(mode=0o700)
        alias = self.root / 'state-alias'
        alias.symlink_to(target, target_is_directory=True)
        with self.assertRaises(storage_budget.BudgetError) as caught:
            with storage_budget.reserve(alias, disk_bytes=0, inodes=0,
                                        minimum_free_bytes=0, minimum_free_inodes=0):
                pass
        self.assertEqual(caught.exception.code, 78)
        self.assertEqual(list(target.iterdir()), [])


class ReservationTests(StorageBudgetTestBase):
    def test_reservation_lifetime_and_record_cleanup(self):
        self.assertEqual(self.records(), [])
        with storage_budget.reserve(self.state, disk_bytes=1, inodes=1,
                                    minimum_free_bytes=0,
                                    minimum_free_inodes=0) as reservation:
            self.assertIsInstance(str(reservation), str)
            self.assertEqual(reservation.disk_bytes, 1)
            self.assertEqual(reservation.inodes, 1)
            self.assertFalse(reservation.closed)
            self.assertEqual(len(self.records()), 1)
        self.assertTrue(reservation.closed)
        self.assertEqual(self.records(), [])

    def test_basic_and_shared_reservations_release_all_descriptors(self):
        shared = self.root / 'shared-pool'
        before = self.fd_count()
        for _ in range(20):
            with storage_budget.reserve(self.state, disk_bytes=1, inodes=1,
                                        minimum_free_bytes=0,
                                        minimum_free_inodes=0):
                pass
            with storage_budget.reserve(self.state, disk_bytes=1, inodes=1,
                                        minimum_free_bytes=0,
                                        minimum_free_inodes=0,
                                        allocation_path=self.state,
                                        pool_dir=shared):
                pass
        with mock.patch.object(storage_budget.os, 'fstatvfs',
                               side_effect=OSError(errno.EIO, 'capacity unavailable')):
            with self.assertRaises(storage_budget.BudgetError):
                with storage_budget.reserve(self.state, disk_bytes=1, inodes=1,
                                            minimum_free_bytes=0,
                                            minimum_free_inodes=0):
                    self.fail('failed admission unexpectedly entered')
        self.assertEqual(self.fd_count(), before)

    def test_failed_close_and_state_replacement_release_all_descriptors(self):
        before = self.fd_count()
        context = storage_budget.reserve(self.state, disk_bytes=1, inodes=1,
                                         minimum_free_bytes=0,
                                         minimum_free_inodes=0)
        reservation = context.__enter__()
        self.assertGreater(self.fd_count(), before)
        record = self.records()[0]
        outside = self.root / 'replacement-target'
        outside.write_bytes(b'preserve')
        record.unlink()
        record.symlink_to(outside)
        with self.assertRaises(storage_budget.BudgetError):
            context.__exit__(None, None, None)
        self.assertTrue(reservation.closed)
        self.assertEqual(self.fd_count(), before)
        reservation.close()
        reservation.close()
        self.assertEqual(self.fd_count(), before)
        record.unlink()

        context = storage_budget.reserve(self.state, disk_bytes=1, inodes=1,
                                         minimum_free_bytes=0,
                                         minimum_free_inodes=0)
        reservation = context.__enter__()
        record = self.records()[0]
        old_state = self.root / 'old-state'
        self.state.rename(old_state)
        self.state.mkdir(mode=0o700)
        context.__exit__(None, None, None)
        self.assertTrue(reservation.closed)
        self.assertFalse((old_state / record.name).exists())
        self.assertEqual(list(self.state.iterdir()), [])
        self.assertEqual(self.fd_count(), before)

    def test_lock_replacement_close_failure_releases_descriptors(self):
        before = self.fd_count()
        context = storage_budget.reserve(self.state, disk_bytes=1, inodes=1,
                                         minimum_free_bytes=0,
                                         minimum_free_inodes=0)
        reservation = context.__enter__()
        record = self.records()[0]
        lock = self.state / 'budget.lock'
        lock.unlink()
        fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        with self.assertRaises(storage_budget.BudgetError) as caught:
            context.__exit__(None, None, None)
        self.assertEqual(caught.exception.code, 78)
        self.assertTrue(reservation.closed)
        self.assertTrue(record.exists())
        self.assertEqual(self.fd_count(), before)

    def test_bytes_and_inodes_are_rejected_independently(self):
        free_bytes, free_inodes = 100 * 4096, 37
        controlled = _controlled_statvfs(free_bytes, free_inodes)
        with mock.patch.object(storage_budget.os, 'fstatvfs',
                               return_value=controlled):
            with storage_budget.reserve(self.state, disk_bytes=free_bytes,
                                        inodes=free_inodes,
                                        minimum_free_bytes=0,
                                        minimum_free_inodes=0):
                pass
            with self.assertRaises(storage_budget.BudgetError) as caught:
                with storage_budget.reserve(self.state, disk_bytes=free_bytes + 1,
                                            inodes=0, minimum_free_bytes=0,
                                            minimum_free_inodes=0):
                    pass
            self.assertEqual(caught.exception.code, 75)
            self.assertIn('bytes', str(caught.exception).lower())

            with self.assertRaises(storage_budget.BudgetError) as caught:
                with storage_budget.reserve(self.state, disk_bytes=0,
                                            inodes=free_inodes + 1,
                                            minimum_free_bytes=0,
                                            minimum_free_inodes=0):
                    pass
            self.assertEqual(caught.exception.code, 75)
            self.assertIn('inode', str(caught.exception).lower())

            with storage_budget.reserve(self.state, disk_bytes=0, inodes=0,
                                        minimum_free_bytes=free_bytes,
                                        minimum_free_inodes=free_inodes):
                pass
            with self.assertRaises(storage_budget.BudgetError) as caught:
                with storage_budget.reserve(self.state, disk_bytes=0, inodes=0,
                                            minimum_free_bytes=free_bytes + 1,
                                            minimum_free_inodes=0):
                    pass
            self.assertEqual(caught.exception.code, 75)

            with self.assertRaises(storage_budget.BudgetError) as caught:
                with storage_budget.reserve(self.state, disk_bytes=0, inodes=0,
                                            minimum_free_bytes=0,
                                            minimum_free_inodes=free_inodes + 1):
                    pass
            self.assertEqual(caught.exception.code, 75)
        self.assertEqual(self.records(), [])

    def test_unavailable_inode_counts_are_explained(self):
        values = os.statvfs(self.state)
        unavailable = os.statvfs_result((
            values.f_bsize, values.f_frsize, values.f_blocks, values.f_bfree,
            values.f_bavail, values.f_files, values.f_ffree, 0,
            values.f_flag, values.f_namemax))
        with mock.patch.object(storage_budget.os, 'fstatvfs',
                               return_value=unavailable):
            with self.assertRaises(storage_budget.BudgetError) as caught:
                with storage_budget.reserve(self.state, disk_bytes=0, inodes=0,
                                            minimum_free_bytes=0,
                                            minimum_free_inodes=0):
                    pass
        self.assertEqual(caught.exception.code, 75)
        self.assertIn('inode', str(caught.exception).lower())
        self.assertIn('unlimited', str(caught.exception).lower())

    def test_live_reservations_aggregate_before_admission(self):
        controlled = _controlled_statvfs(100 * 4096, 100)
        with mock.patch.object(storage_budget.os, 'fstatvfs',
                               return_value=controlled):
            with storage_budget.reserve(self.state, disk_bytes=60 * 4096,
                                        inodes=60,
                                        minimum_free_bytes=20 * 4096,
                                        minimum_free_inodes=20):
                with self.assertRaises(storage_budget.BudgetError) as caught:
                    with storage_budget.reserve(self.state, disk_bytes=60 * 4096,
                                                inodes=60,
                                                minimum_free_bytes=20 * 4096,
                                                minimum_free_inodes=20):
                        self.fail('two live reservations exceeded the available budget')
                self.assertEqual(caught.exception.code, 75)

    def test_budget_lock_blocks_another_process_until_released(self):
        ready, release = CTX.Event(), CTX.Event()
        self.process(_hold_budget_lock, str(self.state), ready, release)
        self.assertTrue(ready.wait(10), 'budget-lock holder did not start')

        started, acquired, result = CTX.Event(), CTX.Event(), CTX.Queue()
        self.process(_reserve_once, str(self.state), started, acquired, result)
        self.assertTrue(started.wait(10))
        self.assertFalse(acquired.wait(0.3), 'reservation ignored the held budget.lock')
        release.set()
        self.assertTrue(acquired.wait(10), 'reservation did not resume after unlock')
        self.assertEqual(result.get(timeout=10), ('ok',))

    def test_process_death_releases_reservation_and_orphan_is_reaped(self):
        entered = CTX.Event()
        process = self.process(_reserve_then_exit, str(self.state), entered)
        self.assertTrue(entered.wait(10), 'reservation process did not enter')
        process.join(10)
        self.assertFalse(process.is_alive())
        orphans = self.records()
        self.assertEqual(len(orphans), 1)
        orphan = orphans[0]

        with storage_budget.reserve(self.state, disk_bytes=0, inodes=0,
                                    minimum_free_bytes=0,
                                    minimum_free_inodes=0) as reservation:
            self.assertFalse(orphan.exists())
            self.assertEqual(len(self.records()), 1)
            self.assertNotEqual(self.records()[0], orphan)
        self.assertEqual(self.records(), [])

    def test_live_corrupt_metadata_fails_closed_and_is_preserved(self):
        with storage_budget.reserve(self.state, disk_bytes=10, inodes=1,
                                    minimum_free_bytes=0, minimum_free_inodes=0):
            record = self.records()[0]
            record.write_bytes(b'{not-json')
            with self.assertRaises(storage_budget.BudgetError) as caught:
                with storage_budget.reserve(self.state, disk_bytes=0, inodes=0,
                                            minimum_free_bytes=0,
                                            minimum_free_inodes=0):
                    pass
            self.assertEqual(caught.exception.code, 78)
            self.assertTrue(record.exists())

    def test_unknown_live_record_name_fails_closed(self):
        record = self.state / 'budget-unknown.reservation'
        record.write_bytes(b'{}')
        os.chmod(record, 0o600)
        fd = os.open(record, os.O_RDWR | os.O_NOFOLLOW)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(storage_budget.BudgetError) as caught:
                with storage_budget.reserve(self.state, disk_bytes=0, inodes=0,
                                            minimum_free_bytes=0,
                                            minimum_free_inodes=0):
                    pass
            self.assertEqual(caught.exception.code, 78)
            self.assertTrue(record.exists())
        finally:
            os.close(fd)

    def test_close_preserves_a_record_replaced_by_a_symlink(self):
        context = storage_budget.reserve(self.state, disk_bytes=1, inodes=1,
                                         minimum_free_bytes=0,
                                         minimum_free_inodes=0)
        reservation = context.__enter__()
        record = self.records()[0]
        outside = self.root / 'replacement-target'
        outside.write_bytes(b'preserve')
        record.unlink()
        record.symlink_to(outside)
        try:
            with self.assertRaises(storage_budget.BudgetError) as caught:
                context.__exit__(None, None, None)
            self.assertEqual(caught.exception.code, 78)
            self.assertTrue(reservation.closed)
            self.assertTrue(record.is_symlink())
            self.assertEqual(outside.read_bytes(), b'preserve')
        finally:
            reservation.close()

    def test_orphan_record_is_reaped_only_after_its_flock_is_acquired(self):
        token = 'a' * 32
        record = self.state / f'budget-{token}.reservation'
        record.write_bytes(b'corrupt')
        os.chmod(record, 0o644)
        fd = os.open(record, os.O_RDWR | os.O_NOFOLLOW)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(storage_budget.BudgetError) as caught:
                with storage_budget.reserve(self.state, disk_bytes=0, inodes=0,
                                            minimum_free_bytes=0,
                                            minimum_free_inodes=0):
                    pass
            self.assertEqual(caught.exception.code, 78)
            self.assertTrue(record.exists())
        finally:
            os.close(fd)

        with storage_budget.reserve(self.state, disk_bytes=0, inodes=0,
                                    minimum_free_bytes=0, minimum_free_inodes=0):
            self.assertFalse(record.exists())
            self.assertEqual(len(self.records()), 1)

    def test_pid_in_orphan_metadata_is_not_treated_as_live(self):
        token = 'b' * 32
        record = self.state / f'budget-{token}.reservation'
        record.write_text(json.dumps({'schema': 1, 'token': token,
                                      'disk_bytes': 1, 'inodes': 1,
                                      'pid': os.getpid()}), encoding='ascii')
        os.chmod(record, 0o600)
        with storage_budget.reserve(self.state, disk_bytes=0, inodes=0,
                                    minimum_free_bytes=0, minimum_free_inodes=0):
            self.assertFalse(record.exists())

    def test_symlink_and_hardlink_entries_are_never_deleted(self):
        outside = self.root / 'outside-record'
        outside.write_bytes(b'preserve')
        lock = self.state / 'budget.lock'
        lock.symlink_to(outside)
        with self.assertRaises(storage_budget.BudgetError) as caught:
            with storage_budget.reserve(self.state, disk_bytes=0, inodes=0,
                                        minimum_free_bytes=0,
                                        minimum_free_inodes=0):
                pass
        self.assertEqual(caught.exception.code, 78)
        self.assertTrue(lock.is_symlink())
        self.assertEqual(outside.read_bytes(), b'preserve')

        lock.unlink()
        record = self.state / ('budget-' + 'c' * 32 + '.reservation')
        record.symlink_to(outside)
        with self.assertRaises(storage_budget.BudgetError) as caught:
            with storage_budget.reserve(self.state, disk_bytes=0, inodes=0,
                                        minimum_free_bytes=0,
                                        minimum_free_inodes=0):
                pass
        self.assertEqual(caught.exception.code, 78)
        self.assertTrue(record.is_symlink())
        self.assertEqual(outside.read_bytes(), b'preserve')

        record.unlink()
        os.link(outside, record)
        with self.assertRaises(storage_budget.BudgetError) as caught:
            with storage_budget.reserve(self.state, disk_bytes=0, inodes=0,
                                        minimum_free_bytes=0,
                                        minimum_free_inodes=0):
                pass
        self.assertEqual(caught.exception.code, 78)
        self.assertTrue(record.exists())
        self.assertEqual(outside.read_bytes(), b'preserve')

    def test_foreign_reservation_entry_is_preserved(self):
        record = self.state / ('budget-' + 'd' * 32 + '.reservation')
        record.write_bytes(b'foreign')
        os.chmod(record, 0o600)
        original_stat = os.stat

        def foreign_stat(name, *args, **kwargs):
            result = original_stat(name, *args, **kwargs)
            if name == record.name and kwargs.get('follow_symlinks') is False:
                values = list(result)
                values[4] = 65534
                return os.stat_result(values)
            return result

        with mock.patch.object(storage_budget.os, 'stat', side_effect=foreign_stat):
            with self.assertRaises(storage_budget.BudgetError) as caught:
                with storage_budget.reserve(self.state, disk_bytes=0, inodes=0,
                                            minimum_free_bytes=0,
                                            minimum_free_inodes=0):
                    pass
        self.assertEqual(caught.exception.code, 78)
        self.assertTrue(record.exists())
        self.assertEqual(record.read_bytes(), b'foreign')

    def test_state_directories_on_the_same_filesystem_are_separate_budgets(self):
        other = self.root / 'other-state'
        other.mkdir(mode=0o700)
        self.assertEqual(self.state.stat().st_dev, other.stat().st_dev)
        lock = other / 'budget.lock'
        fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        controlled = _controlled_statvfs(100 * 4096, 100)
        with mock.patch.object(storage_budget.os, 'fstatvfs',
                               return_value=controlled):
            with storage_budget.reserve(self.state, disk_bytes=60 * 4096,
                                        inodes=60,
                                        minimum_free_bytes=0,
                                        minimum_free_inodes=0):
                with storage_budget.reserve(other, disk_bytes=0, inodes=0,
                                            minimum_free_bytes=50 * 4096,
                                            minimum_free_inodes=50):
                    pass


class SharedPoolTests(StorageBudgetTestBase):
    def test_shared_pool_helper_uses_fixture_config_and_device(self):
        device = self.state.stat().st_dev
        expected = (self.config / 'deepseek-team' / 'storage-budget'
                    / f'dev-{device}')
        self.assertEqual(storage_budget.shared_pool_dir(self.state), expected)
        self.assertFalse(expected.exists())

        with mock.patch.dict(os.environ, {'HOME': str(self.home)}, clear=False):
            os.environ.pop('XDG_CONFIG_HOME', None)
            fallback = (self.home / '.config' / 'deepseek-team'
                        / 'storage-budget' / f'dev-{device}')
            self.assertEqual(storage_budget.shared_pool_dir(self.state), fallback)
            self.assertFalse(fallback.exists())
        with storage_budget.reserve(
                self.state, disk_bytes=1, inodes=1,
                minimum_free_bytes=0, minimum_free_inodes=0,
                allocation_path=self.state,
                pool_dir=storage_budget.shared_pool_dir(self.state)):
            pass
        self.assertEqual(expected.stat().st_mode & 0o777, 0o700)

    def test_state_roots_on_the_same_filesystem_share_pool_admission(self):
        other = self.root / 'state-b'
        other.mkdir(mode=0o700)
        pool = self.root / 'shared-pool'
        self.assertEqual(self.state.stat().st_dev, other.stat().st_dev)
        controlled = _controlled_statvfs(100 * 4096, 100)

        with mock.patch.object(storage_budget.os, 'fstatvfs',
                               return_value=controlled):
            with storage_budget.reserve(
                    self.state, disk_bytes=60 * 4096, inodes=60,
                    minimum_free_bytes=0, minimum_free_inodes=0,
                    allocation_path=self.state, pool_dir=pool) as first:
                self.assertEqual(len(self.records(pool)), 1)
                self.assertEqual(self.records(self.state), [])
                with self.assertRaises(storage_budget.BudgetError) as caught:
                    with storage_budget.reserve(
                            other, disk_bytes=60 * 4096, inodes=60,
                            minimum_free_bytes=0, minimum_free_inodes=0,
                            allocation_path=other, pool_dir=pool):
                        self.fail('same-filesystem roots failed to share capacity')
                self.assertEqual(caught.exception.code, 75)
                with storage_budget.reserve(
                        other, disk_bytes=40 * 4096, inodes=40,
                        minimum_free_bytes=0, minimum_free_inodes=0,
                        allocation_path=other, pool_dir=pool):
                    self.assertEqual(len(self.records(pool)), 2)
                self.assertEqual(first.device, self.state.stat().st_dev)
        self.assertEqual(self.records(pool), [])

    def test_other_device_records_do_not_consume_capacity(self):
        other = self.other_filesystem_directory()
        pool = self.root / 'cross-device-pool'
        controlled = _controlled_statvfs(100 * 4096, 100)
        measured_devices = []

        def measured(fd):
            measured_devices.append(os.fstat(fd).st_dev)
            return controlled

        with mock.patch.object(storage_budget.os, 'fstatvfs', side_effect=measured):
            with storage_budget.reserve(
                    self.state, disk_bytes=80 * 4096, inodes=0,
                    minimum_free_bytes=0, minimum_free_inodes=0,
                    allocation_path=self.state, pool_dir=pool) as first:
                with storage_budget.reserve(
                        other, disk_bytes=80 * 4096, inodes=0,
                        minimum_free_bytes=0, minimum_free_inodes=0,
                        allocation_path=other, pool_dir=pool) as second:
                    self.assertNotEqual(first.device, second.device)
                    records = self.records(pool)
                    self.assertEqual(len(records), 2)
                    devices = {json.loads(record.read_bytes())['device']
                               for record in records}
                    self.assertEqual(devices, {first.device, second.device})
                    with self.assertRaises(storage_budget.BudgetError) as caught:
                        with storage_budget.reserve(
                                other, disk_bytes=30 * 4096, inodes=0,
                                minimum_free_bytes=0, minimum_free_inodes=0,
                                allocation_path=other, pool_dir=pool):
                            self.fail('same-device records were not aggregated')
                    self.assertEqual(caught.exception.code, 75)
        self.assertEqual(set(measured_devices), {first.device, second.device})
        self.assertNotEqual(pool.stat().st_dev, other.stat().st_dev)
        self.assertEqual(self.records(pool), [])

    def test_explicit_pool_rejects_unsafe_parents_without_chmod(self):
        public = self.root / 'public-pool'
        public.mkdir(mode=0o755)
        public.chmod(0o755)
        with self.assertRaises(storage_budget.BudgetError) as caught:
            with storage_budget.reserve(
                    self.state, disk_bytes=0, inodes=0,
                    minimum_free_bytes=0, minimum_free_inodes=0,
                    allocation_path=self.state, pool_dir=public):
                pass
        self.assertEqual(caught.exception.code, 78)
        self.assertEqual(public.stat().st_mode & 0o777, 0o755)

        writable = self.root / 'writable-parent'
        writable.mkdir(mode=0o700)
        writable.chmod(0o777)
        nested = writable / 'pool'
        nested.mkdir(mode=0o700)
        with self.assertRaises(storage_budget.BudgetError) as caught:
            with storage_budget.reserve(
                    self.state, disk_bytes=0, inodes=0,
                    minimum_free_bytes=0, minimum_free_inodes=0,
                    allocation_path=self.state, pool_dir=nested):
                pass
        self.assertEqual(caught.exception.code, 78)
        self.assertEqual(nested.stat().st_mode & 0o777, 0o700)

        target = self.root / 'pool-target'
        target.mkdir(mode=0o700)
        alias = self.root / 'pool-alias'
        alias.symlink_to(target, target_is_directory=True)
        with self.assertRaises(storage_budget.BudgetError) as caught:
            with storage_budget.reserve(
                    self.state, disk_bytes=0, inodes=0,
                    minimum_free_bytes=0, minimum_free_inodes=0,
                    allocation_path=self.state, pool_dir=alias / 'pool'):
                pass
        self.assertEqual(caught.exception.code, 78)
        self.assertEqual(list(target.iterdir()), [])


if __name__ == '__main__':
    unittest.main()
