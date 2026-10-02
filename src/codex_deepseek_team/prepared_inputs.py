"""Fingerprint explicitly registered prepared inputs without exposing content.

Only the paths listed in ``copy.metadata['prepared_paths']`` are inspected, using
the same credential-name rules as workspace import.  Inputs are hashed in place
(ignored or tracked alike) and only digests or path names leave this module, so an
ignored ``local.properties`` change becomes private evidence instead of landing in
a source patch.  Escaping symlinks and credential-like or malformed registrations
are rejected before any content is read.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path, PurePosixPath
import stat
from typing import Mapping

from .workspace import WorkspaceError, _credential_like

__all__ = ['input_snapshot', 'input_changes']

_MISSING = 'missing'
_CHUNK = 1 << 20


def _validate(value: object) -> str:
    """Return a normalized workspace-relative path or reject the registration."""
    if not isinstance(value, str) or not value or '\0' in value:
        raise WorkspaceError('Prepared path registration must be a non-empty string.', 78)
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts:
        raise WorkspaceError(f'Prepared path must be workspace-relative: {value}.', 78)
    if '..' in path.parts or '.git' in path.parts:
        raise WorkspaceError(f'Prepared path is not a safe workspace path: {value}.', 78)
    if _credential_like(Path(value)):
        raise WorkspaceError(f'Refusing credential-like prepared path: {value}.', 78)
    return path.as_posix()


def _within(root: Path, candidate: Path) -> bool:
    return candidate == root or root in candidate.parents


def _hash_file(path: Path, digest, rel: str) -> None:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError:
        raise WorkspaceError(f'Prepared input cannot be read safely: {rel}.', 78) from None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise WorkspaceError(f'Prepared input changed type while reading: {rel}.', 78)
        while True:
            chunk = os.read(fd, _CHUNK)
            if not chunk:
                break
            digest.update(chunk)
    except OSError:
        raise WorkspaceError(f'Prepared input cannot be read safely: {rel}.', 78) from None
    finally:
        os.close(fd)


def _fingerprint(root: Path, root_real: Path, rel: str) -> str:
    """Hash type/mode/content of one registered input, or mark it missing."""
    path = root / rel
    # Resolve every component first so a symlinked parent cannot redirect the
    # read outside the copy; only then is content touched.
    if not _within(root_real, Path(os.path.realpath(path))):
        raise WorkspaceError(f'Prepared input escapes the workspace: {rel}.', 78)
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return _MISSING
    except OSError:
        raise WorkspaceError(f'Prepared input cannot be inspected safely: {rel}.', 78) from None
    digest = hashlib.sha256()
    digest.update(str(stat.S_IMODE(info.st_mode)).encode() + b'\0')
    if stat.S_ISLNK(info.st_mode):
        digest.update(b'L' + os.fsencode(os.readlink(path)))
    elif stat.S_ISREG(info.st_mode):
        digest.update(b'F')
        _hash_file(path, digest, rel)
    elif stat.S_ISDIR(info.st_mode):
        # Registered directories stay bounded to this entry: no broad per-run
        # enumeration of ignored descendants or tool/cache trees.
        digest.update(b'D')
    else:
        digest.update(b'O')
    return digest.hexdigest()


def input_snapshot(copy) -> dict[str, str]:
    """Fingerprint every explicitly registered prepared input of ``copy``.

    Returns a mapping of workspace-relative path to a content/type/mode digest,
    or the literal ``'missing'`` marker when a registered input is absent.  No
    input content is returned or stored.
    """
    raw = copy.metadata.get('prepared_paths', [])
    if raw is None:
        raw = []
    if not isinstance(raw, list):
        raise WorkspaceError('Prepared path metadata must be a list of relative paths.', 78)
    root = Path(copy.path)
    if root.is_symlink() or not root.is_dir():
        raise WorkspaceError('Workspace directory is missing or replaced; prepared inputs not read.', 73)
    root_real = Path(os.path.realpath(root))
    snapshot: dict[str, str] = {}
    for value in raw:
        rel = _validate(value)
        snapshot[rel] = _fingerprint(root, root_real, rel)
    return {name: snapshot[name] for name in sorted(snapshot)}


def input_changes(copy, before: Mapping[str, str]) -> list[str]:
    """Names of registered prepared inputs changed since ``before``.

    ``before`` must be a snapshot from :func:`input_snapshot`; unregistered
    ignored artifacts are never included.
    """
    if not isinstance(before, Mapping) or not all(
            isinstance(name, str) and isinstance(value, str)
            for name, value in before.items()):
        raise WorkspaceError('Prepared input baseline must map relative paths to fingerprints.', 78)
    after = input_snapshot(copy)
    return sorted(name for name in set(before) | set(after)
                  if before.get(name) != after.get(name))
