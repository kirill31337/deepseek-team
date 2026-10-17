"""Materialization admissions reserve apparent size before staging or copying."""
from __future__ import annotations

import contextlib
import os
from pathlib import Path
import unittest
from unittest import mock

from codex_deepseek_team import (
    execution_allocation, storage_budget, toolchain_cache, toolchains, workspace)
from tests.test_toolchains import ToolchainFixture


class MaterializationFixture(ToolchainFixture):
    def reserve_recorder(self):
        records = []
        real_reserve = execution_allocation.reserve

        @contextlib.contextmanager
        def reserve(target, *, disk_bytes, inodes):
            records.append((Path(target), disk_bytes, inodes))
            with real_reserve(target, disk_bytes=disk_bytes, inodes=inodes) as reservation:
                yield reservation

        patcher = mock.patch.object(execution_allocation, 'reserve', side_effect=reserve)
        patcher.start()
        self.addCleanup(patcher.stop)
        return records

    def guard_copies(self, records):
        real_tree = toolchains._copy_tree
        real_file = toolchains._copy_file

        def tree(*args, **kwargs):
            self.assertTrue(records, 'copying began before materialization admission')
            return real_tree(*args, **kwargs)

        def file(*args, **kwargs):
            self.assertTrue(records, 'file copying began before materialization admission')
            return real_file(*args, **kwargs)

        patcher = mock.patch.multiple(toolchains, _copy_tree=tree, _copy_file=file)
        patcher.start()
        self.addCleanup(patcher.stop)

    def cache_staging(self):
        return self.state / 'toolchain-cache' / '.staging'

    def cache_entries(self):
        return self.state / 'toolchain-cache' / 'entries'

    def assert_no_reservation_records(self, target):
        pool = storage_budget.shared_pool_dir(target)
        self.assertEqual(list(pool.glob('budget-*.reservation')), [])

    def sparse_file(self, path, size):
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('wb') as stream:
            stream.truncate(size)
        self.assertEqual(path.stat().st_size, size)
        return path


class PrepareReservationTests(MaterializationFixture):
    def test_source_owner_lock_also_covers_another_state_root(self):
        other = workspace.create(self.source, self.root / 'another-state')
        with other.lock():
            with self.assertRaises(toolchains.ToolchainError) as caught:
                toolchains.prepare(self.copy, copies=[(other.path / 'sample.txt', 'inputs/data.txt')])
        self.assertEqual(caught.exception.code, 75)

    def test_prepare_reserves_apparent_size_before_staging(self):
        root = self.software()
        sparse = self.sparse_file(root / 'lib' / 'sparse.bin', 8 * 1024 * 1024)
        source_file = self.root / 'inputs' / 'local.properties'
        source_file.parent.mkdir(parents=True)
        source_file.write_text('sdk.dir=prepared\n')

        records = self.reserve_recorder()
        self.guard_copies(records)
        toolchains.prepare(self.copy, tools={'sdk': root},
                           copies=[(source_file, 'build/local.properties')])

        self.assertEqual(len(records), 1)
        target, disk_bytes, inodes = records[0]
        self.assertEqual(target, self.copy.path)
        self.assertGreaterEqual(disk_bytes, sparse.stat().st_size)
        self.assertGreaterEqual(disk_bytes, 8 * 1024 * 1024 + 4096 * 4)
        self.assertGreaterEqual(inodes, 12)
        self.assertEqual(list((self.copy.path / '.deepseek-tools').glob('.stage-*')), [])

    def test_prepare_budget_failure_is_toolchain_75_without_staging(self):
        with mock.patch.object(
                execution_allocation, 'reserve',
                side_effect=storage_budget.BudgetError('fixture capacity exhausted', 75)):
            with self.assertRaises(toolchains.ToolchainError) as caught:
                toolchains.prepare(self.copy, tools={'sdk': self.software()})
        self.assertEqual(caught.exception.code, 75)
        self.assertFalse((self.copy.path / '.deepseek-tools').exists())
        self.assertEqual(self.reloaded(self.copy).metadata['status'], 'failed')

    def test_prepare_releases_its_reservation_when_copying_fails(self):
        root = self.software()
        with mock.patch.object(toolchains, '_copy_tree',
                               side_effect=OSError('fixture copy failure')):
            with self.assertRaises(toolchains.ToolchainError):
                toolchains.prepare(self.copy, tools={'sdk': root})
        self.assert_no_reservation_records(self.copy.path)

    def test_invalid_destination_and_credential_source_precede_reservation(self):
        credential_copy = self.fresh_copy()
        records = self.reserve_recorder()
        source_file = self.root / 'inputs' / 'local.properties'
        source_file.parent.mkdir(parents=True)
        source_file.write_text('value\n')
        credential = self.root / 'secrets' / 'id_rsa'
        credential.parent.mkdir(parents=True)
        credential.write_text('fixture credential\n')
        with mock.patch.object(toolchains, '_copy_file',
                               side_effect=AssertionError('copy ran before validation')):
            with self.assertRaises(toolchains.ToolchainError):
                toolchains.prepare(self.copy, copies=[(source_file, '../escape')])
            with self.assertRaises(toolchains.ToolchainError):
                toolchains.prepare(credential_copy,
                                   copies=[(credential, 'prepared/secret')])
        self.assertEqual(records, [])

    def test_another_workspace_source_requires_its_owner_lock(self):
        other = self.fresh_copy()
        source = other.path / 'inputs' / 'from-other.txt'
        source.parent.mkdir(parents=True)
        source.write_text('other workspace\n')

        with other.lock():
            with self.assertRaises(toolchains.ToolchainError) as caught:
                toolchains.prepare(self.copy, copies=[(source, 'prepared/from-other.txt')])
        self.assertEqual(caught.exception.code, 75)
        self.assertIn('snapshot', str(caught.exception).lower())

        destination = self.fresh_copy()
        toolchains.prepare(destination, copies=[(source, 'prepared/from-other.txt')])
        self.assertEqual((destination.path / 'prepared/from-other.txt').read_text(),
                         'other workspace\n')

    def test_saved_recipe_snapshots_the_prepared_destination_for_own_copy_input(self):
        source = self.copy.path / 'inputs' / 'local.properties'
        source.parent.mkdir(parents=True)
        source.write_text('sdk.dir=prepared\n')
        calls = []
        real_snapshot = toolchain_cache.snapshot

        def snapshot(state, value, **kwargs):
            calls.append(Path(value))
            return real_snapshot(state, value, **kwargs)

        with mock.patch.object(toolchain_cache, 'snapshot', side_effect=snapshot):
            toolchains.prepare(self.copy, copies=[(source, 'build/local.properties')],
                               save_project=True)
        prepared = self.copy.path / 'build/local.properties'
        self.assertIn(prepared, calls)
        self.assertNotIn(source, calls)


class SnapshotReservationTests(MaterializationFixture):
    def test_direct_snapshot_cannot_borrow_a_busy_execution_copy(self):
        with self.copy.lock():
            with self.assertRaises(toolchains.ToolchainError) as caught:
                toolchain_cache.snapshot(self.state, self.copy.path / 'sample.txt')
        self.assertEqual(caught.exception.code, 75)

    def test_snapshot_reserves_apparent_size_before_copying(self):
        root = self.software(name='snapshot-sparse')
        sparse = self.sparse_file(root / 'lib' / 'sparse.bin', 4 * 1024 * 1024)
        records = self.reserve_recorder()
        self.guard_copies(records)
        cached = toolchain_cache.snapshot(self.state, root)
        self.assertTrue(cached.is_dir())
        self.assertEqual(len(records), 1)
        target, disk_bytes, inodes = records[0]
        self.assertEqual(target, self.cache_staging())
        self.assertGreaterEqual(disk_bytes, sparse.stat().st_size)
        self.assertGreaterEqual(inodes, 8)

    def test_snapshot_budget_failure_is_toolchain_75_before_staging(self):
        root = self.software(name='snapshot-budget')
        with mock.patch.object(
                execution_allocation, 'reserve',
                side_effect=storage_budget.BudgetError('fixture capacity exhausted', 75)):
            with self.assertRaises(toolchains.ToolchainError) as caught:
                toolchain_cache.snapshot(self.state, root)
        self.assertEqual(caught.exception.code, 75)
        self.assertEqual(list(self.cache_staging().glob('.stage-*')), [])
        self.assertEqual(list(self.cache_entries().iterdir()), [])

    def test_snapshot_releases_its_reservation_when_copying_fails(self):
        source = self.root / 'single.bin'
        source.write_bytes(b'payload\n')
        with mock.patch.object(toolchains, '_copy_file',
                               side_effect=OSError('fixture copy failure')):
            with self.assertRaises(OSError):
                toolchain_cache.snapshot(self.state, source)
        self.assert_no_reservation_records(self.cache_staging())

    def test_inner_storage_budget_75_normalizes_to_toolchain_75(self):
        with mock.patch.object(
                storage_budget, 'reserve',
                side_effect=storage_budget.BudgetError('deep fixture exhaustion', 75)):
            with self.assertRaises(toolchains.ToolchainError) as caught:
                toolchain_cache.snapshot(self.state, self.software(name='deep-budget'))
        self.assertEqual(caught.exception.code, 75)
        self.assertIn('deep fixture exhaustion', str(caught.exception))

    def test_finite_cache_cap_still_rejects_before_admission(self):
        root = self.software(name='snapshot-cap')
        records = self.reserve_recorder()
        with self.assertRaises(toolchains.ToolchainError) as caught:
            toolchain_cache.snapshot(self.state, root, max_bytes=1)
        self.assertEqual(caught.exception.code, 78)
        self.assertIn('finite byte/inode budget', str(caught.exception))
        self.assertEqual(records, [])


class MaterializeReservationTests(MaterializationFixture):
    def setUp(self):
        super().setUp()
        self.cached = toolchain_cache.snapshot(self.state, self.software(name='materialize'))
        self.destination_root = self.root / 'destination'
        self.destination_root.mkdir(mode=0o700)

    def test_materialize_reserves_the_destination_parent_before_copying(self):
        destination = self.destination_root / 'copy'
        records = self.reserve_recorder()
        self.guard_copies(records)
        stats = toolchain_cache.materialize(self.state, self.cached, destination)
        self.assertGreater(stats['files'], 0)
        self.assertEqual(len(records), 1)
        target, disk_bytes, inodes = records[0]
        self.assertEqual(target, self.destination_root)
        self.assertGreaterEqual(disk_bytes, 4096)
        self.assertGreaterEqual(inodes, 4)

    def test_materialize_budget_failure_is_toolchain_75_without_destination(self):
        destination = self.destination_root / 'copy'
        with mock.patch.object(
                execution_allocation, 'reserve',
                side_effect=storage_budget.BudgetError('fixture capacity exhausted', 75)):
            with self.assertRaises(toolchains.ToolchainError) as caught:
                toolchain_cache.materialize(self.state, self.cached, destination)
        self.assertEqual(caught.exception.code, 75)
        self.assertFalse(os.path.lexists(destination))

    def test_materialize_releases_its_reservation_when_restoring_modes_fails(self):
        destination = self.destination_root / 'copy'
        with mock.patch.object(toolchain_cache, '_restore_modes',
                               side_effect=OSError('fixture restore failure')):
            with self.assertRaises(OSError):
                toolchain_cache.materialize(self.state, self.cached, destination)
        self.assert_no_reservation_records(self.destination_root)

    def test_materialize_validates_destination_before_admission(self):
        outside = self.root / 'outside-destination'
        outside.mkdir()
        linked = self.root / 'linked-destination'
        os.symlink(outside, linked)
        destination = linked / 'copy'
        records = self.reserve_recorder()
        with self.assertRaises(toolchains.ToolchainError):
            toolchain_cache.materialize(self.state, self.cached, destination)
        self.assertEqual(records, [])
        self.assertFalse((outside / 'copy').exists())


if __name__ == '__main__':
    unittest.main()
