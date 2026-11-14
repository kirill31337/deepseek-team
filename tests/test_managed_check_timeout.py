"""Managed check timeout validation, provenance and inheritance regressions."""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import shlex
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from codex_deepseek_team import (check_evidence, coordination, delegation_cli,
                                 development, managed, relay, runtime_preflight,
                                 settings, toolchains, worker, workspace,
                                 workspace_lifecycle)
from tests import test_coordination as fixtures
from tests.test_check_reuse import LAUNCHER, private_python


def policy():
    return settings.Policy(75, 'full-access', {
        'delegation_level': 'test',
        'access': 'test',
    })


class CheckTimeoutValidationTests(fixtures.CoordinationCase):
    def managed_args(self, **overrides):
        values = dict(
            runtime='codex', codex='codex', codex_explicit=False,
            claude='claude', claude_explicit=False, task='run checks',
            state_dir=self.state, state_dir_requested=self.state,
            os_sandbox='required', timeout=0, attempts=1,
            attempts_explicit=False, effort='high', no_wait=True,
            workspace=None, resume_after_failure=False, access=None,
            delegation_level=None, max_workers=None, coord_task=None,
            coord_assignment=None, recheck_environment=False,
            force_checks=False, check_timeout=None,
        )
        values.update(overrides)
        return SimpleNamespace(**values)

    def test_cli_rejects_non_positive_non_finite_and_oversized_values(self):
        for value in ('0', '-1', 'nan', 'inf', '-inf', '86401'):
            with self.subTest(value=value), \
                    mock.patch('sys.argv', ['worker', '--check-timeout', value, 'task']), \
                    mock.patch('sys.stderr', new_callable=io.StringIO):
                with self.assertRaises(SystemExit) as caught:
                    worker.parse_args()
                self.assertEqual(caught.exception.code, 2)

    def test_cli_and_verify_reject_invalid_values_before_provider_or_workspace_access(self):
        with mock.patch('sys.argv', ['worker', '--check-timeout', '0', 'task']), \
                mock.patch.object(worker, 'run', side_effect=AssertionError('run reached')), \
                mock.patch('sys.stderr', new_callable=io.StringIO), \
                self.assertRaises(SystemExit):
            worker.main()

        with mock.patch.object(workspace, 'load',
                               side_effect=AssertionError('workspace loaded')), \
                mock.patch('sys.stderr', new_callable=io.StringIO), \
                self.assertRaises(SystemExit):
            delegation_cli.main(['workspace', 'verify', 'a' * 32, '--timeout', '0'])

    def test_programmatic_bool_is_rejected_before_slot_copy_or_credential(self):
        args = self.managed_args(check_timeout=True)
        with mock.patch.object(worker, 'acquire_job_slot',
                               side_effect=AssertionError('slot reached')), \
                mock.patch.object(worker, 'load_api_key',
                                  side_effect=AssertionError('credential reached')), \
                mock.patch.object(workspace, 'create',
                                  side_effect=AssertionError('copy reached')):
            with self.assertRaises(worker.WorkerError) as caught:
                managed.run(args, policy(), worker)
        self.assertEqual(caught.exception.code, 64)
        with self.assertRaises(check_evidence.CheckEvidenceError) as caught:
            check_evidence.validate_check_timeout(True)
        self.assertEqual(caught.exception.code, 64)
        with self.assertRaises(check_evidence.CheckEvidenceError):
            check_evidence.validate_check_timeout('2.5')
        with self.assertRaises(check_evidence.CheckEvidenceError) as caught:
            check_evidence.validate_check_timeout(10 ** 10000)
        self.assertEqual(caught.exception.code, 64)
        with self.assertRaises(coordination.CoordinationError):
            coordination._validate_check_evidence([{
                'command': 'check', 'exit_code': 0,
                'check_timeout_seconds': 10 ** 10000}])

    def test_fractional_and_upper_bound_values_are_accepted(self):
        self.assertEqual(check_evidence.validate_check_timeout(2.5), 2.5)
        self.assertEqual(check_evidence.validate_check_timeout(900), 900.0)
        self.assertEqual(check_evidence.validate_check_timeout(86400), 86400.0)

    def test_public_cli_parses_valid_fractional_values(self):
        with mock.patch('sys.argv', ['worker', '--check-timeout', '2.5', 'task']):
            self.assertEqual(worker.parse_args().check_timeout, 2.5)
        self.assertEqual(delegation_cli._check_timeout('3.25'), 3.25)
        for value in ('0', '-1', 'nan', 'inf', '86401'):
            with self.subTest(value=value), self.assertRaises(argparse.ArgumentTypeError):
                delegation_cli._check_timeout(value)

    def test_queue_expiry_stops_before_copy_or_provider(self):
        args = self.managed_args(timeout=1)

        def delayed_slot(*_unused, **_ignored):
            time.sleep(1.1)
            return os.open(os.devnull, os.O_RDONLY)

        with contextlib.chdir(self.repo), \
                mock.patch.object(worker, 'acquire_slot', side_effect=delayed_slot), \
                mock.patch.object(worker, 'load_api_key',
                                  side_effect=AssertionError('credential reached')), \
                mock.patch.object(workspace, 'create',
                                  side_effect=AssertionError('copy reached')):
            with self.assertRaises(worker.WorkerError) as caught:
                managed.run(args, policy(), worker)
        self.assertEqual(caught.exception.code, 124)
        self.assertIn('timeout', str(caught.exception))


class ManagedCheckTimeoutTests(fixtures.CoordinationCase):
    def setUp(self):
        super().setUp()
        self.copy = workspace.create(self.repo, self.state)
        self.python = private_python(self.root, 'timeout-command-python')
        self.runtime_python = private_python(self.root, 'timeout-runtime-python')
        self.counter = self.root / 'managed-timeout-counter'
        self.probe_counter = self.root / 'managed-timeout-probe-counter'
        self.check = self.command(self.counter)
        self.probe = self.command(self.probe_counter)
        self.model_calls = 0

    def command(self, counter):
        code = ('from pathlib import Path\n'
                f'p = Path({str(counter)!r})\n'
                "p.write_text(str(int(p.read_text()) + 1) if p.exists() else '1')")
        return f'{shlex.quote(str(self.python))} -c {shlex.quote(code)}'

    def count(self, path):
        return int(path.read_text()) if path.exists() else 0

    def plan(self, suffix, *, correction_of=None, checks=None):
        task = coordination.open_task(
            self.repo, session_id='timeout-' + suffix, turn_id='1',
            prompt='run timeout checks', policy=policy())
        deliverable = {
            'id': 'one', 'kind': 'implementation', 'scope': ['a.py'],
            'executor': 'worker', 'acceptance': ['checks pass'],
            'dependencies': [], 'checks': list(checks or [self.check]),
            'checks_reusable': True,
        }
        if correction_of is not None:
            deliverable['correction_of'] = dict(correction_of)
        planned = coordination.plan_task(self.repo, task['id'], {
            'classification': 'substantial',
            'deliverables': [deliverable],
        })
        return task, planned['assignments'][0]['id']

    def run_managed(self, task, assignment, *, check_timeout=None,
                    total_timeout=0, model_code=0, resume=False,
                    force_checks=False, key_error=None):
        env = {
            'PATH': os.environ.get('PATH', '/usr/local/bin:/usr/bin:/bin'),
            'LANG': 'C.UTF-8',
            'HOME': str(self.root / 'managed-runtime-home'),
            'TMPDIR': str(self.root / 'managed-runtime-tmp'),
            'PYTHONHOME': sys.base_prefix,
            'DEEPSEEK_API_KEY': 'canary-managed-timeout-key',
        }
        (self.root / 'managed-runtime-tmp').mkdir(exist_ok=True)

        class Relay:
            def __init__(self, *unused, **unused_keywords):
                self.failures = []

            def __enter__(self):
                return self

            def __exit__(self, *unused):
                return False

        def execute(*_unused, **_ignored):
            self.model_calls += 1
            if model_code:
                return model_code, '', 'fixture model failure'
            result = '\n'.join((
                json.dumps({'type': 'item.completed',
                            'item': {'type': 'agent_message', 'text': 'NO_PROJECT_CHANGE'}}),
                json.dumps({'type': 'turn.completed'}),
            ))
            return 0, result, ''

        args = SimpleNamespace(
            runtime='codex', codex='codex', codex_explicit=False,
            claude='claude', claude_explicit=False, task='run checks',
            state_dir=self.state, state_dir_requested=self.state,
            os_sandbox='required', timeout=total_timeout, attempts=1,
            attempts_explicit=False, effort='high', no_wait=False,
            workspace=self.copy.id, resume_after_failure=resume,
            access=None, delegation_level=None, max_workers=None,
            coord_task=task['id'], coord_assignment=assignment,
            recheck_environment=False, force_checks=force_checks,
            check_timeout=check_timeout,
        )
        key_patch = (mock.patch.object(worker, 'load_api_key', side_effect=key_error)
                     if key_error is not None else
                     mock.patch.object(worker, 'load_api_key', return_value='test-provider-key'))
        with contextlib.chdir(self.repo), \
                mock.patch.object(worker, 'resolve_os_sandbox', return_value=(None, None)), \
                mock.patch.object(runtime_preflight, 'select_runtime',
                    return_value=runtime_preflight.RuntimeSelection(
                        runtime='codex', binary=str(self.runtime_python),
                        layout=[LAUNCHER], environment=env)), \
                mock.patch.object(toolchains, 'probes_for', return_value=[self.probe]), \
                key_patch, \
                mock.patch.object(worker, 'execute', side_effect=execute), \
                mock.patch.object(relay, 'ProviderRelay', Relay):
            code = managed.run(args, policy(), worker, self.copy)
        self.copy = workspace.load(self.state, self.copy.id)
        return code

    def rows(self, task, assignment):
        return next(item for item in coordination.load_task(
            self.repo, task['id'])['assignments'] if item['id'] == assignment)

    def test_failed_resume_baseline_records_new_attempt_and_requires_review(self):
        flag = self.root / 'slow-resume-check'
        code = ('from pathlib import Path\nimport time\n'
                f'time.sleep(2) if Path({str(flag)!r}).exists() else None')
        command = f'{shlex.quote(str(self.python))} -c {shlex.quote(code)}'
        task, assignment = self.plan('resume-baseline-timeout', checks=[command])
        self.assertEqual(self.run_managed(
            task, assignment, check_timeout=5, model_code=9), 9)
        coordination.use_result(
            self.repo, task['id'], assignment, 'needs-rework',
            'failed model attempt inspected before explicit resume')
        previous = self.rows(task, assignment)
        flag.write_text('slow\n')
        with self.assertRaises(worker.WorkerError) as caught:
            self.run_managed(task, assignment, check_timeout=0.3, resume=True)
        self.assertEqual(caught.exception.code, 124)
        row = self.rows(task, assignment)
        attempt = workspace_lifecycle.read(self.copy)['attempts'][-1]
        self.assertNotEqual(row['attempt_id'], previous['attempt_id'])
        self.assertEqual(row['attempt_id'], attempt['id'])
        self.assertEqual(row['check_timeout_seconds'], 0.3)
        self.assertEqual(row['status'], 'failed')
        self.assertEqual(row['failure_stage'], 'preparation')
        self.assertIsNone(row.get('disposition'))
        self.assertIsNone(row.get('started_at'))
        self.assertIsNone(row.get('execution_snapshot'))
        self.assertEqual(row['attempt_history'][-1]['attempt_id'], previous['attempt_id'])
        self.assertEqual(row['attempt_history'][-1]['disposition'], previous['disposition'])
        self.assertEqual(row['routing_feedback'], previous['routing_feedback'])
        self.assertEqual(self.model_calls, 1)
        repeated = coordination.assignment_preparation_failed(
            self.repo, task['id'], assignment, 'same baseline failure',
            exit_code=124, workspace_id=self.copy.id,
            attempt_id=attempt['id'], check_timeout_seconds=0.3,
            resume_after_failure=True)
        self.assertEqual(next(item for item in repeated['assignments']
                              if item['id'] == assignment), row)
        flag.unlink()
        with self.assertRaises(worker.WorkerError):
            self.run_managed(task, assignment, resume=True)
        coordination.use_result(
            self.repo, task['id'], assignment, 'needs-rework',
            'baseline timeout inspected and slow input removed')
        self.assertEqual(self.run_managed(task, assignment, resume=True), 0)
        row = self.rows(task, assignment)
        self.assertEqual(row['check_timeout_seconds'], 0.3)
        self.assertEqual(len(row['attempt_history']), 2)
        self.assertEqual(len(workspace_lifecycle.read(self.copy)['attempts']), 3)
        self.assertEqual(self.model_calls, 2)

    def test_first_baseline_failure_with_explicit_recovery_records_first_attempt(self):
        command = f'{shlex.quote(str(self.python))} -c "import time; time.sleep(2)"'
        task, assignment = self.plan('planned-recovery-baseline', checks=[command])
        with self.assertRaises(worker.WorkerError) as caught:
            self.run_managed(task, assignment, resume=True, check_timeout=0.2,
                             key_error=AssertionError('credential reached'))
        self.assertEqual(caught.exception.code, 124)
        row = self.rows(task, assignment)
        attempts = workspace_lifecycle.read(self.copy)['attempts']
        self.assertEqual(len(attempts), 1)
        self.assertEqual(row['attempt_id'], attempts[0]['id'])
        self.assertEqual(row.get('attempt_history', []), [])
        self.assertEqual(row['status'], 'failed')
        self.assertEqual(row['failure_stage'], 'preparation')
        self.assertEqual(row['check_timeout_seconds'], 0.2)
        self.assertIsNone(row.get('disposition'))
        self.assertIsNone(row.get('started_at'))
        self.assertEqual(self.model_calls, 0)

    def test_failed_post_check_retains_timeout_and_evidence_history(self):
        counter = self.root / 'managed-timeout-post-failure'
        code = ('from pathlib import Path\n'
                f'p = Path({str(counter)!r})\n'
                "seen = int(p.read_text()) if p.exists() else 0\n"
                "p.write_text(str(seen + 1))\n"
                "raise SystemExit(65 if seen == 1 else 0)")
        failing_check = (
            f'{shlex.quote(str(self.python))} -c {shlex.quote(code)}')
        task, assignment = self.plan('post-failure', checks=[failing_check])
        self.assertEqual(self.run_managed(
            task, assignment, check_timeout=7.5), 65)
        self.assertEqual(self.count(counter), 2)
        self.assertEqual(self.copy.metadata['status'], 'failed')
        self.assertEqual(
            [row['exit_code'] for row in self.copy.metadata['baseline_checks']],
            [0])
        self.assertEqual(
            [row['exit_code'] for row in self.copy.metadata['checks']], [65])
        self.assertEqual(
            [row['check_timeout_seconds']
             for row in self.copy.metadata['checks']], [7.5])
        attempt = workspace_lifecycle.read(self.copy)['attempts'][-1]
        self.assertEqual(attempt['status'], 'failed')
        self.assertEqual(attempt['check_timeout_seconds'], 7.5)
        row = self.rows(task, assignment)
        self.assertEqual(row['status'], 'failed')
        self.assertEqual([item['exit_code'] for item in row['checks']], [65])
        self.assertEqual(
            [item['check_timeout_seconds'] for item in row['checks']], [7.5])

        coordination.use_result(
            self.repo, task['id'], assignment, 'needs-rework',
            'inspect the failed post check and continue explicitly')
        self.assertEqual(self.run_managed(task, assignment, resume=True), 0)
        self.assertEqual(self.count(counter), 3)
        resumed = self.rows(task, assignment)
        failed = resumed['attempt_history'][0]
        self.assertEqual([item['exit_code'] for item in failed['checks']], [65])
        self.assertEqual(
            [item['check_timeout_seconds'] for item in failed['checks']], [7.5])
        self.assertEqual(
            [item['check_timeout_seconds'] for item in resumed['checks']], [7.5])
        self.assertEqual(
            [item['check_timeout_seconds']
             for item in workspace_lifecycle.read(self.copy)['attempts']],
            [7.5, 7.5])

    def test_metadata_attempt_conflict_refuses_before_baseline_or_provider(self):
        task, assignment = self.plan('managed-conflict')
        with self.copy.lock():
            self.copy.begin('execution', check_timeout_seconds=42.0)
            self.copy.finish('succeeded')
        with self.copy.lock():
            self.copy.metadata['check_timeout_seconds'] = 43.0
            self.copy.save()
        with mock.patch.object(development, 'run_checks',
                               side_effect=AssertionError('baseline reached')), \
                mock.patch.object(worker, 'load_api_key',
                                  side_effect=AssertionError('credential reached')):
            with self.assertRaises(worker.WorkerError) as caught:
                self.run_managed(task, assignment)
        self.assertEqual(caught.exception.code, 73)
        self.assertIn('conflict', str(caught.exception))
        self.assertEqual(self.model_calls, 0)

    def test_default_is_1800_for_checks_attempt_metadata_and_evidence(self):
        task, assignment = self.plan('default')
        self.assertEqual(self.run_managed(task, assignment), 0)
        row = self.rows(task, assignment)
        lease = workspace_lifecycle.read(self.copy)
        attempt = lease['attempts'][-1]
        self.assertEqual(self.copy.metadata['check_timeout_seconds'], 1800.0)
        self.assertEqual(attempt['check_timeout_seconds'], 1800.0)
        self.assertEqual(row['execution_snapshot']['execution']['check_timeout_seconds'], 1800.0)
        for check_row in [*self.copy.metadata['baseline_checks'],
                          *self.copy.metadata['checks']]:
            self.assertEqual(check_row['check_timeout_seconds'], 1800.0)
        for probe_row in self.copy.metadata['readiness_checks']:
            self.assertEqual(probe_row['check_timeout_seconds'], 120.0)

    def test_baseline_entry_observes_durable_timeout_attempt_and_coordination_identity(self):
        task, assignment = self.plan('baseline-boundary')
        real_run_checks = development.run_checks
        observed = []

        def inspect_boundary(*args, **kwargs):
            if kwargs.get('phase') == 'baseline':
                lease = workspace_lifecycle.read(self.copy)
                attempt = lease['attempts'][-1]
                row = self.rows(task, assignment)
                observed.append(attempt['id'])
                self.assertEqual(self.copy.metadata['status'], 'running')
                self.assertEqual(self.copy.metadata['operation'], 'execution')
                self.assertEqual(self.copy.metadata['check_timeout_seconds'], 900.0)
                self.assertEqual(self.copy.metadata['attempt_id'], attempt['id'])
                self.assertEqual(attempt['operation'], 'execution')
                self.assertEqual(attempt['status'], 'running')
                self.assertEqual(attempt['check_timeout_seconds'], 900.0)
                self.assertNotIn('pending_attempt', lease)
                self.assertEqual(len(lease['attempts']), 1)
                self.assertEqual(row['status'], 'planned')
                self.assertEqual(row['workspace_id'], self.copy.id)
                self.assertEqual(row['job_id'], lease['job_id'])
                self.assertIsNone(row.get('attempt_id'))
                self.assertIsNone(row.get('execution_snapshot'))
                self.assertNotIn('check_timeout_seconds', row)
                self.assertEqual(self.model_calls, 0)
            return real_run_checks(*args, **kwargs)

        with mock.patch.object(development, 'run_checks', side_effect=inspect_boundary):
            self.assertEqual(self.run_managed(
                task, assignment, check_timeout=900), 0)
        self.assertEqual(len(observed), 1)
        final_lease = workspace_lifecycle.read(self.copy)
        self.assertEqual(final_lease['attempts'][-1]['status'], 'succeeded')
        self.assertEqual(len(final_lease['attempts']), 1)
        self.assertNotIn('pending_attempt', final_lease)
        row = self.rows(task, assignment)
        self.assertEqual(row['attempt_id'], final_lease['attempts'][-1]['id'])
        self.assertEqual(row['check_timeout_seconds'], 900.0)

    def test_timed_out_baseline_before_provider_is_neutral_and_resumes_with_same_timeout(self):
        task, assignment = self.plan('interrupted-baseline')
        real_run_checks = development.run_checks
        interrupted = {'baseline': True}

        def interrupt_first_baseline(*args, **kwargs):
            if kwargs.get('phase') == 'baseline' and interrupted['baseline']:
                interrupted['baseline'] = False
                commands = args[2]
                return [{'command': commands[0], 'exit_code': 124, 'timed_out': True}]
            return real_run_checks(*args, **kwargs)

        with mock.patch.object(development, 'run_checks',
                               side_effect=interrupt_first_baseline):
            with self.assertRaises(worker.WorkerError) as caught:
                self.run_managed(task, assignment, check_timeout=900)
        self.assertEqual(caught.exception.code, 124)
        self.assertEqual(self.model_calls, 0)

        self.copy = workspace.load(self.state, self.copy.id)
        row = self.rows(task, assignment)
        lease = workspace_lifecycle.read(self.copy)
        attempt = lease['attempts'][-1]
        self.assertEqual(self.copy.metadata['status'], 'failed')
        self.assertEqual(self.copy.metadata['check_timeout_seconds'], 900.0)
        self.assertEqual(attempt['status'], 'failed')
        self.assertEqual(attempt['check_timeout_seconds'], 900.0)
        self.assertEqual(row['status'], 'failed')
        self.assertEqual(row['error_kind'], 'environment')
        self.assertEqual(row['exit_code'], 124)
        self.assertEqual(row['attempt_id'], attempt['id'])

        coordination.use_result(
            self.repo, task['id'], assignment, 'needs-rework',
            'inspect the neutral baseline timeout and continue explicitly')
        with mock.patch.object(development, 'run_checks',
                               side_effect=interrupt_first_baseline):
            self.assertEqual(self.run_managed(
                task, assignment, resume=True), 0)
        leases = workspace_lifecycle.read(self.copy)['attempts']
        self.assertEqual([item['check_timeout_seconds'] for item in leases],
                         [900.0, 900.0])
        resumed = self.rows(task, assignment)
        self.assertEqual(resumed['attempt_history'][0]['error_kind'], 'environment')
        self.assertEqual(resumed['attempt_history'][0]['check_timeout_seconds'], 900.0)
        self.assertEqual(self.model_calls, 1)

    def test_failed_baseline_never_reads_provider_or_starts_assignment(self):
        task, assignment = self.plan('key-after-baseline')
        real_run_checks = development.run_checks
        observed = {}

        def timeout_baseline(*args, **kwargs):
            if kwargs.get('phase') == 'baseline':
                observed.update(deadline=kwargs.get('deadline'), called_at=time.monotonic())
                return [{'command': args[2][0], 'exit_code': 124, 'timed_out': True}]
            return real_run_checks(*args, **kwargs)

        with mock.patch.object(development, 'run_checks', side_effect=timeout_baseline):
            with self.assertRaises(worker.WorkerError) as caught:
                self.run_managed(
                    task, assignment, check_timeout=900, total_timeout=10,
                    key_error=AssertionError('credential read before the baseline completed'))
        self.assertEqual(caught.exception.code, 124)
        self.assertEqual(self.model_calls, 0)
        self.assertIsNotNone(observed['deadline'])
        remaining = observed['deadline'] - observed['called_at']
        self.assertGreater(remaining, 0)
        self.assertLess(remaining, 10)

        row = self.rows(task, assignment)
        lease = workspace_lifecycle.read(self.copy)
        attempt = lease['attempts'][-1]
        self.assertEqual(row['status'], 'failed')
        self.assertEqual(row['error_kind'], 'environment')
        self.assertEqual(row['failure_stage'], 'preparation')
        self.assertEqual(row['exit_code'], 124)
        self.assertEqual(row['workspace_id'], self.copy.id)
        self.assertEqual(row['job_id'], lease['job_id'])
        self.assertEqual(row['attempt_id'], attempt['id'])
        self.assertEqual(row['check_timeout_seconds'], 900.0)
        self.assertIsNone(row.get('started_at'))
        self.assertIsNone(row.get('execution_snapshot'))
        self.assertEqual(row['checks'], [])
        self.assertEqual(row['baseline_checks'][0]['exit_code'], 124)
        self.assertEqual(attempt['operation'], 'execution')
        self.assertEqual(attempt['status'], 'failed')
        self.assertEqual(attempt['check_timeout_seconds'], 900.0)
        self.assertNotIn('pending_attempt', lease)
        self.assertEqual(len(lease['attempts']), 1)
        self.assertEqual(self.copy.metadata['status'], 'failed')
        self.assertEqual(self.copy.metadata['attempt_id'], attempt['id'])
        self.assertEqual(self.copy.metadata['check_timeout_seconds'], 900.0)

    def test_assignment_started_adopts_only_the_exact_running_attempt(self):
        task, assignment = self.plan('attempt-adoption')
        with self.copy.lock():
            coordination.assignment_preparing(self.repo, task['id'], assignment, self.copy)
            self.copy.begin('execution', check_timeout_seconds=900.0)
            started = coordination.assignment_started(
                self.repo, task['id'], assignment, self.copy.id, 'codex', [],
                effort='high', check_timeout_seconds=900.0,
                attempt_id=self.copy.metadata['attempt_id'])
        row = next(item for item in started['assignments'] if item['id'] == assignment)
        lease = workspace_lifecycle.read(self.copy)
        self.assertEqual(row['status'], 'running')
        self.assertEqual(row['attempt_id'], lease['attempts'][0]['id'])
        self.assertEqual(row['check_timeout_seconds'], 900.0)
        self.assertEqual(
            row['execution_snapshot']['execution']['attempt_id'], lease['attempts'][0]['id'])
        self.assertNotIn('pending_attempt', lease)
        self.assertEqual(len(lease['attempts']), 1)

        ledger_before = coordination._task_path(self.repo, task['id']).read_bytes()
        lease_before = (self.copy.directory / 'lease.json').read_bytes()
        with self.assertRaises(coordination.CoordinationError) as caught:
            coordination.assignment_started(
                self.repo, task['id'], assignment, self.copy.id, 'codex', [],
                effort='high', check_timeout_seconds=901.0,
                attempt_id=self.copy.metadata['attempt_id'])
        self.assertEqual(caught.exception.code, 73)
        self.assertEqual(coordination._task_path(self.repo, task['id']).read_bytes(), ledger_before)
        self.assertEqual((self.copy.directory / 'lease.json').read_bytes(), lease_before)

        with mock.patch.object(workspace_lifecycle.uuid, 'uuid4') as new_attempt:
            with self.assertRaises(coordination.CoordinationError) as caught:
                coordination.assignment_started(
                    self.repo, task['id'], assignment, self.copy.id, 'codex', [],
                    effort='high', check_timeout_seconds=900.0, attempt_id='f' * 32)
        self.assertEqual(caught.exception.code, 73)
        new_attempt.assert_not_called()
        self.assertEqual(coordination._task_path(self.repo, task['id']).read_bytes(), ledger_before)
        self.assertEqual((self.copy.directory / 'lease.json').read_bytes(), lease_before)

    def test_readiness_failure_creates_no_execution_attempt_or_pending_reservation(self):
        task, assignment = self.plan('readiness-before-attempt')
        with mock.patch.object(development, 'missing_requirements',
                               return_value=['missing-tool']):
            with self.assertRaises(worker.WorkerError) as caught:
                self.run_managed(task, assignment, check_timeout=900)
        self.assertEqual(caught.exception.code, 78)
        self.assertEqual(self.model_calls, 0)
        self.copy = workspace.load(self.state, self.copy.id)
        lease = workspace_lifecycle.read(self.copy)
        self.assertEqual(lease['attempts'], [])
        self.assertNotIn('pending_attempt', lease)
        self.assertNotIn('check_timeout_seconds', self.copy.metadata)
        row = self.rows(task, assignment)
        self.assertEqual(row['status'], 'failed')
        self.assertEqual(row['failure_stage'], 'preparation')
        self.assertIsNone(row.get('started_at'))

    def test_explicit_900_is_recorded_for_baseline_post_and_attempt_but_not_probe(self):
        task, assignment = self.plan('explicit-900')
        self.assertEqual(self.run_managed(
            task, assignment, check_timeout=900), 0)
        row = self.rows(task, assignment)
        lease = workspace_lifecycle.read(self.copy)
        attempt = lease['attempts'][-1]
        self.assertEqual(self.copy.metadata['check_timeout_seconds'], 900.0)
        self.assertEqual(attempt['check_timeout_seconds'], 900.0)
        self.assertEqual(row['execution_snapshot']['execution']['check_timeout_seconds'], 900.0)
        self.assertEqual([item['check_timeout_seconds']
                          for item in self.copy.metadata['baseline_checks']], [900.0])
        self.assertEqual([item['check_timeout_seconds']
                          for item in self.copy.metadata['checks']], [900.0])
        self.assertEqual([item['check_timeout_seconds']
                          for item in self.copy.metadata['readiness_checks']], [120.0])

    def test_fractional_resume_without_override_inherits_execution_attempt(self):
        task, assignment = self.plan('fractional-resume')
        self.assertEqual(self.run_managed(
            task, assignment, check_timeout=2.5, model_code=9), 9)
        first = workspace_lifecycle.read(self.copy)['attempts'][-1]
        self.assertEqual(first['check_timeout_seconds'], 2.5)
        coordination.use_result(
            self.repo, task['id'], assignment, 'needs-rework',
            'inspect the failed attempt and resume explicitly')
        self.assertEqual(self.run_managed(
            task, assignment, resume=True), 0)
        attempts = workspace_lifecycle.read(self.copy)['attempts']
        self.assertEqual([item['check_timeout_seconds'] for item in attempts],
                         [2.5, 2.5])
        self.assertEqual(self.copy.metadata['check_timeout_seconds'], 2.5)
        self.assertEqual([item['check_timeout_seconds']
                          for item in self.copy.metadata['baseline_checks']], [2.5])
        self.assertEqual([item['check_timeout_seconds']
                          for item in self.copy.metadata['checks']], [2.5])

    def test_linked_correction_inherits_attempt_and_changed_override_misses_reuse(self):
        first_task, first_assignment = self.plan('correction-base')
        self.assertEqual(self.run_managed(
            first_task, first_assignment, check_timeout=3.5), 0)
        coordination.use_result(
            self.repo, first_task['id'], first_assignment, 'needs-rework',
            'linked correction required')
        reference = {'task_id': first_task['id'], 'assignment_id': first_assignment}
        correction_task, correction_assignment = self.plan(
            'correction-inherit', correction_of=reference)
        self.assertEqual(self.run_managed(
            correction_task, correction_assignment), 0)
        inherited = workspace_lifecycle.read(self.copy)['attempts'][-1]
        self.assertEqual(inherited['check_timeout_seconds'], 3.5)
        self.assertEqual([item['check_timeout_seconds']
                          for item in self.copy.metadata['baseline_checks']], [3.5])

        coordination.use_result(
            self.repo, correction_task['id'], correction_assignment, 'needs-rework',
            'a changed timeout must not reuse the old evidence')
        second_reference = {
            'task_id': correction_task['id'],
            'assignment_id': correction_assignment,
        }
        changed_task, changed_assignment = self.plan(
            'correction-override', correction_of=second_reference)
        before = self.count(self.counter)
        self.assertEqual(self.run_managed(
            changed_task, changed_assignment, check_timeout=4.5), 0)
        self.assertEqual(self.count(self.counter), before + 2)
        self.assertEqual([item['check_timeout_seconds']
                          for item in self.copy.metadata['baseline_checks']], [4.5])
        self.assertEqual([item['check_timeout_seconds']
                          for item in self.copy.metadata['checks']], [4.5])
        self.assertFalse(self.copy.metadata['baseline_checks'][0]['reused'])
        self.assertFalse(self.copy.metadata['checks'][0]['reused'])


class WorkspaceVerifyTimeoutTests(fixtures.CoordinationCase):
    def setUp(self):
        super().setUp()
        self.copy = workspace.create(self.repo, self.state)
        with self.copy.lock():
            self.copy.begin('execution', check_timeout_seconds=42.0)
            self.copy.finish('succeeded')
        self.preflight_calls = []

    def verify(self, timeout=None, *, expect_calls=None, copy=None):
        calls = []
        selection = runtime_preflight.RuntimeSelection(
            runtime='codex', binary='codex', layout=[LAUNCHER],
            environment={'PATH': os.environ.get('PATH', '/usr/bin:/bin')})

        def select(*_args, **kwargs):
            self.preflight_calls.append(dict(kwargs))
            return selection

        def run_checks(_layout, _env, commands, timeout=120, **kwargs):
            calls.append((kwargs.get('phase'), timeout))
            return [{'command': commands[0], 'exit_code': 0,
                     'check_timeout_seconds': timeout}]

        with mock.patch.object(worker, 'resolve_os_sandbox', return_value=(None, None)), \
                mock.patch.object(runtime_preflight, 'select_runtime',
                                  side_effect=select), \
                mock.patch.object(toolchains, 'apply_saved_recipe'), \
                mock.patch.object(toolchains, 'probes_for', return_value=['probe']), \
                mock.patch.object(development, 'run_checks', side_effect=run_checks):
            result = workspace.verify_checks(
                copy or self.copy, ['check'], timeout=timeout, reuse_checks=False)
        if expect_calls is not None:
            self.assertEqual(calls, expect_calls)
        return result, calls

    def test_no_flag_inherits_execution_timeout_for_checks_but_probes_stay_120(self):
        before = workspace_lifecycle.read(self.copy)
        result, _calls = self.verify(
            expect_calls=[('verify-probe', 120.0), ('verify', 42.0)])
        self.assertEqual(
            [item.get('check_timeout_seconds') for item in result['checks']],
            [42.0])
        after = workspace_lifecycle.read(self.copy)
        self.assertEqual(after['attempts'], before['attempts'])
        self.assertEqual(self.copy.metadata['check_timeout_seconds'], 42.0)

    def test_explicit_verify_timeout_controls_checks_and_probes_but_is_not_inherited(self):
        self.verify(timeout=7,
                    expect_calls=[('verify-probe', 7.0), ('verify', 7.0)])
        self.assertTrue(self.preflight_calls)
        self.assertIsNone(self.preflight_calls[-1].get('deadline'))
        self.assertEqual(
            workspace_lifecycle.execution_check_timeout(self.copy), 42.0)
        self.assertEqual(self.copy.metadata['check_timeout_seconds'], 42.0)
        attempt = workspace_lifecycle.read(self.copy)['attempts'][-1]
        self.assertEqual(attempt['check_timeout_seconds'], 42.0)

    def test_legacy_execution_attempt_without_provenance_defaults_to_120(self):
        legacy = workspace.create(self.repo, self.state)
        with legacy.lock():
            legacy.begin('execution')
            legacy.finish('succeeded')
        self.assertNotIn('check_timeout_seconds', legacy.metadata)
        self.assertNotIn(
            'check_timeout_seconds', workspace_lifecycle.read(legacy)['attempts'][-1])
        self.assertEqual(workspace_lifecycle.execution_check_timeout(legacy), 120.0)
        self.verify(expect_calls=[('verify-probe', 120.0), ('verify', 120.0)],
                    copy=legacy)

    def test_verify_without_execution_history_defaults_to_1800_checks_and_120_probes(self):
        fresh = workspace.create(self.repo, self.state)
        self.assertNotIn('check_timeout_seconds', fresh.metadata)
        result, _calls = self.verify(
            expect_calls=[('verify-probe', 120.0), ('verify', 1800.0)],
            copy=fresh)
        self.assertEqual(
            [item['check_timeout_seconds'] for item in result['checks']], [1800.0])
        self.assertEqual(workspace_lifecycle.read(fresh)['attempts'], [])

    def test_huge_attempt_timeout_is_a_controlled_malformed_lease_error(self):
        row = workspace_lifecycle.read(self.copy)
        row['attempts'][-1]['check_timeout_seconds'] = 10 ** 10000
        with self.assertRaises(workspace.WorkspaceError):
            workspace_lifecycle.validate(row, self.copy.id)

    def test_metadata_attempt_conflict_refuses_before_checks_run(self):
        with self.copy.lock():
            self.copy.metadata['check_timeout_seconds'] = 43.0
            self.copy.save()
        with mock.patch.object(development, 'run_checks',
                               side_effect=AssertionError('checks reached')):
            with self.assertRaises(workspace.WorkspaceError) as caught:
                workspace.verify_checks(self.copy, ['check'], timeout=None)
        self.assertIn('conflict', str(caught.exception))




if __name__ == '__main__':
    unittest.main()
