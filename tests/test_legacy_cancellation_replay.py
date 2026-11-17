"""Read-only replay of completed legacy cancellations with stale task baselines."""
from __future__ import annotations

import contextlib
import hashlib
import itertools
import json
import os
import subprocess
import unittest
from unittest import mock

from codex_deepseek_team import (coordination, coordinator_hooks, project, relay,
                                 worker, workspace_gc, workspace_lifecycle)
from tests.test_preparation_finalization import PreparationFinalizationFixture


class LegacyCancellationReplayTests(PreparationFinalizationFixture):
    @contextlib.contextmanager
    def empty_capture(self):
        with mock.patch.object(workspace_gc, '_capture', return_value=b''):
            yield

    @contextlib.contextmanager
    def no_execution_boundary(self):
        failure = AssertionError('legacy publication replay must not execute or capture')
        with mock.patch.object(workspace_gc, '_capture', side_effect=failure), \
                mock.patch.object(worker, 'load_api_key', side_effect=failure), \
                mock.patch.object(relay, 'ProviderRelay', side_effect=failure), \
                mock.patch.object(worker, 'execute', side_effect=failure):
            yield

    def workspace_files(self, copy):
        return {name: (copy.directory / name).read_bytes()
                for name in ('record.json', 'lease.json', 'result.json',
                             'changes.patch')
                if (copy.directory / name).exists()}

    def snapshot(self, task_id, copy):
        return {
            'task': coordination._task_path(self.source, task_id).read_bytes(),
            'workspace': self.workspace_files(copy),
            'work_present': os.path.lexists(copy.path),
        }

    def task_content(self, task):
        return {key: value for key, value in task.items()
                if key not in ('status', 'completed_at', 'updated_at')}

    @contextlib.contextmanager
    def owned(self, copy):
        with workspace_lifecycle.fence(self.state):
            with workspace_gc._owner(copy) as fd:
                yield fd

    def mutate_task(self, task_id, change):
        with coordination._lock(self.source):
            task = coordination.load_task(self.source, task_id)
            change(task)
            coordination._atomic(
                coordination._task_path(self.source, task_id), task)

    def mutate_assignment(self, task_id, assignment_id, change):
        def mutate(task):
            row = next(item for item in task['assignments']
                       if item['id'] == assignment_id)
            change(row)

        self.mutate_task(task_id, mutate)

    def mutate_publication(self, task_id, assignment_id, change):
        def mutate(row):
            change(row['job_finalization'])

        self.mutate_assignment(task_id, assignment_id, mutate)

    def mutate_lease(self, copy, change):
        with self.owned(copy):
            lease = workspace_lifecycle.read(copy)
            change(lease)
            workspace_lifecycle.write(copy, lease)

    def mutate_record(self, copy, change):
        with self.owned(copy):
            change(copy.metadata)
            copy.save()

    def mutate_result(self, copy, change):
        with self.owned(copy) as fd:
            result = json.loads(workspace_lifecycle.read_file(fd, 'result.json'))
            change(result)
            workspace_lifecycle.atomic_file(
                fd, 'result.json', (json.dumps(result) + '\n').encode())

    def replace_empty_artifact(self, copy, patch):
        with self.owned(copy) as fd:
            lease = workspace_lifecycle.read(copy)
            result = json.loads(workspace_lifecycle.read_file(fd, 'result.json'))
            digest = hashlib.sha256(patch).hexdigest()
            result['patch'] = {'file': 'changes.patch', 'bytes': len(patch),
                               'sha256': digest}
            lease['finalization']['capture'] = {
                'bytes': len(patch), 'sha256': digest, 'attempt_id': None}
            workspace_lifecycle.write(copy, lease)
            workspace_lifecycle.atomic_file(fd, 'changes.patch', patch)
            workspace_lifecycle.atomic_file(
                fd, 'result.json', (json.dumps(result) + '\n').encode())

    def replace_artifact(self, copy, patch):
        with self.owned(copy) as fd:
            workspace_lifecycle.atomic_file(fd, 'changes.patch', patch)

    def assert_refused(self, task_id, assignment_id, copy):
        before = self.snapshot(task_id, copy)
        with self.no_execution_boundary():
            with self.assertRaises(coordination.CoordinationError) as caught:
                coordination.finalize_assignment(
                    self.source, task_id, assignment_id,
                    'conflicting legacy history must remain preserved',
                    cancelled=True)
        self.assertEqual(self.snapshot(task_id, copy), before)
        return caught.exception

    def legacy_cancellation(self):
        task, assignment_id = self.new_task()
        source_head_a = task['base_head']
        (self.source / 'app.txt').write_text(f'allocation base {self._turn}\n')
        subprocess.run(
            ['git', '-C', str(self.source), '-c', 'user.name=Fixture',
             '-c', 'user.email=fixture@example.invalid', 'commit', '-qam',
             'allocation base'],
            check=True)
        copy = self.copy()
        self.assertNotEqual(source_head_a, copy.metadata['base_head'])
        copy.metadata.update(
            coord_task=task['id'], coord_assignment=assignment_id,
            status='failed', error_kind='preparation', exit_code=78)
        copy.save()
        coordination.assignment_preparation_failed(
            self.source, task['id'], assignment_id,
            'allocation at a newer source base failed before model execution',
            workspace_id=copy.id)
        with coordination._lock(self.source):
            current = coordination.load_task(self.source, task['id'])
            row = next(item for item in current['assignments']
                       if item['id'] == assignment_id)
            row['disposition'] = {
                'kind': 'rejected', 'evidence': 'preparation cannot run at base B',
                'at': 1.0}
            coordination._atomic(
                coordination._task_path(self.source, task['id']), current)
        with self.empty_capture():
            report = workspace_gc.finalize(
                copy, reason='legacy cancellation at allocation base',
                cancelled=True)
        lease = workspace_lifecycle.read(copy)
        result = workspace_gc.read_result(copy, lease=lease)
        self.assertEqual(lease['phase'], 'cancelled')
        self.assertEqual(lease['cleanup']['state'], 'cleaned')
        self.assertEqual(lease['attempts'], [])
        self.assertNotIn('binding', lease)
        self.assertEqual(result['schema'], 1)
        self.assertEqual(result['base_head'], copy.metadata['base_head'])
        self.assertNotEqual(result['base_head'], source_head_a)
        self.assertEqual(result['patch']['bytes'], 0)
        self.assertEqual(result['patch']['sha256'],
                         'e3b0c44298fc1c149afbf4c8996fb924'
                         '27ae41e4649b934ca495991b7852b855')
        self.assertFalse(copy.path.exists())
        publication = dict(report)
        publication.pop('reason_code', None)
        publication.update(
            id=copy.id, workspace_id=copy.id, job_id=lease['job_id'],
            cancelled=True, at=1.0,
            evidence='legacy cancellation at allocation base was reviewed')
        with coordination._lock(self.source):
            current = coordination.load_task(self.source, task['id'])
            row = next(item for item in current['assignments']
                       if item['id'] == assignment_id)
            self.assertNotIn('job_finalization_request', row)
            row['job_finalization'] = publication
            coordination._atomic(
                coordination._task_path(self.source, task['id']), current)
        return task['id'], assignment_id, copy, publication

    def test_public_finalize_cancel_replays_without_writes(self):
        task_id, assignment_id, copy, publication = self.legacy_cancellation()
        before = self.snapshot(task_id, copy)
        before_row = self.row(task_id, assignment_id)
        with self.no_execution_boundary():
            for evidence in (
                    'repeat the completed cancellation',
                    'repeat again with different prose'):
                code, _output, errors = self.command(
                    'coordination', 'finalize', '--path', str(self.source),
                    '--task', task_id, '--assignment', assignment_id,
                    '--evidence', evidence, '--cancel')
                self.assertEqual(code, 0, errors)
                self.assertEqual(self.snapshot(task_id, copy), before)
                self.assertEqual(self.row(task_id, assignment_id), before_row)
                self.assertEqual(before_row['job_finalization'], publication)
        self.assertFalse(os.path.lexists(copy.path))

    def test_minimal_report_artifacts_without_modern_finalization(self):
        for operation in ('finalize', 'complete', 'stop', 'stop-retry'):
            with self.subTest(operation=operation):
                task_id, assignment_id, copy, _ = self.legacy_cancellation()
                self.mutate_lease(copy, lambda lease: lease.pop('finalization'))
                result = {
                    'schema': 1, 'workspace_id': copy.id,
                    'source': str(self.source), 'base_head': copy.metadata['base_head'],
                    'patch': {'file': 'changes.patch', 'bytes': 0,
                              'sha256': hashlib.sha256(b'').hexdigest()},
                }
                self.mutate_result(copy, lambda saved: (saved.clear(), saved.update(result)))
                publication = {
                    'action': 'cleaned', 'id': copy.id,
                    'job_id': copy.metadata['job_id'],
                    'evidence': 'legacy pre-execution cancellation already completed',
                }
                self.mutate_assignment(
                    task_id, assignment_id,
                    lambda row: row.update(job_finalization=publication))
                lease = workspace_lifecycle.read(copy)
                self.assertNotIn('finalization', lease)
                self.assertNotIn('binding', lease)
                self.assertEqual(lease['attempts'], [])
                self.assertNotIn('job_finalization_request', self.row(task_id, assignment_id))
                before = self.snapshot(task_id, copy)
                before_task = coordination.load_task(self.source, task_id)
                with self.no_execution_boundary():
                    if operation in ('finalize', 'complete'):
                        for _ in range(2):
                            args = ['coordination', operation, '--path', str(self.source),
                                    '--task', task_id]
                            if operation == 'finalize':
                                args += ['--assignment', assignment_id, '--cancel',
                                         '--evidence', 'repeat the retained cancellation']
                            code, _, errors = self.command(*args)
                            self.assertEqual(code, 0, errors)
                    else:
                        response = coordinator_hooks.handle({
                            'cwd': str(self.source), 'session_id': self.session,
                            'hook_event_name': 'Stop',
                            'stop_hook_active': operation == 'stop-retry',
                        }, runtime='codex')
                        self.assertNotEqual(response.get('decision'), 'block', response)
                        self.assertNotIn('Finalization recovery refused', str(response))
                after_task = coordination.load_task(self.source, task_id)
                if operation == 'finalize':
                    self.assertEqual(self.snapshot(task_id, copy), before)
                else:
                    self.assertEqual(after_task['status'], 'completed')
                self.assertEqual(self.task_content(after_task), self.task_content(before_task))
                self.assertEqual(self.workspace_files(copy), before['workspace'])
                self.assertFalse(os.path.lexists(copy.path))

    def test_complete_replays_the_publication_and_is_repeatable(self):
        task_id, assignment_id, copy, publication = self.legacy_cancellation()
        before = self.snapshot(task_id, copy)
        before_task = coordination.load_task(self.source, task_id)
        with self.no_execution_boundary():
            first = coordination.complete_task(self.source, task_id)
            second = coordination.complete_task(self.source, task_id)
        self.assertEqual(first['status'], 'completed')
        self.assertEqual(second['status'], 'completed')
        after = self.snapshot(task_id, copy)
        self.assertEqual(after['workspace'], before['workspace'])
        self.assertEqual(after['work_present'], before['work_present'])
        row = next(item for item in second['assignments']
                   if item['id'] == assignment_id)
        self.assertEqual(row['job_finalization'], publication)
        self.assertEqual(self.task_content(second), self.task_content(before_task))
        self.assertNotIn('job_finalization_request', row)

    def test_refuses_non_hash_baseline_lengths(self):
        for length in (39, 41, 63, 65):
            with self.subTest(length=length):
                task_id, assignment_id, copy, _ = self.legacy_cancellation()
                self.mutate_task(task_id, lambda task: task.update(base_head='a' * length))
                self.assert_refused(task_id, assignment_id, copy)

    def test_refuses_conflicting_optional_finalization(self):
        cases = (('cancelled', False), ('discard_result', True),
                 ('discarded', True), ('result_schema', True),
                 ('reason_code', 'capture_failed'))
        for key, value in cases:
            with self.subTest(field=key):
                task_id, assignment_id, copy, _ = self.legacy_cancellation()
                with self.owned(copy) as fd:
                    lease = json.loads(workspace_lifecycle.read_file(fd, 'lease.json'))
                    lease['finalization'][key] = value
                    workspace_lifecycle.atomic_file(
                        fd, 'lease.json', (json.dumps(lease) + '\n').encode())
                self.assert_refused(task_id, assignment_id, copy)

    def test_stop_replays_for_both_flags_and_runtimes(self):
        project.attach(self.source, coordinator='both')
        for runtime, retry_active in itertools.product(
                ('codex', 'claude'), (False, True)):
            with self.subTest(runtime=runtime, stop_hook_active=retry_active):
                task_id, assignment_id, copy, publication = self.legacy_cancellation()
                before = self.snapshot(task_id, copy)
                before_task = coordination.load_task(self.source, task_id)
                with self.no_execution_boundary():
                    result = coordinator_hooks.handle({
                        'cwd': str(self.source), 'session_id': self.session,
                        'turn_id': 'legacy-stop-' + str(retry_active),
                        'hook_event_name': 'Stop',
                        'stop_hook_active': retry_active,
                    }, runtime=runtime)
                self.assertNotEqual(result.get('decision'), 'block', result)
                self.assertNotIn('Finalization recovery refused', str(result))
                task = coordination.load_task(self.source, task_id)
                self.assertEqual(task['status'], 'completed')
                row = next(item for item in task['assignments']
                           if item['id'] == assignment_id)
                self.assertEqual(row['job_finalization'], publication)
                after = self.snapshot(task_id, copy)
                self.assertEqual(after['workspace'], before['workspace'])
                self.assertEqual(after['work_present'], before['work_present'])
                self.assertEqual(self.task_content(task), self.task_content(before_task))

    def test_optional_report_fields_and_already_cleaned_action_are_accepted(self):
        cases = (
            ('optional-decisions', lambda report: [report.pop(key) for key in (
                'workspace_id', 'cancelled', 'discarded', 'result_schema',
                'reason_code') if key in report]),
            ('already-cleaned', lambda report: report.update(action='already-cleaned')),
        )
        for name, change in cases:
            with self.subTest(name=name):
                task_id, assignment_id, copy, _publication = self.legacy_cancellation()
                self.mutate_publication(task_id, assignment_id, change)
                before = self.snapshot(task_id, copy)
                with self.no_execution_boundary():
                    code, _output, errors = self.command(
                        'coordination', 'finalize', '--path', str(self.source),
                        '--task', task_id, '--assignment', assignment_id,
                        '--evidence', 'repeat the exact completed cancellation',
                        '--cancel')
                self.assertEqual(code, 0, errors)
                self.assertEqual(self.snapshot(task_id, copy), before)

    def test_refuses_report_identity_action_decision_schema_and_evidence(self):
        cases = (
            ('report-id', lambda report: report.update(id='0' * 32)),
            ('report-id-type', lambda report: report.update(id=1)),
            ('report-job', lambda report: report.update(job_id='0' * 32)),
            ('report-job-type', lambda report: report.update(job_id=False)),
            ('report-workspace', lambda report: report.update(workspace_id='0' * 32)),
            ('report-workspace-type', lambda report: report.update(workspace_id=1)),
            ('report-action', lambda report: report.update(action='finalized')),
            ('report-action-missing', lambda report: report.pop('action')),
            ('report-action-type', lambda report: report.update(action=1)),
            ('report-cancelled-false', lambda report: report.update(cancelled=False)),
            ('report-cancelled-int-alias', lambda report: report.update(cancelled=1)),
            ('report-discarded-true', lambda report: report.update(discarded=True)),
            ('report-discarded-int-alias', lambda report: report.update(discarded=0)),
            ('report-schema-two', lambda report: report.update(result_schema=2)),
            ('report-schema-bool-alias', lambda report: report.update(result_schema=True)),
            ('report-schema-float-alias', lambda report: report.update(result_schema=1.0)),
            ('report-reason-code', lambda report: report.update(reason_code='capture_failed')),
            ('report-evidence-missing', lambda report: report.pop('evidence')),
            ('report-evidence-empty', lambda report: report.update(evidence='')),
            ('report-evidence-spaces', lambda report: report.update(evidence='   ')),
            ('report-evidence-type', lambda report: report.update(evidence=1)),
            ('report-unknown-key', lambda report: report.update(discard_result=True)),
            ('report-at-bool-alias', lambda report: report.update(at=True)),
            ('report-reason-empty', lambda report: report.update(reason='')),
            ('report-allocated-bytes-type', lambda report: report.update(allocated_bytes=True)),
            ('report-inodes-negative', lambda report: report.update(inodes=-1)),
        )
        for name, change in cases:
            with self.subTest(name=name):
                task_id, assignment_id, copy, _publication = self.legacy_cancellation()
                self.mutate_publication(task_id, assignment_id, change)
                self.assert_refused(task_id, assignment_id, copy)

    def test_refuses_task_assignment_record_and_report_identity_conflicts(self):
        def task_project(task):
            task['project'] = str(self.root / 'other-project')

        def mutate_task_identity(task):
            task['id'] = 'task-' + '0' * 20

        def task_baseline(task):
            task['base_head'] = 'not-a-source-head'

        def row_workspace(row):
            row['workspace_id'] = '0' * 32

        def row_job(row):
            row['job_id'] = '0' * 32

        def row_status(row):
            row['status'] = 'succeeded'

        def row_error(row):
            row['error_kind'] = 'provider'

        def row_stage(row):
            row['failure_stage'] = 'execution'

        def row_missing_disposition(row):
            row.pop('disposition')

        def row_invalid_disposition(row):
            row['disposition']['kind'] = 'incorporated'

        def row_empty_disposition_evidence(row):
            row['disposition']['evidence'] = '  '

        def record_source(copy):
            self.mutate_record(
                copy, lambda metadata: metadata.update(
                    source=str(self.root / 'other-project')))

        cases = (
            ('task-project', lambda task_id_value, assignment_id, copy: self.mutate_task(
                task_id_value, task_project)),
            ('task-id', lambda task_id_value, assignment_id, copy: self.mutate_task(
                task_id_value, mutate_task_identity)),
            ('task-baseline', lambda task_id_value, assignment_id, copy: self.mutate_task(
                task_id_value, task_baseline)),
            ('row-workspace-id', lambda task_id_value, assignment_id, copy:
                self.mutate_assignment(task_id_value, assignment_id, row_workspace)),
            ('row-job-id', lambda task_id_value, assignment_id, copy:
                self.mutate_assignment(task_id_value, assignment_id, row_job)),
            ('row-status', lambda task_id_value, assignment_id, copy:
                self.mutate_assignment(task_id_value, assignment_id, row_status)),
            ('row-error-kind', lambda task_id_value, assignment_id, copy:
                self.mutate_assignment(task_id_value, assignment_id, row_error)),
            ('row-failure-stage', lambda task_id_value, assignment_id, copy:
                self.mutate_assignment(task_id_value, assignment_id, row_stage)),
            ('row-disposition-missing', lambda task_id_value, assignment_id, copy:
                self.mutate_assignment(task_id_value, assignment_id, row_missing_disposition)),
            ('row-disposition-kind', lambda task_id_value, assignment_id, copy:
                self.mutate_assignment(task_id_value, assignment_id, row_invalid_disposition)),
            ('row-disposition-evidence', lambda task_id_value, assignment_id, copy:
                self.mutate_assignment(
                    task_id_value, assignment_id, row_empty_disposition_evidence)),
            ('record-source', lambda task_id_value, assignment_id, copy: record_source(copy)),
            ('record-coord-task', lambda task_id_value, assignment_id, copy:
                self.mutate_record(
                    copy, lambda metadata: metadata.update(coord_task='task-' + '0' * 20))),
            ('record-coord-assignment', lambda task_id_value, assignment_id, copy:
                self.mutate_record(
                    copy, lambda metadata: metadata.update(coord_assignment='as-' + '0' * 16))),
            ('record-job-id', lambda task_id_value, assignment_id, copy:
                self.mutate_record(
                    copy, lambda metadata: metadata.update(job_id='0' * 32))),
            ('record-work-inode', lambda task_id_value, assignment_id, copy:
                self.mutate_record(
                    copy, lambda metadata: metadata.update(
                        work_inode=metadata['work_inode'] + 1))),
            ('record-base-head', lambda task_id_value, assignment_id, copy:
                self.mutate_record(
                    copy, lambda metadata: metadata.update(base_head='not-a-source-head'))),
            ('record-source-head', lambda task_id_value, assignment_id, copy:
                self.mutate_record(
                    copy, lambda metadata: metadata.update(source_head='0' * 40))),
            ('record-attempt', lambda task_id_value, assignment_id, copy:
                self.mutate_record(copy, lambda metadata: metadata.update(attempt=1))),
            ('record-attempt-id', lambda task_id_value, assignment_id, copy:
                self.mutate_record(copy, lambda metadata: metadata.update(attempt_id='0' * 32))),
            ('record-started-at', lambda task_id_value, assignment_id, copy:
                self.mutate_record(copy, lambda metadata: metadata.update(started_at=1.0))),
            ('record-operation', lambda task_id_value, assignment_id, copy:
                self.mutate_record(copy, lambda metadata: metadata.update(operation='execution'))),
            ('publication-not-object', lambda task_id_value, assignment_id, copy:
                self.mutate_assignment(
                    task_id_value, assignment_id,
                    lambda row: row.update(job_finalization='not-a-publication'))),
        )
        for name, mutate in cases:
            with self.subTest(name=name):
                task_id, assignment_id, copy, _publication = self.legacy_cancellation()
                mutate(task_id, assignment_id, copy)
                self.assert_refused(task_id, assignment_id, copy)

    def test_refuses_execution_pending_history_request_and_correction(self):
        def lease_attempts(lease):
            lease['attempts'].append({
                'id': '0' * 32, 'operation': 'preparation', 'status': 'failed',
                'started_at': 1.0, 'finished_at': 2.0,
                'generation': lease['generation']})

        def deliverable_correction(task):
            task['deliverables'][0]['correction_of'] = {
                'task_id': 'task-' + '0' * 20,
                'assignment_id': 'as-' + '0' * 16}

        cases = (
            ('row-started-at', lambda task_id, assignment_id, copy:
                self.mutate_assignment(
                    task_id, assignment_id, lambda row: row.update(started_at=1.0))),
            ('row-attempt-id', lambda task_id, assignment_id, copy:
                self.mutate_assignment(
                    task_id, assignment_id,
                    lambda row: row.update(attempt_id='0' * 32))),
            ('row-execution-snapshot', lambda task_id, assignment_id, copy:
                self.mutate_assignment(
                    task_id, assignment_id,
                    lambda row: row.update(execution_snapshot={'execution': {}}))),
            ('row-attempt-history', lambda task_id, assignment_id, copy:
                self.mutate_assignment(
                    task_id, assignment_id,
                    lambda row: row.update(attempt_history=[{'workspace_id': copy.id}]))),
            ('row-worker-changes', lambda task_id, assignment_id, copy:
                self.mutate_assignment(
                    task_id, assignment_id,
                    lambda row: row.update(worker_changes=['app.txt']))),
            ('row-checks', lambda task_id, assignment_id, copy:
                self.mutate_assignment(
                    task_id, assignment_id,
                    lambda row: row.update(checks=[{'command': 'true'}]))),
            ('row-request', lambda task_id, assignment_id, copy:
                self.mutate_assignment(
                    task_id, assignment_id,
                    lambda row: row.update(job_finalization_request={'cancelled': True}))),
            ('row-correction', lambda task_id, assignment_id, copy:
                self.mutate_assignment(
                    task_id, assignment_id,
                    lambda row: row.update(correction_of={
                        'task_id': 'task-' + '0' * 20,
                        'assignment_id': 'as-' + '0' * 16}))),
            ('deliverable-correction', lambda task_id, assignment_id, copy:
                self.mutate_task(task_id, deliverable_correction)),
            ('lease-attempt-history', lambda task_id, assignment_id, copy:
                self.mutate_lease(copy, lease_attempts)),
            ('lease-pending-attempt', lambda task_id, assignment_id, copy:
                self.mutate_lease(copy, lambda lease: lease.update(pending_attempt={
                    'task_id': task_id, 'assignment_id': assignment_id,
                    'id': '0' * 32}))),
            ('lease-binding', lambda task_id, assignment_id, copy:
                self.mutate_lease(copy, lambda lease: lease.update(binding={
                    'task_id': task_id, 'assignment_id': assignment_id}))),
            ('lease-binding-history', lambda task_id, assignment_id, copy:
                self.mutate_lease(copy, lambda lease: lease.update(binding_history=[{
                    'task_id': task_id, 'assignment_id': assignment_id}]))),
        )
        for name, mutate in cases:
            with self.subTest(name=name):
                task_id, assignment_id, copy, _publication = self.legacy_cancellation()
                mutate(task_id, assignment_id, copy)
                self.assert_refused(task_id, assignment_id, copy)

    def test_refuses_retained_result_and_artifact_conflicts(self):
        def result_schema(result):
            result['schema'] = 2

        def result_baseline(result):
            result['base_head'] = '0' * 40

        def result_source(result):
            result['source'] = str(self.root / 'other-project')

        def result_workspace(result):
            result['workspace_id'] = '0' * 32

        def result_job(result):
            result['job_id'] = '0' * 32

        def result_attempt(result):
            result['attempt_id'] = '0' * 32

        def result_bytes(result):
            result['patch']['bytes'] = 1

        def result_digest(result):
            result['patch']['sha256'] = '0' * 64

        def result_patch_name(result):
            result['patch']['file'] = 'other.patch'

        def result_unknown(result):
            result['unknown'] = True

        def delete_result(copy):
            (copy.directory / 'result.json').unlink()

        cases = (
            ('result-schema-two', lambda task_id, assignment_id, copy:
                self.mutate_result(copy, result_schema)),
            ('result-baseline', lambda task_id, assignment_id, copy:
                self.mutate_result(copy, result_baseline)),
            ('result-source', lambda task_id, assignment_id, copy:
                self.mutate_result(copy, result_source)),
            ('result-workspace', lambda task_id, assignment_id, copy:
                self.mutate_result(copy, result_workspace)),
            ('result-job', lambda task_id, assignment_id, copy:
                self.mutate_result(copy, result_job)),
            ('result-attempt', lambda task_id, assignment_id, copy:
                self.mutate_result(copy, result_attempt)),
            ('result-patch-bytes', lambda task_id, assignment_id, copy:
                self.mutate_result(copy, result_bytes)),
            ('result-patch-digest', lambda task_id, assignment_id, copy:
                self.mutate_result(copy, result_digest)),
            ('result-patch-name', lambda task_id, assignment_id, copy:
                self.mutate_result(copy, result_patch_name)),
            ('result-unknown-key', lambda task_id, assignment_id, copy:
                self.mutate_result(copy, result_unknown)),
            ('result-missing', lambda task_id, assignment_id, copy: delete_result(copy)),
            ('patch-corrupt', lambda task_id, assignment_id, copy:
                self.replace_artifact(copy, b'corrupt\n')),
            ('patch-missing', lambda task_id, assignment_id, copy:
                (copy.directory / 'changes.patch').unlink()),
            ('patch-nonempty-valid-digest', lambda task_id, assignment_id, copy:
                self.replace_empty_artifact(copy, b'nonempty but valid\n')),
        )
        for name, mutate in cases:
            with self.subTest(name=name):
                task_id, assignment_id, copy, _publication = self.legacy_cancellation()
                mutate(task_id, assignment_id, copy)
                self.assert_refused(task_id, assignment_id, copy)

    def test_refuses_malformed_lease_authority_types(self):
        def cleanup_alias(copy, device, inode):
            def change(lease):
                lease.update(work_device=1, work_inode=1)
                lease['cleanup'].update(device=device, inode=inode)

            self.mutate_lease(copy, change)
            self.mutate_record(copy, lambda metadata: metadata.update(work_inode=1))

        def lease_schema_alias(lease):
            lease['schema'] = True

        def cleanup_name_alias(lease):
            lease['cleanup']['name'] = int('1' * 32)

        def cleanup_extra_name_alias(lease):
            lease['cleanup']['extras'] = [{
                'source': 'preparation-home', 'name': int('2' * 32),
                'device': 1, 'inode': 2}]

        cases = (
            ('device-bool-alias', lambda task_id, assignment_id, copy:
                cleanup_alias(copy, True, 1)),
            ('inode-float-alias', lambda task_id, assignment_id, copy:
                cleanup_alias(copy, 1, 1.0)),
            ('lease-schema-bool-alias', lambda task_id, assignment_id, copy:
                self.mutate_lease(copy, lease_schema_alias)),
            ('cleanup-name-int-alias', lambda task_id, assignment_id, copy:
                self.mutate_lease(copy, cleanup_name_alias)),
            ('cleanup-extra-name-int-alias', lambda task_id, assignment_id, copy:
                self.mutate_lease(copy, cleanup_extra_name_alias)),
        )
        for name, mutate in cases:
            with self.subTest(name=name):
                task_id, assignment_id, copy, _publication = self.legacy_cancellation()
                mutate(task_id, assignment_id, copy)
                self.assert_refused(task_id, assignment_id, copy)

    def test_refuses_replacement_work_and_quarantined_cleanup_data(self):
        with self.subTest(name='replacement-work'):
            task_id, assignment_id, copy, _publication = self.legacy_cancellation()
            copy.path.mkdir(mode=0o700)
            self.assert_refused(task_id, assignment_id, copy)
            self.assertTrue(copy.path.is_dir())

        with self.subTest(name='quarantined-extra'):
            task_id, assignment_id, copy, _publication = self.legacy_cancellation()
            extra_name = 'f' * 32
            lease = workspace_lifecycle.read(copy)
            cleanup_name = lease['cleanup']['name']
            self.mutate_lease(copy, lambda row: row['cleanup'].update(extras=[{
                'source': 'preparation-home', 'name': extra_name,
                'device': 1, 'inode': 2}]))
            quarantine = self.state / 'quarantine'
            quarantine.mkdir(mode=0o700, exist_ok=True)
            (quarantine / cleanup_name).mkdir(mode=0o700)
            (quarantine / extra_name).mkdir(mode=0o700)
            self.assert_refused(task_id, assignment_id, copy)
            self.assertTrue((quarantine / cleanup_name).is_dir())
            self.assertTrue((quarantine / extra_name).is_dir())

    def test_refuses_missing_publication_without_historical_repair(self):
        task_id, assignment_id, copy, _publication = self.legacy_cancellation()
        self.mutate_assignment(
            task_id, assignment_id, lambda row: row.pop('job_finalization'))
        self.assert_refused(task_id, assignment_id, copy)
        self.assertNotIn('job_finalization', self.row(task_id, assignment_id))

    def test_refuses_foreign_live_unknown_and_held_owners(self):
        with self.subTest(name='foreign-owner-uid'):
            task_id, assignment_id, copy, _publication = self.legacy_cancellation()
            foreign_uid = (os.geteuid() + 1) % (2 ** 32)

            def set_foreign(lease):
                lease['owner']['uid'] = foreign_uid

            self.mutate_lease(copy, set_foreign)
            self.assert_refused(task_id, assignment_id, copy)

        with self.subTest(name='foreign-live-owner'):
            task_id, assignment_id, copy, _publication = self.legacy_cancellation()
            process = subprocess.Popen(['sleep', '30'])
            try:
                identity = workspace_lifecycle.process_identity(process.pid)
                self.assertIsNotNone(identity)
                self.mutate_lease(copy, lambda lease: lease.update(owner=identity))
                self.assert_refused(task_id, assignment_id, copy)
            finally:
                process.terminate()
                process.wait(timeout=10)

        with self.subTest(name='unknown-owner'):
            task_id, assignment_id, copy, _publication = self.legacy_cancellation()
            with mock.patch.object(workspace_lifecycle, 'identity_state',
                                   return_value='unknown'):
                self.assert_refused(task_id, assignment_id, copy)

        with self.subTest(name='held-owner-lock'):
            task_id, assignment_id, copy, _publication = self.legacy_cancellation()
            with self.owned(copy):
                self.assert_refused(task_id, assignment_id, copy)


if __name__ == '__main__':
    unittest.main()
