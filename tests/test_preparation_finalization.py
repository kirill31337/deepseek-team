"""Preparation failures stay reviewable and finalize without invented attempts."""
from __future__ import annotations

import contextlib
import io
import os
import subprocess
from types import SimpleNamespace
import unittest
from unittest import mock

from codex_deepseek_team import (activation, cli, coordination,
                                 coordinator_hooks, project, relay,
                                 runtime_preflight, settings, toolchains, worker,
                                 workspace, workspace_gc, workspace_lifecycle)
from tests.test_workspace_lifecycle import LifecycleFixture


class PreparationFinalizationFixture(LifecycleFixture):
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
        self.session = 'preparation-finalization'
        self._turn = 0

    # -- compact fixtures -------------------------------------------------
    def disposable_runtime(self):
        binary = self.root / 'disposable-codex'
        if not binary.exists():
            binary.write_text('#!/bin/sh\nexit 0\n')
            binary.chmod(0o755)
        return str(binary)

    def deliverable(self, identifier='fix', *, dependencies=(), checks=(),
                    correction=None):
        value = {
            'id': identifier, 'kind': 'implementation', 'scope': ['app.txt'],
            'executor': 'worker', 'acceptance': ['the preparation is actionable'],
            'dependencies': list(dependencies), 'checks': list(checks),
        }
        if correction is not None:
            value['correction_of'] = dict(correction)
        return value

    def new_task(self, *, dependencies=(), checks=(), deliverables=None):
        self._turn += 1
        task = coordination.open_task(
            self.source, session_id=self.session, turn_id=str(self._turn),
            prompt='prepare a bounded change', policy=settings.resolve(self.source))
        task = coordination.plan_task(self.source, task['id'], {
            'classification': 'substantial',
            'deliverables': deliverables or [
                self.deliverable(dependencies=dependencies, checks=checks)],
        })
        return task, task['assignments'][0]['id']

    def worker_args(self, task_id, assignment_id, **changes):
        binary = self.disposable_runtime()
        args = SimpleNamespace(
            runtime='codex', codex=binary, codex_explicit=True,
            claude=binary, claude_explicit=False, task='prepare a bounded change',
            state_dir=self.state, state_dir_requested=self.state,
            os_sandbox='required', timeout=0, attempts=1,
            attempts_explicit=False, effort='high', job_deadline=None,
            max_workers=1, no_wait=True, workspace=None,
            resume_after_failure=False, access=None, delegation_level=None,
            recheck_environment=True, force_checks=False,
            coord_task=task_id, coord_assignment=assignment_id)
        for key, value in changes.items():
            setattr(args, key, value)
        return args

    @contextlib.contextmanager
    def no_provider_boundary(self):
        """Stub only the verified external namespace boundary, never lifecycle."""
        binary = self.disposable_runtime()
        selection = runtime_preflight.RuntimeSelection(
            runtime='codex', binary=binary, layout=['/usr/bin/env'],
            environment={'PATH': '/usr/bin:/bin',
                         'HOME': str(self.root / 'home'), 'LANG': 'C.UTF-8'})
        with contextlib.ExitStack() as stack:
            stack.enter_context(contextlib.chdir(self.source))
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
            stack.enter_context(mock.patch.object(
                worker, 'resolve_os_sandbox', return_value=(None, None)))
            stack.enter_context(mock.patch.object(
                runtime_preflight, 'select_runtime', return_value=selection))
            key = stack.enter_context(mock.patch.object(
                worker, 'load_api_key',
                side_effect=AssertionError('provider credential read before preparation failed')))
            provider = stack.enter_context(mock.patch.object(
                relay, 'ProviderRelay',
                side_effect=AssertionError('provider relay opened before preparation failed')))
            execute = stack.enter_context(mock.patch.object(
                worker, 'execute',
                side_effect=AssertionError('model executed before preparation failed')))
            yield SimpleNamespace(load_api_key=key, provider_relay=provider,
                                  execute=execute)

    @contextlib.contextmanager
    def local_capture(self):
        with mock.patch.object(workspace_gc, '_capture',
                               return_value=b'fixture captured patch\n'):
            yield

    def row(self, task_id, assignment_id=None):
        task = coordination.load_task(self.source, task_id)
        if assignment_id is None:
            return task['assignments'][0]
        return next(row for row in task['assignments'] if row['id'] == assignment_id)

    def assert_prestart_failure(self, task_id, assignment_id, copy,
                                *, attempts=None):
        row = self.row(task_id, assignment_id)
        lease = workspace_lifecycle.read(copy)
        self.assertEqual(row['status'], 'failed')
        self.assertEqual(row['queue_state'], 'finished')
        self.assertEqual(row['failure_stage'], 'preparation')
        self.assertEqual(row['exit_code'], 78)
        self.assertFalse(row.get('started_at'))
        self.assertFalse(row.get('attempt_id'))
        self.assertEqual(row['worker_changes'], [])
        self.assertEqual(row['checks'], [])
        self.assertEqual(row['workspace_id'], copy.id)
        self.assertEqual(row['job_id'], lease['job_id'])
        self.assertEqual(lease.get('binding'), {
            'task_id': task_id, 'assignment_id': assignment_id})
        self.assertEqual(lease.get('attempts'), [] if attempts is None else attempts)
        self.assertNotIn('pending_attempt', lease)
        self.assertEqual(copy.metadata['status'], 'failed')
        self.assertEqual(copy.metadata['error_kind'], 'preparation')
        return row

    def run_preparation_failure(self, *, dependencies=(), checks=(), probes=None,
                                verify_binding=True):
        task, assignment_id = self.new_task(
            dependencies=dependencies, checks=checks)
        args = self.worker_args(task['id'], assignment_id)
        with self.no_provider_boundary() as tripwires:
            if probes is None:
                with self.assertRaises(worker.WorkerError) as caught:
                    worker.run(args)
            else:
                with mock.patch.object(toolchains, 'probes_for', return_value=probes):
                    with self.assertRaises(worker.WorkerError) as caught:
                        worker.run(args)
        self.assertEqual(caught.exception.code, 78)
        self.assertFalse(tripwires.load_api_key.called)
        self.assertFalse(tripwires.provider_relay.called)
        self.assertFalse(tripwires.execute.called)
        copy = workspace.load(self.state, self.row(task['id'])['workspace_id'])
        row = (self.assert_prestart_failure(task['id'], assignment_id, copy)
               if verify_binding else self.row(task['id'], assignment_id))
        return task['id'], assignment_id, copy, row

    def finalizable_failure(self):
        """A prestart failure with a recorded disposition, ready to finalize."""
        task_id, assignment_id, copy, _row = self.run_preparation_failure(
            dependencies=[{'kind': 'command',
                           'value': 'dst-missing-preparation-command-7f42'}],
            verify_binding=False)
        coordination.use_result(
            self.source, task_id, assignment_id, 'incorporated',
            'inspected the provider-free preparation evidence')
        return task_id, assignment_id, copy

    def mutate_task(self, task_id, change):
        with coordination._lock(self.source):
            task = coordination.load_task(self.source, task_id)
            change(task)
            coordination._atomic(
                coordination._task_path(self.source, task_id), task)

    def workspace_files(self, copy):
        files = {}
        for name in ('record.json', 'lease.json', 'result.json',
                     'changes.patch'):
            path = copy.directory / name
            if path.exists():
                files[name] = path.read_bytes()
        return files

    def snapshot(self, task_id, *copies):
        return {
            'task': coordination._task_path(self.source, task_id).read_bytes(),
            'workspaces': {copy.id: self.workspace_files(copy) for copy in copies},
            'present': {copy.id: copy.path.exists() for copy in copies},
        }

    def assert_refusal_preserved(self, task_id, snapshots, *copies):
        self.assertEqual(self.snapshot(task_id, *copies), snapshots)
        self.assertTrue(any('finaliz' in issue for issue in
                            coordination.completion_issues(
                                coordination.load_task(self.source, task_id))))

    def require_success(self, action, *arguments, **keywords):
        try:
            return action(*arguments, **keywords)
        except coordination.CoordinationError as error:
            self.fail(f'expected successful terminal publication: {error}')

    def historic_cleaned_failure(self):
        """Inject an old null-binding terminal job after real preparation cleanup."""
        task, assignment_id = self.new_task()
        copy = self.copy()
        copy.metadata.update(coord_task=task['id'],
                             coord_assignment=assignment_id,
                             status='failed', error_kind='preparation',
                             exit_code=78)
        copy.save()
        coordination.assignment_preparation_failed(
            self.source, task['id'], assignment_id,
            'historic preparation failure with no binding',
            workspace_id=copy.id)
        before = self.row(task['id'], assignment_id)
        before_lease = workspace_lifecycle.read(copy)
        self.assertEqual(copy.metadata.get('coord_task'), task['id'])
        self.assertEqual(copy.metadata.get('coord_assignment'), assignment_id)
        self.assertEqual(before['failure_stage'], 'preparation')
        self.assertEqual(before['workspace_id'], copy.id)
        self.assertEqual(before['job_id'], before_lease['job_id'])
        self.assertFalse(before.get('started_at'))
        self.assertFalse(before.get('attempt_id'))
        self.assertNotIn('binding', before_lease)
        with coordination._lock(self.source):
            current = coordination.load_task(self.source, task['id'])
            row = next(item for item in current['assignments']
                       if item['id'] == assignment_id)
            row['disposition'] = {
                'kind': 'rejected', 'evidence': 'historic review rejected it',
                'at': 1.0}
            row['job_finalization_request'] = {
                'at': 1.0, 'evidence': 'historic cancellation intent',
                'cancelled': True, 'workspace_id': copy.id}
            coordination._atomic(
                coordination._task_path(self.source, task['id']), current)
        with self.local_capture():
            workspace_gc.finalize(copy, reason='historic cancellation',
                                  cancelled=True)
        lease = workspace_lifecycle.read(copy)
        self.assertEqual(lease['phase'], 'cancelled')
        self.assertEqual(lease['cleanup']['state'], 'cleaned')
        self.assertNotIn('binding', lease)
        self.assertEqual(lease['attempts'], [])
        self.assertFalse(copy.path.exists())
        return task['id'], assignment_id, copy

    def command(self, *arguments):
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            try:
                code = cli.main(list(arguments))
            except SystemExit as cause:
                code = cause.code
        return code, output.getvalue(), errors.getvalue()

class PreparationFinalizationTests(PreparationFinalizationFixture):
    # -- preparation failures ---------------------------------------------
    def test_missing_declared_command_fails_before_provider_or_attempt(self):
        task_id, assignment_id, _copy, _row = self.run_preparation_failure(
            dependencies=[{'kind': 'command',
                           'value': 'dst-missing-preparation-command-7f42'}])
        constraints = coordination.load_task(self.source, task_id)['constraints']
        self.assertIn('dependency_unavailable',
                      [row['code'] for row in constraints])
        self.assertFalse(self.row(task_id, assignment_id).get('execution_snapshot'))

    def test_missing_declared_check_executable_fails_before_provider_or_attempt(self):
        task_id, _assignment_id, _copy, _row = self.run_preparation_failure(
            checks=['dst-missing-check-runtime-91b2 --version'])
        constraints = coordination.load_task(self.source, task_id)['constraints']
        self.assertIn('dependency_unavailable',
                      [row['code'] for row in constraints])

    def test_failing_smoke_probe_retains_failure_without_an_attempt(self):
        task_id, _assignment_id, copy, row = self.run_preparation_failure(
            probes=['false'])
        self.assertEqual(row['verification_cause'], 'probe_failure')
        self.assertEqual(copy.metadata['verification_cause'], 'probe_failure')
        constraints = coordination.load_task(self.source, task_id)['constraints']
        self.assertIn('environment_incompatible',
                      [item['code'] for item in constraints])

    # -- terminal publication ---------------------------------------------
    def test_rejected_result_automatically_finalizes_and_permits_completion(self):
        task_id, assignment_id, copy, _row = self.run_preparation_failure(
            dependencies=[{'kind': 'command',
                           'value': 'dst-missing-preparation-command-7f42'}],
            verify_binding=False)
        with self.local_capture():
            task = self.require_success(
                coordination.use_result, self.source, task_id, assignment_id,
                'rejected',
                'provider-free preparation failure inspected and rejected')
        row = next(item for item in task['assignments']
                   if item['id'] == assignment_id)
        publication = row['job_finalization']
        self.assertEqual(publication['id'], copy.id)
        self.assertEqual(publication['job_id'], workspace_lifecycle.read(copy)['job_id'])
        self.assertNotIn('job_finalization_request', row)
        self.assertFalse(copy.path.exists())
        lease = workspace_lifecycle.read(copy)
        self.assertEqual(lease['phase'], 'cancelled')
        self.assertEqual(lease['cleanup']['state'], 'cleaned')
        self.assertEqual(lease['binding'], {
            'task_id': task_id, 'assignment_id': assignment_id})
        self.assertEqual(
            coordination.complete_task(self.source, task_id)['status'],
            'completed')

    def test_explicit_cancelled_finalize_is_byte_idempotent(self):
        task_id, assignment_id, copy = self.finalizable_failure()
        with self.local_capture():
            first = self.require_success(
                coordination.finalize_assignment, self.source, task_id,
                assignment_id, 'cancel this provider-free preparation failure',
                cancelled=True)
        publication = first['assignments'][0]['job_finalization']
        self.assertFalse(copy.path.exists())
        before = coordination._task_path(self.source, task_id).read_bytes()
        lease_before = (copy.directory / 'lease.json').read_bytes()
        for evidence in ('repeat with identical cancellation',
                         'repeat a third time with different prose'):
            repeated = coordination.finalize_assignment(
                self.source, task_id, assignment_id, evidence, cancelled=True)
            self.assertEqual(repeated['assignments'][0]['job_finalization'],
                             publication)
            self.assertEqual(
                coordination._task_path(self.source, task_id).read_bytes(),
                before)
            self.assertEqual(
                (copy.directory / 'lease.json').read_bytes(), lease_before)
        self.assertFalse(copy.path.exists())

    def test_crash_after_cleanup_replays_saved_finalization_intent(self):
        task_id, assignment_id, copy = self.finalizable_failure()
        real_publish = coordination._publish_job_finalization
        calls = []

        def crash_once(*arguments, **keywords):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError('fixture crash after cleanup')
            return real_publish(*arguments, **keywords)

        with self.local_capture(), mock.patch.object(
                coordination, '_publish_job_finalization', side_effect=crash_once):
            with self.assertRaises(RuntimeError):
                coordination.finalize_assignment(
                    self.source, task_id, assignment_id,
                    'cancel before the publication crash', cancelled=True)
        self.assertFalse(copy.path.exists())
        interrupted = coordination.load_task(self.source, task_id)
        interrupted_row = next(item for item in interrupted['assignments']
                               if item['id'] == assignment_id)
        self.assertNotIn('job_finalization', interrupted_row)
        self.assertEqual(interrupted_row['job_finalization_request']['workspace_id'],
                         copy.id)
        self.assertEqual(workspace_lifecycle.read(copy)['phase'], 'cancelled')

        with self.local_capture():
            repaired = self.require_success(
                coordination.finalize_assignment, self.source, task_id,
                assignment_id, 'repair publication from durable intent',
                cancelled=True)
        repaired_row = next(item for item in repaired['assignments']
                            if item['id'] == assignment_id)
        self.assertIn('job_finalization', repaired_row)
        self.assertNotIn('job_finalization_request', repaired_row)
        self.assertFalse(coordination.completion_issues(repaired))

    def test_legacy_null_binding_job_recovery_and_complete_reconciliation(self):
        for finish in ('finalize', 'complete'):
            with self.subTest(finish=finish):
                task_id, assignment_id, copy = self.historic_cleaned_failure()
                if finish == 'finalize':
                    task = self.require_success(
                        coordination.finalize_assignment, self.source, task_id,
                        assignment_id,
                        'explicitly recover the historic cancellation',
                        cancelled=True)
                else:
                    task = self.require_success(
                        coordination.complete_task, self.source, task_id)
                row = next(item for item in task['assignments']
                           if item['id'] == assignment_id)
                self.assertEqual(row['job_finalization']['id'], copy.id)
                self.assertNotIn('job_finalization_request', row)
                if finish == 'complete':
                    self.assertEqual(task['status'], 'completed')
                else:
                    self.assertEqual(
                        coordination.complete_task(self.source, task_id)['status'],
                        'completed')
                self.assertFalse(copy.path.exists())

    # -- public completion paths ------------------------------------------
    def test_cli_use_finalize_and_complete_share_terminal_publication(self):
        task_id, assignment_id, copy, _row = self.run_preparation_failure(
            dependencies=[{'kind': 'command',
                           'value': 'dst-missing-preparation-command-7f42'}],
            verify_binding=False)
        with self.local_capture():
            code, _out, error = self.command(
                'coordination', 'use', '--path', str(self.source),
                '--task', task_id, '--assignment', assignment_id,
                '--disposition', 'rejected',
                '--evidence', 'CLI inspected and rejected the preparation failure')
        self.assertEqual(code, 0, error)
        self.assertFalse(copy.path.exists())
        before = coordination._task_path(self.source, task_id).read_bytes()
        code, _out, error = self.command(
            'coordination', 'finalize', '--path', str(self.source),
            '--task', task_id, '--assignment', assignment_id,
            '--evidence', 'repeat the CLI cancellation', '--cancel')
        self.assertEqual(code, 0, error)
        self.assertEqual(
            coordination._task_path(self.source, task_id).read_bytes(), before)
        code, _out, error = self.command(
            'coordination', 'complete', '--path', str(self.source),
            '--task', task_id)
        self.assertEqual(code, 0, error)
        self.assertEqual(
            coordination.load_task(self.source, task_id)['status'], 'completed')

    def test_stop_completes_after_cancelled_publication(self):
        task_id, assignment_id, copy = self.finalizable_failure()
        with self.local_capture():
            self.require_success(
                coordination.finalize_assignment, self.source, task_id,
                assignment_id, 'final cancellation before Stop', cancelled=True)
        self.assertFalse(copy.path.exists())
        result = coordinator_hooks.handle({
            'cwd': str(self.source), 'session_id': self.session,
            'turn_id': 'stop-after-cancel', 'hook_event_name': 'Stop',
            'stop_hook_active': False,
        }, runtime='codex')
        self.assertNotEqual(result.get('decision'), 'block', result)
        self.assertTrue(result.get('continue', True), result)
        self.assertEqual(
            coordination.load_task(self.source, task_id)['status'], 'completed')

    # -- linked correction ------------------------------------------------
    def test_linked_correction_preparation_failure_keeps_job_and_is_cancellable(self):
        task, predecessor = self.new_task()
        copy = self.copy()
        with copy.lock(recover=True):
            coordination.assignment_started(
                self.source, task['id'], predecessor, copy.id, 'codex', [])
            copy.begin('execution')
            (copy.path / 'app.txt').write_text('first result\n')
            copy.finish('succeeded', exit_code=0)
            coordination.assignment_finished(
                self.source, task['id'], predecessor, 'succeeded',
                'first result', ['app.txt'], [])
        coordination.use_result(
            self.source, task['id'], predecessor, 'needs-rework',
            'one linked correction is required')
        job_id = workspace_lifecycle.read(copy)['job_id']
        original_attempts = [
            entry['id'] for entry in workspace_lifecycle.read(copy)['attempts']]
        current = coordination.load_task(self.source, task['id'])
        deliverables = [dict(item) for item in current['deliverables']]
        deliverables.append(self.deliverable(
            'fix-correction',
            dependencies=[{'kind': 'command',
                           'value': 'dst-missing-correction-command-6a20'}],
            correction={'task_id': task['id'], 'assignment_id': predecessor}))
        planned = coordination.plan_task(self.source, task['id'], {
            'classification': 'substantial', 'deliverables': deliverables})
        correction = next(row for row in planned['assignments']
                          if row['deliverable_id'] == 'fix-correction')

        with self.no_provider_boundary() as tripwires:
            with self.assertRaises(worker.WorkerError) as caught:
                worker.run(self.worker_args(task['id'], correction['id']))
        self.assertEqual(caught.exception.code, 78)
        self.assertFalse(tripwires.load_api_key.called)
        self.assertFalse(tripwires.provider_relay.called)
        self.assertFalse(tripwires.execute.called)

        failed = next(item for item in coordination.load_task(
            self.source, task['id'])['assignments']
            if item['id'] == correction['id'])
        lease = workspace_lifecycle.read(copy)
        self.assertEqual(failed['status'], 'failed')
        self.assertEqual(failed['failure_stage'], 'preparation')
        self.assertFalse(failed.get('attempt_id'))
        self.assertFalse(failed.get('started_at'))
        self.assertEqual(failed['workspace_id'], copy.id)
        self.assertEqual(failed['job_id'], job_id)
        self.assertEqual(lease.get('binding'), {
            'task_id': task['id'], 'assignment_id': correction['id']})
        self.assertEqual([entry['id'] for entry in lease['attempts']],
                         original_attempts)
        self.assertNotIn('pending_attempt', lease)

        with self.local_capture():
            finalized = coordination.use_result(
                self.source, task['id'], correction['id'], 'rejected',
                'linked correction preparation failed without execution')
        predecessor_row = next(item for item in finalized['assignments']
                               if item['id'] == predecessor)
        correction_row = next(item for item in finalized['assignments']
                              if item['id'] == correction['id'])
        self.assertEqual(predecessor_row['job_finalization'],
                         correction_row['job_finalization'])
        self.assertFalse(copy.path.exists())

    # -- fail-closed ownership and identity boundaries --------------------
    def test_refuses_foreign_live_unknown_owner_and_held_owner_lock(self):
        task_id, assignment_id, copy = self.finalizable_failure()
        process = subprocess.Popen(['sleep', '30'])
        try:
            foreign = workspace_lifecycle.process_identity(process.pid)
            self.assertIsNotNone(foreign)
            lease = workspace_lifecycle.read(copy)
            lease['owner'] = foreign
            workspace_lifecycle.write(copy, lease)
            snapshots = self.snapshot(task_id, copy)
            with self.assertRaises(coordination.CoordinationError) as caught:
                coordination.finalize_assignment(
                    self.source, task_id, assignment_id,
                    'foreign live owner must not finalize', cancelled=True)
            self.assertEqual(caught.exception.code, 75)
            self.assert_refusal_preserved(task_id, snapshots, copy)
        finally:
            process.terminate()
            process.wait(timeout=10)

        task_id, assignment_id, copy = self.finalizable_failure()
        snapshots = self.snapshot(task_id, copy)
        with mock.patch.object(workspace_lifecycle, 'identity_state',
                               return_value='unknown'):
            with self.assertRaises(coordination.CoordinationError) as caught:
                coordination.finalize_assignment(
                    self.source, task_id, assignment_id,
                    'unknown owner namespace must not finalize', cancelled=True)
        self.assertEqual(caught.exception.code, 75)
        self.assert_refusal_preserved(task_id, snapshots, copy)

        task_id, assignment_id, copy = self.finalizable_failure()
        with copy.lock(recover=True):
            snapshots = self.snapshot(task_id, copy)
            with self.assertRaises(coordination.CoordinationError) as caught:
                coordination.finalize_assignment(
                    self.source, task_id, assignment_id,
                    'held owner lock must not finalize', cancelled=True)
            self.assertEqual(caught.exception.code, 75)
            self.assert_refusal_preserved(task_id, snapshots, copy)

    def test_refuses_foreign_uid_inside_retained_execution_tree(self):
        task_id, assignment_id, copy = self.finalizable_failure()
        snapshots = self.snapshot(task_id, copy)
        # This runtime maps only UID 0, so it cannot create a genuinely foreign
        # owner. Supply the exact ownership failure emitted by real traversal.
        with mock.patch.object(
                workspace_gc, '_tree_usage',
                side_effect=workspace.WorkspaceError(
                    'Foreign owner in execution data; copy preserved.', 73)):
            with self.assertRaises(coordination.CoordinationError) as caught:
                coordination.finalize_assignment(
                    self.source, task_id, assignment_id,
                    'foreign execution owner must preserve the tree',
                    cancelled=True)
        self.assertEqual(caught.exception.code, 73)
        self.assert_refusal_preserved(task_id, snapshots, copy)

    def test_refuses_conflicting_binding_attempt_and_history(self):
        task_id, assignment_id, copy = self.finalizable_failure()
        lease = workspace_lifecycle.read(copy)
        lease['binding'] = {
            'task_id': task_id, 'assignment_id': 'as-' + '0' * 16}
        workspace_lifecycle.write(copy, lease)
        snapshots = self.snapshot(task_id, copy)
        with self.assertRaises(coordination.CoordinationError):
            coordination.finalize_assignment(
                self.source, task_id, assignment_id,
                'conflicting binding must fail closed', cancelled=True)
        self.assert_refusal_preserved(task_id, snapshots, copy)

        task_id, assignment_id, copy = self.finalizable_failure()

        def conflicting_attempt(task):
            row = next(item for item in task['assignments']
                       if item['id'] == assignment_id)
            row['attempt_id'] = 'a' * 32

        self.mutate_task(task_id, conflicting_attempt)
        snapshots = self.snapshot(task_id, copy)
        with self.assertRaises(coordination.CoordinationError):
            coordination.finalize_assignment(
                self.source, task_id, assignment_id,
                'unknown attempt must fail closed', cancelled=True)
        self.assert_refusal_preserved(task_id, snapshots, copy)

        task_id, assignment_id, copy = self.finalizable_failure()

        def conflicting_history(task):
            row = next(item for item in task['assignments']
                       if item['id'] == assignment_id)
            row['attempt_history'] = [{
                'attempt_id': 'b' * 32, 'workspace_id': copy.id,
                'status': 'succeeded'}]

        self.mutate_task(task_id, conflicting_history)
        snapshots = self.snapshot(task_id, copy)
        with self.assertRaises(coordination.CoordinationError):
            coordination.finalize_assignment(
                self.source, task_id, assignment_id,
                'conflicting attempt history must fail closed', cancelled=True)
        self.assert_refusal_preserved(task_id, snapshots, copy)

    def test_refuses_project_job_workspace_assignment_and_attempt_mismatches(self):
        cases = []

        def task_project(task):
            task['project'] = str(self.root / 'other-project')

        cases.append(('task-project', task_project, None))

        def source_project(copy):
            copy.metadata['source'] = str(self.root / 'other-project')
            copy.save()

        cases.append(('source-project', None, source_project))

        def job_identity(task):
            task['assignments'][0]['job_id'] = 'c' * 32

        cases.append(('job-id', job_identity, None))

        def record_job_identity(copy):
            copy.metadata['job_id'] = 'd' * 32
            copy.save()

        cases.append(('record-job-id', None, record_job_identity))

        for name, task_change, copy_change in cases:
            with self.subTest(name=name):
                task_id, assignment_id, copy = self.finalizable_failure()
                if task_change is not None:
                    self.mutate_task(task_id, task_change)
                if copy_change is not None:
                    copy_change(copy)
                snapshots = self.snapshot(task_id, copy)
                with self.assertRaises(coordination.CoordinationError):
                    coordination.finalize_assignment(
                        self.source, task_id, assignment_id,
                        name + ' must fail closed', cancelled=True)
                self.assert_refusal_preserved(task_id, snapshots, copy)

        task_id, assignment_id, copy = self.finalizable_failure()
        snapshots = self.snapshot(task_id, copy)
        with self.assertRaises(coordination.CoordinationError):
            coordination.finalize_assignment(
                self.source, task_id, 'as-' + '0' * 16,
                'unknown assignment must fail closed', cancelled=True)
        self.assert_refusal_preserved(task_id, snapshots, copy)

        task_id, assignment_id, copy = self.finalizable_failure()
        other_task, _other_assignment = self.new_task()
        other_snapshot = self.snapshot(other_task['id'])
        snapshots = self.snapshot(task_id, copy)
        with self.assertRaises(coordination.CoordinationError):
            coordination.finalize_assignment(
                self.source, other_task['id'], assignment_id,
                'caller task identity must fail closed', cancelled=True)
        self.assert_refusal_preserved(task_id, snapshots, copy)
        self.assertEqual(self.snapshot(other_task['id']), other_snapshot)

        task_id, assignment_id, copy = self.finalizable_failure()
        other = self.copy()
        self.mutate_task(task_id, lambda task: task['assignments'][0].update(
            workspace_id=other.id))
        snapshots = self.snapshot(task_id, copy, other)
        with self.assertRaises(coordination.CoordinationError):
            coordination.finalize_assignment(
                self.source, task_id, assignment_id,
                'different workspace must fail closed', cancelled=True)
        self.assert_refusal_preserved(task_id, snapshots, copy, other)

    def test_refuses_missing_evidence_and_replacement_work_tree(self):
        task_id, assignment_id, copy = self.finalizable_failure()
        snapshots = self.snapshot(task_id, copy)
        with self.assertRaises(coordination.CoordinationError) as caught:
            coordination.finalize_assignment(
                self.source, task_id, assignment_id, '  ', cancelled=True)
        self.assertEqual(caught.exception.code, 64)
        self.assert_refusal_preserved(task_id, snapshots, copy)

        saved = copy.directory / 'saved-work'
        copy.path.rename(saved)
        copy.path.mkdir(mode=0o700)
        try:
            snapshots = self.snapshot(task_id, copy)
            with self.assertRaises(coordination.CoordinationError) as caught:
                coordination.finalize_assignment(
                    self.source, task_id, assignment_id,
                    'replacement execution tree must be preserved',
                    cancelled=True)
            self.assertEqual(caught.exception.code, 73)
            self.assert_refusal_preserved(task_id, snapshots, copy)
            self.assertTrue(copy.path.exists())
        finally:
            copy.path.rmdir()
            saved.rename(copy.path)


if __name__ == '__main__':
    unittest.main()
