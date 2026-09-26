"""Pure statistics for local worker quality grouped by task category."""
import copy
import json
import math
import unittest

from codex_deepseek_team import routing_estimator, routing_stats
from codex_deepseek_team.routing_models import RoutingError

NOW = 1_700_000_000
DAY = 86400.0
CONFIG = {'confidence': 0.95, 'half_life_days': 90.0, 'max_evidence_age_days': 365.0}


def make(ident, case_id, *, kind='implementation', outcome='accepted', at=NOW,
         action='worker', origin='local', reliability=1.0, **feature_overrides):
    features = {'kind': kind, 'domain': 'python', 'operation': 'extend', 'runtime': 'codex',
                'model': 'deepseek-flash', 'effort': 'high', 'context_version': 'default'}
    features.update(feature_overrides)
    return {'id': ident, 'case_id': case_id, 'origin': origin, 'source_id': 'local',
            'source_family': 'local', 'action': action, 'outcome': outcome,
            'observed_at': at, 'reliability': reliability, 'features': features}


def category(summary, name):
    return next(item for item in summary['categories'] if item['category'] == name)


class SummarizeTests(unittest.TestCase):
    def test_empty_history_has_no_categories_and_no_score(self):
        summary = routing_stats.summarize([], CONFIG, now=NOW)
        self.assertEqual(summary['categories'], [])
        self.assertIn('assignment', summary['case_unit'])
        self.assertEqual(summary['sort'], 'score')

    def test_single_acceptance_is_conservative_not_fifty_percent(self):
        summary = routing_stats.summarize([make('o1', 'c1')], CONFIG, now=NOW)
        item = category(summary, 'implementation')
        expected = routing_estimator._beta_quantile(0.025, 2.0, 1.0)
        self.assertAlmostEqual(item['score'], expected, places=6)
        self.assertAlmostEqual(item['score'], math.sqrt(0.025), places=6)
        self.assertNotAlmostEqual(item['score'], 0.5, places=3)
        self.assertEqual((item['cases'], item['accepted'], item['unlabelled']), (1, 1, 0))

    def test_poor_history_scores_below_good_history(self):
        summary = routing_stats.summarize(
            [make(f'g{i}', f'good-{i}', kind='review') for i in range(10)]
            + [make(f'p{i}', f'poor-{i}', kind='test', outcome='rejected') for i in range(10)],
            CONFIG, now=NOW)
        self.assertGreater(category(summary, 'review')['score'],
                           category(summary, 'test')['score'])

    def test_neutral_only_category_has_null_score_and_sorts_last(self):
        observations = [make('o1', 'c1'),
                        make('n1', 'n1', kind='test', outcome='infrastructure'),
                        make('n2', 'n2', kind='test', outcome='cancelled'),
                        make('n3', 'n3', kind='test', outcome='unknown')]
        summary = routing_stats.summarize(observations, CONFIG, now=NOW)
        self.assertEqual([item['category'] for item in summary['categories']],
                         ['implementation', 'test'])
        neutral = category(summary, 'test')
        self.assertIsNone(neutral['score'])
        self.assertIsNone(neutral['lower'])
        self.assertIsNone(neutral['upper'])
        self.assertEqual((neutral['cases'], neutral['unlabelled']), (3, 3))
        self.assertEqual(neutral['accepted'], 0)

    def test_external_and_coordinator_observations_are_ignored(self):
        observations = [
            make('x1', 'x1', kind='rust', origin='external'),
            make('x2', 'x2', kind='rust', action='coordinator'),
        ]
        summary = routing_stats.summarize(observations, CONFIG, now=NOW)
        self.assertEqual(summary['categories'], [])

    def test_categories_pool_across_context_versions(self):
        observations = [make('a', 'a', context_version='project-v1'),
                        make('b', 'b', context_version='project-v2')]
        item = category(routing_stats.summarize(observations, CONFIG, now=NOW), 'implementation')
        self.assertEqual((item['cases'], item['accepted']), (2, 2))
        self.assertAlmostEqual(item['effective_support'], 2.0, places=9)

    def test_context_change_does_not_duplicate_an_assignment_or_erase_rework(self):
        observations = [make('a', 'same', outcome='rework', at=NOW - 100,
                             context_version='task-v1'),
                        make('b', 'same', at=NOW - 10, context_version='task-v2')]
        item = category(routing_stats.summarize(observations, CONFIG, now=NOW), 'implementation')
        self.assertEqual((item['cases'], item['accepted'], item['rework']), (1, 0, 1))

    def test_neutral_updates_across_context_versions_remain_one_assignment(self):
        observations = [make('a', 'same', outcome='infrastructure', at=NOW - 100,
                             context_version='task-v1'),
                        make('b', 'same', outcome='unknown', at=NOW - 10,
                             context_version='task-v2')]
        item = category(routing_stats.summarize(observations, CONFIG, now=NOW), 'implementation')
        self.assertEqual((item['cases'], item['unlabelled']), (1, 1))

    def test_runtime_model_and_effort_keep_cases_separate(self):
        observations = [
            make('a', 'same', runtime='codex'),
            make('b', 'same', runtime='claude'),
            make('c', 'other', model='deepseek-flash'),
            make('d', 'other', model='deepseek-flash'),
        ]
        item = category(routing_stats.summarize(observations, CONFIG, now=NOW), 'implementation')
        # 'same' appears under two runtimes -> two cases; 'other' duplicated -> one case.
        self.assertEqual(item['cases'], 3)

    def test_legacy_medium_effort_is_canonical(self):
        observations = [
            make('a', 'c1', effort='medium', at=NOW - 100),
            make('b', 'c1', effort='high', outcome='rejected', at=NOW - 10),
        ]
        item = category(routing_stats.summarize(observations, CONFIG, now=NOW), 'implementation')
        self.assertEqual((item['cases'], item['accepted'], item['rejected']), (1, 0, 1))

    def test_duplicate_case_updates_are_not_independent(self):
        observations = [make('a', 'c1', at=NOW - 100), make('b', 'c1', at=NOW - 10)]
        item = category(routing_stats.summarize(observations, CONFIG, now=NOW), 'implementation')
        self.assertEqual((item['cases'], item['accepted']), (1, 1))

    def test_rework_failure_survives_later_acceptance(self):
        observations = [make('a', 'c1', outcome='rework', at=NOW - 100),
                        make('b', 'c1', at=NOW - 10)]
        item = category(routing_stats.summarize(observations, CONFIG, now=NOW), 'implementation')
        self.assertEqual((item['cases'], item['accepted'], item['rework']), (1, 0, 1))

    def test_future_observations_are_excluded(self):
        observations = [make('a', 'c1', at=NOW + 500)]
        summary = routing_stats.summarize(observations, CONFIG, now=NOW)
        self.assertEqual(summary['categories'], [])

    def test_expired_labelled_and_neutral_cases_are_excluded(self):
        observations = [make('a', 'c1', at=NOW - 400 * DAY),
                        make('b', 'c2', outcome='infrastructure', at=NOW - 400 * DAY)]
        summary = routing_stats.summarize(observations, CONFIG, now=NOW)
        self.assertEqual(summary['categories'], [])

    def test_recent_acceptance_does_not_revive_an_expired_failed_case(self):
        observations = [make('a', 'same', outcome='rework', at=NOW - 400 * DAY),
                        make('b', 'same', at=NOW - 1),
                        make('c', 'new', at=NOW - 1)]
        item = category(routing_stats.summarize(observations, CONFIG, now=NOW), 'implementation')
        self.assertEqual((item['cases'], item['accepted'], item['rework']), (1, 1, 0))

    def test_cutoff_boundary_is_included(self):
        observations = [make('a', 'c1', at=NOW - 365 * DAY)]
        item = category(routing_stats.summarize(observations, CONFIG, now=NOW), 'implementation')
        self.assertEqual(item['cases'], 1)
        self.assertGreater(item['effective_support'], 0)

    def test_aging_halves_effective_support_after_one_half_life(self):
        observations = [make('a', 'c1', at=NOW - 90 * DAY)]
        item = category(routing_stats.summarize(observations, CONFIG, now=NOW), 'implementation')
        self.assertAlmostEqual(item['effective_support'], 0.5, places=9)
        self.assertGreater(item['score'], 0.0)

    def test_default_sort_is_score_then_support_then_category(self):
        observations = [
            make('a', 'a', kind='implementation'),                      # one acceptance
            make('b', 'b', kind='review'), make('c', 'c', kind='review'),
            make('d', 'd', kind='test', outcome='rejected'),
        ]
        summary = routing_stats.summarize(observations, CONFIG, now=NOW)
        self.assertEqual([item['category'] for item in summary['categories']],
                         ['review', 'implementation', 'test'])

    def test_sort_choices_are_deterministic(self):
        observations = [
            make('a', 'a', kind='implementation'),
            make('b', 'b', kind='review'), make('c', 'c', kind='review'),
            make('d', 'd', kind='documentation'),
        ]
        by_cases = routing_stats.summarize(observations, CONFIG, now=NOW, sort='cases')
        self.assertEqual(by_cases['categories'][0]['category'], 'review')
        by_category = routing_stats.summarize(observations, CONFIG, now=NOW, sort='category')
        self.assertEqual([item['category'] for item in by_category['categories']],
                         ['documentation', 'implementation', 'review'])

    def test_unknown_sort_is_rejected(self):
        with self.assertRaises(RoutingError):
            routing_stats.summarize([], CONFIG, now=NOW, sort='bogus')

    def test_summary_is_json_safe_stable_and_ignores_inputs(self):
        observations = [make('a', 'a', at=NOW - 45 * DAY),
                        make('b', 'b', kind='test', outcome='rejected'),
                        make('n', 'n', kind='test', outcome='cancelled')]
        original = copy.deepcopy(observations)
        first = routing_stats.summarize(observations, CONFIG, now=NOW)
        second = routing_stats.summarize(observations, CONFIG, now=NOW)
        self.assertEqual(observations, original)
        self.assertEqual(json.dumps(first, sort_keys=True, allow_nan=False),
                         json.dumps(second, sort_keys=True, allow_nan=False))
        text = json.dumps(first, sort_keys=True, allow_nan=False)
        for banned in ('prompt', 'api_key', 'secret'):
            self.assertNotIn(banned, text)

    def test_render_table_shows_counts_and_explains_case_unit(self):
        observations = [make('a', 'a'),
                        make('n', 'n', kind='test', outcome='infrastructure')]
        rendered = routing_stats.render_table(
            routing_stats.summarize(observations, CONFIG, now=NOW))
        self.assertIn('CATEGORY', rendered)
        self.assertIn('SCORE', rendered)
        self.assertIn('ESTIMATE', rendered)
        self.assertIn('INTERVAL', rendered)
        self.assertIn('EFFECTIVE', rendered)
        self.assertIn('implementation', rendered)
        self.assertIn('n/a', rendered)
        self.assertIn('assignment', rendered)
        self.assertIn('score =', rendered)
        self.assertIn('Pooled category history', rendered)
        self.assertIn('not a routing decision', rendered)

    def test_render_empty_table_reports_no_cases(self):
        rendered = routing_stats.render_table(routing_stats.summarize([], CONFIG, now=NOW))
        self.assertIn('No local worker cases recorded', rendered)


if __name__ == '__main__':
    unittest.main()
