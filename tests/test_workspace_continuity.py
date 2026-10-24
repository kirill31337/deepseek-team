"""Linked corrections reuse one logical job without caller-supplied copy ids."""
from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import shutil
from types import SimpleNamespace
import unittest
from unittest import mock

from codex_deepseek_team import (coordination, coordinator_hooks, development,
                                 preparation_guard, project, relay,
                                 runtime_preflight, settings, worker, workspace,
                                 workspace_gc, workspace_lifecycle)
from codex_deepseek_team.routing import RoutingService
from codex_deepseek_team.routing_models import RoutingError
from tests.test_workspace_lifecycle import LifecycleFixture


class WorkspaceContinuityTests(LifecycleFixture):
    def setUp(self):
        super().setUp()
        project.attach(self.source, coordinator='codex')
        settings.set_values(self.source / settings.PROJECT_FILE,
                            delegation_level=75, access='full-access')
        self.session = 'workspace-continuity'
        self._turn = 0
        self.model_calls = 0

    def deliverable(self, identifier='fix', *, scope='app.txt', correction=None):
        value = {
            'id': identifier, 'kind': 'implementation', 'scope': [scope],
            'executor': 'worker', 'acceptance': ['the correction is applied'],
            'dependencies': [], 'checks': [],
        }
        if correction is not None:
            value['correction_of'] = dict(correction)
        return value

    def new_task(self, *, deliverables=None):
        self._turn += 1
        task = coordination.open_task(
            self.source, session_id=self.session, turn_id=str(self._turn),
            prompt='fix app.txt', policy=settings.resolve(self.source))
        return coordination.plan_task(self.source, task['id'], {
            'classification': 'substantial',
            'deliverables': deliverables or [self.deliverable()],
        })

    def run_attempt(self, task_id, assignment_id, copy, status):
        with copy.lock(recover=True):
            coordination.assignment_started(
                self.source, task_id, assignment_id, copy.id, 'codex', [])
            copy.begin('execution')
            (copy.path / 'app.txt').write_text(status + '\n')
            copy.finish(status, exit_code=0 if status == 'succeeded' else 9)
            coordination.assignment_finished(
                self.source, task_id, assignment_id, status,
                status + ' attempt', ['app.txt'], [],
                exit_code=0 if status == 'succeeded' else 9)

    def reworked_job(self, status='succeeded'):
        task = self.new_task()
        assignment = task['assignments'][0]['id']
        copy = self.copy()
        self.run_attempt(task['id'], assignment, copy, status)
        coordination.use_result(
            self.source, task['id'], assignment, 'needs-rework',
            'reviewed result requires a correction')
        return coordination.load_task(self.source, task['id']), assignment, copy

    def correction_plan(self, task_id, reference, *, identifier='fix-correction',
                        scope='app.txt', include=True):
        task = coordination.load_task(self.source, task_id)
        deliverables = [dict(item) for item in task['deliverables']]
        if include:
            deliverables.append(self.deliverable(
                identifier, scope=scope, correction=reference))
        return {'classification': 'substantial', 'deliverables': deliverables}

    def args(self, task_id, assignment_id, *, workspace_id=None, resume=False):
        binary = self.root / 'disposable-codex'
        if not binary.exists():
            binary.write_text('#!/bin/sh\nexit 0\n')
            binary.chmod(0o755)
        return SimpleNamespace(
            runtime='codex', codex=str(binary), codex_explicit=True,
            claude='claude', claude_explicit=False, task='correct app.txt',
            state_dir=self.state, state_dir_requested=self.state,
            os_sandbox='required', timeout=0, attempts=1,
            attempts_explicit=False, effort='high', no_wait=True,
            workspace=workspace_id, resume_after_failure=resume,
            access=None, delegation_level=None, max_workers=None,
            recheck_environment=False, force_checks=False,
            coord_task=task_id, coord_assignment=assignment_id,
        )

    @contextlib.contextmanager
    def fake_model_boundary(self, *, exit_code=0):
        provider = SimpleNamespace(failures=[])
        provider_manager = mock.MagicMock()
        provider_manager.__enter__.return_value = provider
        selection = runtime_preflight.RuntimeSelection(
            runtime='codex', binary=str(self.root / 'disposable-codex'),
            layout=['fixture-layout'], environment={'PATH': '/usr/bin:/bin'})

        def execute(*_unused, **_ignored):
            self.model_calls += 1
            if exit_code:
                return exit_code, '', 'fixture execution failure'
            return (0, json.dumps({
                'type': 'item.completed',
                'item': {'type': 'agent_message', 'text': 'fixture result'},
            }) + '\n' + json.dumps({'type': 'turn.completed'}) + '\n', '')

        with contextlib.ExitStack() as stack:
            stack.enter_context(contextlib.chdir(self.source))
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
            stack.enter_context(mock.patch.object(
                worker, 'resolve_os_sandbox', return_value=(None, None)))
            stack.enter_context(mock.patch.object(
                runtime_preflight, 'select_runtime', return_value=selection))
            stack.enter_context(mock.patch.object(
                development, 'run_checks', return_value=[]))
            stack.enter_context(mock.patch.object(
                development, 'missing_requirements', return_value=[]))
            stack.enter_context(mock.patch.object(
                development, 'bridge_command', return_value=['fixture-bridge']))
            stack.enter_context(mock.patch.object(
                worker, 'load_api_key', return_value='fixture-provider-key'))
            stack.enter_context(mock.patch.object(
                relay, 'ProviderRelay', return_value=provider_manager))
            stack.enter_context(mock.patch.object(
                worker, 'execute', side_effect=execute))
            yield

    def run_managed(self, task_id, assignment_id, *, workspace_id=None, resume=False,
                    exit_code=0):
        args = self.args(task_id, assignment_id,
                         workspace_id=workspace_id, resume=resume)
        with self.fake_model_boundary(exit_code=exit_code):
            return worker.run(args)

    @staticmethod
    def lease_bytes(copy):
        return (copy.directory / 'lease.json').read_bytes()

    @contextlib.contextmanager
    def local_finalizer(self):
        """Exercise real cleanup while stubbing only the namespace capture bytes."""
        with mock.patch.object(workspace_gc, '_capture',
                               return_value=b'fixture captured patch\n'):
            yield

    @staticmethod
    def assignment_row(task, assignment_id):
        return next(row for row in task['assignments'] if row['id'] == assignment_id)

    def hook(self, event, **extra):
        payload = {
            'cwd': str(self.source), 'session_id': self.session,
            'turn_id': 'hook-' + event, 'hook_event_name': event,
        }
        payload.update(extra)
        result = coordinator_hooks.handle(payload, runtime='codex')
        return result.get('hookSpecificOutput', {}).get('additionalContext', '')

    def test_success_rework_compact_linked_correction_reuses_job_workspace_and_files(self):
        task = self.new_task()
        assignment = task['assignments'][0]['id']
        self.assertEqual(self.run_managed(task['id'], assignment), 0)
        original = coordination.load_task(self.source, task['id'])['assignments'][0]
        copy = workspace.load(self.state, original['workspace_id'])
        inode = copy.path.stat().st_ino
        job_id = copy.metadata['job_id']
        original_attempt = original['attempt_id']
        (copy.path / 'prepared-only.txt').write_text('retained coordinator file\n')

        coordination.use_result(
            self.source, task['id'], assignment, 'needs-rework',
            'the fixture result needs one bounded correction')
        compact = self.hook('SessionStart', source='compact')
        self.assertIn('workspace_id=' + copy.id, compact)
        self.assertIn('job_id=' + job_id, compact)
        self.assertIn('attempt_id=' + original_attempt, compact)

        reference = {'task_id': task['id'], 'assignment_id': assignment}
        plan = self.correction_plan(task['id'], reference)
        planned = coordination.plan_task(self.source, task['id'], plan)
        correction = next(row for row in planned['assignments']
                          if row['deliverable_id'] == 'fix-correction')
        self.assertEqual(correction['correction_of'], reference)
        self.assertEqual(next(item for item in planned['deliverables']
                              if item['id'] == 'fix-correction')['correction_of'],
                         reference)
        summary = coordination.summary(planned)
        self.assertIn('workspace_id=none; job_id=none; attempt_id=none', summary)
        self.assertIn('correction_of=' + task['id'] + '/' + assignment, summary)
        planned_compact = self.hook('SessionStart', source='compact')
        self.assertIn('correction_of=' + task['id'] + '/' + assignment, planned_compact)
        submitted = self.hook('UserPromptSubmit', prompt='continue the correction')
        self.assertIn('correction_of=' + task['id'] + '/' + assignment, submitted)

        before_retry = coordination._task_path(self.source, task['id']).read_bytes()
        repeated = coordination.plan_task(self.source, task['id'], plan)
        self.assertEqual(coordination._task_path(self.source, task['id']).read_bytes(),
                         before_retry)
        self.assertEqual(repeated['assignments'], planned['assignments'])

        self.assertEqual(self.run_managed(task['id'], correction['id']), 0)
        directories = [path for path in (self.state / 'workspaces').iterdir()
                       if len(path.name) == 32]
        self.assertEqual([path.name for path in directories], [copy.id])
        current = coordination.load_task(self.source, task['id'])
        corrected = next(row for row in current['assignments']
                         if row['id'] == correction['id'])
        self.assertEqual(corrected['workspace_id'], copy.id)
        self.assertEqual(corrected['job_id'], job_id)
        self.assertNotEqual(corrected['attempt_id'], original_attempt)
        self.assertEqual(corrected['execution_snapshot']['deliverable']['correction_of'],
                         reference)
        self.assertEqual(copy.path.stat().st_ino, inode)
        self.assertEqual((copy.path / 'prepared-only.txt').read_text(),
                         'retained coordinator file\n')
        lease = self.api().read(copy)
        self.assertEqual(lease['job_id'], job_id)
        self.assertEqual([attempt['id'] for attempt in lease['attempts']],
                         [original_attempt, corrected['attempt_id']])
        restored = self.hook('SessionStart', source='compact')
        self.assertIn('job_id=' + job_id, restored)
        self.assertIn('correction_of=' + task['id'] + '/' + assignment, restored)

        current = coordination.load_task(self.source, task['id'])
        changed_items = [dict(item) for item in current['deliverables']]
        next(item for item in changed_items
             if item['id'] == 'fix-correction')['correction_of'] = {
                 'task_id': task['id'], 'assignment_id': 'as-' + '0' * 16}
        changed = {'classification': 'substantial', 'deliverables': changed_items}
        before_change = coordination._task_path(self.source, task['id']).read_bytes()
        with self.assertRaises(coordination.CoordinationError) as caught:
            coordination.plan_task(self.source, task['id'], changed)
        self.assertIn('immutable', str(caught.exception).lower())
        self.assertEqual(coordination._task_path(self.source, task['id']).read_bytes(),
                         before_change)

    def test_wrong_explicit_copy_fails_before_guard_runtime_or_allocation(self):
        task, assignment, copy = self.reworked_job()
        reference = {'task_id': task['id'], 'assignment_id': assignment}
        planned = coordination.plan_task(
            self.source, task['id'], self.correction_plan(task['id'], reference))
        correction = next(row for row in planned['assignments']
                          if row['deliverable_id'] == 'fix-correction')
        other = self.copy()
        before = coordination._task_path(self.source, task['id']).read_bytes()
        original_file = (copy.path / 'app.txt').read_text()

        args = self.args(task['id'], correction['id'], workspace_id=other.id)
        with contextlib.chdir(self.source), contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()), \
                mock.patch.object(preparation_guard, 'worker_conditions',
                                  side_effect=AssertionError('guard reached')), \
                mock.patch.object(preparation_guard, 'lookup',
                                  side_effect=AssertionError('guard lookup reached')), \
                mock.patch.object(workspace, 'create',
                                  side_effect=AssertionError('workspace allocated')), \
                mock.patch.object(runtime_preflight, 'select_runtime',
                                  side_effect=AssertionError('runtime reached')), \
                mock.patch.object(worker, 'load_api_key',
                                  side_effect=AssertionError('provider reached')):
            with self.assertRaises(worker.WorkerError) as caught:
                worker.run(args)
        self.assertEqual(caught.exception.code, 64)
        self.assertIn('correction', caught.exception.message.lower())
        self.assertEqual(coordination._task_path(self.source, task['id']).read_bytes(), before)
        self.assertEqual((copy.path / 'app.txt').read_text(), original_file)
        self.assertEqual(self.api().read(copy)['job_id'], copy.metadata['job_id'])

    def test_correction_rejects_unavailable_and_mismatched_predecessors(self):
        task, assignment, copy = self.reworked_job()
        shutil.rmtree(copy.path)
        before = coordination._task_path(self.source, task['id']).read_bytes()
        with self.assertRaises(coordination.CoordinationError) as caught:
            coordination.plan_task(
                self.source, task['id'],
                self.correction_plan(task['id'], {
                    'task_id': task['id'], 'assignment_id': assignment}))
        self.assertIn('workspace', str(caught.exception).lower())
        self.assertEqual(coordination._task_path(self.source, task['id']).read_bytes(), before)

        other = self.new_task()
        mismatched = self.correction_plan(task['id'], {
            'task_id': task['id'],
            'assignment_id': other['assignments'][0]['id']})
        with self.assertRaises(coordination.CoordinationError) as caught:
            coordination.plan_task(self.source, task['id'], mismatched)
        self.assertIn('predecessor', str(caught.exception).lower())

    def test_running_linked_correction_replay_is_byte_idempotent_before_and_after_begin(self):
        task, assignment, copy = self.reworked_job()
        reference = {'task_id': task['id'], 'assignment_id': assignment}
        planned = coordination.plan_task(
            self.source, task['id'], self.correction_plan(task['id'], reference))
        correction = next(row for row in planned['assignments']
                          if row['deliverable_id'] == 'fix-correction')

        with copy.lock(recover=True):
            first = coordination.assignment_started(
                self.source, task['id'], correction['id'], copy.id, 'codex', [])
            started = self.assignment_row(first, correction['id'])
            ledger_started = coordination._task_path(self.source, task['id']).read_bytes()
            lease_started = self.lease_bytes(copy)
            attempts_started = len(self.api().read(copy)['attempts'])

            repeated = coordination.assignment_started(
                self.source, task['id'], correction['id'], copy.id, 'codex', [])
            self.assertEqual(self.assignment_row(repeated, correction['id']), started)
            self.assertEqual(
                coordination._task_path(self.source, task['id']).read_bytes(), ledger_started)
            self.assertEqual(self.lease_bytes(copy), lease_started)
            self.assertEqual(len(self.api().read(copy)['attempts']), attempts_started)

            copy.begin('execution')
            ledger_running = coordination._task_path(self.source, task['id']).read_bytes()
            lease_running = self.lease_bytes(copy)
            attempts_running = len(self.api().read(copy)['attempts'])
            replay = coordination.assignment_started(
                self.source, task['id'], correction['id'], copy.id, 'codex', [])
            self.assertEqual(self.assignment_row(replay, correction['id']), started)
            self.assertEqual(
                coordination._task_path(self.source, task['id']).read_bytes(), ledger_running)
            self.assertEqual(self.lease_bytes(copy), lease_running)
            self.assertEqual(len(self.api().read(copy)['attempts']), attempts_running)

            resumed = coordination.assignment_started(
                self.source, task['id'], correction['id'], copy.id, 'codex', [],
                resume_running=True)
            resumed_row = self.assignment_row(resumed, correction['id'])
            self.assertNotEqual(resumed_row['attempt_id'], started['attempt_id'])
            self.assertEqual(resumed_row['attempt_history'][-1]['attempt_id'],
                             started['attempt_id'])
            self.assertIn('pending_attempt', self.api().read(copy))
            copy.begin('execution')
            self.assertEqual(self.api().read(copy)['attempts'][-1]['id'],
                             resumed_row['attempt_id'])

    def test_planned_correction_recovers_reservation_after_ledger_crash_window(self):
        task, assignment, copy = self.reworked_job()
        reference = {'task_id': task['id'], 'assignment_id': assignment}
        planned = coordination.plan_task(
            self.source, task['id'], self.correction_plan(task['id'], reference))
        correction = next(row for row in planned['assignments']
                          if row['deliverable_id'] == 'fix-correction')
        ledger_before = coordination._task_path(self.source, task['id']).read_bytes()

        with copy.lock(recover=True):
            _, reserved_attempt = workspace_lifecycle.prepare_attempt(
                copy, task['id'], correction['id'])
        self.assertEqual(coordination._task_path(self.source, task['id']).read_bytes(),
                         ledger_before)

        with copy.lock(recover=True):
            recovered = coordination.assignment_started(
                self.source, task['id'], correction['id'], copy.id, 'codex', [])
        row = self.assignment_row(recovered, correction['id'])
        self.assertEqual(row['status'], 'running')
        self.assertEqual(row['attempt_id'], reserved_attempt)
        self.assertEqual(self.api().read(copy)['pending_attempt']['id'], reserved_attempt)

    def test_denied_linked_correction_start_preserves_ledger_and_lease_then_retries(self):
        task, assignment, copy = self.reworked_job()
        reference = {'task_id': task['id'], 'assignment_id': assignment}
        planned = coordination.plan_task(
            self.source, task['id'], self.correction_plan(task['id'], reference))
        correction = next(row for row in planned['assignments']
                          if row['deliverable_id'] == 'fix-correction')
        ledger_before = coordination._task_path(self.source, task['id']).read_bytes()
        settings.set_values(self.source / settings.PROJECT_FILE,
                            delegation_level=75, access='read-only')
        with copy.lock(recover=True):
            lease_before = self.lease_bytes(copy)
            with self.assertRaises(coordination.CoordinationError) as caught:
                coordination.assignment_started(
                    self.source, task['id'], correction['id'], copy.id, 'codex', [])
            self.assertEqual(caught.exception.code, 78)
            self.assertEqual(self.lease_bytes(copy), lease_before)
        self.assertEqual(coordination._task_path(self.source, task['id']).read_bytes(),
                         ledger_before)

        settings.set_values(self.source / settings.PROJECT_FILE,
                            delegation_level=75, access='full-access')
        with copy.lock(recover=True):
            retried = coordination.assignment_started(
                self.source, task['id'], correction['id'], copy.id, 'codex', [])
            self.assertEqual(self.assignment_row(retried, correction['id'])['status'], 'running')

    def test_routing_denial_preserves_lease_before_reservation_and_retries(self):
        task, assignment, copy = self.reworked_job()
        reference = {'task_id': task['id'], 'assignment_id': assignment}
        planned = coordination.plan_task(
            self.source, task['id'], self.correction_plan(task['id'], reference))
        correction = next(row for row in planned['assignments']
                          if row['deliverable_id'] == 'fix-correction')
        ledger_before = coordination._task_path(self.source, task['id']).read_bytes()
        with copy.lock(recover=True):
            lease_before = self.lease_bytes(copy)
            with mock.patch.object(RoutingService, 'predict',
                                   side_effect=RoutingError('fixture routing denied', 78)):
                with self.assertRaises(coordination.CoordinationError) as caught:
                    coordination.assignment_started(
                        self.source, task['id'], correction['id'], copy.id, 'codex', [])
            self.assertEqual(caught.exception.code, 78)
            self.assertEqual(self.lease_bytes(copy), lease_before)
        self.assertEqual(coordination._task_path(self.source, task['id']).read_bytes(),
                         ledger_before)

        with copy.lock(recover=True):
            retried = coordination.assignment_started(
                self.source, task['id'], correction['id'], copy.id, 'codex', [])
            self.assertEqual(self.assignment_row(retried, correction['id'])['status'], 'running')

    def test_routing_transaction_failure_does_not_leave_a_reserved_attempt(self):
        task, assignment, copy = self.reworked_job()
        reference = {'task_id': task['id'], 'assignment_id': assignment}
        planned = coordination.plan_task(
            self.source, task['id'], self.correction_plan(task['id'], reference))
        correction = next(row for row in planned['assignments']
                          if row['deliverable_id'] == 'fix-correction')
        ledger_before = coordination._task_path(self.source, task['id']).read_bytes()
        original_batch = RoutingService.batch

        @contextlib.contextmanager
        def failing_batch(service):
            with original_batch(service) as bound:
                yield bound
            raise RoutingError('fixture routing transaction failed', 78)

        with copy.lock(recover=True):
            lease_before = self.lease_bytes(copy)
            with mock.patch.object(RoutingService, 'batch', failing_batch):
                with self.assertRaises(coordination.CoordinationError) as caught:
                    coordination.assignment_started(
                        self.source, task['id'], correction['id'], copy.id, 'codex', [])
            self.assertEqual(caught.exception.code, 78)
            self.assertEqual(self.lease_bytes(copy), lease_before)
        self.assertEqual(coordination._task_path(self.source, task['id']).read_bytes(),
                         ledger_before)

        with copy.lock(recover=True):
            retried = coordination.assignment_started(
                self.source, task['id'], correction['id'], copy.id, 'codex', [])
            self.assertEqual(self.assignment_row(retried, correction['id'])['status'], 'running')

    def test_cross_task_finalization_closes_every_authoritative_predecessor(self):
        predecessor_task, predecessor, copy = self.reworked_job()
        reference = {'task_id': predecessor_task['id'], 'assignment_id': predecessor}
        correction_task = self.new_task(deliverables=[
            self.deliverable('fix-correction', correction=reference)])
        correction = correction_task['assignments'][0]['id']
        self.run_attempt(correction_task['id'], correction, copy, 'succeeded')
        coordination.use_result(
            self.source, correction_task['id'], correction, 'incorporated',
            'cross-task correction accepted')
        old_attempts = self.assignment_row(
            coordination.load_task(self.source, predecessor_task['id']),
            predecessor).get('attempt_history', [])

        with self.local_finalizer():
            finalized = coordination.finalize_assignment(
                self.source, correction_task['id'], correction,
                'latest cross-task correction finalized')

        correction_row = self.assignment_row(finalized, correction)
        old_task = coordination.load_task(self.source, predecessor_task['id'])
        old_row = self.assignment_row(old_task, predecessor)
        self.assertEqual(old_row['job_id'], correction_row['job_id'])
        self.assertEqual(old_row['job_finalization'], correction_row['job_finalization'])
        self.assertEqual(old_row['disposition']['kind'], 'needs-rework')
        self.assertEqual(old_row.get('attempt_history', []), old_attempts)
        self.assertFalse(copy.path.exists())
        self.assertFalse(coordination.completion_issues(old_task))
        self.assertFalse(coordination.completion_issues(finalized))
        self.assertEqual(coordination.complete_task(
            self.source, predecessor_task['id'])['status'], 'completed')
        self.assertEqual(coordination.complete_task(
            self.source, correction_task['id'])['status'], 'completed')

    def test_interrupted_cross_task_publication_is_repaired_idempotently(self):
        predecessor_task, predecessor, copy = self.reworked_job()
        reference = {'task_id': predecessor_task['id'], 'assignment_id': predecessor}
        correction_task = self.new_task(deliverables=[
            self.deliverable('fix-correction', correction=reference)])
        correction = correction_task['assignments'][0]['id']
        self.run_attempt(correction_task['id'], correction, copy, 'succeeded')
        coordination.use_result(
            self.source, correction_task['id'], correction, 'incorporated',
            'cross-task correction accepted')

        with self.local_finalizer():
            finalized = coordination.finalize_assignment(
                self.source, correction_task['id'], correction,
                'latest cross-task correction finalized')
        report = self.assignment_row(finalized, correction)['job_finalization']
        with coordination._lock(self.source):
            partial = coordination.load_task(self.source, correction_task['id'])
            self.assignment_row(partial, correction)['job_finalization'] = dict(report)
            coordination._atomic(coordination._task_path(
                self.source, correction_task['id']), partial)
            old = coordination.load_task(self.source, predecessor_task['id'])
            self.assignment_row(old, predecessor).pop('job_finalization', None)
            coordination._atomic(coordination._task_path(
                self.source, predecessor_task['id']), old)
        before_retry = coordination._task_path(self.source, predecessor_task['id']).read_bytes()

        repaired = coordination.finalize_assignment(
            self.source, correction_task['id'], correction,
            'retry publication after interruption')
        old = coordination.load_task(self.source, predecessor_task['id'])
        self.assertEqual(
            self.assignment_row(old, predecessor).get('job_finalization'), report)
        self.assertNotEqual(
            coordination._task_path(self.source, predecessor_task['id']).read_bytes(),
            before_retry)
        before = coordination._task_path(self.source, predecessor_task['id']).read_bytes()
        coordination.finalize_assignment(
            self.source, correction_task['id'], correction,
            'third idempotent publication')
        self.assertEqual(
            coordination._task_path(self.source, predecessor_task['id']).read_bytes(), before)
        self.assertEqual(self.assignment_row(repaired, correction)['job_finalization'], report)

        with coordination._lock(self.source):
            old = coordination.load_task(self.source, predecessor_task['id'])
            self.assignment_row(old, predecessor).pop('job_finalization', None)
            coordination._atomic(coordination._task_path(
                self.source, predecessor_task['id']), old)
        completed = coordination.complete_task(self.source, predecessor_task['id'])
        self.assertEqual(completed['status'], 'completed')
        self.assertEqual(
            self.assignment_row(completed, predecessor)['job_finalization'], report)
        self.assertEqual(coordination.complete_task(
            self.source, correction_task['id'])['status'], 'completed')

    def test_old_predecessor_cannot_finalize_an_active_successor(self):
        predecessor_task, predecessor, copy = self.reworked_job()
        reference = {'task_id': predecessor_task['id'], 'assignment_id': predecessor}
        correction_task = self.new_task(deliverables=[
            self.deliverable('fix-correction', correction=reference)])
        correction = correction_task['assignments'][0]['id']
        with copy.lock(recover=True):
            coordination.assignment_started(
                self.source, correction_task['id'], correction, copy.id, 'codex', [])
            copy.begin('execution')
        before = coordination._task_path(self.source, predecessor_task['id']).read_bytes()
        lease_before = self.lease_bytes(copy)

        with self.assertRaises(coordination.CoordinationError) as caught:
            coordination.finalize_assignment(
                self.source, predecessor_task['id'], predecessor,
                'old task must not close the active successor', cancelled=True)
        self.assertEqual(caught.exception.code, 64)
        self.assertEqual(coordination._task_path(self.source, predecessor_task['id']).read_bytes(),
                         before)
        self.assertEqual(self.lease_bytes(copy), lease_before)
        self.assertTrue(copy.path.exists())
        active = self.assignment_row(
            coordination.load_task(self.source, correction_task['id']), correction)
        self.assertEqual(active['status'], 'running')

    def test_preplanned_overlapping_launch_rechecks_rework_before_allocation(self):
        task = self.new_task(deliverables=[
            self.deliverable('fix'), self.deliverable('preplanned')])
        original = next(row for row in task['assignments']
                        if row['deliverable_id'] == 'fix')
        target = next(row for row in task['assignments']
                      if row['deliverable_id'] == 'preplanned')
        copy = self.copy()
        self.run_attempt(task['id'], original['id'], copy, 'succeeded')
        coordination.use_result(
            self.source, task['id'], original['id'], 'needs-rework',
            'overlapping result requires a linked correction')
        before = coordination._task_path(self.source, task['id']).read_bytes()
        args = self.args(task['id'], target['id'])

        with contextlib.chdir(self.source), contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()), \
                mock.patch.object(preparation_guard, 'worker_conditions',
                                  side_effect=AssertionError('guard reached')), \
                mock.patch.object(preparation_guard, 'lookup',
                                  side_effect=AssertionError('guard lookup reached')), \
                mock.patch.object(workspace, 'create',
                                  side_effect=AssertionError('NEW_ALLOCATION')), \
                mock.patch.object(runtime_preflight, 'select_runtime',
                                  side_effect=AssertionError('runtime reached')), \
                mock.patch.object(worker, 'load_api_key',
                                  side_effect=AssertionError('provider reached')):
            with self.assertRaises(worker.WorkerError) as caught:
                worker.run(args)
        self.assertEqual(caught.exception.code, 64)
        self.assertIn('correction_of', caught.exception.message)
        self.assertEqual(coordination._task_path(self.source, task['id']).read_bytes(), before)
        directories = [path for path in (self.state / 'workspaces').iterdir()
                       if len(path.name) == 32]
        self.assertEqual([path.name for path in directories], [copy.id])

    def test_preplanned_disjoint_launch_remains_independent(self):
        task = self.new_task(deliverables=[
            self.deliverable('fix'),
            self.deliverable('preplanned', scope='docs/other.md')])
        original = next(row for row in task['assignments']
                        if row['deliverable_id'] == 'fix')
        target = next(row for row in task['assignments']
                      if row['deliverable_id'] == 'preplanned')
        copy = self.copy()
        self.run_attempt(task['id'], original['id'], copy, 'succeeded')
        coordination.use_result(
            self.source, task['id'], original['id'], 'needs-rework',
            'unrelated scope remains unresolved')
        self.assertIsNone(coordination.validate_assignment_start(
            self.source, task['id'], target['id']))

    def test_unavailable_rework_tree_blocks_unlinked_plan_and_launch(self):
        task = self.new_task(deliverables=[
            self.deliverable('fix'), self.deliverable('preplanned')])
        original = next(row for row in task['assignments']
                        if row['deliverable_id'] == 'fix')
        target = next(row for row in task['assignments']
                      if row['deliverable_id'] == 'preplanned')
        copy = self.copy()
        self.run_attempt(task['id'], original['id'], copy, 'succeeded')
        coordination.use_result(
            self.source, task['id'], original['id'], 'needs-rework',
            'overlapping result remains unresolved')
        shutil.rmtree(copy.path)
        before = coordination._task_path(self.source, task['id']).read_bytes()

        with self.assertRaises(coordination.CoordinationError) as caught:
            coordination.plan_task(
                self.source, task['id'],
                self.correction_plan(task['id'], None, identifier='new-unlinked'))
        self.assertIn('unavailable', str(caught.exception).lower())
        self.assertEqual(coordination._task_path(self.source, task['id']).read_bytes(), before)

        args = self.args(task['id'], target['id'])
        with contextlib.chdir(self.source), contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()), \
                mock.patch.object(preparation_guard, 'worker_conditions',
                                  side_effect=AssertionError('guard reached')), \
                mock.patch.object(workspace, 'create',
                                  side_effect=AssertionError('NEW_ALLOCATION')), \
                mock.patch.object(runtime_preflight, 'select_runtime',
                                  side_effect=AssertionError('runtime reached')), \
                mock.patch.object(worker, 'load_api_key',
                                  side_effect=AssertionError('provider reached')):
            with self.assertRaises(worker.WorkerError) as launch:
                worker.run(args)
        self.assertIn('unavailable', launch.exception.message.lower())
        self.assertEqual(coordination._task_path(self.source, task['id']).read_bytes(), before)

    def test_explicitly_finalized_job_allows_independent_new_work(self):
        task, assignment, copy = self.reworked_job()
        coordination.use_result(
            self.source, task['id'], assignment, 'incorporated',
            'accepted after review')
        with self.local_finalizer():
            finalized = coordination.finalize_assignment(
                self.source, task['id'], assignment, 'job closed explicitly')
        self.assertTrue(self.assignment_row(finalized, assignment)['job_finalization'])
        before_repeat = coordination._task_path(self.source, task['id']).read_bytes()
        coordination.finalize_assignment(
            self.source, task['id'], assignment, 'repeat is idempotent')
        self.assertEqual(
            coordination._task_path(self.source, task['id']).read_bytes(), before_repeat)
        self.assertFalse(copy.path.exists())
        independent = self.correction_plan(
            task['id'], None, identifier='new-job', scope='app.txt')
        planned = coordination.plan_task(self.source, task['id'], independent)
        self.assertTrue(any(row['deliverable_id'] == 'new-job'
                            for row in planned['assignments']))

    @unittest.skipUnless(hasattr(os, 'geteuid'), 'capture requires Linux namespaces')
    def test_finalized_predecessor_is_rejected(self):
        self.require_capture()
        task, assignment, copy = self.reworked_job()
        coordination.finalize_assignment(
            self.source, task['id'], assignment, 'job cancelled after review', cancelled=True)
        self.assertFalse(copy.path.exists())
        before = coordination._task_path(self.source, task['id']).read_bytes()
        with self.assertRaises(coordination.CoordinationError) as caught:
            coordination.plan_task(
                self.source, task['id'],
                self.correction_plan(task['id'], {
                    'task_id': task['id'], 'assignment_id': assignment}))
        self.assertIn('final', str(caught.exception).lower())
        self.assertEqual(coordination._task_path(self.source, task['id']).read_bytes(), before)

    def test_unlinked_overlapping_correction_is_rejected_but_independent_work_is_allowed(self):
        task, assignment, _copy = self.reworked_job()
        before = coordination._task_path(self.source, task['id']).read_bytes()
        with self.assertRaises(coordination.CoordinationError) as caught:
            coordination.plan_task(
                self.source, task['id'],
                self.correction_plan(task['id'], None))
        self.assertIn('correction_of', str(caught.exception))
        self.assertIn(assignment, str(caught.exception))
        self.assertEqual(coordination._task_path(self.source, task['id']).read_bytes(), before)

        independent = self.correction_plan(
            task['id'], None, identifier='documentation', scope='docs/other.md')
        planned = coordination.plan_task(self.source, task['id'], independent)
        self.assertTrue(any(row['deliverable_id'] == 'documentation'
                            for row in planned['assignments']))

    def test_two_overlapping_rework_workspaces_are_ambiguous_without_a_link(self):
        task = self.new_task(deliverables=[
            self.deliverable('fix-a'), self.deliverable('fix-b')])
        for row in task['assignments']:
            copy = self.copy()
            self.run_attempt(task['id'], row['id'], copy, 'succeeded')
        for row in task['assignments']:
            coordination.use_result(
                self.source, task['id'], row['id'], 'needs-rework',
                'reviewed correction needed')
        before = coordination._task_path(self.source, task['id']).read_bytes()
        with self.assertRaises(coordination.CoordinationError) as caught:
            coordination.plan_task(
                self.source, task['id'],
                self.correction_plan(task['id'], None, identifier='fix-c'))
        message = str(caught.exception).lower()
        self.assertIn('ambiguous', message)
        self.assertIn('correction_of', message)
        self.assertEqual(coordination._task_path(self.source, task['id']).read_bytes(), before)

    def test_failed_predecessor_requires_explicit_resume_not_a_new_linked_job(self):
        task, assignment, copy = self.reworked_job(status='failed')
        before = coordination._task_path(self.source, task['id']).read_bytes()
        with self.assertRaises(coordination.CoordinationError) as caught:
            coordination.plan_task(
                self.source, task['id'],
                self.correction_plan(task['id'], {
                    'task_id': task['id'], 'assignment_id': assignment}))
        self.assertIn('explicit resume', str(caught.exception).lower())
        self.assertEqual(coordination._task_path(self.source, task['id']).read_bytes(), before)

        inode = copy.path.stat().st_ino
        job_id = copy.metadata['job_id']
        self.assertEqual(self.run_managed(
            task['id'], assignment, workspace_id=copy.id, resume=True), 0)
        current = coordination.load_task(self.source, task['id'])['assignments'][0]
        self.assertEqual(current['workspace_id'], copy.id)
        self.assertEqual(current['job_id'], job_id)
        self.assertEqual(current['attempt_history'][0]['status'], 'failed')
        self.assertEqual(copy.path.stat().st_ino, inode)
        self.assertEqual([attempt['status'] for attempt in self.api().read(copy)['attempts']],
                         ['failed', 'succeeded'])

    def test_failed_linked_correction_resumes_the_same_job_and_predecessor_lineage(self):
        task, assignment, copy = self.reworked_job()
        reference = {'task_id': task['id'], 'assignment_id': assignment}
        planned = coordination.plan_task(
            self.source, task['id'], self.correction_plan(task['id'], reference))
        correction = next(row for row in planned['assignments']
                          if row['deliverable_id'] == 'fix-correction')
        self.assertEqual(self.run_managed(
            task['id'], correction['id'], exit_code=70), 70)
        failed = next(row for row in coordination.load_task(
            self.source, task['id'])['assignments'] if row['id'] == correction['id'])
        self.assertEqual(failed['status'], 'failed')
        self.assertEqual(failed['workspace_id'], copy.id)
        failed_attempt = failed['attempt_id']
        coordination.use_result(
            self.source, task['id'], correction['id'], 'needs-rework',
            'inspect the failed correction and resume it explicitly')

        self.assertEqual(self.run_managed(
            task['id'], correction['id'], workspace_id=copy.id, resume=True), 0)
        current = next(row for row in coordination.load_task(
            self.source, task['id'])['assignments'] if row['id'] == correction['id'])
        self.assertEqual(current['workspace_id'], copy.id)
        self.assertEqual(current['job_id'], failed['job_id'])
        self.assertNotEqual(current['attempt_id'], failed_attempt)
        lease = self.api().read(copy)
        self.assertEqual(lease['binding'], {'task_id': task['id'], 'assignment_id': correction['id']})
        self.assertEqual([attempt['status'] for attempt in lease['attempts']],
                         ['succeeded', 'failed', 'succeeded'])

    def test_linked_correction_preparation_failure_resumes_without_new_copy(self):
        task, assignment, copy = self.reworked_job()
        reference = {'task_id': task['id'], 'assignment_id': assignment}
        planned = coordination.plan_task(
            self.source, task['id'], self.correction_plan(task['id'], reference))
        correction = next(row for row in planned['assignments']
                          if row['deliverable_id'] == 'fix-correction')
        inode = copy.path.stat().st_ino
        job_id = copy.metadata['job_id']
        predecessor = next(row for row in coordination.load_task(
            self.source, task['id'])['assignments'] if row['id'] == assignment)

        with self.fake_model_boundary(), mock.patch.object(
                worker, 'load_api_key',
                side_effect=worker.WorkerError(78, 'fixture credential absent')):
            with self.assertRaises(worker.WorkerError) as caught:
                worker.run(self.args(task['id'], correction['id']))
        self.assertEqual(caught.exception.code, 78)

        failed = next(row for row in coordination.load_task(
            self.source, task['id'])['assignments'] if row['id'] == correction['id'])
        self.assertEqual(failed['status'], 'failed')
        self.assertEqual(failed['workspace_id'], copy.id)
        self.assertEqual(failed['job_id'], job_id)
        self.assertIsNone(failed.get('attempt_id'))
        self.assertNotEqual(failed.get('attempt_id'), predecessor['attempt_id'])

        coordination.use_result(
            self.source, task['id'], correction['id'], 'needs-rework',
            'inspect the preparation failure and resume explicitly')
        self.assertEqual(self.run_managed(
            task['id'], correction['id'], resume=True), 0)

        current = next(row for row in coordination.load_task(
            self.source, task['id'])['assignments'] if row['id'] == correction['id'])
        self.assertEqual(current['workspace_id'], copy.id)
        self.assertEqual(current['job_id'], job_id)
        self.assertNotEqual(current['attempt_id'], predecessor['attempt_id'])
        self.assertEqual(copy.path.stat().st_ino, inode)
        directories = [path for path in (self.state / 'workspaces').iterdir()
                       if len(path.name) == 32]
        self.assertEqual([path.name for path in directories], [copy.id])
        lease = self.api().read(copy)
        self.assertEqual(lease['binding'], {
            'task_id': task['id'], 'assignment_id': correction['id']})
        self.assertEqual([attempt['status'] for attempt in lease['attempts']],
                         ['succeeded', 'succeeded'])


if __name__ == '__main__':
    unittest.main()
