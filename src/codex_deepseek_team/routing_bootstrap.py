"""Automatic learning stages and durable bootstrap admission metadata.

This module never launches work. The recovery scheduler owns the common ticket
lifecycle; bootstrap only changes admission frequency, not worker permissions.
"""
from __future__ import annotations

from .routing_models import RoutingError, fingerprint, read_json, validate_config

MAX_ACTIVE_TRIALS = 3
COORDINATOR_COMPARISON_INTERVAL = 10
MIN_FAILURE_EVIDENCE = .5

SCHEMA = {
    'routing_bootstrap_state': (
        'CREATE TABLE IF NOT EXISTS routing_bootstrap_state ('
        'bucket_id TEXT PRIMARY KEY, opportunity_count INTEGER NOT NULL)'),
    'routing_bootstrap_tickets': (
        'CREATE TABLE IF NOT EXISTS routing_bootstrap_tickets ('
        'ticket_id TEXT PRIMARY KEY)'),
    'routing_decision_bindings': (
        'CREATE TABLE IF NOT EXISTS routing_decision_bindings ('
        'binding_id TEXT PRIMARY KEY, decision_id TEXT NOT NULL)'),
}


def initialize(conn):
    """Add optional schema and index legacy bound decisions once, atomically."""
    existed = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                           "AND name='routing_decision_bindings'").fetchone()
    for statement in SCHEMA.values():
        conn.execute(statement)
    if not existed and conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                                    "AND name='decisions'").fetchone():
        rows = [read_json(row[0]) for row in conn.execute('SELECT value FROM decisions')]
        for decision in sorted(rows, key=lambda d: (d['created_at'], d['id'])):
            if decision.get('binding'):
                conn.execute('INSERT OR IGNORE INTO routing_decision_bindings VALUES (?, ?)',
                             (fingerprint(decision['binding']), decision['id']))
        # Before automatic stages, only ticketed recovery failures created a
        # cooldown. Preserve the timestamps of ordinary/manual legacy failures
        # rather than treating the upgrade as a new incident.
        from . import routing_recovery
        saved = conn.execute("SELECT value FROM metadata WHERE key='config'").fetchone()
        config = validate_config(read_json(saved[0]) if saved else {})
        for (raw,) in conn.execute('SELECT value FROM observations'):
            row = read_json(raw)
            if row['origin'] == 'local' and row['action'] == 'worker' and row['outcome'] in ('rework', 'rejected'):
                routing_recovery.record_failure(conn, row['features'], config, now=row['observed_at'])


def bound_decision(conn, binding, features):
    row = conn.execute('SELECT decision_id FROM routing_decision_bindings WHERE binding_id=?',
                       (fingerprint(binding),)).fetchone()
    if row is None:
        return None
    raw = conn.execute('SELECT value FROM decisions WHERE id=?', (row[0],)).fetchone()
    if raw is None:
        raise RoutingError('Recorded plan binding has no decision.', 78)
    decision = read_json(raw[0])
    if decision['binding'] != binding or decision['features'] != features:
        raise RoutingError('Features changed for an immutable plan decision binding.')
    return decision


def bind_decision(conn, decision):
    conn.execute('INSERT OR IGNORE INTO routing_decision_bindings VALUES (?, ?)',
                 (fingerprint(decision['binding']), decision['id']))


def assess(decision, config):
    """Separate lack of evidence, supported quality and actual local failures."""
    posterior, economics = decision['posterior'], decision['economics']
    supported = (posterior.get('matched_local', 0) > 0
                 and posterior.get('local_effective', 0) >= config['min_local_evidence']
                 and posterior.get('lower', 0) >= config['min_success_probability'])
    failure = posterior.get('local_failure_effective', 0)
    phase = 'adaptive' if supported else 'recovery' if failure >= MIN_FAILURE_EVIDENCE else 'bootstrap'
    support = max(1., config['min_local_evidence'])
    cost_known = all(economics.get(action + '_cost_effective', 0) >= support
                     for action in ('worker', 'coordinator'))
    savings = economics.get('savings_fraction')
    return {'phase': phase, 'quality_supported': supported,
            'local_failure_effective': failure, 'cost_supported': cost_known,
            'economic_veto': cost_known and (savings is None or savings < config['minimum_savings_fraction'])}


def opportunity(conn, bucket):
    conn.execute('INSERT INTO routing_bootstrap_state VALUES (?, 1) ON CONFLICT(bucket_id) '
                 'DO UPDATE SET opportunity_count=opportunity_count+1', (bucket,))
    return conn.execute('SELECT opportunity_count FROM routing_bootstrap_state WHERE bucket_id=?',
                        (bucket,)).fetchone()[0]
