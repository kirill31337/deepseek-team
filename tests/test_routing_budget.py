"""Atomic experiment-budget accounting on a caller-owned SQLite transaction."""
import datetime
import json
import multiprocessing
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest

from codex_deepseek_team import routing_budget
from codex_deepseek_team.routing_models import RoutingError


CONFIG = {'monthly_experiment_budget_usd': 5.0, 'per_experiment_limit_usd': 2.0}
NOW = datetime.datetime(2026, 2, 10, 12, 0, 0, tzinfo=datetime.timezone.utc).timestamp()
COLUMNS = {'reservation_id', 'month', 'reserved_micro', 'actual_micro', 'status', 'exceeded',
           'created_at', 'updated_at'}
FORBIDDEN = ('prompt', 'task', 'content', 'message', 'command', 'path', 'output', 'secret')


def epoch(year, month, day=1, hour=0, minute=0, second=0):
    return datetime.datetime(year, month, day, hour, minute, second,
                             tzinfo=datetime.timezone.utc).timestamp()


def _concurrent_reserve(database, result_path, index, amount, start_at):
    """Reserve in a private connection under BEGIN IMMEDIATE; report the outcome."""
    connection = sqlite3.connect(database, timeout=60, isolation_level=None)
    outcome, detail = 'busy', 'worker did not reach a verdict'
    try:
        connection.execute('PRAGMA busy_timeout = 60000')
        delay = start_at - time.time()
        if delay > 0:
            time.sleep(min(delay, 10.0))
        for _ in range(200):
            try:
                connection.execute('BEGIN IMMEDIATE')
            except sqlite3.OperationalError as error:
                detail = str(error)
                time.sleep(0.02)
                continue
            try:
                routing_budget.reserve(connection, {'monthly_experiment_budget_usd': 1.0,
                                                    'per_experiment_limit_usd': 1.0},
                                       'worker-%d' % index, amount)
                connection.execute('COMMIT')
                outcome, detail = 'ok', ''
            except RoutingError as error:
                outcome, detail = 'rejected', str(error)
                try:
                    connection.execute('ROLLBACK')
                except sqlite3.OperationalError:
                    pass
            except sqlite3.OperationalError as error:
                detail = str(error)
                try:
                    connection.execute('ROLLBACK')
                except sqlite3.OperationalError:
                    pass
                time.sleep(0.02)
                continue
            break
    finally:
        connection.close()
    Path(result_path).write_text(json.dumps({'index': index, 'outcome': outcome, 'detail': detail}))


class BudgetCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='dst-budget-')
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'budget.sqlite3'
        self.connection = self.connect(self.path)
        self.begin()
        routing_budget.initialize(self.connection)
        self.commit()

    def connect(self, path=None):
        connection = sqlite3.connect(path or self.path, isolation_level=None, timeout=60)
        self.addCleanup(connection.close)
        return connection

    def begin(self, connection=None):
        (connection or self.connection).execute('BEGIN IMMEDIATE')

    def commit(self, connection=None):
        (connection or self.connection).execute('COMMIT')

    def rollback(self, connection=None):
        (connection or self.connection).execute('ROLLBACK')

    def reserve(self, reservation_id, amount, config=None, now=NOW):
        self.begin()
        try:
            result = routing_budget.reserve(self.connection, config or CONFIG, reservation_id,
                                            amount, now=now)
        except BaseException:
            self.rollback()
            raise
        self.commit()
        return result

    def settle(self, reservation_id, actual, config=None, now=NOW):
        self.begin()
        try:
            result = routing_budget.settle(self.connection, config or CONFIG, reservation_id,
                                           actual, now=now)
        except BaseException:
            self.rollback()
            raise
        self.commit()
        return result

    def release(self, reservation_id, config=None, now=NOW):
        self.begin()
        try:
            result = routing_budget.release(self.connection, config or CONFIG, reservation_id,
                                            now=now)
        except BaseException:
            self.rollback()
            raise
        self.commit()
        return result

    def state(self, config=None, now=NOW):
        return routing_budget.status(self.connection, config or CONFIG, now=now)

    def assertRejected(self, function, *args, now=NOW, **kwargs):
        self.begin()
        try:
            with self.assertRaises(RoutingError):
                function(*args, now=now, **kwargs)
        finally:
            self.rollback()


class SchemaCase(BudgetCase):
    def test_large_truthful_settlements_do_not_overflow_sqlite_sum(self):
        config = {'monthly_experiment_budget_usd': 1e12, 'per_experiment_limit_usd': 1e12}
        for i in range(12):
            self.reserve(f'large-{i}', 1, config=config)
        for i in range(12):
            self.settle(f'large-{i}', 1e12, config=config)
        self.assertEqual(self.state(config=config)['spent_usd'], 12e12)
        self.assertTrue(self.state(config=config)['overrun'])

    def test_initialize_is_transactional_and_repeatable(self):
        fresh = self.connect(Path(self.tmp.name) / 'fresh.sqlite3')
        fresh.execute('BEGIN IMMEDIATE')
        routing_budget.initialize(fresh)
        routing_budget.initialize(fresh)
        self.assertTrue(fresh.in_transaction)
        fresh.execute('ROLLBACK')
        gone = fresh.execute("SELECT name FROM sqlite_master WHERE name='routing_budget'").fetchall()
        self.assertEqual(gone, [])
        fresh.execute('BEGIN IMMEDIATE')
        routing_budget.initialize(fresh)
        fresh.execute('COMMIT')
        self.assertEqual(routing_budget.status(fresh, CONFIG, now=NOW)['reservations']['total'], 0)

    def test_initialize_does_not_commit_caller_transaction(self):
        fresh = self.connect(Path(self.tmp.name) / 'fresh.sqlite3')
        fresh.execute('BEGIN IMMEDIATE')
        routing_budget.initialize(fresh)
        self.assertTrue(fresh.in_transaction)
        fresh.execute('ROLLBACK')
        self.assertEqual(fresh.execute(
            "SELECT name FROM sqlite_master WHERE name='routing_budget'").fetchall(), [])

    def test_indexes_share_the_table_prefix(self):
        names = [row[0] for row in self.connection.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='routing_budget' "
            "AND sql IS NOT NULL")]
        self.assertTrue(names)
        for name in names:
            self.assertTrue(name.startswith('routing_budget'), name)

    def test_schema_stores_only_budget_metadata(self):
        columns = [row[1] for row in self.connection.execute('PRAGMA table_info(routing_budget)')]
        self.assertTrue(COLUMNS <= set(columns))
        self.assertTrue(set(columns) <= COLUMNS, columns)
        for column in columns:
            for word in FORBIDDEN:
                self.assertNotIn(word, column)

    def test_status_before_initialize_is_a_routing_error(self):
        fresh = self.connect(Path(self.tmp.name) / 'fresh.sqlite3')
        with self.assertRaises(RoutingError):
            routing_budget.status(fresh, CONFIG)


class TransactionCase(BudgetCase):
    def test_mutations_require_a_caller_owned_transaction(self):
        for function in (lambda: routing_budget.reserve(self.connection, CONFIG, 'no-txn', 0.10),
                         lambda: routing_budget.settle(self.connection, CONFIG, 'no-txn', 0.10),
                         lambda: routing_budget.release(self.connection, CONFIG, 'no-txn')):
            with self.assertRaises(RoutingError):
                function()
            self.assertFalse(self.connection.in_transaction)
        self.assertEqual(self.state()['reservations']['total'], 0)

    def test_status_is_read_only_and_needs_no_transaction(self):
        state = routing_budget.status(self.connection, CONFIG, now=epoch(2026, 2, 1))
        self.assertFalse(self.connection.in_transaction)
        self.assertEqual(state['month'], '2026-02')
        self.assertEqual(state['reservations'], {'reserved': 0, 'settled': 0, 'released': 0, 'total': 0})

    def test_caller_rollback_discards_reservation(self):
        self.begin()
        routing_budget.reserve(self.connection, CONFIG, 'rolled-back', 1.0, now=NOW)
        self.rollback()
        self.assertEqual(self.state()['reservations']['total'], 0)

    def test_committed_reservations_persist_for_other_connections(self):
        self.reserve('persist', 0.25)
        other = self.connect()
        other.execute('BEGIN IMMEDIATE')
        state = routing_budget.status(other, CONFIG, now=NOW)
        other.execute('COMMIT')
        self.assertEqual(state['reserved_usd'], 0.25)
        self.assertEqual(state['reservations'], {'reserved': 1, 'settled': 0, 'released': 0, 'total': 1})

    def test_uncommitted_reservation_is_invisible_to_other_connections(self):
        self.begin()
        routing_budget.reserve(self.connection, CONFIG, 'pending', 1.0, now=NOW)
        other = self.connect()
        state = routing_budget.status(other, CONFIG, now=NOW)
        self.assertEqual(state['reserved_usd'], 0.0)
        self.rollback()
        self.assertEqual(self.state()['reserved_usd'], 0.0)

    def test_default_clock_uses_the_current_utc_month(self):
        month = time.strftime('%Y-%m', time.gmtime())
        self.reserve('live', 0.10, now=None)
        self.assertEqual(self.state(now=None)['month'], month)
        self.assertEqual(self.state(now=None)['reserved_usd'], 0.10)


class StatusCase(BudgetCase):
    def test_status_reports_empty_month(self):
        state = self.state(now=epoch(2026, 5, 17, 12, 30))
        self.assertEqual(set(state), {'month', 'limit_usd', 'per_experiment_limit_usd', 'reserved_usd',
                                      'spent_usd', 'remaining_usd', 'overrun', 'reservations'})
        self.assertEqual(state['month'], '2026-05')
        self.assertEqual(state['limit_usd'], 5.0)
        self.assertEqual(state['per_experiment_limit_usd'], 2.0)
        self.assertEqual(state['reserved_usd'], 0.0)
        self.assertEqual(state['spent_usd'], 0.0)
        self.assertEqual(state['remaining_usd'], 5.0)
        self.assertIs(state['overrun'], False)

    def test_status_counts_reservations_per_month(self):
        self.reserve('a', 1.0)
        self.reserve('b', 1.0)
        self.settle('b', 0.5)
        self.release('a')
        self.reserve('c', 1.0, now=epoch(2026, 3, 3))
        state = self.state()
        self.assertEqual(state['reservations'], {'reserved': 0, 'settled': 1, 'released': 1, 'total': 2})
        self.assertEqual(state['spent_usd'], 0.5)
        self.assertEqual(state['reserved_usd'], 0.0)
        self.assertEqual(self.state(now=epoch(2026, 3, 3))['reservations']['total'], 1)

    def test_status_uses_utc_month_boundaries(self):
        self.assertEqual(self.state(now=epoch(2026, 1, 31, 23, 59, 59))['month'], '2026-01')
        self.assertEqual(self.state(now=epoch(2026, 2, 1, 0, 0, 0))['month'], '2026-02')
        self.assertEqual(self.state(now=epoch(2026, 12, 31, 23, 59, 59))['month'], '2026-12')
        self.assertEqual(self.state(now=epoch(2027, 1, 1, 0, 0, 0))['month'], '2027-01')

    def test_status_accepts_utc_datetime_and_rejects_bad_clock(self):
        aware = datetime.datetime(2026, 4, 2, tzinfo=datetime.timezone.utc)
        self.assertEqual(self.state(now=aware)['month'], '2026-04')
        self.assertEqual(self.state(now=datetime.datetime(2026, 4, 2))['month'], '2026-04')
        for bad in ('now', float('nan'), float('inf'), -1, False, 10 ** 20):
            with self.assertRaises(RoutingError):
                routing_budget.status(self.connection, CONFIG, now=bad)


class ValidationCase(BudgetCase):
    def test_reservation_shape_and_normalization(self):
        reservation = self.reserve('shape-1', 0.25, now=epoch(2026, 2, 10, 8, 0))
        self.assertEqual(set(reservation), {'id', 'month', 'reserved_usd', 'actual_usd', 'status',
                                            'created_at', 'updated_at'})
        self.assertEqual(reservation['id'], 'shape-1')
        self.assertEqual(reservation['month'], '2026-02')
        self.assertEqual(reservation['reserved_usd'], 0.25)
        self.assertIsNone(reservation['actual_usd'])
        self.assertEqual(reservation['status'], 'reserved')
        self.assertEqual(reservation['created_at'], epoch(2026, 2, 10, 8, 0))
        self.assertEqual(reservation['updated_at'], reservation['created_at'])

    def test_zero_reservation_is_invalid(self):
        for amount in (0, 0.0, -0.0):
            self.assertRejected(routing_budget.reserve, self.connection, CONFIG, 'zero', amount)
        self.assertEqual(self.state()['reservations']['total'], 0)

    def test_zero_actual_is_allowed(self):
        self.reserve('free', 0.10)
        settled = self.settle('free', 0)
        self.assertEqual(settled['status'], 'settled')
        self.assertEqual(settled['actual_usd'], 0.0)

    def test_rejects_non_finite_negative_boolean_and_excessive_amounts(self):
        for amount in (True, False, -0.01, float('nan'), float('inf'), float('-inf'), 1e13,
                       '1.0', None, [1], {}, 10 ** 20):
            self.assertRejected(routing_budget.reserve, self.connection, CONFIG, 'bad-amount', amount)
            self.assertRejected(routing_budget.settle, self.connection, CONFIG, 'bad-amount', amount)
        self.assertEqual(self.state()['reservations']['total'], 0)

    def test_rejects_weird_reservation_ids(self):
        for identifier in ('', ' ', 'bad id', 'a' * 129, '-leading', None, 7, True, b'bytes',
                           'ünïcode', 'line\nbreak', 'tab\tid'):
            self.assertRejected(routing_budget.reserve, self.connection, CONFIG, identifier, 0.10)
            self.assertRejected(routing_budget.release, self.connection, CONFIG, identifier)
        self.assertEqual(self.state()['reservations']['total'], 0)

    def test_accepts_supported_identifier_alphabet(self):
        self.reserve('case:1/a-b_c.d', 0.10)
        self.assertEqual(self.state()['reservations']['total'], 1)

    def test_malformed_configuration_cannot_corrupt_accounting(self):
        for config in (None, [], 'config', 5,
                       {'monthly_experiment_budget_usd': -1.0},
                       {'monthly_experiment_budget_usd': float('nan')},
                       {'monthly_experiment_budget_usd': float('inf')},
                       {'monthly_experiment_budget_usd': True},
                       {'monthly_experiment_budget_usd': '5.0'},
                       {'per_experiment_limit_usd': -0.5},
                       {'per_experiment_limit_usd': float('nan')}):
            self.assertRejected(routing_budget.reserve, self.connection, config, 'config-shape', 0.10)
            self.assertRejected(routing_budget.settle, self.connection, config, 'config-shape', 0.10)
            self.assertRejected(routing_budget.release, self.connection, config, 'config-shape')
            with self.assertRaises(RoutingError):
                routing_budget.status(self.connection, config, now=NOW)
        self.assertEqual(self.state()['reservations']['total'], 0)

    def test_disabled_budgets_block_new_reservations(self):
        for config in ({'monthly_experiment_budget_usd': 0.0, 'per_experiment_limit_usd': 1.0},
                       {'monthly_experiment_budget_usd': 1.0, 'per_experiment_limit_usd': 0.0},
                       {'monthly_experiment_budget_usd': 0.0, 'per_experiment_limit_usd': 0.0},
                       {}):
            self.assertRejected(routing_budget.reserve, self.connection, config, 'disabled', 0.10)
        self.assertEqual(self.state()['reservations']['total'], 0)


class LimitCase(BudgetCase):
    def test_amount_above_per_experiment_limit_is_rejected(self):
        config = {'monthly_experiment_budget_usd': 10.0, 'per_experiment_limit_usd': 2.0}
        self.assertRejected(routing_budget.reserve, self.connection, config, 'too-big', 2.000001)
        self.reserve('at-limit', 2.0, config=config)

    def test_exact_per_experiment_and_monthly_boundaries_are_allowed(self):
        config = {'monthly_experiment_budget_usd': 1.0, 'per_experiment_limit_usd': 1.0}
        self.reserve('first', 1.0, config=config)
        state = self.state(config)
        self.assertEqual(state['remaining_usd'], 0.0)
        self.assertIs(state['overrun'], False)
        self.assertRejected(routing_budget.reserve, self.connection, config, 'second', 0.000001)

    def test_exact_monthly_boundary_across_two_reservations(self):
        config = {'monthly_experiment_budget_usd': 1.0, 'per_experiment_limit_usd': 1.0}
        self.reserve('sixty', 0.60, config=config)
        self.reserve('forty', 0.40, config=config)
        self.assertEqual(self.state(config)['remaining_usd'], 0.0)
        self.assertRejected(routing_budget.reserve, self.connection, config, 'overflow', 0.000001)

    def test_remaining_is_clamped_at_zero(self):
        config = {'monthly_experiment_budget_usd': 1.0, 'per_experiment_limit_usd': 1.0}
        self.reserve('over', 1.0, config=config)
        self.settle('over', 3.0, config=config)
        state = self.state(config)
        self.assertEqual(state['remaining_usd'], 0.0)
        self.assertEqual(state['spent_usd'], 3.0)
        self.assertIs(state['overrun'], True)

    def test_decimal_microdollar_math_blocks_float_overspend(self):
        config = {'monthly_experiment_budget_usd': 0.3, 'per_experiment_limit_usd': 0.3}
        self.reserve('tenth', 0.1, config=config)
        self.reserve('fifth', 0.2, config=config)
        state = self.state(config)
        self.assertEqual(state['reserved_usd'], 0.3)
        self.assertEqual(state['remaining_usd'], 0.0)
        self.assertRejected(routing_budget.reserve, self.connection, config, 'dust', 0.0000001)

    def test_ceiling_amounts_and_floor_limits_never_allow_overspend(self):
        config = {'monthly_experiment_budget_usd': 1.0000000001, 'per_experiment_limit_usd': 1.0000000001}
        self.reserve('whole', 1.0, config=config)
        self.assertRejected(routing_budget.reserve, self.connection, config, 'dust', 0.0000001)
        self.release('whole', config=config)
        per_experiment = {'monthly_experiment_budget_usd': 1.0, 'per_experiment_limit_usd': 0.0000019}
        tiny = self.reserve('tiny', 0.0000004, config=per_experiment)
        self.assertEqual(tiny['reserved_usd'], 0.000001)
        self.assertRejected(routing_budget.reserve, self.connection, per_experiment, 'tiny-two', 0.0000011)
        self.assertRejected(routing_budget.reserve, self.connection,
                            {'monthly_experiment_budget_usd': 0.0000005,
                             'per_experiment_limit_usd': 1.0}, 'sub-micro', 0.10)
        self.assertRejected(routing_budget.reserve, self.connection,
                            {'monthly_experiment_budget_usd': 1.0,
                             'per_experiment_limit_usd': 0.0000005}, 'sub-micro-run', 0.10)


class IdempotencyCase(BudgetCase):
    def test_repeated_reserve_with_same_amount_returns_existing_record(self):
        first = self.reserve('repeat', 0.40, now=epoch(2026, 2, 10, 1, 0))
        second = self.reserve('repeat', 0.40, now=epoch(2026, 2, 11, 1, 0))
        self.assertEqual(first, second)
        state = self.state(now=epoch(2026, 2, 12))
        self.assertEqual(state['reserved_usd'], 0.40)
        self.assertEqual(state['reservations']['total'], 1)

    def test_repeated_reserve_is_idempotent_across_statuses(self):
        self.reserve('lifecycle', 0.40)
        self.settle('lifecycle', 0.50)
        settled = self.reserve('lifecycle', 0.40)
        self.assertEqual(settled['status'], 'settled')
        self.assertEqual(settled['actual_usd'], 0.50)
        state = self.state()
        self.assertEqual(state['spent_usd'], 0.50)
        self.assertEqual(state['reserved_usd'], 0.0)
        self.assertEqual(state['reservations']['total'], 1)

    def test_altered_amount_for_same_id_is_rejected(self):
        self.reserve('conflict', 0.40)
        self.assertRejected(routing_budget.reserve, self.connection, CONFIG, 'conflict', 0.41)
        self.assertRejected(routing_budget.reserve, self.connection, CONFIG, 'conflict', 0.4000001)
        self.settle('conflict', 0.40)
        self.assertRejected(routing_budget.reserve, self.connection, CONFIG, 'conflict', 0.41)
        self.assertEqual(self.state()['reservations']['total'], 1)

    def test_amount_that_normalizes_to_the_same_microdollars_is_idempotent(self):
        first = self.reserve('same-micro', 1.0)
        second = self.reserve('same-micro', 0.9999999)
        self.assertEqual(first['reserved_usd'], 1.0)
        self.assertEqual(second, first)

    def test_repeated_settlement_with_same_actual_is_idempotent(self):
        self.reserve('settle-once', 0.40)
        first = self.settle('settle-once', 0.45, now=epoch(2026, 2, 11))
        second = self.settle('settle-once', 0.45, now=epoch(2026, 2, 12))
        self.assertEqual(first, second)
        self.assertEqual(self.state(now=epoch(2026, 2, 12))['spent_usd'], 0.45)

    def test_conflicting_settlement_actual_is_rejected(self):
        self.reserve('settle-twice', 0.40)
        self.settle('settle-twice', 0.45)
        self.assertRejected(routing_budget.settle, self.connection, CONFIG, 'settle-twice', 0.46)
        self.assertRejected(routing_budget.settle, self.connection, CONFIG, 'settle-twice', 0.44)
        self.assertEqual(self.state()['spent_usd'], 0.45)

    def test_missing_reservations_are_rejected(self):
        self.assertRejected(routing_budget.settle, self.connection, CONFIG, 'ghost', 0.10)
        self.assertRejected(routing_budget.release, self.connection, CONFIG, 'ghost')

    def test_release_is_idempotent_and_only_applies_to_reserved(self):
        self.reserve('release-me', 0.40)
        first = self.release('release-me', now=epoch(2026, 2, 11))
        second = self.release('release-me', now=epoch(2026, 2, 12))
        self.assertEqual(first, second)
        self.assertEqual(first['status'], 'released')
        self.assertIsNone(first['actual_usd'])

    def test_settled_reservation_cannot_be_released_and_released_cannot_settle(self):
        self.reserve('settled', 0.40)
        self.settle('settled', 0.40)
        self.assertRejected(routing_budget.release, self.connection, CONFIG, 'settled')
        self.reserve('released', 0.40)
        self.release('released')
        self.assertRejected(routing_budget.settle, self.connection, CONFIG, 'released', 0.40)

    def test_released_reservation_allows_a_different_id_to_reuse_capacity(self):
        config = {'monthly_experiment_budget_usd': 1.0, 'per_experiment_limit_usd': 1.0}
        self.reserve('freed', 1.0, config=config)
        self.assertRejected(routing_budget.reserve, self.connection, config, 'blocked', 0.5)
        self.release('freed', config=config)
        self.reserve('blocked', 1.0, config=config)
        state = self.state(config)
        self.assertEqual(state['reserved_usd'], 1.0)
        self.assertEqual(state['reservations'], {'reserved': 1, 'settled': 0, 'released': 1, 'total': 2})

    def test_reserve_after_settlement_does_not_resurrect_spending(self):
        config = {'monthly_experiment_budget_usd': 1.0, 'per_experiment_limit_usd': 1.0}
        self.reserve('consumed', 1.0, config=config)
        self.settle('consumed', 1.0, config=config)
        again = self.reserve('consumed', 1.0, config=config)
        self.assertEqual(again['status'], 'settled')
        self.assertRejected(routing_budget.reserve, self.connection, config, 'newcomer', 0.10)
        state = self.state(config)
        self.assertEqual(state['spent_usd'], 1.0)
        self.assertEqual(state['reserved_usd'], 0.0)


class OverrunCase(BudgetCase):
    def test_settlement_beyond_monthly_limit_marks_overrun_and_blocks_month(self):
        config = {'monthly_experiment_budget_usd': 1.0, 'per_experiment_limit_usd': 1.0}
        self.reserve('overspend', 0.50, config=config)
        self.settle('overspend', 1.50, config=config)
        state = self.state(config)
        self.assertIs(state['overrun'], True)
        self.assertEqual(state['spent_usd'], 1.5)
        self.assertEqual(state['remaining_usd'], 0.0)
        self.assertRejected(routing_budget.reserve, self.connection, config, 'after-overrun', 0.01)
        self.assertEqual(self.state(config)['reservations']['reserved'], 0)

    def test_exact_monthly_exhaustion_marks_overrun(self):
        config = {'monthly_experiment_budget_usd': 2.0, 'per_experiment_limit_usd': 2.0}
        self.reserve('exhaust', 2.0, config=config)
        self.assertIs(self.state(config)['overrun'], False)
        self.settle('exhaust', 2.0, config=config)
        state = self.state(config)
        self.assertEqual(state['spent_usd'], 2.0)
        self.assertIs(state['overrun'], True)

    def test_settlement_beyond_per_experiment_limit_marks_overrun_with_room_left(self):
        config = {'monthly_experiment_budget_usd': 10.0, 'per_experiment_limit_usd': 1.0}
        self.reserve('overrun-run', 0.50, config=config)
        self.settle('overrun-run', 2.0, config=config)
        state = self.state(config)
        self.assertIs(state['overrun'], True)
        self.assertEqual(state['spent_usd'], 2.0)
        self.assertEqual(state['remaining_usd'], 8.0)
        self.assertRejected(routing_budget.reserve, self.connection, config, 'blocked-by-run', 0.10)

    def test_overrun_is_scoped_to_its_month(self):
        config = {'monthly_experiment_budget_usd': 1.0, 'per_experiment_limit_usd': 1.0}
        self.reserve('january', 3.0, config={'monthly_experiment_budget_usd': 5.0,
                                             'per_experiment_limit_usd': 5.0}, now=epoch(2026, 1, 5))
        self.settle('january', 3.0, config=config, now=epoch(2026, 2, 5))
        january = self.state(config, now=epoch(2026, 1, 6))
        march = self.state(config, now=epoch(2026, 3, 1))
        self.assertEqual(january['spent_usd'], 3.0)
        self.assertIs(january['overrun'], True)
        self.assertEqual(march['spent_usd'], 0.0)
        self.assertIs(march['overrun'], False)
        self.reserve('march', 1.0, config=config, now=epoch(2026, 3, 1))

    def test_release_after_overrun_is_still_safe(self):
        config = {'monthly_experiment_budget_usd': 1.0, 'per_experiment_limit_usd': 1.0}
        self.reserve('settled', 0.5, config=config)
        self.reserve('open', 0.5, config=config)
        self.settle('settled', 1.5, config=config)
        self.release('open', config=config)
        state = self.state(config)
        self.assertIs(state['overrun'], True)
        self.assertEqual(state['reserved_usd'], 0.0)
        self.assertEqual(state['spent_usd'], 1.5)


class MonthCase(BudgetCase):
    def test_settlement_is_charged_to_the_reserved_month(self):
        january, march = epoch(2026, 1, 15), epoch(2026, 3, 15)
        config = {'monthly_experiment_budget_usd': 5.0, 'per_experiment_limit_usd': 5.0}
        self.reserve('historical', 1.0, config=config, now=january)
        self.assertEqual(self.state(config, now=january)['reservations']['reserved'], 1)
        settled = self.settle('historical', 2.0, config=config, now=march)
        self.assertEqual(settled['month'], '2026-01')
        self.assertEqual(settled['actual_usd'], 2.0)
        old = self.state(config, now=epoch(2026, 1, 20))
        new = self.state(config, now=march)
        self.assertEqual(old['spent_usd'], 2.0)
        self.assertEqual(old['reserved_usd'], 0.0)
        self.assertEqual(old['remaining_usd'], 3.0)
        self.assertEqual(new['spent_usd'], 0.0)
        self.assertEqual(new['reserved_usd'], 0.0)
        self.assertEqual(new['remaining_usd'], 5.0)
        self.reserve('march-work', 5.0, config=config, now=march)

    def test_old_settlement_does_not_consume_the_new_month_budget(self):
        config = {'monthly_experiment_budget_usd': 1.0, 'per_experiment_limit_usd': 1.0}
        self.reserve('old', 1.0, config=config, now=epoch(2026, 1, 10))
        self.settle('old', 1.0, config=config, now=epoch(2026, 2, 10))
        self.assertIs(self.state(config, now=epoch(2026, 2, 10))['overrun'], False)
        self.reserve('new', 1.0, config=config, now=epoch(2026, 2, 10))

    def test_reservation_month_follows_the_utc_rollover(self):
        last_of_january = epoch(2026, 1, 31, 23, 59, 59)
        first_of_february = epoch(2026, 2, 1, 0, 0, 0)
        reservation = self.reserve('rollover', 1.0, now=last_of_january)
        self.assertEqual(reservation['month'], '2026-01')
        self.assertEqual(self.state(now=last_of_january)['reserved_usd'], 1.0)
        self.assertEqual(self.state(now=first_of_february)['reserved_usd'], 0.0)
        self.assertEqual(self.state(now=first_of_february)['remaining_usd'], 5.0)


class CapacityCase(BudgetCase):
    def test_ledger_is_bounded(self):
        self.assertEqual(routing_budget.MAX_RESERVATIONS, 50000)
        original = routing_budget.MAX_RESERVATIONS
        routing_budget.MAX_RESERVATIONS = 3
        self.addCleanup(setattr, routing_budget, 'MAX_RESERVATIONS', original)
        for index in range(3):
            self.reserve('cap-%d' % index, 0.10)
        self.assertRejected(routing_budget.reserve, self.connection, CONFIG, 'cap-overflow', 0.10)
        self.assertEqual(self.state()['reservations']['total'], 3)
        self.reserve('cap-0', 0.10)

    def test_reservations_never_expire(self):
        self.reserve('durable', 0.10, now=epoch(2020, 1, 1))
        other = self.connect()
        year_2020 = routing_budget.status(other, CONFIG, now=epoch(2020, 1, 15))
        year_2030 = routing_budget.status(other, CONFIG, now=epoch(2030, 6, 1))
        self.assertEqual(year_2020['reserved_usd'], 0.10)
        self.assertEqual(year_2020['reservations']['reserved'], 1)
        self.assertEqual(year_2030['reservations']['total'], 0)


class ConcurrencyCase(BudgetCase):
    def test_concurrent_begin_immediate_reservations_cannot_overspend(self):
        if 'fork' not in multiprocessing.get_all_start_methods():
            self.skipTest('fork start method is unavailable')
        workers, amount = 6, 0.30
        results = Path(self.tmp.name) / 'results'
        results.mkdir()
        start_at = time.time() + 0.5
        context = multiprocessing.get_context('fork')
        processes = []
        for index in range(workers):
            process = context.Process(target=_concurrent_reserve,
                                      args=(str(self.path), str(results / ('%d.json' % index)),
                                            index, amount, start_at))
            process.start()
            processes.append(process)
        for process in processes:
            process.join(60)
            self.assertFalse(process.is_alive(), 'worker did not finish in time')
            self.assertEqual(process.exitcode, 0)
        outcomes = []
        for index in range(workers):
            payload = json.loads((results / ('%d.json' % index)).read_text())
            self.assertIn(payload['outcome'], ('ok', 'rejected'), payload)
            outcomes.append(payload['outcome'])
        accepted = outcomes.count('ok')
        self.assertEqual(accepted, 3)
        self.assertEqual(outcomes.count('rejected'), workers - accepted)
        state = self.state({'monthly_experiment_budget_usd': 1.0, 'per_experiment_limit_usd': 1.0},
                           now=None)
        self.assertEqual(state['reserved_usd'], 0.90)
        self.assertLessEqual(state['reserved_usd'], state['limit_usd'])
        self.assertEqual(state['reservations']['reserved'], accepted)

    def test_two_connections_serialize_under_begin_immediate(self):
        config = {'monthly_experiment_budget_usd': 1.0, 'per_experiment_limit_usd': 1.0}
        first = self.connect()
        second = self.connect()
        first.execute('BEGIN IMMEDIATE')
        routing_budget.reserve(first, config, 'first-writer', 1.0, now=NOW)
        first.execute('COMMIT')
        second.execute('BEGIN IMMEDIATE')
        state = routing_budget.status(second, config, now=NOW)
        second.execute('COMMIT')
        self.assertEqual(state['remaining_usd'], 0.0)
        second.execute('BEGIN IMMEDIATE')
        with self.assertRaises(RoutingError):
            routing_budget.reserve(second, config, 'second-writer', 0.000001, now=NOW)
        second.execute('ROLLBACK')
        self.assertEqual(self.state(config)['reservations']['total'], 1)


if __name__ == '__main__':
    unittest.main()
