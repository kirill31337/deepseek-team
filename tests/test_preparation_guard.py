"""Negative preparation memory for well-identified pre-provider failures.

The guard is separate from the positive check cache: it remembers only an
unchanged environment/source/toolchain condition, never a task or workspace id,
and it is consulted before a managed job allocates another copy or reads a
provider credential.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

from codex_deepseek_team import (coordination, development, managed,
                                 preparation_guard, runtime_preflight, settings,
                                 toolchains, worker, workspace)
from tests import test_coordination as fixtures


class GuardFixture(fixtures.CoordinationCase):
    def disposable_runtime(self, name='disposable-codex', marker='one'):
        binary = self.root / name
        if not binary.exists():
            binary.write_text('#!/bin/sh\n# %s\nexit 0\n' % marker)
            binary.chmod(0o755)
        return str(binary)

    def tool_source(self, marker='one'):
        software = self.root / 'software'
        (software / 'bin').mkdir(parents=True, exist_ok=True)
        (software / 'bin' / 'tool').write_text(marker)
        return software

    def plan(self, turn_id='1', *, checks=None, dependencies=None):
        task = coordination.open_task(
            self.repo, session_id='preparation-guard', turn_id=turn_id,
            prompt='Review a.py', policy=self.policy())
        task = coordination.plan_task(self.repo, task['id'], {
            'classification': 'substantial',
            'deliverables': [{
                'id': 'review', 'kind': 'review', 'executor': 'worker',
                'scope': ['a.py'], 'acceptance': ['review a.py'],
                'dependencies': list(dependencies or []),
                'checks': list(checks or []),
            }],
        })
        return task

    def assignment(self, task):
        return task['assignments'][0]['id']

    def args(self, task=None, **changes):
        args = SimpleNamespace(
            runtime='codex', codex=self.disposable_runtime(), codex_explicit=True,
            claude='claude', claude_explicit=False, task='Review a.py',
            state_dir=self.state, state_dir_requested=self.state, os_sandbox='required',
            timeout=0, attempts=1, attempts_explicit=False, effort='high', no_wait=False,
            workspace=None, resume_after_failure=False, access=None,
            delegation_level=None, max_workers=None, recheck_environment=False,
            job_deadline=None,
        )
        if task is not None:
            args.coord_task = task['id']
            args.coord_assignment = self.assignment(task)
        else:
            args.coord_task = None
            args.coord_assignment = None
        for key, value in changes.items():
            setattr(args, key, value)
        return args

    @contextlib.contextmanager
    def failing_runtime(self, error=None):
        error = error or runtime_preflight.RuntimePreflightError(
            'runtime readiness failed', 78)
        with mock.patch.object(worker, 'resolve_os_sandbox', return_value=(None, None)), \
                mock.patch.object(runtime_preflight, 'select_runtime', side_effect=error), \
                mock.patch.object(worker, 'load_api_key',
                                  side_effect=AssertionError('provider key read')), \
                mock.patch.object(worker, 'execute',
                                  side_effect=AssertionError('model executed')):
            yield

    @contextlib.contextmanager
    def successful_runtime(self):
        selection = runtime_preflight.RuntimeSelection(
            runtime='codex', binary=self.disposable_runtime(),
            layout=['test-layout'], environment={'PATH': '/usr/bin'})
        with mock.patch.object(worker, 'resolve_os_sandbox', return_value=(None, None)), \
                mock.patch.object(runtime_preflight, 'select_runtime',
                                  return_value=selection), \
                mock.patch.object(development, 'run_checks', return_value=[]), \
                mock.patch.object(development, 'missing_requirements', return_value=[]):
            yield

    def run_worker(self, args):
        with contextlib.chdir(self.repo), contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            return worker.run(args)

    def guard_records(self):
        directory = self.state / preparation_guard.DIRECTORY
        if not directory.exists():
            return []
        return sorted(directory.glob('*.json'))

    def worker_conditions(self, declarations=None):
        return preparation_guard.worker_conditions(
            self.repo, runtime='codex', codex=self.disposable_runtime(),
            claude='claude', codex_explicit=True, claude_explicit=False,
            state=self.state,
            declarations=declarations or {'dependencies': [], 'checks': []})


class RepeatedManagedFailureTests(GuardFixture):
    def test_sandbox_failure_is_remembered_before_any_copy(self):
        first_task = self.plan('sandbox-first')
        with mock.patch.object(worker, 'resolve_os_sandbox',
                               side_effect=worker.WorkerError(
                                   78, 'bubblewrap unavailable')):
            with self.assertRaises(worker.WorkerError):
                self.run_worker(self.args(first_task))
        first_row = coordination.load_task(
            self.repo, first_task['id'])['assignments'][0]
        self.assertFalse(first_row.get('workspace_id'))
        self.assertEqual(len(self.guard_records()), 1)

        second_task = self.plan('sandbox-second')
        with mock.patch.object(workspace, 'create',
                               side_effect=AssertionError('workspace allocated')), \
                mock.patch.object(worker, 'resolve_os_sandbox',
                                  side_effect=AssertionError('sandbox re-probed')), \
                mock.patch.object(worker, 'load_api_key',
                                  side_effect=AssertionError('provider key read')):
            with self.assertRaises(worker.WorkerError) as caught:
                self.run_worker(self.args(second_task))
        self.assertIn('did not allocate a workspace', caught.exception.message)
        self.assertFalse((self.state / 'workspaces').exists())

    def test_unchanged_new_job_stops_before_allocation_probe_and_provider(self):
        first_task = self.plan('first')
        with self.failing_runtime(), self.assertRaises(worker.WorkerError):
            self.run_worker(self.args(first_task))
        first_row = coordination.load_task(self.repo, first_task['id'])['assignments'][0]
        self.assertEqual(first_row['status'], 'failed')
        original_id = first_row['workspace_id']
        self.assertTrue(original_id)
        original = workspace.load(self.state, original_id)
        self.assertEqual(original.metadata['status'], 'failed')

        second_task = self.plan('second')
        second_args = self.args(second_task)
        with mock.patch.object(workspace, 'create',
                               side_effect=AssertionError('workspace allocated')) as create, \
                mock.patch.object(worker, 'resolve_os_sandbox',
                                  side_effect=AssertionError('sandbox re-probed')) as sandbox, \
                mock.patch.object(runtime_preflight, 'select_runtime',
                                  side_effect=AssertionError('runtime re-probed')) as runtime, \
                mock.patch.object(worker, 'load_api_key',
                                  side_effect=AssertionError('provider key read')) as key:
            with self.assertRaises(worker.WorkerError) as caught:
                self.run_worker(second_args)
        self.assertEqual(caught.exception.code, 78)
        self.assertIn(original_id, caught.exception.message)
        self.assertIn('--recheck-environment', caught.exception.message)
        self.assertFalse(create.called)
        self.assertFalse(sandbox.called)
        self.assertFalse(runtime.called)
        self.assertFalse(key.called)
        second_row = coordination.load_task(self.repo, second_task['id'])['assignments'][0]
        self.assertEqual(second_row['failure_stage'], 'preparation')
        self.assertFalse(second_row.get('workspace_id'))
        copies = [path for path in (self.state / 'workspaces').iterdir()
                  if len(path.name) == 32]
        self.assertEqual([path.name for path in copies], [original_id])
        self.assertEqual(workspace.load(self.state, original_id).metadata,
                         original.metadata)

    def test_changed_runtime_identity_permits_a_new_attempt(self):
        first_task = self.plan('first')
        with self.failing_runtime(), self.assertRaises(worker.WorkerError):
            self.run_worker(self.args(first_task))
        first_row = coordination.load_task(self.repo, first_task['id'])['assignments'][0]
        original_id = first_row['workspace_id']

        second_task = self.plan('second')
        second_args = self.args(
            second_task, codex=self.disposable_runtime('disposable-codex-2', 'two'))
        with mock.patch.object(workspace, 'create', wraps=workspace.create) as create, \
                self.failing_runtime(error=runtime_preflight.RuntimePreflightError(
                    'runtime readiness failed again', 78)):
            with self.assertRaises(worker.WorkerError):
                self.run_worker(second_args)
        self.assertEqual(create.call_count, 1)
        rows = coordination.load_task(self.repo, second_task['id'])['assignments']
        self.assertTrue(rows[0]['workspace_id'])
        self.assertNotEqual(rows[0]['workspace_id'], original_id)

    def test_declared_dependency_failure_is_remembered_and_blocks(self):
        dependency = {'kind': 'command', 'value': 'missing-tool'}
        first_task = self.plan('dependency-first', dependencies=[dependency])
        with self.successful_runtime(), \
                mock.patch.object(development, 'missing_requirements',
                                  return_value=['missing-tool']), \
                mock.patch.object(worker, 'load_api_key',
                                  side_effect=AssertionError('provider key read')):
            with self.assertRaises(worker.WorkerError):
                self.run_worker(self.args(first_task))
        first_row = coordination.load_task(
            self.repo, first_task['id'])['assignments'][0]
        self.assertTrue(first_row['workspace_id'])

        second_task = self.plan('dependency-second', dependencies=[dependency])
        with mock.patch.object(workspace, 'create',
                               side_effect=AssertionError('workspace allocated')), \
                mock.patch.object(worker, 'resolve_os_sandbox',
                                  side_effect=AssertionError('sandbox re-probed')), \
                mock.patch.object(worker, 'load_api_key',
                                  side_effect=AssertionError('provider key read')):
            with self.assertRaises(worker.WorkerError) as caught:
                self.run_worker(self.args(second_task))
        self.assertIn('declared dependency availability',
                      caught.exception.message)
        self.assertEqual(len([path for path in (self.state / 'workspaces').iterdir()
                              if len(path.name) == 32]), 1)

    def test_appearing_declared_command_permits_a_new_attempt(self):
        dependency = {'kind': 'command', 'value': 'guard-fixture-tool'}
        binaries = self.root / 'guard-bin'
        binaries.mkdir()
        environ = dict(os.environ,
                       PATH=str(binaries) + os.pathsep + os.environ.get('PATH', ''))
        first_task = self.plan('dependency-install-first', dependencies=[dependency])
        with mock.patch.dict(os.environ, environ), \
                self.successful_runtime(), \
                mock.patch.object(worker, 'load_api_key',
                                  side_effect=AssertionError('provider key read')):
            with self.assertRaises(worker.WorkerError):
                self.run_worker(self.args(first_task))
        self.assertEqual(len(self.guard_records()), 1)

        tool = binaries / 'guard-fixture-tool'
        tool.write_text('#!/bin/sh\nexit 0\n')
        tool.chmod(0o755)
        second_task = self.plan('dependency-install-second', dependencies=[dependency])
        args = self.args(second_task)
        args.validate_admission = lambda **unused: None
        selection = runtime_preflight.RuntimeSelection(
            runtime='codex', binary=self.disposable_runtime(),
            layout=['test-layout'], environment={'PATH': str(binaries) + ':/usr/bin'})
        with mock.patch.dict(os.environ, environ), \
                mock.patch.object(worker, 'resolve_os_sandbox', return_value=(None, None)), \
                mock.patch.object(runtime_preflight, 'select_runtime', return_value=selection), \
                mock.patch.object(development, 'run_checks', return_value=[]), \
                mock.patch.object(development, 'missing_requirements', return_value=[]), \
                mock.patch.object(workspace, 'create', wraps=workspace.create) as create, \
                mock.patch.object(worker, 'load_api_key',
                                  side_effect=AssertionError('provider key read')):
            with self.assertRaises(AssertionError):
                self.run_worker(args)
        self.assertEqual(create.call_count, 1)

    def test_declared_smoke_probe_failure_is_remembered_and_blocks(self):
        task = self.plan('probe-first')

        def checks(layout, env, commands, timeout=120, *, deadline=None,
                   directory=None, phase='post', redact_values=(), **reuse_options):
            if phase == 'probe':
                return [{'command': 'fixture-probe', 'exit_code': 7,
                         'phase': 'probe'}]
            return []

        with self.successful_runtime(), \
                mock.patch.object(development, 'run_checks', side_effect=checks), \
                mock.patch.object(worker, 'load_api_key',
                                  side_effect=AssertionError('provider key read')):
            with self.assertRaises(worker.WorkerError):
                self.run_worker(self.args(task))
        first_row = coordination.load_task(self.repo, task['id'])['assignments'][0]
        self.assertTrue(first_row['workspace_id'])

        second_task = self.plan('probe-second')
        with mock.patch.object(workspace, 'create',
                               side_effect=AssertionError('workspace allocated')), \
                mock.patch.object(worker, 'resolve_os_sandbox',
                                  side_effect=AssertionError('sandbox re-probed')), \
                mock.patch.object(worker, 'load_api_key',
                                  side_effect=AssertionError('provider key read')):
            with self.assertRaises(worker.WorkerError) as caught:
                self.run_worker(self.args(second_task))
        self.assertIn('declared smoke probe', caught.exception.message)
        self.assertEqual(len([path for path in (self.state / 'workspaces').iterdir()
                              if len(path.name) == 32]), 1)

    def test_explicit_recheck_allows_one_attempt_and_preserves_original_record(self):
        first_task = self.plan('first')
        with self.failing_runtime(), self.assertRaises(worker.WorkerError):
            self.run_worker(self.args(first_task))
        conditions = self.worker_conditions()
        original = preparation_guard.lookup(self.state, conditions)
        self.assertTrue(original)

        second_task = self.plan('second')
        second_args = self.args(second_task, recheck_environment=True)
        with mock.patch.object(workspace, 'create', wraps=workspace.create) as create, \
                self.failing_runtime(error=runtime_preflight.RuntimePreflightError(
                    'runtime readiness failed again', 78)):
            with self.assertRaises(worker.WorkerError):
                self.run_worker(second_args)
        self.assertEqual(create.call_count, 1)
        self.assertEqual(preparation_guard.lookup(self.state, conditions), original)
        self.assertEqual(original['workspace_id'],
                         coordination.load_task(
                             self.repo, first_task['id'])['assignments'][0]['workspace_id'])

    def test_uncertain_timeout_is_not_remembered(self):
        first_task = self.plan('first')
        with mock.patch.object(worker, 'resolve_os_sandbox', return_value=(None, None)), \
                mock.patch.object(runtime_preflight, 'select_runtime',
                                  side_effect=runtime_preflight.RuntimePreflightError(
                                      'shared budget expired', 124)):
            with self.assertRaises(worker.WorkerError):
                self.run_worker(self.args(first_task))
        self.assertEqual(self.guard_records(), [])

        second_task = self.plan('second')
        with mock.patch.object(workspace, 'create', wraps=workspace.create) as create, \
                self.failing_runtime():
            with self.assertRaises(worker.WorkerError):
                self.run_worker(self.args(second_task))
        self.assertEqual(create.call_count, 1)

    def test_uncertain_runtime_probe_setup_failure_is_not_remembered(self):
        first_task = self.plan('runtime-uncertain')
        with mock.patch.object(worker, 'resolve_os_sandbox', return_value=(None, None)), \
                mock.patch.object(runtime_preflight, 'select_runtime',
                                  side_effect=runtime_preflight.RuntimePreflightError(
                                      'Codex probe evidence could not be captured safely.', 78)):
            with self.assertRaises(worker.WorkerError):
                self.run_worker(self.args(first_task))
        self.assertEqual(self.guard_records(), [])

        second_task = self.plan('runtime-uncertain-second')
        with mock.patch.object(workspace, 'create', wraps=workspace.create) as create, \
                self.failing_runtime():
            with self.assertRaises(worker.WorkerError):
                self.run_worker(self.args(second_task))
        self.assertEqual(create.call_count, 1)

    def test_generic_runtime_context_failure_is_not_remembered(self):
        task = self.plan('runtime-context-uncertain')
        with mock.patch.object(worker, 'resolve_os_sandbox', return_value=(None, None)), \
                mock.patch.object(runtime_preflight, 'select_runtime',
                                  side_effect=runtime_preflight.RuntimePreflightError(
                                      'runtime preparation failed: the worker namespace context '
                                      'could not be prepared (details withheld).', 78)):
            with self.assertRaises(worker.WorkerError):
                self.run_worker(self.args(task))
        self.assertEqual(self.guard_records(), [])

    def test_sandbox_probe_launch_failure_is_not_remembered(self):
        task = self.plan('sandbox-probe-uncertain')
        with mock.patch.object(worker, 'resolve_os_sandbox', return_value=(None, None)), \
                mock.patch.object(runtime_preflight, 'select_runtime',
                                  side_effect=runtime_preflight.RuntimePreflightError(
                                      'runtime preparation failed: Preparation: development '
                                      'OS sandbox probe could not run.', 78)):
            with self.assertRaises(worker.WorkerError):
                self.run_worker(self.args(task))
        self.assertEqual(self.guard_records(), [])

    def test_uncertain_sandbox_setup_failure_is_not_remembered(self):
        first_task = self.plan('sandbox-uncertain')
        with mock.patch.object(worker, 'resolve_os_sandbox',
                               side_effect=worker.WorkerError(
                                   78, 'Bubblewrap could not create the required worker namespace.')):
            with self.assertRaises(worker.WorkerError):
                self.run_worker(self.args(first_task))
        self.assertEqual(self.guard_records(), [])

        second_task = self.plan('sandbox-uncertain-second')
        with mock.patch.object(workspace, 'create', wraps=workspace.create) as create, \
                self.failing_runtime():
            with self.assertRaises(worker.WorkerError):
                self.run_worker(self.args(second_task))
        self.assertEqual(create.call_count, 1)

    def test_probe_timeout_is_not_remembered(self):
        task = self.plan('probe-timeout')

        def checks(layout, env, commands, timeout=120, *, deadline=None,
                   directory=None, phase='post', redact_values=()):
            if phase == 'probe':
                return [{'command': 'fixture-probe', 'exit_code': 124,
                         'timed_out': True, 'phase': 'probe'}]
            return []

        with self.successful_runtime(), \
                mock.patch.object(development, 'run_checks', side_effect=checks), \
                mock.patch.object(worker, 'load_api_key',
                                  side_effect=AssertionError('provider key read')):
            with self.assertRaises(worker.WorkerError):
                self.run_worker(self.args(task))
        self.assertEqual(self.guard_records(), [])

        second_task = self.plan('probe-timeout-second')
        with mock.patch.object(workspace, 'create', wraps=workspace.create) as create, \
                self.failing_runtime():
            with self.assertRaises(worker.WorkerError):
                self.run_worker(self.args(second_task))
        self.assertEqual(create.call_count, 1)

    def test_mixed_probe_timeout_is_not_remembered(self):
        task = self.plan('probe-mixed-timeout')

        def checks(layout, env, commands, timeout=120, *, deadline=None,
                   directory=None, phase='post', redact_values=()):
            if phase == 'probe':
                return [{'command': 'fixture-failure', 'exit_code': 5, 'phase': 'probe'},
                        {'command': 'fixture-timeout', 'exit_code': 124,
                         'timed_out': True, 'phase': 'probe'}]
            return []

        with self.successful_runtime(), \
                mock.patch.object(development, 'run_checks', side_effect=checks), \
                mock.patch.object(worker, 'load_api_key',
                                  side_effect=AssertionError('provider key read')):
            with self.assertRaises(worker.WorkerError):
                self.run_worker(self.args(task))
        self.assertEqual(self.guard_records(), [])

    def test_cancelled_preparation_is_not_remembered(self):
        first_task = self.plan('cancelled')
        with mock.patch.object(worker, 'resolve_os_sandbox',
                               side_effect=KeyboardInterrupt()):
            with self.assertRaises(KeyboardInterrupt):
                self.run_worker(self.args(first_task))
        self.assertEqual(self.guard_records(), [])

        second_task = self.plan('cancelled-second')
        with mock.patch.object(workspace, 'create', wraps=workspace.create) as create, \
                self.failing_runtime():
            with self.assertRaises(worker.WorkerError):
                self.run_worker(self.args(second_task))
        self.assertEqual(create.call_count, 1)

    def test_repeated_fresh_explicit_copies_stop_before_repeating_probes(self):
        first = workspace.create(self.repo, self.state)
        error = runtime_preflight.RuntimePreflightError('runtime readiness failed', 78)
        with mock.patch.object(worker, 'resolve_os_sandbox', return_value=(None, None)), \
                mock.patch.object(runtime_preflight, 'select_runtime',
                                  side_effect=error) as first_select, \
                mock.patch.object(worker, 'load_api_key',
                                  side_effect=AssertionError('provider key read')):
            with self.assertRaises(worker.WorkerError):
                self.run_worker(self.args(workspace=first.id))
        self.assertEqual(first_select.call_count, 1)
        self.assertEqual(len(self.guard_records()), 1)
        first_metadata = dict(workspace.load(self.state, first.id).metadata)
        self.assertEqual(first_metadata['status'], 'failed')

        second = workspace.create(self.repo, self.state)
        second_metadata = dict(workspace.load(self.state, second.id).metadata)
        with mock.patch.object(workspace, 'create',
                               side_effect=AssertionError('workspace allocated')) as create, \
                mock.patch.object(worker, 'resolve_os_sandbox',
                                  side_effect=AssertionError('sandbox re-probed')), \
                mock.patch.object(runtime_preflight, 'select_runtime',
                                  side_effect=AssertionError('runtime re-probed')) as select, \
                mock.patch.object(worker, 'load_api_key',
                                  side_effect=AssertionError('provider key read')) as key:
            with self.assertRaises(worker.WorkerError) as caught:
                self.run_worker(self.args(workspace=second.id))
        self.assertEqual(caught.exception.code, 78)
        self.assertIn(first.id, caught.exception.message)
        self.assertIn(second.id, caught.exception.message)
        self.assertIn('no additional workspace', caught.exception.message.lower())
        self.assertFalse(create.called)
        self.assertFalse(select.called)
        self.assertFalse(key.called)
        self.assertEqual(len(self.guard_records()), 1)
        self.assertEqual(workspace.load(self.state, second.id).metadata, second_metadata)
        self.assertEqual(workspace.load(self.state, first.id).metadata, first_metadata)

    def test_repeated_prepared_explicit_copies_stop_before_repeating_probes(self):
        software = self.tool_source()
        first = workspace.create(self.repo, self.state)
        toolchains.prepare(first, tools={'sdk': software})
        error = runtime_preflight.RuntimePreflightError('runtime readiness failed', 78)
        with mock.patch.object(worker, 'resolve_os_sandbox', return_value=(None, None)), \
                mock.patch.object(runtime_preflight, 'select_runtime',
                                  side_effect=error) as first_select, \
                mock.patch.object(worker, 'load_api_key',
                                  side_effect=AssertionError('provider key read')):
            with self.assertRaises(worker.WorkerError):
                self.run_worker(self.args(workspace=first.id))
        self.assertEqual(first_select.call_count, 1)
        self.assertEqual(len(self.guard_records()), 1)

        second = workspace.create(self.repo, self.state)
        toolchains.prepare(second, tools={'sdk': software})
        second_metadata = dict(workspace.load(self.state, second.id).metadata)
        with mock.patch.object(workspace, 'create',
                               side_effect=AssertionError('workspace allocated')) as create, \
                mock.patch.object(worker, 'resolve_os_sandbox',
                                  side_effect=AssertionError('sandbox re-probed')), \
                mock.patch.object(runtime_preflight, 'select_runtime',
                                  side_effect=AssertionError('runtime re-probed')) as select, \
                mock.patch.object(worker, 'load_api_key',
                                  side_effect=AssertionError('provider key read')):
            with self.assertRaises(worker.WorkerError) as caught:
                self.run_worker(self.args(workspace=second.id))
        self.assertIn(first.id, caught.exception.message)
        self.assertIn(second.id, caught.exception.message)
        self.assertFalse(create.called)
        self.assertFalse(select.called)
        self.assertEqual(len(self.guard_records()), 1)
        self.assertEqual(workspace.load(self.state, second.id).metadata, second_metadata)

    def test_changed_prepared_tool_content_permits_another_attempt(self):
        software = self.tool_source('one')
        first = workspace.create(self.repo, self.state)
        toolchains.prepare(first, tools={'sdk': software})
        error = runtime_preflight.RuntimePreflightError('runtime readiness failed', 78)
        with mock.patch.object(worker, 'resolve_os_sandbox', return_value=(None, None)), \
                mock.patch.object(runtime_preflight, 'select_runtime', side_effect=error), \
                mock.patch.object(worker, 'load_api_key',
                                  side_effect=AssertionError('provider key read')):
            with self.assertRaises(worker.WorkerError):
                self.run_worker(self.args(workspace=first.id))
        self.assertEqual(len(self.guard_records()), 1)

        (software / 'bin' / 'tool').write_text('two')
        second = workspace.create(self.repo, self.state)
        prepared = toolchains.prepare(second, tools={'sdk': software})
        self.assertEqual(prepared['tools'][0]['name'], 'sdk')
        with mock.patch.object(worker, 'resolve_os_sandbox', return_value=(None, None)), \
                mock.patch.object(runtime_preflight, 'select_runtime',
                                  side_effect=runtime_preflight.RuntimePreflightError(
                                      'changed readiness still failed', 78)) as select, \
                mock.patch.object(worker, 'load_api_key',
                                  side_effect=AssertionError('provider key read')):
            with self.assertRaises(worker.WorkerError):
                self.run_worker(self.args(workspace=second.id))
        self.assertEqual(select.call_count, 1)
        self.assertEqual(len(self.guard_records()), 2)

    def test_changed_prepared_declarations_permit_another_attempt(self):
        software = self.tool_source()
        first = workspace.create(self.repo, self.state)
        toolchains.prepare(first, tools={'sdk': software})
        error = runtime_preflight.RuntimePreflightError('runtime readiness failed', 78)
        with mock.patch.object(worker, 'resolve_os_sandbox', return_value=(None, None)), \
                mock.patch.object(runtime_preflight, 'select_runtime', side_effect=error), \
                mock.patch.object(worker, 'load_api_key',
                                  side_effect=AssertionError('provider key read')):
            with self.assertRaises(worker.WorkerError):
                self.run_worker(self.args(workspace=first.id))
        self.assertEqual(len(self.guard_records()), 1)

        second = workspace.create(self.repo, self.state)
        toolchains.prepare(second, tools={'sdk': software}, probes=['true'])
        with mock.patch.object(worker, 'resolve_os_sandbox', return_value=(None, None)), \
                mock.patch.object(runtime_preflight, 'select_runtime',
                                  side_effect=runtime_preflight.RuntimePreflightError(
                                      'declared readiness still failed', 78)) as select, \
                mock.patch.object(worker, 'load_api_key',
                                  side_effect=AssertionError('provider key read')):
            with self.assertRaises(worker.WorkerError):
                self.run_worker(self.args(workspace=second.id))
        self.assertEqual(select.call_count, 1)
        self.assertEqual(len(self.guard_records()), 2)

    def test_recheck_environment_does_not_bypass_failed_copy_recovery(self):
        copy = workspace.create(self.repo, self.state)
        with self.failing_runtime(), self.assertRaises(worker.WorkerError):
            self.run_worker(self.args(workspace=copy.id))
        self.assertEqual(len(self.guard_records()), 1)
        failed_metadata = dict(workspace.load(self.state, copy.id).metadata)
        self.assertEqual(failed_metadata['status'], 'failed')

        with mock.patch.object(worker, 'resolve_os_sandbox', return_value=(None, None)), \
                mock.patch.object(runtime_preflight, 'select_runtime',
                                  side_effect=AssertionError('runtime re-probed')) as select, \
                mock.patch.object(worker, 'load_api_key',
                                  side_effect=AssertionError('provider key read')):
            with self.assertRaises(worker.WorkerError) as caught:
                self.run_worker(self.args(workspace=copy.id, recheck_environment=True))
        self.assertIn('resume-after-failure', caught.exception.message)
        self.assertFalse(select.called)
        self.assertEqual(len(self.guard_records()), 1)
        self.assertEqual(workspace.load(self.state, copy.id).metadata, failed_metadata)

    def test_explicit_resume_is_not_blocked_by_guard(self):
        copy = workspace.create(self.repo, self.state)
        with self.failing_runtime(), self.assertRaises(worker.WorkerError):
            self.run_worker(self.args(workspace=copy.id))
        self.assertEqual(len(self.guard_records()), 1)

        with mock.patch.object(worker, 'resolve_os_sandbox', return_value=(None, None)), \
                mock.patch.object(runtime_preflight, 'select_runtime',
                                  side_effect=runtime_preflight.RuntimePreflightError(
                                      'explicit recovery probe failed', 78)) as select, \
                mock.patch.object(worker, 'load_api_key',
                                  side_effect=AssertionError('provider key read')):
            with self.assertRaises(worker.WorkerError):
                self.run_worker(self.args(workspace=copy.id, resume_after_failure=True))
        self.assertEqual(select.call_count, 1)
        self.assertEqual(len(self.guard_records()), 1)


class IneligibleFailureTests(GuardFixture):
    def test_provider_failure_is_not_cached(self):
        args = self.args()
        args.validate_admission = lambda **unused: None
        slot = os.open(os.devnull, os.O_RDONLY)
        policy = settings.resolve(self.repo)
        with contextlib.chdir(self.repo), contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()), \
                mock.patch.object(worker, 'acquire_job_slot', return_value=slot), \
                self.successful_runtime(), \
                mock.patch.object(worker, 'load_api_key',
                                  side_effect=worker.WorkerError(
                                      78, 'Provider credential is absent.')):
            with self.assertRaises(worker.WorkerError):
                managed.run(args, policy, worker)
        self.assertEqual(self.guard_records(), [])
        self.assertIsNone(preparation_guard.lookup(self.state, self.worker_conditions()))

    def test_ordinary_baseline_test_failure_is_not_cached(self):
        check = 'python3 -c "raise SystemExit(65)"'
        task = self.plan('baseline', checks=[check])
        args = self.args(task)

        def checks(layout, env, commands, timeout=120, *, deadline=None,
                   directory=None, phase='post', redact_values=(), **reuse_options):
            if phase == 'baseline':
                return [{'command': commands[0], 'exit_code': 65, 'phase': 'baseline'}]
            return []

        with self.successful_runtime(), \
                mock.patch.object(development, 'run_checks', side_effect=checks), \
                mock.patch.object(worker, 'load_api_key',
                                  side_effect=worker.WorkerError(
                                      78, 'Provider credential is absent.')):
            with self.assertRaises(worker.WorkerError):
                self.run_worker(args)
        self.assertEqual(self.guard_records(), [])


class DeclarativePreparationGuardTests(GuardFixture):
    def declarations(self, source):
        return {'tools': {'sdk': source}, 'copies': [],
                'environment': {}, 'probes': []}

    def prepare(self, copy, source, *, recheck=False):
        return toolchains.prepare(
            copy, tools={'sdk': source}, recheck_environment=recheck)

    def test_missing_materialization_source_blocks_a_new_copy(self):
        missing = self.root / 'missing-sdk'
        first = workspace.create(self.repo, self.state)
        with self.assertRaises(toolchains.MissingSourceError):
            self.prepare(first, missing)
        conditions = preparation_guard.declarative_conditions(
            self.repo, state=self.state, declarations=self.declarations(missing))
        original = preparation_guard.lookup(self.state, conditions)
        self.assertEqual(original['workspace_id'], first.id)

        second = workspace.create(self.repo, self.state)
        before = dict(second.metadata)
        with self.assertRaises(toolchains.ToolchainError) as caught:
            self.prepare(second, missing)
        self.assertIn(first.id, str(caught.exception))
        self.assertIn('--recheck-environment', str(caught.exception))
        self.assertEqual(workspace.load(self.state, second.id).metadata, before)
        self.assertFalse((second.path / '.deepseek-tools').exists())

        with self.assertRaises(toolchains.MissingSourceError):
            self.prepare(second, missing, recheck=True)
        self.assertEqual(workspace.load(self.state, second.id).metadata['status'], 'failed')
        self.assertEqual(preparation_guard.lookup(self.state, conditions), original)

        missing.mkdir()
        (missing / 'bin').mkdir()
        (missing / 'bin' / 'tool').write_text('#!/bin/sh\nexit 0\n')
        third = workspace.create(self.repo, self.state)
        result = self.prepare(third, missing)
        self.assertEqual([entry['name'] for entry in result['tools']], ['sdk'])
        self.assertEqual(workspace.load(self.state, third.id).metadata['status'], 'succeeded')

    def test_recheck_does_not_bypass_failed_copy_recovery(self):
        missing = self.root / 'missing-sdk-for-recovery'
        copy = workspace.create(self.repo, self.state)
        with self.assertRaises(toolchains.MissingSourceError):
            self.prepare(copy, missing)
        with self.assertRaises(workspace.WorkspaceError):
            self.prepare(copy, missing, recheck=True)
        record = workspace.load(self.state, copy.id)
        self.assertEqual(record.metadata['status'], 'failed')

    def test_arbitrary_host_preparation_commands_are_never_classified(self):
        copy = workspace.create(self.repo, self.state)
        code = workspace.prepare(
            copy, [sys.executable, '-c', 'raise SystemExit(3)'])
        self.assertEqual(code, 3)
        self.assertEqual(self.guard_records(), [])


class FingerprintIdentityTests(GuardFixture):
    def declarations(self, source):
        return {'tools': {'sdk': source}, 'copies': [],
                'environment': {}, 'probes': []}

    def test_nested_tool_source_change_changes_the_fingerprint(self):
        software = self.root / 'software'
        (software / 'bin').mkdir(parents=True)
        tool = software / 'bin' / 'tool'
        tool.write_text('version-one\n')
        first = preparation_guard.declarative_conditions(
            self.repo, state=self.state, declarations=self.declarations(software))
        self.assertIsNotNone(first.fingerprint)

        tool.write_text('version-two\n')
        second = preparation_guard.declarative_conditions(
            self.repo, state=self.state, declarations=self.declarations(software))
        self.assertNotEqual(first.fingerprint, second.fingerprint)

    def test_runtime_rewrite_with_restored_mtime_changes_the_fingerprint(self):
        binary = Path(self.disposable_runtime())
        first = self.worker_conditions()
        old = binary.stat()
        content = binary.read_bytes()
        self.assertIn(b'one', content)
        binary.write_bytes(content.replace(b'one', b'two', 1))
        os.utime(binary, ns=(old.st_atime_ns, old.st_mtime_ns))
        unchanged_mtime = binary.stat()
        self.assertEqual(unchanged_mtime.st_mtime_ns, old.st_mtime_ns)
        self.assertEqual(unchanged_mtime.st_size, old.st_size)
        second = self.worker_conditions()
        self.assertIsNotNone(first.fingerprint)
        self.assertIsNotNone(second.fingerprint)
        self.assertNotEqual(first.fingerprint, second.fingerprint)

    def test_declared_command_lookup_change_changes_the_fingerprint(self):
        binaries = self.root / 'guard-bin'
        binaries.mkdir()
        environ = dict(os.environ,
                       PATH=str(binaries) + os.pathsep + os.environ.get('PATH', ''))
        declarations = {
            'dependencies': [{'kind': 'command', 'value': 'guard-fixture-tool'}],
            'checks': [],
        }
        before = preparation_guard.worker_conditions(
            self.repo, runtime='codex', codex=self.disposable_runtime(),
            claude='claude', codex_explicit=True, claude_explicit=False,
            state=self.state, declarations=declarations, environ=environ)
        tool = binaries / 'guard-fixture-tool'
        tool.write_text('#!/bin/sh\nexit 0\n')
        tool.chmod(0o755)
        after = preparation_guard.worker_conditions(
            self.repo, runtime='codex', codex=self.disposable_runtime(),
            claude='claude', codex_explicit=True, claude_explicit=False,
            state=self.state, declarations=declarations, environ=environ)
        self.assertIsNotNone(before.fingerprint)
        self.assertIsNotNone(after.fingerprint)
        self.assertNotEqual(before.fingerprint, after.fingerprint)

    def test_runtime_adjacent_declared_command_change_changes_the_fingerprint(self):
        runtime_dir = self.root / 'runtime-bin'
        runtime_dir.mkdir()
        codex = runtime_dir / 'codex'
        codex.write_text('#!/bin/sh\nexit 0\n')
        codex.chmod(0o755)
        environ = dict(os.environ, PATH='/usr/bin:/bin')
        declarations = {
            'dependencies': [{'kind': 'command', 'value': 'guard-runtime-helper'}],
            'checks': [],
        }
        before = preparation_guard.worker_conditions(
            self.repo, runtime='codex', codex=str(codex), claude='claude',
            codex_explicit=True, claude_explicit=False,
            state=self.state, declarations=declarations, environ=environ)
        helper = runtime_dir / 'guard-runtime-helper'
        helper.write_text('#!/bin/sh\nexit 0\n')
        helper.chmod(0o755)
        after = preparation_guard.worker_conditions(
            self.repo, runtime='codex', codex=str(codex), claude='claude',
            codex_explicit=True, claude_explicit=False,
            state=self.state, declarations=declarations, environ=environ)
        self.assertIsNotNone(before.fingerprint)
        self.assertIsNotNone(after.fingerprint)
        self.assertNotEqual(before.fingerprint, after.fingerprint)

    def test_declared_check_lookup_change_changes_the_fingerprint(self):
        binaries = self.root / 'guard-check-bin'
        binaries.mkdir()
        environ = dict(os.environ,
                       PATH=str(binaries) + os.pathsep + os.environ.get('PATH', ''))
        declarations = {
            'dependencies': [],
            'checks': ['guard-fixture-check --version'],
        }
        before = preparation_guard.worker_conditions(
            self.repo, runtime='codex', codex=self.disposable_runtime(),
            claude='claude', codex_explicit=True, claude_explicit=False,
            state=self.state, declarations=declarations, environ=environ)
        check = binaries / 'guard-fixture-check'
        check.write_text('#!/bin/sh\nexit 0\n')
        check.chmod(0o755)
        after = preparation_guard.worker_conditions(
            self.repo, runtime='codex', codex=self.disposable_runtime(),
            claude='claude', codex_explicit=True, claude_explicit=False,
            state=self.state, declarations=declarations, environ=environ)
        self.assertIsNotNone(before.fingerprint)
        self.assertIsNotNone(after.fingerprint)
        self.assertNotEqual(before.fingerprint, after.fingerprint)

    def test_unreadable_platform_executable_identity_is_unknown(self):
        original = preparation_guard._identity

        def identity(value, *, content=False):
            if value == preparation_guard._APPARMOR_PROFILE:
                return None
            return original(value, content=content)

        with mock.patch.object(preparation_guard, '_identity', side_effect=identity):
            conditions = preparation_guard.worker_conditions(
                self.repo, runtime='codex', codex=self.disposable_runtime(),
                claude='claude', codex_explicit=True, claude_explicit=False,
                state=self.state)
        self.assertIsNone(conditions.fingerprint)
        self.assertIn('platform', conditions.unknown)

    def test_git_metadata_tool_input_is_left_uncached_without_reading(self):
        with mock.patch.object(preparation_guard, '_read_hashed_file',
                               wraps=preparation_guard._read_hashed_file) as read:
            conditions = preparation_guard.declarative_conditions(
                self.repo, state=self.state,
                declarations=self.declarations(self.repo / '.git'))
        self.assertIsNone(conditions.fingerprint)
        self.assertIn('declarations', conditions.unknown)
        self.assertFalse(read.called)

    def test_broad_tool_input_is_left_uncached_without_traversal(self):
        with mock.patch.object(preparation_guard, '_tree_digest',
                               wraps=preparation_guard._tree_digest) as tree:
            conditions = preparation_guard.declarative_conditions(
                self.repo, state=self.state, declarations=self.declarations('/'))
        self.assertIsNone(conditions.fingerprint)
        self.assertIn('declarations', conditions.unknown)
        self.assertFalse(tree.called)

    def test_relative_runtime_pin_is_left_uncached(self):
        conditions = preparation_guard.worker_conditions(
            self.repo, runtime='codex', codex='bin/codex',
            claude='claude', codex_explicit=True, claude_explicit=False,
            state=self.state)
        self.assertIsNone(conditions.fingerprint)
        self.assertIn('runtime-codex', conditions.unknown)

    def test_absolute_path_dependency_change_changes_the_fingerprint(self):
        dependency = self.root / 'prepared-dependency'
        declarations = {
            'dependencies': [{'kind': 'path', 'value': str(dependency)}],
            'checks': [],
        }
        before = preparation_guard.worker_conditions(
            self.repo, runtime='codex', codex=self.disposable_runtime(),
            claude='claude', codex_explicit=True, claude_explicit=False,
            state=self.state, declarations=declarations)
        dependency.write_text('prepared\n')
        after = preparation_guard.worker_conditions(
            self.repo, runtime='codex', codex=self.disposable_runtime(),
            claude='claude', codex_explicit=True, claude_explicit=False,
            state=self.state, declarations=declarations)
        self.assertIsNotNone(before.fingerprint)
        self.assertIsNotNone(after.fingerprint)
        self.assertNotEqual(before.fingerprint, after.fingerprint)

    def test_copy_relative_dependency_is_left_uncached(self):
        conditions = preparation_guard.worker_conditions(
            self.repo, runtime='codex', codex=self.disposable_runtime(),
            claude='claude', codex_explicit=True, claude_explicit=False,
            state=self.state,
            declarations={'dependencies': [{'kind': 'path', 'value': 'tools/tool'}],
                          'checks': []})
        self.assertIsNone(conditions.fingerprint)
        self.assertIn('declarations', conditions.unknown)

    def test_credential_like_tool_input_is_left_uncached_without_hashing(self):
        software = self.root / 'software-with-secret'
        software.mkdir()
        (software / '.env').write_text('SENTINEL_SECRET_VALUE\n')
        with mock.patch.object(preparation_guard, '_read_hashed_file',
                               wraps=preparation_guard._read_hashed_file) as read:
            conditions = preparation_guard.declarative_conditions(
                self.repo, state=self.state, declarations=self.declarations(software))
        self.assertIsNone(conditions.fingerprint)
        self.assertIn('declarations', conditions.unknown)
        self.assertFalse(any(Path(call.args[0]).name == '.env'
                             for call in read.call_args_list))

    def test_git_subdirectory_uses_the_same_canonical_project(self):
        subdirectory = self.repo / 'subdirectory'
        subdirectory.mkdir()
        root = preparation_guard.declarative_conditions(
            self.repo, state=self.state, declarations=self.declarations(self.repo))
        nested = preparation_guard.declarative_conditions(
            subdirectory, state=self.state, declarations=self.declarations(self.repo))
        self.assertEqual(root.project, nested.project)
        self.assertEqual(root.fingerprint, nested.fingerprint)

    def test_tree_beyond_the_identity_bound_is_left_uncached(self):
        software = self.root / 'software'
        software.mkdir()
        for name in ('one', 'two', 'three'):
            (software / name).write_text(name)
        with mock.patch.object(preparation_guard, 'MAX_TREE_ENTRIES', 2):
            conditions = preparation_guard.declarative_conditions(
                self.repo, state=self.state, declarations=self.declarations(software))
        self.assertIsNone(conditions.fingerprint)
        self.assertIn('declarations', conditions.unknown)


class RecordStorageTests(GuardFixture):
    def conditions(self):
        return preparation_guard.declarative_conditions(
            self.repo, state=self.state,
            declarations={'tools': {}, 'copies': [], 'environment': {}, 'probes': []})

    def test_concurrent_records_publish_one_complete_original(self):
        conditions = self.conditions()
        barrier = threading.Barrier(8)

        def remember(index):
            barrier.wait()
            return preparation_guard.record_failure(
                self.state, conditions, category='materialization',
                workspace_id='%032x' % (index + 1))

        with ThreadPoolExecutor(max_workers=8) as pool:
            records = list(pool.map(remember, range(8)))
        self.assertEqual(len({record['workspace_id'] for record in records}), 1)
        self.assertTrue(all(record == records[0] for record in records))
        files = self.guard_records()
        self.assertEqual(len(files), 1)
        self.assertEqual([path.name for path in files
                          if '.tmp-' in path.name], [])
        stored = json.loads(files[0].read_text())
        self.assertEqual(stored, records[0])

    def test_record_capacity_is_bounded_and_preserves_existing_evidence(self):
        first = preparation_guard.Conditions(str(self.repo), 'a' * 64)
        second = preparation_guard.Conditions(str(self.repo), 'b' * 64)
        with mock.patch.object(preparation_guard, 'MAX_RECORDS', 1):
            stored = preparation_guard.record_failure(
                self.state, first, category='runtime')
            rejected = preparation_guard.record_failure(
                self.state, second, category='runtime')
        self.assertIsNotNone(stored)
        self.assertIsNone(rejected)
        self.assertEqual(len(self.guard_records()), 1)
        self.assertEqual(preparation_guard.lookup(self.state, first), stored)
        self.assertIsNone(preparation_guard.lookup(self.state, second))

    def test_time_passage_does_not_expire_a_record(self):
        conditions = self.conditions()
        with mock.patch.object(preparation_guard.time, 'time', return_value=1_000):
            stored = preparation_guard.record_failure(
                self.state, conditions, category='materialization')
        with mock.patch.object(preparation_guard.time, 'time',
                               return_value=10_000_000_000):
            self.assertEqual(preparation_guard.lookup(self.state, conditions), stored)

    def test_unsafe_symlinked_record_directory_is_rejected(self):
        self.state.mkdir(mode=0o700)
        outside = self.root / 'outside'
        outside.mkdir(mode=0o700)
        os.symlink(outside, self.state / preparation_guard.DIRECTORY)
        with self.assertRaises(preparation_guard.PreparationGuardError):
            preparation_guard.lookup(self.state, self.conditions())
        self.assertEqual(list(outside.iterdir()), [])

    def test_symlinked_record_is_rejected_without_following(self):
        conditions = self.conditions()
        directory = self.state / preparation_guard.DIRECTORY
        self.state.mkdir(mode=0o700)
        directory.mkdir(mode=0o700)
        outside = self.root / 'outside-record.json'
        outside.write_text('{"schema": 1}\n')
        os.symlink(outside, directory / (conditions.fingerprint + '.json'))
        with self.assertRaises(preparation_guard.PreparationGuardError):
            preparation_guard.lookup(self.state, conditions)
        self.assertEqual(outside.read_text(), '{"schema": 1}\n')

    def test_record_is_bounded_and_never_contains_environment_values(self):
        secret = 'SENTINEL_ENVIRONMENT_VALUE_DO_NOT_STORE'
        with mock.patch.dict(os.environ, {'GUARD_TEST_SECRET': secret}):
            conditions = preparation_guard.worker_conditions(
                self.repo, runtime='codex', codex=self.disposable_runtime(),
                claude='claude', codex_explicit=True, claude_explicit=False,
                state=self.state,
                declarations={'dependencies': [], 'checks': []})
            preparation_guard.record_failure(
                self.state, conditions, category='runtime',
                workspace_id='a' * 32)
        raw = self.guard_records()[0].read_bytes()
        self.assertLessEqual(len(raw), preparation_guard.MAX_RECORD_BYTES)
        self.assertNotIn(secret.encode(), raw)
        self.assertNotIn(b'task_id', raw)
        self.assertNotIn(b'secret', raw.lower())

    def test_invalid_categories_codes_and_identities_are_rejected(self):
        conditions = self.conditions()
        for category in ('provider', 'verification', 'unknown'):
            with self.subTest(category=category):
                with self.assertRaises(preparation_guard.PreparationGuardError):
                    preparation_guard.record_failure(
                        self.state, conditions, category=category)
        for code in (124, 130, True, '78'):
            with self.subTest(code=code):
                with self.assertRaises(preparation_guard.PreparationGuardError):
                    preparation_guard.record_failure(
                        self.state, conditions, category='runtime', exit_code=code)
        with self.assertRaises(preparation_guard.PreparationGuardError):
            preparation_guard.record_failure(
                self.state, conditions, category='runtime',
                workspace_id='not-a-workspace')

    def test_malformed_fingerprint_cannot_escape_the_storage_directory(self):
        self.state.mkdir(mode=0o700)
        outside = self.root / 'outside.json'
        conditions = preparation_guard.Conditions(str(self.repo), '../outside')
        with self.assertRaises(preparation_guard.PreparationGuardError):
            preparation_guard.record_failure(
                self.state, conditions, category='runtime')
        self.assertFalse(outside.exists())

    def test_unknown_source_identity_never_claims_unchanged(self):
        source = self.root / 'not-a-git-project'
        source.mkdir()
        conditions = preparation_guard.declarative_conditions(
            source, state=self.state,
            declarations={'tools': {}, 'copies': [], 'environment': {}, 'probes': []})
        self.assertIsNone(conditions.fingerprint)
        self.assertIn('source-head', conditions.unknown)
        self.assertIsNone(preparation_guard.record_failure(
            self.state, conditions, category='materialization'))
        self.assertEqual(self.guard_records(), [])

    def test_saved_recipe_and_available_source_changes_change_the_key(self):
        software = self.root / 'software'
        (software / 'bin').mkdir(parents=True)
        (software / 'bin' / 'tool').write_text('#!/bin/sh\nexit 0\n')
        copy = workspace.create(self.repo, self.state)
        toolchains.prepare(copy, tools={'sdk': software}, save_project=True)
        declarations = {'dependencies': [], 'checks': []}
        first = preparation_guard.worker_conditions(
            self.repo, runtime='codex', codex=self.disposable_runtime(),
            claude='claude', codex_explicit=True, claude_explicit=False,
            state=self.state, declarations=declarations)
        recipe = next((self.state / 'toolchain-recipes').glob('*.json'))
        recipe.write_text(recipe.read_text() + ' ')
        second = preparation_guard.worker_conditions(
            self.repo, runtime='codex', codex=self.disposable_runtime(),
            claude='claude', codex_explicit=True, claude_explicit=False,
            state=self.state, declarations=declarations)
        self.assertNotEqual(first.fingerprint, second.fingerprint)

    def test_unsafe_state_root_permissions_are_rejected(self):
        self.state.mkdir(mode=0o755)
        with self.assertRaises(preparation_guard.PreparationGuardError):
            preparation_guard.lookup(self.state, self.conditions())


class CliOptionTests(GuardFixture):
    def cli(self, *arguments):
        env = dict(os.environ,
                   PYTHONPATH=str(Path(__file__).resolve().parents[1] / 'src'),
                   HOME=str(self.root / 'home'), CODEX_HOME=str(self.root / 'codex'))
        return subprocess.run([sys.executable, '-m', 'codex_deepseek_team', *arguments],
                              cwd=self.repo, env=env, text=True,
                              capture_output=True, timeout=60)

    def test_worker_and_declarative_prepare_accept_recheck_environment(self):
        help_text = self.cli('worker', '--help')
        self.assertEqual(help_text.returncode, 0, help_text.stderr)
        self.assertIn('--recheck-environment', help_text.stdout)
        prepare_help = self.cli('workspace', 'prepare', '--help')
        self.assertEqual(prepare_help.returncode, 0, prepare_help.stderr)
        self.assertIn('--recheck-environment', prepare_help.stdout)

    def test_cli_rejects_recheck_for_legacy_host_command_preparation(self):
        created = self.cli('workspace', 'create', str(self.repo),
                           '--state-dir', str(self.state), '--json')
        self.assertEqual(created.returncode, 0, created.stderr)
        workspace_id = json.loads(created.stdout)['id']
        result = self.cli('workspace', 'prepare', workspace_id,
                          '--state-dir', str(self.state),
                          '--recheck-environment', '--', sys.executable, '-c', 'pass')
        self.assertEqual(result.returncode, 64, result.stderr)
        self.assertIn('never classified', result.stderr)

    def test_cli_blocks_then_rechecks_declarative_preparation(self):
        missing = self.root / 'missing-sdk'
        created = self.cli('workspace', 'create', str(self.repo),
                           '--state-dir', str(self.state), '--json')
        self.assertEqual(created.returncode, 0, created.stderr)
        first = json.loads(created.stdout)
        prepared = self.cli('workspace', 'prepare', first['id'],
                            '--state-dir', str(self.state),
                            '--tool', 'sdk=' + str(missing))
        self.assertEqual(prepared.returncode, 78, prepared.stderr)

        second = json.loads(self.cli('workspace', 'create', str(self.repo),
                                     '--state-dir', str(self.state), '--json').stdout)
        blocked = self.cli('workspace', 'prepare', second['id'],
                           '--state-dir', str(self.state),
                           '--tool', 'sdk=' + str(missing))
        self.assertEqual(blocked.returncode, 78, blocked.stderr)
        self.assertIn(first['id'], blocked.stderr)
        self.assertIn('--recheck-environment', blocked.stderr)
        record = json.loads((self.state / 'workspaces' / second['id'] /
                             'record.json').read_text())
        self.assertEqual(record['status'], 'ready')

        rechecked = self.cli('workspace', 'prepare', second['id'],
                             '--state-dir', str(self.state),
                             '--tool', 'sdk=' + str(missing),
                             '--recheck-environment')
        self.assertEqual(rechecked.returncode, 78, rechecked.stderr)
        record = json.loads((self.state / 'workspaces' / second['id'] /
                             'record.json').read_text())
        self.assertEqual(record['status'], 'failed')


if __name__ == '__main__':
    unittest.main()
