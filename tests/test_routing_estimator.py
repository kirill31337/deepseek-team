"""Behavioral checks for contextual evidence and leakage-free routing estimates."""
from copy import deepcopy
import json
import math
import random
import unittest
from unittest import mock

from codex_deepseek_team import routing_estimator

from codex_deepseek_team.routing_estimator import estimate, evaluate, forecast
from codex_deepseek_team.routing_models import DEFAULT_CONFIG, FEATURE_DEFAULTS

NOW = 1_780_000_000.0


def features(**changes):
    value = dict(FEATURE_DEFAULTS)
    value.update(kind="implementation", domain="python", operation="fix",
                 localization="known", coupling="local", verification="tests",
                 clarity="clear", risk="low", scope_size="small", runtime="codex",
                 model="deepseek-test", effort="high", context_version="v1")
    value.update(changes)
    return value


def config(**changes):
    return {**DEFAULT_CONFIG, **changes}


def observation(number=0, **changes):
    value = dict(id=f"obs-{number}", case_id=f"case-{number}", origin="local",
                 source_id="project", source_family="project", features=features(),
                 action="worker", outcome="accepted", observed_at=NOW,
                 cost_usd=1.0, duration_seconds=10.0, reliability=1.0)
    value.update(changes)
    return value


def good_history(count=50):
    return ([observation(i) for i in range(count)] +
            [observation(i + count, action="coordinator", cost_usd=5.0)
             for i in range(count)])


class PosteriorTests(unittest.TestCase):
    def test_no_evidence_is_uniform_with_exact_interval(self):
        p = estimate(features(), [], config(), now=NOW)
        self.assertEqual(p["mean"], .5)
        self.assertEqual((p["alpha"], p["beta"]), (1., 1.))
        self.assertAlmostEqual(p["lower"], .025, places=10)
        self.assertAlmostEqual(p["upper"], .975, places=10)

    def test_beta_quantiles_match_analytic_all_success_posterior(self):
        p = estimate(features(), [observation(i) for i in range(9)], config(), now=NOW)
        self.assertEqual((p["alpha"], p["beta"]), (10., 1.))
        self.assertAlmostEqual(p["lower"], .025 ** .1, places=10)
        self.assertAlmostEqual(p["upper"], .975 ** .1, places=10)

    def test_rework_is_failure_and_unlabelled_is_not_failure(self):
        rows = [observation(1, outcome="rework"), observation(2, outcome="rejected")]
        rows += [observation(i + 3, outcome=o) for i, o in
                 enumerate(["unknown", "cancelled", "infrastructure"])]
        p = estimate(features(), rows, config(), now=NOW)
        self.assertEqual((p["alpha"], p["beta"]), (1., 3.))
        self.assertAlmostEqual(p["lower"], 1 - .975 ** (1 / 3), places=10)
        self.assertAlmostEqual(p["upper"], 1 - .025 ** (1 / 3), places=10)

    def test_execution_drift_cannot_supply_support(self):
        for key, other in [("model", "new-model"), ("runtime", "claude"),
                           ("effort", "low"), ("context_version", "v2")]:
            with self.subTest(key=key):
                p = estimate(features(**{key: other}), [observation()], config(), now=NOW)
                self.assertEqual(p["local_effective"], 0)

    def test_coordinator_quality_does_not_become_worker_quality(self):
        p = estimate(features(), [observation(action="coordinator")], config(), now=NOW)
        self.assertEqual(p["mean"], .5)

    def test_half_life_reliability_and_age_are_applied(self):
        row = observation(observed_at=NOW - 90 * 86400, reliability=.5)
        p = estimate(features(), [row], config(), now=NOW)
        self.assertAlmostEqual(p["local_effective"], .25)
        stale = observation(observed_at=NOW - 366 * 86400)
        self.assertEqual(estimate(features(), [stale], config(), now=NOW)["local_effective"], 0)
        future = observation(observed_at=NOW + 10)
        self.assertEqual(estimate(features(), [future], config(), now=NOW)["local_effective"], 0)

    def test_similar_context_is_discounted_and_unrelated_excluded(self):
        near = features(localization="partial")
        p = estimate(near, [observation()], config(), now=NOW)
        self.assertGreater(p["local_effective"], 0)
        self.assertLess(p["local_effective"], 1)
        distant = features(kind="research", domain="javascript", operation="extend",
                           localization="unknown", coupling="cross-component", verification="none",
                           clarity="unknown", risk="high", scope_size="large")
        self.assertEqual(estimate(distant, [observation()], config(), now=NOW)["local_effective"], 0)

    def test_task_family_anchors_block_both_positive_and_negative_transfer(self):
        for origin in ("local", "external"):
            for outcome in ("accepted", "rework", "rejected"):
                rows = [observation(i, outcome=outcome, origin=origin) for i in range(30)]
                for changes in (dict(kind="review"), dict(domain="rust"),
                                dict(operation="review"),
                                dict(kind="review", domain="rust", operation="review")):
                    with self.subTest(origin=origin, outcome=outcome, changes=changes):
                        p = estimate(features(**changes), rows, config(min_similarity=0), now=NOW)
                        self.assertEqual((p["alpha"], p["beta"]), (1., 1.))
                        self.assertEqual(p["mean"], .5)
                        self.assertEqual(p["evidence_ids"], [])

    def test_unknown_family_anchors_are_not_comparable_even_when_equal(self):
        for key in ("domain", "operation"):
            card = features(**{key: "unknown"})
            rows = [observation(features=card)]
            self.assertEqual(estimate(card, rows, config(min_similarity=0), now=NOW)["local_effective"], 0.)

    def test_relabelled_retry_does_not_create_an_independent_case(self):
        rows = [observation(1, case_id="same", observed_at=NOW-1),
                observation(2, case_id="same", features=features(domain="rust"))]
        original = estimate(features(), rows, config(), now=NOW)
        relabelled = estimate(features(domain="rust"), rows, config(), now=NOW)
        self.assertEqual(original["matched_local"], 1)
        self.assertEqual(original["evidence_ids"], ["obs-1"])
        self.assertEqual(relabelled["matched_local"], 0)
        self.assertEqual(relabelled["mean"], .5)

    def test_stale_failures_decay_toward_uncertain_prior(self):
        rows = [observation(i, outcome="rejected") for i in range(100)]
        cfg = config(half_life_days=7)
        fresh = estimate(features(), rows, cfg, now=NOW)
        stale = estimate(features(), rows, cfg, now=NOW+90*86400)
        expired = estimate(features(), rows, cfg, now=NOW+366*86400)
        self.assertLess(fresh["mean"], .02)
        self.assertGreater(stale["mean"], .49)
        self.assertLess(stale["mean"], .5)
        self.assertGreater(stale["upper"]-stale["lower"], .9)
        self.assertLess(stale["local_effective"], fresh["local_effective"])
        self.assertEqual(expired["mean"], .5)
        self.assertAlmostEqual(expired["lower"], .025, places=10)
        self.assertAlmostEqual(expired["upper"], .975, places=10)

    def test_duplicate_source_ids_do_not_multiply_a_case(self):
        rows = [observation(i, case_id="same", origin="external", source_id=f"source-{i}")
                for i in range(100)]
        p = estimate(features(), rows, config(), now=NOW)
        self.assertEqual(p["matched_external"], 1)
        self.assertEqual(p["external_effective"], 1)

    def test_first_labelled_attempt_is_not_replaced_by_later_success(self):
        rows = [observation(1, case_id="same", outcome="infrastructure", observed_at=NOW-3),
                observation(2, case_id="same", outcome="rework", observed_at=NOW-2),
                observation(3, case_id="same", outcome="accepted", observed_at=NOW-1)]
        p = estimate(features(), rows[::-1], config(), now=NOW)
        self.assertEqual(p["alpha"], 1)
        self.assertGreater(p["beta"], 1.99)
        self.assertEqual(p["evidence_ids"], ["obs-2"])

    def test_failure_dominates_acceptance_in_both_temporal_orders(self):
        for failure in ("rework", "rejected"):
            for first, second in (("accepted", failure), (failure, "accepted")):
                with self.subTest(first=first, second=second):
                    rows = [observation(1, case_id="same", outcome=first, observed_at=NOW-2),
                            observation(2, case_id="same", outcome=second, observed_at=NOW-1)]
                    for ordering in (rows, rows[::-1]):
                        p = estimate(features(), ordering, config(), now=NOW)
                        self.assertEqual(p["alpha"], 1.)
                        self.assertGreater(p["beta"], 1.99)
                        self.assertEqual(p["matched_local"], 1)
                        self.assertEqual(p["evidence_ids"], ["obs-1" if first == failure else "obs-2"])

    def test_failure_correction_is_not_known_before_its_timestamp(self):
        rows = [observation(1, case_id="same", observed_at=NOW-3),
                observation(2, case_id="same", outcome="rework", observed_at=NOW-1),
                observation(3, case_id="same", outcome="rejected", observed_at=NOW)]
        before = estimate(features(), rows, config(), now=NOW-2)
        self.assertGreater(before["alpha"], 1.99)
        self.assertEqual(before["beta"], 1.)
        after = estimate(features(), rows, config(), now=NOW)
        self.assertEqual(after["alpha"], 1.)
        self.assertEqual(after["evidence_ids"], ["obs-2"])

    def test_conflicting_simultaneous_attempts_use_failure_conservatively(self):
        rows = [observation(1, case_id="same"),
                observation(2, case_id="same", outcome="rework")]
        for ordering in [rows, rows[::-1]]:
            p = estimate(features(), ordering, config(), now=NOW)
            self.assertEqual((p["alpha"], p["beta"]), (1., 2.))

    def test_external_global_and_family_caps_apply_proportionally(self):
        rows = [observation(i, origin="external", source_family=f"family-{i // 100}")
                for i in range(300)]
        p = estimate(features(), rows, config(), now=NOW)
        self.assertAlmostEqual(p["external_effective"], 5)
        p = estimate(features(), rows[:100], config(), now=NOW)
        self.assertAlmostEqual(p["external_effective"], 2)
        self.assertEqual(p["local_effective"], 0)
        p = estimate(features(), rows + [observation(1000)], config(), now=NOW)
        self.assertAlmostEqual(p["local_effective"], 1)

    def test_fractional_and_large_balanced_posteriors_are_finite_symmetric(self):
        for n, reliability in [(2, .01), (10000, 1.)]:
            rows = [observation(i, reliability=reliability,
                                outcome="accepted" if i % 2 else "rejected") for i in range(n)]
            p = estimate(features(), rows, config(), now=NOW)
            self.assertAlmostEqual(p["lower"] + p["upper"], 1, places=10)
            self.assertLess(p["lower"], .5)
            self.assertGreater(p["upper"], .5)
            json.dumps(p, allow_nan=False)

    def test_fractional_quantiles_match_closed_form_cdf(self):
        rows = [observation(1, reliability=.5),
                observation(2, reliability=.5, outcome="rejected")]
        p = estimate(features(), rows, config(), now=NOW)
        for key, probability in [("lower", .025), ("upper", .975)]:
            x = p[key]
            actual_cdf = 2 / math.pi * (math.asin(math.sqrt(x)) -
                                        (1 - 2*x) * math.sqrt(x*(1-x)))
            self.assertAlmostEqual(actual_cdf, probability, places=10)

    def test_extreme_valid_confidence_is_finite(self):
        for rows in [[], [observation()], [observation(outcome="rejected")]]:
            p = estimate(features(), rows, config(confidence=math.nextafter(1., 0.)), now=NOW)
            json.dumps(p, allow_nan=False)
            self.assertLessEqual(p["lower"], p["mean"])
            self.assertGreaterEqual(p["upper"], p["mean"])

    def test_inputs_are_not_mutated(self):
        f, rows, cfg = features(), good_history(), config()
        before = deepcopy((f, rows, cfg))
        forecast(f, rows, cfg, now=NOW)
        evaluate(rows, cfg, now=NOW)
        self.assertEqual((f, rows, cfg), before)


class ForecastTests(unittest.TestCase):
    def test_forecast_contains_estimates_without_executor_decision(self):
        for card in (features(), features(kind="security", risk="protected")):
            for mode in ("auto", "off"):
                with self.subTest(card=card, mode=mode):
                    result = forecast(card, [], config(mode=mode), now=NOW)
                    self.assertEqual(set(result), {"posterior", "economics"})
                    self.assertEqual(result["posterior"]["mean"], .5)
                    self.assertIsNone(result["economics"]["expected_savings_usd"])

    def test_measured_total_costs_are_reported(self):
        result = forecast(features(), good_history(), config(), now=NOW)
        self.assertEqual(result["economics"]["worker_mean_cost_usd"], 1)
        self.assertEqual(result["economics"]["coordinator_mean_cost_usd"], 5)
        self.assertEqual(result["economics"]["expected_total_cost_usd"], 1)
        self.assertEqual(result["economics"]["expected_savings_usd"], 4)

    def test_economics_does_not_borrow_costs_from_another_family(self):
        result = forecast(features(domain="rust"), good_history(), config(), now=NOW)
        self.assertIsNone(result["economics"]["worker_mean_cost_usd"])
        self.assertIsNone(result["economics"]["coordinator_mean_cost_usd"])

    def test_posterior_reports_uncertainty_beyond_a_high_mean(self):
        result = forecast(features(), good_history(5), config(), now=NOW)
        self.assertGreater(result["posterior"]["mean"], .8)
        self.assertLess(result["posterior"]["lower"], .8)

    def test_missing_costs_remain_unknown_and_sparse_costs_report_support(self):
        rows = good_history()
        for row in rows:
            row["cost_usd"] = None
        result = forecast(features(), rows, config(), now=NOW)
        self.assertIsNone(result["economics"]["expected_savings_usd"])
        rows[-1]["cost_usd"] = 100
        rows[0]["cost_usd"] = .01
        result = forecast(features(), rows, config(), now=NOW)
        self.assertEqual(result["economics"]["worker_cost_cases"], 1)
        self.assertEqual(result["economics"]["coordinator_cost_cases"], 1)
        self.assertEqual(result["economics"]["worker_cost_effective"], 1.)

    def test_unlabelled_costs_are_real_cost_without_quality_failures(self):
        rows = good_history()
        rows += [observation(i + 1000, outcome="infrastructure", cost_usd=100.)
                 for i in range(50)]
        result = forecast(features(), rows, config(), now=NOW)
        self.assertEqual(result["posterior"]["beta"], 1.)
        self.assertEqual(result["posterior"]["local_effective"], 50.)
        self.assertEqual(result["economics"]["worker_mean_cost_usd"], 50.5)

    def test_labelled_total_replaces_earlier_infrastructure_cost(self):
        rows = [observation(1, case_id="same", outcome="infrastructure",
                            cost_usd=3., observed_at=NOW-1),
                observation(2, case_id="same", cost_usd=5.)]
        result = forecast(features(), rows, config(), now=NOW)
        self.assertEqual(result["economics"]["worker_mean_cost_usd"], 5.)
        self.assertEqual(result["economics"]["worker_cost_cases"], 1)

    def test_final_cost_does_not_launder_first_rework_into_success(self):
        rows = [observation(1, case_id="same", outcome="rework", cost_usd=3., observed_at=NOW-1),
                observation(2, case_id="same", cost_usd=8.)]
        result = forecast(features(), rows, config(), now=NOW)
        self.assertEqual(result["posterior"]["alpha"], 1.)
        self.assertGreater(result["posterior"]["beta"], 1.99)
        self.assertEqual(result["economics"]["worker_mean_cost_usd"], 8.)
        self.assertEqual(result["economics"]["worker_cost_cases"], 1)

    def test_subnormal_costs_never_produce_infinite_savings(self):
        rows = good_history()
        for row in rows:
            row["cost_usd"] = 1 if row["action"] == "worker" else 1e-310
        result = forecast(features(), rows, config(), now=NOW)
        json.dumps(result, allow_nan=False)
        self.assertIsNone(result["economics"]["savings_fraction"])

    def test_expensive_worker_reports_negative_measured_savings(self):
        rows = good_history()
        for row in rows:
            row["cost_usd"] = 10 if row["action"] == "worker" else 1
        result = forecast(features(), rows, config(), now=NOW)
        self.assertEqual(result["economics"]["expected_savings_usd"], -9.)
        self.assertEqual(result["economics"]["savings_fraction"], -9.)


class EvaluationTests(unittest.TestCase):
    def test_evaluation_reports_observations_without_simulated_policy(self):
        result = evaluate([observation()], config(), now=NOW)
        self.assertNotIn("coverage", result)
        self.assertNotIn("observed_policy", result)
        self.assertNotIn("policy_cases", result)
        self.assertNotIn("action", result["predictions"][0])

    def test_empty_evaluation_has_no_invented_metrics(self):
        result = evaluate([], config(), now=NOW)
        self.assertEqual(result["evaluated_cases"], 0)
        self.assertIsNone(result["brier_score"])
        self.assertIsNone(result["log_loss"])
        self.assertEqual(result["observed_cases"], 0)

    def test_observed_outcomes_do_not_filter_cold_start_or_expensive_execution(self):
        rows = [observation(1, cost_usd=12.),
                observation(2, outcome="rework", cost_usd=None),
                observation(3, action="coordinator", outcome="rejected", cost_usd=3.)]
        result = evaluate(rows, config(), now=NOW)
        self.assertEqual(result["observed_cases"], 3)
        self.assertEqual(result["evaluated_cases"], 2)
        self.assertEqual(result["observed_outcomes"]["worker"],
                         {"cases": 2, "accepted": 1, "rework": 1, "rejected": 0,
                          "cost_cases": 1, "total_cost_usd": 12., "mean_cost_usd": 12.})
        self.assertEqual(result["observed_outcomes"]["coordinator"],
                         {"cases": 1, "accepted": 0, "rework": 0, "rejected": 1,
                          "cost_cases": 1, "total_cost_usd": 3., "mean_cost_usd": 3.})

    def test_single_outcome_is_scored_before_learning(self):
        result = evaluate([observation()], config(), now=NOW)
        self.assertEqual(result["evaluated_cases"], 1)
        self.assertAlmostEqual(result["brier_score"], .25)
        self.assertAlmostEqual(result["log_loss"], math.log(2))
        self.assertEqual(result["predictions"][0]["probability"], .5)

    def test_same_timestamp_cases_cannot_train_on_each_other(self):
        rows = [observation(i) for i in range(10)]
        result = evaluate(rows, config(), now=NOW)
        self.assertTrue(all(p["probability"] == .5 for p in result["predictions"]))

    def test_repeated_case_never_leaks_into_its_own_forecast(self):
        rows = [observation(1, case_id="same", outcome="rejected", observed_at=NOW-3),
                observation(2, case_id="same", observed_at=NOW-2),
                observation(3, observed_at=NOW-1)]
        result = evaluate(rows, config(), now=NOW)
        self.assertEqual(result["evaluated_cases"], 2)
        self.assertEqual(result["predictions"][0]["probability"], .5)
        self.assertLess(result["predictions"][1]["probability"], .334)

    def test_evaluation_uses_earlier_unlabelled_spending(self):
        rows = good_history()
        for row in rows:
            row["observed_at"] = NOW - 3
        rows += [observation(i + 1000, outcome="infrastructure", cost_usd=100.,
                             observed_at=NOW-2) for i in range(50)]
        rows.append(observation(2000, observed_at=NOW-1))
        result = evaluate(rows, config(), now=NOW)
        self.assertGreater(result["predictions"][-1]["economics"]["worker_mean_cost_usd"], 50.)
        self.assertEqual(result["evaluated_cases"], 51)

    def test_incremental_evaluation_matches_prefix_forecasts_with_expiry_and_revisions(self):
        rng = random.Random(812)
        rows = []
        for i in range(90):
            card = features(domain="javascript" if i % 4 else "python",
                            context_version="v2" if i % 7 == 0 else "v1")
            rows.append(observation(i, case_id=f"case-{i % 39}",
                                    origin="external" if i % 6 == 0 else "local",
                                    source_family=f"family-{i % 3}",
                                    action="coordinator" if i % 5 == 0 else "worker",
                                    outcome=rng.choice(["accepted", "accepted", "rework", "infrastructure"]),
                                    observed_at=NOW-50 + i//3, features=card,
                                    reliability=rng.choice([1., .5, .1]),
                                    cost_usd=rng.choice([None, 1., 2., 20.])))
        cfg = config(half_life_days=.00005, max_evidence_age_days=.0002,
                     min_local_evidence=1)
        result = evaluate(rows, cfg, now=NOW)
        self.assertGreater(result["evaluated_cases"], 10)
        by_id = {row["id"]: row for row in rows}
        for prediction in result["predictions"]:
            row = by_id[prediction["id"]]
            prior = [r for r in rows if r["observed_at"] < row["observed_at"]
                     and r["case_id"] != row["case_id"]]
            expected = forecast(row["features"], prior, cfg,
                                 now=row["observed_at"])
            self.assertAlmostEqual(prediction["probability"], expected["posterior"]["mean"], places=12)
            for name, value in expected["economics"].items():
                if isinstance(value, (int, float)):
                    self.assertAlmostEqual(prediction["economics"][name], value, places=10)
                else:
                    self.assertEqual(prediction["economics"][name], value)

    def test_expiring_large_cost_preserves_small_remaining_costs(self):
        rows = [observation(1000, outcome="infrastructure", cost_usd=1e12, observed_at=NOW-6)]
        rows += [dict(row, observed_at=NOW-5) for row in good_history()]
        rows += [observation(1001, observed_at=NOW-3), observation(1002, observed_at=NOW-1)]
        cfg = config(max_evidence_age_days=4.5/86400, minimum_savings_fraction=.8)
        result = evaluate(rows, cfg, now=NOW)
        expected = forecast(features(), rows[:-1], cfg, now=NOW-1)
        self.assertAlmostEqual(expected["economics"]["worker_mean_cost_usd"], 1., places=12)
        self.assertAlmostEqual(result["predictions"][-1]["economics"]["worker_mean_cost_usd"], 1., places=12)

    def test_incremental_decay_rollovers_and_machine_underflow(self):
        rows = [observation(i, observed_at=NOW-400+i*.25,
                            outcome="rework" if i%10 == 0 else "accepted")
                for i in range(1300)]
        cfg = config(half_life_days=1/86400)
        result = evaluate(rows, cfg, now=NOW)
        for i in (1, 1025, 1299):
            expected = forecast(features(), rows[:i], cfg, now=rows[i]["observed_at"])
            self.assertAlmostEqual(result["predictions"][i]["probability"], expected["posterior"]["mean"], places=12)
        tiny_cfg = config(half_life_days=5e-324)
        tiny = evaluate([observation(i, observed_at=NOW-10+i) for i in range(5)], tiny_cfg, now=NOW)
        self.assertTrue(all(p["probability"] == .5 for p in tiny["predictions"]))

    def test_subnormal_external_weight_can_underflow_after_similarity(self):
        other = features(**{key: "unknown" for key in
                            ("localization", "coupling", "verification", "clarity", "risk")})
        rows = [observation(1, origin="external", features=other,
                            reliability=5e-324, observed_at=NOW-1), observation(2)]
        result = evaluate(rows, config(min_similarity=0), now=NOW)
        self.assertEqual(result["predictions"][0]["probability"], .5)
        json.dumps(result, allow_nan=False)

    def test_quality_correction_updates_target_without_future_training_leakage(self):
        rows = [observation(1, case_id="corrected", observed_at=NOW-5),
                observation(2, observed_at=NOW-4),
                observation(3, case_id="corrected", outcome="rework", observed_at=NOW-3, cost_usd=7.),
                observation(4, observed_at=NOW-3),
                observation(5, observed_at=NOW-2),
                observation(6, case_id="corrected", observed_at=NOW-1, cost_usd=9.)]
        result = evaluate(rows, config(mode="off"), now=NOW)
        predicted = {p["id"]: p for p in result["predictions"]}
        self.assertEqual(result["evaluated_cases"], 4)
        self.assertEqual(predicted["obs-1"]["observed_at"], NOW-5)
        self.assertEqual(predicted["obs-1"]["probability"], .5)
        self.assertEqual(predicted["obs-1"]["outcome"], "rework")
        self.assertEqual(predicted["obs-1"]["outcome_id"], "obs-3")
        self.assertEqual(predicted["obs-1"]["outcome_observed_at"], NOW-3)
        self.assertGreater(predicted["obs-2"]["probability"], .66)
        self.assertGreater(predicted["obs-4"]["probability"], .74)
        self.assertLess(predicted["obs-5"]["probability"], .61)
        lookup = {row["id"]: row for row in rows}
        for prediction in result["predictions"]:
            row = lookup[prediction["id"]]
            prior = [r for r in rows if r["observed_at"] < row["observed_at"]
                     and r["case_id"] != row["case_id"]]
            expected = forecast(row["features"], prior, config(mode="off"),
                                 now=row["observed_at"])
            self.assertAlmostEqual(prediction["probability"], expected["posterior"]["mean"], places=12)
        before = evaluate(rows, config(mode="off"), now=NOW-4)
        self.assertEqual(before["predictions"][0]["outcome"], "accepted")
        self.assertEqual(before["predictions"][0]["probability"], .5)

    def test_observed_outcomes_use_actual_executor_corrected_quality_and_latest_cost(self):
        rows = [observation(1, case_id="same", action="coordinator", observed_at=NOW-3, cost_usd=1.),
                observation(2, case_id="same", action="coordinator", outcome="rejected", observed_at=NOW-2, cost_usd=7.),
                observation(3, case_id="same", action="coordinator", observed_at=NOW-1, cost_usd=9.)]
        result = evaluate(rows, config(mode="off"), now=NOW)
        self.assertEqual(result["observed_cases"], 1)
        self.assertEqual(result["observed_outcomes"]["coordinator"]["cases"], 1)
        self.assertEqual(result["observed_outcomes"]["coordinator"]["accepted"], 0)
        self.assertEqual(result["observed_outcomes"]["coordinator"]["rejected"], 1)
        self.assertEqual(result["observed_outcomes"]["coordinator"]["mean_cost_usd"], 9.)

    def test_expired_acceptance_can_be_corrected_but_expired_failure_stays_failed(self):
        rows = [observation(1, case_id="same", observed_at=NOW-10),
                observation(2, case_id="same", outcome="rejected", observed_at=NOW-5),
                observation(3, observed_at=NOW-4),
                observation(4, case_id="same", observed_at=NOW-1),
                observation(5)]
        cfg = config(max_evidence_age_days=2/86400)
        result = evaluate(rows, cfg, now=NOW)
        predicted = {p["id"]: p for p in result["predictions"]}
        self.assertLess(predicted["obs-3"]["probability"], .334)
        self.assertEqual(predicted["obs-5"]["probability"], .5)
        self.assertEqual(predicted["obs-1"]["outcome"], "rejected")
        self.assertEqual(estimate(features(), rows, cfg, now=NOW)["evidence_ids"], ["obs-5"])

    def test_many_corrections_keep_one_failure_per_case_without_prefix_scans(self):
        rows = []
        for i in range(1200):
            rows += [observation(2*i, case_id=f"case-{i}", observed_at=NOW-3000+2*i),
                     observation(2*i+1, case_id=f"case-{i}", outcome="rework", observed_at=NOW-2999+2*i)]
        with mock.patch.object(routing_estimator, "_similarity", wraps=routing_estimator._similarity) as similarity:
            result = evaluate(rows, config(), now=NOW)
        self.assertEqual(result["evaluated_cases"], 1200)
        self.assertTrue(all(p["outcome"] == "rework" for p in result["predictions"]))
        self.assertLess(result["predictions"][-1]["probability"], .001)
        self.assertLess(similarity.call_count, 1200 * 20)

    def test_chronological_unrelated_family_starts_uncertain(self):
        rows = [observation(i, outcome="rejected", observed_at=NOW-1) for i in range(100)]
        rows.append(observation(1000, features=features(kind="review", domain="rust", operation="review")))
        result = evaluate(rows, config(min_similarity=0), now=NOW)
        self.assertEqual(result["predictions"][-1]["probability"], .5)

    def test_repeated_context_evaluation_does_not_rescan_each_prior_case(self):
        rows = [observation(i, observed_at=NOW-2000+i) for i in range(1200)]
        with mock.patch.object(routing_estimator, "_similarity", wraps=routing_estimator._similarity) as similarity:
            result = evaluate(rows, config(), now=NOW)
        self.assertEqual(result["evaluated_cases"], 1200)
        self.assertEqual(result["predictions"][0]["probability"], .5)
        self.assertGreater(result["predictions"][-1]["probability"], .99)
        # This observes actual similarity work, independent of machine speed.
        # A prefix rescan needs roughly 1.4 million comparisons for this input.
        self.assertLess(similarity.call_count, 1200 * 20)

    def test_future_evidence_is_not_used_in_earlier_prediction(self):
        rows = [observation(1, observed_at=NOW-10)]
        rows += [observation(i+2, origin="external", observed_at=NOW-1) for i in range(20)]
        result = evaluate(rows, config(), now=NOW)
        self.assertEqual(result["evaluated_cases"], 1)
        self.assertEqual(result["predictions"][0]["probability"], .5)
        json.dumps(result, allow_nan=False)


if __name__ == "__main__":
    unittest.main()
