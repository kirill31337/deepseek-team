"""Immediate delegation separates suitability from process capacity."""
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from codex_deepseek_team import coordination, settings
from codex_deepseek_team.routing import RoutingService
from codex_deepseek_team.routing_models import RoutingError, validate_features


class ImmediateDelegationTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        self.root = base / 'project'
        self.root.mkdir()
        subprocess.run(['git', 'init', '-q', str(self.root)], check=True)
        subprocess.run(['git', '-C', str(self.root), '-c', 'user.name=Tests',
                        '-c', 'user.email=tests@example.invalid', 'commit', '-qm',
                        'initial', '--allow-empty'], check=True)
        env = patch.dict(os.environ, DEEPSEEK_TEAM_STATE_DIR=str(base / 'state'),
                         XDG_CONFIG_HOME=str(base / 'config'))
        env.start()
        self.addCleanup(env.stop)
        settings.set_values(self.root / settings.PROJECT_FILE, access='full-access')
        self.service = RoutingService(self.root)
        self.features = validate_features(dict(kind='implementation', domain='python',
            operation='extend', localization='known', coupling='local', verification='tests',
            clarity='clear', risk='low', scope_size='small', context_version='immediate-v1'))

    def predict(self, key='one', **changes):
        return self.service.predict(dict(self.features, **changes), recovery=True,
            binding=dict(task_id='task', deliverable_id=key, plan_hash='plan'))

    def observe(self, case, outcome='rework', *, ago=0, suffix='', **changes):
        return self.service.observe(dict(id=case + outcome + suffix, case_id=case,
            origin='local', action='worker', features=dict(self.features, **changes),
            outcome=outcome, observed_at=time.time()-ago))

    def test_first_twelve_tasks_remain_delegated_without_slots_or_holdout(self):
        decisions = [self.predict(str(n)) for n in range(12)]
        self.assertEqual([d['action'] for d in decisions], ['worker'] * 12)
        self.assertTrue(all('recovery' not in d for d in decisions))
        self.assertTrue(all(d['economics']['expected_savings_usd'] is None for d in decisions))
        self.assertEqual(self.service.recovery_status()['pending_count'], 0)

    def test_bounded_component_work_does_not_need_prior_successes(self):
        decision = self.predict(scope_size='medium', coupling='component',
                                localization='partial', risk='medium')
        self.assertEqual(decision['action'], 'worker')
        self.assertEqual(decision['posterior']['matched_local'], 0)

    def test_unbounded_unverifiable_and_protected_work_stays_local(self):
        for n, changes in enumerate((dict(risk='high'), dict(risk='unknown'),
                dict(scope_size='large'), dict(coupling='cross-component'),
                dict(localization='unknown'), dict(clarity='partial'),
                dict(verification='none'), dict(verification='manual'), dict(kind='security'))):
            with self.subTest(changes=changes):
                self.assertNotEqual(self.predict(str(n), **changes)['action'], 'worker')

    def test_documentation_with_explicit_manual_acceptance_can_be_planned(self):
        task = coordination.open_task(self.root, session_id='manual', turn_id='1',
            prompt='Update docs', policy=settings.resolve(self.root))
        item = dict(id='docs', kind='documentation', scope=['README.md'], executor='auto',
            acceptance=['Examples match the supported CLI and terminology'], checks=[], dependencies=[],
            features=dict(self.features, kind='documentation', domain='documentation',
                          operation='document', verification='manual'))
        plan = coordination.plan_task(self.root, task['id'],
            dict(classification='substantial', deliverables=[item]))
        self.assertEqual(plan['deliverables'][0]['executor'], 'worker')
        self.assertEqual(len(plan['assignments']), 1)

    def test_write_assignment_without_declared_checks_is_not_admitted(self):
        task = coordination.open_task(self.root, session_id='no-check', turn_id='1',
            prompt='Change code', policy=settings.resolve(self.root))
        item = dict(id='code', kind='implementation', scope=['example.py'], executor='auto',
            acceptance=['Works'], checks=[], dependencies=[], features=self.features)
        plan = coordination.plan_task(self.root, task['id'],
            dict(classification='substantial', deliverables=[item]))
        self.assertEqual(plan['deliverables'][0]['executor'], 'coordinator')

    def test_one_rework_does_not_pause_and_three_distinct_cases_do(self):
        self.observe('a')
        self.assertEqual(self.predict('after-one')['action'], 'worker')
        self.observe('a', suffix='again')
        self.observe('b')
        self.assertEqual(self.predict('after-two')['action'], 'worker')
        self.observe('c')
        self.assertNotEqual(self.predict('after-three')['action'], 'worker')
        self.assertEqual(len(self.service.observations()), 4)

    def test_rejection_pauses_only_matching_family_and_expires_without_stride(self):
        self.observe('rejected', 'rejected')
        self.assertNotEqual(self.predict('blocked')['action'], 'worker')
        self.assertEqual(self.predict('other', domain='rust')['action'], 'worker')
        with patch('codex_deepseek_team.routing.time.time', return_value=time.time()+301):
            self.assertEqual(self.predict('recovered')['action'], 'worker')
            self.assertEqual(self.predict('recovered-again')['action'], 'worker')

    def test_infrastructure_is_neutral_and_acceptance_does_not_erase_failure(self):
        self.observe('infra', 'infrastructure')
        self.assertEqual(self.predict()['action'], 'worker')
        self.observe('bad', 'rejected')
        self.observe('bad', 'accepted')
        self.assertNotEqual(self.predict('after-retry')['action'], 'worker')

    def test_queued_start_rechecks_rejection_mode_and_access(self):
        decision = self.predict()
        self.observe('bad', 'rejected')
        with self.assertRaises(RoutingError):
            self.service.start_recovery(decision['id'])
        self.service.configure({'mode': 'off'})
        with self.assertRaises(RoutingError):
            self.service.start_recovery(decision['id'])

    def test_explicit_read_only_never_admits_writing(self):
        settings.set_values(self.root / settings.PROJECT_FILE, access='read-only')
        self.assertNotEqual(self.predict()['action'], 'worker')
        self.assertEqual(self.predict('review', kind='review', operation='review',
                                      verification='manual')['action'], 'worker')

    def test_evidence_admission_remains_an_explicit_option(self):
        self.service.configure({'admission_policy': 'evidence'})
        self.assertEqual([self.predict(str(n))['action'] for n in range(4)],
                         ['worker', 'worker', 'worker', 'abstain'])

    def test_queue_validation_rejects_released_or_expired_evidence_tickets(self):
        self.service.configure({'admission_policy': 'evidence'})
        decision = self.predict('ticket')
        self.service.start_recovery(decision['id'], validate_only=True)
        self.assertEqual(self.service.recovery_status()['pending_count'], 1)
        with patch('codex_deepseek_team.routing.time.time', return_value=time.time()+3601):
            with self.assertRaisesRegex(RoutingError, 'ticket'):
                self.service.start_recovery(decision['id'], validate_only=True)
        self.service.release_recovery(decision['recovery']['ticket_id'])
        with self.assertRaisesRegex(RoutingError, 'ticket'):
            self.service.start_recovery(decision['id'], validate_only=True)

    def test_existing_trial_uses_current_immediate_failure_policy(self):
        self.service.configure({'admission_policy': 'evidence'})
        decision = self.predict('old-trial')
        self.service.start_recovery(decision['id'])
        self.service.configure({'admission_policy': 'immediate'})
        self.service.observe(dict(id='old-trial-rework', case_id='old-trial',
            origin='local', action='worker', features=self.features, outcome='rework',
            observed_at=time.time(), decision_id=decision['id']))
        self.assertEqual(self.service.recovery_status()['running_count'], 0)
        self.assertEqual(self.predict('new-immediate')['action'], 'worker')
        self.assertEqual(self.service.observations()[0]['outcome'], 'rework')

    def test_queue_visibility_preserves_assignment_and_routing_identity(self):
        task = coordination.open_task(self.root, session_id='queued', turn_id='1',
            prompt='Change code', policy=settings.resolve(self.root))
        item = dict(id='code', kind='implementation', scope=['example.py'], executor='auto',
            acceptance=['Check passes'], checks=['python3 -V'], dependencies=[], features=self.features)
        plan = coordination.plan_task(self.root, task['id'],
            dict(classification='substantial', deliverables=[item]))
        aid = plan['assignments'][0]['id']
        decision = plan['deliverables'][0]['routing']['decision_id']
        coordination.assignment_queue_state(self.root, task['id'], aid, 'waiting')
        queued = coordination.load_task(self.root, task['id'])
        self.assertEqual(queued['assignments'][0]['status'], 'planned')
        self.assertEqual(queued['assignments'][0]['queue_state'], 'waiting')
        self.assertEqual(queued['deliverables'][0]['routing']['decision_id'], decision)
        coordination.assignment_queue_state(self.root, task['id'], aid, 'ready')
        ready = coordination.load_task(self.root, task['id'])
        self.assertEqual(ready['assignments'][0]['queue_state'], 'ready')
        self.assertEqual(len(ready['assignments']), 1)
        coordination.assignment_started(self.root, task['id'], aid, 'workspace',
                                        'codex', [], effort='medium')
        done = coordination.assignment_finished(self.root, task['id'], aid,
            'succeeded', 'verified', ['example.py'], [{'command': 'python3 -V', 'exit_code': 0}])
        self.assertEqual(done['assignments'][0]['queue_state'], 'finished')


if __name__ == '__main__':
    unittest.main()
