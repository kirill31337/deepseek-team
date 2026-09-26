"""Local learned-quality assessment for automatic delegation.

The learning unit is one coordinator-reviewed DeepSeek worker assignment, not a
whole prompt or session. Comparable task classes are pooled by exact runtime,
model and canonical effort plus the ``kind``/``domain``/``operation`` anchors,
using the same six soft-context similarity fields as the contextual forecast.
The per-task ``context_version`` is provenance only: it never fragments a class
and never blocks class-level learning.

Only local DeepSeek worker outcomes can build the veto. External snapshots,
coordinator results and neutral infrastructure/cancelled/unknown outcomes are
never DeepSeek quality failures, and a small or uncertain sample never vetoes
eligible work.
"""
from __future__ import annotations

from . import routing_estimator, routing_quality
from .routing_models import fingerprint, validate_config, validate_features

__all__ = ['IDENTITY', 'ANCHORS', 'SOFT_CONTEXT', 'assess_quality']

# Exact execution identity for quality comparison: context_version is absent.
IDENTITY = ('runtime', 'model', 'effort')
ANCHORS = tuple(routing_estimator._ANCHORS)
SOFT_CONTEXT = tuple(routing_estimator._SOFT_CONTEXT)


def _case_key(row):
    """One reviewed assignment is one case, regardless of context version."""
    return (row['origin'], row['case_id'], row['action'],
            *(row['features'][key] for key in IDENTITY))


def _case_outcomes(observations, now):
    """Yield the earliest labelled failure, or the first acceptance, per case.

    Mirrors :func:`routing_estimator._case_outcomes` while comparing the class
    identity above: repeated feedback, rework and resume events for one
    assignment stay one case, and a later acceptance can never launder the
    first labelled failure.
    """
    selected = {}
    order = lambda row: (row['observed_at'], row['outcome'] == 'accepted', row['id'])
    for row in sorted(observations, key=order):
        if row['observed_at'] > now or row['outcome'] not in routing_estimator._LABELLED:
            continue
        key = _case_key(row)
        previous = selected.get(key)
        if previous is None or (previous['outcome'] == 'accepted' and row['outcome'] != 'accepted'):
            selected[key] = row
    yield from sorted(selected.values(), key=order)


def _graded_cases(observations, now):
    """Group every known event of one assignment for graded quality resolution.

    Context versions are provenance here, so one assignment stays one case even
    when later feedback was recorded under a different context version.
    """
    grouped = {}
    for row in observations:
        if row['observed_at'] <= now:
            grouped.setdefault(_case_key(row), []).append(row)
    return grouped


def _similarity(features, other):
    """The existing contextual similarity without the context-version barrier."""
    if any(features[key] != other[key] for key in IDENTITY):
        return 0.
    if any(features[key] == 'unknown' or features[key] != other[key] for key in ANCHORS):
        return 0.
    matches = sum(features[key] == other[key] and features[key] != 'unknown'
                  for key in SOFT_CONTEXT)
    return matches / len(SOFT_CONTEXT)


def assess_quality(features, observations, config, *, now=None) -> dict:
    """Return the auditable local learned-quality assessment for one feature card."""
    card = validate_features(features)
    policy = validate_config(config)
    timestamp = routing_estimator._timestamp(now)
    canonical = routing_estimator._canonical_observations(observations)
    cases = _graded_cases(canonical, timestamp)
    weighted = []
    for row in _case_outcomes(canonical, timestamp):
        if row.get('origin') != 'local' or row.get('action') != 'worker':
            continue
        age = (timestamp - row['observed_at']) / 86400.
        if age > policy['max_evidence_age_days']:
            continue
        similarity = _similarity(card, row['features'])
        if similarity <= 0 or similarity < policy['min_similarity']:
            continue
        weight = similarity * row['reliability'] * 2. ** (-age / policy['half_life_days'])
        if weight <= 0:
            continue
        # Explicit graded quality replaces the case's legacy inference; a
        # neutral case supplies no success or failure mass at all.
        score = routing_quality.case_score(cases.get(_case_key(row), (row,)), row)
        if score is None:
            continue
        if score != (1.0 if row['outcome'] == 'accepted' else 0.0):
            row = dict(row, _score=score)
        weighted.append((row, weight))
    posterior = routing_estimator._posterior(weighted, policy)
    evidence_ids = posterior.get('evidence_ids', [])
    posterior.update(evidence_ids=evidence_ids[:128], evidence_count=len(evidence_ids),
                     evidence_digest=fingerprint(evidence_ids))
    minimum = policy['quality_min_evidence']
    threshold = policy['quality_min_success_probability']
    sufficient = posterior['local_effective'] >= minimum
    return {
        'posterior': posterior,
        'sufficient': sufficient,
        'veto': bool(sufficient and posterior['upper'] < threshold),
        'quality_min_evidence': minimum,
        'quality_min_success_probability': threshold,
        'comparison': {
            'basis': 'local_worker_quality_cases',
            'identity': list(IDENTITY),
            'anchors': list(ANCHORS),
            'soft_context': list(SOFT_CONTEXT),
            'context_version': 'ignored_for_class_learning',
            'confidence': policy['confidence'],
            'min_similarity': policy['min_similarity'],
            'half_life_days': policy['half_life_days'],
            'max_evidence_age_days': policy['max_evidence_age_days'],
        },
    }
