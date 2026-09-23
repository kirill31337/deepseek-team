"""Scoped tests for the conservative recovery scheduler."""
import math
import os
import sqlite3
import tempfile
import threading
import unittest

from codex_deepseek_team import routing_models as models
from codex_deepseek_team import routing_recovery as recovery


def features(**overrides):
    card = dict(kind='implementation', domain='python', operation='fix',
                localization='known', coupling='local', verification='tests',
                clarity='clear', risk='low', scope_size='small', runtime='codex',
                effort='medium', model='deepseek-flash', context_version='v1')
    card.update(overrides)
    return card


def binding(task='t', deliverable='d', plan='p'):
    return {'task_id': task, 'deliverable_id': deliverable, 'plan_hash': plan}


class RecoveryTestBase(unittest.TestCase):
    def setUp(self):
        self.conn = self.new_connection()

    def new_connection(self, path=':memory:'):
        conn = sqlite3.connect(path)
        recovery.initialize(conn)
        self.addCleanup(conn.close)
        return conn

    def begin(self, conn=None):
        (conn or self.conn).execute('BEGIN IMMEDIATE')

    def selected(self, conn, config, card, bind, now=None):
        self.begin(conn)
        result = recovery.consider(conn, config, card, bind, now=now)
        conn.commit()
        return result

    def mutate(self, func, conn=None, *args, **kwargs):
        conn = conn or self.conn
        self.begin(conn)
        try:
            result = func(conn, *args, **kwargs)
        except Exception:
            conn.rollback()
            raise
        conn.commit()
        return result


class SelectionQuotaTests(RecoveryTestBase):
    def test_tiny_positive_rate_does_not_overflow_or_round_up_quota(self):
        for rate in (5e-324, 0.0999999999999):
            with self.subTest(rate=rate):
                conn = self.new_connection()
                config = {'recovery_rate': rate}
                first = self.selected(conn, config, features(), binding(plan='first'))
                self.assertTrue(first['selected'])
                self.mutate(lambda c: recovery.release(c, first['ticket_id']), conn)
                for index in range(2, 12):
                    result = self.selected(conn, config, features(), binding(plan=str(index)))
                    self.assertFalse(result['selected'])

    def test_initial_selection_and_exact_quota_boundary(self):
        config = {'recovery_rate': 0.1}
        first = self.selected(self.conn, config, features(), binding('t', 'd', '1'))
        self.assertTrue(first['selected'])
        self.assertIsNotNone(first['ticket_id'])
        state = recovery.status(self.conn, config)
        self.assertEqual((state['eligible_seen'], state['selected_count'],
                          state['pending_count'], state['running_count']), (1, 1, 1, 0))
        self.mutate(lambda c: recovery.mark_started(c, first['ticket_id']))
        self.mutate(lambda c: recovery.finish(c, first['ticket_id'], 'accepted'))
        results = [self.selected(self.conn, config, features(), binding('t', 'd', str(i)))
                   for i in range(2, 12)]
        self.assertTrue(all(not r['selected'] for r in results[:9]))
        self.assertTrue(all(r['reason'] == 'quota' for r in results[:9]))
        self.assertTrue(results[9]['selected'])
        self.assertEqual(recovery.status(self.conn, config)['eligible_seen'], 11)

    def test_disabled_rate_never_selects_or_counts(self):
        config = {'recovery_rate': 0}
        first = self.selected(self.conn, config, features(), binding())
        self.assertFalse(first['selected'])
        self.assertEqual(first['reason'], 'disabled')
        second = self.selected(self.conn, config, features(), binding())
        self.assertEqual(first, second)
        self.assertEqual(recovery.status(self.conn, config)['eligible_seen'], 0)
        self.assertEqual(recovery.status(self.conn, config)['selected_count'], 0)

    def test_feature_and_domain_changes_do_not_reset_global_quota(self):
        config = {'recovery_rate': 0.1}
        first = self.selected(self.conn, config, features(), binding('t', 'd', '1'))
        self.mutate(lambda c: recovery.release(c, first['ticket_id']))
        variants = [('go', 'v2'), ('rust', 'v3'), ('javascript', 'v4'), ('shell', 'v5'),
                    ('python', 'v6'), ('other', 'v7'), ('documentation', 'v8'),
                    ('python', 'v9'), ('go', 'v10')]
        for index, (domain, version) in enumerate(variants):
            result = self.selected(self.conn, config,
                                   features(domain=domain, context_version=version),
                                   binding('t', 'd', 'x%d' % index))
            self.assertFalse(result['selected'])
        final = self.selected(self.conn, config, features(domain='rust', context_version='v99'),
                              binding('t', 'd', 'final'))
        self.assertTrue(final['selected'])
        self.assertEqual(recovery.status(self.conn, config)['eligible_seen'], 11)

    def test_only_one_active_ticket_across_contexts(self):
        config = {'recovery_rate': 0.25}
        first = self.selected(self.conn, config, features(), binding('t', 'd', '1'))
        self.assertTrue(first['selected'])
        results = [self.selected(self.conn, config, features(effort='high'),
                                 binding('t', 'd', str(i))) for i in range(2, 6)]
        self.assertTrue(not any(r['selected'] for r in results))
        self.assertEqual(results[-1]['reason'], 'active_ticket')
        self.assertEqual(recovery.status(self.conn, config)['pending_count'], 1)


class EligibilityTests(RecoveryTestBase):
    def test_explicit_read_only_access_never_selects_write_kind(self):
        config = {'recovery_rate': .25, 'access': 'read-only'}
        for kind in models.WRITE_KINDS:
            result = self.selected(self.conn, config, features(kind=kind), binding(plan=kind))
            self.assertEqual(result['reason'], 'guard_access')
            self.assertFalse(result['selected'])
        self.assertEqual(recovery.status(self.conn, config)['eligible_seen'], 0)
        self.assertTrue(self.selected(self.conn, config, features(kind='review'), binding())['selected'])

    def test_unknown_high_protected_and_unverified_cases_are_never_explored(self):
        config = {'recovery_rate': 0.25}
        cases = [
            (features(risk='high'), 'ineligible_risk'),
            (features(risk='protected'), 'ineligible_risk'),
            (features(risk='medium'), 'ineligible_risk'),
            (features(kind='security'), 'ineligible_kind'),
            (features(kind='architecture'), 'ineligible_kind'),
            (features(kind='integration'), 'ineligible_kind'),
            (features(kind='review', verification='none'), 'ineligible_verification'),
            (features(kind='review', verification='manual'), 'ineligible_verification'),
            (features(verification='none'), 'ineligible_verification'),
            (features(domain='unknown'), 'ineligible_domain'),
            (features(operation='unknown'), 'ineligible_operation'),
            (features(scope_size='large'), 'ineligible_scope_size'),
            (features(coupling='component'), 'ineligible_coupling'),
            (features(clarity='unknown'), 'ineligible_clarity'),
            (features(clarity='partial'), 'ineligible_clarity'),
            (features(localization='partial'), 'ineligible_localization'),
            (features(model='deepseek-pro'), 'ineligible_model'),
            (features(context_version='unknown'), 'ineligible_context_version'),
        ]
        for index, (card, reason) in enumerate(cases):
            result = self.selected(self.conn, config, card, binding('t', 'd', str(index)))
            self.assertFalse(result['selected'])
            self.assertEqual(result['reason'], reason)
        self.assertEqual(recovery.status(self.conn, config)['eligible_seen'], 0)
        self.assertEqual(recovery.status(self.conn, config)['selected_count'], 0)

    def test_reproducer_verification_is_eligible(self):
        config = {'recovery_rate': 0.25}
        result = self.selected(self.conn, config, features(verification='reproducer'), binding())
        self.assertTrue(result['selected'])

    def test_guard_fields_are_checked_defensively(self):
        cases = [('mode', 'shadow', 'guard_mode'), ('mode', 'auto', None),
                 ('profile', 'manual', 'guard_profile'), ('access', 'root', 'guard_access'),
                 ('recommendation', 'worker', 'guard_recommendation'),
                 ('decision', 'abstain', None)]
        for index, (key, value, reason) in enumerate(cases):
            conn = self.new_connection()
            config = {'recovery_rate': 0.25, key: value}
            result = self.selected(conn, config, features(), binding('t', 'd', str(index)))
            if reason is None:
                self.assertTrue(result['selected'], (key, value))
            else:
                self.assertFalse(result['selected'])
                self.assertEqual(result['reason'], reason)


class ImmutabilityTests(RecoveryTestBase):
    def test_same_binding_is_never_rolled_again(self):
        config = {'recovery_rate': 0.1}
        first = self.selected(self.conn, config, features(), binding('t', 'd', 'a'))
        self.assertTrue(first['selected'])
        blocked = self.selected(self.conn, config, features(), binding('t', 'd', 'b'))
        self.assertFalse(blocked['selected'])
        for _ in range(20):
            self.assertEqual(self.selected(self.conn, config, features(), binding('t', 'd', 'a')), first)
            self.assertEqual(self.selected(self.conn, config, features(), binding('t', 'd', 'b')), blocked)
        state = recovery.status(self.conn, config)
        self.assertEqual((state['eligible_seen'], state['selected_count']), (2, 1))

    def test_changing_features_for_a_binding_errors(self):
        config = {'recovery_rate': 0.1}
        self.selected(self.conn, config, features(), binding())
        self.begin()
        try:
            with self.assertRaises(models.RoutingError):
                recovery.consider(self.conn, config, features(domain='go'), binding())
        finally:
            self.conn.rollback()

    def test_missing_transaction_is_rejected_for_eligible_candidates(self):
        with self.assertRaises(models.RoutingError):
            recovery.consider(self.conn, {'recovery_rate': 0.1}, features(), binding())
        self.assertFalse(self.conn.in_transaction)

    def test_ineligible_calls_do_not_require_a_transaction(self):
        result = recovery.consider(self.conn, {'recovery_rate': 0.25},
                                   features(risk='high'), binding())
        self.assertFalse(result['selected'])
        self.assertFalse(self.conn.in_transaction)


class LifecycleTests(RecoveryTestBase):
    def test_unlabelled_result_can_become_accepted_but_failure_never_laundered(self):
        first = self.selected(self.conn, {}, features(), binding(), now=100)
        ticket = first['ticket_id']
        self.mutate(lambda c: recovery.finish(c, ticket, 'unknown', now=101))
        accepted = self.mutate(lambda c: recovery.finish(c, ticket, 'accepted', now=102))
        self.assertEqual(accepted['outcome'], 'accepted')
        self.assertTrue(accepted['changed'])
        retained = self.mutate(lambda c: recovery.finish(c, ticket, 'infrastructure', now=103))
        self.assertEqual(retained['outcome'], 'accepted')
        self.mutate(lambda c: recovery.finish(c, ticket, 'rework', now=104))
        failed = self.mutate(lambda c: recovery.finish(c, ticket, 'accepted', now=105))
        self.assertEqual(failed['outcome'], 'rework')
        self.assertFalse(failed['changed'])

    def test_mark_started_is_idempotent_and_pending_expires(self):
        config = {'recovery_rate': 0.25}
        first = self.selected(self.conn, config, features(), binding('t', 'd', '1'), now=100)
        ticket = first['ticket_id']
        expired = self.mutate(lambda c: recovery.mark_started(c, ticket, now=100 + recovery.PENDING_TTL_SECONDS))
        self.assertFalse(expired['started'])
        self.assertEqual(expired['status'], 'expired')
        state = recovery.status(self.conn, config, now=100 + recovery.PENDING_TTL_SECONDS + 1)
        self.assertEqual(state['pending_count'], 0)
        self.assertEqual(state['tickets'], [])
        self.mutate(lambda c: recovery.release(c, ticket, now=100 + recovery.PENDING_TTL_SECONDS))

    def test_mark_started_running_is_idempotent_and_never_expires(self):
        config = {'recovery_rate': 0.25}
        first = self.selected(self.conn, config, features(), binding('t', 'd', '1'), now=100)
        ticket = first['ticket_id']
        started = self.mutate(lambda c: recovery.mark_started(c, ticket, now=101))
        self.assertTrue(started['started'])
        self.assertEqual(started['status'], 'running')
        self.mutate(lambda c: recovery.mark_started(c, ticket, now=102))
        self.assertEqual(self.mutate(lambda c: recovery.mark_started(c, ticket, now=102))['started'], True)
        state = recovery.status(self.conn, config, now=100 + 10 ** 9)
        self.assertEqual(state['running_count'], 1)
        self.assertEqual(state['pending_count'], 0)
        results = [self.selected(self.conn, config, features(), binding('t', 'd', str(i)),
                                 now=100 + 10 ** 9) for i in range(2, 6)]
        self.assertEqual(results[-1]['reason'], 'active_ticket')
        self.mutate(lambda c: recovery.finish(c, ticket, 'accepted', now=100 + 10 ** 9 + 1))

    def test_cleanup_inside_consider_frees_expired_slot(self):
        config = {'recovery_rate': 0.25}
        orphan = self.selected(self.conn, config, features(), binding('t', 'd', '1'), now=0)
        results = [self.selected(self.conn, config, features(), binding('t', 'd', str(i)),
                                 now=recovery.PENDING_TTL_SECONDS + 1) for i in range(2, 6)]
        self.assertTrue(all(not r['selected'] for r in results[:3]))
        self.assertTrue(results[3]['selected'])
        state = recovery.status(self.conn, config, now=recovery.PENDING_TTL_SECONDS + 1)
        self.assertEqual(state['pending_count'], 1)
        self.assertEqual([t['ticket_id'] for t in state['tickets']], [results[3]['ticket_id']])
        self.assertNotEqual(results[3]['ticket_id'], orphan['ticket_id'])

    def test_release_pending_is_idempotent_and_never_releases_running_or_resolved(self):
        config = {'recovery_rate': 0.25}
        first = self.selected(self.conn, config, features(), binding('t', 'd', '1'), now=0)
        released = self.mutate(lambda c: recovery.release(c, first['ticket_id'], now=1))
        self.assertTrue(released['released'])
        self.assertEqual(released['status'], 'released')
        again = self.mutate(lambda c: recovery.release(c, first['ticket_id'], now=2))
        self.assertTrue(again['released'])
        self.assertEqual(again['reason'], 'already_released')
        unknown = self.mutate(lambda c: recovery.release(c, 'does-not-exist', now=3))
        self.assertFalse(unknown['released'])
        self.assertIsNone(unknown['status'])
        self.assertEqual(unknown['reason'], 'unknown')
        second = None
        for index in range(2, 6):
            candidate = self.selected(self.conn, config, features(),
                                      binding('t', 'd', str(index)), now=4)
            if candidate['selected']:
                second = candidate
        self.assertIsNotNone(second)
        self.mutate(lambda c: recovery.mark_started(c, second['ticket_id'], now=5))
        running = self.mutate(lambda c: recovery.release(c, second['ticket_id'], now=6))
        self.assertFalse(running['released'])
        self.assertEqual(running['reason'], 'running')
        self.mutate(lambda c: recovery.finish(c, second['ticket_id'], 'accepted', now=7))
        resolved = self.mutate(lambda c: recovery.release(c, second['ticket_id'], now=8))
        self.assertFalse(resolved['released'])
        self.assertEqual(resolved['reason'], 'resolved')

    def test_release_does_not_undo_quota_history(self):
        config = {'recovery_rate': 0.25}
        first = self.selected(self.conn, config, features(), binding('t', 'd', '1'), now=0)
        self.mutate(lambda c: recovery.release(c, first['ticket_id'], now=1))
        state = recovery.status(self.conn, config)
        self.assertEqual((state['eligible_seen'], state['selected_count']), (1, 1))
        for index in range(2, 5):
            self.assertFalse(self.selected(self.conn, config, features(),
                                           binding('t', 'd', str(index)))['selected'])
        final = self.selected(self.conn, config, features(), binding('t', 'd', '5'))
        self.assertTrue(final['selected'])


class CooldownTests(RecoveryTestBase):
    def test_failure_starts_finite_bucket_cooldown_then_recovers(self):
        config = {'recovery_rate': 0.25, 'recovery_cooldown_seconds': 100}
        first = self.selected(self.conn, config, features(), binding('t', 'd', '1'), now=0)
        self.mutate(lambda c: recovery.mark_started(c, first['ticket_id'], now=1))
        outcome = self.mutate(lambda c: recovery.finish(c, first['ticket_id'], 'rework', now=10))
        self.assertTrue(outcome['labelled'])
        results = [self.selected(self.conn, config, features(), binding('t', 'd', str(i)), now=20)
                   for i in range(2, 6)]
        self.assertTrue(all(not r['selected'] for r in results))
        self.assertEqual(results[-1]['reason'], 'bucket_cooldown')
        recovered = self.selected(self.conn, config, features(), binding('t', 'd', '6'), now=200)
        self.assertTrue(recovered['selected'])
        state = recovery.status(self.conn, config, now=200)
        self.assertEqual((state['eligible_seen'], state['selected_count']), (6, 2))

    def test_different_bucket_still_subject_to_global_quota(self):
        config = {'recovery_rate': 0.25, 'recovery_cooldown_seconds': 1000}
        first = self.selected(self.conn, config, features(), binding('t', 'd', '1'), now=0)
        self.mutate(lambda c: recovery.finish(c, first['ticket_id'], 'rework', now=1))
        results = [self.selected(self.conn, config, features(domain='go'),
                                 binding('t', 'd', str(i)), now=5) for i in range(2, 6)]
        self.assertTrue(all(not r['selected'] for r in results[:3]))
        self.assertTrue(results[3]['selected'])

    def test_zero_cooldown_allows_immediate_recovery(self):
        config = {'recovery_rate': 0.25, 'recovery_cooldown_seconds': 0}
        first = self.selected(self.conn, config, features(), binding('t', 'd', '1'), now=0)
        self.mutate(lambda c: recovery.finish(c, first['ticket_id'], 'rejected', now=1))
        results = [self.selected(self.conn, config, features(), binding('t', 'd', str(i)), now=2)
                   for i in range(2, 6)]
        self.assertTrue(results[-1]['selected'])

    def test_accepted_can_be_corrected_to_failure_which_restarts_cooldown(self):
        config = {'recovery_rate': 0.25, 'recovery_cooldown_seconds': 100}
        first = self.selected(self.conn, config, features(), binding('t', 'd', '1'), now=0)
        self.mutate(lambda c: recovery.finish(c, first['ticket_id'], 'accepted', now=0))
        corrected = self.mutate(lambda c: recovery.finish(c, first['ticket_id'], 'rework', now=500))
        self.assertTrue(corrected['changed'])
        self.assertEqual(corrected['outcome'], 'rework')
        self.assertTrue(corrected['labelled'])
        results = [self.selected(self.conn, config, features(), binding('t', 'd', str(i)), now=550)
                   for i in range(2, 6)]
        self.assertEqual(results[-1]['reason'], 'bucket_cooldown')

    def test_failure_can_never_become_accepted(self):
        config = {'recovery_rate': 0.25, 'recovery_cooldown_seconds': 100}
        first = self.selected(self.conn, config, features(), binding('t', 'd', '1'), now=0)
        self.mutate(lambda c: recovery.finish(c, first['ticket_id'], 'rework', now=1))
        retained = self.mutate(lambda c: recovery.finish(c, first['ticket_id'], 'accepted', now=2))
        self.assertFalse(retained['changed'])
        self.assertEqual(retained['outcome'], 'rework')
        self.assertTrue(retained['labelled'])
        self.assertEqual(retained['reason'], 'retained')
        repeated = self.mutate(lambda c: recovery.finish(c, first['ticket_id'], 'rework', now=3))
        self.assertFalse(repeated['changed'])
        self.assertEqual(repeated['reason'], 'idempotent')

    def test_unlabelled_outcomes_never_start_quality_cooldown(self):
        for outcome in ('infrastructure', 'cancelled', 'unknown'):
            conn = self.new_connection()
            config = {'recovery_rate': 0.25, 'recovery_cooldown_seconds': 100}
            first = self.selected(conn, config, features(), binding('t', 'd', '1'), now=0)
            self.mutate(lambda c: recovery.mark_started(c, first['ticket_id'], now=1), conn=conn)
            resolved = self.mutate(lambda c: recovery.finish(c, first['ticket_id'], outcome, now=2),
                                   conn=conn)
            self.assertFalse(resolved['labelled'])
            corrected = self.mutate(lambda c: recovery.finish(c, first['ticket_id'], 'accepted', now=3),
                                   conn=conn)
            self.assertTrue(corrected['changed'])
            self.assertEqual(corrected['outcome'], 'accepted')
            results = [self.selected(conn, config, features(), binding('t', 'd', str(i)), now=4)
                       for i in range(2, 6)]
            self.assertTrue(results[-1]['selected'], outcome)

    def test_finish_pending_ticket_directly_resolves(self):
        config = {'recovery_rate': 0.25}
        first = self.selected(self.conn, config, features(), binding('t', 'd', '1'), now=0)
        resolved = self.mutate(lambda c: recovery.finish(c, first['ticket_id'], 'accepted', now=1))
        self.assertEqual(resolved['status'], 'resolved')
        released = self.mutate(lambda c: recovery.release(c, first['ticket_id'], now=2))
        self.assertFalse(released['released'])

    def test_finish_on_released_ticket_is_not_open(self):
        config = {'recovery_rate': 0.25}
        first = self.selected(self.conn, config, features(), binding('t', 'd', '1'), now=0)
        self.mutate(lambda c: recovery.release(c, first['ticket_id'], now=1))
        result = self.mutate(lambda c: recovery.finish(c, first['ticket_id'], 'rework', now=2))
        self.assertFalse(result['changed'])
        self.assertEqual(result['reason'], 'not_open')


class StatusTests(RecoveryTestBase):
    def test_status_is_read_only_with_closed_metadata(self):
        config = {'recovery_rate': 0.25, 'recovery_cooldown_seconds': 50}
        first = self.selected(self.conn, config, features(), binding('t', 'd', '1'))
        state = recovery.status(self.conn, config)
        self.assertFalse(self.conn.in_transaction)
        self.assertEqual(set(state), {'eligible_seen', 'selected_count', 'running_count',
                                      'pending_count', 'tickets', 'recovery_rate',
                                      'cooldown_seconds', 'bootstrap_pending_count', 'bootstrap_running_count',
                                      'recovery_active_count', 'max_active_trials', 'coordinator_comparison_interval'})
        self.assertEqual(state['recovery_rate'], 0.25)
        self.assertEqual(state['cooldown_seconds'], 50.0)
        self.assertEqual(state['tickets'][0]['ticket_id'], first['ticket_id'])
        self.assertEqual(set(state['tickets'][0]),
                         {'ticket_id', 'binding_id', 'bucket_id', 'status', 'created_at',
                          'expires_at', 'started_at', 'admission'})
        self.assertEqual(recovery.status(self.conn, config), state)

    def test_status_counts_only_active_selected_records(self):
        config = {'recovery_rate': 0.25}
        first = self.selected(self.conn, config, features(), binding('t', 'd', '1'))
        self.assertEqual(recovery.status(self.conn, config)['tickets'][0]['status'], 'pending')
        self.mutate(lambda c: recovery.mark_started(c, first['ticket_id']))
        state = recovery.status(self.conn, config)
        self.assertEqual((state['running_count'], state['pending_count'], state['selected_count']),
                         (1, 0, 1))
        self.mutate(lambda c: recovery.finish(c, first['ticket_id'], 'accepted'))
        state = recovery.status(self.conn, config)
        self.assertEqual((state['running_count'], state['pending_count'], state['selected_count']),
                         (0, 0, 1))
        self.assertEqual(state['tickets'], [])


class AtomicityTests(RecoveryTestBase):
    def test_rollback_leaves_no_state(self):
        config = {'recovery_rate': 0.1}
        self.begin()
        first = recovery.consider(self.conn, config, features(), binding())
        self.assertTrue(first['selected'])
        self.conn.rollback()
        self.assertEqual(recovery.status(self.conn, config)['eligible_seen'], 0)
        again = self.selected(self.conn, config, features(), binding())
        self.assertTrue(again['selected'])
        self.assertEqual(recovery.status(self.conn, config)['eligible_seen'], 1)

    def test_two_connections_share_one_active_slot(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'recovery.sqlite')
            first_conn = self.new_connection(path)
            second_conn = self.new_connection(path)
            config = {'recovery_rate': 0.25}
            self.begin(first_conn)
            selected = recovery.consider(first_conn, config, features(), binding('t', 'd', '1'))
            first_conn.commit()
            self.assertTrue(selected['selected'])
            self.begin(second_conn)
            results = [recovery.consider(second_conn, config, features(), binding('t', 'd', str(i)))
                       for i in range(2, 6)]
            second_conn.commit()
            self.assertTrue(not any(r['selected'] for r in results))
            self.assertEqual(results[-1]['reason'], 'active_ticket')
            state = recovery.status(first_conn, config)
            self.assertEqual((state['selected_count'], state['pending_count']), (1, 1))

    def test_concurrent_processes_select_exactly_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'recovery.sqlite')
            setup = self.new_connection(path)
            config = {'recovery_rate': 0.25}
            count = 8
            barrier = threading.Barrier(count)
            results = []
            lock = threading.Lock()

            def worker(index):
                conn = sqlite3.connect(path, timeout=30, isolation_level=None)
                conn.execute('PRAGMA busy_timeout = 30000')
                barrier.wait()
                conn.execute('BEGIN IMMEDIATE')
                try:
                    result = recovery.consider(conn, config, features(),
                                               binding('t', 'd', str(index)))
                finally:
                    conn.commit()
                conn.close()
                with lock:
                    results.append(result)

            threads = [threading.Thread(target=worker, args=(i,)) for i in range(count)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(len(results), count)
            self.assertEqual(sum(1 for r in results if r['selected']), 1)
            state = recovery.status(setup, config)
            self.assertEqual((state['eligible_seen'], state['selected_count']), (count, 1))


class ValidationTests(RecoveryTestBase):
    def test_invalid_recovery_config_is_rejected(self):
        bad_configs = [
            {'recovery_rate': 10 ** 400},
            {'recovery_cooldown_seconds': 10 ** 400},
            {'recovery_rate': True},
            {'recovery_rate': float('nan')},
            {'recovery_rate': float('inf')},
            {'recovery_rate': 0.3},
            {'recovery_rate': -0.1},
            {'recovery_rate': '0.1'},
            {'recovery_cooldown_seconds': True},
            {'recovery_cooldown_seconds': float('nan')},
            {'recovery_cooldown_seconds': float('inf')},
            {'recovery_cooldown_seconds': -1},
            {'recovery_cooldown_seconds': recovery.MAX_RECOVERY_COOLDOWN_SECONDS + 1},
        ]
        for config in bad_configs:
            with self.assertRaises(models.RoutingError):
                recovery.status(self.conn, config)
            self.begin()
            try:
                with self.assertRaises(models.RoutingError):
                    recovery.consider(self.conn, config, features(), binding())
            finally:
                self.conn.rollback()

    def test_non_mapping_config_is_rejected(self):
        for config in (None, [], 'auto', 3):
            with self.assertRaises(models.RoutingError):
                recovery.status(self.conn, config)

    def test_invalid_now_is_rejected(self):
        for value in (float('nan'), float('inf'), 'now', object(), -1, 10 ** 400):
            with self.assertRaises(models.RoutingError):
                recovery.status(self.conn, {}, now=value)
            self.begin()
            try:
                with self.assertRaises(models.RoutingError):
                    recovery.consider(self.conn, {}, features(), binding(), now=value)
            finally:
                self.conn.rollback()

    def test_invalid_features_and_bindings_are_rejected(self):
        config = {'recovery_rate': 0.25}
        self.begin()
        try:
            with self.assertRaises(models.RoutingError):
                recovery.consider(self.conn, config, {'unexpected': 1}, binding())
            with self.assertRaises(models.RoutingError):
                recovery.consider(self.conn, config, features(risk='bogus'), binding())
            with self.assertRaises(models.RoutingError):
                recovery.consider(self.conn, config, features(),
                                  {'task_id': 't', 'deliverable_id': 'd'})
            with self.assertRaises(models.RoutingError):
                recovery.consider(self.conn, config, features(),
                                  {'task_id': 't', 'deliverable_id': 'd', 'plan_hash': 'p', 'x': 'y'})
        finally:
            self.conn.rollback()

    def test_invalid_ticket_operations_are_rejected(self):
        self.begin()
        try:
            with self.assertRaises(models.RoutingError):
                recovery.mark_started(self.conn, 'missing')
            with self.assertRaises(models.RoutingError):
                recovery.finish(self.conn, 'missing', 'accepted')
            with self.assertRaises(models.RoutingError):
                recovery.finish(self.conn, 'missing', 'nonsense')
            with self.assertRaises(models.RoutingError):
                recovery.mark_started(self.conn, '')
        finally:
            self.conn.rollback()

    def test_considered_binding_cap_is_enforced(self):
        original = recovery.MAX_CONSIDERED_BINDINGS
        recovery.MAX_CONSIDERED_BINDINGS = 1
        try:
            self.selected(self.conn, {'recovery_rate': 0.25}, features(), binding('t', 'd', '1'))
            self.begin()
            try:
                with self.assertRaises(models.RoutingError):
                    recovery.consider(self.conn, {'recovery_rate': 0.25}, features(),
                                      binding('t', 'd', '2'))
            finally:
                self.conn.rollback()
        finally:
            recovery.MAX_CONSIDERED_BINDINGS = original


class SchemaTests(RecoveryTestBase):
    def test_schema_names_are_prefixed_and_exact(self):
        self.assertTrue(recovery.SCHEMA)
        for name in recovery.SCHEMA:
            self.assertTrue(name.startswith('routing_recovery'), name)
        rows = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE name LIKE 'routing_recovery%'").fetchall()
        self.assertEqual({row[0] for row in rows}, set(recovery.SCHEMA))

    def test_initialize_is_idempotent_and_does_not_commit(self):
        recovery.initialize(self.conn)
        self.assertFalse(self.conn.in_transaction)
        self.begin()
        recovery.initialize(self.conn)
        self.assertTrue(self.conn.in_transaction)
        self.conn.rollback()

    def test_constants_match_specification(self):
        self.assertEqual(recovery.PENDING_TTL_SECONDS, 3600)
        self.assertEqual(recovery.MAX_CONSIDERED_BINDINGS, 50000)
        self.assertEqual(recovery.DEFAULT_RECOVERY_RATE, 0.1)
        self.assertEqual(recovery.MAX_RECOVERY_RATE, 0.25)
        self.assertEqual(recovery.DEFAULT_RECOVERY_COOLDOWN_SECONDS, 3600)
        self.assertEqual(recovery.MAX_RECOVERY_COOLDOWN_SECONDS, 2592000)
        self.assertEqual(recovery.ELIGIBLE_KINDS, models.WRITE_KINDS | models.READ_KINDS)
        self.assertFalse(recovery.ELIGIBLE_KINDS & models.PROTECTED_KINDS)


if __name__ == '__main__':
    unittest.main()
