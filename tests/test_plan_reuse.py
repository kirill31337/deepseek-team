"""Idempotent logical distributions and immutable reuse declarations."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from codex_deepseek_team import coordination, settings


class PlanReuseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='dst-plan-reuse-')
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.repo = base / 'repo'
        self.repo.mkdir()
        subprocess.run(['git', 'init', '-q', str(self.repo)], check=True)
        (self.repo / 'a.py').write_text('VALUE = 1\n')
        subprocess.run(['git', '-C', str(self.repo), 'add', '.'], check=True)
        subprocess.run(['git', '-C', str(self.repo), '-c', 'user.name=Tests', '-c',
                        'user.email=tests@example.invalid', 'commit', '-qm', 'base'],
                       check=True)
        self.state = base / 'state'
        self.env = mock.patch.dict(os.environ, {
            'DEEPSEEK_TEAM_STATE_DIR': str(self.state),
            'XDG_CONFIG_HOME': str(base / 'config'),
            'HOME': str(base / 'home'),
        })
        (base / 'home').mkdir()
        self.env.start()
        self.addCleanup(self.env.stop)
        settings.set_values(self.repo / settings.PROJECT_FILE,
                            delegation_level=75, access='full-access')

    def policy(self):
        return settings.Policy(75, 'full-access', {
            'delegation_level': 'test',
            'access': 'test',
        })

    def task(self, turn='1'):
        return coordination.open_task(
            self.repo, session_id='plan-reuse', turn_id=turn,
            prompt='implement the registered slice', policy=self.policy())

    def deliverable(self, **changes):
        item = {
            'id': 'impl', 'kind': 'implementation', 'scope': ['a.py'],
            'executor': 'worker', 'acceptance': ['works'],
            'dependencies': [], 'checks': [],
        }
        item.update(changes)
        return item

    def payload(self, **changes):
        return {'classification': 'substantial',
                'deliverables': [self.deliverable(**changes)]}

    def ledger_path(self, task):
        return coordination._task_path(self.repo, task['id'])

    def snapshot(self, task):
        info = self.ledger_path(task).stat()
        return self.ledger_path(task).read_bytes(), info.st_mtime_ns, info.st_ino

    def test_identical_plan_is_a_noop_before_routing_or_write_side_effects(self):
        task = self.task()
        first = coordination.plan_task(self.repo, task['id'], self.payload())
        before = self.snapshot(task)
        routing_ids = [item['routing']['decision_id'] for item in first['deliverables']]
        assignment_ids = [row['id'] for row in first['assignments']]

        with (
            mock.patch.object(coordination, '_prepare_routing',
                              side_effect=AssertionError('routing must not run')),
            mock.patch.object(coordination, '_routing_batch',
                              side_effect=AssertionError('routing transaction must not open')),
            mock.patch.object(coordination, '_atomic',
                              side_effect=AssertionError('ledger must not be rewritten')),
        ):
            repeated = coordination.plan_task(self.repo, task['id'], self.payload())

        self.assertEqual(self.snapshot(task), before)
        self.assertEqual([item['routing']['decision_id'] for item in repeated['deliverables']],
                         routing_ids)
        self.assertEqual([row['id'] for row in repeated['assignments']], assignment_ids)

    def test_identical_plan_after_execution_preserves_bytes_ids_outcomes_and_phase(self):
        task = self.task()
        planned = coordination.plan_task(self.repo, task['id'], self.payload())
        assignment = planned['assignments'][0]
        routing_id = planned['deliverables'][0]['routing']['decision_id']
        started = coordination.assignment_started(
            self.repo, task['id'], assignment['id'], 'ws', 'codex', [], effort='high')
        assignment_routing_id = started['assignments'][0]['routing_decision_id']
        coordination.assignment_finished(
            self.repo, task['id'], assignment['id'], 'succeeded',
            'implemented', ['a.py'], [{'exit_code': 0}])
        coordination.use_result(
            self.repo, task['id'], assignment['id'], 'incorporated', 'reviewed and accepted')
        completed = coordination.complete_task(self.repo, task['id'])
        self.assertEqual(completed['status'], 'completed')
        before = self.snapshot(task)

        repeated = coordination.plan_task(self.repo, task['id'], self.payload())

        self.assertEqual(self.snapshot(task), before)
        self.assertEqual(repeated['status'], 'completed')
        self.assertEqual(repeated['assignments'][0]['id'], assignment['id'])
        self.assertEqual(repeated['assignments'][0]['disposition']['kind'], 'incorporated')
        self.assertEqual(repeated['assignments'][0]['result_summary'], 'implemented')
        self.assertEqual(repeated['deliverables'][0]['routing']['decision_id'], routing_id)
        self.assertEqual(repeated['assignments'][0]['routing_decision_id'],
                         assignment_routing_id)

    def test_scope_checks_and_dependencies_are_real_revisions(self):
        changes = {
            'scope': {'scope': ['b.py']},
            'checks': {'checks': ['python3 -m unittest -q']},
            'dependencies': {'dependencies': [{'kind': 'command', 'value': 'python3'}]},
        }
        for field, change in changes.items():
            with self.subTest(field=field):
                task = self.task(turn='revision-' + field)
                first = coordination.plan_task(self.repo, task['id'], self.payload())
                before = self.snapshot(task)
                revised = coordination.plan_task(self.repo, task['id'],
                                                 self.payload(**change))
                self.assertNotEqual(self.snapshot(task)[0], before[0])
                self.assertEqual(revised['assignments'][0]['id'],
                                 first['assignments'][0]['id'])
                self.assertEqual(revised['deliverables'][0][field],
                                 self.payload(**change)['deliverables'][0][field])
                self.assertGreaterEqual(revised['updated_at'], first['updated_at'])

    def test_checks_reusable_is_a_strict_boolean_with_false_default(self):
        task = self.task()
        planned = coordination.plan_task(self.repo, task['id'], self.payload())
        self.assertIs(planned['deliverables'][0]['checks_reusable'], False)

        enabled = coordination.plan_task(
            self.repo, task['id'], self.payload(checks_reusable=True))
        self.assertIs(enabled['deliverables'][0]['checks_reusable'], True)

        for index, bad in enumerate((0, 1, 'true', None, [], {})):
            with self.subTest(value=bad), self.assertRaises(coordination.CoordinationError) as caught:
                coordination.plan_task(self.repo, self.task(turn=f'bad-{index}')['id'],
                                       self.payload(checks_reusable=bad))
            self.assertEqual(caught.exception.code, 64)
            self.assertIn('checks_reusable', caught.exception.message)

    def test_started_reuse_declaration_is_immutable_without_rewriting_the_ledger(self):
        task = self.task()
        planned = coordination.plan_task(self.repo, task['id'], self.payload())
        assignment_id = planned['assignments'][0]['id']
        coordination.assignment_started(
            self.repo, task['id'], assignment_id, 'ws', 'codex', [], effort='high')
        before = self.snapshot(task)

        with self.assertRaises(coordination.CoordinationError) as caught:
            coordination.plan_task(self.repo, task['id'],
                                   self.payload(checks_reusable=True))
        self.assertEqual(caught.exception.code, 64)
        self.assertIn('immutable', caught.exception.message)
        self.assertEqual(self.snapshot(task), before)

    def test_unsupported_plan_format_is_diagnosed_without_changes(self):
        task = self.task()
        coordination.plan_task(self.repo, task['id'], self.payload())
        path = self.ledger_path(task)
        saved = json.loads(path.read_text())
        saved['schema'] = 1
        saved.pop('routing_committed', None)
        coordination._atomic(path, saved)
        before = self.snapshot(task)

        with self.assertRaises(coordination.CoordinationError) as caught:
            coordination.plan_task(self.repo, task['id'], self.payload())
        self.assertEqual(caught.exception.code, 78)
        self.assertIn('Unsupported', caught.exception.message)
        self.assertEqual(self.snapshot(task), before)

    def test_crashed_schema_two_plan_is_repaired_instead_of_treated_as_a_noop(self):
        task = self.task()
        path = self.ledger_path(task)
        saved = {
            'schema': 2, 'id': task['id'], 'project': str(self.repo),
            'session_id': task['session_id'], 'turn_id': task['turn_id'],
            'prompt_sha256': task['prompt_sha256'], 'base_head': task['base_head'],
            'created_at': task['created_at'], 'updated_at': task['updated_at'],
            'status': 'planned', 'classification': 'substantial', 'small_evidence': None,
            'policy': task['policy'], 'saved_policy': task['saved_policy'],
            'runtime': 'codex',
            'deliverables': [dict(self.deliverable(), executor='worker',
                                  features={'kind': 'implementation', 'runtime': 'codex',
                                            'effort': 'high'})],
            'assignments': [], 'coordinator_events': [], 'constraints': [],
        }
        coordination._atomic(path, saved)

        repaired = coordination.plan_task(self.repo, task['id'], self.payload())

        self.assertIs(repaired.get('routing_committed'), True)
        self.assertIn('routing', repaired['deliverables'][0])

    def test_reuse_declaration_is_preserved_in_execution_snapshot(self):
        task = self.task()
        planned = coordination.plan_task(
            self.repo, task['id'], self.payload(checks_reusable=True))
        row = coordination.assignment_started(
            self.repo, task['id'], planned['assignments'][0]['id'],
            'ws', 'codex', [], effort='high')
        self.assertIs(row['assignments'][0]['execution_snapshot']
                      ['deliverable']['checks_reusable'], True)


if __name__ == '__main__':
    unittest.main()
