"""Behavioral contracts for automatic bootstrap, adaptive and recovery stages."""
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from codex_deepseek_team import settings
from codex_deepseek_team import routing_bootstrap, routing_recovery
from codex_deepseek_team.routing import RoutingService
from codex_deepseek_team.routing_models import RoutingError, validate_features


class AutomaticStageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.root = base / 'project'
        self.root.mkdir()
        subprocess.run(['git', 'init', '-q', str(self.root)], check=True)
        env = patch.dict(os.environ, DEEPSEEK_TEAM_STATE_DIR=str(base / 'state'),
                         XDG_CONFIG_HOME=str(base / 'config'))
        env.start()
        self.addCleanup(env.stop)
        settings.set_values(self.root / '.deepseek-team.toml', delegation_level='auto', access='full-access')
        self.service = RoutingService(self.root)
        # These contracts cover the explicitly selected evidence admission path.
        self.service.configure({'admission_policy': 'evidence', 'recovery_cooldown_seconds': 3600})
        self.features = validate_features(dict(kind='implementation', domain='python', operation='fix',
            localization='known', coupling='local', verification='tests', clarity='clear', risk='low',
            scope_size='small', context_version='test-v1'))
        self.now, self.serial = time.time(), 0

    def predict(self, key=None, features=None, **kwargs):
        self.serial += 1
        binding = dict(task_id='task', deliverable_id=key or f'd{self.serial}', plan_hash='plan')
        return self.service.predict(features or self.features, binding=binding, recovery=True, **kwargs)

    def observe(self, case, outcome='accepted', action='worker', cost=None, decision=None, age=10, **changes):
        row = dict(id=f'{case}-{outcome}-{action}', case_id=case, origin='local', features=self.features,
                   action=action, outcome=outcome, observed_at=self.now - age, cost_usd=cost)
        if decision:
            row.update(features=decision['features'], decision_id=decision['id'], observed_at=time.time())
        return self.service.observe(dict(row, **changes))

    def seed(self, count=25, worker_cost=None, coordinator_cost=None):
        for n in range(count):
            self.observe(f'worker{n}', cost=worker_cost)
            if coordinator_cost is not None:
                self.observe(f'coordinator{n}', action='coordinator', cost=coordinator_cost)

    def test_fresh_auto_admits_three_without_recovery_stride(self):
        decisions = [self.predict() for _ in range(4)]
        self.assertEqual([d['action'] for d in decisions], ['worker'] * 3 + ['abstain'])
        self.assertTrue(all(d['phase'] == 'bootstrap' for d in decisions))
        self.assertEqual(decisions[-1]['recovery']['reason'], 'capacity')
        self.assertEqual(self.service.recovery_status()['bootstrap_pending_count'], 3)
        self.service.release_recovery(decisions[0]['recovery']['ticket_id'])
        self.assertEqual(self.predict()['action'], 'worker')

    def test_five_successes_continue_until_quality_is_supported(self):
        self.seed(count=5)
        decision = self.predict()
        self.assertLess(decision['posterior']['lower'], .8)
        self.assertEqual((decision['phase'], decision['action']), ('bootstrap', 'worker'))

    def test_supported_quality_unknown_prices_keeps_bounded_learning(self):
        self.seed()
        decision = self.predict()
        self.assertEqual((decision['phase'], decision['action']), ('adaptive', 'worker'))
        self.assertIn('bounded_cost_learning', decision['reason_codes'])
        self.assertIsNone(decision['economics']['expected_savings_usd'])
        self.assertTrue(decision['recovery']['selected'])

    def test_comparable_coordinator_opportunity_every_tenth_case(self):
        for index in range(1, 12):
            decision = self.predict()
            if index == 10:
                self.assertEqual(decision['action'], 'abstain')
                self.assertEqual(decision['recovery']['reason'], 'coordinator_comparison')
            else:
                self.assertEqual(decision['action'], 'worker')
                self.service.start_recovery(decision['id'])
                self.observe(f'actual{index}', decision=decision)
        self.assertEqual(self.predict(features={**self.features, 'domain': 'rust'})['action'], 'worker')

    def test_supported_quality_and_cost_use_adaptive_without_trial(self):
        self.seed(worker_cost=.1, coordinator_cost=1.)
        decision = self.predict()
        self.assertEqual((decision['phase'], decision['action']), ('adaptive', 'worker'))
        self.assertNotIn('recovery', decision)

    def test_known_bad_economics_veto_all_trials(self):
        self.seed(worker_cost=2., coordinator_cost=1.)
        decision = self.predict()
        self.assertEqual(decision['action'], 'coordinator')
        self.assertIn('insufficient_measured_savings', decision['reason_codes'])
        self.assertFalse(decision.get('recovery', {}).get('selected', False))
        self.observe('failure', outcome='rejected', age=7200)
        self.assertEqual(self.predict()['action'], 'coordinator')

    def test_actual_failure_pauses_only_its_family(self):
        self.observe('failure', outcome='rework')
        decision = self.predict()
        self.assertEqual(decision['phase'], 'recovery')
        self.assertNotEqual(decision['action'], 'worker')
        self.assertIn('quality_failure_cooldown', decision['reason_codes'])
        other = self.predict(features={**self.features, 'kind': 'fixture'})
        self.assertEqual((other['phase'], other['action']), ('bootstrap', 'worker'))

    def test_failure_recovery_then_successes_restore_adaptive(self):
        self.observe('failure', outcome='rejected', age=7200)
        first = self.predict()
        self.assertEqual((first['phase'], first['action']), ('recovery', 'worker'))
        self.service.start_recovery(first['id'])
        self.observe('probe', decision=first)
        self.assertNotEqual(self.predict()['action'], 'worker')
        self.seed(count=50, worker_cost=.1, coordinator_cost=1.)
        self.assertEqual(self.predict()['phase'], 'adaptive')

    def test_infrastructure_releases_capacity_without_quality_penalty(self):
        first = self.predict()
        self.service.start_recovery(first['id'])
        self.observe('provider', outcome='infrastructure', decision=first)
        second = self.predict()
        self.assertEqual((second['phase'], second['action']), ('bootstrap', 'worker'))
        self.assertEqual(second['posterior']['matched_local'], 0)

    def test_retry_cannot_erase_failure_or_add_case(self):
        self.observe('same', outcome='rework', age=7200)
        self.observe('same', outcome='accepted', age=7000)
        decision = self.predict()
        self.assertEqual(decision['phase'], 'recovery')
        self.assertEqual(decision['posterior']['matched_local'], 1)
        self.assertLess(decision['posterior']['mean'], .5)

    def test_aged_failure_loses_influence_without_erasing_record(self):
        self.observe('old', outcome='rejected', age=180 * 86400)
        decision = self.predict()
        self.assertEqual((decision['phase'], decision['action']), ('bootstrap', 'worker'))
        self.assertEqual(self.service.observations()[0]['outcome'], 'rejected')

    def test_binding_immutable_across_new_evidence_and_config(self):
        first = self.predict(key='fixed')
        self.service.release_recovery(first['recovery']['ticket_id'])
        self.seed(worker_cost=.1, coordinator_cost=1.)
        self.service.configure({'confidence': .9})
        self.assertEqual(self.predict(key='fixed'), first)
        with self.assertRaises(RoutingError):
            self.service.start_recovery(first['id'])

    def test_coordinator_feedback_cannot_close_worker_trial(self):
        decision = self.predict()
        self.service.start_recovery(decision['id'])
        with self.assertRaises(RoutingError):
            self.observe('wrong-action', action='coordinator', decision=decision)
        self.assertEqual(self.service.recovery_status()['running_count'], 1)
        self.assertEqual(self.service.observations(), [])

    def test_failure_prevents_starting_already_queued_trial(self):
        decision = self.predict()
        self.observe('failure', outcome='rejected')
        with self.assertRaisesRegex(RoutingError, 'cooldown'):
            self.service.start_recovery(decision['id'])
        self.assertEqual(self.service.recovery_status()['running_count'], 0)

    def test_denied_binding_cannot_reroll_when_capacity_frees(self):
        admitted = [self.predict() for _ in range(3)]
        denied = self.predict(key='denied')
        for decision in admitted:
            self.service.release_recovery(decision['recovery']['ticket_id'])
        self.seed(worker_cost=.1, coordinator_cost=1.)
        self.assertEqual(self.predict(key='denied'), denied)

    def test_bootstrap_and_recovery_share_capacity_not_quota(self):
        self.observe('bad-python', outcome='rejected', age=7200)
        self.assertEqual(self.predict()['phase'], 'recovery')
        boot = [self.predict(features={**self.features, 'domain': domain}) for domain in ('rust', 'go')]
        self.assertTrue(all(d['action'] == 'worker' for d in boot))
        self.assertNotEqual(self.predict(features={**self.features, 'domain': 'java'})['action'], 'worker')
        self.assertEqual(self.service.recovery_status()['pending_count'], 3)

    def test_preview_unsafe_access_manual_off_do_not_admit(self):
        self.assertNotEqual(self.service.predict(self.features, record=False)['action'], 'worker')
        for changes in ({'risk': 'high'}, {'risk': 'unknown'}, {'scope_size': 'large'},
                        {'verification': 'manual'}, {'kind': 'security'}):
            self.assertNotEqual(self.predict(features={**self.features, **changes})['action'], 'worker')
        settings.set_values(self.root / '.deepseek-team.toml', access='read-only')
        self.assertNotEqual(self.predict()['action'], 'worker')
        settings.set_values(self.root / '.deepseek-team.toml', access='full-access', delegation_level=75)
        self.assertNotEqual(self.predict()['action'], 'worker')
        settings.set_values(self.root / '.deepseek-team.toml', delegation_level='auto')
        self.service.configure({'mode': 'off'})
        self.assertNotEqual(self.predict()['action'], 'worker')
        self.assertEqual(self.service.recovery_status()['selected_count'], 0)

    def test_concurrent_admissions_across_connections_share_three_slots(self):
        barrier, decisions, errors = threading.Barrier(8), [], []
        def submit(index):
            try:
                service = RoutingService(self.root)
                barrier.wait(timeout=10)
                decisions.append(service.predict(self.features, recovery=True,
                    binding=dict(task_id='concurrent', deliverable_id=f'd{index}', plan_hash='p')))
            except Exception as error:
                errors.append(error)
        threads = [threading.Thread(target=submit, args=(n,)) for n in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertFalse(errors, errors)
        self.assertEqual(sum(d['action'] == 'worker' for d in decisions), 3)
        self.assertEqual(self.service.recovery_status()['pending_count'], 3)

    def test_failed_transaction_rolls_back_trial_and_bound_decision(self):
        binding = dict(task_id='rollback', deliverable_id='d', plan_hash='p')
        with self.assertRaisesRegex(RuntimeError, 'interrupted'):
            with self.service.batch() as service:
                service.predict(self.features, binding=binding, recovery=True)
                raise RuntimeError('interrupted')
        self.assertEqual(self.service.status()['decisions'], 0)
        self.assertEqual(self.service.recovery_status()['pending_count'], 0)
        self.assertEqual(self.service.predict(self.features, binding=binding, recovery=True)['action'], 'worker')

    def test_read_only_can_bootstrap_review_without_widening_access(self):
        settings.set_values(self.root / '.deepseek-team.toml', access='read-only')
        decision = self.predict(features={**self.features, 'kind': 'review', 'operation': 'review'})
        self.assertEqual((decision['phase'], decision['action'], decision['access']),
                         ('bootstrap', 'worker', 'read-only'))

    def test_turning_recovery_off_does_not_disable_initial_learning(self):
        self.service.configure({'recovery_rate': 0})
        self.assertEqual(self.predict()['action'], 'worker')
        self.observe('failure', outcome='rejected', age=7200)
        self.assertNotEqual(self.predict()['action'], 'worker')

    def test_current_revocation_narrows_frozen_worker_decision(self):
        self.seed(worker_cost=.1, coordinator_cost=1.)
        decision = self.predict(key='same')
        self.assertEqual(decision['action'], 'worker')
        self.service.configure({'mode': 'off'})
        self.assertEqual(self.predict(key='same')['action'], 'coordinator')

    def test_feedback_after_adaptive_work_also_starts_family_cooldown(self):
        self.seed(worker_cost=.1, coordinator_cost=1.)
        decision = self.predict()
        self.assertNotIn('recovery', decision)
        self.observe('adaptive-failure', outcome='rejected', decision=decision)
        after = self.predict()
        self.assertEqual(after['phase'], 'recovery')
        self.assertNotEqual(after['action'], 'worker')
        self.assertIn('quality_failure_cooldown', after['reason_codes'])

    def test_upgrade_hydrates_recent_legacy_failure_without_refreshing_timestamp(self):
        self.observe('legacy-failure', outcome='rejected')
        with self.service.store.transaction() as db:
            for name in routing_bootstrap.SCHEMA:
                db.execute(f'DROP TABLE {name}')
            db.execute('DELETE FROM routing_recovery_cooldowns')
        decision = self.predict()
        self.assertEqual(decision['phase'], 'recovery')
        self.assertNotEqual(decision['action'], 'worker')
        with self.service.store.transaction() as db:
            failure_at, until = db.execute('SELECT failure_at, cooldown_until FROM routing_recovery_cooldowns').fetchone()
        self.assertEqual(failure_at, self.now - 10)
        self.assertEqual(until, self.now - 10 + 3600)
        with patch('codex_deepseek_team.routing.time.time', return_value=self.now + 3601):
            self.assertEqual(self.predict()['action'], 'worker')


if __name__ == '__main__':
    unittest.main()
