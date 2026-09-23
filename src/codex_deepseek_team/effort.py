"""Canonical DeepSeek reasoning-effort levels and the legacy ``medium`` alias.

The canonical concrete levels are ``low``, ``high`` and ``max``; ``auto`` stays a
policy value. The historical ``medium`` spelling remains accepted at every public
boundary (CLI, saved config, API and feature cards) and normalizes to ``high``
before any runtime argument or effective-policy/feature comparison. Normalization
is non-destructive: stored files, ledgers and routing records keep their original
spelling until a writer already touches them.
"""
from __future__ import annotations

CANONICAL_EFFORTS = ('low', 'high', 'max')
LEGACY_EFFORT_ALIASES = {'medium': 'high'}
# Concrete levels accepted on the CLI, including the legacy alias.
EFFORT_LEVELS = ('low', 'high', 'max', 'medium')
# Values persisted for the effective effort policy (saved config accepts the alias too).
EFFORT_POLICY_LEVELS = ('auto', 'low', 'high', 'max')
EFFORT_POLICY_CHOICES = ('auto', 'low', 'high', 'max', 'medium')
# Fallback when policy is auto and no concrete frontier selection reaches the runner.
DEFAULT_EFFORT = 'high'


def normalize_effort(value):
    """Return the canonical concrete level, or ``None`` for an unsupported value."""
    if not isinstance(value, str):
        return None
    value = LEGACY_EFFORT_ALIASES.get(value, value)
    return value if value in CANONICAL_EFFORTS else None


def normalize_policy_effort(value):
    """Return ``auto`` or the canonical concrete level, or ``None`` when invalid."""
    if value == 'auto':
        return 'auto'
    return normalize_effort(value)
