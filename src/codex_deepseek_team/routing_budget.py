"""Atomic SQLite accounting for estimated routing experiment spending.

This ledger bounds *estimated experiment spending*; it is not a provider billing
limit and it never talks to a network or a model.  Amounts are stored as integer
microdollars converted with :class:`decimal.Decimal` (``ROUND_CEILING`` for
amounts, ``ROUND_FLOOR`` for limits) so that binary floating point can never
round a reservation or a limit towards a silent overspend.

Every mutation (``reserve``, ``settle``, ``release``) requires an active,
caller-owned transaction (``connection.in_transaction``).  The functions never
begin, commit or roll back: the root store wraps them in ``BEGIN IMMEDIATE`` so
that the read/check/write sequence is atomic across connections and processes.
``initialize`` likewise never commits; it is safe to call inside the caller's
transaction.  ``status`` is read-only.

Money is normalised to microdollars: ``0.0000001`` rounds up to one
microdollar.  A monthly or per-experiment limit that floors to zero disables
spending rather than allowing it.  Reservations persist across crashes (no
expiry) and are capped by :data:`MAX_RESERVATIONS` to bound database growth.
Only identifiers, months, integer amounts, statuses and timestamps are stored -
never task text, prompts, paths or credentials.

Reservations carry the UTC month they were created in; settlements and the
spending they record always stay attached to that month, so settling an old
reservation never consumes the current month.  ``created_at``/``updated_at``
are UTC epoch seconds and ``now`` accepts an epoch number, an aware or naive
``datetime`` (treated as UTC) or ``None`` for the current clock.

A month is ``overrun`` when one of its experiments settled above the
per-experiment limit or when its settled spending reached the monthly limit;
an overrun month accepts no new reservations.  Overrun is evaluated against the
configuration supplied to the call (the caller-validated current configuration)
together with the per-experiment excess recorded when each experiment settled,
so raising the monthly limit re-authorises further reservations.  Releasing a
reservation always remains possible.
"""
from __future__ import annotations

import datetime
import math
import sqlite3
import time
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

from .routing_models import RoutingError, identifier

TABLE = 'routing_budget'
INDEXES = (f'{TABLE}_month_status_idx', f'{TABLE}_month_exceeded_idx')
MAX_RESERVATIONS = 50000
MAX_USD = 1e12
MAX_TIMESTAMP = 253402300799.0  # 9999-12-31T23:59:59Z
MICRODOLLARS = Decimal(1_000_000)
MICRO_QUANTUM = Decimal('0.000001')
_SELECT = (f'SELECT reservation_id, month, reserved_micro, actual_micro, status, exceeded, '
           f'created_at, updated_at FROM {TABLE}')

_SCHEMA = (
    f'''CREATE TABLE IF NOT EXISTS {TABLE} (
        reservation_id TEXT PRIMARY KEY,
        month TEXT NOT NULL,
        reserved_micro INTEGER NOT NULL CHECK (reserved_micro > 0),
        actual_micro INTEGER CHECK (actual_micro IS NULL OR actual_micro >= 0),
        status TEXT NOT NULL CHECK (status IN ('reserved', 'settled', 'released')),
        exceeded INTEGER NOT NULL DEFAULT 0 CHECK (exceeded IN (0, 1)),
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL
    )''',
    f'CREATE INDEX IF NOT EXISTS {INDEXES[0]} ON {TABLE} (month, status)',
    f'CREATE INDEX IF NOT EXISTS {INDEXES[1]} ON {TABLE} (month, exceeded)',
)


def initialize(connection) -> None:
    """Create the ledger table and its indexes.  Never commits."""
    for statement in _SCHEMA:
        connection.execute(statement)


def status(connection, config, *, now=None) -> dict:
    """Report the current UTC month against the configured experiment budget.

    Aggregates are evaluated per reservation month, so a settlement recorded
    today for an older reservation is charged to that reservation's month
    rather than to the current one.  ``remaining_usd`` is floored at zero.
    """
    monthly_micro, per_experiment_micro = _limits(config)
    month = _month(_timestamp(now))
    totals, counts = _aggregate(connection, month)
    remaining = max(0, monthly_micro - totals['reserved_micro'] - totals['spent_micro'])
    return {
        'month': month,
        'limit_usd': _usd(monthly_micro),
        'per_experiment_limit_usd': _usd(per_experiment_micro),
        'reserved_usd': _usd(totals['reserved_micro']),
        'spent_usd': _usd(totals['spent_micro']),
        'remaining_usd': _usd(remaining),
        'overrun': _overrun(monthly_micro, totals),
        'reservations': counts,
    }


def reserve(connection, config, reservation_id: str, amount_usd, *, now=None) -> dict:
    """Atomically reserve estimated spending for one experiment.

    Repeating the call with the same identifier and the same normalised amount
    returns the stored reservation regardless of its status; the reservation is
    never re-created and spending is never resurrected.  A different normalised
    amount for the same identifier is rejected.
    """
    _require_transaction(connection, 'reserve')
    reservation_id = identifier(reservation_id, 'reservation_id')
    amount_micro = _micro_ceil(amount_usd, 'amount_usd')
    if amount_micro <= 0:
        raise RoutingError('amount_usd must be positive; a zero reservation is not permitted.')
    monthly_micro, per_experiment_micro = _limits(config)
    timestamp = _timestamp(now)
    month = _month(timestamp)
    existing = _fetch(connection, reservation_id)
    if existing is not None:
        if existing[2] != amount_micro:
            raise RoutingError(f'Reservation {reservation_id!r} already exists with a different amount.')
        return _reservation(existing)
    if monthly_micro <= 0 or per_experiment_micro <= 0:
        raise RoutingError('Experiment budget is disabled by a zero monthly or per-experiment limit.')
    if amount_micro > per_experiment_micro:
        raise RoutingError('amount_usd exceeds the configured per-experiment limit.')
    recorded = connection.execute(f'SELECT COUNT(*) FROM {TABLE}').fetchone()[0]
    if recorded >= MAX_RESERVATIONS:
        raise RoutingError(f'Experiment budget ledger is full ({MAX_RESERVATIONS} reservations).')
    totals, _counts = _aggregate(connection, month)
    if _overrun(monthly_micro, totals):
        raise RoutingError(f'Experiment budget for {month} is overrun; new reservations are blocked.')
    if totals['spent_micro'] + totals['reserved_micro'] + amount_micro > monthly_micro:
        raise RoutingError('Reservation would exceed the monthly experiment budget.')
    connection.execute(
        f'INSERT INTO {TABLE} (reservation_id, month, reserved_micro, actual_micro, status, '
        f'exceeded, created_at, updated_at) VALUES (?, ?, ?, NULL, ?, 0, ?, ?)',
        (reservation_id, month, amount_micro, 'reserved', timestamp, timestamp))
    stored = _fetch(connection, reservation_id)
    if stored is None:
        raise RoutingError('Reservation was not persisted.')
    return _reservation(stored)


def settle(connection, config, reservation_id: str, actual_usd, *, now=None) -> dict:
    """Record the true spending of a reserved experiment.

    The actual amount is charged to the reservation's own month.  It may exceed
    the estimate, the per-experiment limit or the monthly limit: the truth is
    recorded, and a per-experiment excess or an exhausted month marks that month
    as overrun so no further reservations are accepted there.  Only a released
    reservation cannot be settled.
    """
    _require_transaction(connection, 'settle')
    reservation_id = identifier(reservation_id, 'reservation_id')
    actual_micro = _micro_ceil(actual_usd, 'actual_usd')
    _monthly_micro, per_experiment_micro = _limits(config)
    timestamp = _timestamp(now)
    existing = _fetch(connection, reservation_id)
    if existing is None:
        raise RoutingError(f'Reservation {reservation_id!r} does not exist.')
    if existing[4] == 'settled':
        if existing[3] == actual_micro:
            return _reservation(existing)
        raise RoutingError(f'Reservation {reservation_id!r} is already settled with a different amount.')
    if existing[4] == 'released':
        raise RoutingError(f'Reservation {reservation_id!r} was released and cannot be settled.')
    exceeded = int(per_experiment_micro > 0 and actual_micro > per_experiment_micro)
    cursor = connection.execute(
        f'UPDATE {TABLE} SET status = ?, actual_micro = ?, exceeded = ?, updated_at = ? '
        f"WHERE reservation_id = ? AND status = 'reserved'",
        ('settled', actual_micro, exceeded, timestamp, reservation_id))
    if cursor.rowcount != 1:
        raise RoutingError(f'Reservation {reservation_id!r} changed concurrently.')
    return _reservation(_fetch(connection, reservation_id))


def release(connection, config, reservation_id: str, *, now=None) -> dict:
    """Release a reserved experiment so its estimate stops being held.

    Only a reserved reservation can be released; releasing an already released
    reservation is idempotent, and a settled reservation cannot be released
    because its spending is already recorded.  Releasing never needs the budget
    limits, but the configuration is still validated so every mutation rejects
    malformed input identically.
    """
    _require_transaction(connection, 'release')
    reservation_id = identifier(reservation_id, 'reservation_id')
    _limits(config)
    timestamp = _timestamp(now)
    existing = _fetch(connection, reservation_id)
    if existing is None:
        raise RoutingError(f'Reservation {reservation_id!r} does not exist.')
    if existing[4] == 'released':
        return _reservation(existing)
    if existing[4] == 'settled':
        raise RoutingError(f'Reservation {reservation_id!r} is settled and cannot be released.')
    cursor = connection.execute(
        f'UPDATE {TABLE} SET status = ?, updated_at = ? '
        f"WHERE reservation_id = ? AND status = 'reserved'",
        ('released', timestamp, reservation_id))
    if cursor.rowcount != 1:
        raise RoutingError(f'Reservation {reservation_id!r} changed concurrently.')
    return _reservation(_fetch(connection, reservation_id))


def _require_transaction(connection, action: str) -> None:
    try:
        active = connection.in_transaction
    except AttributeError:
        active = False
    if active is not True:
        raise RoutingError(f'{action}() requires an active caller-owned transaction '
                           '(connection.in_transaction).')


def _fetch(connection, reservation_id: str):
    try:
        return connection.execute(f'{_SELECT} WHERE reservation_id = ?', (reservation_id,)).fetchone()
    except sqlite3.OperationalError as error:
        _reject_uninitialized(error)
        raise


def _aggregate(connection, month: str):
    totals = {'reserved_micro': 0, 'spent_micro': 0, 'exceeded': False}
    counts = {'reserved': 0, 'settled': 0, 'released': 0, 'total': 0}
    try:
        # Actual overruns must remain representable even when multiple valid
        # settlements sum beyond SQLite's signed 64-bit integer range.
        rows = connection.execute(
            f'SELECT status, reserved_micro, actual_micro, exceeded '
            f'FROM {TABLE} WHERE month = ?', (month,)).fetchall()
    except sqlite3.OperationalError as error:
        _reject_uninitialized(error)
        raise
    for state, reserved_micro, spent_micro, exceeded in rows:
        counts[state] += 1
        counts['total'] += 1
        if state == 'reserved':
            totals['reserved_micro'] += reserved_micro
        elif state == 'settled':
            totals['spent_micro'] += spent_micro
            totals['exceeded'] = totals['exceeded'] or bool(exceeded)
    return totals, counts


def _reject_uninitialized(error: sqlite3.OperationalError) -> None:
    """Turn a missing ledger into a routing error; let lock/busy errors surface."""
    if 'no such table' in str(error):
        raise RoutingError('Experiment budget ledger is not initialized.') from None


def _overrun(monthly_micro: int, totals: dict) -> bool:
    return bool(totals['exceeded']) or (monthly_micro > 0 and totals['spent_micro'] >= monthly_micro)


def _reservation(row) -> dict:
    _identifier, month, reserved_micro, actual_micro, state, _exceeded, created_at, updated_at = row
    return {
        'id': _identifier,
        'month': month,
        'reserved_usd': _usd(reserved_micro),
        'actual_usd': None if actual_micro is None else _usd(actual_micro),
        'status': state,
        'created_at': created_at,
        'updated_at': updated_at,
    }


def _limits(config):
    if not isinstance(config, dict):
        raise RoutingError('Experiment budget configuration must be a mapping.')
    return (_micro_floor(config.get('monthly_experiment_budget_usd', 0.),
                         'monthly_experiment_budget_usd'),
            _micro_floor(config.get('per_experiment_limit_usd', 0.),
                         'per_experiment_limit_usd'))


def _micro_value(value, name: str) -> Decimal:
    if type(value) not in (int, float) or not 0 <= value <= MAX_USD or not math.isfinite(value):
        raise RoutingError(f'{name} must be a finite number between 0 and {MAX_USD:g}.')
    return Decimal(str(value))


def _micro_ceil(value, name: str) -> int:
    return int(_micro_value(value, name).quantize(MICRO_QUANTUM, rounding=ROUND_CEILING)
               * MICRODOLLARS)


def _micro_floor(value, name: str) -> int:
    return int(_micro_value(value, name).quantize(MICRO_QUANTUM, rounding=ROUND_FLOOR)
               * MICRODOLLARS)


def _usd(micro: int) -> float:
    return micro / 1_000_000


def _timestamp(now) -> float:
    if now is None:
        return time.time()
    if isinstance(now, datetime.datetime):
        if now.tzinfo is None:
            now = now.replace(tzinfo=datetime.timezone.utc)
        now = now.timestamp()
    if type(now) not in (int, float) or not 0 <= now <= MAX_TIMESTAMP or not math.isfinite(now):
        raise RoutingError('now must be a finite UTC timestamp within the supported range.')
    return float(now)


def _month(timestamp: float) -> str:
    return time.strftime('%Y-%m', time.gmtime(timestamp))
