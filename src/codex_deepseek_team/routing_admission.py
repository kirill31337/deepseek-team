"""Single immediate admission path for automatic delegation.

This module replaces the removed bootstrap/recovery staging. There are no
stages, tickets, quotas, holdouts or compatibility scans: admission is decided
once, immediately, from the task feature card, the configured access and the
measured economics. Persistent state is limited to two tables: an immutable
binding of plan bindings to recorded decisions and one quality-failure
cooldown per task family.
"""
from __future__ import annotations

import time

from .routing_models import (
    DEFAULT_CONFIG,
    PROTECTED_KINDS,
    READ_KINDS,
    WRITE_KINDS,
    RoutingError,
    fingerprint,
    number,
    read_json,
    validate_features,
)

__all__ = [
    'FAMILY_FIELDS', 'REWORK_LIMIT', 'DEFAULT_FAILURE_COOLDOWN_SECONDS', 'SCHEMA',
    'ineligible_reason', 'manual_acceptance', 'economic_veto',
    'initialize', 'bound_decision', 'bind_decision', 'record_outcome',
    'cooling', 'status',
]

FAMILY_FIELDS = ('kind', 'domain', 'operation', 'runtime', 'model', 'effort', 'context_version')
REWORK_LIMIT = 3
DEFAULT_FAILURE_COOLDOWN_SECONDS = 300.0
MAX_FAILURE_COOLDOWN_SECONDS = 2592000.0

ELIGIBLE_KINDS = READ_KINDS | WRITE_KINDS
MANUAL_KINDS = READ_KINDS | frozenset(('documentation',))

SCHEMA = {
    'routing_decision_bindings': (
        'CREATE TABLE IF NOT EXISTS routing_decision_bindings ('
        'binding_id TEXT PRIMARY KEY, decision_id TEXT NOT NULL)'),
    'routing_admission_cooldowns': (
        'CREATE TABLE IF NOT EXISTS routing_admission_cooldowns ('
        'family_id TEXT PRIMARY KEY, failure_at REAL NOT NULL, cooldown_until REAL NOT NULL)'),
}


def initialize(conn) -> None:
    """Create only the current admission tables; the caller owns the transaction."""
    for statement in SCHEMA.values():
        conn.execute(statement)


def ineligible_reason(features, access):
    """Return a stable reason an item cannot be delegated, or ``None`` if it can."""
    card = validate_features(features)
    kind = card['kind']
    if kind in PROTECTED_KINDS:
        return 'protected_kind'
    if kind not in ELIGIBLE_KINDS:
        return 'ineligible_kind'
    if kind in WRITE_KINDS and access != 'full-access':
        return 'write_requires_full_access'
    if card['risk'] not in ('low', 'medium'):
        return 'ineligible_risk'
    if card['scope_size'] not in ('small', 'medium'):
        return 'ineligible_scope_size'
    if card['coupling'] not in ('local', 'component'):
        return 'ineligible_coupling'
    if card['localization'] not in ('known', 'partial'):
        return 'ineligible_localization'
    if card['clarity'] != 'clear':
        return 'ineligible_clarity'
    if card['verification'] == 'manual':
        if kind not in MANUAL_KINDS:
            return 'ineligible_verification'
    elif card['verification'] not in ('tests', 'reproducer'):
        return 'ineligible_verification'
    if card['domain'] == 'unknown':
        return 'ineligible_domain'
    if card['operation'] == 'unknown':
        return 'ineligible_operation'
    if card['context_version'] == 'unknown':
        return 'ineligible_context_version'
    if card['model'] != 'deepseek-flash':
        return 'ineligible_model'
    return None


def manual_acceptance(item):
    """Return whether an item may be accepted by a manual read/documentation check."""
    if not isinstance(item, dict):
        return False
    features = item.get('features')
    if not isinstance(features, dict):
        return False
    try:
        card = validate_features(features)
    except RoutingError:
        return False
    if card['kind'] not in MANUAL_KINDS or card['verification'] != 'manual':
        return False
    criteria = item.get('acceptance')
    return (isinstance(criteria, list)
            and any(isinstance(criterion, str) and criterion.strip() for criterion in criteria))


def _bounded(value, default):
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def economic_veto(decision, config):
    """Return whether measured costs are known and do not show enough savings.

    Missing cost evidence never vetoes and never implies savings.
    """
    economics = (decision or {}).get('economics') or {}
    settings = config if isinstance(config, dict) else {}
    support = max(1.0, _bounded(settings.get('min_local_evidence'), DEFAULT_CONFIG['min_local_evidence']))
    worker = economics.get('worker_cost_effective')
    coordinator = economics.get('coordinator_cost_effective')
    if worker is None or coordinator is None:
        return False
    try:
        known = float(worker) >= support and float(coordinator) >= support
    except (TypeError, ValueError):
        return False
    if not known:
        return False
    savings = economics.get('savings_fraction')
    if savings is None:
        return True
    try:
        return float(savings) < _bounded(settings.get('minimum_savings_fraction'),
                                         DEFAULT_CONFIG['minimum_savings_fraction'])
    except (TypeError, ValueError):
        return False


def _now(value):
    return number(time.time() if value is None else value, 'now')


def _family(features):
    card = validate_features(features)
    return tuple(card[field] for field in FAMILY_FIELDS)


def _family_id(features):
    return fingerprint(list(_family(features)))


def _cooldown_seconds(config):
    if not isinstance(config, dict):
        raise RoutingError('Admission configuration must be a mapping.')
    return number(config.get('failure_cooldown_seconds', DEFAULT_FAILURE_COOLDOWN_SECONDS),
                  'failure_cooldown_seconds', 0., MAX_FAILURE_COOLDOWN_SECONDS)


def bound_decision(conn, binding, features):
    """Return the recorded decision for ``binding``, or ``None`` when unbound."""
    row = conn.execute('SELECT decision_id FROM routing_decision_bindings WHERE binding_id=?',
                       (fingerprint(binding),)).fetchone()
    if row is None:
        return None
    raw = conn.execute('SELECT value FROM decisions WHERE id=?', (row[0],)).fetchone()
    if raw is None:
        raise RoutingError('Recorded decision binding has no decision.', 78)
    decision = read_json(raw[0])
    card = validate_features(features)
    if decision.get('binding') != binding or decision.get('features') != card:
        raise RoutingError('Features changed for an immutable decision binding.')
    return decision


def bind_decision(conn, decision):
    """Bind a decision to its binding idempotently; reject a conflicting decision."""
    binding = decision['binding']
    decision_id = decision['id']
    key = fingerprint(binding)
    row = conn.execute('SELECT decision_id FROM routing_decision_bindings WHERE binding_id=?',
                       (key,)).fetchone()
    if row is not None:
        if row[0] != decision_id:
            raise RoutingError('Binding already maps to a different decision.', 65)
        return None
    conn.execute('INSERT INTO routing_decision_bindings(binding_id, decision_id) VALUES(?, ?)',
                 (key, decision_id))
    return None


def _start_cooldown(conn, family_id, failure_at, cooldown_until):
    conn.execute(
        'INSERT INTO routing_admission_cooldowns(family_id, failure_at, cooldown_until) '
        'VALUES(?, ?, ?) ON CONFLICT(family_id) DO UPDATE SET '
        'failure_at = MAX(failure_at, excluded.failure_at), '
        'cooldown_until = MAX(cooldown_until, excluded.cooldown_until)',
        (family_id, failure_at, cooldown_until))


def record_outcome(conn, row, observations, config):
    """Start or extend a family cooldown for a local worker quality failure.

    A local worker rejection pauses immediately. Rework pauses only once at
    least :data:`REWORK_LIMIT` distinct local worker rework cases appear in the
    same task family within the trailing cooldown window. Acceptance and
    neutral outcomes never pause and never clear an existing pause. Timestamps
    always come from the observation, never from the wall clock, and the
    monotonic upsert cannot shorten an existing pause. Returns whether a pause
    was recorded.
    """
    if row.get('origin') != 'local' or row.get('action') != 'worker':
        return False
    outcome = row.get('outcome')
    if outcome not in ('rework', 'rejected'):
        return False
    seconds = _cooldown_seconds(config)
    timestamp = number(row['observed_at'], 'observed_at')
    triggered = outcome == 'rejected'
    if not triggered:
        family = _family(row['features'])
        start = timestamp - seconds
        cases = set()
        for item in observations:
            if (item.get('origin') != 'local' or item.get('action') != 'worker'
                    or item.get('outcome') != 'rework'):
                continue
            at = item.get('observed_at')
            if not isinstance(at, (int, float)) or at < start or at > timestamp:
                continue
            if _family(item['features']) != family:
                continue
            cases.add(item['case_id'])
            if len(cases) >= REWORK_LIMIT:
                break
        triggered = len(cases) >= REWORK_LIMIT
    if not triggered:
        return False
    _start_cooldown(conn, _family_id(row['features']), timestamp, timestamp + seconds)
    return True


def cooling(conn, features, now=None):
    """Return whether the task family is currently in a quality-failure cooldown."""
    timestamp = _now(now)
    row = conn.execute('SELECT cooldown_until FROM routing_admission_cooldowns WHERE family_id=?',
                       (_family_id(features),)).fetchone()
    return row is not None and float(row[0]) > timestamp


def status(conn, now=None):
    """Return the active quality-failure cooldowns without prompts or secrets."""
    timestamp = _now(now)
    rows = conn.execute(
        'SELECT family_id, failure_at, cooldown_until FROM routing_admission_cooldowns '
        'WHERE cooldown_until > ? ORDER BY family_id', (timestamp,)).fetchall()
    return {'active_cooldowns': [{'family_id': r[0], 'failure_at': r[1], 'cooldown_until': r[2]}
                                 for r in rows]}
