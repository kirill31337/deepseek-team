"""Plan revisions must preserve a deliverable bound for preparation."""
from __future__ import annotations

import copy as copy_module
import unittest

from codex_deepseek_team import coordination, settings, workspace_lifecycle
from tests.test_preparation_finalization import PreparationFinalizationFixture


class PreparationBindingPlanTests(PreparationFinalizationFixture):
    def bind_preparation(self, *, delegation_level=None):
        if delegation_level is not None:
            settings.set_values(self.source / settings.PROJECT_FILE,
                                delegation_level=delegation_level,
                                access='full-access')
        task, assignment_id = self.new_task()
        copy = self.copy()
        with copy.lock():
            coordination.assignment_preparing(
                self.source, task['id'], assignment_id, copy)
        task = coordination.load_task(self.source, task['id'])
        self.assert_bound(task['id'], assignment_id, copy)
        return task, assignment_id, copy

    def normalized_plan(self, task):
        current = coordination.load_task(self.source, task['id'])
        return {
            'classification': current['classification'],
            'deliverables': copy_module.deepcopy(current['deliverables']),
        }

    def assert_bound(self, task_id, assignment_id, copy):
        row = self.row(task_id, assignment_id)
        lease = workspace_lifecycle.read(copy)
        self.assertEqual(row['status'], 'planned')
        self.assertEqual(row['workspace_id'], copy.id)
        self.assertEqual(row['job_id'], lease['job_id'])
        self.assertEqual(lease['binding'], {
            'task_id': task_id, 'assignment_id': assignment_id})
        self.assertEqual(lease['attempts'], [])
        self.assertNotIn('pending_attempt', lease)

    def test_bound_deliverable_cannot_be_removed(self):
        task, assignment_id, copy = self.bind_preparation()
        before = self.snapshot(task['id'], copy)

        with self.assertRaises(coordination.CoordinationError):
            coordination.plan_task(self.source, task['id'], {
                'classification': 'substantial',
                'deliverables': [self.deliverable('replacement')],
            })

        self.assertEqual(self.snapshot(task['id'], copy), before)
        self.assert_bound(task['id'], assignment_id, copy)

    def test_bound_deliverable_definition_is_immutable(self):
        changes = {
            'scope': ['replacement.txt'],
            'checks': ['python3 -V'],
        }
        for field, value in changes.items():
            with self.subTest(field=field):
                task, assignment_id, copy = self.bind_preparation()
                plan = self.normalized_plan(task)
                plan['deliverables'][0][field] = value
                before = self.snapshot(task['id'], copy)

                with self.assertRaises(coordination.CoordinationError):
                    coordination.plan_task(self.source, task['id'], plan)

                self.assertEqual(self.snapshot(task['id'], copy), before)
                self.assert_bound(task['id'], assignment_id, copy)

    def test_bound_deliverable_cannot_be_reassigned_away_from_worker(self):
        task, assignment_id, copy = self.bind_preparation(delegation_level=25)
        plan = self.normalized_plan(task)
        plan['deliverables'][0]['executor'] = 'coordinator'
        before = self.snapshot(task['id'], copy)

        with self.assertRaises(coordination.CoordinationError):
            coordination.plan_task(self.source, task['id'], plan)

        self.assertEqual(self.snapshot(task['id'], copy), before)
        self.assert_bound(task['id'], assignment_id, copy)

    def test_identical_plan_is_byte_exact_noop_after_binding(self):
        task, assignment_id, copy = self.bind_preparation()
        before = self.snapshot(task['id'], copy)
        before_row = self.row(task['id'], assignment_id)
        before_lease = workspace_lifecycle.read(copy)

        repeated = coordination.plan_task(
            self.source, task['id'], self.normalized_plan(task))

        self.assertEqual(self.snapshot(task['id'], copy), before)
        row = repeated['assignments'][0]
        lease = workspace_lifecycle.read(copy)
        self.assertEqual(row['id'], before_row['id'])
        self.assertEqual(row['job_id'], before_row['job_id'])
        self.assertEqual(row['workspace_id'], before_row['workspace_id'])
        self.assertEqual(row['status'], 'planned')
        self.assertEqual(lease['job_id'], before_lease['job_id'])
        self.assertEqual(lease['workspace_id'], before_lease['workspace_id'])
        self.assertEqual(lease['binding'], before_lease['binding'])
        self.assertNotIn('attempt_id', row)
        self.assertNotIn('started_at', row)
        self.assertNotIn('execution_snapshot', row)
        self.assertEqual(lease['attempts'], [])
        self.assertNotIn('pending_attempt', lease)

    def test_refused_mutations_keep_failure_review_and_cancellation_available(self):
        task, assignment_id, copy = self.bind_preparation()

        with self.assertRaises(coordination.CoordinationError):
            coordination.plan_task(self.source, task['id'], {
                'classification': 'substantial',
                'deliverables': [self.deliverable('replacement')],
            })
        plan = self.normalized_plan(task)
        plan['deliverables'][0]['scope'] = ['replacement.txt']
        with self.assertRaises(coordination.CoordinationError):
            coordination.plan_task(self.source, task['id'], plan)
        self.assert_bound(task['id'], assignment_id, copy)

        with copy.lock():
            copy.failed('preparation', 78)
        coordination.assignment_preparation_failed(
            self.source, task['id'], assignment_id,
            'provider-free preparation failure', exit_code=78,
            workspace_id=copy.id)
        self.assertEqual(self.row(task['id'], assignment_id)['status'], 'failed')
        self.assertEqual(workspace_lifecycle.read(copy)['binding'], {
            'task_id': task['id'], 'assignment_id': assignment_id})

        with self.local_capture():
            cancelled = coordination.use_result(
                self.source, task['id'], assignment_id, 'rejected',
                'rejected the provider-free preparation failure')

        row = cancelled['assignments'][0]
        self.assertEqual(row['job_finalization']['id'], copy.id)
        self.assertFalse(copy.path.exists())
        lease = workspace_lifecycle.read(copy)
        self.assertEqual(lease['phase'], 'cancelled')
        self.assertEqual(lease['binding'], {
            'task_id': task['id'], 'assignment_id': assignment_id})
        self.assertEqual(
            coordination.complete_task(self.source, task['id'])['status'],
            'completed')


if __name__ == '__main__':
    unittest.main()
