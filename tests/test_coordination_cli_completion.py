"""CLI completion closes only fully terminal, correctly distributed tasks.

The completion command must reuse ``coordination.complete_task`` exactly: it
performs the same distribution, worker-disposition and coordinator/native
outcome validation under the same ledger lock. These are compatibility tests
against real temporary Git and private ledger state, driven from a non-Git
working directory through ``--path``.
"""
import contextlib
import io
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from codex_deepseek_team import coordination, coordination_cli, settings
from codex_deepseek_team.routing import RoutingService


class CompletionCliCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='dst-complete-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        subprocess.run(['git', 'init', '-q', str(self.repo)], check=True)
        (self.repo / 'a.py').write_text('VALUE = 1\n')
        (self.repo / 'src').mkdir()
        (self.repo / 'src' / 'core.py').write_text('VALUE = 1\n')
        subprocess.run(['git', '-C', str(self.repo), 'add', '.'], check=True)
        subprocess.run(['git', '-C', str(self.repo), '-c', 'user.name=Test', '-c',
                        'user.email=test@example.test', 'commit', '-qm', 'base'],
                       check=True, capture_output=True)
        for relative in ('home', 'codex', 'claude', 'xdg/config'):
            (self.root / relative).mkdir(parents=True, exist_ok=True)
        self.overrides = {
            'HOME': str(self.root / 'home'),
            'CODEX_HOME': str(self.root / 'codex'),
            'CLAUDE_CONFIG_DIR': str(self.root / 'claude'),
            'XDG_CONFIG_HOME': str(self.root / 'xdg' / 'config'),
            'DEEPSEEK_TEAM_STATE_DIR': str(self.root / 'state'),
        }
        self.env = mock.patch.dict(os.environ, self.overrides)
        self.env.start()
        self.addCleanup(self.env.stop)
        # Complete from a directory that is not a Git repository; --path must
        # resolve the real project and its private ledger independently.
        self.non_git = self.root / 'elsewhere'
        self.non_git.mkdir()
        previous_cwd = os.getcwd()
        os.chdir(self.non_git)
        self.addCleanup(os.chdir, previous_cwd)
        settings.set_values(self.repo / settings.PROJECT_FILE,
                            delegation_level=75, access='full-access')

    def policy(self, level=75, access='full-access'):
        return settings.Policy(level, access, {
            'delegation_level': 'test',
            'access': 'test',
        })

    def start(self, session_id='sess-1'):
        return coordination.open_task(
            self.repo, session_id=session_id, turn_id='turn-1',
            prompt='implement feature', policy=self.policy())

    def plan(self, executor, *, session_id='plan', **overrides):
        task = coordination.open_task(
            self.repo, session_id=session_id, turn_id='turn-1',
            prompt='implement feature', policy=self.policy())
        item = {'id': 'impl', 'kind': 'implementation', 'scope': ['src/core.py'],
                'executor': executor, 'acceptance': ['behavior verified'],
                'dependencies': [], 'checks': ['python3 -m unittest']}
        if executor == 'native-agent':
            item['delegation_reason'] = 'Independent implementation in an isolated context'
            item['native_exception'] = {
                'code': 'explicit_user_request',
                'evidence': 'user asked for a native implementation of this deliverable',
            }
        item.update(overrides)
        return coordination.plan_task(self.repo, task['id'], {
            'classification': 'small',
            'small_evidence': 'one exact non-wildcard file scope',
            'deliverables': [item],
        })

    def save(self, task):
        coordination._atomic(coordination._task_path(self.repo, task['id']), task)

    def complete(self, task_id):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = coordination_cli.main([
                'coordination', 'complete',
                '--path', str(self.repo), '--task', task_id,
            ])
        return code, stdout.getvalue(), stderr.getvalue()


class CompletionCliTests(CompletionCliCase):
    def test_complete_rejects_missing_distribution(self):
        task = self.start()
        code, stdout, stderr = self.complete(task['id'])
        self.assertEqual(code, 78)
        self.assertEqual(stdout, '')
        self.assertIn('Cannot complete task', stderr)
        self.assertIn('distribution plan is missing', stderr)
        self.assertEqual(coordination.load_task(self.repo, task['id'])['status'], 'planning')

    def test_complete_rejects_unfinished_and_undisposed_worker(self):
        task = self.plan('worker')
        for status in ('planned', 'running'):
            with self.subTest(status=status):
                task['assignments'][0]['status'] = status
                task['assignments'][0]['disposition'] = None
                self.save(task)
                code, stdout, stderr = self.complete(task['id'])
                self.assertEqual(code, 78)
                self.assertEqual(stdout, '')
                self.assertIn('worker assignment is unfinished', stderr)
        for status in ('succeeded', 'failed'):
            with self.subTest(status=status):
                task['assignments'][0]['status'] = status
                task['assignments'][0]['disposition'] = None
                self.save(task)
                code, stdout, stderr = self.complete(task['id'])
                self.assertEqual(code, 78)
                self.assertEqual(stdout, '')
                self.assertIn('worker result has no disposition', stderr)
        self.assertNotEqual(coordination.load_task(self.repo, task['id'])['status'], 'completed')
        task['assignments'][0]['disposition'] = {
            'kind': 'rejected', 'evidence': 'inspected the failed result'}
        self.save(task)
        code, stdout, stderr = self.complete(task['id'])
        self.assertEqual(code, 0, stderr)
        self.assertIn('status=completed', stdout)

    def test_complete_requires_terminal_coordinator_outcome(self):
        task = self.plan('coordinator')
        code, _, stderr = self.complete(task['id'])
        self.assertEqual(code, 78)
        self.assertIn('coordinator outcome is outstanding', stderr)
        for outcome in ('rework', 'rejected', 'infrastructure', 'unknown'):
            with self.subTest(outcome=outcome):
                coordination.observe_coordinator_result(
                    self.repo, task['id'], 'impl', outcome, 'recorded outcome')
                code, _, stderr = self.complete(task['id'])
                self.assertEqual(code, 78)
                self.assertIn('coordinator outcome is outstanding', stderr)
        coordination.observe_coordinator_result(
            self.repo, task['id'], 'impl', 'accepted', 'focused checks passed')
        code, stdout, stderr = self.complete(task['id'])
        self.assertEqual(code, 0, stderr)
        self.assertIn('status=completed', stdout)
        self.assertEqual(coordination.load_task(self.repo, task['id'])['status'], 'completed')

    def test_complete_rejects_invalidated_outcomes(self):
        for executor in ('coordinator', 'native-agent'):
            with self.subTest(executor=executor):
                task = self.plan(executor, session_id=f'invalidate-{executor}')
                coordination.observe_coordinator_result(
                    self.repo, task['id'], 'impl', 'accepted', 'checks before edit')
                coordination.record_coordinator_event(
                    self.repo, task['id'], 'mutation_requested', ['src/core.py'])
                code, _, stderr = self.complete(task['id'])
                self.assertEqual(code, 78)
                self.assertIn(f'{executor} outcome is outstanding', stderr)
                coordination.observe_coordinator_result(
                    self.repo, task['id'], 'impl', 'accepted', 'checks rerun after edit')
                code, stdout, stderr = self.complete(task['id'])
                self.assertEqual(code, 0, stderr)
                self.assertIn('status=completed', stdout)

    def test_complete_requires_terminal_native_outcome(self):
        task = self.plan('native-agent')
        code, _, stderr = self.complete(task['id'])
        self.assertEqual(code, 78)
        self.assertIn('native-agent outcome is outstanding', stderr)
        for outcome in ('rework', 'rejected', 'unknown'):
            with self.subTest(outcome=outcome):
                coordination.observe_coordinator_result(
                    self.repo, task['id'], 'impl', outcome, 'recorded outcome')
                code, _, stderr = self.complete(task['id'])
                self.assertEqual(code, 78)
                self.assertIn('native-agent outcome is outstanding', stderr)
        coordination.observe_coordinator_result(
            self.repo, task['id'], 'impl', 'cancelled', 'cancelled after review')
        code, stdout, stderr = self.complete(task['id'])
        self.assertEqual(code, 0, stderr)
        self.assertIn('status=completed', stdout)

    def test_current_accepted_or_cancelled_plans_complete(self):
        for executor in ('coordinator', 'native-agent'):
            for outcome in ('accepted', 'cancelled'):
                with self.subTest(executor=executor, outcome=outcome):
                    task = self.plan(executor, session_id=f'{executor}-{outcome}')
                    coordination.observe_coordinator_result(
                        self.repo, task['id'], 'impl', outcome, 'reviewed outcome')
                    code, stdout, stderr = self.complete(task['id'])
                    self.assertEqual(code, 0, stderr)
                    self.assertIn('status=completed', stdout)
                    self.assertEqual(
                        coordination.load_task(self.repo, task['id'])['status'], 'completed')

    def test_repeated_completion_is_safe_and_adds_no_observations(self):
        task = self.plan('coordinator')
        coordination.observe_coordinator_result(
            self.repo, task['id'], 'impl', 'accepted', 'focused checks passed')
        before = RoutingService(self.repo).observations()
        code, stdout, stderr = self.complete(task['id'])
        self.assertEqual(code, 0, stderr)
        first = coordination.load_task(self.repo, task['id'])
        history = first['deliverables'][0]['result']
        self.assertEqual(RoutingService(self.repo).observations(), before)
        code, stdout, stderr = self.complete(task['id'])
        self.assertEqual(code, 0, stderr)
        self.assertIn('status=completed', stdout)
        second = coordination.load_task(self.repo, task['id'])
        self.assertEqual(second['deliverables'][0]['result'], history)
        self.assertEqual(RoutingService(self.repo).observations(), before)


if __name__ == '__main__':
    unittest.main()
