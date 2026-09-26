"""Identity contracts for graded quality across context versions.

One coordinator-reviewed assignment is one quality case per execution identity
(``origin``, ``case_id``, ``action``, ``runtime``, ``model``, canonical
``effort``). ``context_version`` is provenance: an explicit grade recorded under
another context version must reach the same assignment's earlier
forecast-context evidence and retrospective evaluation target, but only from
the moment that event is observed. Exact-context forecast matching, cost
identity and the cooldown family keep their original context identity.
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
                                 routing_quality, settings)
from codex_deepseek_team.routing import RoutingService
from codex_deepseek_team.routing_models import (validate_config, validate_features,
                                                validate_observation)

NOW = 2_000_000_000.0
CASE = 'as-405a2a8ccc76247c'
MET = {'grade': 'met', 'attribution': 'worker',
       'evidence': 'supplied requirements satisfied with cosmetic edits'}
MAJOR = {'grade': 'major_gaps', 'attribution': 'worker',
         'evidence': 'main supplied requirement failed'}
NEUTRAL = {'grade': 'major_gaps', 'attribution': 'coordinator',
           'evidence': 'gap owned by the coordinator, not the worker'}


def card(**changes):
    value = dict(kind='implementation', domain='python', operation='fix',
                 localization='known', coupling='local', verification='tests',
                 clarity='clear', risk='low', scope_size='small', runtime='codex',
                 model='deepseek-flash', effort='high', context_version='v1')
    value.update(changes)
    return validate_features(value)


def observation(case, outcome, *, at, context_version='v1', ident=None, quality=None,
                cost_usd=None, **changes):
    row = dict(id=ident or f'{case}:{context_version}:{outcome}:{at}', case_id=case,
               origin='local', features=card(context_version=context_version, **changes),
               action='worker', outcome=outcome, observed_at=at)
    if quality is not None:
        row['quality'] = quality
    if cost_usd is not None:
        row['cost_usd'] = cost_usd
    return validate_observation(row, now=NOW)


def evaluate(rows, *, now=NOW, **config):
    return routing_estimator.evaluate(rows, validate_config(config), now=now)


def forecast(rows, *, features=None, now=NOW, **config):
    return routing_estimator.forecast(features or card(), rows, validate_config(config), now=now)


class QualityCaseIdentityTests(unittest.TestCase):
    def test_context_version_is_provenance_not_a_second_quality_case(self):
        v1 = observation(CASE, 'accepted', at=NOW - 100, context_version='v1')
        v2 = observation(CASE, 'rework', at=NOW - 50, context_version='v2', quality=MAJOR)
        self.assertEqual(routing_quality.quality_case_key(v1),
                         routing_quality.quality_case_key(v2))

    def test_execution_drift_still_separates_quality_cases(self):
        base = observation(CASE, 'accepted', at=NOW - 100)
        for changes in ({'effort': 'low'}, {'runtime': 'claude'}, {'model': 'other-model'}):
            with self.subTest(changes=changes):
                self.assertNotEqual(
                    routing_quality.quality_case_key(base),
                    routing_quality.quality_case_key(
                        observation(CASE, 'accepted', at=NOW - 100, **changes)))


class BatchForecastIdentityTests(unittest.TestCase):
    def test_later_explicit_grade_reaches_earlier_context_evidence(self):
        rows = [observation(CASE, 'accepted', at=NOW - 100, context_version='v1'),
                observation(CASE, 'rework', at=NOW - 50, context_version='v2', quality=MAJOR)]
        posterior = forecast(rows)['posterior']
        self.assertEqual(posterior['matched_local'], 1)
        self.assertAlmostEqual(posterior['local_effective'], 1., places=4)
        self.assertAlmostEqual(posterior['local_failure_effective'], .7, places=4)
        self.assertAlmostEqual(posterior['mean'], 1.3 / 3., places=4)
        learned = routing_learning.assess_quality(card(), rows, validate_config({}), now=NOW)
        self.assertAlmostEqual(learned['posterior']['mean'], posterior['mean'], places=6)

    def test_exact_context_matching_is_not_widened(self):
        rows = [observation(CASE, 'accepted', at=NOW - 100, context_version='v1'),
                observation(CASE, 'rework', at=NOW - 50, context_version='v2', quality=MAJOR)]
        unrelated = forecast(rows, features=card(context_version='v3'))
        self.assertEqual(unrelated['posterior']['local_effective'], 0.)
        cross = forecast([rows[0]], features=card(context_version='v2'))
        self.assertEqual(cross['posterior']['local_effective'], 0.)
        same = forecast([rows[1]], features=card(context_version='v2'))
        self.assertAlmostEqual(same['posterior']['local_failure_effective'], .7, places=4)

    def test_quality_identity_keeps_distinct_effort_apart(self):
        rows = [observation(CASE, 'accepted', at=NOW - 100, context_version='v1'),
                observation(CASE, 'rework', at=NOW - 50, context_version='v2', effort='low',
                            quality=MAJOR)]
        high = forecast(rows)['posterior']
        self.assertEqual(high['local_failure_effective'], 0.)
        self.assertAlmostEqual(high['mean'], 2. / 3., places=4)
        low = forecast(rows, features=card(effort='low', context_version='v2'))['posterior']
        self.assertAlmostEqual(low['local_failure_effective'], .7, places=4)

    def test_a_grade_after_the_batch_cutoff_is_not_known(self):
        rows = [observation(CASE, 'accepted', at=NOW - 100, context_version='v1'),
                observation(CASE, 'rework', at=NOW + 10, context_version='v2', quality=MAJOR)]
        before = forecast(rows, now=NOW - 50)['posterior']
        self.assertEqual(before['local_failure_effective'], 0.)
        after = forecast(rows, now=NOW + 20)['posterior']
        self.assertAlmostEqual(after['local_failure_effective'], .7, places=4)


class RetrospectiveEvaluationIdentityTests(unittest.TestCase):
    def test_explicit_v2_grade_corrects_the_v1_prediction_and_raw_counts(self):
        rows = [observation(CASE, 'accepted', at=NOW - 100, context_version='v1', cost_usd=2.),
                observation(CASE, 'rework', at=NOW - 50, context_version='v2', quality=MAJOR,
                            cost_usd=5.)]
        result = evaluate(rows)
        self.assertEqual(result['evaluated_cases'], 1)
        prediction = result['predictions'][0]
        self.assertEqual(prediction['probability'], .5)
        self.assertEqual(prediction['outcome'], 'rework')
        self.assertEqual(prediction['quality_score'], .3)
        self.assertEqual(prediction['quality_kind'], 'explicit')
        self.assertEqual(result['quality_cases']['explicit'], 1)
        self.assertEqual(result['quality_cases']['legacy'], 0)
        self.assertEqual(result['quality_cases']['clean_first_pass'], 0)
        worker = result['observed_outcomes']['worker']
        self.assertEqual((worker['accepted'], worker['rework'], worker['rejected']), (0, 1, 0))
        # Cost identity keeps the exact context version of the forecast event.
        self.assertEqual((worker['cost_cases'], worker['mean_cost_usd']), (1, 2.))
        self.assertAlmostEqual(result['brier_score'], .04, places=12)
        self.assertAlmostEqual(result['log_loss'], 0.6931471805599453, places=12)
        json.dumps(result, allow_nan=False)

    def test_prefix_without_the_grade_never_sees_it(self):
        accepted = observation(CASE, 'accepted', at=NOW - 100, context_version='v1', cost_usd=2.)
        graded = observation(CASE, 'rework', at=NOW - 50, context_version='v2', quality=MAJOR)
        prefix = evaluate([accepted], now=NOW - 75)
        self.assertEqual(prefix['predictions'][0]['outcome'], 'accepted')
        self.assertEqual(prefix['predictions'][0]['quality_kind'], 'legacy')
        self.assertEqual(prefix['predictions'][0]['quality_score'], 1.0)
        cutoff = evaluate([accepted, graded], now=NOW - 60)
        self.assertEqual(cutoff['predictions'][0]['outcome'], 'accepted')
        self.assertEqual(cutoff['predictions'][0]['quality_kind'], 'legacy')
        corrected = evaluate([accepted, graded])
        self.assertEqual(corrected['predictions'][0]['probability'], .5)
        self.assertEqual(corrected['predictions'][0]['outcome'], 'rework')
        self.assertEqual(corrected['predictions'][0]['quality_score'], .3)

    def test_reverse_order_grade_still_grades_the_raw_context_failure(self):
        rows = [observation(CASE, 'rework', at=NOW - 100, context_version='v2', ident='raw'),
                observation(CASE, 'accepted', at=NOW - 50, context_version='v1', quality=MAJOR,
                            ident='grade')]
        result = evaluate(rows)
        prediction = result['predictions'][0]
        self.assertEqual(prediction['observed_at'], NOW - 100)
        self.assertEqual(prediction['outcome'], 'rework')
        self.assertEqual(prediction['quality_score'], .3)
        self.assertEqual(prediction['quality_kind'], 'explicit')
        worker = result['observed_outcomes']['worker']
        self.assertEqual((worker['accepted'], worker['rework']), (0, 1))
        self.assertEqual(result['quality_cases']['clean_first_pass'], 0)

    def test_neutral_cross_context_assessment_supersedes_the_binary_failure(self):
        rows = [observation(CASE, 'accepted', at=NOW - 100, context_version='v1'),
                observation(CASE, 'rework', at=NOW - 50, context_version='v2', quality=NEUTRAL)]
        result = evaluate(rows)
        prediction = result['predictions'][0]
        self.assertEqual(prediction['outcome'], 'rework')
        self.assertIsNone(prediction['quality_score'])
        self.assertEqual(prediction['quality_kind'], 'neutral')
        self.assertIsNone(result['brier_score'])
        self.assertEqual(result['quality_cases']['neutral'], 1)
        self.assertEqual(result['quality_cases']['clean_first_pass'], 0)
        self.assertEqual(forecast(rows)['posterior']['local_effective'], 0.)

    def test_cosmetic_rework_keeps_full_credit_across_contexts(self):
        rows = [observation(CASE, 'rework', at=NOW - 100, context_version='v1', ident='raw'),
                observation(CASE, 'accepted', at=NOW - 50, context_version='v2', quality=MET,
                            ident='grade')]
        result = evaluate(rows)
        prediction = result['predictions'][0]
        self.assertEqual(prediction['outcome'], 'rework')
        self.assertEqual(prediction['quality_kind'], 'explicit')
        self.assertEqual(prediction['quality_score'], 1.0)
        worker = result['observed_outcomes']['worker']
        self.assertEqual((worker['accepted'], worker['rework']), (0, 1))
        posterior = forecast(rows)['posterior']
        self.assertEqual(posterior['local_failure_effective'], 0.)
        self.assertAlmostEqual(posterior['mean'], 2. / 3., places=4)

    def test_distinct_execution_identity_grade_does_not_grade_its_sibling(self):
        for changes in ({'effort': 'low'}, {'runtime': 'claude'}, {'model': 'other-model'}):
            with self.subTest(changes=changes):
                rows = [observation(CASE, 'accepted', at=NOW - 100, context_version='v1'),
                        observation(CASE, 'rework', at=NOW - 50, context_version='v2',
                                    quality=MAJOR, **changes)]
                result = evaluate(rows)
                prediction = result['predictions'][0]
                self.assertEqual(prediction['outcome'], 'accepted')
                self.assertEqual(prediction['quality_kind'], 'legacy')
                self.assertEqual(prediction['quality_score'], 1.0)
                self.assertEqual(result['observed_outcomes']['worker']['accepted'], 1)
                self.assertEqual(result['quality_cases']['clean_first_pass'], 1)

    def test_incremental_grade_reaches_earlier_context_evidence_without_leaking_back(self):
        first = observation(CASE, 'accepted', at=NOW - 100, context_version='v1', ident='first')
        grade = observation(CASE, 'rework', at=NOW - 50, context_version='v2', quality=MAJOR,
                            ident='grade')
        later = observation('later-case', 'accepted', at=NOW - 10, context_version='v1',
                            ident='later')
        without = evaluate([first, later])
        self.assertEqual(without['predictions'][0]['probability'], .5)
        self.assertAlmostEqual(without['predictions'][-1]['probability'], 2. / 3., places=4)
        with_grade = evaluate([first, grade, later])
        self.assertEqual(with_grade['predictions'][0]['probability'], .5)
        self.assertAlmostEqual(with_grade['predictions'][-1]['probability'], 1.3 / 3., places=4)
        # The incremental evaluator matches a fresh contextual forecast over the
        # same prefix, so the earlier context entry carries the later grade.
        expected = routing_estimator.forecast(card(), [first, grade], validate_config({}),
                                              now=NOW - 10)
        self.assertAlmostEqual(with_grade['predictions'][-1]['probability'],
                               expected['posterior']['mean'], places=12)


class CooldownIdentityTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        self.conn.isolation_level = None
        self.addCleanup(self.conn.close)
        routing_admission.initialize(self.conn)

    def record(self, rows, index=-1):
        return routing_admission.record_outcome(self.conn, rows[index], rows, {})

    def test_cross_context_met_suppresses_a_legacy_rejection_pause(self):
        cross = [observation(CASE, 'rework', at=100., context_version='v2', quality=MET,
                             ident='met'),
                 observation(CASE, 'rejected', at=110., context_version='v1', ident='rej')]
        same = [observation(CASE + '-same', 'rework', at=100., context_version='v1', quality=MET,
                            ident='met'),
                observation(CASE + '-same', 'rejected', at=110., context_version='v1',
                            ident='rej')]
        self.assertFalse(self.record(cross))
        self.assertFalse(routing_admission.cooling(self.conn, card(), now=120.))
        self.assertFalse(self.record(same))
        self.assertFalse(routing_admission.cooling(self.conn, card(), now=120.))

    def test_cross_context_neutral_assessment_suppresses_a_legacy_rejection(self):
        rows = [observation(CASE, 'rework', at=100., context_version='v2', quality=NEUTRAL,
                            ident='neutral'),
                observation(CASE, 'rejected', at=110., context_version='v1', ident='rej')]
        self.assertFalse(self.record(rows))
        self.assertFalse(routing_admission.cooling(self.conn, card(), now=120.))

    def test_cross_context_substantive_failure_still_pauses(self):
        rows = [observation(CASE, 'rework', at=100., context_version='v2', quality=MAJOR,
                            ident='major'),
                observation(CASE, 'rejected', at=110., context_version='v1', ident='rej')]
        self.assertTrue(self.record(rows))
        self.assertTrue(routing_admission.cooling(self.conn, card(), now=120.))

    def test_three_rework_cases_qualify_through_cross_context_grades(self):
        met, substantive = [], []
        for index in range(3):
            met_case, major_case = f'{CASE}-met-{index}', f'{CASE}-major-{index}'
            met += [observation(met_case, 'rework', at=100. + index, context_version='v2',
                                quality=MET, ident=f'{met_case}-grade'),
                    observation(met_case, 'rework', at=101. + index, context_version='v1',
                                ident=f'{met_case}-raw')]
            substantive += [observation(major_case, 'rework', at=100. + index,
                                        context_version='v2', quality=MAJOR,
                                        ident=f'{major_case}-grade'),
                            observation(major_case, 'rework', at=101. + index,
                                        context_version='v1', ident=f'{major_case}-raw')]
        self.assertFalse(self.record(met))
        self.assertFalse(routing_admission.cooling(self.conn, card(), now=120.))
        self.assertTrue(self.record(substantive))
        self.assertTrue(routing_admission.cooling(self.conn, card(), now=120.))

    def test_cooldown_family_retains_its_context_identity(self):
        rows = [observation(CASE, 'rejected', at=100., context_version='v1')]
        self.assertTrue(self.record(rows))
        self.assertTrue(routing_admission.cooling(self.conn, card(context_version='v1'),
                                                  now=110.))
        self.assertFalse(routing_admission.cooling(self.conn, card(context_version='v2'),
                                                   now=110.))


class ServiceIdentityTests(unittest.TestCase):
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
        settings.set_values(self.root / settings.PROJECT_FILE, access='full-access')
        self.features = card()

    def stored(self, ident, outcome, *, observed_at, context_version='v1', quality=None,
               cost_usd=None, case_id=CASE):
        row = dict(id=ident, case_id=case_id, origin='local',
                   features=card(context_version=context_version), action='worker',
                   outcome=outcome, observed_at=observed_at)
        if quality is not None:
            row['quality'] = quality
        if cost_usd is not None:
            row['cost_usd'] = cost_usd
        return self.service.observe(row)

    def test_service_evaluation_and_learned_quality_agree_on_identity(self):
        now = time.time()
        self.stored('accepted-v1', 'accepted', observed_at=now - 100, context_version='v1',
                    cost_usd=2.)
        self.stored('rework-v2', 'rework', observed_at=now - 50, context_version='v2',
                    quality=MAJOR, cost_usd=5.)
        result = self.service.evaluate()
        prediction = result['predictions'][0]
        self.assertEqual((prediction['outcome'], prediction['quality_kind']),
                         ('rework', 'explicit'))
        self.assertAlmostEqual(prediction['quality_score'], .3, places=9)
        self.assertEqual(result['quality_cases']['explicit'], 1)
        self.assertEqual(result['quality_cases']['legacy'], 0)
        self.assertEqual(result['quality_cases']['clean_first_pass'], 0)
        worker = result['observed_outcomes']['worker']
        self.assertEqual((worker['accepted'], worker['rework']), (0, 1))
        self.assertEqual(worker['mean_cost_usd'], 2.)
        decision = self.service.predict(self.features)
        self.assertEqual(decision['action'], 'worker')
        self.assertAlmostEqual(decision['quality']['posterior']['mean'], 1.3 / 3., places=4)
        json.dumps(result, allow_nan=False)

    def test_service_cooldown_ignores_a_legacy_rejection_with_a_cross_context_met_grade(self):
        now = time.time()
        self.stored('met-v2', 'rework', observed_at=now - 100, context_version='v2', quality=MET)
        self.stored('rejected-v1', 'rejected', observed_at=now - 50, context_version='v1')
        decision = self.service.predict(self.features)
        self.assertEqual(decision['action'], 'worker')
        self.assertNotIn('quality_failure_cooldown', decision['reason_codes'])

    def test_service_cooldown_still_pauses_for_a_cross_context_substantive_failure(self):
        now = time.time()
        self.stored('major-v2', 'rework', observed_at=now - 100, context_version='v2',
                    quality=MAJOR)
        self.stored('rejected-v1', 'rejected', observed_at=now - 50, context_version='v1')
        decision = self.service.predict(self.features)
        self.assertEqual(decision['action'], 'coordinator')
        self.assertIn('quality_failure_cooldown', decision['reason_codes'])


if __name__ == '__main__':
    unittest.main()
