"""Pure behavioral contracts for the local learned-quality assessment."""
import json
import unittest

from codex_deepseek_team import routing_learning
from codex_deepseek_team.routing_models import (RoutingError, validate_config, validate_features,
                                                validate_observation)


NOW = 2_000_000_000.0


def card(**changes):
    value = dict(kind='implementation', domain='python', operation='fix',
                 localization='known', coupling='local', verification='tests',
                 clarity='clear', risk='low', scope_size='small', runtime='codex',
                 model='deepseek-flash', effort='high', context_version='task-v1')
    value.update(changes)
    return validate_features(value)


def observation(case, outcome='rejected', *, at=NOW - 3600., origin='local', action='worker',
                ident=None, **changes):
    row = dict(id=ident or f'{origin}:{case}:{outcome}:{at}', case_id=case, origin=origin,
               action=action, features=card(**changes), outcome=outcome, observed_at=at)
    if origin == 'external':
        row.update(source_id='public-suite', source_family='public-suite')
    return validate_observation(row, now=NOW)


def assess(observations, *, features=None, config=None, now=NOW):
    return routing_learning.assess_quality(
        features or card(), observations, config or validate_config({}), now=now)


class QualityAssessmentTests(unittest.TestCase):
    def test_empty_history_is_insufficient_not_bad(self):
        result = assess([])
        self.assertFalse(result['sufficient'])
        self.assertFalse(result['veto'])
        self.assertEqual(result['posterior']['mean'], .5)
        self.assertEqual(result['posterior']['local_effective'], 0.)
        json.dumps(result)

    def test_small_failure_sample_never_vetoes_despite_a_low_upper_bound(self):
        result = assess([observation(f'case-{n}') for n in range(9)])
        self.assertLess(result['posterior']['upper'], result['quality_min_success_probability'])
        self.assertFalse(result['sufficient'])
        self.assertFalse(result['veto'])

    def test_sufficient_poor_history_vetoes_and_good_history_does_not(self):
        poor = assess([observation(f'bad-{n}') for n in range(12)])
        self.assertTrue(poor['sufficient'])
        self.assertTrue(poor['veto'])
        self.assertLess(poor['posterior']['upper'], poor['quality_min_success_probability'])
        self.assertAlmostEqual(poor['posterior']['local_failure_effective'], 12., places=2)
        good = assess([observation(f'good-{n}', outcome='accepted') for n in range(12)])
        self.assertTrue(good['sufficient'])
        self.assertFalse(good['veto'])
        self.assertEqual(good['posterior']['local_failure_effective'], 0.)

    def test_thresholds_come_from_policy_configuration(self):
        rows = [observation(f'case-{n}') for n in range(6)]
        default = assess(rows)
        self.assertFalse(default['sufficient'])
        self.assertFalse(default['veto'])
        lowered = assess(rows, config=validate_config({'quality_min_evidence': 5.}))
        self.assertTrue(lowered['sufficient'])
        self.assertTrue(lowered['veto'])
        relaxed = assess(rows, config=validate_config({'quality_min_evidence': 5.,
                                                       'quality_min_success_probability': .3}))
        self.assertTrue(relaxed['sufficient'])
        self.assertFalse(relaxed['veto'])
        self.assertEqual(relaxed['quality_min_evidence'], 5.)
        self.assertEqual(relaxed['quality_min_success_probability'], .3)

    def test_context_version_is_provenance_rather_than_a_comparison_barrier(self):
        rows = [observation(f'case-{n}', context_version=f'task-{n}') for n in range(12)]
        result = assess(rows)
        self.assertEqual(result['posterior']['matched_local'], 12)
        self.assertTrue(result['veto'])
        self.assertEqual(result['comparison']['context_version'], 'ignored_for_class_learning')

    def test_identity_and_anchor_separation(self):
        cases = ({'runtime': 'claude'}, {'model': 'other-model'}, {'effort': 'low'},
                 {'kind': 'test'}, {'domain': 'rust'}, {'operation': 'extend'})
        for changes in cases:
            with self.subTest(changes=changes):
                result = assess([observation(f'case-{n}', **changes) for n in range(20)])
                self.assertFalse(result['sufficient'], changes)
                self.assertFalse(result['veto'], changes)

    def test_legacy_medium_effort_counts_as_the_canonical_high_level(self):
        rows = []
        for index in range(12):
            row = observation(f'case-{index}')
            row['features'] = dict(row['features'], effort='medium')
            rows.append(row)
        result = assess(rows)
        self.assertTrue(result['sufficient'])
        self.assertTrue(result['veto'])

    def test_soft_context_similarity_and_configured_minimum(self):
        one_mismatch = assess([observation(f'case-{n}', risk='medium') for n in range(13)])
        self.assertTrue(one_mismatch['sufficient'])
        self.assertTrue(one_mismatch['veto'])
        disjoint = assess([observation(f'case-{n}', localization='unknown', coupling='component',
                                       verification='manual', clarity='partial') for n in range(30)])
        self.assertFalse(disjoint['sufficient'])
        boundary_rows = [observation(f'case-{n}', coupling='component', verification='manual',
                                     clarity='partial') for n in range(30)]
        self.assertFalse(assess(boundary_rows)['sufficient'])
        at_boundary = assess(boundary_rows, config=validate_config({'min_similarity': .5}))
        self.assertTrue(at_boundary['sufficient'])
        self.assertTrue(at_boundary['veto'])

    def test_external_and_coordinator_outcomes_never_build_the_local_veto(self):
        external = assess([observation(f'ext-{n}', origin='external') for n in range(30)])
        self.assertFalse(external['sufficient'])
        self.assertFalse(external['veto'])
        self.assertEqual(external['posterior']['external_effective'], 0.)
        coordinator = assess([observation(f'coord-{n}', action='coordinator') for n in range(30)])
        self.assertFalse(coordinator['sufficient'])
        self.assertFalse(coordinator['veto'])

    def test_infrastructure_cancelled_and_unknown_outcomes_are_neutral(self):
        rows = [observation(f'neutral-{n}', outcome)
                for n, outcome in enumerate(('infrastructure', 'cancelled', 'unknown') * 10)]
        result = assess(rows)
        self.assertEqual(result['posterior']['local_effective'], 0.)
        self.assertFalse(result['sufficient'])
        self.assertFalse(result['veto'])

    def test_one_assignment_is_one_case_even_with_many_feedback_events(self):
        rows = [observation('same-case', 'rework', at=NOW - 3600., ident='first'),
                observation('same-case', 'accepted', at=NOW - 3500., ident='second')]
        rows.extend(observation('same-case', 'accepted', at=NOW - 3400. + n, ident=f'extra-{n}')
                    for n in range(12))
        result = assess(rows)
        self.assertEqual(result['posterior']['matched_local'], 1)
        self.assertFalse(result['sufficient'])
        self.assertFalse(result['veto'])
        # Acceptance after rework can never launder the first labelled failure.
        self.assertGreater(result['posterior']['beta'], 1.5)

    def test_distinct_assignments_in_one_task_remain_distinct_cases(self):
        result = assess([observation(f'assignment-{n}') for n in range(12)])
        self.assertEqual(result['posterior']['matched_local'], 12)
        self.assertTrue(result['veto'])

    def test_future_observations_are_not_counted(self):
        result = assess([observation(f'future-{n}', at=NOW + 30) for n in range(20)])
        self.assertFalse(result['sufficient'])
        self.assertFalse(result['veto'])

    def test_aging_expiry_and_decay_restore_admission(self):
        expired = assess([observation(f'old-{n}', at=NOW - 40 * 86400) for n in range(20)],
                         config=validate_config({'max_evidence_age_days': 30.}))
        self.assertFalse(expired['sufficient'])
        self.assertFalse(expired['veto'])
        # The same rows inside the default horizon still count; aging, not absence,
        # is what restored admission above.
        retained = assess([observation(f'fresh-{n}', at=NOW - 40 * 86400) for n in range(20)])
        self.assertTrue(retained['veto'])
        decayed = assess([observation(f'decay-{n}', at=NOW - 3 * 86400) for n in range(20)],
                         config=validate_config({'half_life_days': 1.}))
        self.assertLess(decayed['posterior']['local_effective'], 10.)
        self.assertFalse(decayed['veto'])

    def test_assessment_is_json_safe_and_reports_its_comparison_scope(self):
        result = assess([observation(f'case-{n}') for n in range(12)])
        self.assertEqual(json.loads(json.dumps(result)), result)
        comparison = result['comparison']
        self.assertEqual(comparison['identity'], ['runtime', 'model', 'effort'])
        self.assertEqual(comparison['anchors'], ['kind', 'domain', 'operation'])
        self.assertEqual(len(comparison['soft_context']), 6)
        self.assertEqual(comparison['basis'], 'local_worker_quality_cases')
        self.assertLessEqual(len(result['posterior']['evidence_ids']), 128)
        self.assertEqual(result['posterior']['evidence_count'], 12)



class QualityPolicyValidationTests(unittest.TestCase):
    def test_quality_settings_carry_validated_defaults(self):
        config = validate_config({})
        self.assertEqual(config['quality_min_evidence'], 10.)
        self.assertEqual(config['quality_min_success_probability'], .7)

    def test_invalid_quality_settings_are_rejected(self):
        for values in ({'quality_min_evidence': 0.}, {'quality_min_evidence': -1.},
                       {'quality_min_evidence': float('inf')}, {'quality_min_evidence': 10001.},
                       {'quality_min_evidence': True}, {'quality_min_success_probability': -.01},
                       {'quality_min_success_probability': 1.01},
                       {'quality_min_success_probability': float('nan')},
                       {'min_success_probability': .8}):
            with self.subTest(values=values), self.assertRaises(RoutingError):
                validate_config(values)
        relaxed = validate_config({'quality_min_evidence': 10.5,
                                   'quality_min_success_probability': 0.})
        self.assertEqual(relaxed['quality_min_evidence'], 10.5)
        self.assertEqual(relaxed['quality_min_success_probability'], 0.)

if __name__ == '__main__':
    unittest.main()
