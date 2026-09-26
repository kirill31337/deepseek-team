"""Explicit graded quality feedback: coordination, CLI, hooks and lessons rendering.

Every test uses real temporary projects, HOME and private state: the real
coordination ledger JSON, the real SQLite lessons service, the real routing
service and (for CLI contracts) the real ``python -m codex_deepseek_team``
process. No helper under test is replaced by a stub.
"""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock

from codex_deepseek_team import (activation, coordination, coordination_cli,
                                 coordinator_hooks, lessons, project,
                                 routing_learning, routing_quality)
from codex_deepseek_team.routing import RoutingService
from test_coordination import CoordinationCase

SOURCE_ROOT = Path(__file__).resolve().parents[1]
PYTHONPATH = str(SOURCE_ROOT / 'src')

REWORK = {'cause': 'worker_error', 'severity': 'minor',
          'summary': 'the worker missed the recorded acceptance check',
          'prevention': 'restate the acceptance check in the brief'}
GRADE_MARKER = 'Grade each assignment yourself'


def assessment(grade='met', attribution='worker', evidence='declared checks verified'):
    return {'grade': grade, 'attribution': attribution, 'evidence': evidence}


class QualityFeedbackCase(CoordinationCase):
    def setUp(self):
        super().setUp()
        enabled = mock.patch.dict(os.environ, {'DEEPSEEK_TEAM_DISABLED': '0'})
        enabled.start()
        self.addCleanup(enabled.stop)
        for runtime in ('codex', 'claude'):
            project.attach(self.repo, coordinator=runtime)

    # -- helpers ---------------------------------------------------------
    def cli(self, *arguments, input=''):
        env = dict(os.environ, PYTHONPATH=PYTHONPATH)
        env.pop('DEEPSEEK_TEAM_DISABLED', None)
        return subprocess.run([sys.executable, '-m', 'codex_deepseek_team', *arguments],
                              cwd=str(self.repo), env=env, input=input, text=True,
                              capture_output=True, timeout=90)

    def planned(self, session, *, executor='worker'):
        task = coordination.open_task(self.repo, session_id=session, turn_id='turn-1',
                                      prompt='implement feature', policy=self.policy())
        return coordination.plan_task(self.repo, task['id'], {
            'classification': 'substantial',
            'deliverables': [{'id': 'impl', 'kind': 'implementation', 'scope': ['a.py'],
                              'executor': executor, 'acceptance': ['implemented'],
                              'dependencies': [], 'checks': []}]})

    def running(self, session, *, effort='high', runtime='codex', executor='worker'):
        plan = self.planned(session, executor=executor)
        assignment_id = plan['assignments'][0]['id']
        coordination.assignment_started(self.repo, plan['id'], assignment_id,
                                        'ws-' + session, runtime, [], effort=effort)
        return plan['id'], assignment_id

    def finished(self, session, *, summary='worker reported done'):
        task_id, assignment_id = self.running(session)
        coordination.assignment_finished(self.repo, task_id, assignment_id, 'succeeded',
                                         summary, [], [])
        return task_id, assignment_id

    def failed(self, session, error_kind):
        task_id, assignment_id = self.running('failed-' + session)
        coordination.assignment_finished(self.repo, task_id, assignment_id, 'failed',
                                         'no usable result', [], [], error_kind=error_kind)
        return task_id, assignment_id

    def row(self, task_id):
        return coordination.load_task(self.repo, task_id)['assignments'][0]

    def case_observations(self, task_id, *, service=None):
        service = service or RoutingService(self.repo)
        case_id = self.row(task_id)['routing_feedback'][-1]['observation']['case_id']
        return [row for row in service.observations() if row['case_id'] == case_id]

    def record_case(self, suffix, *, task=None, assignment=None):
        task = task or 'task-' + suffix
        assignment = assignment or 'assignment-' + suffix
        record = lessons.record_outcome(
            self.repo, task_id=task, assignment_id=assignment,
            disposition='needs-rework', evidence='reviewer found a gap',
            context={'features': {'kind': 'implementation', 'runtime': 'codex',
                                  'effort': 'high'}},
            rework=REWORK)
        return f'{task}/{assignment}', record['seq']

    def apply_rules(self, rules, *, expected_version, through_event, summary):
        return lessons.apply_review(self.repo, {
            'expected_version': expected_version, 'through_event': through_event,
            'summary': summary, 'rules': rules})


class QualityFeedbackApiTests(QualityFeedbackCase):
    def test_invalid_assessment_and_cost_leave_every_store_unchanged(self):
        task_id, assignment_id = self.finished('invalid-quality')
        service = RoutingService(self.repo)
        before = service.observations()
        bad_quality = [
            {'grade': 'excellent', 'attribution': 'worker', 'evidence': 'x'},
            {'grade': 'met', 'attribution': 'nobody', 'evidence': 'x'},
            {'grade': 'met', 'attribution': 'worker', 'evidence': ''},
            {'grade': 'met', 'attribution': 'worker', 'evidence': 'x' * 4001},
            {'grade': 'met', 'attribution': 'worker'},
            {'grade': 'met', 'attribution': 'worker', 'evidence': 'x', 'score': 1.0},
            ['met'],
        ]
        for value in bad_quality:
            with self.subTest(value=value):
                with self.assertRaises(coordination.CoordinationError) as caught:
                    coordination.use_result(self.repo, task_id, assignment_id,
                                            'incorporated', 'verified', quality=value)
                self.assertEqual(caught.exception.code, 64)
        for cost in ('1.0', -1, float('nan'), True):
            with self.subTest(cost=cost):
                with self.assertRaises(coordination.CoordinationError) as caught:
                    coordination.use_result(self.repo, task_id, assignment_id,
                                            'incorporated', 'verified', cost_usd=cost)
                self.assertEqual(caught.exception.code, 64)
        row = self.row(task_id)
        self.assertIsNone(row['disposition'])
        self.assertNotIn('lessons_feedback', row)
        self.assertNotIn('routing_feedback', row)
        self.assertEqual(service.observations(), before)
        self.assertEqual(lessons.journal(self.repo), [])

    def test_exact_retry_dedupes_and_changed_assessment_appends(self):
        task_id, assignment_id = self.finished('quality-roundtrip')
        met = assessment()
        coordination.use_result(self.repo, task_id, assignment_id, 'incorporated',
                                'verified output', quality=met)
        service = RoutingService(self.repo)
        rows = service.observations()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['outcome'], 'accepted')
        self.assertEqual(rows[0]['quality'], met)
        outbox = self.row(task_id)['routing_feedback'][-1]['observation']
        self.assertEqual(outbox['quality'], met)
        events = lessons.journal(self.repo)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[-1]['context']['quality'], met)

        coordination.use_result(self.repo, task_id, assignment_id, 'incorporated',
                                'verified output', quality=dict(met))
        self.assertEqual(len(service.observations()), 1)
        self.assertEqual(len(lessons.journal(self.repo)), 1)

        changed = assessment('major_gaps', 'worker', 'missed the declared acceptance check')
        coordination.use_result(self.repo, task_id, assignment_id, 'incorporated',
                                'verified output', quality=changed)
        case_rows = self.case_observations(task_id, service=service)
        self.assertEqual(len(case_rows), 2)
        self.assertEqual(len({row['id'] for row in case_rows}), 2)
        self.assertEqual(routing_quality.case_quality(case_rows)['kind'], 'explicit')
        self.assertEqual(routing_quality.case_score(case_rows, case_rows[0]), .3)
        events = lessons.journal(self.repo)
        self.assertEqual(len(events), 2)
        self.assertEqual(events[-1]['context']['quality'], changed)

    def test_legacy_fingerprint_omission_and_explicit_grade_survives_incorporation(self):
        task_id, assignment_id = self.finished('legacy-omission')
        coordination.use_result(self.repo, task_id, assignment_id, 'needs-rework', 'gap')
        service = RoutingService(self.repo)
        rows = service.observations()
        self.assertEqual(len(rows), 1)
        self.assertNotIn('quality', rows[0])
        self.assertNotIn('quality', self.row(task_id)['routing_feedback'][-1]['observation'])
        self.assertNotIn('quality', lessons.journal(self.repo)[-1]['context'])

        task_id, assignment_id = self.finished('explicit-survives')
        unusable = assessment('unusable', 'worker', 'the main requested result failed')
        coordination.use_result(self.repo, task_id, assignment_id, 'needs-rework',
                                'gap', quality=unusable)
        coordination.use_result(self.repo, task_id, assignment_id, 'incorporated',
                                'corrected output verified')
        case_rows = self.case_observations(task_id, service=service)
        self.assertEqual([row['outcome'] for row in case_rows], ['rework', 'accepted'])
        self.assertNotIn('quality', case_rows[-1])
        self.assertEqual(routing_quality.case_quality(case_rows)['kind'], 'explicit')
        self.assertEqual(routing_quality.case_score(case_rows, case_rows[0]), 0.0)
        self.assertTrue(routing_quality.case_substantive(case_rows))

    def test_structured_rework_on_incorporation_is_operational_rework(self):
        task_id, assignment_id = self.finished('rework-on-incorporation')
        coordination.use_result(self.repo, task_id, assignment_id, 'incorporated',
                                'verified after correction', rework=REWORK)
        service = RoutingService(self.repo)
        rows = self.case_observations(task_id, service=service)
        self.assertEqual([row['outcome'] for row in rows], ['rework'])
        self.assertEqual(routing_quality.case_quality(rows)['kind'], 'legacy')
        self.assertEqual(routing_quality.case_score(rows, rows[0]), 0.0)

        task_id, assignment_id = self.finished('reproduced-rework')
        coordination.use_result(self.repo, task_id, assignment_id, 'reproduced',
                                'fixed after coordinator correction', rework=REWORK)
        self.assertEqual([row['outcome'] for row in self.case_observations(task_id)],
                         ['rework'])

    def test_explicit_met_keeps_full_credit_for_a_corrected_result(self):
        task_id, assignment_id = self.finished('full-credit')
        met = assessment('met', 'worker', 'cosmetic preference edit only')
        coordination.use_result(self.repo, task_id, assignment_id, 'incorporated',
                                'verified', rework=REWORK, quality=met)
        service = RoutingService(self.repo)
        rows = self.case_observations(task_id, service=service)
        self.assertEqual([row['outcome'] for row in rows], ['rework'])
        self.assertEqual(routing_quality.case_quality(rows)['kind'], 'explicit')
        self.assertEqual(routing_quality.case_score(rows, rows[0]), 1.0)
        self.assertFalse(routing_quality.case_substantive(rows))
        self.assertEqual(service.status()['admission']['active_cooldowns'], [])

    def test_explicit_assessment_controls_the_hard_failure_cooldown(self):
        service = RoutingService(self.repo)
        clean, clean_assignment = self.finished('clean-rejection')
        coordination.use_result(self.repo, clean, clean_assignment, 'rejected',
                                'superseded requirement; the worker met the original brief',
                                quality=assessment('met', 'worker', 'original brief was met'))
        self.assertEqual(service.status()['admission']['active_cooldowns'], [])
        bad, bad_assignment = self.finished('substantive-rejection')
        coordination.use_result(self.repo, bad, bad_assignment, 'rejected',
                                'defect reproduced on the coordinator copy',
                                quality=assessment('major_gaps', 'worker',
                                                   'a supplied requirement failed'))
        self.assertTrue(service.status()['admission']['active_cooldowns'])

    def test_minor_gap_supplies_fractional_credit_without_a_hard_pause(self):
        task_id, assignment_id = self.finished('minor-gap')
        coordination.use_result(self.repo, task_id, assignment_id, 'rejected',
                                'a bounded actual requirement was missed',
                                quality=assessment('minor_gaps', 'worker',
                                                   'one declared criterion was missed'))
        service = RoutingService(self.repo)
        rows = self.case_observations(task_id, service=service)
        self.assertEqual(routing_quality.case_quality(rows)['kind'], 'explicit')
        self.assertEqual(routing_quality.case_score(rows, rows[0]), .8)
        self.assertFalse(routing_quality.case_substantive(rows))
        self.assertEqual(service.status()['admission']['active_cooldowns'], [])

    def test_unassessable_and_non_worker_assessments_stay_neutral(self):
        service = RoutingService(self.repo)
        for grade, attribution in (('unassessable', 'worker'),
                                   ('major_gaps', 'coordinator'),
                                   ('unassessable', 'unknown')):
            with self.subTest(grade=grade, attribution=attribution):
                task_id, assignment_id = self.finished(
                    'neutral-' + attribution + '-' + grade)
                coordination.use_result(self.repo, task_id, assignment_id, 'rejected',
                                        'outcome recorded with no worker quality',
                                        quality=assessment(grade, attribution))
                rows = self.case_observations(task_id, service=service)
                self.assertEqual(routing_quality.case_quality(rows)['kind'], 'neutral')
                self.assertIsNone(routing_quality.case_score(rows, rows[0]))
                self.assertFalse(routing_quality.case_substantive(rows))
        self.assertEqual(service.status()['admission']['active_cooldowns'], [])

    def test_routing_outbox_outage_recovery_keeps_one_assessment(self):
        task_id, assignment_id = self.finished('outage-routing')
        met = assessment()
        real_observe = RoutingService.observe
        attempts = []

        def flaky(service, observation):
            real_observe(service, observation)
            attempts.append(observation['id'])
            raise RuntimeError('interruption after the SQLite commit')

        with mock.patch.object(RoutingService, 'observe', autospec=True, side_effect=flaky):
            with self.assertRaises(RuntimeError):
                coordination.use_result(self.repo, task_id, assignment_id, 'incorporated',
                                        'verified output', quality=met)
        coordination.sync_routing_feedback(self.repo, task_id)
        coordination.sync_routing_feedback(self.repo, task_id)
        rows = RoutingService(self.repo).observations()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['quality'], met)
        self.assertEqual(len(attempts), 1)
        self.assertTrue(self.row(task_id)['routing_feedback'][-1]['recorded'])
        coordination.sync_lessons_feedback(self.repo, task_id)
        events = lessons.journal(self.repo)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[-1]['context']['quality'], met)

    def test_lessons_outbox_outage_recovery_keeps_one_assessment(self):
        task_id, assignment_id = self.finished('outage-lessons')
        met = assessment('met', 'shared', 'shared evidence names the worker gap')
        real_record = lessons.record_outcome
        attempts = []

        def flaky(root, **event):
            attempts.append(event['assignment_id'] + '/' + event['disposition'])
            if len(attempts) == 1:
                raise lessons.LessonError('simulated journal outage')
            return real_record(root, **event)

        with mock.patch.object(lessons, 'record_outcome', side_effect=flaky):
            with self.assertRaises(coordination.CoordinationError):
                coordination.use_result(self.repo, task_id, assignment_id, 'incorporated',
                                        'verified output', quality=met)
        self.assertFalse(self.row(task_id)['lessons_feedback'][-1]['recorded'])
        coordination.sync_lessons_feedback(self.repo, task_id)
        coordination.sync_lessons_feedback(self.repo, task_id)
        events = lessons.journal(self.repo)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[-1]['context']['quality'], met)
        self.assertEqual(len(attempts), 1)
        self.assertTrue(self.row(task_id)['lessons_feedback'][-1]['recorded'])
        rows = RoutingService(self.repo).observations()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['quality'], met)

    def test_neutral_outcomes_stay_neutral_under_an_attached_assessment(self):
        service = RoutingService(self.repo)
        task_id, assignment_id = self.finished('neutral-features')
        features = self.row(task_id)['routing_features']
        for error_kind, expected in (('provider', 'infrastructure'),
                                     ('environment', 'infrastructure'),
                                     ('cancelled', 'cancelled')):
            task_id, assignment_id = self.failed(error_kind, error_kind)
            before = len(service.observations())
            coordination.use_result(self.repo, task_id, assignment_id, 'rejected',
                                    'no usable result',
                                    quality=assessment('unusable', 'worker', 'no result'))
            rows = service.observations()[before:]
            self.assertTrue(rows)
            self.assertTrue(all(row['outcome'] in ('infrastructure', 'cancelled')
                                for row in rows), rows)
            self.assertTrue(any(row.get('quality') for row in rows))
            learned = routing_learning.assess_quality(features, service.observations(),
                                                      service.config())
            self.assertEqual(learned['posterior']['matched_local'], 0)
        self.assertEqual(service.status()['admission']['active_cooldowns'], [])


class QualityFeedbackCliTests(QualityFeedbackCase):
    def use(self, task_id, assignment_id, *extra, input=''):
        return self.cli('coordination', 'use', '--path', str(self.repo),
                        '--task', task_id, '--assignment', assignment_id,
                        '--disposition', 'incorporated', '--evidence', 'verified output',
                        *extra, input=input)

    def test_quality_json_file_stdin_and_null_semantics(self):
        task_id, assignment_id = self.finished('cli-file')
        path = self.root / 'quality.json'
        met = assessment('met', 'shared', 'shared attribution names the worker gap')
        path.write_text(json.dumps(met))
        result = self.use(task_id, assignment_id, '--quality-json', str(path))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(lessons.journal(self.repo)[-1]['context']['quality'], met)
        self.assertEqual(RoutingService(self.repo).observations()[-1]['quality'], met)

        task_id, assignment_id = self.finished('cli-stdin')
        result = self.use(task_id, assignment_id, '--quality-json', '-',
                          input=json.dumps(assessment('minor_gaps', 'worker')))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(lessons.journal(self.repo)[-1]['context']['quality']['grade'],
                         'minor_gaps')

        task_id, assignment_id = self.finished('cli-null')
        result = self.use(task_id, assignment_id, '--quality-json', '-', input='null')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('quality', lessons.journal(self.repo)[-1]['context'])
        self.assertEqual([row['outcome'] for row in self.case_observations(task_id)],
                         ['accepted'])

    def test_both_stdin_feedback_sources_are_rejected_before_reading(self):
        task_id, assignment_id = self.finished('cli-both-stdin')

        class ExplodingStdin:
            def read(self, *arguments):
                raise AssertionError('stdin must not be read when both flags use -')

        stderr = io.StringIO()
        with mock.patch.object(sys, 'stdin', ExplodingStdin()), \
                contextlib.redirect_stderr(stderr), contextlib.redirect_stdout(io.StringIO()):
            code = coordination_cli.main([
                'coordination', 'use', '--path', str(self.repo), '--task', task_id,
                '--assignment', assignment_id, '--disposition', 'incorporated',
                '--evidence', 'verified', '--rework-json', '-', '--quality-json', '-'])
        self.assertEqual(code, 64)
        self.assertIn('stdin', stderr.getvalue())
        self.assertIsNone(self.row(task_id)['disposition'])
        self.assertEqual(lessons.journal(self.repo), [])

        result = self.use(task_id, assignment_id, '--rework-json', '-', '--quality-json', '-')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(lessons.journal(self.repo), [])

    def test_duplicate_keys_and_malformed_feedback_change_nothing(self):
        task_id, assignment_id = self.finished('cli-malformed')
        service = RoutingService(self.repo)
        before = service.observations()
        duplicate_rework = ('{"cause":"worker_error","cause":"brief_gap",'
                            '"severity":"minor","summary":"s","prevention":"p"}')
        for extra, value in [
            (('--rework-json', '-'), duplicate_rework),
            (('--quality-json', '-'),
             '{"grade":"met","grade":"unusable","attribution":"worker","evidence":"x"}'),
            (('--quality-json', '-'),
             json.dumps({'grade': 'nonsense', 'attribution': 'worker', 'evidence': 'x'})),
            (('--quality-json', '-'), json.dumps(['met'])),
            (('--quality-json', '-'), '{not json'),
        ]:
            with self.subTest(value=value):
                result = self.use(task_id, assignment_id, *extra, input=value)
                self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIsNone(self.row(task_id)['disposition'])
        self.assertNotIn('lessons_feedback', self.row(task_id))
        self.assertEqual(service.observations(), before)
        self.assertEqual(lessons.journal(self.repo), [])

    def test_missing_quality_file_changes_nothing(self):
        task_id, assignment_id = self.finished('cli-missing-file')
        result = self.use(task_id, assignment_id, '--quality-json',
                          str(self.root / 'absent.json'))
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(lessons.journal(self.repo), [])


class LessonsRulesRenderingTests(QualityFeedbackCase):
    def test_rules_render_the_committed_snapshot_after_a_publish_failure(self):
        case, seq = self.record_case('00000000000000000000', assignment='00000000')
        self.apply_rules([{'id': 'rule-first', 'when': {'kind': 'implementation'},
                           'condition': 'first condition', 'action': 'first action',
                           'evidence': [case]}],
                         expected_version=0, through_event=seq, summary='first review')
        mirror = Path(lessons.status(self.repo)['rules_file'])
        self.assertIn('rule-first', mirror.read_text())

        second = [{'id': 'rule-second', 'when': {'kind': 'implementation'},
                   'condition': 'second condition', 'action': 'second action',
                   'evidence': [case]}]
        with mock.patch.object(lessons, '_write_markdown',
                               side_effect=OSError('simulated publish failure')):
            with self.assertRaises(lessons.LessonError):
                self.apply_rules(second, expected_version=1, through_event=seq,
                                 summary='second review')
        self.assertEqual(lessons.status(self.repo)['version'], 2)
        stale = mirror.read_text()
        self.assertIn('rule-first', stale)
        self.assertNotIn('rule-second', stale)

        result = self.cli('lessons', 'rules', '--path', str(self.repo))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('rule-second', result.stdout)
        self.assertNotIn('rule-first', result.stdout)
        structured = json.loads(self.cli('lessons', 'rules', '--path', str(self.repo),
                                         '--json').stdout)
        self.assertEqual(structured['version'], 2)
        self.assertEqual(structured['rule_ids'], ['rule-second'])
        self.assertIn('rule-second', json.dumps(structured))
        # Read commands never rewrite or regenerate the stale mirror.
        self.assertEqual(mirror.read_text(), stale)

    def test_rules_reads_create_no_state_and_render_committed_rules(self):
        self.assertFalse(self.state.exists())
        result = self.cli('lessons', 'rules', '--path', str(self.repo))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('No active delegation lessons', result.stdout)
        self.assertFalse(self.state.exists())

    def test_long_rule_and_case_identifiers_are_retained_exactly(self):
        task = 'task-' + 'a' * 123
        assignment = 'assignment-' + 'b' * 117
        case = f'{task}/{assignment}'
        rule_id = 'rule-' + 'c' * 123
        self.assertEqual(len(task), 128)
        self.assertEqual(len(assignment), 128)
        self.assertEqual(len(rule_id), 128)
        _, seq = self.record_case('long-ids', task=task, assignment=assignment)
        when = {'kind': 'implementation', 'runtime': 'codex', 'effort': 'high'}
        self.apply_rules([{'id': rule_id, 'when': when, 'condition': 'long condition',
                           'action': 'long action', 'evidence': [case]}],
                         expected_version=0, through_event=seq, summary='long ids')

        task_id, assignment_id = self.finished('long-ids-assignment')
        snapshot = self.row(task_id)['execution_snapshot']['lessons']
        self.assertIn(rule_id, snapshot['rule_ids'])
        block = next(item for item in snapshot['rules'] if item['id'] == rule_id)
        self.assertEqual(block['id'], rule_id)
        self.assertEqual(block['evidence'], [case])

        coordination.use_result(self.repo, task_id, assignment_id, 'incorporated', 'verified')
        block = lessons.journal(self.repo)[-1]['context']['lessons']['rules'][0]
        self.assertEqual(block['id'], rule_id)
        self.assertEqual(block['evidence'], [case])


class LifecycleGuidanceTests(QualityFeedbackCase):
    def prompt(self, runtime, session):
        return coordinator_hooks.handle({
            'session_id': session, 'turn_id': 'turn-1', 'cwd': str(self.repo),
            'hook_event_name': 'UserPromptSubmit', 'prompt': 'implement feature'},
            runtime=runtime)

    def session_start(self, runtime, session, source='startup'):
        return coordinator_hooks.handle({
            'session_id': session, 'cwd': str(self.repo),
            'hook_event_name': 'SessionStart', 'source': source}, runtime=runtime)

    def context(self, result):
        return result['hookSpecificOutput']['additionalContext']

    def test_prompt_and_session_contexts_carry_the_rubric_without_lessons(self):
        self.assertFalse((self.state / 'lessons').exists())
        for runtime in ('codex', 'claude'):
            with self.subTest(runtime=runtime, event='UserPromptSubmit'):
                text = self.context(self.prompt(runtime, runtime + '-prompt'))
                self.assertIn(GRADE_MARKER, text)
                self.assertIn('--quality-json', text)
                self.assertIn('--rework-json', text)
            with self.subTest(runtime=runtime, event='SessionStart'):
                text = self.context(self.session_start(runtime, runtime + '-session'))
                self.assertIn(GRADE_MARKER, text)
                self.assertIn('--quality-json', text)
        claude = self.context(self.session_start('claude', 'claude-compact', 'compact'))
        self.assertEqual(claude.count(GRADE_MARKER), 1)
        codex = self.context(self.session_start('codex', 'codex-compact', 'compact'))
        self.assertEqual(codex.count(GRADE_MARKER), 1)

    def test_off_and_claude_plan_mode_semantics_are_preserved(self):
        activation.set_enabled(self.repo, False)
        off = self.context(self.prompt('codex', 'off-prompt'))
        self.assertIn('disabled for this project', off)
        self.assertNotIn(GRADE_MARKER, off)
        off_session = self.context(self.session_start('claude', 'off-session'))
        self.assertIn('disabled for this project', off_session)
        self.assertNotIn(GRADE_MARKER, off_session)
        activation.set_enabled(self.repo, True)

        plan_session = 'claude-plan-mode'
        text = self.context(self.prompt('claude', plan_session))
        self.assertIn(GRADE_MARKER, text)
        self.assertEqual(coordinator_hooks.handle({
            'session_id': plan_session, 'cwd': str(self.repo), 'hook_event_name': 'Stop',
            'permission_mode': 'plan'}, runtime='claude'), {})


if __name__ == '__main__':
    unittest.main()
