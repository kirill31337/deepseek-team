"""Linked corrections remain valid after provider-free preparation of a succeeded job."""
from __future__ import annotations

import os
from unittest import mock

from codex_deepseek_team import (activation, coordination, project, settings,
                                 workspace, workspace_lifecycle)
from tests.test_workspace_lifecycle import LifecycleFixture


class CorrectionPreparationTests(LifecycleFixture):
    def setUp(self):
        super().setUp()
        for name in ('home', 'codex', 'claude', 'config'):
            (self.root / name).mkdir(mode=0o700, exist_ok=True)
        self.state.mkdir(mode=0o700, exist_ok=True)
        disabled = mock.patch.dict(os.environ, {'DEEPSEEK_TEAM_DISABLED': '0'})
        disabled.start()
        self.addCleanup(disabled.stop)
        activation.set_enabled(self.source, True)
        project.attach(self.source, coordinator='codex')
        settings.set_values(self.source / settings.PROJECT_FILE,
                            delegation_level=75, access='full-access')
        self.session = 'correction-preparation'
        self._turn = 0

    def deliverable(self, identifier='fix', *, correction=None):
        value = {
            'id': identifier, 'kind': 'implementation', 'scope': ['app.txt'],
            'executor': 'worker', 'acceptance': ['the correction is applied'],
            'dependencies': [], 'checks': [],
        }
        if correction is not None:
            value['correction_of'] = dict(correction)
        return value

    def new_task(self):
        self._turn += 1
        task = coordination.open_task(
            self.source, session_id=self.session, turn_id=str(self._turn),
            prompt='prepare a linked correction', policy=settings.resolve(self.source))
        return coordination.plan_task(self.source, task['id'], {
            'classification': 'substantial',
            'deliverables': [self.deliverable()],
        })

    def row(self, task_id, assignment_id):
        task = coordination.load_task(self.source, task_id)
        return next(row for row in task['assignments'] if row['id'] == assignment_id)

    def reviewed_execution(self):
        task = self.new_task()
        assignment = task['assignments'][0]['id']
        copy = self.copy()
        with copy.lock(recover=True):
            coordination.assignment_started(
                self.source, task['id'], assignment, copy.id, 'codex', [])
            copy.begin('execution')
            (copy.path / 'app.txt').write_text('reviewed result\n')
            copy.finish('succeeded', exit_code=0)
            coordination.assignment_finished(
                self.source, task['id'], assignment, 'succeeded',
                'reviewed result', ['app.txt'], [])
        coordination.use_result(
            self.source, task['id'], assignment, 'needs-rework',
            'one linked correction is required')
        return task, assignment, copy

    def correction_plan(self, task_id, reference):
        task = coordination.load_task(self.source, task_id)
        deliverables = [dict(item) for item in task['deliverables']]
        deliverables.append(self.deliverable('fix-correction', correction=reference))
        return {'classification': 'substantial', 'deliverables': deliverables}

    def workspace_files(self, copy):
        return {name: (copy.directory / name).read_bytes()
                for name in ('record.json', 'lease.json')}

    def workspace_ids(self):
        directory = self.state / 'workspaces'
        if not directory.exists():
            return set()
        return {path.name for path in directory.iterdir() if len(path.name) == 32}

    def assert_plan_refused(self, task_id, assignment_id, copy, *, code=64):
        ledger = coordination._task_path(self.source, task_id).read_bytes()
        files = self.workspace_files(copy)
        workspaces = self.workspace_ids()
        with mock.patch.object(workspace, 'create',
                               side_effect=AssertionError('correction allocated a new copy')):
            with self.assertRaises(coordination.CoordinationError) as caught:
                coordination.plan_task(
                    self.source, task_id,
                    self.correction_plan(task_id, {
                        'task_id': task_id,
                        'assignment_id': assignment_id,
                    }))
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(coordination._task_path(self.source, task_id).read_bytes(), ledger)
        self.assertEqual(self.workspace_files(copy), files)
        self.assertEqual(self.workspace_ids(), workspaces)
        return caught.exception

    def test_planning_accepts_successful_preparation_after_reviewed_execution(self):
        task, assignment, copy = self.reviewed_execution()
        execution_attempt = self.row(task['id'], assignment)['attempt_id']
        job_id = workspace_lifecycle.read(copy)['job_id']
        inode = copy.path.stat().st_ino
        binding = workspace_lifecycle.read(copy)['binding']

        self.assertEqual(workspace.prepare(copy, ['true']), 0)
        first_preparation = copy.metadata['attempt_id']
        self.assertEqual(workspace.prepare(copy, ['true']), 0)
        prepared = workspace_lifecycle.read(copy)
        self.assertEqual([(entry['operation'], entry['status'])
                          for entry in prepared['attempts']],
                         [('execution', 'succeeded'), ('preparation', 'succeeded'),
                          ('preparation', 'succeeded')])
        preparation_attempt = prepared['attempts'][-1]['id']
        self.assertNotEqual(preparation_attempt, first_preparation)
        self.assertEqual(copy.metadata['attempt_id'], preparation_attempt)
        self.assertEqual(copy.metadata['operation'], 'preparation')
        self.assertEqual(copy.metadata['status'], 'succeeded')
        self.assertEqual(copy.metadata['job_id'], job_id)
        self.assertEqual(copy.metadata['work_inode'], inode)

        reference = {'task_id': task['id'], 'assignment_id': assignment}
        record = copy.directory / 'record.json'
        lease = copy.directory / 'lease.json'
        record_before, lease_before = record.read_bytes(), lease.read_bytes()
        workspaces = self.workspace_ids()
        with mock.patch.object(workspace, 'create',
                               side_effect=AssertionError('correction allocated a new copy')):
            planned = coordination.plan_task(
                self.source, task['id'], self.correction_plan(task['id'], reference))

        correction = next(row for row in planned['assignments']
                          if row['deliverable_id'] == 'fix-correction')
        self.assertEqual(correction['correction_of'], reference)
        self.assertEqual(record.read_bytes(), record_before)
        self.assertEqual(lease.read_bytes(), lease_before)
        self.assertEqual(self.workspace_ids(), workspaces)
        predecessor = self.row(task['id'], assignment)
        self.assertEqual(predecessor['attempt_id'], execution_attempt)
        self.assertEqual(predecessor['job_id'], job_id)
        self.assertEqual(workspace_lifecycle.read(copy)['binding'], binding)
        self.assertEqual(copy.path.stat().st_ino, inode)

    def test_unknown_or_stale_preparation_identity_is_refused(self):
        task, assignment, copy = self.reviewed_execution()
        self.assertEqual(workspace.prepare(copy, ['true']), 0)
        preparation_attempt = copy.metadata['attempt_id']
        execution_attempt = self.row(task['id'], assignment)['attempt_id']

        for label, metadata in (
                ('unknown metadata attempt', {'attempt_id': 'f' * 32}),
                ('stale execution identity', {'attempt_id': execution_attempt,
                                               'operation': 'execution'}),
        ):
            with self.subTest(label):
                copy.metadata.update(metadata)
                copy.save()
                self.assert_plan_refused(task['id'], assignment, copy)
                copy.metadata.update(attempt_id=preparation_attempt,
                                     operation='preparation')
                copy.save()

    def test_newer_execution_is_refused(self):
        task, assignment, copy = self.reviewed_execution()
        self.assertEqual(workspace.prepare(copy, ['true']), 0)
        execution_attempt = self.row(task['id'], assignment)['attempt_id']
        with copy.lock(recover=True):
            copy.begin('execution')
            (copy.path / 'app.txt').write_text('newer execution\n')
            copy.finish('succeeded', exit_code=0)
        copy.metadata.update(attempt_id=execution_attempt, operation='execution')
        copy.save()
        self.assert_plan_refused(task['id'], assignment, copy)

    def test_unknown_preparation_operation_is_refused(self):
        task, assignment, copy = self.reviewed_execution()
        self.assertEqual(workspace.prepare(copy, ['true']), 0)
        lease = workspace_lifecycle.read(copy)
        lease['attempts'][-1]['operation'] = 'mystery'
        workspace_lifecycle.write(copy, lease)
        self.assert_plan_refused(task['id'], assignment, copy)

    def test_changed_execution_result_is_refused(self):
        task, assignment, copy = self.reviewed_execution()
        self.assertEqual(workspace.prepare(copy, ['true']), 0)
        lease = workspace_lifecycle.read(copy)
        lease['attempts'][0]['status'] = 'failed'
        workspace_lifecycle.write(copy, lease)
        self.assert_plan_refused(task['id'], assignment, copy)

    def test_failed_preparation_is_refused(self):
        task, assignment, copy = self.reviewed_execution()
        self.assertEqual(workspace.prepare(copy, ['false']), 1)
        self.assert_plan_refused(task['id'], assignment, copy)

    def test_running_preparation_is_refused(self):
        task, assignment, copy = self.reviewed_execution()
        with copy.lock(recover=True):
            copy.begin('preparation')
        self.assert_plan_refused(task['id'], assignment, copy)

    def test_pending_execution_reservation_is_refused(self):
        task, assignment, copy = self.reviewed_execution()
        self.assertEqual(workspace.prepare(copy, ['true']), 0)
        with copy.lock(recover=True):
            workspace_lifecycle.prepare_attempt(copy, task['id'], assignment)
        self.assert_plan_refused(task['id'], assignment, copy)

    def test_wrong_binding_or_finalized_job_is_refused(self):
        task, assignment, copy = self.reviewed_execution()
        self.assertEqual(workspace.prepare(copy, ['true']), 0)

        lease = workspace_lifecycle.read(copy)
        original_binding = dict(lease['binding'])
        lease['binding'] = {'task_id': task['id'], 'assignment_id': 'as-' + '0' * 16}
        workspace_lifecycle.write(copy, lease)
        self.assert_plan_refused(task['id'], assignment, copy)

        lease['binding'] = original_binding
        lease['phase'] = 'cancelled'
        workspace_lifecycle.write(copy, lease)
        self.assert_plan_refused(task['id'], assignment, copy, code=73)
