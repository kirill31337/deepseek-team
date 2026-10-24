"""Regression coverage for preparation review and terminal publication gaps."""
from __future__ import annotations

import copy as copy_module
import json
import subprocess
import time
import unittest
from unittest import mock

from codex_deepseek_team import (coordination, coordinator_hooks, worker,
                                 workspace, workspace_gc, workspace_lifecycle)
from tests.test_preparation_finalization import PreparationFinalizationFixture


class PreparationReviewRegressionTests(unittest.TestCase):
    """Compose the shared fixture so its ordinary tests are not re-run here."""

    def new_fixture(self):
        fixture = PreparationFinalizationFixture(methodName='runTest')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        return fixture

    def force_sandbox_failure(self, fixture, args):
        with fixture.no_provider_boundary() as tripwires:
            with mock.patch.object(
                    worker, 'resolve_os_sandbox',
                    side_effect=worker.WorkerError(78, 'fixture containment failure')):
                with self.assertRaises(worker.WorkerError) as caught:
                    worker.run(args)
        self.assertEqual(caught.exception.code, 78)
        self.assertFalse(tripwires.load_api_key.called)
        self.assertFalse(tripwires.provider_relay.called)
        self.assertFalse(tripwires.execute.called)

    def failed_use_with_saved_intent(self, fixture):
        """Create the durable intent left when rejection fails after attestation."""
        # Avoid declared-dependency refusal masking the saved-intent check: this
        # real prestart probe failure leaves the same finalizable ledger shape.
        task_id, assignment_id, copy, _row = fixture.run_preparation_failure(
            probes=['false'])
        with mock.patch.object(
                workspace_gc, '_capture',
                side_effect=workspace.WorkspaceError(
                    'fixture capture failure after finalization intent', 73)):
            with self.assertRaises((workspace.WorkspaceError,
                                    coordination.CoordinationError)):
                coordination.use_result(
                    fixture.source, task_id, assignment_id, 'rejected',
                    'fixture rejection records intent before cleanup')

        row = fixture.row(task_id, assignment_id)
        if not row.get('job_finalization_request'):
            # Callback ordering may change. A correctly identified saved intent
            # is still legitimate test setup when publication never ran.
            lease = workspace_lifecycle.read(copy)
            with coordination._lock(fixture.source):
                task = coordination.load_task(fixture.source, task_id)
                current = next(item for item in task['assignments']
                               if item['id'] == assignment_id)
                current['disposition'] = {
                    'kind': 'rejected',
                    'evidence': 'fixture rejection records intent before cleanup',
                    'at': time.time()}
                current['job_finalization_request'] = {
                    'at': time.time(),
                    'evidence': 'fixture rejection records intent before cleanup',
                    'cancelled': True,
                    'project': str(fixture.source.resolve()),
                    'task_id': task_id,
                    'assignment_id': assignment_id,
                    'workspace_id': copy.id,
                    'job_id': lease['job_id'],
                }
                coordination._atomic(
                    coordination._task_path(fixture.source, task_id), task)
        row = fixture.row(task_id, assignment_id)
        self.assertEqual(row['status'], 'failed')
        self.assertEqual(row['disposition']['kind'], 'rejected')
        self.assertTrue(row.get('job_finalization_request'))
        self.assertTrue(copy.path.exists())
        return task_id, assignment_id, copy

    def test_fresh_explicit_copy_is_associated_before_sandbox_failure(self):
        fixture = self.new_fixture()
        task, assignment_id = fixture.new_task()
        copy = fixture.copy()
        args = fixture.worker_args(task['id'], assignment_id, workspace=copy.id)

        self.force_sandbox_failure(fixture, args)

        copy = workspace.load(fixture.state, copy.id)
        row = fixture.row(task['id'], assignment_id)
        lease = workspace_lifecycle.read(copy)
        self.assertEqual(row['status'], 'failed')
        self.assertEqual(row['failure_stage'], 'preparation')
        self.assertEqual(row['workspace_id'], copy.id)
        self.assertEqual(row['job_id'], lease['job_id'])
        self.assertFalse(row.get('started_at'))
        self.assertFalse(row.get('attempt_id'))
        self.assertFalse(row.get('execution_snapshot'))
        self.assertEqual(copy.metadata.get('coord_task'), task['id'])
        self.assertEqual(copy.metadata.get('coord_assignment'), assignment_id)
        self.assertEqual(lease.get('binding'), {
            'task_id': task['id'], 'assignment_id': assignment_id})
        self.assertEqual(lease.get('attempts'), [])
        self.assertNotIn('pending_attempt', lease)

        with fixture.local_capture():
            finalized = fixture.require_success(
                coordination.use_result, fixture.source, task['id'], assignment_id,
                'rejected', 'provider-free sandbox preparation failure rejected')
        final_row = next(item for item in finalized['assignments']
                         if item['id'] == assignment_id)
        self.assertTrue(final_row.get('job_finalization'))
        self.assertFalse(copy.path.exists())
        self.assertEqual(
            coordination.complete_task(fixture.source, task['id'])['status'],
            'completed')

    def test_linked_correction_binds_same_job_before_sandbox_failure(self):
        fixture = self.new_fixture()
        task, predecessor = fixture.new_task()
        copy = fixture.copy()
        with copy.lock(recover=True):
            coordination.assignment_started(
                fixture.source, task['id'], predecessor, copy.id, 'codex', [])
            copy.begin('execution')
            (copy.path / 'app.txt').write_text('first result\n')
            copy.finish('succeeded', exit_code=0)
            coordination.assignment_finished(
                fixture.source, task['id'], predecessor, 'succeeded',
                'first result', ['app.txt'], [])
        coordination.use_result(
            fixture.source, task['id'], predecessor, 'needs-rework',
            'one linked correction is required')
        job_id = workspace_lifecycle.read(copy)['job_id']
        original_attempts = copy_module.deepcopy(
            workspace_lifecycle.read(copy)['attempts'])

        current = coordination.load_task(fixture.source, task['id'])
        deliverables = [dict(item) for item in current['deliverables']]
        deliverables.append(fixture.deliverable(
            'fix-correction',
            correction={'task_id': task['id'], 'assignment_id': predecessor}))
        planned = coordination.plan_task(fixture.source, task['id'], {
            'classification': 'substantial', 'deliverables': deliverables})
        correction = next(row for row in planned['assignments']
                          if row['deliverable_id'] == 'fix-correction')

        args = fixture.worker_args(task['id'], correction['id'])
        self.force_sandbox_failure(fixture, args)

        failed = fixture.row(task['id'], correction['id'])
        lease = workspace_lifecycle.read(copy)
        self.assertEqual(failed['status'], 'failed')
        self.assertEqual(failed['failure_stage'], 'preparation')
        self.assertFalse(failed.get('attempt_id'))
        self.assertFalse(failed.get('started_at'))
        self.assertEqual(failed['workspace_id'], copy.id)
        self.assertEqual(failed['job_id'], job_id)
        self.assertEqual(lease.get('binding'), {
            'task_id': task['id'], 'assignment_id': correction['id']})
        self.assertEqual(lease.get('attempts'), original_attempts)
        self.assertNotIn('pending_attempt', lease)

        with fixture.local_capture():
            finalized = fixture.require_success(
                coordination.use_result, fixture.source, task['id'], correction['id'],
                'rejected', 'linked correction failed before execution')
        predecessor_row = next(item for item in finalized['assignments']
                               if item['id'] == predecessor)
        correction_row = next(item for item in finalized['assignments']
                              if item['id'] == correction['id'])
        self.assertTrue(predecessor_row.get('job_finalization'))
        self.assertEqual(predecessor_row['job_finalization'],
                         correction_row['job_finalization'])
        self.assertFalse(copy.path.exists())
        self.assertEqual(
            coordination.complete_task(fixture.source, task['id'])['status'],
            'completed')

    def test_saved_finalization_intent_blocks_resume_and_direct_reservation(self):
        operations = ('worker-resume', 'assignment-preparing', 'assignment-started')
        for operation in operations:
            with self.subTest(operation=operation):
                fixture = self.new_fixture()
                task_id, assignment_id, copy = self.failed_use_with_saved_intent(fixture)
                before = fixture.snapshot(task_id, copy)
                request = fixture.row(task_id, assignment_id)['job_finalization_request']
                lease_before = workspace_lifecycle.read(copy)

                if operation == 'worker-resume':
                    args = fixture.worker_args(
                        task_id, assignment_id, workspace=copy.id,
                        resume_after_failure=True)
                    with fixture.no_provider_boundary() as tripwires:
                        with self.assertRaises(worker.WorkerError) as caught:
                            worker.run(args)
                    self.assertIn(caught.exception.code, (73, 75))
                    self.assertFalse(tripwires.load_api_key.called)
                    self.assertFalse(tripwires.provider_relay.called)
                    self.assertFalse(tripwires.execute.called)
                elif operation == 'assignment-preparing':
                    with self.assertRaises(coordination.CoordinationError) as caught:
                        coordination.assignment_preparing(
                            fixture.source, task_id, assignment_id, copy)
                    self.assertIn(caught.exception.code, (73, 75))
                else:
                    with self.assertRaises(coordination.CoordinationError) as caught:
                        coordination.assignment_started(
                            fixture.source, task_id, assignment_id, copy.id,
                            'codex', [])
                    self.assertIn(caught.exception.code, (73, 75))

                row = fixture.row(task_id, assignment_id)
                lease = workspace_lifecycle.read(copy)
                self.assertEqual(row['status'], 'failed')
                self.assertEqual(row['disposition']['kind'], 'rejected')
                self.assertEqual(row['job_finalization_request'], request)
                self.assertEqual(lease.get('attempts'), lease_before['attempts'])
                self.assertEqual(lease.get('binding'), lease_before['binding'])
                self.assertNotIn('pending_attempt', lease)
                self.assertEqual(fixture.snapshot(task_id, copy), before)

    def test_head_advance_never_executes_stale_source_and_remains_terminalizable(self):
        fixture = self.new_fixture()
        task, assignment_id = fixture.new_task()
        subprocess.run(
            ['git', '-C', str(fixture.source), '-c', 'user.name=Fixture',
             '-c', 'user.email=fixture@example.invalid', 'commit', '--allow-empty',
             '-qm', 'advance after planning'],
            check=True)

        args = fixture.worker_args(task['id'], assignment_id)
        with fixture.no_provider_boundary() as tripwires:
            with self.assertRaises(worker.WorkerError) as caught:
                worker.run(args)
        self.assertIn(caught.exception.code, (64, 73, 78))
        self.assertFalse(tripwires.load_api_key.called)
        self.assertFalse(tripwires.provider_relay.called)
        self.assertFalse(tripwires.execute.called)

        row = fixture.row(task['id'], assignment_id)
        self.assertEqual(row['status'], 'failed')
        self.assertEqual(row['failure_stage'], 'preparation')
        self.assertFalse(row.get('started_at'))
        self.assertFalse(row.get('attempt_id'))
        identifier = row.get('workspace_id')
        if identifier:
            execution_copy = workspace.load(fixture.state, identifier)
            lease = workspace_lifecycle.read(execution_copy)
            self.assertEqual(row['job_id'], lease['job_id'])
            self.assertEqual(lease.get('binding'), {
                'task_id': task['id'], 'assignment_id': assignment_id})
            self.assertNotIn('pending_attempt', lease)
            self.assertFalse(any(entry.get('operation') == 'execution'
                                 for entry in lease.get('attempts', [])))
            with fixture.local_capture():
                coordination.use_result(
                    fixture.source, task['id'], assignment_id, 'rejected',
                    'stale planned HEAD refused before execution')
            self.assertFalse(execution_copy.path.exists())
        else:
            coordination.use_result(
                fixture.source, task['id'], assignment_id, 'rejected',
                'stale planned HEAD refused before allocation')

        self.assertEqual(
            coordination.complete_task(fixture.source, task['id'])['status'],
            'completed')

    def test_stop_reconciles_crash_after_cleanup_without_second_capture(self):
        fixture = self.new_fixture()
        task_id, assignment_id, copy = fixture.finalizable_failure()
        captures = []

        def capture_once(execution_copy):
            captures.append(execution_copy.id)
            return b'fixture captured patch\n'

        real_publish = coordination._publish_job_finalization
        publications = []

        def crash_once(*arguments, **keywords):
            publications.append(1)
            if len(publications) == 1:
                raise RuntimeError('fixture crash after cleanup')
            return real_publish(*arguments, **keywords)

        with mock.patch.object(workspace_gc, '_capture', side_effect=capture_once), \
                mock.patch.object(coordination, '_publish_job_finalization',
                                  side_effect=crash_once):
            with self.assertRaises(RuntimeError):
                coordination.finalize_assignment(
                    fixture.source, task_id, assignment_id,
                    'cancel before the publication crash', cancelled=True)
        self.assertEqual(captures, [copy.id])
        self.assertFalse(copy.path.exists())
        interrupted = fixture.row(task_id, assignment_id)
        self.assertTrue(interrupted.get('job_finalization_request'))
        self.assertNotIn('job_finalization', interrupted)
        self.assertEqual(workspace_lifecycle.read(copy)['phase'], 'cancelled')

        with mock.patch.object(
                workspace_gc, '_capture',
                side_effect=AssertionError('Stop attempted a second capture')), \
                fixture.no_provider_boundary() as tripwires:
            result = coordinator_hooks.handle({
                'cwd': str(fixture.source), 'session_id': fixture.session,
                'turn_id': 'stop-after-cleanup-crash', 'hook_event_name': 'Stop',
                'stop_hook_active': False,
            }, runtime='codex')
        self.assertFalse(tripwires.load_api_key.called)
        self.assertFalse(tripwires.provider_relay.called)
        self.assertFalse(tripwires.execute.called)
        self.assertNotEqual(result.get('decision'), 'block', result)
        self.assertTrue(result.get('continue', True), result)
        self.assertNotIn('requires explicit finalize or cancel',
                         json.dumps(result).lower())
        completed = coordination.load_task(fixture.source, task_id)
        self.assertEqual(completed['status'], 'completed')
        completed_row = next(item for item in completed['assignments']
                             if item['id'] == assignment_id)
        self.assertTrue(completed_row.get('job_finalization'))
        self.assertNotIn('job_finalization_request', completed_row)
        self.assertEqual(captures, [copy.id])

    def preparation_history_fixture(self, fixture, *, execution_history):
        task, assignment_id = fixture.new_task()
        execution_copy = fixture.copy()
        workspace.prepare(execution_copy, ['true'])
        if execution_history:
            with execution_copy.lock(recover=True):
                coordination.assignment_started(
                    fixture.source, task['id'], assignment_id,
                    execution_copy.id, 'codex', [])
                execution_copy.begin('execution')
                (execution_copy.path / 'app.txt').write_text('executed source\n')
                execution_copy.finish('failed', error_kind='execution', exit_code=65)
                coordination.assignment_finished(
                    fixture.source, task['id'], assignment_id, 'failed',
                    'execution fixture failed', ['app.txt'], [],
                    error_kind='execution', exit_code=65)
        else:
            execution_copy.metadata.update(
                coord_task=task['id'], coord_assignment=assignment_id,
                standalone_job=False, status='failed', error_kind='preparation',
                exit_code=78)
            execution_copy.save()
            coordination.assignment_preparation_failed(
                fixture.source, task['id'], assignment_id,
                'legacy preparation-only failure', workspace_id=execution_copy.id)

        lease = workspace_lifecycle.read(execution_copy)
        lease.pop('binding', None)
        lease.pop('binding_history', None)
        workspace_lifecycle.write(execution_copy, lease)
        with coordination._lock(fixture.source):
            current = coordination.load_task(fixture.source, task['id'])
            row = next(item for item in current['assignments']
                       if item['id'] == assignment_id)
            row['disposition'] = {
                'kind': 'rejected', 'evidence': 'legacy job cancellation reviewed',
                'at': 1.0}
            row['job_finalization_request'] = {
                'at': 1.0, 'evidence': 'legacy cancellation intent',
                'cancelled': True, 'workspace_id': execution_copy.id}
            coordination._atomic(
                coordination._task_path(fixture.source, task['id']), current)

        attempts_before = copy_module.deepcopy(
            workspace_lifecycle.read(execution_copy)['attempts'])
        metadata_attempt = execution_copy.metadata.get('attempt_id')
        with fixture.local_capture():
            workspace_gc.finalize(
                execution_copy, reason='legacy cancellation cleanup', cancelled=True)
        self.assertFalse(execution_copy.path.exists())
        return (task['id'], assignment_id, execution_copy, attempts_before,
                metadata_attempt)

    def test_legacy_preparation_history_recovers_but_execution_history_is_rejected(self):
        for history in ('preparation', 'execution'):
            with self.subTest(history=history):
                fixture = self.new_fixture()
                (task_id, assignment_id, execution_copy, attempts_before,
                 metadata_attempt) = self.preparation_history_fixture(
                    fixture, execution_history=history == 'execution')
                snapshot = fixture.snapshot(task_id, execution_copy)

                if history == 'preparation':
                    self.assertEqual(len(attempts_before), 1)
                    self.assertEqual(attempts_before[0]['operation'], 'preparation')
                    self.assertTrue(metadata_attempt)
                    finalized = fixture.require_success(
                        coordination.finalize_assignment,
                        fixture.source, task_id, assignment_id,
                        'recover preparation-only cancellation', cancelled=True)
                    row = next(item for item in finalized['assignments']
                               if item['id'] == assignment_id)
                    self.assertTrue(row.get('job_finalization'))
                    self.assertTrue(row['job_finalization']['cancelled'])
                    lease = workspace_lifecycle.read(execution_copy)
                    self.assertEqual(lease['attempts'], attempts_before)
                    persisted = workspace.load(fixture.state, execution_copy.id)
                    self.assertEqual(persisted.metadata.get('attempt_id'),
                                     metadata_attempt)
                    self.assertEqual(persisted.metadata.get('operation'),
                                     'preparation')
                    self.assertEqual(
                        coordination.complete_task(fixture.source, task_id)['status'],
                        'completed')
                else:
                    self.assertEqual([entry.get('operation') for entry in attempts_before],
                                     ['preparation', 'execution'])
                    with self.assertRaises(coordination.CoordinationError) as caught:
                        coordination.finalize_assignment(
                            fixture.source, task_id, assignment_id,
                            'execution history is not a legacy preparation',
                            cancelled=True)
                    self.assertEqual(caught.exception.code, 73)
                    self.assertEqual(fixture.snapshot(task_id, execution_copy), snapshot)
                    self.assertNotEqual(
                        coordination.load_task(fixture.source, task_id)['status'],
                        'completed')

    def test_disposition_race_refuses_without_deleting_or_publishing(self):
        fixture = self.new_fixture()
        task_id, assignment_id, copy = fixture.finalizable_failure()
        real_finalize = workspace_gc.finalize
        observed = {}

        def interpose(*arguments, **keywords):
            coordination._use_result(
                fixture.source, task_id, assignment_id, 'rejected',
                'concurrent review changed the disposition')
            observed['snapshot'] = fixture.snapshot(task_id, copy)
            observed['lease'] = workspace_lifecycle.read(copy)
            return real_finalize(*arguments, **keywords)

        with fixture.local_capture(), mock.patch.object(
                workspace_gc, 'finalize', side_effect=interpose):
            with self.assertRaises(coordination.CoordinationError) as caught:
                coordination.finalize_assignment(
                    fixture.source, task_id, assignment_id,
                    'accept the original incorporated result')
        self.assertIn(caught.exception.code, (73, 75))
        self.assertTrue(copy.path.exists())
        self.assertEqual(workspace_lifecycle.read(copy)['phase'],
                         observed['lease']['phase'])
        self.assertNotIn(workspace_lifecycle.read(copy)['phase'],
                         workspace_lifecycle.FINAL_PHASES)
        self.assertEqual(fixture.snapshot(task_id, copy), observed['snapshot'])
        row = fixture.row(task_id, assignment_id)
        self.assertEqual(row['disposition']['kind'], 'rejected')
        self.assertNotIn('job_finalization', row)
        self.assertNotIn('job_finalization_request', row)
        self.assertNotEqual(
            coordination.load_task(fixture.source, task_id)['status'], 'completed')

    def test_missing_ledger_job_id_fails_closed_for_retained_finalized_evidence(self):
        for evidence in ('saved-request', 'retained-result'):
            with self.subTest(evidence=evidence):
                fixture = self.new_fixture()
                task_id, assignment_id, copy = fixture.historic_cleaned_failure()
                with coordination._lock(fixture.source):
                    current = coordination.load_task(fixture.source, task_id)
                    row = next(item for item in current['assignments']
                               if item['id'] == assignment_id)
                    row.pop('job_id', None)
                    if evidence == 'retained-result':
                        row.pop('job_finalization_request', None)
                    coordination._atomic(
                        coordination._task_path(fixture.source, task_id), current)
                self.assertEqual(fixture.row(task_id, assignment_id).get('workspace_id'),
                                 copy.id)
                self.assertTrue((copy.directory / 'result.json').exists())
                snapshot = fixture.snapshot(task_id, copy)
                task_before = coordination.load_task(fixture.source, task_id)

                with self.assertRaises(coordination.CoordinationError):
                    coordination.complete_task(fixture.source, task_id)

                self.assertEqual(fixture.snapshot(task_id, copy), snapshot)
                task_after = coordination.load_task(fixture.source, task_id)
                self.assertEqual(task_after['status'], task_before['status'])
                self.assertNotEqual(task_after['status'], 'completed')


if __name__ == '__main__':
    unittest.main()
