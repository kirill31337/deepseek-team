"""Closed, credential-free contracts for hybrid delegation evidence."""
from __future__ import annotations

import hashlib
import json
import math
import re
import time


class RoutingError(Exception):
    def __init__(self, message: str, code: int = 64):
        super().__init__(message)
        self.message, self.code = message, code


PROTECTED_KINDS = frozenset(('architecture', 'security', 'integration', 'final_verification',
                            'commit_push', 'production', 'secret_signing'))
WRITE_KINDS = frozenset(('implementation', 'test', 'fixture', 'documentation', 'metadata'))
READ_KINDS = frozenset(('review', 'research', 'diagnostic', 'test_plan'))
FEATURE_VALUES = {
    'kind': WRITE_KINDS | READ_KINDS | PROTECTED_KINDS,
    'domain': ('python', 'javascript', 'typescript', 'java', 'go', 'rust', 'shell', 'documentation', 'other', 'unknown'),
    'operation': ('diagnose', 'fix', 'extend', 'refactor', 'test', 'document', 'review', 'unknown'),
    'localization': ('known', 'partial', 'unknown'),
    'coupling': ('local', 'component', 'cross-component', 'unknown'),
    'verification': ('reproducer', 'tests', 'manual', 'none', 'unknown'),
    'clarity': ('clear', 'partial', 'unknown'),
    'risk': ('low', 'medium', 'high', 'protected', 'unknown'),
    'scope_size': ('small', 'medium', 'large', 'unknown'),
    'runtime': ('codex', 'claude'),
    'effort': ('low', 'medium', 'high'),
}
FEATURE_DEFAULTS = dict.fromkeys(FEATURE_VALUES, 'unknown')
FEATURE_DEFAULTS.update(kind='implementation', runtime='codex', effort='medium',
                        model='deepseek-flash', context_version='default')
DEFAULT_CONFIG = {
    'mode': 'auto', 'confidence': .95,
    'min_local_evidence': 5., 'external_weight_cap': 5., 'source_weight_cap': 2.,
    'half_life_days': 90., 'max_evidence_age_days': 365., 'min_similarity': .6,
    'minimum_savings_fraction': .1,
    'monthly_experiment_budget_usd': 0., 'per_experiment_limit_usd': 0.,
    'failure_cooldown_seconds': 300.,
}
MAX_JSON_BYTES = 8 * 1024 * 1024
MAX_OBSERVATIONS = 50000


def identifier(value, name='identifier') -> str:
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}', value):
        raise RoutingError(f'{name} must be a nonempty identifier of at most 128 characters.')
    return value


def number(value, name, minimum=0., maximum=1e12) -> float:
    if type(value) not in (int, float) or not minimum <= value <= maximum or not math.isfinite(value):
        raise RoutingError(f'{name} must be a finite number between {minimum:g} and {maximum:g}.')
    return float(value)


def validate_features(value: dict) -> dict:
    if not isinstance(value, dict) or set(value) - set(FEATURE_DEFAULTS):
        raise RoutingError('Feature card contains unsupported fields; never include prompts, code or paths.')
    result = dict(FEATURE_DEFAULTS, **value)
    for key, allowed in FEATURE_VALUES.items():
        if not isinstance(result[key], str) or result[key] not in allowed:
            raise RoutingError(f'Invalid feature {key}.')
    for key in ('model', 'context_version'):
        identifier(result[key], key)
    return result


def validate_config(value: dict, base=None) -> dict:
    if not isinstance(value, dict) or set(value) - set(DEFAULT_CONFIG):
        raise RoutingError('Unknown routing configuration field.')
    result = dict(DEFAULT_CONFIG)
    if base is not None:
        result.update(validate_config(base))
    result.update(value)
    if result['mode'] not in ('off', 'shadow', 'advisory', 'auto'):
        raise RoutingError('Routing mode must be off, shadow, advisory or auto.')
    for key in set(DEFAULT_CONFIG) - {'mode'}:
        result[key] = number(result[key], key)
    if not 0 < result['confidence'] < 1:
        raise RoutingError('confidence must be strictly between zero and one.')
    for key in ('min_similarity', 'minimum_savings_fraction'):
        if not 0 <= result[key] <= 1:
            raise RoutingError(f'{key} must be between zero and one.')
    for key in ('half_life_days', 'max_evidence_age_days'):
        if result[key] <= 0:
            raise RoutingError(f'{key} must be positive.')
    for key in ('external_weight_cap', 'source_weight_cap', 'min_local_evidence'):
        if result[key] > 10000:
            raise RoutingError(f'{key} exceeds the supported evidence bound.')
    if result['failure_cooldown_seconds'] > 2592000:
        raise RoutingError('Failure cooldown is limited to 0..2592000 seconds.')
    return result


def validate_observation(value: dict, now=None) -> dict:
    allowed = {'id', 'case_id', 'origin', 'source_id', 'source_family', 'features', 'action',
               'outcome', 'observed_at', 'cost_usd', 'duration_seconds', 'reliability', 'decision_id'}
    required = {'id', 'case_id', 'origin', 'features', 'action', 'outcome', 'observed_at'}
    if not isinstance(value, dict) or set(value) - allowed or required - set(value):
        raise RoutingError('Observation has missing or unsupported fields.')
    result = dict(value)
    for key in ('id', 'case_id'):
        identifier(result[key], key)
    if result['origin'] not in ('local', 'external') or result['action'] not in ('worker', 'coordinator'):
        raise RoutingError('Invalid observation origin or action.')
    if result['outcome'] not in ('accepted', 'rework', 'rejected', 'infrastructure', 'cancelled', 'unknown'):
        raise RoutingError('Invalid observation outcome.')
    result['features'] = validate_features(result['features'])
    result['observed_at'] = number(result['observed_at'], 'observed_at', 0, (time.time() if now is None else now) + 60)
    for key in ('cost_usd', 'duration_seconds'):
        result[key] = None if result.get(key) is None else number(result[key], key)
    result['reliability'] = number(result.get('reliability', 1.), 'reliability', 0, 1)
    for key in ('source_id', 'source_family'):
        result.setdefault(key, 'local' if result['origin'] == 'local' else None)
        identifier(result[key], key)
    if result.get('decision_id') is not None:
        identifier(result['decision_id'], 'decision_id')
    else:
        result['decision_id'] = None
    return result


def canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False)


def fingerprint(value) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def read_json(data: bytes | str):
    if isinstance(data, str):
        try:
            data = data.encode('utf-8')
        except UnicodeError:
            raise RoutingError('Invalid JSON text encoding.') from None
    if len(data) > MAX_JSON_BYTES:
        raise RoutingError('JSON input exceeds the 8 MiB limit.')
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise RoutingError('Duplicate JSON fields are not permitted.')
            result[key] = value
        return result
    try:
        return json.loads(data, object_pairs_hook=pairs, parse_constant=lambda _: (_ for _ in ()).throw(
            RoutingError('Non-finite JSON values are not permitted.')))
    except (ValueError, UnicodeError, RecursionError):
        raise RoutingError('Invalid JSON input.') from None
