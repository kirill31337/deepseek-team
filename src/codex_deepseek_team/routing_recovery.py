"""Persistent trial scheduler for regular low-risk tasks.

The scheduler exists so that adaptive delegation cannot collapse permanently
after a negative worker rating.  It selects at most one small, low-risk,
fully specified fallback case at a time and then applies a global project-wide
quota of one selection per ``ceil(1 / recovery_rate)`` distinct eligible
binding opportunities. Bootstrap shares this lifecycle with up to three total
trials, no recovery stride and a periodic coordinator comparison.
It never widens caller access, never retries a paid
call and never re-rolls a task that already produced an immutable result.

Contract
--------
* :data:`SCHEMA` maps every created table/index name to its ``CREATE`` SQL.
  Names are prefixed ``routing_recovery``; :func:`initialize` creates them plus
  bootstrap metadata and never commits.
* :func:`consider`, :func:`mark_started`, :func:`finish` and :func:`release`
  mutate the caller's database and therefore require a caller-owned
  ``BEGIN IMMEDIATE`` transaction.  The module never begins, commits or rolls
  back a transaction.  :func:`status` is read-only and needs no transaction.
* Only closed metadata (hashes, opaque ids, closed reason codes, timestamps)
  is stored or returned; raw tasks, prompts, code or paths are never recorded.
"""
from __future__ import annotations

import time
import uuid
from decimal import Decimal

from . import routing_bootstrap

from .routing_models import (
    READ_KINDS,
    WRITE_KINDS,
    RoutingError,
    fingerprint,
    identifier,
    number,
    validate_features,
)

__all__ = [
    'SCHEMA', 'PENDING_TTL_SECONDS', 'MAX_CONSIDERED_BINDINGS',
    'DEFAULT_RECOVERY_RATE', 'MAX_RECOVERY_RATE',
    'DEFAULT_RECOVERY_COOLDOWN_SECONDS', 'MAX_RECOVERY_COOLDOWN_SECONDS',
    'ELIGIBLE_KINDS', 'initialize', 'consider', 'mark_started', 'finish',
    'release', 'status',
]

PENDING_TTL_SECONDS = 3600.0
MAX_CONSIDERED_BINDINGS = 50000
DEFAULT_RECOVERY_RATE = 0.1
MAX_RECOVERY_RATE = 0.25
DEFAULT_RECOVERY_COOLDOWN_SECONDS = 3600.0
MAX_RECOVERY_COOLDOWN_SECONDS = 2592000.0

ELIGIBLE_KINDS = WRITE_KINDS | READ_KINDS

STATUS_PENDING = 'pending'
STATUS_RUNNING = 'running'
STATUS_RESOLVED = 'resolved'
STATUS_RELEASED = 'released'
STATUS_EXPIRED = 'expired'
ACTIVE_STATUSES = (STATUS_PENDING, STATUS_RUNNING)

OUTCOME_ACCEPTED = 'accepted'
OUTCOME_REWORK = 'rework'
OUTCOME_REJECTED = 'rejected'
OUTCOME_INFRASTRUCTURE = 'infrastructure'
OUTCOME_CANCELLED = 'cancelled'
OUTCOME_UNKNOWN = 'unknown'
VALID_OUTCOMES = (OUTCOME_ACCEPTED, OUTCOME_REWORK, OUTCOME_REJECTED,
                  OUTCOME_INFRASTRUCTURE, OUTCOME_CANCELLED, OUTCOME_UNKNOWN)

# Quality labels supersede missing labels; failures always dominate acceptance.
_OUTCOME_RANK = {
    OUTCOME_ACCEPTED: 1,
    OUTCOME_INFRASTRUCTURE: 0,
    OUTCOME_CANCELLED: 0,
    OUTCOME_UNKNOWN: 0,
    OUTCOME_REWORK: 2,
    OUTCOME_REJECTED: 3,
}

BINDING_FIELDS = ('task_id', 'deliverable_id', 'plan_hash')

SCHEMA = {
    'routing_recovery_state': (
        'CREATE TABLE IF NOT EXISTS routing_recovery_state ('
        'id INTEGER PRIMARY KEY CHECK (id = 1), '
        'opportunity_count INTEGER NOT NULL DEFAULT 0, '
        'last_selection_ordinal INTEGER NOT NULL DEFAULT 0, '
        'updated_at REAL)'),
    'routing_recovery_bindings': (
        'CREATE TABLE IF NOT EXISTS routing_recovery_bindings ('
        'binding_id TEXT PRIMARY KEY, '
        'features_fingerprint TEXT NOT NULL, '
        'bucket_id TEXT NOT NULL, '
        'ordinal INTEGER NOT NULL, '
        'selected INTEGER NOT NULL, '
        'ticket_id TEXT, '
        'reason TEXT NOT NULL, '
        'created_at REAL NOT NULL)'),
    'routing_recovery_tickets': (
        'CREATE TABLE IF NOT EXISTS routing_recovery_tickets ('
        'ticket_id TEXT PRIMARY KEY, '
        'binding_id TEXT NOT NULL, '
        'bucket_id TEXT NOT NULL, '
        'status TEXT NOT NULL, '
        'outcome TEXT, '
        'created_at REAL NOT NULL, '
        'expires_at REAL NOT NULL, '
        'started_at REAL, '
        'closed_at REAL, '
        'cooldown_seconds REAL NOT NULL)'),
    'routing_recovery_cooldowns': (
        'CREATE TABLE IF NOT EXISTS routing_recovery_cooldowns ('
        'bucket_id TEXT PRIMARY KEY, '
        'failure_at REAL NOT NULL, '
        'cooldown_until REAL NOT NULL)'),
    'routing_recovery_idx_bindings_bucket': (
        'CREATE INDEX IF NOT EXISTS routing_recovery_idx_bindings_bucket '
        'ON routing_recovery_bindings(bucket_id)'),
    'routing_recovery_idx_tickets_status': (
        'CREATE INDEX IF NOT EXISTS routing_recovery_idx_tickets_status '
        'ON routing_recovery_tickets(status)'),
    'routing_recovery_idx_tickets_bucket': (
        'CREATE INDEX IF NOT EXISTS routing_recovery_idx_tickets_bucket '
        'ON routing_recovery_tickets(bucket_id)'),
}


def initialize(conn) -> None:
    """Create the recovery schema on ``conn`` without committing."""
    for statement in SCHEMA.values():
        conn.execute(statement)
    routing_bootstrap.initialize(conn)


def consider(conn, config, features, binding, *, now=None, admission='recovery') -> dict:
    """Consider one candidate binding and maybe open a recovery ticket.

    Returns ``{'selected': bool, 'reason': str, 'ticket_id': str | None}``.
    The call is a pure read for disabled, guarded or ineligible inputs and only
    mutates the caller's transaction for a fresh eligible binding.
    """
    timestamp = _epoch(now)
    if admission not in ('bootstrap', 'recovery'):
        raise RoutingError('Unknown automatic admission stage.')
    recovery = _recovery_config(config)
    validated = validate_features(features)
    binding_key = _binding_key(binding)
    features_key = fingerprint(validated)

    stored = conn.execute(
        'SELECT features_fingerprint, selected, ticket_id, reason '
        'FROM routing_recovery_bindings WHERE binding_id = ?',
        (binding_key,)).fetchone()
    if stored is not None:
        if stored[0] != features_key:
            raise RoutingError('Features changed for an already considered binding.')
        return {'selected': bool(stored[1]), 'reason': stored[3], 'ticket_id': stored[2]}

    guard = _guard_reason(config)
    if guard is not None:
        return _not_selected(guard)
    if config.get('access') == 'read-only' and validated['kind'] in WRITE_KINDS:
        return _not_selected('guard_access')
    if admission == 'recovery' and recovery['recovery_rate'] <= 0.0:
        return _not_selected('disabled')
    ineligible = _ineligible_reason(validated)
    if ineligible is not None:
        return _not_selected(ineligible)

    _require_transaction(conn)
    _expire_pending(conn, timestamp)
    considered = conn.execute(
        'SELECT COUNT(*) FROM routing_recovery_bindings').fetchone()[0]
    if considered >= MAX_CONSIDERED_BINDINGS:
        raise RoutingError('Considered binding limit reached.')

    bucket = _bucket_key(validated)
    opportunity, last_selection = _load_counters(conn)
    ordinal = (routing_bootstrap.opportunity(conn, bucket) if admission == 'bootstrap'
               else opportunity + 1)
    due = (True if admission == 'bootstrap' else
           _selection_due(ordinal, last_selection, recovery['recovery_rate']))
    active = conn.execute(
        'SELECT COUNT(*), SUM(CASE WHEN b.ticket_id IS NULL THEN 1 ELSE 0 END) '
        'FROM routing_recovery_tickets t LEFT JOIN routing_bootstrap_tickets b '
        'ON b.ticket_id=t.ticket_id WHERE t.status IN (?, ?)', ACTIVE_STATUSES).fetchone()
    cooling = _bucket_cooling(conn, bucket, timestamp)

    if not due:
        reason = 'quota'
    elif active[0] >= routing_bootstrap.MAX_ACTIVE_TRIALS:
        reason = 'capacity'
    elif admission == 'recovery' and active[1]:
        reason = 'active_ticket'
    elif cooling:
        reason = 'bucket_cooldown'
    elif admission == 'bootstrap' and ordinal % routing_bootstrap.COORDINATOR_COMPARISON_INTERVAL == 0:
        reason = 'coordinator_comparison'
    else:
        reason = 'selected'
    selected = reason == 'selected'

    ticket_id = None
    if selected:
        ticket_id = uuid.uuid4().hex
        conn.execute(
            'INSERT INTO routing_recovery_tickets(ticket_id, binding_id, bucket_id, status, '
            'outcome, created_at, expires_at, started_at, closed_at, cooldown_seconds) '
            'VALUES(?, ?, ?, ?, NULL, ?, ?, NULL, NULL, ?)',
            (ticket_id, binding_key, bucket, STATUS_PENDING, timestamp,
             timestamp + PENDING_TTL_SECONDS, recovery['recovery_cooldown_seconds']))
        if admission == 'bootstrap':
            conn.execute('INSERT INTO routing_bootstrap_tickets VALUES (?)', (ticket_id,))
        else:
            last_selection = ordinal

    conn.execute(
        'INSERT INTO routing_recovery_bindings(binding_id, features_fingerprint, bucket_id, '
        'ordinal, selected, ticket_id, reason, created_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?)',
        (binding_key, features_key, bucket, ordinal, 1 if selected else 0,
         ticket_id, reason, timestamp))
    if admission == 'recovery':
        _save_counters(conn, ordinal, last_selection, timestamp)
    return {'selected': selected, 'reason': reason, 'ticket_id': ticket_id}


def mark_started(conn, ticket_id, *, now=None) -> dict:
    """Transition a selected pending ticket to running (idempotent)."""
    timestamp = _epoch(now)
    _require_transaction(conn)
    identifier(ticket_id, 'ticket_id')
    _expire_pending(conn, timestamp)
    row = conn.execute(
        'SELECT status, bucket_id FROM routing_recovery_tickets WHERE ticket_id = ?',
        (ticket_id,)).fetchone()
    if row is None:
        raise RoutingError('Unknown recovery ticket.')
    status = row[0]
    if status == STATUS_RUNNING:
        return {'ticket_id': ticket_id, 'status': status, 'started': True,
                'reason': 'idempotent'}
    if status == STATUS_PENDING:
        if _bucket_cooling(conn, row[1], timestamp):
            raise RoutingError('Task family is in a quality failure cooldown; inspect and replan later.', 78)
        conn.execute(
            'UPDATE routing_recovery_tickets SET status = ?, started_at = ? WHERE ticket_id = ?',
            (STATUS_RUNNING, timestamp, ticket_id))
        return {'ticket_id': ticket_id, 'status': STATUS_RUNNING, 'started': True,
                'reason': 'started'}
    return {'ticket_id': ticket_id, 'status': status, 'started': False,
            'reason': status}


def finish(conn, ticket_id, outcome, *, now=None, start_cooldown=True) -> dict:
    """Resolve a selected ticket, allowing monotonic corrections to failures."""
    timestamp = _epoch(now)
    _require_transaction(conn)
    identifier(ticket_id, 'ticket_id')
    if outcome not in VALID_OUTCOMES:
        raise RoutingError('Invalid recovery outcome.')
    row = conn.execute(
        'SELECT status, outcome, bucket_id, cooldown_seconds '
        'FROM routing_recovery_tickets WHERE ticket_id = ?',
        (ticket_id,)).fetchone()
    if row is None:
        raise RoutingError('Unknown recovery ticket.')
    status, current, bucket, cooldown_seconds = row[0], row[1], row[2], row[3]

    if status in (STATUS_RELEASED, STATUS_EXPIRED):
        return {'ticket_id': ticket_id, 'status': status, 'outcome': current,
                'labelled': _is_failure(current), 'changed': False, 'reason': 'not_open'}

    if status == STATUS_RESOLVED:
        old_rank = _OUTCOME_RANK.get(current, -1)
        new_rank = _OUTCOME_RANK[outcome]
        if new_rank > old_rank:
            conn.execute('UPDATE routing_recovery_tickets SET outcome = ? WHERE ticket_id = ?',
                         (outcome, ticket_id))
            if start_cooldown and _is_failure(outcome):
                _start_cooldown(conn, bucket, cooldown_seconds, timestamp)
            return {'ticket_id': ticket_id, 'status': status, 'outcome': outcome,
                    'labelled': _is_failure(outcome), 'changed': True, 'reason': 'corrected'}
        reason = 'idempotent' if new_rank == old_rank else 'retained'
        return {'ticket_id': ticket_id, 'status': status, 'outcome': current,
                'labelled': _is_failure(current), 'changed': False, 'reason': reason}

    conn.execute(
        'UPDATE routing_recovery_tickets SET status = ?, outcome = ?, closed_at = ? '
        'WHERE ticket_id = ?', (STATUS_RESOLVED, outcome, timestamp, ticket_id))
    if start_cooldown and _is_failure(outcome):
        _start_cooldown(conn, bucket, cooldown_seconds, timestamp)
    return {'ticket_id': ticket_id, 'status': STATUS_RESOLVED, 'outcome': outcome,
            'labelled': _is_failure(outcome), 'changed': True, 'reason': 'resolved'}


def release(conn, ticket_id, *, now=None) -> dict:
    """Cancel an unused pending ticket; running tickets are never released."""
    timestamp = _epoch(now)
    _require_transaction(conn)
    identifier(ticket_id, 'ticket_id')
    row = conn.execute(
        'SELECT status FROM routing_recovery_tickets WHERE ticket_id = ?',
        (ticket_id,)).fetchone()
    if row is None:
        return {'ticket_id': ticket_id, 'status': None, 'released': False,
                'reason': 'unknown'}
    status = row[0]
    if status == STATUS_PENDING:
        conn.execute(
            'UPDATE routing_recovery_tickets SET status = ?, closed_at = ? WHERE ticket_id = ?',
            (STATUS_RELEASED, timestamp, ticket_id))
        return {'ticket_id': ticket_id, 'status': STATUS_RELEASED, 'released': True,
                'reason': 'released'}
    if status == STATUS_RELEASED:
        return {'ticket_id': ticket_id, 'status': STATUS_RELEASED, 'released': True,
                'reason': 'already_released'}
    return {'ticket_id': ticket_id, 'status': status, 'released': False,
            'reason': status}


def status(conn, config, *, now=None) -> dict:
    """Return read-only scheduler counters and the single active slot."""
    timestamp = _epoch(now)
    recovery = _recovery_config(config)
    opportunity = _load_counters(conn)[0]
    selected_count = conn.execute(
        'SELECT COUNT(*) FROM routing_recovery_tickets').fetchone()[0]
    running = conn.execute(
        'SELECT COUNT(*) FROM routing_recovery_tickets WHERE status = ?',
        (STATUS_RUNNING,)).fetchone()[0]
    pending_rows = conn.execute(
        'SELECT ticket_id, binding_id, bucket_id, status, created_at, expires_at, started_at '
        'FROM routing_recovery_tickets WHERE status = ? AND expires_at > ? '
        'ORDER BY created_at, ticket_id', (STATUS_PENDING, timestamp)).fetchall()
    running_rows = conn.execute(
        'SELECT ticket_id, binding_id, bucket_id, status, created_at, expires_at, started_at '
        'FROM routing_recovery_tickets WHERE status = ? '
        'ORDER BY created_at, ticket_id', (STATUS_RUNNING,)).fetchall()
    tickets = [{'ticket_id': r[0], 'binding_id': r[1], 'bucket_id': r[2], 'status': r[3],
                'created_at': r[4], 'expires_at': r[5], 'started_at': r[6]}
               for r in list(pending_rows) + list(running_rows)]
    bootstrap_ids = {r[0] for r in conn.execute('SELECT ticket_id FROM routing_bootstrap_tickets')}
    for ticket in tickets:
        ticket['admission'] = 'bootstrap' if ticket['ticket_id'] in bootstrap_ids else 'recovery'
    return {
        'eligible_seen': opportunity,
        'selected_count': selected_count,
        'running_count': running,
        'pending_count': len(pending_rows),
        'bootstrap_pending_count': sum(t['admission'] == 'bootstrap' and t['status'] == STATUS_PENDING for t in tickets),
        'bootstrap_running_count': sum(t['admission'] == 'bootstrap' and t['status'] == STATUS_RUNNING for t in tickets),
        'recovery_active_count': sum(t['admission'] == 'recovery' for t in tickets),
        'max_active_trials': routing_bootstrap.MAX_ACTIVE_TRIALS,
        'coordinator_comparison_interval': routing_bootstrap.COORDINATOR_COMPARISON_INTERVAL,
        'tickets': tickets,
        'recovery_rate': recovery['recovery_rate'],
        'cooldown_seconds': recovery['recovery_cooldown_seconds'],
    }


def _not_selected(reason: str) -> dict:
    return {'selected': False, 'reason': reason, 'ticket_id': None}


def _epoch(value) -> float:
    return number(time.time() if value is None else value, 'now')


def _bounded_number(value, name, minimum, maximum) -> float:
    return number(value, name, minimum, maximum)


def _recovery_config(config) -> dict:
    if not isinstance(config, dict):
        raise RoutingError('Recovery configuration must be a mapping.')
    rate = _bounded_number(config.get('recovery_rate', DEFAULT_RECOVERY_RATE),
                           'recovery_rate', 0.0, MAX_RECOVERY_RATE)
    cooldown = _bounded_number(
        config.get('recovery_cooldown_seconds', DEFAULT_RECOVERY_COOLDOWN_SECONDS),
        'recovery_cooldown_seconds', 0.0, MAX_RECOVERY_COOLDOWN_SECONDS)
    return {'recovery_rate': rate, 'recovery_cooldown_seconds': cooldown}


def _guard_reason(config):
    if 'mode' in config and config['mode'] != 'auto':
        return 'guard_mode'
    if 'profile' in config and config['profile'] != 'auto':
        return 'guard_profile'
    if 'access' in config and config['access'] not in ('read-only', 'full-access'):
        return 'guard_access'
    for key in ('recommendation', 'decision'):
        if key in config and config[key] not in ('abstain', 'coordinator'):
            return 'guard_recommendation'
    return None


def _ineligible_reason(features):
    if features['risk'] != 'low':
        return 'ineligible_risk'
    if features['scope_size'] != 'small':
        return 'ineligible_scope_size'
    if features['coupling'] != 'local':
        return 'ineligible_coupling'
    if features['localization'] != 'known':
        return 'ineligible_localization'
    if features['clarity'] != 'clear':
        return 'ineligible_clarity'
    if features['verification'] not in ('tests', 'reproducer'):
        return 'ineligible_verification'
    if features['domain'] == 'unknown':
        return 'ineligible_domain'
    if features['operation'] == 'unknown':
        return 'ineligible_operation'
    if features['kind'] not in ELIGIBLE_KINDS:
        return 'ineligible_kind'
    if features['model'] != 'deepseek-flash':
        return 'ineligible_model'
    if features['context_version'] == 'unknown':
        return 'ineligible_context_version'
    return None


def _binding_key(binding) -> str:
    if not isinstance(binding, dict) or set(binding) != set(BINDING_FIELDS):
        raise RoutingError('Binding must contain exactly task_id, deliverable_id and plan_hash.')
    for field in BINDING_FIELDS:
        identifier(binding[field], field)
    return fingerprint({field: binding[field] for field in BINDING_FIELDS})


def _bucket_key(features) -> str:
    return fingerprint({
        'runtime': features['runtime'],
        'model': features['model'],
        'effort': features['effort'],
        'context_version': features['context_version'],
        'kind': features['kind'],
        'domain': features['domain'],
        'operation': features['operation'],
    })


def _stride(rate: float) -> int:
    # Preserve the configured decimal rate without reciprocal overflow or an
    # epsilon that silently increases the allowed exploration quota.
    numerator, denominator = Decimal(str(rate)).as_integer_ratio()
    return (denominator + numerator - 1) // numerator


def _selection_due(ordinal: int, last_selection: int, rate: float) -> bool:
    if last_selection == 0:
        return True
    return ordinal - last_selection >= _stride(rate)


def _require_transaction(conn) -> None:
    in_transaction = getattr(conn, 'in_transaction', None)
    if in_transaction is not True:
        raise RoutingError('A caller-owned BEGIN IMMEDIATE transaction is required.')


def _expire_pending(conn, timestamp: float) -> None:
    conn.execute(
        'UPDATE routing_recovery_tickets SET status = ?, closed_at = ? '
        'WHERE status = ? AND expires_at <= ?',
        (STATUS_EXPIRED, timestamp, STATUS_PENDING, timestamp))


def _bucket_cooling(conn, bucket: str, timestamp: float) -> bool:
    row = conn.execute(
        'SELECT cooldown_until FROM routing_recovery_cooldowns WHERE bucket_id = ?',
        (bucket,)).fetchone()
    return row is not None and row[0] > timestamp


def cooling(conn, features, *, now=None):
    return _bucket_cooling(conn, _bucket_key(features), _epoch(now))


def record_failure(conn, features, config, *, now=None):
    _require_transaction(conn)
    _start_cooldown(conn, _bucket_key(features), _recovery_config(config)['recovery_cooldown_seconds'], _epoch(now))


def _start_cooldown(conn, bucket: str, seconds: float, timestamp: float) -> None:
    conn.execute(
        'INSERT INTO routing_recovery_cooldowns(bucket_id, failure_at, cooldown_until) '
        'VALUES(?, ?, ?) ON CONFLICT(bucket_id) DO UPDATE SET '
        'failure_at = MAX(failure_at, excluded.failure_at), '
        'cooldown_until = MAX(cooldown_until, excluded.cooldown_until)',
        (bucket, timestamp, timestamp + seconds))


def _is_failure(outcome) -> bool:
    return outcome in (OUTCOME_REWORK, OUTCOME_REJECTED)


def _load_counters(conn):
    row = conn.execute(
        'SELECT opportunity_count, last_selection_ordinal '
        'FROM routing_recovery_state WHERE id = 1').fetchone()
    if row is None:
        return 0, 0
    return int(row[0]), int(row[1])


def _save_counters(conn, opportunity: int, last_selection: int, timestamp: float) -> None:
    conn.execute(
        'INSERT INTO routing_recovery_state(id, opportunity_count, last_selection_ordinal, updated_at) '
        'VALUES(1, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET '
        'opportunity_count = excluded.opportunity_count, '
        'last_selection_ordinal = excluded.last_selection_ordinal, '
        'updated_at = excluded.updated_at',
        (opportunity, last_selection, timestamp))
