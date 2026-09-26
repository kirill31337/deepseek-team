"""Behavioral contracts for explicit graded worker quality.

These tests exercise the real validation, aggregation, statistics, cooldown and
chronological-evaluation paths rather than only the pure rubric helper.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from codex_deepseek_team import (routing_admission, routing_estimator, routing_learning,
                                 routing_quality, routing_stats, settings)
from codex_deepseek_team.routing import RoutingService
from codex_deepseek_team.routing_models import (RoutingError, canonical, validate_config,
                                                validate_features, validate_observation,
                                                validate_quality)

NOW = 2_000_000_000.0


def card(**changes):
    value = dict(kind='implementation', domain='python', operation='fix',
                 localization='known', coupling='local', verification='tests',
                 clarity='clear', risk='low', scope_size='small', runtime='codex',
                 model='deepseek-flash', effort='high', context_version='task-v1')
    value.update(changes)
    return validate_features(value)


def assessment(grade='met', attribution='worker',
               evidence='Declared checks and requirements verified.'):
    return {'grade': grade, 'attribution': attribution, 'evidence': evidence}


def observation(case, outcome='rework', *, at=NOW - 3600., ident=None, quality=None,
                cost_usd=None, origin='local', action='worker', **changes):
    row = dict(id=ident or f'{case}:{outcome}:{at}', case_id=case, origin=origin, action=action,
               features=card(**changes), outcome=outcome, observed_at=at)
    if quality is not None:
        row['quality'] = quality
    if cost_usd is not None:
        row['cost_usd'] = cost_usd
    if origin == 'external':
        row.update(source_id='public-suite', source_family='public-suite')
    return validate_observation(row, now=NOW)


def graded(case, grade='met', attribution='worker', *, at=NOW - 3600., outcome='rework',
           ident=None, **changes):
    return observation(case, outcome, at=at, ident=ident,
                       quality=assessment(grade, attribution), **changes)


def assess(rows, *, config=None, now=NOW, features=None):
    return routing_learning.assess_quality(features or card(), rows,
                                           config or validate_config({}), now=now)


class QualityContractTests(unittest.TestCase):
    def test_absent_quality_normalizes_to_none_and_never_adds_a_field(self):
        self.assertIsNone(validate_quality(None))
        plain = dict(id='legacy', case_id='legacy-case', origin='local', features=card(),
                     action='worker', outcome='accepted', observed_at=NOW - 10)
        normalized = validate_observation(plain, now=NOW)
        self.assertNotIn('quality', normalized)
        explicit_none = validate_observation(dict(plain, quality=None), now=NOW)
        self.assertNotIn('quality', explicit_none)
        self.assertEqual(canonical(normalized), canonical(explicit_none))

    def test_valid_assessment_round_trips_with_exact_keys(self):
        for grade in ('met', 'minor_gaps', 'major_gaps', 'unusable', 'unassessable'):
            with self.subTest(grade=grade):
                value = validate_quality(assessment(grade, 'shared'))
                self.assertEqual(set(value), {'grade', 'attribution', 'evidence'})
                self.assertEqual(value['grade'], grade)
                self.assertEqual(value['attribution'], 'shared')

    def test_caller_supplied_scores_and_unknown_keys_are_rejected(self):
        bad = [
            dict(assessment(), score=0.8),
            {'grade': 'met', 'attribution': 'worker'},
            {'grade': 'met', 'evidence': 'ok'},
            {'grade': 0.8, 'attribution': 'worker', 'evidence': 'ok'},
            {'grade': 'met', 'attribution': 'manager', 'evidence': 'ok'},
            {'grade': 'great', 'attribution': 'worker', 'evidence': 'ok'},
            {'grade': 'met', 'attribution': 'worker', 'evidence': ''},
            {'grade': 'met', 'attribution': 'worker', 'evidence': '   '},
            {'grade': 'met', 'attribution': 'worker', 'evidence': 'x' * 4001},
            {'grade': 'met', 'attribution': 'worker', 'evidence': 12},
            'met',
        ]
        for value in bad:
            with self.subTest(value=value), self.assertRaises(RoutingError):
                validate_quality(value)
        self.assertEqual(len(validate_quality(assessment(evidence='x' * 4000))['evidence']), 4000)

    def test_observation_accepts_a_valid_assessment_and_rejects_bad_ones(self):
        row = dict(id='q1', case_id='qc', origin='local', features=card(), action='worker',
                   outcome='rework', observed_at=NOW - 10, quality=assessment('minor_gaps', 'worker'))
        stored = validate_observation(row, now=NOW)
        self.assertEqual(stored['quality']['grade'], 'minor_gaps')
        for bad in ('met', {'grade': 'met', 'attribution': 'worker', 'evidence': 'ok', 'score': 1.0},
                    {'grade': 'met', 'attribution': 'worker', 'evidence': ' '}):
            with self.subTest(bad=bad), self.assertRaises(RoutingError):
                validate_observation(dict(row, quality=bad), now=NOW)

    def test_declared_rubric_weights_are_the_contract(self):
        self.assertEqual(routing_quality.GRADE_WEIGHTS,
                         {'met': 1.0, 'minor_gaps': .8, 'major_gaps': .3, 'unusable': 0.0,
                          'unassessable': None})
        self.assertEqual(routing_quality.rubric_weight('minor_gaps'), .8)
        self.assertIsNone(routing_quality.rubric_weight('unassessable'))
        self.assertEqual(routing_quality.assessment_score(assessment('major_gaps', 'shared')), .3)
        self.assertIsNone(routing_quality.assessment_score(assessment('major_gaps', 'coordinator')))
        self.assertTrue(routing_quality.substantive_failure(assessment('unusable', 'worker')))
        self.assertFalse(routing_quality.substantive_failure(assessment('minor_gaps', 'worker')))
        self.assertFalse(routing_quality.substantive_failure(assessment('major_gaps', 'environment')))


class CaseResolutionTests(unittest.TestCase):
    def resolution(self, rows):
        return routing_quality.case_quality(rows)

    def test_no_explicit_assessment_stays_legacy(self):
        rows = [observation('case-1', 'rework', ident='a'),
                observation('case-1', 'accepted', at=NOW - 10, ident='b')]
        self.assertEqual(self.resolution(rows)['kind'], 'legacy')
        self.assertIsNone(self.resolution(rows)['score'])
        self.assertEqual(routing_quality.case_score(rows, rows[0]), 0.0)
        self.assertEqual(routing_quality.case_score(rows, rows[1]), 1.0)

    def test_explicit_met_plus_rework_earns_full_credit(self):
        rows = [observation('case-1', 'rework', ident='a'),
                graded('case-1', 'met', 'worker', at=NOW - 10, outcome='accepted', ident='b')]
        resolved = self.resolution(rows)
        self.assertEqual((resolved['kind'], resolved['score']), ('explicit', 1.0))
        self.assertEqual(routing_quality.case_score(rows, rows[0]), 1.0)

    def test_worst_scored_worker_assessment_wins_with_earliest_tie(self):
        rows = [graded('case-1', 'met', 'worker', at=NOW - 100, ident='a'),
                graded('case-1', 'major_gaps', 'worker', at=NOW - 90, ident='b'),
                graded('case-1', 'unusable', 'worker', at=NOW - 80, ident='c')]
        self.assertEqual(self.resolution(rows)['score'], 0.0)
        tied = [graded('case-1', 'major_gaps', 'worker', at=NOW - 90, ident='later'),
                graded('case-1', 'major_gaps', 'shared', at=NOW - 120, ident='earlier')]
        self.assertEqual(self.resolution(tied)['row_id'], 'earlier')

    def test_only_unscored_explicit_assessments_make_the_case_neutral(self):
        rows = [graded('case-1', 'met', 'coordinator', ident='a'),
                graded('case-1', 'unusable', 'environment', at=NOW - 10, outcome='accepted', ident='b')]
        resolved = self.resolution(rows)
        self.assertEqual(resolved['kind'], 'neutral')
        self.assertIsNone(routing_quality.case_score(rows, rows[0]))

    def test_assessments_on_neutral_outcomes_stay_neutral(self):
        rows = [observation('case-1', 'infrastructure', ident='a',
                            quality=assessment('unusable', 'worker'))]
        self.assertEqual(self.resolution(rows)['kind'], 'legacy')
        self.assertFalse(routing_quality.case_substantive(rows))


class LearnedQualityGradedTests(unittest.TestCase):
    def test_explicit_met_rework_supplies_full_credit_instead_of_a_failure(self):
        rows = [graded(f'case-{n}', 'met', 'worker') for n in range(12)]
        result = assess(rows)
        self.assertTrue(result['sufficient'])
        self.assertFalse(result['veto'])
        self.assertAlmostEqual(result['posterior']['local_effective'], 12., places=1)
        self.assertLess(result['posterior']['local_failure_effective'], .01)
        self.assertGreater(result['posterior']['mean'], .9)

    def test_minor_gaps_supply_fractional_credit_and_never_hard_veto(self):
        result = assess([graded(f'case-{n}', 'minor_gaps', 'worker') for n in range(12)])
        self.assertTrue(result['sufficient'])
        self.assertFalse(result['veto'])
        self.assertAlmostEqual(result['posterior']['local_failure_effective'], 2.4, places=1)
        self.assertGreater(result['posterior']['mean'], .7)

    def test_major_gaps_and_unusable_count_as_substantive_failures(self):
        major = assess([graded(f'case-{n}', 'major_gaps', 'worker') for n in range(12)])
        self.assertTrue(major['veto'])
        self.assertAlmostEqual(major['posterior']['local_failure_effective'], 8.4, places=1)
        unusable = assess([graded(f'case-{n}', 'unusable', 'worker') for n in range(12)])
        self.assertTrue(unusable['veto'])
        self.assertAlmostEqual(unusable['posterior']['local_failure_effective'], 12., places=1)
        self.assertLess(unusable['posterior']['mean'], .2)

    def test_neutral_attribution_is_excluded_rather_than_a_success_or_failure(self):
        for attribution in ('coordinator', 'environment', 'unknown'):
            with self.subTest(attribution=attribution):
                result = assess([graded(f'case-{n}', 'unusable', attribution) for n in range(12)])
                self.assertEqual(result['posterior']['local_effective'], 0.)
                self.assertEqual(result['posterior']['local_failure_effective'], 0.)
                self.assertEqual(result['posterior']['mean'], .5)
                self.assertFalse(result['sufficient'])
                self.assertFalse(result['veto'])

    def test_neutral_infrastructure_with_a_worker_assessment_stays_neutral(self):
        rows = [observation(f'infra-{n}', 'infrastructure',
                            quality=assessment('unusable', 'worker')) for n in range(12)]
        result = assess(rows)
        self.assertEqual(result['posterior']['local_effective'], 0.)
        self.assertFalse(result['sufficient'])
        self.assertFalse(result['veto'])

    def test_earlier_explicit_defect_survives_a_later_acceptance(self):
        rows = [graded('case-1', 'major_gaps', 'worker', at=NOW - 100, outcome='rejected', ident='a'),
                observation('case-1', 'accepted', at=NOW - 10, ident='b')]
        result = assess(rows)
        self.assertEqual(result['posterior']['matched_local'], 1)
        self.assertGreater(result['posterior']['beta'], 1.5)  # no acceptance laundering
        self.assertGreater(result['posterior']['local_failure_effective'], .5)

    def test_legacy_history_keeps_the_binary_first_failure_behavior(self):
        legacy = assess([observation(f'case-{n}') for n in range(12)])
        self.assertTrue(legacy['sufficient'])
        self.assertTrue(legacy['veto'])
        self.assertAlmostEqual(legacy['posterior']['local_failure_effective'], 12., places=2)
        accepted = assess([observation(f'good-{n}', 'accepted') for n in range(12)])
        self.assertFalse(accepted['veto'])

    def test_future_assessments_are_not_counted(self):
        result = assess([graded(f'future-{n}', 'unusable', 'worker', at=NOW + 30) for n in range(12)])
        self.assertFalse(result['sufficient'])
        self.assertFalse(result['veto'])


class StatsGradedTests(unittest.TestCase):
    def summarize(self, rows):
        return routing_stats.summarize(rows, validate_config({}), now=NOW)

    def test_table_counts_explicit_legacy_and_neutral_cases_and_keeps_raw_counts(self):
        rows = [observation('clean', 'accepted', at=NOW - 100),
                observation('legacy-rework', 'rework', at=NOW - 90),
                graded('explicit-rework', 'met', 'worker', at=NOW - 80),
                graded('neutral-rework', 'met', 'coordinator', at=NOW - 70)]
        item = self.summarize(rows)['categories'][0]
        self.assertEqual((item['cases'], item['explicit'], item['legacy'], item['neutral'],
                          item['clean']), (4, 1, 2, 1, 1))
        self.assertEqual((item['accepted'], item['rework'], item['rejected']), (1, 3, 0))
        # Rubric credit for the explicit met case lifts the score above the raw
        # disposition rate; the neutral case adds no support.
        self.assertAlmostEqual(item['mean'], .6, places=3)
        self.assertGreater(item['mean'], .5)

    def test_score_is_rubric_based_not_the_clean_first_pass_rate(self):
        rows = [graded(f'rework-met-{n}', 'met', 'worker') for n in range(10)]
        item = self.summarize(rows)['categories'][0]
        self.assertEqual((item['clean'], item['rework']), (0, 10))
        self.assertGreater(item['mean'], .9)
        self.assertGreater(item['score'], .7)
        legacy = self.summarize([observation(f'rework-{n}') for n in range(10)])['categories'][0]
        self.assertLess(legacy['score'], .05)

    def test_neutral_only_category_has_no_score_and_no_rubric_mass(self):
        rows = [graded('neutral', 'major_gaps', 'environment')]
        item = self.summarize(rows)['categories'][0]
        self.assertIsNone(item['score'])
        self.assertEqual((item['neutral'], item['legacy'], item['explicit']), (1, 0, 0))
        self.assertEqual(item['effective_support'], 0.)

    def test_rendered_table_explains_rubric_versus_clean_first_pass(self):
        rows = [graded('explicit', 'minor_gaps', 'worker'),
                observation('legacy', 'accepted'),
                graded('neutral', 'met', 'coordinator')]
        rendered = routing_stats.render_table(self.summarize(rows))
        for column in ('CATEGORY', 'SCORE', 'ESTIMATE', 'INTERVAL', 'CASES', 'EFFECTIVE',
                       'ACCEPTED', 'REWORK', 'REJECTED', 'UNLABELLED', 'EXPLICIT', 'LEGACY',
                       'NEUTRAL', 'CLEAN'):
            self.assertIn(column, rendered)
        self.assertIn('rubric', rendered)
        self.assertIn('minor_gaps', rendered)
        self.assertIn('clean-first-pass', rendered)
        self.assertIn('assignment', rendered)
        json.dumps(self.summarize(rows), allow_nan=False)


class CooldownGradedTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        self.conn.isolation_level = None
        self.addCleanup(self.conn.close)
        routing_admission.initialize(self.conn)

    def record(self, rows, index=-1):
        return routing_admission.record_outcome(self.conn, rows[index], rows, {})

    def test_minor_gaps_and_met_reworks_never_start_the_hard_cooldown(self):
        rows = [graded(f'case-{n}', grade, 'worker', at=100. + n)
                for n in range(3) for grade in ('met',)]
        self.assertFalse(self.record(rows))
        self.assertFalse(routing_admission.cooling(self.conn, card(), now=200.))
        minor = [graded(f'minor-{n}', 'minor_gaps', 'worker', at=100. + n) for n in range(3)]
        self.assertFalse(self.record(minor))
        self.assertFalse(routing_admission.cooling(self.conn, card(), now=200.))

    def test_a_rejected_case_with_an_explicit_met_assessment_does_not_pause(self):
        rows = [graded('case-1', 'met', 'worker', outcome='rejected', at=100.)]
        self.assertFalse(self.record(rows))
        self.assertFalse(routing_admission.cooling(self.conn, card(), now=110.))

    def test_substantive_explicit_failures_still_pause(self):
        rejected = [graded('case-1', 'major_gaps', 'worker', outcome='rejected', at=100.)]
        self.assertTrue(self.record(rejected))
        self.assertTrue(routing_admission.cooling(self.conn, card(), now=110.))

    def fresh_connection(self):
        conn = sqlite3.connect(':memory:')
        conn.isolation_level = None
        self.addCleanup(conn.close)
        routing_admission.initialize(conn)
        return conn

    def test_three_distinct_substantive_reworks_pause_but_neutral_ones_do_not(self):
        rows = [graded('good-1', 'major_gaps', 'worker', at=100.),
                graded('good-2', 'major_gaps', 'shared', at=101.),
                graded('neutral-1', 'major_gaps', 'environment', at=102.),
                graded('good-3', 'unusable', 'worker', at=103.)]
        partial = self.fresh_connection()
        self.assertFalse(routing_admission.record_outcome(partial, rows[2], rows[:3], {}))
        self.assertFalse(routing_admission.cooling(partial, card(), now=110.))
        self.assertTrue(self.record(rows))
        self.assertTrue(routing_admission.cooling(self.conn, card(), now=110.))

    def test_legacy_rejection_behavior_is_unchanged(self):
        rows = [observation('legacy', 'rejected', at=100., ident='legacy')]
        self.assertTrue(self.record(rows))
        self.assertTrue(routing_admission.cooling(self.conn, card(), now=110.))

    def test_a_later_incorporation_does_not_erase_a_known_explicit_defect(self):
        rows = [graded('case-1', 'major_gaps', 'worker', outcome='rejected', at=100., ident='a'),
                observation('case-1', 'accepted', at=200., ident='b')]
        self.assertTrue(self.record(rows, index=0))
        self.assertFalse(self.record(rows))
        self.assertTrue(routing_admission.cooling(self.conn, card(), now=210.))


class ContextualEstimateGradedTests(unittest.TestCase):
    def forecast(self, rows, **config):
        return routing_estimator.forecast(card(), rows, validate_config(config), now=NOW)

    def test_contextual_estimate_uses_graded_credit(self):
        rows = [graded(f'case-{n}', 'met', 'worker') for n in range(12)]
        result = self.forecast(rows)
        self.assertAlmostEqual(result['posterior']['local_failure_effective'], 0., places=2)
        self.assertGreater(result['posterior']['mean'], .9)
        legacy = self.forecast([observation(f'case-{n}') for n in range(12)])
        self.assertLess(legacy['posterior']['mean'], .2)

    def test_neutral_cases_are_excluded_from_the_contextual_estimate(self):
        rows = [graded(f'case-{n}', 'unusable', 'coordinator') for n in range(12)]
        result = self.forecast(rows)
        self.assertEqual(result['posterior']['local_effective'], 0.)
        self.assertEqual(result['posterior']['mean'], .5)

    def test_cost_economics_stay_independent_of_quality(self):
        rows = [observation(f'case-{n}', 'accepted', cost_usd=2.) for n in range(3)]
        rows += [observation(f'coord-{n}', 'accepted', action='coordinator', cost_usd=9.)
                 for n in range(3)]
        plain = self.forecast(rows)['economics']
        attached = self.forecast([dict(row, quality=assessment('major_gaps', 'worker'))
                                  for row in rows])['economics']
        self.assertEqual(plain, attached)

    def test_sparse_quality_observations_stay_unknown(self):
        self.assertEqual(routing_estimator.forecast(card(), [], validate_config({}), now=NOW)
                         ['posterior']['mean'], .5)


class ChronologicalEvaluationGradedTests(unittest.TestCase):
    def evaluate(self, rows, **config):
        return routing_estimator.evaluate(rows, validate_config(config), now=NOW)

    def test_legacy_evaluation_metrics_are_unchanged(self):
        result = self.evaluate([observation('one', 'accepted')])
        self.assertAlmostEqual(result['brier_score'], .25)
        self.assertEqual(result['evaluated_cases'], 1)

    def test_nonexplicit_evaluation_metrics_are_unchanged(self):
        result = self.evaluate([observation('one', 'accepted')])
        self.assertEqual(result['predictions'][0]['quality_score'], 1.0)
        self.assertEqual(result['predictions'][0]['quality_kind'], 'legacy')

    def test_neutral_cases_are_excluded_from_evaluation_metrics(self):
        rows = [graded('neutral', 'met', 'coordinator', outcome='rework')]
        result = self.evaluate(rows)
        self.assertEqual(result['evaluated_cases'], 1)
        self.assertIsNone(result['brier_score'])
        self.assertIsNone(result['predictions'][0]['quality_score'])
        self.assertEqual(result['predictions'][0]['quality_kind'], 'neutral')
        self.assertEqual(result['quality_cases']['neutral'], 1)

    def test_a_later_assessment_never_changes_an_earlier_prediction(self):
        rows = [observation(f'good-{n}', 'accepted', at=NOW - 200, ident=f'good-{n}')
                for n in range(12)]
        rows += [observation(f'defect-{n}', 'accepted', at=NOW - 100, ident=f'defect-a-{n}')
                 for n in range(6)]
        rows += [graded(f'defect-{n}', 'major_gaps', 'worker', outcome='accepted',
                        at=NOW - 5, ident=f'defect-b-{n}') for n in range(6)]
        rows.append(observation('before', 'accepted', at=NOW - 10, ident='before'))
        rows.append(observation('after', 'accepted', at=NOW - 1, ident='after'))
        result = self.evaluate(rows)
        predicted = {p['case_id']: p for p in result['predictions']}
        self.assertGreater(predicted['before']['probability'], .9)
        self.assertLess(predicted['after']['probability'], .8)
        # Targets are corrected retrospectively at the cutoff, but the early
        # forecast itself never saw the later assessment.
        self.assertEqual(predicted['defect-0']['quality_score'], .3)
        self.assertGreater(predicted['defect-0']['probability'], .9)
        self.assertEqual(result['quality_cases']['explicit'], 6)
        self.assertEqual(result['quality_cases']['legacy'], 14)

    def test_fractional_rubric_credit_scores_brier_and_log_loss(self):
        result = self.evaluate([graded('one', 'major_gaps', 'worker', outcome='accepted')])
        prediction = result['predictions'][0]
        self.assertEqual(prediction['probability'], .5)
        self.assertEqual(prediction['quality_score'], .3)
        self.assertAlmostEqual(result['brier_score'], .04, places=12)
        self.assertAlmostEqual(result['log_loss'], .6931471805599453, places=12)

    def test_quality_cases_report_clean_first_pass_counts(self):
        rows = [observation(f'clean-{n}', 'accepted', at=NOW - 100 - n, ident=f'clean-{n}')
                for n in range(3)]
        rows += [graded(f'fix-{n}', 'met', 'worker', at=NOW - 50 - n, ident=f'fix-{n}')
                 for n in range(2)]
        result = self.evaluate(rows)
        self.assertEqual(result['quality_cases']['clean_first_pass'], 3)
        self.assertEqual(result['quality_cases']['explicit'], 2)
        json.dumps(result, allow_nan=False)


class ServiceGradedQualityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'project'
        self.root.mkdir()
        subprocess.run(['git', 'init', '-q', str(self.root)], check=True)
        environment = patch.dict(os.environ,
                                 DEEPSEEK_TEAM_STATE_DIR=str(Path(self.tmp.name) / 'state'),
                                 XDG_CONFIG_HOME=str(Path(self.tmp.name) / 'config'))
        environment.start()
        self.addCleanup(environment.stop)
        self.service = RoutingService(self.root)
        self.features = card()

    def stored_observation(self, **changes):
        row = dict(id='o1', case_id='c1', origin='local', features=self.features,
                   action='worker', outcome='rework', observed_at=time.time() - 100)
        row.update(changes)
        return self.service.observe(row)

    def test_observation_quality_round_trips_and_absent_quality_stays_absent(self):
        plain = self.stored_observation()
        self.assertNotIn('quality', self.service.observations()[0])
        with_quality = self.stored_observation(
            id='o2', case_id='c2', quality=assessment('minor_gaps', 'worker'))
        self.assertEqual(with_quality['quality']['grade'], 'minor_gaps')
        self.assertEqual(self.service.observations()[-1]['quality'],
                         {'grade': 'minor_gaps', 'attribution': 'worker',
                          'evidence': 'Declared checks and requirements verified.'})
        self.assertNotIn('quality', plain)

    def test_invalid_quality_leaves_no_state_and_conflicts_are_rejected(self):
        with self.assertRaises(RoutingError):
            self.stored_observation(quality={'grade': 'met', 'attribution': 'worker',
                                             'evidence': 'ok', 'score': 1.0})
        self.assertEqual(self.service.observations(), [])
        self.stored_observation(quality=assessment('met', 'worker'))
        with self.assertRaises(RoutingError):
            self.stored_observation(quality=assessment('unusable', 'worker'))
        self.assertEqual(len(self.service.observations()), 1)

    def test_appending_an_assessment_grades_an_existing_case_without_rewriting_it(self):
        settings.set_values(self.root / settings.PROJECT_FILE, access='full-access')
        now = time.time()
        for index in range(12):
            self.stored_observation(id=f'r-{index}', case_id=f'rc-{index}',
                                    observed_at=now - 7200)
        blocked = self.service.predict(self.features)
        self.assertEqual(blocked['action'], 'coordinator')
        for index in range(12):
            self.stored_observation(id=f'a-{index}', case_id=f'rc-{index}', outcome='accepted',
                                    observed_at=now - 7100, quality=assessment('met', 'worker'))
        graded = self.service.predict(self.features)
        self.assertEqual(graded['action'], 'worker')
        self.assertFalse(graded['quality']['veto'])
        self.assertLess(graded['quality']['posterior']['local_failure_effective'], .01)
        saved = {row['id']: row for row in self.service.observations()}
        self.assertEqual(saved['r-0']['outcome'], 'rework')
        self.assertNotIn('quality', saved['r-0'])

    def test_decision_aggregates_graded_quality_through_the_real_service(self):
        settings.set_values(self.root / settings.PROJECT_FILE, access='full-access')
        now = time.time()
        for index in range(12):
            self.stored_observation(id=f'q-{index}', case_id=f'qc-{index}',
                                    quality=assessment('major_gaps', 'environment'),
                                    observed_at=now - 7200)
        neutral = self.service.predict(self.features)
        self.assertEqual(neutral['action'], 'worker')
        self.assertFalse(neutral['quality']['sufficient'])
        for index in range(12):
            self.stored_observation(id=f'f-{index}', case_id=f'fc-{index}',
                                    quality=assessment('major_gaps', 'worker'),
                                    observed_at=now - 7200)
        graded = self.service.predict(self.features)
        self.assertEqual(graded['action'], 'coordinator')
        self.assertIn('learned_quality_below_threshold', graded['reason_codes'])
        self.assertTrue(graded['quality']['veto'])
        self.assertEqual(self.service.decision(graded['id'])['quality'], graded['quality'])
        json.dumps(graded['quality'])


if __name__ == '__main__':
    unittest.main()
