"""End-to-end delegation-lessons integration with the real coordination ledger.

Every test uses a real temporary project, HOME and private state: a real Git
repository, the real coordination ledger JSON and the real lessons SQLite
service. Only external runtime/namespace probes are replaced in the managed-run
test, exactly like the existing managed-preparation fixtures.
"""
import contextlib
import hashlib
import io
import json
import os
from types import SimpleNamespace
import unittest
from unittest import mock

from codex_deepseek_team import (coordination, development, lessons, relay,
                                 settings, worker, workspace)
from codex_deepseek_team.lessons import LessonError
from test_coordination import CoordinationCase


def rule(identifier, when, *, condition='Check the recorded correction before execution.',
         action='Apply the recorded prevention before delegating.', evidence):
    return {'id': identifier, 'when': when, 'condition': condition, 'action': action,
            'evidence': evidence}


class LessonsIntegrationCase(CoordinationCase):
    def setUp(self):
        super().setUp()
        disabled = mock.patch.dict(os.environ, {'DEEPSEEK_TEAM_DISABLED': '0'})
        disabled.start()
        self.addCleanup(disabled.stop)

    def expected_keys(self):
        from codex_deepseek_team.routing_models import FEATURE_DEFAULTS
        return set(FEATURE_DEFAULTS)

    def plan_work(self, *, session='lessons', deliverables=None, checks=None):
        task = coordination.open_task(
            self.repo, session_id=session, turn_id='turn-' + session,
            prompt='implement feature', policy=self.policy())
        items = deliverables or [{
            'id': 'impl', 'kind': 'implementation', 'scope': ['a.py'],
            'executor': 'worker', 'acceptance': ['implemented'],
            'dependencies': [], 'checks': list(checks or [])}]
        planned = coordination.plan_task(self.repo, task['id'], {
            'classification': 'substantial', 'deliverables': items})
        return planned

    def start(self, task_id, assignment_id, *, workspace_id='ws-1', runtime='codex',
              effort='high', prepared=(), execution=None):
        return coordination.assignment_started(
            self.repo, task_id, assignment_id, workspace_id, runtime, list(prepared),
            effort=effort, execution=execution)

    def finish(self, task_id, assignment_id, *, status='succeeded', summary='worker reported done',
               changes=(), checks=()):
        return coordination.assignment_finished(
            self.repo, task_id, assignment_id, status, summary, list(changes), list(checks))

    def seed_rework_case(self, *, cause='worker_error', session='seed'):
        planned = self.plan_work(session=session)
        assignment_id = planned['assignments'][0]['id']
        self.start(planned['id'], assignment_id)
        self.finish(planned['id'], assignment_id)
        coordination.use_result(self.repo, planned['id'], assignment_id, 'needs-rework',
                                'reviewer found a gap', rework={
                                    'cause': cause, 'severity': 'major',
                                    'summary': 'the first brief missed the recorded check',
                                    'prevention': 'state the acceptance check in the brief'})
        return f"{planned['id']}/{assignment_id}"

    def apply_rules(self, rules, *, expected_version=0, through_event=None, summary='review'):
        if through_event is None:
            through_event = lessons.status(self.repo)['through_event']
        return lessons.apply_review(self.repo, {
            'expected_version': expected_version, 'through_event': through_event,
            'summary': summary, 'rules': rules})

    def row(self, task_id):
        return coordination.load_task(self.repo, task_id)['assignments'][0]


class AssignmentSnapshotTests(LessonsIntegrationCase):
    def test_snapshot_records_original_context_execution_and_applicable_rules(self):
        case = self.seed_rework_case()
        self.apply_rules([
            rule('rule-match', {'kind': 'implementation', 'runtime': 'codex', 'effort': 'high'},
                 condition='Restate the acceptance check from the recorded brief gap.',
                 evidence=[case]),
            rule('rule-other', {'kind': 'test'}, evidence=[case]),
        ])
        planned = self.plan_work(session='snapshot')
        assignment_id = planned['assignments'][0]['id']
        digest = hashlib.sha256(b'implement the checked behavior').hexdigest()
        self.start(planned['id'], assignment_id, prepared=['imported.py'], execution={
            'prompt_sha256': digest,
            'prompt_brief': 'implement the checked behavior',
            'base_head': planned['base_head'],
            'git_digest': 'd' * 64,
            'prepared_fingerprints': {'imported.py': 'a' * 64},
            'model': 'deepseek-flash',
        })
        snapshot = self.row(planned['id'])['execution_snapshot']
        self.assertEqual(snapshot['deliverable']['id'], 'impl')
        self.assertEqual(snapshot['deliverable']['kind'], 'implementation')
        self.assertEqual(snapshot['deliverable']['scope'], ['a.py'])
        self.assertEqual(snapshot['deliverable']['acceptance'], ['implemented'])
        self.assertEqual(snapshot['deliverable']['dependencies'], [])
        self.assertEqual(snapshot['deliverable']['checks'], [])
        self.assertEqual(snapshot['execution']['runtime'], 'codex')
        self.assertEqual(snapshot['execution']['effort'], 'high')
        self.assertEqual(snapshot['execution']['workspace_id'], 'ws-1')
        self.assertEqual(snapshot['execution']['base_head'], planned['base_head'])
        self.assertEqual(snapshot['execution']['git_digest'], 'd' * 64)
        self.assertEqual(snapshot['execution']['prompt_sha256'], digest)
        self.assertEqual(snapshot['execution']['prompt_brief'],
                         'implement the checked behavior')
        self.assertNotIn('prompt', snapshot['execution'])
        self.assertEqual(snapshot['execution']['prepared_changes'], ['imported.py'])
        self.assertEqual(snapshot['execution']['prepared_fingerprints'],
                         {'imported.py': 'a' * 64})
        self.assertEqual(snapshot['features']['kind'], 'implementation')
        self.assertEqual(snapshot['features']['runtime'], 'codex')
        self.assertEqual(snapshot['features']['effort'], 'high')
        self.assertEqual(snapshot['lessons']['version'], 1)
        self.assertEqual(snapshot['lessons']['rule_ids'], ['rule-match'])
        self.assertEqual([item['id'] for item in snapshot['lessons']['rules']], ['rule-match'])
        self.assertIn('Restate the acceptance check',
                      snapshot['lessons']['rules'][0]['condition'])
        self.assertNotIn('rule-other', json.dumps(snapshot))

    def test_execution_context_is_closed_and_never_stores_raw_prompt(self):
        planned = self.plan_work(session='closed-input')
        assignment_id = planned['assignments'][0]['id']
        for value in ({'prompt': 'raw text'}, {'prompt_sha256': 'not-a-digest'},
                      {'base_head': 7}, {'digest': 'x'}, 'raw text'):
            with self.subTest(value=value):
                with self.assertRaises(coordination.CoordinationError) as caught:
                    self.start(planned['id'], assignment_id, execution=value)
                self.assertEqual(caught.exception.code, 64)
        self.assertEqual(self.row(planned['id'])['status'], 'planned')
        self.assertIsNone(self.row(planned['id']).get('execution_snapshot'))

    def test_bounded_brief_is_retained_without_the_raw_prompt(self):
        planned = self.plan_work(session='brief')
        assignment_id = planned['assignments'][0]['id']
        self.start(planned['id'], assignment_id, execution={
            'prompt_sha256': 'b' * 64,
            'prompt_brief': 'x' * 2000,
        })
        stored = self.row(planned['id'])['execution_snapshot']
        self.assertEqual(stored['execution']['prompt_brief'], 'x' * 600)
        self.assertEqual(set(stored['execution']),
                         {'workspace_id', 'runtime', 'effort', 'model', 'source_head',
                          'base_head', 'git_digest', 'prepared_changes',
                          'prepared_fingerprints', 'prompt_sha256', 'prompt_brief',
                          'started_at'})

    def test_running_resume_keeps_the_original_snapshot(self):
        planned = self.plan_work(session='running-resume')
        assignment_id = planned['assignments'][0]['id']
        self.start(planned['id'], assignment_id)
        original = self.row(planned['id'])['execution_snapshot']
        case = self.seed_rework_case(session='resume-seed')
        self.apply_rules([rule('rule-late', {'kind': 'implementation'}, evidence=[case])])
        self.start(planned['id'], assignment_id)
        resumed = self.row(planned['id'])['execution_snapshot']
        self.assertEqual(resumed, original)
        self.assertEqual(resumed['lessons']['version'], 0)
        self.assertEqual(resumed['lessons']['rule_ids'], [])

    def test_failed_resume_preserves_the_earlier_attempt_snapshot_in_history(self):
        planned = self.plan_work(session='failed-resume')
        assignment_id = planned['assignments'][0]['id']
        self.start(planned['id'], assignment_id)
        original = self.row(planned['id'])['execution_snapshot']
        self.finish(planned['id'], assignment_id, status='failed', summary='runtime failed',
                    checks=[{'command': 'python3 -m unittest', 'exit_code': 1}])
        coordination.use_result(self.repo, planned['id'], assignment_id, 'rejected',
                                'recorded failure inspected')
        case = self.seed_rework_case(session='resume-rule-seed')
        self.apply_rules([rule('rule-resume', {'kind': 'implementation'}, evidence=[case])])
        self.start(planned['id'], assignment_id)
        row = self.row(planned['id'])
        self.assertEqual(row['attempt_history'][0]['execution_snapshot'], original)
        self.assertEqual(row['attempt_history'][0]['status'], 'failed')
        self.assertEqual(row['execution_snapshot']['lessons']['version'], 1)
        self.assertEqual(row['execution_snapshot']['lessons']['rule_ids'], ['rule-resume'])


class OutcomeRecordingTests(LessonsIntegrationCase):
    def test_clean_result_records_one_clean_case_with_the_original_context(self):
        planned = self.plan_work(session='clean')
        assignment_id = planned['assignments'][0]['id']
        self.start(planned['id'], assignment_id, prepared=['imported.py'])
        self.finish(planned['id'], assignment_id, changes=['a.py'],
                    checks=[{'command': 'python3 -m unittest', 'exit_code': 0}])
        coordination.use_result(self.repo, planned['id'], assignment_id, 'incorporated',
                                'verified worker output')
        state = lessons.status(self.repo)
        self.assertEqual(state['counts'], {'cases': 1, 'clean': 1, 'rework': 0,
                                           'rejected': 0, 'unknown_attribution': 0})
        events = lessons.journal(self.repo)
        self.assertEqual(len(events), 1)
        context = events[0]['context']
        self.assertEqual(events[0]['case_id'], f"{planned['id']}/{assignment_id}")
        self.assertIsNone(events[0]['rework'])
        self.assertEqual(context['disposition'], 'incorporated')
        self.assertEqual(context['deliverable']['scope'], ['a.py'])
        self.assertEqual(context['execution']['worker_changes'], ['a.py'])
        self.assertEqual(context['execution']['checks'],
                         [{'command': 'python3 -m unittest', 'exit_code': 0}])
        self.assertEqual(context['execution']['runtime'], 'codex')
        self.assertEqual(context['features']['kind'], 'implementation')
        self.assertEqual(context['lessons']['version'], 0)

    def test_needs_rework_then_incorporated_stays_one_rework_case(self):
        planned = self.plan_work(session='two-dispositions')
        assignment_id = planned['assignments'][0]['id']
        self.start(planned['id'], assignment_id)
        self.finish(planned['id'], assignment_id)
        coordination.use_result(self.repo, planned['id'], assignment_id, 'needs-rework',
                                'review found a gap', rework={
                                    'cause': 'brief_gap', 'severity': 'minor',
                                    'summary': 'the brief omitted the acceptance check',
                                    'prevention': 'state the check in the brief'})
        coordination.use_result(self.repo, planned['id'], assignment_id, 'incorporated',
                                'corrected output verified')
        state = lessons.status(self.repo)
        self.assertEqual(state['counts'], {'cases': 1, 'clean': 0, 'rework': 1,
                                           'rejected': 0, 'unknown_attribution': 0})
        events = lessons.journal(self.repo)
        self.assertEqual([event['disposition'] for event in events],
                         ['needs-rework', 'incorporated'])
        self.assertEqual({event['case_id'] for event in events},
                         {f"{planned['id']}/{assignment_id}"})

    def test_rework_attached_to_incorporation_is_never_a_clean_case(self):
        planned = self.plan_work(session='rework-incorporated')
        assignment_id = planned['assignments'][0]['id']
        self.start(planned['id'], assignment_id)
        self.finish(planned['id'], assignment_id)
        coordination.use_result(self.repo, planned['id'], assignment_id, 'incorporated',
                                'accepted after the coordinator corrected the brief',
                                rework={'cause': 'brief_gap', 'severity': 'minor',
                                        'summary': 'the coordinator corrected the brief',
                                        'prevention': 'check the brief first'})
        state = lessons.status(self.repo)
        self.assertEqual(state['counts']['rework'], 1)
        self.assertEqual(state['counts']['clean'], 0)

    def test_invalid_rework_is_rejected_before_any_ledger_mutation(self):
        planned = self.plan_work(session='invalid-rework')
        assignment_id = planned['assignments'][0]['id']
        self.start(planned['id'], assignment_id)
        self.finish(planned['id'], assignment_id)
        bad_values = [
            {'cause': 'nonsense', 'severity': 'major', 'summary': 's', 'prevention': 'p'},
            {'cause': 'worker_error', 'severity': 'major', 'summary': 's', 'prevention': 'p',
             'extra': 'x'},
            ['not', 'an', 'object'],
            {'cause': 'worker_error', 'severity': 'major', 'summary': '', 'prevention': 'p'},
        ]
        for value in bad_values:
            with self.subTest(value=value):
                with self.assertRaises(coordination.CoordinationError) as caught:
                    coordination.use_result(self.repo, planned['id'], assignment_id,
                                            'needs-rework', 'reviewer found a gap', rework=value)
                self.assertEqual(caught.exception.code, 64)
        row = self.row(planned['id'])
        self.assertIsNone(row['disposition'])
        self.assertNotIn('lessons_feedback', row)
        self.assertEqual(lessons.journal(self.repo), [])
        self.assertFalse((self.state / 'lessons').exists())

    def test_legacy_outcome_stays_unknown_and_never_uses_current_rules(self):
        case = self.seed_rework_case(session='legacy-seed')
        self.apply_rules([rule('rule-current', {'kind': 'implementation'}, evidence=[case])])
        planned = self.plan_work(session='legacy')
        assignment_id = planned['assignments'][0]['id']
        self.start(planned['id'], assignment_id)
        self.finish(planned['id'], assignment_id)
        # Simulate a ledger row written before the lessons feature existed.
        path = coordination._task_path(self.repo, planned['id'])
        stored = json.loads(path.read_text())
        row = stored['assignments'][0]
        row.pop('execution_snapshot', None)
        row.pop('lessons_feedback', None)
        coordination._atomic(path, stored)
        coordination.use_result(self.repo, planned['id'], assignment_id, 'incorporated',
                                'legacy output accepted')
        event = lessons.journal(self.repo)[-1]
        self.assertEqual(event['case_id'], f"{planned['id']}/{assignment_id}")
        self.assertIn('lessons', event['context'])
        self.assertIsNone(event['context']['lessons']['version'])
        self.assertEqual(event['context']['lessons']['rule_ids'], [])
        self.assertNotIn('rule-current', json.dumps(event['context']))
        state = lessons.status(self.repo)
        self.assertTrue(any(cohort['version'] == 'unknown' for cohort in state['cohorts']))

    def test_outbox_replays_after_service_failure_without_duplicate_cases(self):
        planned = self.plan_work(session='outbox')
        assignment_id = planned['assignments'][0]['id']
        self.start(planned['id'], assignment_id)
        self.finish(planned['id'], assignment_id)
        with mock.patch.object(lessons, 'record_outcome',
                               side_effect=LessonError('simulated journal failure')):
            with self.assertRaises(coordination.CoordinationError) as caught:
                coordination.use_result(self.repo, planned['id'], assignment_id,
                                        'incorporated', 'verified locally')
        self.assertEqual(caught.exception.code, 64)
        row = self.row(planned['id'])
        self.assertIsNotNone(row['disposition'])
        self.assertEqual(len(row['lessons_feedback']), 1)
        self.assertFalse(row['lessons_feedback'][0]['recorded'])
        self.assertEqual(lessons.journal(self.repo), [])
        coordination.sync_lessons_feedback(self.repo, planned['id'])
        events = lessons.journal(self.repo)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['case_id'], f"{planned['id']}/{assignment_id}")
        self.assertTrue(self.row(planned['id'])['lessons_feedback'][0]['recorded'])
        coordination.sync_lessons_feedback(self.repo, planned['id'])
        self.assertEqual(len(lessons.journal(self.repo)), 1)
        # The exact same disposition replay is idempotent at the service level.
        coordination.use_result(self.repo, planned['id'], assignment_id, 'incorporated',
                                'verified locally')
        self.assertEqual(len(lessons.journal(self.repo)), 1)
        self.assertEqual(lessons.status(self.repo)['counts']['cases'], 1)

    def test_three_distinct_rework_cases_of_one_cohort_make_review_due(self):
        planned = self.plan_work(session='cohort', deliverables=[
            {'id': f'impl-{index}', 'kind': 'implementation', 'scope': [f'file{index}.py'],
             'executor': 'worker', 'acceptance': ['implemented'], 'dependencies': [], 'checks': []}
            for index in range(3)])
        for row in planned['assignments']:
            self.start(planned['id'], row['id'])
            self.finish(planned['id'], row['id'])
            coordination.use_result(self.repo, planned['id'], row['id'], 'needs-rework',
                                    'review found a gap', rework={
                                        'cause': 'worker_error', 'severity': 'major',
                                        'summary': 'the worker missed the recorded check',
                                        'prevention': 'restate the check'})
        state = lessons.status(self.repo)
        self.assertTrue(state['review_due'])
        self.assertTrue(any('rework_cases=3/3' in reason and 'kind=implementation' in reason
                            and 'cause=worker_error' in reason
                            for reason in state['due_reasons']), state['due_reasons'])
        # Repeated events for one assignment can never satisfy the threshold.
        single = self.plan_work(session='single-cohort')
        assignment_id = single['assignments'][0]['id']
        self.start(single['id'], assignment_id)
        self.finish(single['id'], assignment_id)
        for index in range(3):
            coordination.use_result(self.repo, single['id'], assignment_id, 'needs-rework',
                                    f'gap {index}', rework={
                                        'cause': 'worker_error', 'severity': 'major',
                                        'summary': 'one repeated case', 'prevention': 'restate'})
        case = next(item for item in lessons.review_bundle(self.repo)['cases']
                    if item['case_id'] == f"{single['id']}/{assignment_id}")
        self.assertEqual(len(case['events']), 3)
        self.assertEqual(lessons.status(self.repo)['counts']['cases'], 4)

    def test_outcome_keeps_the_rules_selected_before_execution(self):
        case = self.seed_rework_case(session='rules-change-seed')
        self.apply_rules([rule('rule-first', {'kind': 'implementation'},
                               condition='Use the first recorded rule.', evidence=[case])])
        planned = self.plan_work(session='rules-change')
        assignment_id = planned['assignments'][0]['id']
        self.start(planned['id'], assignment_id)
        self.apply_rules([rule('rule-second', {'kind': 'implementation'},
                               condition='Use the second recorded rule.', evidence=[case])],
                         expected_version=1)
        self.assertEqual(lessons.status(self.repo)['version'], 2)
        self.finish(planned['id'], assignment_id)
        coordination.use_result(self.repo, planned['id'], assignment_id, 'incorporated',
                                'verified output')
        block = lessons.journal(self.repo)[-1]['context']['lessons']
        self.assertEqual(block['version'], 1)
        self.assertEqual(block['rule_ids'], ['rule-first'])
        self.assertEqual(block['rules'][0]['condition'], 'Use the first recorded rule.')
        self.assertNotIn('rule-second', json.dumps(block))
        self.assertTrue(block['captured'])
        self.assertTrue(block['advisory'])

    def test_lessons_context_never_leaks_into_routing_fields(self):
        planned = self.plan_work(session='routing-clean')
        assignment_id = planned['assignments'][0]['id']
        self.start(planned['id'], assignment_id)
        self.finish(planned['id'], assignment_id)
        coordination.use_result(self.repo, planned['id'], assignment_id, 'incorporated',
                                'verified worker output')
        row = self.row(planned['id'])
        self.assertEqual(set(row['routing_features']), self.expected_keys())
        self.assertNotIn('lessons', row['routing_features'])
        from codex_deepseek_team.routing import RoutingService
        for observation in RoutingService(self.repo).observations():
            self.assertLessEqual(set(observation['features']), self.expected_keys())
        event = lessons.journal(self.repo)[-1]
        self.assertNotIn('routing', event['context'])
        self.assertNotIn('routing_features', event['context'])


class ChangesPatchReferenceTests(LessonsIntegrationCase):
    def test_outcome_records_the_changes_patch_reference(self):
        planned = self.plan_work(session='patch-ref')
        assignment_id = planned['assignments'][0]['id']
        copy = workspace.create(self.repo, self.state)
        patch = copy.path.parent / 'changes.patch'
        patch.write_text('diff --git a/a.py b/a.py\n')
        os.chmod(patch, 0o600)
        self.start(planned['id'], assignment_id, workspace_id=copy.id)
        self.finish(planned['id'], assignment_id)
        coordination.use_result(self.repo, planned['id'], assignment_id, 'rejected',
                                'output rejected after inspecting the diff')
        reference = lessons.journal(self.repo)[-1]['context']['execution']['changes_patch']
        self.assertEqual(reference['file'], 'changes.patch')
        self.assertEqual(reference['workspace_id'], copy.id)
        self.assertEqual(reference['sha256'], hashlib.sha256(patch.read_bytes()).hexdigest())
        self.assertEqual(reference['bytes'], len(patch.read_bytes()))

    def test_missing_workspace_patch_stays_explicitly_unknown(self):
        planned = self.plan_work(session='patch-missing')
        assignment_id = planned['assignments'][0]['id']
        self.start(planned['id'], assignment_id, workspace_id='ws-not-a-real-copy')
        self.finish(planned['id'], assignment_id)
        coordination.use_result(self.repo, planned['id'], assignment_id, 'rejected',
                                'no retained copy')
        execution = lessons.journal(self.repo)[-1]['context']['execution']
        self.assertIsNone(execution['changes_patch'])


class ManagedRunSnapshotTests(LessonsIntegrationCase):
    class FakeRelay:
        failures = ()

        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def test_managed_run_snapshots_prompt_and_workspace_evidence_before_provider(self):
        planned = self.plan_work(session='managed')
        assignment_id = planned['assignments'][0]['id']
        (self.repo / 'imported.py').write_text('IMPORTED = 1\n')
        copy = workspace.create(self.repo, self.state)
        workspace.import_paths(copy, ['imported.py'])
        prepared_snapshot = workspace.content_snapshot(copy)
        observed = {}

        def fake_execute(args, env, task, timeout):
            observed['task'] = task
            observed['row'] = self.row(planned['id'])
            return (0, json.dumps({'type': 'item.completed',
                                   'item': {'type': 'agent_message', 'text': 'IMPLEMENTED'}})
                    + '\n' + json.dumps({'type': 'turn.completed'}) + '\n', '')

        args = SimpleNamespace(
            task='Review a.py only with fixture-key', os_sandbox='required', attempts=1,
            attempts_explicit=False,
            runtime='codex', codex='codex', claude='claude', state_dir=self.state, timeout=30,
            resume_after_failure=False, coord_task=planned['id'], coord_assignment=assignment_id,
            delegation_level=None, access=None, max_workers=None, effort='high',
            workspace=copy.id, no_wait=False)
        with contextlib.chdir(self.repo), contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()), \
                mock.patch.object(worker, 'resolve_runtime', return_value=('codex', '/usr/bin/true')), \
                mock.patch.object(worker, 'resolve_os_sandbox', return_value=(None, None)), \
                mock.patch.object(worker, 'load_api_key', return_value='fixture-key'), \
                mock.patch.object(worker, 'execute', side_effect=fake_execute), \
                mock.patch.object(development, 'check_runtime', return_value='test'), \
                mock.patch.object(development, 'layout', return_value=['test-layout']), \
                mock.patch.object(development, 'probe', return_value=None), \
                mock.patch.object(development, 'missing_requirements', return_value=[]), \
                mock.patch.object(development, 'bridge_command', return_value=['test-bridge']), \
                mock.patch.object(relay, 'ProviderRelay', self.FakeRelay):
            code = worker.run(args)
        self.assertEqual(code, 0)
        self.assertEqual(observed['task'], 'Review a.py only with fixture-key')
        self.assertEqual(observed['row']['status'], 'running')
        snapshot = observed['row']['execution_snapshot']
        self.assertEqual(snapshot['execution']['prompt_sha256'],
                         hashlib.sha256(b'Review a.py only with fixture-key').hexdigest())
        self.assertTrue(snapshot['execution']['prompt_brief'].startswith('Review a.py only'))
        self.assertIn('[REDACTED]', snapshot['execution']['prompt_brief'])
        self.assertNotIn('fixture-key', json.dumps(snapshot))
        self.assertEqual(snapshot['execution']['base_head'], copy.metadata['base_head'])
        self.assertEqual(snapshot['execution']['git_digest'], copy.metadata['git_digest'])
        self.assertEqual(snapshot['execution']['prepared_changes'], ['imported.py'])
        self.assertEqual(snapshot['execution']['prepared_fingerprints'],
                         {'imported.py': prepared_snapshot['imported.py']})
        self.assertEqual(snapshot['execution']['model'], 'deepseek-flash')
        self.assertEqual(self.row(planned['id'])['status'], 'succeeded')
        coordination.use_result(self.repo, planned['id'], assignment_id, 'incorporated',
                                'managed output verified')
        execution = lessons.journal(self.repo)[-1]['context']['execution']
        self.assertEqual(execution['changes_patch']['file'], 'changes.patch')
        self.assertEqual(execution['prompt_sha256'],
                         hashlib.sha256(b'Review a.py only with fixture-key').hexdigest())


if __name__ == '__main__':
    unittest.main()
