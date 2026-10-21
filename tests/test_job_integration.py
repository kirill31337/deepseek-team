"""Actual workspace/ledger consumers share job identity through final review."""
import json
import os
from types import SimpleNamespace
from unittest import mock

from codex_deepseek_team import coordination, coordinator_hooks, project, settings, workspace
from codex_deepseek_team import development, managed, runtime_preflight, worker
from tests.test_workspace_lifecycle import LifecycleFixture


class JobIntegrationTests(LifecycleFixture):
    def dead_owner(self, copy):
        row = self.api().read(copy)
        row['owner'] = {'pid': 999999999, 'start_time': '0', 'boot_id': 'old-boot', 'uid': os.geteuid()}
        self.api().write(copy, row)

    def test_reserved_running_retry_and_reviewed_result_are_not_orphans(self):
        for decision in ('needs-rework', 'incorporated'):
            with self.subTest(decision=decision):
                task, assignment = self.task()
                copy = self.copy()
                self.run_attempt(task, assignment, copy, 'failed')
                coordination.use_result(self.source, task['id'], assignment, decision, 'reviewed')
                if decision == 'needs-rework':
                    with copy.lock(recover=True):
                        coordination.assignment_started(self.source, task['id'], assignment, copy.id, 'codex', [])
                self.dead_owner(copy)
                report = self.api('workspace_gc').collect(self.state)
                row = next(r for r in report['workspaces'] if r['id'] == copy.id)
                self.assertEqual(row['action'], 'kept', row)

    def test_final_disposition_and_finalization_evidence_are_frozen(self):
        self.require_capture()
        task, assignment = self.task()
        copy = self.copy()
        self.run_attempt(task, assignment, copy, 'succeeded')
        coordination.use_result(self.source, task['id'], assignment, 'incorporated', 'accepted')
        final = coordination.finalize_assignment(self.source, task['id'], assignment, 'final evidence')
        original = final['assignments'][0]
        with self.assertRaises(coordination.CoordinationError):
            coordination.use_result(self.source, task['id'], assignment, 'rejected', 'late change')
        repeated = coordination.finalize_assignment(self.source, task['id'], assignment, 'different evidence')
        self.assertEqual(repeated['assignments'][0]['job_finalization'], original['job_finalization'])
        updated = coordination.use_result(self.source, task['id'], assignment, 'incorporated',
            'quality reassessed', quality={'grade':'met','attribution':'worker','evidence':'verified again'})
        self.assertEqual(updated['assignments'][0]['disposition'], original['disposition'])

    def test_prestart_failure_has_job_identity_and_requires_finalization(self):
        task, assignment = self.task()
        copy = self.copy()
        copy.metadata.update(coord_task=task['id'], coord_assignment=assignment)
        copy.save()
        with copy.lock():
            copy.failed('preparation', 78)
        coordination.assignment_preparation_failed(self.source, task['id'], assignment,
            'preparation unavailable', exit_code=78, workspace_id=copy.id)
        coordination.use_result(self.source, task['id'], assignment, 'incorporated', 'failure inspected')
        current = coordination.load_task(self.source, task['id'])
        self.assertEqual(current['assignments'][0].get('job_id'), self.api().read(copy)['job_id'])
        self.assertTrue(coordination.completion_issues(current))

    def test_preparation_refuses_reserved_execution_without_corrupting_attempts(self):
        task, assignment = self.task()
        copy = self.copy()
        with copy.lock():
            coordination.assignment_started(self.source, task['id'], assignment, copy.id, 'codex', [])
        before = self.api().read(copy)
        with self.assertRaises(workspace.WorkspaceError):
            workspace.prepare(copy, ['true'])
        after = self.api().read(copy)
        self.assertEqual(after['attempts'], before['attempts'])
        self.assertEqual(after['pending_attempt'], before['pending_attempt'])

    def test_preparation_home_setup_error_records_terminal_attempt(self):
        copy = self.copy()
        (copy.directory / 'preparation-home').write_text('unsafe regular file')
        with self.assertRaises(workspace.WorkspaceError):
            workspace.prepare(copy, ['true'])
        self.assertEqual(self.api().read(copy)['phase'], 'review')
        self.assertEqual(self.api().read(copy)['attempts'][-1]['status'], 'failed')
        self.assertEqual(workspace.load(self.state, copy.id).metadata['status'], 'failed')

    def test_explicit_rejection_finalizes_cancel_without_a_separate_final_flag(self):
        self.require_capture()
        task, assignment = self.task()
        copy = self.copy()
        self.run_attempt(task, assignment, copy, 'failed')
        result = coordination.use_result(self.source, task['id'], assignment, 'rejected', 'partial work rejected')
        self.assertFalse(copy.path.exists())
        self.assertEqual(result['assignments'][0]['job_finalization']['action'], 'cleaned')
        self.assertEqual(self.api().read(copy)['phase'], 'cancelled')

    def test_three_process_attempts_with_linked_correction_keep_one_inode_until_final(self):
        self.require_capture()
        task, assignment = self.task()
        copy = self.copy()
        inode = copy.path.stat().st_ino
        self.run_attempt(task, assignment, copy, 'failed')
        coordination.use_result(self.source, task['id'], assignment, 'needs-rework', 'first failure reviewed')
        self.run_attempt(task, assignment, copy, 'succeeded')
        coordination.use_result(self.source, task['id'], assignment, 'needs-rework', 'second review requires correction')
        task = coordination.plan_task(self.source, task['id'], {
            'classification': 'substantial', 'deliverables': [
                {'id': name, 'kind': 'implementation', 'scope': ['app.txt'],
                 'executor': 'worker', 'acceptance': ['fixed'],
                 'dependencies': [], 'checks': ['python3 -V'],
                 **({'correction_of': {'task_id': task['id'], 'assignment_id': assignment}}
                    if name == 'correction' else {})}
                for name in ('fix', 'correction')]})
        correction = task['assignments'][-1]['id']
        self.run_attempt(task, correction, copy, 'succeeded')
        self.assertEqual(copy.path.stat().st_ino, inode)
        lease = self.api().read(copy)
        self.assertEqual([r['status'] for r in lease['attempts']], ['failed', 'succeeded', 'succeeded'])
        self.assertEqual(len({r['id'] for r in lease['attempts']}), 3)
        coordination.use_result(self.source, task['id'], correction, 'incorporated', 'final delta accepted')
        coordination.finalize_assignment(self.source, task['id'], correction, 'no further corrections')
        self.assertFalse(copy.path.exists())
        self.assertFalse(coordination.completion_issues(coordination.load_task(self.source, task['id'])))
        self.assertEqual(self.api().read(copy)['work_inode'], inode)
        before = (copy.directory / 'lease.json').read_bytes()
        reassessed = coordination.use_result(self.source, task['id'], assignment, 'needs-rework',
            'append historic quality only', quality={'grade':'major_gaps','attribution':'worker',
            'evidence':'earlier failed attempt remains a rework case'})
        self.assertEqual(reassessed['assignments'][0]['disposition']['kind'], 'needs-rework')
        self.assertEqual((copy.directory / 'lease.json').read_bytes(), before)

    def test_completion_requires_current_job_finalization_not_only_disposition(self):
        self.require_capture()
        task, assignment = self.task()
        copy = self.copy()
        self.run_attempt(task, assignment, copy, 'succeeded')
        coordination.use_result(self.source, task['id'], assignment, 'incorporated', 'accepted')
        pending = coordination.load_task(self.source, task['id'])
        self.assertTrue(any('finaliz' in issue for issue in coordination.completion_issues(pending)))
        coordination.finalize_assignment(self.source, task['id'], assignment, 'final accepted')
        self.assertFalse(coordination.completion_issues(coordination.load_task(self.source, task['id'])))

    def test_running_replay_keeps_attempt_but_explicit_process_resume_gets_new_identity(self):
        task, assignment = self.task()
        copy = self.copy()
        with copy.lock():
            coordination.assignment_started(self.source, task['id'], assignment, copy.id, 'codex', [])
            copy.begin('execution')
            original = copy.metadata['attempt_id']
            coordination.assignment_started(self.source, task['id'], assignment, copy.id, 'codex', [])
            self.assertNotIn('pending_attempt', self.api().read(copy))
            coordination.assignment_started(self.source, task['id'], assignment, copy.id, 'codex', [],
                                            resume_running=True)
            copy.begin('execution')
            row = coordination.load_task(self.source, task['id'])['assignments'][0]
            self.assertNotEqual(row['attempt_id'], original)
            self.assertEqual(row['attempt_id'], copy.metadata['attempt_id'])
            self.assertEqual(row['attempt_history'][0]['attempt_id'], original)
            self.assertEqual(self.api().read(copy)['attempts'][0]['status'], 'interrupted')

    def test_failure_before_begin_closes_reserved_attempt_and_retry_gets_new_id(self):
        task, assignment = self.task()
        copy = self.copy()
        with copy.lock():
            coordination.assignment_started(self.source, task['id'], assignment, copy.id, 'codex', [])
            original = coordination.load_task(self.source, task['id'])['assignments'][0]['attempt_id']
            copy.failed('environment', 78)
            lease = self.api().read(copy)
            self.assertNotIn('pending_attempt', lease)
            self.assertEqual(lease['attempts'][-1]['id'], original)
            self.assertEqual(lease['attempts'][-1]['exit_code'], 78)

    def task(self):
        self._task_number = getattr(self, '_task_number', 0) + 1
        settings.set_values(self.source / settings.PROJECT_FILE,
                            delegation_level=75, access='full-access')
        task = coordination.open_task(self.source, session_id='job-session', turn_id=str(self._task_number),
                                      prompt='fix app', policy=settings.resolve(self.source))
        task = coordination.plan_task(self.source, task['id'], {
            'classification': 'substantial', 'deliverables': [{
                'id': 'fix', 'kind': 'implementation', 'scope': ['app.txt'],
                'executor': 'worker', 'acceptance': ['fixed'],
                'dependencies': [], 'checks': ['python3 -V']} ]})
        return task, task['assignments'][0]['id']

    def run_attempt(self, task, assignment, copy, status):
        with copy.lock(recover=True):
            coordination.assignment_started(self.source, task['id'], assignment,
                                            copy.id, 'codex', [])
            copy.begin('execution')
            (copy.path / 'app.txt').write_text(status + '\n')
            copy.finish(status, exit_code=0 if status == 'succeeded' else 9)
            coordination.assignment_finished(self.source, task['id'], assignment,
                status, 'attempt result', ['app.txt'], [], exit_code=copy.metadata['exit_code'])

    def test_create_and_begin_automatically_register_lease_and_attempt(self):
        lifecycle = self.api()
        copy = self.copy()
        self.assertIsNotNone(lifecycle.read(copy), 'create must lease before returning')
        with copy.lock():
            copy.begin('execution')
            self.assertEqual(len(lifecycle.read(copy)['attempts']), 1)
            self.assertEqual(copy.metadata['job_id'], lifecycle.read(copy)['job_id'])
            self.assertEqual(copy.metadata['attempt_id'], lifecycle.read(copy)['attempts'][0]['id'])
            copy.finish('failed', exit_code=9)
        self.assertEqual(lifecycle.read(copy)['phase'], 'review')

    def test_final_accepted_assignment_cleans_before_parent_task_ends(self):
        self.require_capture()
        lifecycle = self.api()
        task, assignment = self.task()
        copy = self.copy()
        inode = copy.path.stat().st_ino
        self.run_attempt(task, assignment, copy, 'failed')
        coordination.use_result(self.source, task['id'], assignment, 'needs-rework', 'inspect failed run')
        self.assertEqual(lifecycle.read(copy)['phase'], 'retry')
        self.run_attempt(task, assignment, copy, 'succeeded')
        coordination.use_result(self.source, task['id'], assignment, 'incorporated', 'reviewed final diff')
        self.assertTrue(copy.path.exists(), 'ordinary quality feedback remains repeatable until final')
        self.assertTrue(hasattr(coordination, 'finalize_assignment'), 'explicit assignment finalizer missing')
        final = coordination.finalize_assignment(self.source, task['id'], assignment,
                                                 'final integration accepted')
        self.assertFalse(copy.path.exists())
        self.assertNotEqual(final['status'], 'completed')
        self.assertEqual(copy.metadata['work_inode'], inode)
        row = final['assignments'][0]
        self.assertEqual(row['attempt_history'][0]['status'], 'failed')
        self.assertEqual(row['attempt_history'][0]['exit_code'], 9)
        self.assertEqual(row['workspace_id'], copy.id)
        self.assertEqual(row['job_finalization']['action'], 'cleaned')
        self.assertIsNotNone(coordination._changes_patch_reference(copy.id))
        inspected = workspace.load(self.state, copy.id)
        self.assertEqual(inspected.metadata['status'], 'succeeded')
        with self.assertRaises(workspace.WorkspaceError):
            with inspected.lock(recover=True):
                self.fail('finalized execution was reopened')

    def test_failed_continuation_cannot_switch_workspace(self):
        task, assignment = self.task()
        copy = self.copy()
        self.run_attempt(task, assignment, copy, 'failed')
        coordination.use_result(self.source, task['id'], assignment, 'needs-rework', 'failed check inspected')
        different = self.copy()
        with self.assertRaises(coordination.CoordinationError):
            coordination.assignment_started(self.source, task['id'], assignment,
                                            different.id, 'codex', [])
        self.assertEqual(coordination.load_task(self.source, task['id'])['assignments'][0]['workspace_id'], copy.id)

    def test_finalizer_refuses_review_that_still_needs_rework(self):
        task, assignment = self.task()
        copy = self.copy()
        self.run_attempt(task, assignment, copy, 'succeeded')
        coordination.use_result(self.source, task['id'], assignment, 'needs-rework', 'another fix required')
        self.assertTrue(hasattr(coordination, 'finalize_assignment'), 'explicit assignment finalizer missing')
        with self.assertRaises(coordination.CoordinationError):
            coordination.finalize_assignment(self.source, task['id'], assignment, 'premature acceptance')
        self.assertTrue(copy.path.exists())

    def test_actual_attempt_identity_matches_ledger_and_prior_attempt_stays_shared(self):
        task, assignment = self.task()
        copy = self.copy()
        self.run_attempt(task, assignment, copy, 'failed')
        row = coordination.load_task(self.source, task['id'])['assignments'][0]
        self.assertIn('job_id', row, 'ledger lacks logical job identity')
        self.assertEqual(row['job_id'], copy.metadata['job_id'])
        self.assertEqual(row['attempt_id'], copy.metadata['attempt_id'])
        first_id = row['attempt_id']
        coordination.use_result(self.source, task['id'], assignment, 'needs-rework', 'retry inspected')
        self.run_attempt(task, assignment, copy, 'succeeded')
        row = coordination.load_task(self.source, task['id'])['assignments'][0]
        self.assertEqual(row['job_id'], copy.metadata['job_id'])
        self.assertEqual(row['attempt_id'], copy.metadata['attempt_id'])
        self.assertNotEqual(row['attempt_id'], first_id)
        self.assertEqual(row['attempt_history'][0]['attempt_id'], first_id)

    def test_failed_runner_resume_without_workspace_option_reuses_recorded_copy(self):
        task, assignment = self.task()
        copy = self.copy()
        self.run_attempt(task, assignment, copy, 'failed')
        coordination.use_result(self.source, task['id'], assignment, 'needs-rework', 'partial run inspected')
        inode = copy.path.stat().st_ino
        args = SimpleNamespace(os_sandbox='required', task='correct this job', attempts=1,
            attempts_explicit=False, effort='high', state_dir=self.state, timeout=0,
            no_wait=True, max_workers=8, coord_task=task['id'], coord_assignment=assignment,
            resume_after_failure=True, runtime='codex', codex='codex', claude='claude')
        old = os.getcwd()
        try:
            os.chdir(self.source)
            with mock.patch.object(runtime_preflight, 'select_runtime',
                    side_effect=development.DevelopmentError('fixture runtime probe unavailable', 78)):
                with self.assertRaises(worker.WorkerError):
                    managed.run(args, settings.resolve(self.source), worker)
        finally:
            os.chdir(old)
        self.assertEqual(len(list((self.state / 'workspaces').iterdir())), 1,
                         'explicit continuation allocated a second checkout')
        self.assertEqual(copy.path.stat().st_ino, inode)
        self.assertEqual((copy.path / 'app.txt').read_text(), 'failed\n')

    def test_rework_after_finalization_does_not_mutate_persistent_feedback(self):
        self.require_capture()
        task, assignment = self.task()
        copy = self.copy()
        self.run_attempt(task, assignment, copy, 'succeeded')
        coordination.use_result(self.source, task['id'], assignment, 'incorporated', 'accepted')
        coordination.finalize_assignment(self.source, task['id'], assignment, 'final accepted')
        before = coordination._task_path(self.source, task['id']).read_bytes()
        with self.assertRaises(coordination.CoordinationError):
            coordination.use_result(self.source, task['id'], assignment, 'needs-rework', 'too late')
        self.assertEqual(coordination._task_path(self.source, task['id']).read_bytes(), before)

    def test_abandon_finalizes_a_stopped_job_without_waiting_for_parent(self):
        self.require_capture()
        task, assignment = self.task()
        copy = self.copy()
        with copy.lock():
            coordination.assignment_started(self.source, task['id'], assignment,
                                            copy.id, 'codex', [])
            copy.begin('execution')
            (copy.path / 'app.txt').write_text('interrupted partial edit\n')
        result = coordination.abandon_assignment(self.source, task['id'], assignment,
            'stopped job inspected', confirmed_stopped=True)
        self.assertFalse(copy.path.exists())
        self.assertEqual(result['assignments'][0]['error_kind'], 'cancelled')
        self.assertNotEqual(result['status'], 'completed')

    def test_durable_retry_queue_survives_coordinator_loss_before_recovery(self):
        task, assignment = self.task()
        copy = self.copy()
        self.run_attempt(task, assignment, copy, 'failed')
        coordination.use_result(self.source, task['id'], assignment, 'needs-rework', 'retry requested')
        lifecycle, gc = self.api(), self.api('workspace_gc')
        row = lifecycle.read(copy)
        row['owner'] = {'pid': 999999999, 'start_time': '0', 'boot_id': 'old-boot',
                        'uid': os.geteuid()}
        lifecycle.write(copy, row)
        report = gc.collect(self.state, apply=True)
        item = next(item for item in report['workspaces'] if item['id'] == copy.id)
        self.assertEqual(item['action'], 'kept', item)
        self.assertTrue(copy.path.exists())
        gc.recover(copy)
        self.assertEqual(lifecycle.read(copy)['generation'], row['generation'] + 1)

    def test_explicit_coordinator_resume_renews_its_own_job_generation(self):
        task, assignment = self.task()
        project.attach(self.source, coordinator='codex')
        copy = self.copy()
        self.run_attempt(task, assignment, copy, 'failed')
        coordination.use_result(self.source, task['id'], assignment, 'needs-rework', 'retry queued')
        lifecycle = self.api()
        row = lifecycle.read(copy)
        row['owner'] = {'pid': 999999999, 'start_time': '0', 'boot_id': 'old-boot',
                        'uid': os.geteuid()}
        lifecycle.write(copy, row)
        inode = copy.path.stat().st_ino
        coordinator_hooks.handle({'cwd': str(self.source), 'session_id': 'job-session',
            'hook_event_name': 'SessionStart', 'source': 'resume'}, runtime='codex')
        renewed = lifecycle.read(copy)
        self.assertEqual(renewed['generation'], row['generation'] + 1)
        self.assertEqual(renewed['owner'], lifecycle.owner_identity())
        self.assertEqual(renewed['phase'], 'retry')
        self.assertEqual(copy.path.stat().st_ino, inode)
