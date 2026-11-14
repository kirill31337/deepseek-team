"""C5 pre-start cancellation, launch reservations and queue ownership."""
from __future__ import annotations

import contextlib
import fcntl
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import textwrap
import time
from types import SimpleNamespace
import unittest
from unittest import mock

from codex_deepseek_team import (activation, coordination, coordination_cli,
                                 coordinator_hooks, project, settings, worker,
                                 workspace, workspace_lifecycle, worker_slots)

from test_coordination import CoordinationCase


RUNTIMES = ('codex', 'claude')


class CancelFixture(CoordinationCase):
    def setUp(self):
        super().setUp()
        settings.set_values(self.repo / settings.PROJECT_FILE,
                            delegation_level=75, access='full-access')
        activation.set_enabled(self.repo, True)
        project.attach(self.repo, coordinator='both')
        self.session = 'cancel-session'
        self.env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / 'src'))

    def open(self, turn='turn-1'):
        return coordination.open_task(
            self.repo, session_id=self.session, turn_id=turn,
            prompt='implement cancellation', policy=settings.resolve(self.repo))

    def planned(self, *, executor='worker', scope=None, checks=None, turn='turn-1'):
        task = self.open(turn)
        item = {
            'id': 'impl', 'kind': 'implementation', 'scope': list(scope or ['a.py']),
            'executor': executor, 'acceptance': ['implemented'],
            'dependencies': [], 'checks': list(checks or []),
        }
        task = coordination.plan_task(self.repo, task['id'], {
            'classification': 'substantial', 'deliverables': [item]})
        return task, task['assignments'][0]['id']

    def cancel(self, task, assignment=None, evidence='user cancelled before launch'):
        return coordination_cli.main([
            'coordination', 'cancel', '--path', str(self.repo), '--task', task['id'],
            '--assignment', assignment or task['assignments'][0]['id'],
            '--evidence', evidence,
        ])

    def row(self, task):
        return coordination.load_task(self.repo, task['id'])['assignments'][0]

    def raw(self, task):
        return coordination._task_path(self.repo, task['id']).read_bytes()

    def reserve(self, task, assignment):
        return coordination.register_launch_reservation(
            self.repo, task['id'], assignment)

    def assert_cancelled(self, task, assignment):
        row = self.row(task)
        self.assertEqual(row['status'], 'failed')
        self.assertEqual(row['error_kind'], 'cancelled')
        self.assertEqual(row['failure_stage'], 'before_start')
        self.assertEqual(row['queue_state'], 'finished')
        self.assertEqual(row['disposition']['kind'], 'rejected')
        self.assertTrue(row['disposition']['evidence'])
        self.assertIsInstance(row['disposition']['at'], (int, float))
        self.assertIsInstance(row['finished_at'], (int, float))
        self.assertNotIn('started_at', row)
        self.assertFalse(row.get('workspace_id'))
        self.assertFalse(row.get('job_id'))
        self.assertFalse(row.get('attempt_id'))
        self.assertEqual(row.get('worker_changes'), [])
        self.assertEqual(row.get('checks'), [])
        self.assertNotIn('attempt_history', row)
        self.assertNotIn('execution_snapshot', row)
        self.assertNotIn('routing_features', row)
        self.assertNotIn('check_timeout_seconds', row)
        self.assertEqual(coordination.completion_issues(task), [])

    def script(self, body):
        return textwrap.dedent(body)

    def run_script(self, body, *arguments, timeout=30, env=None):
        return subprocess.run(
            [sys.executable, '-c', self.script(body), *map(str, arguments)],
            cwd=self.repo, env=env or self.env, text=True,
            capture_output=True, timeout=timeout)

    def registration_script(self, body=''):
        return self.script(f'''
import json
from pathlib import Path
import sys
from codex_deepseek_team import coordination
root, ready, go = map(Path, (sys.argv[1], sys.argv[4], sys.argv[5]))
task_id, assignment_id = sys.argv[2:4]
reservation = coordination.register_launch_reservation(root, task_id, assignment_id)
ready.write_text(json.dumps(reservation))
while not go.exists():
    import time
    time.sleep(0.02)
{body}
''')

    def wait_ready(self, path, timeout=10):
        deadline = time.monotonic() + timeout
        while not path.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(path.exists(), 'launcher never reached the barrier')
        return json.loads(path.read_text())


class CancellationStateTests(CancelFixture):
    def test_public_cli_cancels_pristine_planned_assignment_and_is_idempotent(self):
        task, assignment = self.planned()
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(self.cancel(task), 0)
        self.assertIn('status=failed', output.getvalue())
        task = coordination.load_task(self.repo, task['id'])
        self.assert_cancelled(task, assignment)
        settled = self.raw(task)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.cancel(task, evidence='different repeat evidence'), 0)
        self.assertEqual(self.raw(task), settled)
        self.assertEqual(coordination.load_task(self.repo, task['id']),
                         json.loads(settled))

    def test_cancel_does_not_fabricate_quality_cost_checks_or_started_state(self):
        task, assignment = self.planned(checks=['python3 -m unittest -q'])
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.cancel(task), 0)
        row = self.row(task)
        self.assertIsNone(row.get('exit_code'))
        self.assertFalse(row.get('baseline_checks'))
        self.assertFalse(row.get('verification_runs'))
        self.assertFalse(row.get('lessons_feedback'))
        self.assertNotIn('quality', row)
        self.assertNotIn('cost_usd', row)

    def test_launch_that_registers_and_releases_during_inspection_does_not_block(self):
        task, assignment = self.planned()

        def churn(*unused, **unused_keywords):
            reservation = self.reserve(task, assignment)
            coordination.release_launch_reservation(
                self.repo, task['id'], assignment, reservation)
            return []

        with mock.patch.object(worker_slots, 'inspect_bindings', side_effect=churn):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(self.cancel(task), 0)
        self.assert_cancelled(coordination.load_task(self.repo, task['id']), assignment)

    def test_touched_or_started_assignments_are_not_c5_cancellable(self):
        for changes in (
                {'workspace_id': 'a' * 32}, {'job_id': 'b' * 32},
                {'attempt_id': 'attempt'}, {'started_at': time.time()},
                {'prepared_changes': ['a.py']}, {'worker_changes': ['a.py']},
                {'launch_phase': 'allocation'}, {'execution_snapshot': {'model': 'm'}}):
            with self.subTest(changes=changes):
                task, assignment = self.planned(turn='touched-' + next(iter(changes)))
                changed = coordination.load_task(self.repo, task['id'])
                changed['assignments'][0].update(changes)
                coordination._atomic(coordination._task_path(self.repo, task['id']), changed)
                with contextlib.redirect_stderr(io.StringIO()) as error:
                    self.assertEqual(self.cancel(task), 75)
                self.assertIn('stop', error.getvalue().lower())
                self.assertEqual(self.row(task)['status'], 'planned')

    def test_terminal_or_unknown_assignment_is_rejected_without_history_rewrite(self):
        task, assignment = self.planned()
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.cancel(task), 0)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.cancel(task), 0)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(coordination_cli.main([
                'coordination', 'cancel', '--path', str(self.repo), '--task', task['id'],
                '--assignment', 'as-' + '0' * 16, '--evidence', 'unknown']), 64)

    def test_empty_evidence_is_rejected_before_mutation(self):
        task, _ = self.planned()
        before = self.raw(task)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(self.cancel(task, evidence='   '), 64)
        self.assertEqual(self.raw(task), before)

    def test_queue_head_failure_releases_reservation_and_keeps_cancel_available(self):
        task, assignment = self.planned()
        args = SimpleNamespace(
            state_dir=self.state, timeout=0, no_wait=False, max_workers=1,
            coord_task=task['id'], coord_assignment=assignment,
            resume_after_failure=False)

        def changed_while_waiting(state, **options):
            subprocess.run(['git', '-C', str(self.repo), '-c', 'user.name=Test',
                            '-c', 'user.email=test@example.test', 'commit', '-qm',
                            'queue-head-change', '--allow-empty'], check=True)
            options['validate']()
            self.fail('stale HEAD acquired a slot')

        with mock.patch.object(worker, 'acquire_slot', side_effect=changed_while_waiting):
            with self.assertRaises(worker.WorkerError) as caught:
                worker.acquire_job_slot(
                    args, policy=settings.resolve(self.repo), root=self.repo)
        self.assertEqual(caught.exception.code, 78)
        self.assertIn('HEAD', caught.exception.message)
        row = self.row(task)
        self.assertEqual(row['status'], 'planned')
        self.assertEqual(row['queue_state'], 'blocked')
        self.assertNotIn('launch_reservation', row)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.cancel(task), 0)
        self.assert_cancelled(coordination.load_task(self.repo, task['id']), assignment)

    def test_pending_scope_is_released_and_unrelated_scope_still_denied(self):
        for runtime in RUNTIMES:
            with self.subTest(runtime=runtime):
                self.session = 'scope-' + runtime
                task, assignment = self.planned(turn='scope-' + runtime)
                hook = lambda path, runtime=runtime: coordinator_hooks.handle({
                    'session_id': self.session, 'turn_id': 'scope-' + runtime,
                    'cwd': str(self.repo), 'hook_event_name': 'PreToolUse',
                    'permission_mode': 'default', 'tool_name': 'Edit',
                    'tool_input': {'file_path': str(self.repo / path)},
                }, runtime=runtime)
                before = hook('a.py')
                self.assertEqual(before['hookSpecificOutput']['permissionDecision'], 'deny')
                self.assertIn(
                    'pending worker assignment',
                    before['hookSpecificOutput']['permissionDecisionReason'])
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(self.cancel(task), 0)
                self.assertNotIn('permissionDecision', hook('a.py'))
                outside = hook('b.py')
                self.assertEqual(outside['hookSpecificOutput']['permissionDecision'], 'deny')
                self.assertNotIn(
                    'pending worker assignment',
                    outside['hookSpecificOutput']['permissionDecisionReason'])

    def test_stale_head_cancel_completes_then_next_turn_uses_current_head(self):
        task, assignment = self.planned()
        old_head = task['base_head']
        (self.repo / 'a.py').write_text('VALUE = 2\n')
        subprocess.run(['git', '-C', str(self.repo), '-c', 'user.name=Test',
                        '-c', 'user.email=test@example.test', 'commit', '-qam', 'next'],
                       check=True)
        new_head = subprocess.run(['git', '-C', str(self.repo), 'rev-parse', 'HEAD'],
                                  check=True, capture_output=True, text=True).stdout.strip()
        self.assertNotEqual(old_head, new_head)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.cancel(task), 0)
        completed = coordination.complete_task(self.repo, task['id'])
        self.assertEqual(completed['status'], 'completed')
        self.assertEqual(completed['base_head'], old_head)
        self.assertEqual(completed['policy'], task['policy'])
        following = coordination.begin_turn(
            self.repo, session_id=self.session, turn_id='turn-2',
            prompt='next work', policy=settings.resolve(self.repo))
        self.assertNotEqual(following['id'], task['id'])
        self.assertEqual(following['base_head'], new_head)

    def test_stop_hook_accepts_cancelled_terminal_history_for_both_runtimes(self):
        for runtime in RUNTIMES:
            with self.subTest(runtime=runtime):
                self.session = 'stop-' + runtime
                task, assignment = self.planned(turn='stop-' + runtime)
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(self.cancel(task), 0)
                stop = coordinator_hooks.handle({
                    'session_id': self.session, 'turn_id': 'turn-1', 'cwd': str(self.repo),
                    'hook_event_name': 'Stop', 'permission_mode': 'default',
                    'stop_hook_active': False,
                }, runtime=runtime)
                self.assertNotIn('decision', stop)
                self.assertEqual(coordination.load_task(self.repo, task['id'])['status'], 'completed')

    def test_independent_precreated_copy_is_preserved(self):
        task, assignment = self.planned()
        copy = workspace.create(self.repo, self.state)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.cancel(task), 0)
        self.assertTrue(copy.path.is_dir())
        self.assertTrue((copy.directory / 'record.json').is_file())
        self.assertEqual(coordination.load_task(self.repo, task['id'])['assignments'][0]['status'],
                         'failed')


class ReservationOwnershipTests(CancelFixture):
    def test_live_reservation_blocks_cancel_with_stop_guidance(self):
        task, assignment = self.planned()
        reservation = self.reserve(task, assignment)
        self.assertGreaterEqual(reservation['generation'], 1)
        with contextlib.redirect_stderr(io.StringIO()) as error:
            self.assertEqual(self.cancel(task), 75)
        message = error.getvalue()
        self.assertIn('live', message.lower())
        self.assertIn('stop', message.lower())
        self.assertEqual(self.row(task)['status'], 'planned')

    def test_dead_reservation_can_be_cancelled(self):
        task, assignment = self.planned()
        code = self.script('''
from pathlib import Path
import sys
from codex_deepseek_team import coordination
task_id, assignment_id, root = sys.argv[1:4]
coordination.register_launch_reservation(Path(root), task_id, assignment_id)
''')
        result = subprocess.run([sys.executable, '-c', code, task['id'], assignment,
                                 str(self.repo)], env=self.env, cwd=self.repo,
                                text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.cancel(task), 0)
        self.assert_cancelled(coordination.load_task(self.repo, task['id']), assignment)

    def test_unknown_owner_state_fails_closed(self):
        task, assignment = self.planned()
        self.reserve(task, assignment)
        with mock.patch.object(workspace_lifecycle, 'identity_state', return_value='unknown'):
            with contextlib.redirect_stderr(io.StringIO()) as error:
                self.assertEqual(self.cancel(task), 75)
        self.assertIn('uninspectable', error.getvalue().lower())
        self.assertEqual(self.row(task)['status'], 'planned')

    def test_allocation_marker_blocks_cancel_even_after_owner_death(self):
        task, assignment = self.planned()
        reservation = self.reserve(task, assignment)
        coordination.begin_launch_allocation(self.repo, task['id'], assignment, reservation)
        code = self.script('''
from pathlib import Path
import sys
from codex_deepseek_team import workspace_lifecycle
identity = workspace_lifecycle.process_identity(int(sys.argv[1]))
print(identity['start_time'] if identity else '')
''')
        child = subprocess.Popen([sys.executable, '-c', code, str(10 ** 8)], env=self.env,
                                 text=True, stdout=subprocess.PIPE)
        child.communicate(timeout=20)
        with contextlib.redirect_stderr(io.StringIO()) as error:
            self.assertEqual(self.cancel(task), 75)
        self.assertIn('allocation', error.getvalue().lower())
        self.assertEqual(self.row(task)['status'], 'planned')

    def test_bound_foreign_live_ticket_does_not_block_legacy_cancel(self):
        target, assignment = self.planned()
        foreign = dict(task_id='task-' + '1' * 20, assignment_id='as-' + '1' * 16,
                       token='foreign-holder', generation=1)
        holder = worker_slots.acquire(self.state, limit=1, binding=foreign)
        ready = self.root / 'foreign-ready'
        body = f'''
from pathlib import Path
import sys
from codex_deepseek_team import worker_slots
binding = dict(task_id='task-' + '1' * 20, assignment_id='as-' + '1' * 16,
               token='foreign-token', generation=1)
worker_slots.acquire(Path(sys.argv[1]), limit=1, timeout=20, binding=binding)
Path(sys.argv[2]).write_text('ready')
'''
        child = subprocess.Popen([sys.executable, '-c', self.script(body), str(self.state),
                                  str(ready)], env=self.env, text=True,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 10
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(list(self.state.glob('ticket-*')))
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(self.cancel(target), 0)
            self.assertEqual(self.row(target)['status'], 'failed')
        finally:
            os.close(holder)
            child.terminate()
            child.communicate(timeout=10)

    def test_unbound_live_legacy_ticket_fails_closed(self):
        target, assignment = self.planned()
        holder = worker_slots.acquire(self.state, limit=1)
        body = '''
from pathlib import Path
import sys
from codex_deepseek_team import worker_slots
worker_slots.acquire(Path(sys.argv[1]), limit=1, timeout=20)
'''
        child = subprocess.Popen([sys.executable, '-c', self.script(body), str(self.state)],
                                 env=self.env, text=True,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 10
            while not list(self.state.glob('ticket-*')) and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(list(self.state.glob('ticket-*')))
            with contextlib.redirect_stderr(io.StringIO()) as error:
                self.assertEqual(self.cancel(target), 75)
            self.assertIn('ambiguous', error.getvalue().lower())
        finally:
            os.close(holder)
            child.terminate()
            child.communicate(timeout=10)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.cancel(target), 0)

    def test_target_bound_live_ticket_blocks_until_the_waiter_exits(self):
        target, assignment = self.planned()
        foreign = dict(task_id='task-' + '3' * 20, assignment_id='as-' + '3' * 16,
                       token='ticket-foreign-holder', generation=1)
        holder = worker_slots.acquire(self.state, limit=1, binding=foreign)
        ready = self.root / 'ticket-ready'
        go = self.root / 'ticket-go'
        granted = self.root / 'ticket-granted'
        body = '''
from pathlib import Path
import sys
from codex_deepseek_team import worker_slots
slot = worker_slots.acquire(Path(sys.argv[6]), limit=1, binding=reservation)
Path(sys.argv[7]).write_text('granted')
'''
        child = subprocess.Popen([
            sys.executable, '-c', self.registration_script(body), str(self.repo),
            target['id'], assignment, str(ready), str(go), str(self.state),
            str(granted)], env=self.env, cwd=self.repo, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            self.wait_ready(ready)
            go.write_text('go')
            deadline = time.monotonic() + 10
            ticket = None
            while time.monotonic() < deadline and ticket is None:
                try:
                    artifacts = worker_slots.inspect_bindings(self.state)
                except worker_slots.SlotError as error:
                    if error.code != 75:
                        raise
                    time.sleep(0.02)
                    continue
                for artifact in artifacts:
                    binding = artifact.get('binding')
                    if (artifact['kind'] == 'ticket' and binding is not None
                            and binding['task_id'] == target['id']
                            and binding['assignment_id'] == assignment):
                        ticket = artifact
                        break
                if ticket is None:
                    time.sleep(0.02)
            self.assertIsNotNone(ticket, 'target launch never entered the queue')
            with contextlib.redirect_stderr(io.StringIO()) as error:
                self.assertEqual(self.cancel(target), 75)
            self.assertIn('live', error.getvalue().lower())
            self.assertIn('stop', error.getvalue().lower())
            self.assertEqual(self.row(target)['status'], 'planned')
        except BaseException:
            child.terminate()
            child.communicate(timeout=10)
            raise
        finally:
            os.close(holder)
        _stdout, stderr = child.communicate(timeout=20)
        self.assertEqual(child.returncode, 0, stderr)
        self.assertTrue(granted.exists())
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.cancel(target), 0)
        self.assert_cancelled(coordination.load_task(self.repo, target['id']), assignment)

    def test_live_slot_bound_to_target_blocks_and_foreign_slot_does_not(self):
        target, assignment = self.planned()
        reservation = self.reserve(target, assignment)
        foreign = dict(task_id='task-' + '2' * 20, assignment_id='as-' + '2' * 16,
                       token='foreign', generation=1)
        slot = worker_slots.acquire(self.state, limit=2, binding=foreign)
        try:
            with contextlib.redirect_stderr(io.StringIO()) as error:
                self.assertEqual(self.cancel(target), 75)
            self.assertIn('live', error.getvalue().lower())
        finally:
            os.close(slot)
        bound = worker_slots.acquire(self.state, limit=2, binding=reservation)
        try:
            with contextlib.redirect_stderr(io.StringIO()) as error:
                self.assertEqual(self.cancel(target), 75)
            self.assertIn('live', error.getvalue().lower())
        finally:
            os.close(bound)


class LaunchProcessBarrierTests(CancelFixture):
    def test_cancel_wins_at_preallocation_barrier_and_launcher_cannot_allocate(self):
        task, assignment = self.planned()
        ready, go = self.root / 'ready', self.root / 'go'
        created = self.root / 'created'
        child = subprocess.Popen([
            sys.executable, '-c', self.registration_script(), str(self.repo),
            task['id'], assignment, str(ready), str(go)], env=self.env, cwd=self.repo,
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            reservation = self.wait_ready(ready)
            child.terminate()
            child.communicate(timeout=20)
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(self.cancel(task), 0)
            late = self.script(f'''
from pathlib import Path
import json, sys
from codex_deepseek_team import coordination
root, task_id, assignment_id, created = sys.argv[1:5]
reservation = json.loads(sys.stdin.read())
try:
    coordination.begin_launch_allocation(Path(root), task_id, assignment_id, reservation)
except coordination.CoordinationError as error:
    print(error.code, error.message)
else:
    Path(created).write_text('allocation')
    raise SystemExit(9)
''')
            late_result = subprocess.run(
                [sys.executable, '-c', late, str(self.repo), task['id'], assignment,
                 str(created)], input=json.dumps(reservation), env=self.env, cwd=self.repo,
                text=True, capture_output=True, timeout=20)
            self.assertEqual(late_result.returncode, 0, late_result.stderr)
            self.assertIn('cancelled', late_result.stdout.lower())
            self.assertFalse(created.exists())
        finally:
            if child.poll() is None:
                child.terminate()
                child.communicate(timeout=10)

    def test_allocation_winner_blocks_cancel_from_barrier(self):
        task, assignment = self.planned()
        ready, go = self.root / 'ready-alloc', self.root / 'go-alloc'
        body = '''
coordination.begin_launch_allocation(root, task_id, assignment_id, reservation)
Path(str(go) + '.allocated').write_text('allocated')
'''
        child = subprocess.Popen([
            sys.executable, '-c', self.registration_script(body), str(self.repo),
            task['id'], assignment, str(ready), str(go)], env=self.env, cwd=self.repo,
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            self.wait_ready(ready)
            go.write_text('go')
            deadline = time.monotonic() + 10
            while not Path(str(go) + '.allocated').exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(Path(str(go) + '.allocated').exists())
            with contextlib.redirect_stderr(io.StringIO()) as error:
                self.assertEqual(self.cancel(task), 75)
            self.assertIn('allocation', error.getvalue().lower())
            child.communicate(timeout=20)
            self.assertEqual(child.returncode, 0)
        finally:
            if child.poll() is None:
                child.terminate()
                child.communicate(timeout=10)

    def test_cancel_refuses_after_grant_before_allocation_then_wins(self):
        task, assignment = self.planned()
        ready, go = self.root / 'grant-ready', self.root / 'grant-go'
        body = '''
from pathlib import Path
import sys, time
from codex_deepseek_team import worker_slots
slot = worker_slots.acquire(Path(sys.argv[6]), limit=1, binding=reservation)
Path(str(go) + '.granted').write_text('granted')
while not Path(str(go) + '.hold').exists():
    time.sleep(0.02)
'''
        child = subprocess.Popen([
            sys.executable, '-c', self.registration_script(body), str(self.repo),
            task['id'], assignment, str(ready), str(go), str(self.state)],
            env=self.env, cwd=self.repo, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            self.wait_ready(ready)
            go.write_text('go')
            granted = Path(str(go) + '.granted')
            deadline = time.monotonic() + 10
            while not granted.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(granted.exists(), 'launcher never received a slot grant')
            with contextlib.redirect_stderr(io.StringIO()) as error:
                self.assertEqual(self.cancel(task), 75)
            self.assertIn('live', error.getvalue().lower())
            self.assertEqual(self.row(task)['status'], 'planned')
            Path(str(go) + '.hold').write_text('hold')
            _stdout, stderr = child.communicate(timeout=20)
            self.assertEqual(child.returncode, 0, stderr)
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(self.cancel(task), 0)
            self.assert_cancelled(coordination.load_task(self.repo, task['id']), assignment)
        finally:
            if child.poll() is None:
                child.terminate()
                child.communicate(timeout=10)

    def test_live_registered_launcher_blocks_cancel_until_it_exits(self):
        task, assignment = self.planned()
        ready, go = self.root / 'live-ready', self.root / 'live-go'
        child = subprocess.Popen([
            sys.executable, '-c', self.registration_script(), str(self.repo),
            task['id'], assignment, str(ready), str(go)], env=self.env, cwd=self.repo,
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            self.wait_ready(ready)
            with contextlib.redirect_stderr(io.StringIO()) as error:
                self.assertEqual(self.cancel(task), 75)
            self.assertIn('live', error.getvalue().lower())
            self.assertEqual(self.row(task)['status'], 'planned')
        finally:
            child.terminate()
            child.communicate(timeout=20)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.cancel(task), 0)
        self.assert_cancelled(coordination.load_task(self.repo, task['id']), assignment)

    def test_worker_admission_issues_no_ticket_or_copy_after_cancel(self):
        task, assignment = self.planned()
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.cancel(task), 0)
        before = set(self.state.glob('ticket-*'))
        body = '''
from types import SimpleNamespace
from pathlib import Path
import sys
from codex_deepseek_team import settings, worker
root, task_id, assignment_id, state = Path(sys.argv[1]), sys.argv[2], sys.argv[3], Path(sys.argv[4])
args = SimpleNamespace(state_dir=state, timeout=0, no_wait=False, max_workers=1,
    coord_task=task_id, coord_assignment=assignment_id, resume_after_failure=False,
    job_deadline=None, workspace=None)
try:
    worker.acquire_job_slot(args, policy=settings.resolve(root), root=root)
except worker.WorkerError as error:
    print(error.code, error.message)
    raise SystemExit(0)
raise SystemExit(9)
'''
        result = self.run_script(body, self.repo, task['id'], assignment, self.state)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(result.stdout.startswith('78 '), result.stdout)
        self.assertIn('cancelled', result.stdout.lower())
        self.assertEqual(set(self.state.glob('ticket-*')), before)
        self.assertFalse((self.state / 'workspaces').exists())


    def test_late_cancelled_worker_entrypoint_reads_no_provider_key(self):
        task, assignment = self.planned()
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.cancel(task), 0)
        body = '''
from pathlib import Path
from types import SimpleNamespace
import sys
from unittest import mock
from codex_deepseek_team import settings, worker
root, task_id, assignment_id, state = Path(sys.argv[1]), sys.argv[2], sys.argv[3], Path(sys.argv[4])
args = SimpleNamespace(
    state_dir=state, workspace=None, task='must not run', runtime='codex', codex='codex',
    claude='claude', os_sandbox='required', timeout=0, no_wait=False, max_workers=1,
    attempts=1, attempts_explicit=False, effort='high', access=None,
    delegation_level=None, check_timeout=None, coord_task=task_id,
    coord_assignment=assignment_id, resume_after_failure=False,
    job_deadline=None, validate_admission=None)
with mock.patch.object(worker, 'load_api_key',
                       side_effect=AssertionError('provider key read after cancellation')):
    try:
        worker.run(args)
    except worker.WorkerError as error:
        print(error.code, error.message)
        raise SystemExit(0)
raise SystemExit(9)
'''
        result = self.run_script(body, self.repo, task['id'], assignment, self.state)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(result.stdout.startswith('78 '), result.stdout)
        self.assertIn('cancelled', result.stdout.lower())
        self.assertEqual(list(self.state.glob('ticket-*')), [])
        self.assertFalse((self.state / 'workspaces').exists())

if __name__ == '__main__':
    unittest.main()
