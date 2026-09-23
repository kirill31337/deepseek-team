"""Behavioral contracts for the single immediate admission path."""
import sqlite3
import unittest

from codex_deepseek_team import routing_admission as admission
from codex_deepseek_team.routing_models import RoutingError, canonical, validate_features


def features(**overrides):
    card = dict(kind='implementation', domain='python', operation='fix',
                localization='known', coupling='local', verification='tests',
                clarity='clear', risk='low', scope_size='small', runtime='codex',
                effort='medium', model='deepseek-flash', context_version='v1')
    card.update(overrides)
    return card


def binding(task='t', deliverable='d', plan='p'):
    return {'task_id': task, 'deliverable_id': deliverable, 'plan_hash': plan}


def decision(decision_id='decision-1', card=None, bind=None):
    return {'id': decision_id, 'binding': bind or binding(), 'features': card or features(),
            'created_at': 0.0, 'action': 'worker'}


def obs(case, outcome='rework', card=None, at=0.0, action='worker', origin='local', ident=None):
    return {'id': ident or f'{case}:{outcome}:{at}', 'case_id': case, 'origin': origin,
            'action': action, 'features': card or features(), 'outcome': outcome, 'observed_at': at}


class AdapterBase(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        self.conn.isolation_level = None
        self.addCleanup(self.conn.close)
        admission.initialize(self.conn)

    def decisions_fixture(self):
        self.conn.execute('CREATE TABLE decisions (id TEXT PRIMARY KEY, value TEXT NOT NULL)')


class IneligibleReasonTests(unittest.TestCase):
    def test_accepts_medium_component_full_access_with_tests(self):
        card = features(risk='medium', scope_size='medium', coupling='component',
                        localization='partial')
        self.assertIsNone(admission.ineligible_reason(card, 'full-access'))

    def test_protected_kind_is_rejected_even_with_full_access(self):
        for kind in ('architecture', 'security', 'commit_push', 'secret_signing'):
            self.assertEqual(admission.ineligible_reason(features(kind=kind), 'full-access'),
                             'protected_kind')

    def test_writes_require_full_access(self):
        self.assertEqual(admission.ineligible_reason(features(), 'read-only'),
                         'write_requires_full_access')
        self.assertEqual(admission.ineligible_reason(features(kind='documentation'), 'read-only'),
                         'write_requires_full_access')
        self.assertIsNone(admission.ineligible_reason(features(kind='review'), 'read-only'))

    def test_manual_verification_only_for_read_or_documentation(self):
        manual_review = features(kind='review', verification='manual')
        self.assertIsNone(admission.ineligible_reason(manual_review, 'read-only'))
        self.assertIsNone(admission.ineligible_reason(features(kind='documentation', verification='manual'),
                                                      'full-access'))
        self.assertEqual(admission.ineligible_reason(features(verification='manual'), 'full-access'),
                         'ineligible_verification')

    def test_unknown_and_weak_fields_are_rejected(self):
        cases = [
            (features(risk='high'), 'ineligible_risk'),
            (features(scope_size='large'), 'ineligible_scope_size'),
            (features(coupling='cross-component'), 'ineligible_coupling'),
            (features(localization='unknown'), 'ineligible_localization'),
            (features(clarity='partial'), 'ineligible_clarity'),
            (features(verification='none'), 'ineligible_verification'),
            (features(domain='unknown'), 'ineligible_domain'),
            (features(operation='unknown'), 'ineligible_operation'),
            (features(context_version='unknown'), 'ineligible_context_version'),
            (features(model='other-model'), 'ineligible_model'),
        ]
        for card, reason in cases:
            with self.subTest(reason=reason):
                self.assertEqual(admission.ineligible_reason(card, 'full-access'), reason)


class ManualAcceptanceTests(unittest.TestCase):
    def test_review_with_manual_criterion_is_accepted(self):
        item = {'features': features(kind='review', verification='manual'), 'acceptance': ['diff reviewed']}
        self.assertTrue(admission.manual_acceptance(item))

    def test_documentation_with_registered_acceptance_list(self):
        item = {'features': features(kind='documentation', verification='manual'),
                'acceptance': ['docs match behavior']}
        self.assertTrue(admission.manual_acceptance(item))

    def test_blank_or_missing_criterion_is_rejected(self):
        card = features(kind='review', verification='manual')
        self.assertFalse(admission.manual_acceptance({'features': card, 'acceptance': '   '}))
        self.assertFalse(admission.manual_acceptance({'features': card}))
        self.assertFalse(admission.manual_acceptance({'features': card, 'acceptance': ['', ' ']}))
        self.assertFalse(admission.manual_acceptance({'features': card, 'acceptance_criterion': 'x'}))

    def test_wrong_kind_or_verification_is_rejected(self):
        self.assertFalse(admission.manual_acceptance(
            {'features': features(verification='manual'), 'acceptance': ['ok']}))
        self.assertFalse(admission.manual_acceptance(
            {'features': features(kind='review', verification='tests'), 'acceptance': ['ok']}))


class EconomicVetoTests(unittest.TestCase):
    def test_missing_costs_never_veto(self):
        self.assertFalse(admission.economic_veto({'economics': {}}, {}))
        self.assertFalse(admission.economic_veto(
            {'economics': {'worker_cost_effective': 9.0}}, {}))
        self.assertFalse(admission.economic_veto({'economics': None}, {}))

    def test_known_costs_with_unknown_savings_veto(self):
        evidence = {'worker_cost_effective': 5.0, 'coordinator_cost_effective': 5.0,
                    'savings_fraction': None}
        self.assertTrue(admission.economic_veto({'economics': evidence},
                                                {'min_local_evidence': 5}))

    def test_known_costs_with_sufficient_savings_do_not_veto(self):
        evidence = {'worker_cost_effective': 5.0, 'coordinator_cost_effective': 5.0,
                    'savings_fraction': 0.5}
        self.assertFalse(admission.economic_veto({'economics': evidence},
                                                 {'min_local_evidence': 5, 'minimum_savings_fraction': 0.1}))

    def test_insufficient_cost_evidence_does_not_veto(self):
        evidence = {'worker_cost_effective': 1.0, 'coordinator_cost_effective': 9.0,
                    'savings_fraction': 0.0}
        self.assertFalse(admission.economic_veto({'economics': evidence},
                                                 {'min_local_evidence': 5}))


class BindingTests(AdapterBase):
    def setUp(self):
        super().setUp()
        self.decisions_fixture()
        self.card = features()

    def store(self, record):
        self.conn.execute('INSERT INTO decisions VALUES (?, ?)', (record['id'], canonical(record)))

    def test_unbound_binding_has_no_decision(self):
        self.assertIsNone(admission.bound_decision(self.conn, binding(), self.card))

    def test_bind_and_lookup_round_trip(self):
        record = decision(card=self.card)
        self.store(record)
        admission.bind_decision(self.conn, record)
        self.assertEqual(admission.bound_decision(self.conn, binding(), self.card), record)

    def test_initialize_creates_only_current_tables_and_no_backfill(self):
        self.assertEqual(set(admission.SCHEMA), {'routing_decision_bindings', 'routing_admission_cooldowns'})
        self.store(decision(card=self.card))
        admission.initialize(self.conn)
        admission.initialize(self.conn)
        tables = {r[0] for r in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertLessEqual({'routing_decision_bindings', 'routing_admission_cooldowns'}, tables)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM routing_decision_bindings').fetchone()[0], 0)

    def test_rebinding_same_decision_is_idempotent(self):
        record = decision(card=self.card)
        self.store(record)
        admission.bind_decision(self.conn, record)
        admission.bind_decision(self.conn, record)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM routing_decision_bindings').fetchone()[0], 1)

    def test_conflicting_decision_for_same_binding_is_rejected(self):
        first = decision('decision-1', card=self.card)
        self.store(first)
        admission.bind_decision(self.conn, first)
        other = decision('decision-2', card=self.card)
        self.store(other)
        with self.assertRaises(RoutingError):
            admission.bind_decision(self.conn, other)

    def test_missing_decision_row_raises_78(self):
        record = decision(card=self.card)
        admission.bind_decision(self.conn, record)
        with self.assertRaises(RoutingError) as caught:
            admission.bound_decision(self.conn, binding(), self.card)
        self.assertEqual(caught.exception.code, 78)

    def test_changed_features_are_rejected(self):
        record = decision(card=self.card)
        self.store(record)
        admission.bind_decision(self.conn, record)
        with self.assertRaises(RoutingError):
            admission.bound_decision(self.conn, binding(), features(operation='extend'))

    def test_different_binding_is_unbound(self):
        record = decision(card=self.card)
        self.store(record)
        admission.bind_decision(self.conn, record)
        self.assertIsNone(admission.bound_decision(self.conn, binding(plan='other'), self.card))


class RecordOutcomeTests(AdapterBase):
    config = {'failure_cooldown_seconds': 300.0}

    def test_rejection_pauses_immediately_and_expires(self):
        row = obs('c1', 'rejected', at=1000.0)
        self.assertTrue(admission.record_outcome(self.conn, row, [row], self.config))
        self.assertTrue(admission.cooling(self.conn, features(), now=1000.0))
        self.assertFalse(admission.cooling(self.conn, features(), now=1300.0))
        entry = admission.status(self.conn, now=1000.0)['active_cooldowns'][0]
        self.assertEqual((entry['failure_at'], entry['cooldown_until']), (1000.0, 1300.0))

    def test_one_rework_does_not_pause_but_third_distinct_case_does(self):
        rows = [obs('c1', 'rework', at=100.0), obs('c2', 'rework', at=150.0)]
        self.assertFalse(admission.record_outcome(self.conn, rows[1], rows, self.config))
        self.assertFalse(admission.cooling(self.conn, features(), now=150.0))
        rows.append(obs('c3', 'rework', at=200.0))
        self.assertTrue(admission.record_outcome(self.conn, rows[2], rows, self.config))
        self.assertTrue(admission.cooling(self.conn, features(), now=200.0))

    def test_rework_window_excludes_old_failures(self):
        rows = [obs('c1', 'rework', at=0.0), obs('c2', 'rework', at=1.0), obs('c3', 'rework', at=1000.0)]
        self.assertFalse(admission.record_outcome(self.conn, rows[2], rows, self.config))

    def test_duplicate_case_counts_once(self):
        rows = [obs('c1', 'rework', at=100.0, ident='a'), obs('c1', 'rework', at=110.0, ident='b'),
                obs('c1', 'rework', at=120.0, ident='c')]
        self.assertFalse(admission.record_outcome(self.conn, rows[2], rows, self.config))
        self.assertFalse(admission.cooling(self.conn, features(), now=120.0))

    def test_other_families_do_not_combine(self):
        rows = [obs('c1', 'rework', at=100.0), obs('c2', 'rework', at=110.0),
                obs('c9', 'rework', at=120.0, card=features(kind='test'))]
        self.assertFalse(admission.record_outcome(self.conn, rows[1], rows, self.config))

    def test_neutral_outcomes_never_pause(self):
        for outcome in ('infrastructure', 'cancelled', 'unknown', 'accepted'):
            with self.subTest(outcome=outcome):
                row = obs('n1', outcome, at=10.0)
                self.assertFalse(admission.record_outcome(self.conn, row, [row], self.config))
        self.assertFalse(admission.cooling(self.conn, features(), now=10.0))

    def test_acceptance_does_not_clear_an_existing_pause(self):
        rejected = obs('c1', 'rejected', at=1000.0)
        self.assertTrue(admission.record_outcome(self.conn, rejected, [rejected], self.config))
        accepted = obs('c2', 'accepted', at=1100.0)
        self.assertFalse(admission.record_outcome(self.conn, accepted, [accepted, rejected], self.config))
        self.assertTrue(admission.cooling(self.conn, features(), now=1100.0))

    def test_external_or_coordinator_rows_are_ignored(self):
        external = obs('c1', 'rejected', at=1000.0, origin='external')
        self.assertFalse(admission.record_outcome(self.conn, external, [external], self.config))
        coordinator = obs('c1', 'rejected', at=1000.0, action='coordinator')
        self.assertFalse(admission.record_outcome(self.conn, coordinator, [coordinator], self.config))

    def test_monotonic_upsert_cannot_shorten_existing_pause(self):
        later = obs('c1', 'rejected', at=500.0)
        admission.record_outcome(self.conn, later, [later], self.config)
        earlier = obs('c2', 'rejected', at=100.0)
        admission.record_outcome(self.conn, earlier, [earlier], self.config)
        entry = admission.status(self.conn, now=700.0)['active_cooldowns'][0]
        self.assertEqual((entry['failure_at'], entry['cooldown_until']), (500.0, 800.0))

    def test_zero_cooldown_expires_immediately(self):
        row = obs('c1', 'rejected', at=1000.0)
        admission.record_outcome(self.conn, row, [row], {'failure_cooldown_seconds': 0})
        self.assertFalse(admission.cooling(self.conn, features(), now=1000.0))

    def test_cooldown_is_scoped_to_the_task_family(self):
        row = obs('c1', 'rejected', at=1000.0)
        admission.record_outcome(self.conn, row, [row], self.config)
        self.assertFalse(admission.cooling(self.conn, features(operation='extend'), now=1000.0))

    def test_status_reports_only_active_cooldowns(self):
        row = obs('c1', 'rejected', at=1000.0)
        admission.record_outcome(self.conn, row, [row], self.config)
        active = admission.status(self.conn, now=1000.0)['active_cooldowns']
        self.assertEqual(len(active), 1)
        self.assertEqual(set(active[0]), {'family_id', 'failure_at', 'cooldown_until'})
        self.assertEqual(admission.status(self.conn, now=9999.0)['active_cooldowns'], [])

    def test_rollback_discards_a_recorded_pause(self):
        self.conn.execute('BEGIN IMMEDIATE')
        row = obs('c1', 'rejected', at=1000.0)
        admission.record_outcome(self.conn, row, [row], self.config)
        self.conn.rollback()
        self.assertEqual(admission.status(self.conn, now=1000.0)['active_cooldowns'], [])
        self.assertFalse(admission.cooling(self.conn, features(), now=1000.0))


class FamilyIdTests(unittest.TestCase):
    def test_family_fields_are_the_declared_tuple(self):
        self.assertEqual(admission.FAMILY_FIELDS,
                         ('kind', 'domain', 'operation', 'runtime', 'model', 'effort', 'context_version'))
        self.assertEqual(admission.REWORK_LIMIT, 3)
        self.assertEqual(validate_features(features()), validate_features(features()))


if __name__ == '__main__':
    unittest.main()
