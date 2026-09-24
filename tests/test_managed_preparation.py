"""Early managed failures must close the real ledger without starting a model."""
import contextlib
import io
import json
import subprocess
import time
from types import SimpleNamespace
from unittest import mock

from codex_deepseek_team import activation, coordination, development, settings, worker, workspace
from tests import test_coordination as fixtures


class ManagedPreparationTests(fixtures.CoordinationCase):
    def plan(self, turn_id='1'):
        task = coordination.open_task(self.repo, session_id='managed-preparation', turn_id=turn_id,
                                      prompt='Review a.py', policy=self.policy())
        task = coordination.plan_task(self.repo, task['id'], {
            'classification': 'substantial',
            'deliverables': [{
                'id': 'review', 'kind': 'review', 'executor': 'worker',
                'scope': ['a.py'], 'acceptance': ['review a.py'],
                'dependencies': [], 'checks': [],
            }],
        })
        self.task_id = task['id']
        self.assignment_id = task['assignments'][0]['id']
        return SimpleNamespace(
            runtime='codex', codex='codex', claude='claude', task='Review a.py',
            state_dir=self.state, os_sandbox='required', timeout=0,
            attempts=1, attempts_explicit=False, effort='high', no_wait=False,
            workspace=None, resume_after_failure=False, access=None,
            delegation_level=None, max_workers=None,
            coord_task=self.task_id, coord_assignment=self.assignment_id,
        )

    def row(self):
        return coordination.load_task(self.repo, self.task_id)['assignments'][0]

    def commit_backup(self):
        (self.repo / '.env.backup').write_text('NONSECRET_TEST_CONTENT_DO_NOT_PRINT\n')
        subprocess.run(['git', '-C', str(self.repo), 'add', '.env.backup'], check=True)
        subprocess.run(['git', '-C', str(self.repo), '-c', 'user.name=Test',
                        '-c', 'user.email=test@example.test', 'commit', '-qm', 'backup'],
                       check=True)

    @contextlib.contextmanager
    def local_runtime(self, *, sandbox_error=None, probe_error=None, key_error=None):
        # Unit tests replace external runtime/namespace probes only. Git,
        # admission, copy creation and persistent coordination stay real.
        with contextlib.ExitStack() as stack:
            stack.enter_context(contextlib.chdir(self.repo))
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
            stack.enter_context(mock.patch.object(worker, 'resolve_runtime',
                                                 return_value=('codex', '/usr/bin/true')))
            stack.enter_context(mock.patch.object(development, 'check_runtime', return_value='test'))
            stack.enter_context(mock.patch.object(worker, 'resolve_os_sandbox',
                                                 return_value=(None, None), side_effect=sandbox_error))
            stack.enter_context(mock.patch.object(development, 'layout', return_value=['test-layout']))
            stack.enter_context(mock.patch.object(development, 'probe', side_effect=probe_error))
            stack.enter_context(mock.patch.object(development, 'missing_requirements', return_value=[]))
            stack.enter_context(mock.patch.object(worker, 'load_api_key',
                side_effect=key_error or AssertionError('provider credential read before failed preparation')))
            stack.enter_context(mock.patch.object(worker, 'execute',
                side_effect=AssertionError('model must not execute during failed preparation')))
            yield

    def assert_preparation_failed(self, code=78):
        row = self.row()
        self.assertEqual(row['status'], 'failed')
        self.assertEqual(row['queue_state'], 'finished')
        self.assertEqual(row['error_kind'], 'environment')
        self.assertEqual(row['failure_stage'], 'preparation')
        self.assertEqual(row['exit_code'], code)
        self.assertFalse(row.get('started_at'))
        self.assertEqual(row['worker_changes'], [])
        self.assertEqual(row['checks'], [])
        return row

    def test_committed_backup_closes_assignment_without_copy_or_provider(self):
        self.commit_backup()
        args = self.plan()
        with self.local_runtime(), self.assertRaises(worker.WorkerError) as caught:
            worker.run(args)
        self.assertEqual(caught.exception.code, 78)
        row = self.assert_preparation_failed()
        self.assertIn('.env.backup', row['result_summary'])
        self.assertNotIn('NONSECRET_TEST_CONTENT_DO_NOT_PRINT', json.dumps(row))
        self.assertIsNone(row.get('workspace_id'))
        self.assertFalse((self.state / 'workspaces').exists())
        coordination.use_result(self.repo, self.task_id, self.assignment_id,
                                'rejected', 'Confirmed source preparation blocker; no model ran.')
        self.assertEqual(coordination.complete_task(self.repo, self.task_id)['status'], 'completed')

    def test_sandbox_failure_is_recorded_before_copy_and_credential_access(self):
        args = self.plan()
        with self.local_runtime(sandbox_error=worker.WorkerError(78, 'sandbox unavailable')):
            with self.assertRaises(worker.WorkerError):
                worker.run(args)
        row = self.assert_preparation_failed()
        self.assertIn('sandbox unavailable', row['result_summary'])
        self.assertIsNone(row.get('workspace_id'))

    def test_runtime_failure_is_recorded_without_source_copy(self):
        args = self.plan()
        with self.local_runtime(), mock.patch.object(development, 'check_runtime',
                side_effect=development.DevelopmentError('runtime unsupported')):
            with self.assertRaises(worker.WorkerError):
                worker.run(args)
        self.assertIn('runtime unsupported', self.assert_preparation_failed()['result_summary'])

    def test_probe_failure_retains_complete_copy_for_explicit_preparation(self):
        args = self.plan()
        with self.local_runtime(probe_error=development.DevelopmentError('namespace probe failed')):
            with self.assertRaises(worker.WorkerError):
                worker.run(args)
        row = self.assert_preparation_failed()
        copy = workspace.load(self.state, row['workspace_id'])
        self.assertEqual(copy.metadata['status'], 'failed')
        self.assertEqual(copy.metadata['error_kind'], 'preparation')
        # A complete baseline can be inspected/prepared, unlike a rejected tree.
        with copy.lock(recover=True):
            self.assertEqual(copy.changes()[0], [])

    def test_missing_provider_key_is_environment_failure_before_model_start(self):
        args = self.plan()
        with self.local_runtime(key_error=worker.WorkerError(78, 'Provider credential is absent.')):
            with self.assertRaises(worker.WorkerError):
                worker.run(args)
        self.assertIn('credential is absent', self.assert_preparation_failed()['result_summary'])

    def test_failure_after_copy_allocation_links_the_quarantined_copy(self):
        args = self.plan()
        real_git = workspace.git

        def fail_fetch(path, *arguments, **options):
            if arguments[0] == 'fetch':
                raise workspace.WorkspaceError('Git preparation failed.')
            return real_git(path, *arguments, **options)

        with self.local_runtime(), mock.patch.object(workspace, 'git', side_effect=fail_fetch):
            with self.assertRaises(worker.WorkerError):
                worker.run(args)
        row = self.assert_preparation_failed()
        copy = workspace.load(self.state, row['workspace_id'])
        self.assertEqual(copy.metadata['status'], 'failed')
        self.assertFalse(copy.metadata.get('base_head'))
        self.assertFalse(copy.metadata.get('git_digest'))

    def test_filesystem_error_records_safe_reason_without_exception_payload(self):
        args = self.plan()
        with self.local_runtime(probe_error=OSError('PRIVATE_ERROR_PAYLOAD')):
            with self.assertRaises(OSError):
                worker.run(args)
        row = self.assert_preparation_failed(code=71)
        self.assertNotIn('PRIVATE_ERROR_PAYLOAD', json.dumps(row))

    def test_repeated_launch_preserves_terminal_failure_and_allocates_no_copy(self):
        self.commit_backup()
        args = self.plan()
        with self.local_runtime():
            with self.assertRaises(worker.WorkerError):
                worker.run(args)
            first = self.assert_preparation_failed()
            with self.assertRaises(worker.WorkerError):
                worker.run(args)
        self.assertEqual(self.row(), first)
        self.assertFalse((self.state / 'workspaces').exists())

    def test_resumed_partial_execution_is_not_overwritten_by_new_probe_failure(self):
        args = self.plan()
        copy = workspace.create(self.repo, self.state)
        (copy.path / 'a.py').write_text('PARTIAL = True\n')
        coordination.assignment_started(self.repo, self.task_id, self.assignment_id,
                                        copy.id, 'codex', [], effort='high')
        coordination.assignment_finished(self.repo, self.task_id, self.assignment_id,
            'failed', 'partial execution retained', ['a.py'], [], error_kind='execution', exit_code=70)
        coordination.use_result(self.repo, self.task_id, self.assignment_id,
                                'needs-rework', 'Inspected partial work before explicit continuation.')
        copy.failed('execution', 70)
        first = self.row()
        args.workspace, args.resume_after_failure = copy.id, True
        with self.local_runtime(probe_error=development.DevelopmentError('namespace unavailable now')):
            with self.assertRaises(worker.WorkerError):
                worker.run(args)
        self.assertEqual(self.row(), first)
        self.assertEqual((copy.path / 'a.py').read_text(), 'PARTIAL = True\n')
        self.assertEqual(workspace.load(self.state, copy.id).metadata['error_kind'], 'execution')

    def test_keyboard_interrupt_during_preparation_records_cancellation(self):
        args = self.plan()
        with self.local_runtime(sandbox_error=KeyboardInterrupt()), self.assertRaises(KeyboardInterrupt):
            worker.run(args)
        row = self.row()
        self.assertEqual(row['status'], 'failed')
        self.assertEqual((row['status'], row['error_kind'], row['exit_code']), ('failed', 'cancelled', 130))
        self.assertFalse(row.get('started_at'))

    def test_admission_refusal_never_grants_technical_retention(self):
        for change in ('disabled', 'revoked', 'timeout'):
            with self.subTest(change=change):
                activation.set_enabled(self.repo, True)
                settings.set_values(self.repo / settings.PROJECT_FILE, access='full-access')
                args = self.plan(change)

                def change_admission(*unused):
                    if change == 'disabled':
                        activation.set_enabled(self.repo, False)
                    elif change == 'revoked':
                        settings.set_values(self.repo / settings.PROJECT_FILE, access='read-only')
                    else:
                        args.job_deadline = time.monotonic() - 1

                with self.local_runtime(probe_error=change_admission):
                    with self.assertRaises(worker.WorkerError):
                        worker.run(args)
                row = self.row()
                self.assertEqual(row['status'], 'failed')
                self.assertEqual(row.get('preparation_cause'), 'admission')
                self.assertEqual(coordination.load_task(self.repo, self.task_id)['constraints'], [])

    def test_dependency_failure_keeps_one_specific_runner_constraint(self):
        args = self.plan()
        with self.local_runtime(), mock.patch.object(development, 'missing_requirements',
                                                    return_value=['missing-tool']):
            with self.assertRaises(worker.WorkerError):
                worker.run(args)
        self.assert_preparation_failed()
        constraints = coordination.load_task(self.repo, self.task_id)['constraints']
        self.assertEqual([row['code'] for row in constraints], ['dependency_unavailable'])

    def test_cancellation_after_copy_creation_keeps_inspectable_baseline(self):
        args = self.plan()
        with self.local_runtime(probe_error=KeyboardInterrupt()), self.assertRaises(KeyboardInterrupt):
            worker.run(args)
        row = self.row()
        self.assertEqual((row['status'], row['error_kind'], row['exit_code']), ('failed', 'cancelled', 130))
        self.assertFalse(coordination.load_task(self.repo, self.task_id)['constraints'])
        copy = workspace.load(self.state, row['workspace_id'])
        self.assertEqual((copy.metadata['status'], copy.metadata['exit_code']), ('failed', 130))
        with copy.lock(recover=True):
            self.assertEqual(copy.changes()[0], [])

    def test_losing_preparation_cannot_overwrite_another_launchs_running_copy(self):
        args = self.plan()
        winner = workspace.create(self.repo, self.state)
        expected = {}

        def another_launch_wins(*unused):
            coordination.assignment_started(self.repo, self.task_id, self.assignment_id,
                                            winner.id, 'codex', [], effort='high')
            with winner.lock():
                winner.begin('execution')
            expected['row'] = self.row()
            expected['copy'] = dict(winner.metadata)
            raise development.DevelopmentError('losing launch failed its probe')

        with self.local_runtime(probe_error=another_launch_wins):
            with self.assertRaises(worker.WorkerError):
                worker.run(args)
        self.assertEqual(self.row(), expected['row'])
        self.assertEqual(workspace.load(self.state, winner.id).metadata, expected['copy'])
        copies = [workspace.load(self.state, path.name)
                  for path in (self.state / 'workspaces').iterdir()]
        loser = next(copy for copy in copies if copy.id != winner.id)
        self.assertEqual(loser.metadata['status'], 'failed')
