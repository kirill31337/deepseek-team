"""Canonical project scope matching shared by lifecycle hooks and the ledger.

The Codex/Claude mutation gate and accepted-result invalidation must agree on
two *distinct* relations; the historical split (hooks only understood a terminal
``*`` while the ledger used ``fnmatch``) made them disagree.

* containment / authorization -- is a concrete mutation path inside a declared
  scope?  A plain (non-glob) scope is a directory that also covers descendants,
  while a glob scope matches with ``fnmatch.fnmatchcase`` semantics: ``*`` may
  cross ``/`` and ``?``/``[...]`` are supported, case-sensitively.
* overlap / intersection -- do two path expressions conflict?  This is the
  conservative relation used for pending-worker duplicate detection *and* for
  invalidating accepted results after a later mutation.

Paths are canonicalised against the attached project root, resolving symlinks on
any existing prefix.  Anything that resolves outside the root canonicalises to
``None`` so external scopes never grant authority and outside mutations are
rejected by the caller.  Component boundaries (``src`` vs ``src_backup``) are
preserved by comparing on ``/`` separated components.
"""
from __future__ import annotations

import fnmatch
from pathlib import Path

_GLOB_CHARS = "*?["


def is_glob(value: str) -> bool:
    """True when the value contains an ``fnmatch`` wildcard token."""
    return any(char in value for char in _GLOB_CHARS)


def canonical(root, value, *, cwd=None) -> str | None:
    """Return the project-relative canonical path/scope, or ``None`` on escape.

    Existing symlink prefixes are resolved while trailing glob characters are
    preserved, so ``aliasdir/*.py`` and ``src/*.py`` canonicalise identically.
    Empty or non-string values never grant authority and return ``None``.
    """
    if value is None or not isinstance(value, str) or not value:
        return None
    root_path = Path(root)
    try:
        root_real = root_path.resolve()
    except (OSError, RuntimeError, ValueError):
        return None
    base = Path(cwd) if cwd is not None else root_path
    candidate = Path(value)
    candidate = candidate if candidate.is_absolute() else base / candidate
    try:
        resolved = candidate.resolve()
    except (OSError, RuntimeError, ValueError):
        return None
    try:
        relative = resolved.relative_to(root_real)
    except ValueError:
        return None
    text = relative.as_posix()
    return text or "."


def contained(path: str, scope: str) -> bool:
    """Authorization: may the declared ``scope`` authorize mutating ``path``?

    A plain scope is a directory that also covers descendants.  A glob scope
    matches by ``fnmatch.fnmatchcase`` only: a path that merely *contains* the
    glob's match space (an ancestor directory of unknown type) never expands the
    authority the scope grants.
    """
    if scope == ".":
        return True
    if is_glob(scope):
        return fnmatch.fnmatchcase(path, scope)
    return path == scope or path.startswith(scope.rstrip("/") + "/")


def _may_match_descendant(directory: str, pattern: str) -> bool:
    # A descendant starts with directory + '/'. Some pattern prefix must consume
    # that string, possibly ending inside a '*' (which fnmatch lets cross '/').
    # Partial character classes may add conservative false positives, never
    # extra authority: this helper is only used for conflict/invalidation.
    prefix = directory.rstrip('/') + '/'
    return any(fnmatch.fnmatchcase(prefix, pattern[:end])
               for end in range(1, len(pattern) + 1))


def _literal_prefix(pattern: str) -> str:
    return pattern[:next((i for i, char in enumerate(pattern) if char in _GLOB_CHARS),
                         len(pattern))]


def overlaps(one: str, two: str) -> bool:
    """Conservative overlap/intersection for conflict and invalidation.

    Used for both pending-worker duplicate detection and accepted-result
    invalidation so the two callers cannot drift apart.  Directory containment
    is checked with a trailing ``/`` so ``src`` never swallows ``src_backup``.
    """
    if one == "." or two == ".":
        return True
    if one == two:
        return True
    if is_glob(one) and is_glob(two):
        # Exact intersection of arbitrary classes is unnecessary for this gate;
        # only provably disjoint literal prefixes may avoid a conflict.
        left, right = _literal_prefix(one), _literal_prefix(two)
        return left.startswith(right) or right.startswith(left)
    if is_glob(one) and fnmatch.fnmatchcase(two, one):
        return True
    if is_glob(two) and fnmatch.fnmatchcase(one, two):
        return True
    if is_glob(one) and _may_match_descendant(two, one):
        return True
    if is_glob(two) and _may_match_descendant(one, two):
        return True
    return (one.startswith(two.rstrip("/") + "/")
            or two.startswith(one.rstrip("/") + "/"))


def mutation_overlaps(root: Path, path: str, scope: str) -> bool:
    """Match canonical mutation paths, conservatively including unknown children."""
    try:
        if (Path(root) / path).is_file():
            return contained(path, scope)
    except OSError:
        pass
    return overlaps(path, scope)
