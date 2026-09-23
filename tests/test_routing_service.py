import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from codex_deepseek_team import settings
from codex_deepseek_team.routing import RoutingService
from codex_deepseek_team.routing_models import RoutingError, validate_features, validate_observation


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'project'
        self.root.mkdir()
        subprocess.run(['git', 'init', '-q', str(self.root)], check=True)
        self.env = patch.dict(os.environ, DEEPSEEK_TEAM_STATE_DIR=str(Path(self.tmp.name) / 'state'),
                              XDG_CONFIG_HOME=str(Path(self.tmp.name) / 'config'))
        self.env.start()
        self.addCleanup(self.env.stop)
        self.service = RoutingService(self.root)
        self.features = validate_features(dict(kind='implementation', domain='python', operation='fix',
            localization='known', coupling='local', verification='tests', clarity='clear', risk='low', scope_size='small'))

    def observation(self, **changes):
        return dict(dict(id='o1', case_id='c1', origin='local', features=self.features,
                         action='worker', outcome='accepted', observed_at=100.), **changes)

    def worker_decision(self, features=None):
        """Record an eligible, full-access worker decision bound to task-123."""
        settings.set_values(self.root / settings.PROJECT_FILE,
                            delegation_level=75, access='full-access')
        binding = {'task_id': 'task-123', 'deliverable_id': 'implementation', 'plan_hash': 'a' * 64}
        card = self.features if features is None else features
        decision = self.service.predict(card, access='full-access', binding=binding,
                                        verification_ready=True)
        self.assertEqual(decision['action'], 'worker')
        return binding, decision

    def test_partial_feature_card_omitting_kind_matches_worker_decision(self):
        binding, decision = self.worker_decision()
        self.assertTrue(self.service.validate_plan_decision(
            decision['id'], self.features, binding, executor='worker'))
        partial = {key: value for key, value in self.features.items() if key != 'kind'}
        self.assertNotIn('kind', partial)
        self.assertTrue(self.service.validate_plan_decision(
            decision['id'], partial, binding, executor='worker'))

    def test_validate_plan_decision_rejects_invalid_or_unknown_cards(self):
        binding, decision = self.worker_decision()
        partial = {key: value for key, value in self.features.items() if key != 'kind'}
        self.assertFalse(self.service.validate_plan_decision(
            decision['id'], {**partial, 'kind': 'bogus'}, binding, executor='worker'))
        self.assertFalse(self.service.validate_plan_decision(
            decision['id'], {**partial, 'prompt': 'secret'}, binding, executor='worker'))
        self.assertFalse(self.service.validate_plan_decision(
            decision['id'], partial, {**binding, 'plan_hash': 'b' * 64}, executor='worker'))
        self.assertFalse(self.service.validate_plan_decision(
            decision['id'], partial, binding, executor='coordinator'))
        self.assertFalse(self.service.validate_plan_decision(
            'decision-' + '0' * 32, partial, binding, executor='worker'))

    def test_config_persists_and_rejects_unknowns(self):
        self.assertEqual(self.service.config()['mode'], 'auto')
        self.service.configure({'mode': 'shadow'})
        self.assertEqual(RoutingService(self.root).config()['mode'], 'shadow')
        with self.assertRaises(RoutingError):
            self.service.configure({'allow_unsafe_workers': True})

    def test_observation_immutable_and_external_cannot_be_forged(self):
        observed = self.service.observe(self.observation())
        self.assertEqual(self.service.observe(self.observation()), observed)
        self.assertEqual(len(self.service.observations()), 1)
        with self.assertRaises(RoutingError):
            self.service.observe(self.observation(outcome='rejected'))
        with self.assertRaises(RoutingError):
            self.service.observe(self.observation(id='other', origin='external', source_id='source', source_family='family'))

    def test_legacy_medium_case_rejects_backdated_high_event(self):
        old = validate_observation(self.observation(observed_at=200.))
        old['features'] = dict(old['features'], effort='medium')
        # Insert pre-upgrade data directly: normal validation now canonicalizes it.
        with self.service.store.transaction() as db:
            self.service.store.put(db, 'observations', old['id'], old)
        with self.assertRaisesRegex(RoutingError, 'backdated'):
            self.service.observe(self.observation(id='backdated', observed_at=100.))
        self.service.observe(self.observation(id='later', observed_at=300.))

    def test_decision_captures_original_features_and_config(self):
        result = self.service.predict(self.features)
        self.service.configure({'confidence': .9})
        saved = self.service.decision(result['id'])
        self.assertEqual(saved['config']['confidence'], .95)
        self.assertEqual(saved['features'], self.features)
        self.assertIn(saved['action'], ('coordinator', 'abstain'))
        with self.assertRaises(RoutingError):
            self.service.observe(self.observation(decision_id=result['id'], features={**self.features, 'domain': 'rust'}))

    def test_explicit_access_cannot_widen_policy(self):
        result = self.service.predict(self.features, access='full-access')
        self.assertEqual(result['access'], 'read-only')
        self.assertEqual(result['action'], 'coordinator')

    def test_import_idempotent_and_conflict_rolls_back(self):
        record = self.observation()
        record.pop('origin')
        record['source_family'] = 'public-suite'
        payload = {'schema_version': 1, 'format': 'deepseek-team-evidence',
                   'source_family': 'public-suite', 'observations': [record]}
        raw = json.dumps(payload).encode()
        args = (raw, 'source', 'https://example.com/snapshot.json', hashlib.sha256(raw).hexdigest())
        self.service.import_snapshot(*args)
        self.service.import_snapshot(*args)
        self.assertEqual(len(self.service.observations()), 1)
        payload['observations'] = [{**record, 'id': 'new'}, {**record, 'outcome': 'rejected'}]
        raw = json.dumps(payload).encode()
        with self.assertRaises(RoutingError):
            self.service.import_snapshot(raw, 'source', args[2], hashlib.sha256(raw).hexdigest())
        self.assertEqual(len(self.service.observations()), 1)

    def test_bound_plan_decisions_cannot_be_reused_for_changed_scope(self):
        self.service.configure({'mode': 'auto'})
        binding = {'task_id': 'task-123', 'deliverable_id': 'test', 'plan_hash': 'a' * 64}
        decision = self.service.predict(self.features, binding=binding)
        self.assertTrue(self.service.validate_plan_decision(decision['id'], self.features, binding))
        self.assertFalse(self.service.validate_plan_decision(decision['id'], self.features,
                         {**binding, 'plan_hash': 'b' * 64}))
        self.assertFalse(self.service.validate_plan_decision(decision['id'], {**self.features, 'kind': 'review'}, binding))
        other = self.service.predict(self.features)
        self.assertFalse(self.service.validate_plan_decision(other['id'], self.features, binding))

    def test_backdated_outcomes_cannot_replace_first_failure(self):
        self.service.observe(self.observation(outcome='rework', observed_at=200.))
        with self.assertRaises(RoutingError):
            self.service.observe(self.observation(id='backdated', observed_at=100.))
        self.service.observe(self.observation(id='later', observed_at=300., cost_usd=.5))
        self.assertEqual(len(self.service.observations()), 2)

    def test_decision_is_bound_to_one_actual_case(self):
        decision = self.service.predict(self.features)
        row = self.observation(decision_id=decision['id'], observed_at=time.time())
        self.service.observe(row)
        with self.assertRaises(RoutingError):
            self.service.observe(dict(row, id='different', case_id='different-case'))

    def test_decisions_persist_bounded_evidence_references(self):
        prediction = dict(posterior={'evidence_ids': [str(n) for n in range(1000)]}, economics={})
        with patch('codex_deepseek_team.routing.routing_estimator.forecast', return_value=prediction):
            result = self.service.predict(self.features)
        saved = self.service.decision(result['id'])
        self.assertEqual(len(saved['posterior']['evidence_ids']), 128)
        self.assertEqual(saved['posterior']['evidence_count'], 1000)
        self.assertEqual(len(saved['posterior']['evidence_digest']), 64)


if __name__ == '__main__':
    unittest.main()
