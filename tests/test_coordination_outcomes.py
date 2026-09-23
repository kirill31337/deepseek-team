"""Completion depends on explicit, current outcomes for every executor."""
import contextlib
import io

from codex_deepseek_team import coordination, coordination_cli
from codex_deepseek_team.routing import RoutingService
from test_coordination import CoordinationCase


class OutcomeCase(CoordinationCase):
    def start(self, level=25, access='full-access'):
        return coordination.open_task(self.repo, session_id='sess-1', turn_id='turn-1',
                                      prompt='implement feature', policy=self.policy(level, access))


class DeliverableOutcomeTests(OutcomeCase):
    def plan(self, executor='coordinator', **changes):
        self.plan_number = getattr(self, 'plan_number', 0) + 1
        task = coordination.open_task(
            self.repo, session_id='outcomes', turn_id=str(self.plan_number),
            prompt='implement feature', policy=self.policy(25))
        item = dict(id='impl', kind='implementation', scope=['src/core.py'],
                    executor=executor, acceptance=['behavior verified'],
                    dependencies=[], checks=['python3 -m unittest'])
        if executor == 'native-agent':
            item['delegation_reason'] = 'Independent implementation in an isolated context'
        item.update(changes)
        self.plan_input = {'classification': 'substantial', 'deliverables': [item]}
        return coordination.plan_task(self.repo, task['id'], self.plan_input)

    def issues(self, task):
        self.assertTrue(hasattr(coordination, 'completion_issues'),
                        'completion must inspect deliverable outcomes')
        return coordination.completion_issues(task)

    def save(self, task):
        # Simulate a persisted pre-migration ledger, not a supplied plan.
        coordination._atomic(coordination._task_path(self.repo, task['id']), task)

    def test_direct_completion_rejects_missing_distribution(self):
        task = self.start(25)
        with self.assertRaises(coordination.CoordinationError):
            coordination.complete_task(self.repo, task['id'])

    def test_direct_completion_requires_coordinator_and_native_outcomes(self):
        for executor in ('coordinator', 'native-agent'):
            with self.subTest(executor=executor):
                task = self.plan(executor)
                with self.assertRaises(coordination.CoordinationError):
                    coordination.complete_task(self.repo, task['id'])

    def test_native_cli_result_completes_without_training_coordinator_router(self):
        task = self.plan('native-agent')
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            result = coordination_cli.main([
                'coordination', 'result', '--path', str(self.repo), '--task', task['id'],
                '--deliverable', 'impl', '--outcome', 'accepted', '--evidence', 'focused tests passed',
            ])
        self.assertEqual(result, 0)
        task = coordination.load_task(self.repo, task['id'])
        outcome = task['deliverables'][0].get('result', {})
        self.assertEqual(outcome.get('outcome'), 'accepted')
        self.assertEqual(outcome.get('evidence'), 'focused tests passed')
        self.assertIsInstance(outcome.get('recorded_at'), (int, float))
        self.assertEqual(RoutingService(self.repo).observations(), [])
        self.assertEqual(coordination.complete_task(self.repo, task['id'])['status'], 'completed')

    def test_only_accepted_or_cancelled_are_terminal(self):
        task = self.plan()
        for outcome in ('rework', 'rejected', 'infrastructure', 'unknown', 'accepted', 'cancelled'):
            with self.subTest(outcome=outcome):
                task = coordination.observe_coordinator_result(
                    self.repo, task['id'], 'impl', outcome, 'verified outcome')
                self.assertEqual(bool(self.issues(task)), outcome not in ('accepted', 'cancelled'))

    def test_summary_shows_pending_recorded_and_invalidated_nonworker_outcomes(self):
        for executor in ('coordinator', 'native-agent'):
            with self.subTest(executor=executor):
                task = self.plan(executor)
                summary = coordination.summary(task)
                self.assertIn('status=planned', summary)
                self.assertIn(executor + ' deliverable=impl', summary)
                self.assertIn('outcome=pending', summary)
                task = coordination.observe_coordinator_result(
                    self.repo, task['id'], 'impl', 'accepted', 'focused tests passed')
                self.assertIn('outcome=accepted', coordination.summary(task))
                coordination.record_coordinator_event(self.repo, task['id'], 'mutation_requested', [])
                task = coordination.load_task(self.repo, task['id'])
                self.assertIn('outcome=invalidated', coordination.summary(task))

    def test_evidence_alone_cannot_complete_a_legacy_deliverable(self):
        task = self.plan()
        task['deliverables'][0]['result_evidence'] = 'claimed complete'
        self.assertTrue(self.issues(task))

    def test_legacy_coordinator_feedback_counts_but_last_failed_outcome_wins(self):
        task = self.plan()
        for outcome in ('accepted', 'rejected', 'cancelled'):
            task = coordination.observe_coordinator_result(
                self.repo, task['id'], 'impl', outcome, 'legacy verification')
            task['deliverables'][0].pop('result', None)
            self.save(task)
            self.assertEqual(bool(self.issues(task)), outcome == 'rejected')

    def test_historical_mutation_events_invalidate_only_later_matching_outcomes(self):
        for legacy in (False, True):
            with self.subTest(legacy=legacy):
                task = self.plan()
                task = coordination.observe_coordinator_result(
                    self.repo, task['id'], 'impl', 'accepted', 'verified before old hook edit')
                item = task['deliverables'][0]
                if legacy:
                    item.pop('result')
                    recorded_at = item['routing_feedback'][-1]['observation']['observed_at']
                else:
                    recorded_at = item['result']['recorded_at']
                task['coordinator_events'] = [
                    {'kind': 'mutation_requested', 'paths': ['src/core.py'], 'at': recorded_at - 1},
                    {'kind': 'mutation_requested', 'paths': ['README.md'], 'at': recorded_at + 1},
                ]
                self.assertFalse(self.issues(task))
                task['coordinator_events'].append(
                    {'kind': 'mutation_requested', 'paths': ['src/core.py'], 'at': recorded_at + 1})
                self.save(task)
                self.assertTrue(self.issues(task))
                self.assertIn('outcome=invalidated', coordination.summary(task))
                with self.assertRaises(coordination.CoordinationError):
                    coordination.complete_task(self.repo, task['id'])

    def test_plan_cannot_inject_result_metadata(self):
        task = self.plan(result={'outcome': 'accepted', 'evidence': 'forged', 'recorded_at': 1},
                         result_evidence='forged', result_invalidated_at=0)
        self.assertTrue(self.issues(task))
        item = task['deliverables'][0]
        self.assertNotIn('result', item)
        self.assertNotIn('result_evidence', item)
        self.assertNotIn('result_invalidated_at', item)

    def test_native_result_survives_equivalent_replan_but_changed_scope_is_rejected(self):
        task = self.plan('native-agent')
        task = coordination.observe_coordinator_result(
            self.repo, task['id'], 'impl', 'accepted', 'native checks passed')
        before = task['deliverables'][0]['result']
        task = coordination.plan_task(self.repo, task['id'], self.plan_input)
        self.assertEqual(task['deliverables'][0]['result'], before)
        self.assertFalse(self.issues(task))
        self.plan_input['deliverables'][0]['scope'] = ['src/different.py']
        with self.assertRaises(coordination.CoordinationError):
            coordination.plan_task(self.repo, task['id'], self.plan_input)

    def test_mutation_invalidates_current_and_legacy_acceptance_only_for_matching_scope(self):
        for legacy in (False, True):
            for paths in (['src/core.py'], [str(self.repo / 'src/core.py')],
                          ['src/../src/core.py'], ['src'], []):
                with self.subTest(legacy=legacy, paths=paths):
                    task = self.plan()
                    task = coordination.observe_coordinator_result(
                        self.repo, task['id'], 'impl', 'accepted', 'fresh checks passed')
                    if legacy:
                        task['deliverables'][0].pop('result', None)
                        self.save(task)
                    coordination.record_coordinator_event(self.repo, task['id'], 'mutation_requested', ['README.md'])
                    self.assertFalse(self.issues(coordination.load_task(self.repo, task['id'])))
                    coordination.record_coordinator_event(self.repo, task['id'], 'mutation_requested', paths)
                    task = coordination.load_task(self.repo, task['id'])
                    self.assertTrue(self.issues(task))
                    task = coordination.plan_task(self.repo, task['id'], self.plan_input)
                    self.assertTrue(self.issues(task), 'replanning must not restore stale acceptance')

    def test_mutation_invalidates_glob_and_directory_scopes(self):
        for scope in ('src/*', 'src', '.'):
            with self.subTest(scope=scope):
                task = self.plan(scope=[scope])
                coordination.observe_coordinator_result(self.repo, task['id'], 'impl', 'accepted', 'verified')
                coordination.record_coordinator_event(self.repo, task['id'], 'mutation_requested', ['src/core.py'])
                self.assertTrue(self.issues(coordination.load_task(self.repo, task['id'])))

    def test_resolved_mutations_invalidate_file_and_directory_alias_scopes(self):
        (self.repo / 'alias.py').symlink_to('a.py')
        (self.repo / 'src').mkdir()
        (self.repo / 'src/core.py').write_text('VALUE = 1\n')
        (self.repo / 'aliasdir').symlink_to('src', target_is_directory=True)
        cases = [('alias.py', 'a.py'), ('aliasdir', 'src/core.py'),
                 ('aliasdir/core.py', 'src/core.py'), ('src/core.py', 'aliasdir/core.py')]
        for scope, path in cases:
            with self.subTest(scope=scope, path=path):
                task = self.plan(scope=[scope])
                coordination.observe_coordinator_result(
                    self.repo, task['id'], 'impl', 'accepted', 'verified before alias edit')
                coordination.record_coordinator_event(self.repo, task['id'], 'mutation_requested', [path])
                self.assertTrue(self.issues(coordination.load_task(self.repo, task['id'])))
                with self.assertRaises(coordination.CoordinationError):
                    coordination.complete_task(self.repo, task['id'])

    def test_resolving_directory_alias_preserves_wildcard_scope_matching(self):
        (self.repo / 'src').mkdir()
        (self.repo / 'aliasdir').symlink_to('src', target_is_directory=True)
        for scope in ('aliasdir/*.py', 'aliasdir/core?.py', 'aliasdir/[c]*.py'):
            with self.subTest(scope=scope):
                task = self.plan(scope=[scope])
                coordination.observe_coordinator_result(
                    self.repo, task['id'], 'impl', 'accepted', 'verified before alias edit')
                coordination.record_coordinator_event(
                    self.repo, task['id'], 'mutation_requested', ['src/README.md'])
                self.assertFalse(self.issues(coordination.load_task(self.repo, task['id'])))
                coordination.record_coordinator_event(
                    self.repo, task['id'], 'mutation_requested', ['src/core1.py'])
                self.assertTrue(self.issues(coordination.load_task(self.repo, task['id'])))

    def test_completed_task_reopens_after_mutation_and_requires_fresh_result(self):
        for executor in ('coordinator', 'native-agent'):
            with self.subTest(executor=executor):
                task = self.plan(executor)
                coordination.observe_coordinator_result(
                    self.repo, task['id'], 'impl', 'accepted', 'initial checks passed')
                coordination.complete_task(self.repo, task['id'])
                coordination.record_coordinator_event(
                    self.repo, task['id'], 'mutation_requested', ['src/core.py'])
                task = coordination.load_task(self.repo, task['id'])
                self.assertEqual(task['status'], 'active')
                self.assertNotIn('completed_at', task)
                self.assertEqual(coordination.active_task(self.repo, 'outcomes')['id'], task['id'])
                with self.assertRaises(coordination.CoordinationError):
                    coordination.complete_task(self.repo, task['id'])
                coordination.observe_coordinator_result(
                    self.repo, task['id'], 'impl', 'accepted', 'checks rerun after edit')
                self.assertEqual(coordination.complete_task(self.repo, task['id'])['status'], 'completed')

    def test_direct_completion_rejects_pending_and_undisposed_workers(self):
        task = self.plan('worker')
        for status in ('planned', 'running', 'succeeded', 'failed'):
            with self.subTest(status=status):
                task['assignments'][0]['status'] = status
                self.save(task)
                with self.assertRaises(coordination.CoordinationError):
                    coordination.complete_task(self.repo, task['id'])
        task['assignments'][0]['disposition'] = {'kind': 'rejected', 'evidence': 'inspected failure'}
        self.save(task)
        self.assertEqual(coordination.complete_task(self.repo, task['id'])['status'], 'completed')


class UnstartedTaskTests(OutcomeCase):
    def close(self, task):
        self.assertTrue(hasattr(coordination, 'close_unstarted_task'),
                        'unstarted conversations need a non-completion closure')
        return coordination.close_unstarted_task(self.repo, task['id'])

    def test_closed_draft_is_not_completed_and_next_turn_opens_new_task(self):
        task = self.start(25)
        self.assertTrue(self.close(task))
        self.assertTrue(self.close(task))
        task = coordination.load_task(self.repo, task['id'])
        self.assertEqual(task['status'], 'closed')
        self.assertIn('status=closed', coordination.summary(task))
        self.assertNotIn('completed_at', task)
        self.assertIsNone(coordination.active_task(self.repo, 'sess-1'))
        following = coordination.begin_turn(self.repo, session_id='sess-1', turn_id='turn-2',
                                            prompt='implement next feature', policy=self.policy(25))
        self.assertNotEqual(task['id'], following['id'])

    def test_close_refuses_planned_or_attempted_work(self):
        task = self.start(25)
        for field, value in (('classification', 'small'), ('deliverables', [{'id': 'impl'}]),
                             ('assignments', [{'status': 'planned'}]),
                             ('coordinator_events', [{'kind': 'mutation_requested', 'paths': []}])):
            with self.subTest(field=field):
                changed = dict(task, **{field: value})
                coordination._atomic(coordination._task_path(self.repo, task['id']), changed)
                self.assertFalse(self.close(changed))
                self.assertNotEqual(coordination.load_task(self.repo, task['id'])['status'], 'closed')

    def test_status_of_closed_draft_has_no_missing_plan_warning_but_cannot_complete(self):
        task = self.start(25)
        self.assertTrue(self.close(task))
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = coordination_cli.main([
                'coordination', 'status', '--path', str(self.repo), '--task', task['id']])
        self.assertEqual(result, 0)
        self.assertIn('status=closed', output.getvalue())
        self.assertNotIn('Distribution issues', output.getvalue())
        self.assertEqual(coordination.validate_task(self.repo, task['id']), ['distribution plan is missing'])
        with self.assertRaises(coordination.CoordinationError):
            coordination.complete_task(self.repo, task['id'])
