"""Adversarial recovery boundaries for already-terminal preparation history."""
from __future__ import annotations

import contextlib
import errno
import json
import os
import subprocess
import unittest
from unittest import mock

from codex_deepseek_team import (coordination, relay, worker, workspace_gc,
                                 workspace_lifecycle)
from tests.test_preparation_finalization import PreparationFinalizationFixture


class PreparationRecoveryBoundaryTests(PreparationFinalizationFixture):
    @contextlib.contextmanager
    def owned(self, copy):
        with workspace_lifecycle.fence(self.state):
            with workspace_gc._owner(copy) as fd:
                yield fd

    def mutate_lease(self, copy, change):
        with self.owned(copy):
            lease = workspace_lifecycle.read(copy)
            change(lease)
            workspace_lifecycle.write(copy, lease)

    def mutate_record(self, copy, change):
        with self.owned(copy):
            change(copy)
            copy.save()

    def mutate_result(self, copy, change):
        with self.owned(copy) as fd:
            result = json.loads(workspace_lifecycle.read_file(fd, 'result.json'))
            change(result)
            workspace_lifecycle.atomic_file(
                fd, 'result.json', (json.dumps(result) + '\n').encode())

    def mutate_patch(self, copy, change):
        with self.owned(copy) as fd:
            patch = workspace_lifecycle.read_file(
                fd, 'changes.patch', maximum=workspace_gc.MAX_PATCH_BYTES)
            workspace_lifecycle.atomic_file(fd, 'changes.patch', change(patch))

    def mutate_assignment(self, task_id, assignment_id, change):
        def mutate(task):
            row = next(item for item in task['assignments']
                       if item['id'] == assignment_id)
            change(row)

        self.mutate_task(task_id, mutate)

    def assert_legacy_refused(self, task_id, assignment_id, *copies, code=None):
        snapshots = self.snapshot(task_id, *copies)
        with self.assertRaises(coordination.CoordinationError) as caught:
            coordination.finalize_assignment(
                self.source, task_id, assignment_id,
                'inconsistent historic evidence must stay preserved', cancelled=True)
        if code is not None:
            self.assertEqual(caught.exception.code, code)
        self.assert_refusal_preserved(task_id, snapshots, *copies)
        row = self.row(task_id, assignment_id)
        self.assertNotIn('job_finalization', row)
        return caught.exception

    def test_historical_null_binding_requires_corroborated_identity(self):
        cases = []

        def coord_task(copy):
            copy.metadata['coord_task'] = 'task-' + '1' * 20

        cases.append(('saved-coord-task', None, coord_task))

        def coord_assignment(copy):
            copy.metadata['coord_assignment'] = 'as-' + '1' * 16

        cases.append(('saved-coord-assignment', None, coord_assignment))

        def record_job(copy):
            copy.metadata['job_id'] = 'c' * 32

        cases.append(('record-job-id', None, record_job))

        def record_source(copy):
            copy.metadata['source'] = str(self.root / 'other-project')

        cases.append(('record-source-project', None, record_source))

        def task_project(task):
            task['project'] = str(self.root / 'other-project')

        cases.append(('task-project', task_project, None))

        def task_identity(task):
            task['id'] = 'task-' + '2' * 20

        cases.append(('task-id', task_identity, None))

        for name, task_change, record_change in cases:
            with self.subTest(name=name):
                task_id, assignment_id, copy = self.historic_cleaned_failure()
                if task_change is not None:
                    self.mutate_task(task_id, task_change)
                if record_change is not None:
                    self.mutate_record(copy, record_change)
                self.assert_legacy_refused(task_id, assignment_id, copy)

        with self.subTest(name='ledger-job-id'):
            task_id, assignment_id, copy = self.historic_cleaned_failure()
            self.mutate_assignment(
                task_id, assignment_id, lambda row: row.update(job_id='d' * 32))
            self.assert_legacy_refused(task_id, assignment_id, copy)

        with self.subTest(name='ledger-workspace-id'):
            task_id, assignment_id, copy = self.historic_cleaned_failure()
            other = self.copy()
            self.mutate_assignment(
                task_id, assignment_id, lambda row: row.update(workspace_id=other.id))
            self.assert_legacy_refused(task_id, assignment_id, copy, other)

    def test_historical_saved_intent_requires_consistent_identity_and_history(self):
        with self.subTest(name='missing-intent'):
            task_id, assignment_id, copy = self.historic_cleaned_failure()
            self.mutate_assignment(
                task_id, assignment_id,
                lambda row: row.pop('job_finalization_request'))
            self.assert_legacy_refused(task_id, assignment_id, copy)

        request_cases = []
        request_cases.append((
            'missing-workspace-id', lambda request, _copy: request.pop('workspace_id')))
        request_cases.append((
            'other-workspace-id',
            lambda request, _copy: request.update(workspace_id='f' * 32)))
        request_cases.append((
            'other-job-id', lambda request, _copy: request.update(job_id='e' * 32)))
        request_cases.append((
            'other-task-id',
            lambda request, _copy: request.update(task_id='task-' + '3' * 20)))
        request_cases.append((
            'other-assignment-id',
            lambda request, _copy: request.update(assignment_id='as-' + '3' * 16)))
        request_cases.append((
            'other-project',
            lambda request, _copy: request.update(project=str(self.root / 'other-project'))))
        request_cases.append((
            'changed-terminal-decision',
            lambda request, _copy: request.update(cancelled=False)))
        request_cases.append((
            'missing-terminal-decision',
            lambda request, _copy: request.pop('cancelled')))

        for name, change in request_cases:
            with self.subTest(name=name):
                task_id, assignment_id, copy = self.historic_cleaned_failure()

                def mutate(request):
                    change(request, copy)

                self.mutate_assignment(
                    task_id, assignment_id,
                    lambda row: mutate(row['job_finalization_request']))
                self.assert_legacy_refused(task_id, assignment_id, copy)

        with self.subTest(name='conflicting-binding-history'):
            task_id, assignment_id, copy = self.historic_cleaned_failure()
            self.mutate_lease(copy, lambda lease: lease.update(binding_history=[{
                'task_id': task_id, 'assignment_id': 'as-' + '4' * 16}]))
            self.assert_legacy_refused(task_id, assignment_id, copy)

        with self.subTest(name='pending-execution-reservation'):
            task_id, assignment_id, copy = self.historic_cleaned_failure()
            self.mutate_lease(copy, lambda lease: lease.update(pending_attempt={
                'task_id': task_id, 'assignment_id': assignment_id, 'id': '5' * 32}))
            self.assert_legacy_refused(task_id, assignment_id, copy)

        execution_cases = {
            'assignment-attempt-id': lambda row: row.update(attempt_id='6' * 32),
            'assignment-start-time': lambda row: row.update(started_at=1.0),
            'assignment-execution-snapshot': lambda row: row.update(
                execution_snapshot={'execution': {'attempt_id': '7' * 32}}),
        }
        for name, change in execution_cases.items():
            with self.subTest(name=name):
                task_id, assignment_id, copy = self.historic_cleaned_failure()
                self.mutate_assignment(task_id, assignment_id, change)
                self.assert_legacy_refused(task_id, assignment_id, copy)

    def test_terminal_legacy_repair_rejects_owner_and_cleanup_boundaries(self):
        with self.subTest(name='unrelated-live-owner'):
            task_id, assignment_id, copy = self.historic_cleaned_failure()
            process = subprocess.Popen(['sleep', '30'])
            try:
                foreign = workspace_lifecycle.process_identity(process.pid)
                self.assertIsNotNone(foreign)
                self.mutate_lease(copy, lambda lease: lease.update(owner=foreign))
                self.assert_legacy_refused(task_id, assignment_id, copy, code=75)
            finally:
                process.terminate()
                process.wait(timeout=10)

        with self.subTest(name='unknown-owner-namespace'):
            task_id, assignment_id, copy = self.historic_cleaned_failure()
            with mock.patch.object(workspace_lifecycle, 'identity_state',
                                   return_value='unknown'):
                self.assert_legacy_refused(task_id, assignment_id, copy, code=75)

        with self.subTest(name='held-owner-lock'):
            task_id, assignment_id, copy = self.historic_cleaned_failure()
            with self.owned(copy):
                self.assert_legacy_refused(task_id, assignment_id, copy, code=75)

        with self.subTest(name='replacement-work-tree'):
            task_id, assignment_id, copy = self.historic_cleaned_failure()
            copy.path.mkdir(mode=0o700)
            canary = copy.path / 'replacement-canary'
            canary.write_text('foreign replacement work\n')
            snapshots = self.snapshot(task_id, copy)
            with self.assertRaises(coordination.CoordinationError) as caught:
                coordination.finalize_assignment(
                    self.source, task_id, assignment_id,
                    'replacement work must stay preserved', cancelled=True)
            self.assertEqual(caught.exception.code, 73)
            self.assert_refusal_preserved(task_id, snapshots, copy)
            self.assertEqual(canary.read_text(), 'foreign replacement work\n')
            self.assertEqual(copy.path.stat().st_uid, os.geteuid())

    def test_terminal_legacy_repair_rejects_foreign_lease_uid(self):
        task_id, assignment_id, copy = self.historic_cleaned_failure()
        foreign_uid = (os.geteuid() + 1) % (2 ** 32)

        def foreign_owner(lease):
            lease['owner']['uid'] = foreign_uid

        self.mutate_lease(copy, foreign_owner)
        self.assert_legacy_refused(task_id, assignment_id, copy, code=73)

    def test_retained_result_identity_and_patch_mismatches_refuse(self):
        result_cases = {
            'result-source': lambda result: result.update(
                source=str(self.root / 'other-project')),
            'result-workspace': lambda result: result.update(workspace_id='8' * 32),
            'result-job': lambda result: result.update(job_id='9' * 32),
        }
        for name, change in result_cases.items():
            with self.subTest(name=name):
                task_id, assignment_id, copy = self.historic_cleaned_failure()
                self.mutate_result(copy, change)
                self.assert_legacy_refused(task_id, assignment_id, copy, code=73)

        with self.subTest(name='retained-patch-hash'):
            task_id, assignment_id, copy = self.historic_cleaned_failure()
            self.mutate_patch(copy, lambda patch: patch + b'corrupted patch\n')
            self.assert_legacy_refused(task_id, assignment_id, copy, code=73)

    def test_retained_foreign_record_owner_refuses_without_mocking(self):
        task_id, assignment_id, copy = self.historic_cleaned_failure()
        record = copy.directory / 'record.json'
        original = record.stat()
        foreign_uid = (os.geteuid() + 1) % (2 ** 32)
        try:
            os.chown(record, foreign_uid, original.st_gid)
        except OSError as error:
            if error.errno in (errno.EPERM, errno.EINVAL):
                self.skipTest(
                    'worker namespace cannot represent a foreign UID; rerun on host')
            raise
        try:
            self.assertNotEqual(record.stat().st_uid, os.geteuid())
            self.assert_legacy_refused(task_id, assignment_id, copy)
        finally:
            os.chown(record, original.st_uid, original.st_gid)

    def test_complete_task_repairs_modern_cancellation_after_cleanup_crash(self):
        task_id, assignment_id, copy = self.finalizable_failure()
        original_row = dict(self.row(task_id, assignment_id))
        original_lease = workspace_lifecycle.read(copy)
        failure_fields = (
            'status', 'failure_stage', 'exit_code', 'result_summary',
            'preparation_cause', 'finished_at', 'workspace_id', 'job_id',
            'attempt_id', 'started_at', 'worker_changes', 'checks')
        original_failure = {key: original_row.get(key) for key in failure_fields}
        captures = []
        real_publish = coordination._publish_job_finalization
        publication_calls = []

        def capture(copy):
            captures.append(copy.id)
            return b'fixture captured patch\n'

        def crash_once(*arguments, **keywords):
            publication_calls.append(1)
            if len(publication_calls) == 1:
                raise RuntimeError('fixture crash after cleanup')
            return real_publish(*arguments, **keywords)

        with mock.patch.object(workspace_gc, '_capture', side_effect=capture), \
                mock.patch.object(coordination, '_publish_job_finalization',
                                  side_effect=crash_once):
            with self.assertRaises(RuntimeError):
                coordination.finalize_assignment(
                    self.source, task_id, assignment_id,
                    'cancel before the first publication', cancelled=True)

        self.assertEqual(captures, [copy.id])
        self.assertFalse(copy.path.exists())
        interrupted = self.row(task_id, assignment_id)
        self.assertNotIn('job_finalization', interrupted)
        request = interrupted['job_finalization_request']
        self.assertEqual(request['workspace_id'], copy.id)
        self.assertTrue(request['cancelled'])
        interrupted_lease = workspace_lifecycle.read(copy)
        self.assertEqual(interrupted_lease['phase'], 'cancelled')
        self.assertEqual(interrupted_lease['cleanup']['state'], 'cleaned')
        self.assertEqual(interrupted_lease['attempts'], original_lease['attempts'])

        provider_guard = AssertionError('completion must not open a provider')
        with mock.patch.object(workspace_gc, '_capture',
                               side_effect=provider_guard), \
                mock.patch.object(worker, 'load_api_key', side_effect=provider_guard), \
                mock.patch.object(relay, 'ProviderRelay', side_effect=provider_guard), \
                mock.patch.object(worker, 'execute', side_effect=provider_guard):
            completed = coordination.complete_task(self.source, task_id)

        repaired = next(item for item in completed['assignments']
                        if item['id'] == assignment_id)
        self.assertNotIn('job_finalization_request', repaired)
        self.assertEqual(repaired['job_finalization']['id'], copy.id)
        self.assertEqual(repaired['job_finalization']['job_id'],
                         original_lease['job_id'])
        self.assertEqual(repaired['job_finalization']['evidence'], request['evidence'])
        self.assertTrue(repaired['job_finalization']['cancelled'])
        self.assertEqual(
            {key: repaired.get(key) for key in failure_fields}, original_failure)
        repaired_lease = workspace_lifecycle.read(copy)
        self.assertEqual(repaired_lease['attempts'], original_lease['attempts'])
        self.assertEqual(repaired_lease['events'], interrupted_lease['events'])
        self.assertEqual(captures, [copy.id])
        self.assertEqual(completed['status'], 'completed')


if __name__ == '__main__':
    unittest.main()
