"""Allocation consumers reserve capacity before creating execution payloads."""
from pathlib import Path
import os
from unittest import mock

from codex_deepseek_team import storage_budget, workspace, managed, relay
from tests.test_workspace_lifecycle import LifecycleFixture


class CheckoutAdmissionTests(LifecycleFixture):
    def test_runtime_capacity_denial_precedes_payload_and_credential_access(self):
        copy = self.copy()
        with mock.patch.object(storage_budget, 'reserve',
                side_effect=storage_budget.BudgetError('no runtime room', 75)):
            with self.assertRaises(storage_budget.BudgetError):
                with managed._runtime_environment(copy):
                    self.fail('runtime allocation proceeded')
        self.assertFalse(list(copy.directory.glob('runtime-*')))

    def test_runtime_control_is_owned_and_short_socket_alias_handles_long_state_path(self):
        copy = self.copy()
        with managed._runtime_environment(copy) as (home, control, socket_path):
            self.assertTrue(home.is_relative_to(copy.directory))
            self.assertTrue(control.is_relative_to(copy.directory))
            self.assertFalse(control.is_relative_to(home))
            self.assertLess(len(os.fsencode(socket_path)), 108)
            with relay.ProviderRelay(socket_path, 'fixture-unused-key'):
                self.assertTrue((control / 'provider.sock').exists())
        self.assertFalse(list(copy.directory.glob('runtime-*')))

    def test_budget_rejection_precedes_checkout_and_identifies_no_workspace(self):
        with mock.patch.object(storage_budget, 'reserve',
                side_effect=storage_budget.BudgetError('fixture capacity exhausted', 75)):
            with self.assertRaises(workspace.WorkspaceError) as caught:
                self.copy()
        self.assertEqual(caught.exception.code, 75)
        self.assertIsNone(caught.exception.workspace_id)
        copies = self.state / 'workspaces'
        self.assertFalse(copies.exists() and list(copies.iterdir()))
        self.assertEqual((self.source / 'app.txt').read_text(), 'base\n')

    def test_checkout_estimate_uses_pinned_commit_not_ignored_or_untracked_data(self):
        allocation = self.api('execution_allocation')
        report = workspace.check_source(self.source)
        before = allocation.checkout_requirement(self.source, report['head'])
        with (self.source / 'untracked-runtime').open('wb') as stream:
            stream.truncate(1024 ** 3)
        after = allocation.checkout_requirement(self.source, report['head'])
        self.assertEqual(before, after)
        self.assertGreater(before['disk_bytes'], 0)
        self.assertGreater(before['inodes'], 2)

    def test_real_checkout_consumption_fits_reserved_requirement(self):
        allocation = self.api('execution_allocation')
        report = workspace.check_source(self.source)
        requirement = allocation.checkout_requirement(self.source, report['head'])
        copy = self.copy()
        actual = storage_budget.measure(copy.path)
        self.assertLessEqual(actual['allocated_bytes'], requirement['disk_bytes'])
        self.assertLessEqual(actual['inodes'], requirement['inodes'])

    def test_runtime_budget_is_shared_on_same_device_and_released_on_exit(self):
        allocation = self.api('execution_allocation')
        self.assertTrue(hasattr(allocation, 'runtime'), 'runtime allocation admission missing')
        first, second = self.root / 'runtime-one', self.root / 'runtime-two'
        first.mkdir(mode=0o700)
        second.mkdir(mode=0o700)
        original = os.statvfs(first)
        controlled = os.statvfs_result((
            original.f_bsize, 4096, original.f_blocks, 131072, 131072,
            original.f_files, 100000, 100000, original.f_flag, original.f_namemax))
        with mock.patch.object(storage_budget.os, 'fstatvfs', return_value=controlled):
            with allocation.runtime(first, second):
                pool = storage_budget.shared_pool_dir(first)
                self.assertEqual(len(list(pool.glob('budget-*.reservation'))), 1)
                with self.assertRaises(storage_budget.BudgetError):
                    with allocation.reserve(first, disk_bytes=1, inodes=1):
                        pass
            with allocation.reserve(first, disk_bytes=256 * 1024 * 1024, inodes=8192):
                pass
        self.assertEqual(list(pool.glob('budget-*.reservation')), [])
