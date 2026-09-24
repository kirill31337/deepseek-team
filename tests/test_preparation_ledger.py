"""Preparation-ledger reconciliation: planned-only failure, protected results.

The managed runner calls the durable ledger API under test when preparation
(owned copy, sandbox, declared dependencies or the provider boundary) fails
before any model execution. These tests use real ledger fixtures on a throwaway
Git project and never contact a provider.
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from codex_deepseek_team import coordination, settings, worker, worker_slots, workspace
from codex_deepseek_team.routing import RoutingService


class PreparationLedgerCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='dst-preparation-')
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.root = self.base / 'repo'
        self.root.mkdir()
        (self.root / 'a.py').write_text('VALUE = 1\n')
        self._git('init', '-q')
        self._git('add', '.')
        self._git('-c', 'user.name=Tests', '-c', 'user.email=tests@example.invalid',
                  'commit', '-qm', 'base')
        env = mock.patch.dict(os.environ, {
            'DEEPSEEK_TEAM_STATE_DIR': str(self.base / 'state'),
            'XDG_CONFIG_HOME': str(self.base / 'config'),
            'HOME': str(self.base / 'home'),
        })
        env.start()
        self.addCleanup(env.stop)
        (self.base / 'home').mkdir()
        self.state = self.base / 'state'
        settings.set_values(self.root / settings.PROJECT_FILE,
                            delegation_level=75, access='full-access')

    def _git(self, *arguments):
        subprocess.run(['git', '-C', str(self.root), *arguments],
                       check=True, capture_output=True)

    # -- fixtures ----------------------------------------------------------
    def open_task(self, turn_id='1'):
        return coordination.open_task(
            self.root, session_id='sess-preparation', turn_id=turn_id,
            prompt='implement feature', policy=settings.resolve(self.root))

    def plan(self, task, *, checks=None, deliverable='impl', executor='worker', features=None):
        item = {'id': deliverable, 'kind': 'implementation', 'scope': ['a.py'],
                'executor': executor, 'acceptance': ['implemented'],
                'dependencies': [], 'checks': list(checks or [])}
        if features is not None:
            item['features'] = dict(features)
        return coordination.plan_task(self.root, task['id'], {
            'classification': 'substantial', 'deliverables': [item]})

    def raw(self, task):
        """Exact persisted ledger bytes; a no-op must not rewrite the file."""
        return coordination._task_path(self.root, task['id']).read_bytes()

    def row(self, task):
        return coordination.load_task(self.root, task['id'])['assignments'][0]

    def service(self):
        return RoutingService(self.root)

    def admission_args(self, task, assignment_id, **changes):
        args = SimpleNamespace(state_dir=self.state, timeout=0, no_wait=True,
                               max_workers=1, coord_task=task['id'],
                               coord_assignment=assignment_id,
                               resume_after_failure=False, job_deadline=None)
        for key, value in changes.items():
            setattr(args, key, value)
        return args

    def launch(self, task, assignment_id, **changes):
        return worker.acquire_job_slot(self.admission_args(task, assignment_id, **changes),
                                       policy=settings.resolve(self.root), root=self.root)


class PreparationTransitionTests(PreparationLedgerCase):
    def test_planned_assignment_fails_with_retained_workspace_and_runner_constraint(self):
        task = self.open_task()
        planned = self.plan(task, checks=['python3 -V'])
        assignment_id = planned['assignments'][0]['id']
        before = coordination.load_task(self.root, task['id'])['updated_at']
        result = coordination.assignment_preparation_failed(
            self.root, task['id'], assignment_id,
            'sandbox probe failed: bwrap is unavailable',
            workspace_id='ws-retained', runtime='codex', effort='medium')
        row = result['assignments'][0]
        self.assertEqual(row['status'], 'failed')
        self.assertEqual(row['queue_state'], 'finished')
        self.assertEqual(row['error_kind'], 'environment')
        self.assertEqual(row['failure_stage'], 'preparation')
        self.assertEqual(row['exit_code'], 78)
        self.assertEqual(row['result_summary'],
                         'sandbox probe failed: bwrap is unavailable')
        self.assertEqual(row['workspace_id'], 'ws-retained')
        self.assertEqual(row['runtime'], 'codex')
        # The legacy alias is normalized before it reaches the ledger.
        self.assertEqual(row['effort'], 'high')
        self.assertEqual(row['worker_changes'], [])
        self.assertEqual(row['checks'], [])
        self.assertIsInstance(row['finished_at'], float)
        # No model ran: no start time, routing features or quality feedback.
        self.assertIsNone(row.get('started_at'))
        self.assertNotIn('routing_features', row)
        self.assertNotIn('routing_decision_id', row)
        self.assertNotIn('routing_feedback', row)
        self.assertIsNone(row['disposition'])
        self.assertNotIn('result', result['deliverables'][0])
        self.assertNotIn('routing_feedback', result['deliverables'][0])
        self.assertEqual(result['deliverables'][0]['checks'], ['python3 -V'])
        constraint = result['constraints'][0]
        self.assertEqual(constraint['deliverable_id'], 'impl')
        self.assertEqual(constraint['code'], 'environment_incompatible')
        self.assertEqual(constraint['source'], 'runner')
        self.assertIn('bwrap is unavailable', constraint['evidence'])
        self.assertGreater(result['updated_at'], before)
        self.assertEqual(result['status'], 'planned')
        self.assertEqual(coordination.load_task(self.root, task['id']), result)
        self.assertEqual(self.service().observations(), [])
        self.assertEqual(self.service().status()['admission']['active_cooldowns'], [])
        text = coordination.summary(result)
        self.assertIn('error=environment', text)
        self.assertIn('stage=preparation', text)

    def test_failure_without_a_retained_workspace_keeps_optional_fields_unknown(self):
        task = self.open_task('no-workspace')
        planned = self.plan(task, checks=['python3 -V'])
        assignment_id = planned['assignments'][0]['id']
        result = coordination.assignment_preparation_failed(
            self.root, task['id'], assignment_id,
            'declared dependency unavailable inside the sandbox', exit_code=69)
        row = result['assignments'][0]
        self.assertEqual(row['status'], 'failed')
        self.assertEqual(row['error_kind'], 'environment')
        self.assertEqual(row['exit_code'], 69)
        self.assertEqual(row['failure_stage'], 'preparation')
        self.assertIsNone(row['workspace_id'])
        self.assertIsNone(row['runtime'])
        self.assertIsNone(row['effort'])
        self.assertIsNone(row.get('started_at'))
        self.assertEqual(row['checks'], [])
        self.assertEqual(result['constraints'][0]['evidence'],
                         'declared dependency unavailable inside the sandbox')

    def test_cancelled_preparation_closes_planned_assignment_without_environment_constraint(self):
        task = self.open_task('cancelled')
        planned = self.plan(task)
        assignment_id = planned['assignments'][0]['id']
        result = coordination.assignment_preparation_failed(
            self.root, task['id'], assignment_id,
            'worker launch cancelled before execution', exit_code=130,
            workspace_id='ws-cancelled')
        row = result['assignments'][0]
        self.assertEqual(row['status'], 'failed')
        self.assertEqual(row['error_kind'], 'cancelled')
        self.assertEqual(row['failure_stage'], 'preparation')
        self.assertEqual(row['exit_code'], 130)
        self.assertEqual(row['queue_state'], 'finished')
        self.assertEqual(row['workspace_id'], 'ws-cancelled')
        self.assertEqual(result['constraints'], [])

    def test_summary_is_capped_and_invalid_inputs_never_touch_the_ledger(self):
        task = self.open_task('capped')
        planned = self.plan(task)
        assignment_id = planned['assignments'][0]['id']
        result = coordination.assignment_preparation_failed(
            self.root, task['id'], assignment_id, 'x' * 9000)
        self.assertEqual(len(result['assignments'][0]['result_summary']), 4000)
        self.assertEqual(len(result['constraints'][0]['evidence']), 1000)
        raw = self.raw(task)
        for changes in ({'effort': 'xhigh'}, {'effort': 'auto'}, {'effort': 3},
                        {'exit_code': '78'}, {'exit_code': True}):
            with self.subTest(changes=changes):
                with self.assertRaises(coordination.CoordinationError) as caught:
                    coordination.assignment_preparation_failed(
                        self.root, task['id'], assignment_id, 'later failure', **changes)
                self.assertEqual(caught.exception.code, 64)
                self.assertEqual(self.raw(task), raw)
        with self.assertRaises(coordination.CoordinationError) as caught:
            coordination.assignment_preparation_failed(
                self.root, task['id'], 'as-missing', 'unknown assignment')
        self.assertEqual(caught.exception.code, 64)
        self.assertEqual(self.raw(task), raw)


    def test_auto_plan_preparation_failure_leaves_unstarted_routing_unknown(self):
        task = self.open_task('auto-routing')
        features = {'kind': 'implementation', 'domain': 'python', 'operation': 'fix',
                    'localization': 'known', 'coupling': 'local', 'verification': 'tests',
                    'clarity': 'clear', 'risk': 'low', 'scope_size': 'small',
                    'runtime': 'codex', 'model': 'deepseek-flash', 'effort': 'high',
                    'context_version': 'preparation-v1'}
        planned = self.plan(task, checks=['python3 -V'], executor='auto', features=features)
        self.assertEqual(planned['deliverables'][0]['executor'], 'worker')
        assignment_id = planned['assignments'][0]['id']
        decision_id = planned['deliverables'][0]['routing']['decision_id']
        service = self.service()
        decision = service.decision(decision_id)
        result = coordination.assignment_preparation_failed(
            self.root, task['id'], assignment_id,
            'sandbox probe failed before the runtime started',
            workspace_id='ws-auto', runtime='codex', effort='high')
        row = result['assignments'][0]
        self.assertEqual(row['status'], 'failed')
        self.assertEqual(row['failure_stage'], 'preparation')
        self.assertNotIn('routing_features', row)
        self.assertNotIn('routing_decision_id', row)
        # An unstarted attempt teaches the router nothing: no observation,
        # no cooldown and no rewritten decision.
        self.assertEqual(service.observations(), [])
        self.assertEqual(service.status()['admission']['active_cooldowns'], [])
        self.assertEqual(service.decision(decision_id), decision)


class PreparationIdempotencyTests(PreparationLedgerCase):
    def test_repeated_reconciliation_is_a_no_op_without_duplicate_constraints(self):
        task = self.open_task('repeat')
        planned = self.plan(task)
        assignment_id = planned['assignments'][0]['id']
        first = coordination.assignment_preparation_failed(
            self.root, task['id'], assignment_id, 'sandbox probe failed',
            workspace_id='ws-first', runtime='codex', effort='high')
        raw = self.raw(task)
        second = coordination.assignment_preparation_failed(
            self.root, task['id'], assignment_id, 'sandbox probe failed',
            workspace_id='ws-first', runtime='codex', effort='high')
        self.assertEqual(self.raw(task), raw)
        third = coordination.assignment_preparation_failed(
            self.root, task['id'], assignment_id, 'cancelled by a later launch',
            exit_code=130, workspace_id='ws-other', runtime='claude', effort='low')
        self.assertEqual(self.raw(task), raw)
        self.assertEqual(second, first)
        self.assertEqual(third, first)
        row = third['assignments'][0]
        self.assertEqual(row['result_summary'], 'sandbox probe failed')
        self.assertEqual(row['exit_code'], 78)
        self.assertEqual(row['error_kind'], 'environment')
        self.assertEqual(row['workspace_id'], 'ws-first')
        self.assertEqual(row['runtime'], 'codex')
        self.assertEqual(row['effort'], 'high')
        self.assertEqual(len(third['constraints']), 1)

    def test_runner_constraint_recorded_by_preparation_is_not_duplicated(self):
        task = self.open_task('dedup')
        planned = self.plan(task)
        assignment_id = planned['assignments'][0]['id']
        coordination.record_constraint(
            self.root, task['id'], 'impl', 'environment_incompatible',
            'workspace base HEAD does not match coordination task base HEAD')
        result = coordination.assignment_preparation_failed(
            self.root, task['id'], assignment_id,
            'Workspace source version does not match the coordination task base HEAD.')
        self.assertEqual(len(result['constraints']), 1)
        self.assertEqual(result['constraints'][0]['evidence'],
                         'workspace base HEAD does not match coordination task base HEAD')
        self.assertEqual(result['assignments'][0]['status'], 'failed')
        self.assertEqual(result['assignments'][0]['result_summary'],
                         'Workspace source version does not match the coordination task base HEAD.')


class ProtectedResultTests(PreparationLedgerCase):
    def test_running_assignment_is_not_relabelled_by_another_failed_launch(self):
        task = self.open_task('running')
        planned = self.plan(task)
        assignment_id = planned['assignments'][0]['id']
        coordination.assignment_started(self.root, task['id'], assignment_id,
                                        'ws-live', 'codex', [], effort='high')
        raw = self.raw(task)
        result = coordination.assignment_preparation_failed(
            self.root, task['id'], assignment_id,
            'another launch failed during preparation',
            workspace_id='ws-other', runtime='claude', effort='low')
        self.assertEqual(self.raw(task), raw)
        row = result['assignments'][0]
        self.assertEqual(row['status'], 'running')
        self.assertEqual(row['queue_state'], 'running')
        self.assertEqual(row['workspace_id'], 'ws-live')
        self.assertIsNone(row['error_kind'])
        self.assertIsNone(row['exit_code'])
        self.assertIsNone(row.get('failure_stage'))
        self.assertEqual(result['constraints'], [])

    def test_succeeded_assignment_keeps_result_checks_and_queue_state(self):
        task = self.open_task('succeeded')
        planned = self.plan(task, checks=['python3 -V'])
        assignment_id = planned['assignments'][0]['id']
        coordination.assignment_started(self.root, task['id'], assignment_id,
                                        'ws-done', 'codex', [], effort='high')
        coordination.assignment_finished(
            self.root, task['id'], assignment_id, 'succeeded', 'model answered',
            ['a.py'], [{'command': 'python3 -V', 'exit_code': 0}])
        coordination.use_result(self.root, task['id'], assignment_id,
                                'incorporated', 'accepted after review')
        raw = self.raw(task)
        result = coordination.assignment_preparation_failed(
            self.root, task['id'], assignment_id,
            'a later duplicate launch hit preparation trouble',
            exit_code=130, workspace_id='ws-new')
        self.assertEqual(self.raw(task), raw)
        row = result['assignments'][0]
        self.assertEqual(row['status'], 'succeeded')
        self.assertEqual(row['queue_state'], 'finished')
        self.assertEqual(row['result_summary'], 'model answered')
        self.assertEqual(row['checks'], [{'command': 'python3 -V', 'exit_code': 0}])
        self.assertEqual(row['worker_changes'], ['a.py'])
        self.assertEqual(row['disposition']['kind'], 'incorporated')
        self.assertEqual(result['constraints'], [])

    def test_failed_partial_execution_keeps_original_failure_and_disposition(self):
        task = self.open_task('partial')
        planned = self.plan(task)
        assignment_id = planned['assignments'][0]['id']
        coordination.assignment_started(self.root, task['id'], assignment_id,
                                        'ws-partial', 'codex', [], effort='high')
        coordination.assignment_finished(
            self.root, task['id'], assignment_id, 'failed',
            'declared checks failed after partial edits', ['a.py'],
            [{'command': 'python3 -m unittest', 'exit_code': 1}],
            error_kind='execution', exit_code=65)
        coordination.use_result(self.root, task['id'], assignment_id,
                                'needs-rework', 'inspect the partial diff')
        raw = self.raw(task)
        result = coordination.assignment_preparation_failed(
            self.root, task['id'], assignment_id,
            'a resume launch failed during preparation', workspace_id='ws-other')
        self.assertEqual(self.raw(task), raw)
        row = result['assignments'][0]
        self.assertEqual(row['status'], 'failed')
        self.assertEqual(row['error_kind'], 'execution')
        self.assertEqual(row['exit_code'], 65)
        self.assertEqual(row['result_summary'], 'declared checks failed after partial edits')
        self.assertEqual(row['worker_changes'], ['a.py'])
        self.assertEqual(row['checks'], [{'command': 'python3 -m unittest', 'exit_code': 1}])
        self.assertEqual(row['disposition']['kind'], 'needs-rework')
        self.assertIsNone(row.get('failure_stage'))
        self.assertEqual(result['constraints'], [])

    def test_explicit_resume_of_a_failed_attempt_is_not_overwritten(self):
        task = self.open_task('resume')
        planned = self.plan(task)
        assignment_id = planned['assignments'][0]['id']
        coordination.assignment_started(self.root, task['id'], assignment_id,
                                        'ws-1', 'codex', [], effort='high')
        coordination.assignment_finished(self.root, task['id'], assignment_id, 'failed',
                                         'first attempt failed', [], [],
                                         error_kind='execution', exit_code=65)
        coordination.use_result(self.root, task['id'], assignment_id,
                                'rejected', 'no usable result')
        coordination.assignment_started(self.root, task['id'], assignment_id,
                                        'ws-1', 'codex', [], effort='high')
        raw = self.raw(task)
        result = coordination.assignment_preparation_failed(
            self.root, task['id'], assignment_id,
            'the continuation failed during preparation',
            workspace_id='ws-1', runtime='codex', effort='high')
        self.assertEqual(self.raw(task), raw)
        row = result['assignments'][0]
        self.assertEqual(row['status'], 'running')
        self.assertEqual(len(row['attempt_history']), 1)
        self.assertEqual(row['attempt_history'][0]['status'], 'failed')
        self.assertEqual(row['attempt_history'][0]['failure_stage'], None)


class DispositionAndRecoveryTests(PreparationLedgerCase):
    def test_failed_preparation_result_is_dispositionable_then_completable(self):
        task = self.open_task('disposition')
        planned = self.plan(task)
        assignment_id = planned['assignments'][0]['id']
        coordination.assignment_preparation_failed(
            self.root, task['id'], assignment_id,
            'owned copy dependency preparation failed')
        self.assertEqual(self.service().observations(), [])
        coordination.use_result(self.root, task['id'], assignment_id, 'rejected',
                                'preparation failed; no worker result existed')
        row = coordination.load_task(self.root, task['id'])['assignments'][0]
        self.assertEqual(row['disposition']['kind'], 'rejected')
        # No model ran, so no fabricated acceptance/rejection observation.
        self.assertEqual(self.service().observations(), [])
        self.assertEqual(self.service().status()['admission']['active_cooldowns'], [])
        completed = coordination.complete_task(self.root, task['id'])
        self.assertEqual(completed['status'], 'completed')

    def test_task_can_be_replanned_after_a_dispositioned_preparation_failure(self):
        task = self.open_task('replan')
        planned = self.plan(task, checks=['python3 -V'])
        assignment_id = planned['assignments'][0]['id']
        coordination.assignment_preparation_failed(
            self.root, task['id'], assignment_id,
            'declared dependency unavailable inside the sandbox')
        coordination.use_result(self.root, task['id'], assignment_id, 'rejected',
                                'environment could not run the worker')
        replanned = self.plan(task, checks=['python3 -V'])
        self.assertEqual(replanned['status'], 'planned')
        row = replanned['assignments'][0]
        self.assertEqual(row['id'], assignment_id)
        self.assertEqual(row['status'], 'failed')
        self.assertEqual(row['failure_stage'], 'preparation')
        self.assertEqual(row['disposition']['kind'], 'rejected')
        self.assertEqual(len(replanned['constraints']), 1)
        self.assertEqual(coordination.validate_task(self.root, task['id']), [])


class AdmissionQueueIntegrityTests(PreparationLedgerCase):
    def test_retained_preparation_failure_requires_baseline_inspection(self):
        task = self.open_task('retained-preparation')
        planned = self.plan(task)
        assignment_id = planned['assignments'][0]['id']
        copy = workspace.create(self.root, self.state)
        coordination.assignment_preparation_failed(self.root, task['id'], assignment_id,
            'sandbox readiness failed', workspace_id=copy.id)
        raw = self.raw(task)
        with self.assertRaises(worker.WorkerError) as caught:
            self.launch(task, assignment_id)
        self.assertEqual(caught.exception.code, 78)
        self.assertIn('workspace show', caught.exception.message)
        self.assertIn('incomplete baseline', caught.exception.message.lower())
        self.assertEqual(self.raw(task), raw)

    def test_rejected_duplicate_launch_keeps_terminal_queue_and_result(self):
        task = self.open_task('duplicate-succeeded')
        planned = self.plan(task)
        assignment_id = planned['assignments'][0]['id']
        coordination.assignment_started(self.root, task['id'], assignment_id,
                                        'ws-1', 'codex', [], effort='high')
        coordination.assignment_finished(self.root, task['id'], assignment_id,
                                         'succeeded', 'done', ['a.py'], [])
        raw = self.raw(task)
        with self.assertRaises(worker.WorkerError) as caught:
            self.launch(task, assignment_id)
        self.assertEqual(caught.exception.code, 78)
        self.assertEqual(self.raw(task), raw)
        row = self.row(task)
        self.assertEqual(row['status'], 'succeeded')
        self.assertEqual(row['queue_state'], 'finished')

    def test_rejected_duplicate_launch_of_failed_assignment_is_actionable(self):
        task = self.open_task('duplicate-failed')
        planned = self.plan(task)
        assignment_id = planned['assignments'][0]['id']
        coordination.assignment_preparation_failed(
            self.root, task['id'], assignment_id, 'sandbox probe failed')
        raw = self.raw(task)
        with self.assertRaises(worker.WorkerError) as caught:
            self.launch(task, assignment_id)
        self.assertEqual(caught.exception.code, 78)
        message = str(caught.exception)
        self.assertIn('recorded failure', message)
        self.assertIn('disposition', message)
        self.assertNotIn('--resume-after-failure', message)
        self.assertIn('new coordination task', message)
        self.assertIn('Nothing was retried automatically', message)
        self.assertEqual(self.raw(task), raw)

    def test_rejected_duplicate_launch_leaves_admission_free_for_planned_work(self):
        finished = self.open_task('rejected-keeps-admission')
        planned = self.plan(finished)
        assignment_id = planned['assignments'][0]['id']
        coordination.assignment_started(self.root, finished['id'], assignment_id,
                                        'ws-1', 'codex', [], effort='high')
        coordination.assignment_finished(self.root, finished['id'], assignment_id,
                                         'succeeded', 'done', [], [])
        with self.assertRaises(worker.WorkerError):
            self.launch(finished, assignment_id)
        other = self.open_task('rejected-keeps-admission-next')
        other_planned = self.plan(other)
        fd = self.launch(other, other_planned['assignments'][0]['id'])
        try:
            row = self.row(other)
            self.assertEqual(row['status'], 'planned')
            self.assertEqual(row['queue_state'], 'ready')
        finally:
            os.close(fd)

    def test_rejected_launch_fails_before_any_owned_copy_is_created(self):
        task = self.open_task('no-workspace-copy')
        planned = self.plan(task)
        assignment_id = planned['assignments'][0]['id']
        coordination.assignment_started(self.root, task['id'], assignment_id,
                                        'ws-1', 'codex', [], effort='high')
        coordination.assignment_finished(self.root, task['id'], assignment_id,
                                         'failed', 'failed after execution', ['a.py'],
                                         [{'command': 'python3 -m unittest', 'exit_code': 1}])
        with mock.patch.object(workspace, 'create',
                               side_effect=AssertionError('copy created before admission')):
            with self.assertRaises(worker.WorkerError) as caught:
                self.launch(task, assignment_id)
        self.assertEqual(caught.exception.code, 78)
        self.assertFalse((self.state / 'workspaces').exists())

    def test_active_assignment_is_not_labeled_stopped_by_a_rejected_launch(self):
        task = self.open_task('duplicate-running')
        planned = self.plan(task)
        assignment_id = planned['assignments'][0]['id']
        started = coordination.assignment_started(self.root, task['id'], assignment_id,
                                                  'ws-live', 'codex', [], effort='high')
        started_at = started['assignments'][0]['started_at']
        raw = self.raw(task)
        with self.assertRaises(worker.WorkerError) as caught:
            self.launch(task, assignment_id)
        self.assertEqual(caught.exception.code, 78)
        self.assertEqual(self.raw(task), raw)
        row = self.row(task)
        self.assertEqual(row['status'], 'running')
        self.assertEqual(row['queue_state'], 'running')
        self.assertEqual(row['started_at'], started_at)

    def test_planned_assignment_still_records_waiting_blocked_cancelled_and_ready(self):
        task = self.open_task('planned-queue')
        planned = self.plan(task)
        assignment_id = planned['assignments'][0]['id']
        held = worker_slots.acquire(self.state, limit=1, wait=True)
        try:
            with self.assertRaises(worker.WorkerError) as caught:
                self.launch(task, assignment_id)
            self.assertEqual(caught.exception.code, 75)
            row = self.row(task)
            self.assertEqual(row['status'], 'planned')
            self.assertEqual(row['queue_state'], 'blocked')
            # A queued planned launch still reports waiting before cancellation.
            with self.assertRaises(worker.WorkerError) as timed_out:
                self.launch(task, assignment_id, no_wait=False, timeout=0.4)
            # The slot allocator deadline (75) and the job deadline (124) race;
            # both are genuine queue timeouts and both must stay blocked.
            self.assertIn(timed_out.exception.code, (75, 124))
            message = str(timed_out.exception).lower()
            self.assertTrue('timeout' in message or 'timed out' in message, message)
            row = self.row(task)
            self.assertEqual(row['queue_state'], 'blocked')
            self.assertIn('queued_at', row)
            with mock.patch.object(worker, 'acquire_slot', side_effect=KeyboardInterrupt), \
                    self.assertRaises(KeyboardInterrupt):
                self.launch(task, assignment_id)
            row = self.row(task)
            self.assertEqual(row['status'], 'planned')
            self.assertEqual(row['queue_state'], 'cancelled')
        finally:
            os.close(held)
        fd = self.launch(task, assignment_id)
        try:
            row = self.row(task)
            self.assertEqual(row['status'], 'planned')
            self.assertEqual(row['queue_state'], 'ready')
        finally:
            os.close(fd)


if __name__ == '__main__':
    unittest.main()
