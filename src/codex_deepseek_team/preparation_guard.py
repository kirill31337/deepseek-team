"""Remember well-identified pre-provider preparation failures.

This is the negative counterpart to workspace-local check reuse.  A record is
keyed only by canonical project/source state and the observable non-secret
conditions that can make preparation fail: runtime candidates and executable
identities, PATH, platform/sandbox facts, saved recipe inputs and declared
dependency/check inputs.  Task ids, workspace ids, provider credentials, raw
environment values and arbitrary exception text are never part of the key or
the small diagnostic record.

Only explicit callers may publish a record.  Today that means managed runtime
readiness/sandbox availability, declared dependency or smoke-probe failures,
and a validated missing declarative materialization source.  Provider errors,
ordinary project-test failures, cancellation, admission refusals, uncertain
timeouts and generic exceptions are deliberately not classified.  Legacy
``workspace prepare -- COMMAND`` runs arbitrary host commands and is never
guarded.

An unknown condition (unreadable executable identity, missing source HEAD,
unreadable recipe) produces no fingerprint, so it can never be mistaken for an
unchanged condition.  Records are bounded, have no TTL, and are published under
a stable private lock with atomic replacement.  Unsafe symlinks/ownership are
rejected rather than followed.

Precise uncached limits:

* Credential-like paths and Git metadata make a source/input identity unknown
  and are never read.  Broad mixed host roots are likewise not traversed.
* A source/input tree is fingerprinted only while it has at most
  ``MAX_TREE_ENTRIES`` entries, ``MAX_TREE_BYTES`` bytes of ordinary-file
  content and ``MAX_TREE_DEPTH`` nesting levels.  Unreadable, special-file,
  oversized or changing trees make the whole condition unknown, so they are
  never claimed unchanged and no failure is recorded for them.
* Ordinary files contribute their bounded content digest.  A nested symlink
  contributes its own name, mode and target text; the guard does not claim an
  unchanged identity for content reached only behind that link.
* Runtime/platform executable identities are bounded stat identities (device,
  inode, size, mode, modification and change time); their content is not re-read.
  The change time makes a normal rewrite that restores mtime observable.
* A fresh explicit copy and a successfully declaratively prepared copy are keyed
  by their recorded base HEAD, bounded working-tree content and stable toolchain
  metadata. Workspace/task ids, timestamps and Git control data are excluded.
  Only a verified top-level ``.git`` directory is skipped without reading it;
  nested Git paths, credentials, symlinks, special files, changing files and
  bounded-out trees make the copy identity unknown instead of unchanged.
* At most ``MAX_RECORDS`` fingerprints are retained.  Once the directory is
  full, additional failures are still reported normally but are not cached;
  existing evidence is never pruned to make room.
* Readiness errors whose text indicates a timeout, shared time-budget expiry,
  probe-evidence/setup failure, generic context preparation or generic
  namespace-creation failure remain uncached because their cause is uncertain.
* A saved-recipe missing-source failure is fingerprinted only while that recipe
  is observably present.  Replaying a stale recipe may invalidate it; the next
  condition is then a fresh observation and is never claimed unchanged.
* Declared command/check lookups are fingerprinted only against absolute PATH
  entries and otherwise already-existing absolute executables.  A slash-relative
  runtime pin, relative path dependency, or command whose own token is a
  slash-relative path is unknown because its resolution depends on the current
  or future copy directory; such jobs are never guarded by an unchanged claim.
* Legacy arbitrary ``workspace prepare -- COMMAND`` preparation and any failure
  category or exception outside the explicit allowlist above stay uncached.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import time
import uuid

from . import state_storage, verification, workspace

SCHEMA = 1
DIRECTORY = 'preparation-failures'
MAX_RECORD_BYTES = 16384
MAX_RECORDS = 256
MAX_FINGERPRINT_INPUT_BYTES = 1 << 20
MAX_TREE_ENTRIES = 100_000
MAX_TREE_BYTES = 256 * 1024 * 1024
MAX_TREE_DEPTH = 64

CATEGORIES = {
    'runtime': 'runtime/sandbox readiness',
    'dependency': 'declared dependency availability',
    'probe': 'declared smoke probe',
    'materialization': 'toolchain materialization source',
}

_FINGERPRINT = re.compile(r'[0-9a-f]{64}\Z')
_WORKSPACE = re.compile(r'[0-9a-f]{32}\Z')
_RECORD_NAME = re.compile(r'[0-9a-f]{64}\.json\Z')
_SOURCE_HEAD = re.compile(r'[0-9a-f]{40,64}\Z')
_SYSTEM_DIRECTORIES = ('/usr/local/bin', '/usr/bin')
_TOOL_DIRECTORIES = ('/usr/local/bin', '/usr/bin', '/bin')
_APPARMOR_PROFILE = '/etc/apparmor.d/deepseek-team-bwrap'
_BROAD_INPUT_ROOTS = frozenset(str(path) for path in (
    Path('/'), Path('/usr'), Path('/etc'), Path('/home'), Path('/root'),
    Path('/tmp'), Path('/var'), Path('/run')))


class PreparationGuardError(Exception):
    """Private guard storage is unsafe/unavailable or a record is malformed."""

    def __init__(self, message: str, code: int = 78):
        self.message, self.code = message, code
        super().__init__(message)


@dataclass(frozen=True)
class Conditions:
    """Fingerprintable conditions plus the identities that were unknown."""

    project: str
    fingerprint: str | None
    unknown: tuple[str, ...] = ()


def _canonical(path) -> str | None:
    """Resolve the canonical Git top level, falling back to a real path."""
    try:
        resolved = str(Path(os.path.realpath(path)))
    except (OSError, ValueError):
        return None
    try:
        result = subprocess.run(
            ['git', '-C', resolved, 'rev-parse', '--show-toplevel'],
            env=_git_environment(), capture_output=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return resolved
    if result.returncode == 0 and result.stdout.strip():
        top = result.stdout.strip().decode('utf-8', 'surrogateescape')
        try:
            return str(Path(os.path.realpath(top)))
        except (OSError, ValueError):
            return None
    return resolved


def _git_environment() -> dict[str, str]:
    env = {name: os.environ[name] for name in ('PATH', 'HOME', 'LANG', 'LC_ALL')
           if name in os.environ}
    env.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull,
               GIT_OPTIONAL_LOCKS='0', GIT_TERMINAL_PROMPT='0',
               GIT_NO_REPLACE_OBJECTS='1')
    return env


def _source_head(project: str) -> str | None:
    try:
        result = subprocess.run(
            ['git', '-C', project, 'rev-parse', '--verify', 'HEAD'],
            env=_git_environment(), capture_output=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode or not result.stdout.strip():
        return None
    return result.stdout.strip().decode('ascii', 'replace')


def _read_hashed_file(path: str):
    """Return a regular file's content digest, or None when it is not safely pinned."""
    try:
        info = os.stat(path)
    except OSError:
        return None
    if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_TREE_BYTES:
        return None
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | os.O_CLOEXEC
    try:
        fd = os.open(path, flags)
    except OSError:
        return None
    try:
        opened = os.fstat(fd)
        if ((opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns,
             opened.st_ctime_ns)
                != (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
                    info.st_ctime_ns)):
            return None
        digest = hashlib.sha256()
        remaining = opened.st_size + 1
        while remaining:
            chunk = os.read(fd, min(1 << 20, remaining))
            if not chunk:
                break
            digest.update(chunk)
            remaining -= len(chunk)
        after = os.fstat(fd)
        if ((after.st_size, after.st_mtime_ns, after.st_ctime_ns)
                != (opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns)):
            return None
    except OSError:
        return None
    finally:
        os.close(fd)
    if opened.st_size + 1 - remaining != opened.st_size:
        return None
    return digest.hexdigest()


def _tree_digest(root: str) -> str | None:
    """Bounded recursive identity for a source/tool tree, or None if unknown."""
    digest = hashlib.sha256()
    counters = {'entries': 0, 'bytes': 0}

    def add(value: bytes) -> None:
        digest.update(len(value).to_bytes(8, 'big'))
        digest.update(value)

    def visit(path: str, relative: bytes, depth: int) -> bool:
        if depth > MAX_TREE_DEPTH:
            return False
        try:
            entries = []
            with os.scandir(path) as iterator:
                for entry in iterator:
                    entries.append(entry)
                    if len(entries) > MAX_TREE_ENTRIES:
                        return False
        except OSError:
            return False
        entries.sort(key=lambda entry: os.fsencode(entry.name))
        for entry in entries:
            counters['entries'] += 1
            if counters['entries'] > MAX_TREE_ENTRIES:
                return False
            name = os.fsencode(entry.name)
            child = relative + b'/' + name if relative else name
            relative_path = Path(os.fsdecode(child))
            if '.git' in relative_path.parts or workspace._credential_like(relative_path):
                return False  # Never read credential-like content or Git objects.
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError:
                return False
            mode = stat.S_IMODE(info.st_mode)
            if stat.S_ISLNK(info.st_mode):
                try:
                    target = os.readlink(entry.path)
                except OSError:
                    return False
                add(b'L')
                add(child)
                add(mode.to_bytes(4, 'big'))
                add(os.fsencode(target))
            elif stat.S_ISDIR(info.st_mode):
                add(b'D')
                add(child)
                add(mode.to_bytes(4, 'big'))
                if not visit(entry.path, child, depth + 1):
                    return False
            elif stat.S_ISREG(info.st_mode):
                if counters['bytes'] + info.st_size > MAX_TREE_BYTES:
                    return False
                content = _read_hashed_file(entry.path)
                if content is None:
                    return False
                counters['bytes'] += info.st_size
                add(b'F')
                add(child)
                add(mode.to_bytes(4, 'big'))
                add(bytes.fromhex(content))
            else:
                return False
        return True

    return digest.hexdigest() if visit(root, b'', 0) else None


def _identity(path: str, *, content: bool = False):
    """Observable executable/source identity, or None when it is unknown."""
    try:
        info = os.stat(path)
    except FileNotFoundError:
        return {'status': 'missing'}
    except OSError:
        return None
    try:
        real = os.path.realpath(path)
    except OSError:
        return None
    if stat.S_ISDIR(info.st_mode):
        kind = 'directory'
    elif stat.S_ISREG(info.st_mode):
        kind = 'file'
    else:
        kind = 'other'
    identity = {'status': 'present', 'kind': kind, 'real': real,
                'dev': info.st_dev, 'ino': info.st_ino, 'size': info.st_size,
                'mtime_ns': info.st_mtime_ns, 'ctime_ns': info.st_ctime_ns,
                'mode': stat.S_IMODE(info.st_mode)}
    if kind == 'directory':
        tree = _tree_digest(real)
        if tree is None:
            return None
        identity['tree_sha256'] = tree
    elif kind == 'file' and content:
        body = _read_hashed_file(real)
        if body is None:
            return None
        identity['sha256'] = body
    return identity


def fresh_copy(copy) -> bool:
    """An explicit copy whose preparation is complete but never executed."""
    metadata = getattr(copy, 'metadata', None)
    if not isinstance(metadata, dict):
        return False
    status = metadata.get('status')
    return (status == 'ready' or
            (status == 'succeeded' and metadata.get('operation') == 'preparation'))


def _same_file_identity(left, right) -> bool:
    return ((left.st_dev, left.st_ino, left.st_size, left.st_mode,
             left.st_mtime_ns, left.st_ctime_ns)
            == (right.st_dev, right.st_ino, right.st_size, right.st_mode,
                right.st_mtime_ns, right.st_ctime_ns))


def _workspace_tree_digest(root, *, work_inode=None) -> str | None:
    """Bounded content identity for an owned copy, excluding only its top-level .git."""
    root = Path(root)
    try:
        root_info = os.lstat(root)
    except OSError:
        return None
    if (stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode)
            or (work_inode is not None and root_info.st_ino != work_inode)):
        return None
    try:
        git_info = os.lstat(root / '.git')
    except OSError:
        return None
    if stat.S_ISLNK(git_info.st_mode) or not stat.S_ISDIR(git_info.st_mode):
        return None
    digest = hashlib.sha256()
    counters = {'entries': 0, 'bytes': 0}

    def add(value: bytes) -> None:
        digest.update(len(value).to_bytes(8, 'big'))
        digest.update(value)

    def visit(path: str, relative: bytes, depth: int) -> bool:
        if depth > MAX_TREE_DEPTH:
            return False
        try:
            entries = []
            with os.scandir(path) as iterator:
                for entry in iterator:
                    entries.append(entry)
                    if len(entries) > MAX_TREE_ENTRIES:
                        return False
        except OSError:
            return False
        entries.sort(key=lambda entry: os.fsencode(entry.name))
        for entry in entries:
            name = os.fsencode(entry.name)
            child = relative + b'/' + name if relative else name
            if not relative and entry.name == '.git':
                continue  # The verified top-level control directory is never read.
            relative_path = Path(os.fsdecode(child))
            if ('.git' in relative_path.parts
                    or workspace._credential_like(relative_path)):
                return False
            counters['entries'] += 1
            if counters['entries'] > MAX_TREE_ENTRIES:
                return False
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError:
                return False
            mode = stat.S_IMODE(info.st_mode)
            if stat.S_ISLNK(info.st_mode):
                return False  # A link's resolved content is not verified here.
            if stat.S_ISDIR(info.st_mode):
                add(b'D')
                add(child)
                add(mode.to_bytes(4, 'big'))
                if not visit(entry.path, child, depth + 1):
                    return False
            elif stat.S_ISREG(info.st_mode):
                if counters['bytes'] + info.st_size > MAX_TREE_BYTES:
                    return False
                content = _read_hashed_file(entry.path)
                if content is None:
                    return False
                try:
                    after = entry.stat(follow_symlinks=False)
                except OSError:
                    return False
                if not _same_file_identity(info, after):
                    return False
                counters['bytes'] += info.st_size
                add(b'F')
                add(child)
                add(mode.to_bytes(4, 'big'))
                add(bytes.fromhex(content))
            else:
                return False
        return True

    return digest.hexdigest() if visit(str(root), b'', 0) else None


def _toolchain_condition(metadata):
    """Only stable declared/materialized toolchain metadata, never timestamps or ids."""
    raw = metadata.get('toolchain')
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        return None
    result = {}
    for key in ('version', 'source_project'):
        if key in raw:
            value = raw[key]
            if isinstance(value, bool) or not isinstance(value, (str, int)):
                return None
            result[key] = value
    tools = raw.get('tools', {})
    if not isinstance(tools, dict):
        return None
    normalized_tools = {}
    for name, entry in tools.items():
        if not isinstance(name, str) or not isinstance(entry, dict):
            return None
        row = {}
        for key in ('source',):
            if key in entry:
                if not isinstance(entry[key], str):
                    return None
                row[key] = entry[key]
        for key in ('files', 'bytes', 'links_skipped'):
            if key in entry:
                value = entry[key]
                if isinstance(value, bool) or not isinstance(value, int):
                    return None
                row[key] = value
        normalized_tools[name] = row
    result['tools'] = normalized_tools
    copies = raw.get('copies', [])
    if not isinstance(copies, list):
        return None
    normalized_copies = []
    for entry in copies:
        if not isinstance(entry, dict):
            return None
        row = {}
        for key in ('source', 'destination'):
            value = entry.get(key)
            if not isinstance(value, str):
                return None
            row[key] = value
        normalized_copies.append(row)
    result['copies'] = normalized_copies
    environment = raw.get('environment', {})
    if (not isinstance(environment, dict)
            or not all(isinstance(key, str) and isinstance(value, str)
                       for key, value in environment.items())):
        return None
    result['environment'] = {key: environment[key] for key in sorted(environment)}
    probes = raw.get('probes', [])
    if not isinstance(probes, list) or not all(isinstance(value, str)
                                               for value in probes):
        return None
    result['probes'] = list(probes)
    return result


def _copy_condition(copy):
    """Identity of a fresh owned copy's actual working tree and prepared metadata."""
    metadata = getattr(copy, 'metadata', None)
    if not fresh_copy(copy):
        return None
    head = metadata.get('base_head') or metadata.get('source_head')
    if not isinstance(head, str) or _SOURCE_HEAD.fullmatch(head) is None:
        return None
    toolchain = _toolchain_condition(metadata)
    if toolchain is None:
        return None
    prepared_paths = metadata.get('prepared_paths', [])
    if (not isinstance(prepared_paths, list)
            or not all(isinstance(value, str) for value in prepared_paths)):
        return None
    tree = _workspace_tree_digest(copy.path, work_inode=metadata.get('work_inode'))
    if tree is None:
        return None
    result = {'base_head': head, 'tree_sha256': tree,
              'toolchain': toolchain}
    if prepared_paths:
        result['prepared_paths'] = sorted(set(prepared_paths))
    return result


def _candidate_paths(value: str, explicit: bool, path_value: str) -> list[str]:
    """Match the managed runtime lookup without executing anything."""
    separator = os.sep in value or (os.altsep is not None and os.altsep in value)
    if explicit or separator:
        found = shutil.which(value, path=path_value)
        return [found] if found else []
    directories = [entry for entry in path_value.split(os.pathsep)
                   if entry and os.path.isabs(entry)]
    directories.extend(_SYSTEM_DIRECTORIES)
    candidates: list[str] = []
    seen: set[str] = set()
    for directory in directories:
        candidate = os.path.join(directory, value)
        try:
            key = os.path.realpath(candidate)
        except OSError:
            key = candidate
        if key in seen:
            continue
        seen.add(key)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            candidates.append(candidate)
    return candidates


def _lookup_executable(value: str, path_value: str):
    """Identity of a command lookup against the conditions visible pre-copy."""
    if not isinstance(value, str) or not value or '\0' in value:
        return None
    separator = os.sep in value or (os.altsep is not None and os.altsep in value)
    if separator:
        if not os.path.isabs(value):
            return None  # Copy-relative commands depend on a future copy.
        identity = _identity(value)
        if identity is None:
            return None
        return {'value': value, 'identity': identity}
    entries = path_value.split(os.pathsep)
    if any(not entry or not os.path.isabs(entry) for entry in entries):
        return None  # Relative/empty PATH entries depend on the current directory.
    directories = list(entries)
    directories.extend(_TOOL_DIRECTORIES)
    candidates = []
    seen: set[str] = set()
    for directory in directories:
        candidate = os.path.join(directory, value)
        try:
            key = os.path.realpath(candidate)
        except OSError:
            key = candidate
        if key in seen:
            continue
        seen.add(key)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            identity = _identity(candidate)
            if identity is None:
                return None
            candidates.append({'path': candidate, 'identity': identity})
    return {'value': value, 'candidates': candidates}


def _declaration_path(path_value: str, runtimes):
    """Host and prepared-runtime directories that can satisfy declared commands."""
    entries = path_value.split(os.pathsep)
    for plan in runtimes or ():
        for candidate in plan.get('candidates', ()):
            entries.append(os.path.dirname(candidate['path']))
    entries.append(str(Path(sys.executable).parent))
    unique = []
    seen = set()
    for entry in entries:
        if entry not in seen:
            seen.add(entry)
            unique.append(entry)
    return os.pathsep.join(unique)


def _check_condition(command: str, path_value: str):
    if not isinstance(command, str):
        return None
    try:
        executable = verification.first_executable(command)
    except verification.CheckCommandError:
        return {'command': command, 'executable': {'status': 'invalid'}}
    lookup = _lookup_executable(executable, path_value)
    if lookup is None:
        return None
    return {'command': command, 'executable': lookup}


def _runtime_plans(runtime, codex, claude, explicit_codex, explicit_claude):
    if runtime == 'codex':
        return [('codex', codex, explicit_codex)]
    if runtime == 'claude':
        return [('claude', claude, explicit_claude)]
    return [('codex', codex, explicit_codex),
            ('claude', claude, explicit_claude)]


def _runtime_conditions(runtime, codex, claude, explicit_codex, explicit_claude,
                        path_value):
    unknown = []
    plans = []
    for name, value, explicit in _runtime_plans(
            runtime, codex, claude, explicit_codex, explicit_claude):
        candidates = []
        if isinstance(value, str) and value:
            separator = os.sep in value or (os.altsep is not None and os.altsep in value)
            if separator and not os.path.isabs(value):
                unknown.append('runtime-' + name)
                plans.append({'runtime': name, 'value': value,
                              'explicit': bool(explicit), 'candidates': candidates})
                continue
            for candidate in _candidate_paths(value, bool(explicit), path_value):
                identity = _identity(candidate)
                if identity is None:
                    unknown.append('runtime-' + name)
                    continue
                candidates.append({'path': candidate, 'identity': identity})
        plans.append({'runtime': name, 'value': value, 'explicit': bool(explicit),
                      'candidates': candidates})
    return plans if not unknown else None, unknown


def _read_sysctl(path: str):
    try:
        return Path(path).read_text(encoding='ascii').strip()
    except FileNotFoundError:
        return None
    except (OSError, ValueError, UnicodeError):
        return False  # known-unreadable, not a claimed unchanged value


def _platform_conditions(path_value: str):
    values = {'uid': os.geteuid(), 'system': sys.platform,
              'bwrap': _identity(shutil.which('bwrap', path=path_value) or ''),
              'aa_exec': _identity(shutil.which('aa-exec', path=path_value) or ''),
              'apparmor_profile': _identity(_APPARMOR_PROFILE, content=True)}
    try:
        uname = os.uname()
    except OSError:
        return None
    values.update(machine=uname.machine, release=uname.release,
                  sysname=uname.sysname)
    for label, path in (
            ('apparmor_restriction',
             '/proc/sys/kernel/apparmor_restrict_unprivileged_userns'),
            ('unprivileged_userns_clone',
             '/proc/sys/kernel/unprivileged_userns_clone'),
            ('max_user_namespaces', '/proc/sys/user/max_user_namespaces')):
        value = _read_sysctl(path)
        if value is False:
            return None
        values[label] = value
    if (values['bwrap'] is None or values['aa_exec'] is None
            or values['apparmor_profile'] is None):
        return None
    return values


def _platform_without_sandbox():
    values = {'uid': os.geteuid(), 'system': sys.platform}
    try:
        uname = os.uname()
    except OSError:
        return None
    values.update(machine=uname.machine, release=uname.release,
                  sysname=uname.sysname)
    return values


def _recipe_conditions(state: Path, project: str):
    """Digest a private saved recipe and observe its declared source identities."""
    key = hashlib.sha256(os.fsencode(project)).hexdigest()
    directory = Path(state) / 'toolchain-recipes'
    path = directory / (key + '.json')
    try:
        directory_info = os.lstat(directory)
    except FileNotFoundError:
        return {'status': 'absent'}
    except OSError:
        return None
    if (not stat.S_ISDIR(directory_info.st_mode)
            or stat.S_ISLNK(directory_info.st_mode)
            or directory_info.st_uid != os.geteuid()
            or stat.S_IMODE(directory_info.st_mode) & 0o077):
        return None
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return {'status': 'absent'}
    except OSError:
        return None
    if (not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)
            or info.st_nlink != 1 or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) & 0o077
            or info.st_size > MAX_FINGERPRINT_INPUT_BYTES):
        return None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        opened = os.fstat(fd)
        if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
            return None
        raw = os.read(fd, MAX_FINGERPRINT_INPUT_BYTES + 1)
    except OSError:
        return None
    finally:
        os.close(fd)
    if len(raw) > MAX_FINGERPRINT_INPUT_BYTES:
        return None
    try:
        payload = json.loads(raw.decode('utf-8'))
    except (UnicodeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    tools = payload.get('tools')
    copies = payload.get('copies')
    if not isinstance(tools, dict) or not isinstance(copies, list):
        return None
    sources = []
    for name, entry in tools.items():
        if not isinstance(name, str) or not isinstance(entry, dict):
            return None
        source = entry.get('source')
        condition = _source_condition(source, project=project)
        if condition is None:
            return None
        sources.append({'name': name, 'identity': condition})
    for entry in copies:
        if not isinstance(entry, dict):
            return None
        condition = _source_condition(entry.get('source'), project=project)
        if condition is None:
            return None
        sources.append({'destination': entry.get('destination'),
                        'identity': condition})
    return {'status': 'present', 'sha256': hashlib.sha256(raw).hexdigest(),
            'sources': sources}


def _source_condition(value, *, project=None):
    if isinstance(value, os.PathLike):
        try:
            value = os.fspath(value)
        except TypeError:
            return None
    if (not isinstance(value, str) or not value or '\0' in value):
        return None
    try:
        real = os.path.realpath(value)
        real_path = Path(real)
    except (OSError, ValueError):
        return None
    if (workspace._credential_like(Path(value))
            or workspace._credential_like(real_path)
            or '.git' in Path(value).parts or '.git' in real_path.parts):
        return None
    try:
        home = Path.home()
    except (OSError, RuntimeError):
        return None
    if real in (_BROAD_INPUT_ROOTS | {str(home), str(home / '.local')}):
        return None
    if project is not None:
        try:
            if real == project or Path(project).is_relative_to(real_path):
                return None
        except (OSError, ValueError):
            return None
    identity = _identity(real, content=True)
    if identity is None:
        return None
    return {'source': value, 'identity': identity}


def _normalize_declarations(declarations, *, path_value='', project=None):
    """Return only JSON-safe declared inputs, or None when an identity is unknown."""
    if not isinstance(declarations, dict):
        return None
    result = {}
    for name in ('dependencies', 'checks', 'environment', 'probes'):
        if name in declarations:
            result[name] = declarations[name]
    if 'dependencies' in result:
        dependencies = result['dependencies']
        if not isinstance(dependencies, (list, tuple)):
            return None
        normalized = []
        for entry in dependencies:
            if (not isinstance(entry, dict)
                    or not isinstance(entry.get('kind'), str)
                    or not isinstance(entry.get('value'), str)):
                return None
            row = {'kind': entry['kind'], 'value': entry['value']}
            if entry['kind'] == 'command':
                lookup = _lookup_executable(entry['value'], path_value)
            elif (entry['kind'] == 'path' and '\0' not in entry['value']
                  and os.path.isabs(entry['value'])):
                lookup = _identity(entry['value'])
            else:
                return None
            if lookup is None:
                return None
            row['lookup'] = lookup
            normalized.append(row)
        result['dependencies'] = sorted(normalized,
                                        key=lambda row: (row['kind'], row['value']))
    if 'checks' in result:
        checks = result['checks']
        if not isinstance(checks, (list, tuple)) or not all(
                isinstance(value, str) for value in checks):
            return None
        normalized = []
        for command in checks:
            condition = _check_condition(command, path_value)
            if condition is None:
                return None
            normalized.append(condition)
        result['checks'] = normalized
    if 'environment' in result:
        environment = result['environment']
        if not isinstance(environment, dict) or not all(
                isinstance(key, str) and isinstance(value, str)
                for key, value in environment.items()):
            return None
        result['environment'] = {key: environment[key]
                                 for key in sorted(environment)}
    if 'probes' in result:
        probes = result['probes']
        if not isinstance(probes, (list, tuple)) or not all(
                isinstance(value, str) for value in probes):
            return None
        result['probes'] = list(probes)
    for name in ('tools', 'copies'):
        if name not in declarations:
            continue
        values = declarations[name]
        if name == 'tools':
            if not isinstance(values, dict):
                return None
            tools = []
            for tool_name, source in values.items():
                if not isinstance(tool_name, str):
                    return None
                condition = _source_condition(source, project=project)
                if condition is None:
                    return None
                tools.append({'name': tool_name, 'identity': condition})
            result['tools'] = sorted(tools, key=lambda row: row['name'])
        else:
            if not isinstance(values, (list, tuple)):
                return None
            copies = []
            for entry in values:
                if not isinstance(entry, (list, tuple)) or len(entry) != 2:
                    return None
                condition = _source_condition(entry[0], project=project)
                if condition is None or not isinstance(entry[1], str):
                    return None
                copies.append({'destination': entry[1], 'identity': condition})
            result['copies'] = copies
    return result


def _digest(payload: dict) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(',', ':'),
                     ensure_ascii=True).encode('ascii')
    return hashlib.sha256(raw).hexdigest()


def worker_conditions(source, *, runtime, codex, claude, codex_explicit,
                      claude_explicit, state, declarations=None, environ=None,
                      copy=None):
    """Fingerprint a managed job before any copy allocation or provider read."""
    env = os.environ if environ is None else environ
    project = _canonical(copy.source if copy is not None else source)
    unknown: list[str] = []
    copy_values = None
    head = None
    if project is None:
        unknown.append('project')
    if copy is not None:
        copy_values = _copy_condition(copy)
        if copy_values is None:
            unknown.append('copy')
        else:
            # The owned copy is the execution source; its recorded base HEAD and
            # bounded working tree describe exactly what this job will use.
            head = copy_values['base_head']
    if head is None and project is not None:
        head = _source_head(project)
        if head is None:
            unknown.append('source-head')
    path_value = env.get('PATH', '')
    runtimes, runtime_unknown = _runtime_conditions(
        runtime, codex, claude, codex_explicit, claude_explicit, path_value)
    unknown.extend(runtime_unknown)
    platform_values = _platform_conditions(path_value)
    if platform_values is None:
        unknown.append('platform')
    recipe = (_recipe_conditions(Path(state), project)
              if project is not None else None)
    if recipe is None:
        unknown.append('recipe')
    normalized = _normalize_declarations(
        {'dependencies': [], 'checks': []} if declarations is None else declarations,
        path_value=_declaration_path(path_value, runtimes), project=project)
    if normalized is None:
        unknown.append('declarations')
    payload = {'schema': SCHEMA, 'project': project, 'source_head': head,
               'path': path_value, 'runtime': runtimes,
               'platform': platform_values, 'recipe': recipe,
               'declarations': normalized}
    if copy is not None:
        payload['copy'] = copy_values
    fingerprint = None if unknown else _digest(payload)
    return Conditions(project or '', fingerprint, tuple(sorted(set(unknown))))


def declarative_conditions(source, *, state, declarations, environ=None):
    """Fingerprint explicit declarative preparation inputs before materializing."""
    env = os.environ if environ is None else environ
    project = _canonical(source)
    unknown: list[str] = []
    if project is None:
        unknown.append('project')
        head = None
    else:
        head = _source_head(project)
        if head is None:
            unknown.append('source-head')
    platform_values = _platform_without_sandbox()
    if platform_values is None:
        unknown.append('platform')
    normalized = _normalize_declarations(
        declarations, path_value=env.get('PATH', ''), project=project)
    if normalized is None:
        unknown.append('declarations')
    payload = {'schema': SCHEMA, 'project': project, 'source_head': head,
               'path': env.get('PATH', ''), 'platform': platform_values,
               'declarations': normalized}
    fingerprint = None if unknown else _digest(payload)
    return Conditions(project or '', fingerprint, tuple(sorted(set(unknown))))


def _open_directory(state, *, create: bool):
    state = Path(state)
    try:
        root_fd = state_storage.open_private_directory(state, create=create)
    except state_storage.StorageError as error:
        if not create and error.errno == errno.ENOENT:
            return None
        raise PreparationGuardError(
            'Preparation guard storage is unavailable or unsafe; retained '
            'workspaces and coordination records were not changed.') from None
    try:
        try:
            return state_storage.open_private_directory(
                state / DIRECTORY, create=create, parent=root_fd,
                label='Preparation guard directory')
        except state_storage.StorageError as error:
            if not create and error.errno == errno.ENOENT:
                return None
            raise PreparationGuardError(
                'Preparation guard directory is unavailable or unsafe '
                '(owner-only ordinary directory required).') from None
    finally:
        os.close(root_fd)


@contextmanager
def _locked(directory_fd):
    try:
        lock_fd = os.open('lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
                          0o600, dir_fd=directory_fd)
    except OSError:
        raise PreparationGuardError('Preparation guard lock is unsafe.') from None
    try:
        info = os.fstat(lock_fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077):
            raise PreparationGuardError('Preparation guard lock is unsafe.')
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
        except OSError:
            raise PreparationGuardError('Preparation guard lock could not be acquired.') from None
        yield
    finally:
        os.close(lock_fd)


def _entry(directory_fd, name):
    try:
        info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError:
        raise PreparationGuardError('Preparation guard record is unreadable.') from None
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077
            or info.st_size > MAX_RECORD_BYTES):
        raise PreparationGuardError('Preparation guard record is unsafe or oversized.')
    return info


def _read_record(directory_fd, name):
    info = _entry(directory_fd, name)
    if info is None:
        return None
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                     dir_fd=directory_fd)
    except OSError:
        raise PreparationGuardError('Preparation guard record is unsafe.') from None
    try:
        opened = os.fstat(fd)
        if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
            raise PreparationGuardError('Preparation guard record changed during reading.')
        raw = os.read(fd, MAX_RECORD_BYTES + 1)
    except OSError:
        raise PreparationGuardError('Preparation guard record is unreadable.') from None
    finally:
        os.close(fd)
    if len(raw) > MAX_RECORD_BYTES:
        raise PreparationGuardError('Preparation guard record is oversized.')
    try:
        payload = json.loads(raw.decode('ascii'))
    except (UnicodeError, ValueError):
        return None
    if not _valid_record(payload, name[:-5]):
        return None
    return payload


def _valid_record(payload, fingerprint):
    if not isinstance(payload, dict) or set(payload) != {
            'schema', 'fingerprint', 'project', 'category', 'workspace_id',
            'exit_code', 'created_at', 'reference'}:
        return False
    if payload.get('schema') != SCHEMA or payload.get('fingerprint') != fingerprint:
        return False
    if not isinstance(payload.get('project'), str) or not payload['project']:
        return False
    if payload.get('category') not in CATEGORIES:
        return False
    workspace = payload.get('workspace_id')
    if workspace is not None and (not isinstance(workspace, str)
                                  or _WORKSPACE.fullmatch(workspace) is None):
        return False
    code = payload.get('exit_code')
    if isinstance(code, bool) or not isinstance(code, int) or code in (124, 130):
        return False
    created = payload.get('created_at')
    if isinstance(created, bool) or not isinstance(created, (int, float)):
        return False
    reference = payload.get('reference')
    return isinstance(reference, str) and 0 < len(reference) <= 512


def _publish(directory_fd, name, payload):
    raw = json.dumps(payload, sort_keys=True, separators=(',', ':'),
                     ensure_ascii=True).encode('ascii')
    if len(raw) > MAX_RECORD_BYTES:
        raise PreparationGuardError('Preparation guard record exceeds its bound.')
    temporary = '.tmp-' + uuid.uuid4().hex
    fd = None
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
                     | os.O_CLOEXEC, 0o600, dir_fd=directory_fd)
        with os.fdopen(fd, 'wb') as stream:
            fd = None
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, name, src_dir_fd=directory_fd,
                   dst_dir_fd=directory_fd)
        os.fsync(directory_fd)
    finally:
        if fd is not None:
            os.close(fd)
        try:
            os.unlink(temporary, dir_fd=directory_fd)
        except FileNotFoundError:
            pass


def _record_count(directory_fd):
    return sum(1 for name in os.listdir(directory_fd)
               if _RECORD_NAME.fullmatch(name))


def record_failure(state, conditions, category, *, exit_code=78,
                   workspace_id=None):
    """Publish the first record for these conditions; never overwrite evidence."""
    if conditions is None or conditions.fingerprint is None:
        return None
    if (not isinstance(conditions.fingerprint, str)
            or _FINGERPRINT.fullmatch(conditions.fingerprint) is None
            or not isinstance(conditions.project, str) or not conditions.project):
        raise PreparationGuardError('Preparation guard requires a canonical project and fingerprint.')
    if category not in CATEGORIES:
        raise PreparationGuardError('Unknown preparation failure category.')
    if (isinstance(exit_code, bool) or not isinstance(exit_code, int)
            or exit_code in (124, 130)):
        raise PreparationGuardError('Preparation guard requires a definite non-cancellation code.')
    if workspace_id is not None and (not isinstance(workspace_id, str)
                                     or _WORKSPACE.fullmatch(workspace_id) is None):
        raise PreparationGuardError('Preparation guard requires an owned workspace id or none.')
    name = conditions.fingerprint + '.json'
    directory_fd = _open_directory(state, create=True)
    try:
        with _locked(directory_fd):
            existing = _read_record(directory_fd, name)
            if existing is not None:
                return existing
            if _entry(directory_fd, name) is not None:
                return None  # Preserve an unreadable/tampered record verbatim.
            if _record_count(directory_fd) >= MAX_RECORDS:
                return None  # Bounded storage degrades open, never prunes evidence.
            payload = {'schema': SCHEMA, 'fingerprint': conditions.fingerprint,
                       'project': conditions.project, 'category': category,
                       'workspace_id': workspace_id, 'exit_code': exit_code,
                       'created_at': time.time(),
                       'reference': ('workspace-' + workspace_id
                                     if workspace_id else 'no-workspace')}
            _publish(directory_fd, name, payload)
            return payload
    finally:
        os.close(directory_fd)


def lookup(state, conditions):
    """Return the original record for unchanged conditions, or None."""
    if (conditions is None or not isinstance(conditions.fingerprint, str)
            or _FINGERPRINT.fullmatch(conditions.fingerprint) is None
            or not isinstance(conditions.project, str) or not conditions.project):
        return None
    directory_fd = _open_directory(state, create=False)
    if directory_fd is None:
        return None
    name = conditions.fingerprint + '.json'
    try:
        with _locked(directory_fd):
            payload = _read_record(directory_fd, name)
            if (payload is None or payload.get('fingerprint') != conditions.fingerprint
                    or payload.get('project') != conditions.project):
                return None
            return payload
    finally:
        os.close(directory_fd)


def clear(state, conditions):
    """Remove only this exact project/fingerprint record after a successful attempt."""
    if (conditions is None or not isinstance(conditions.fingerprint, str)
            or _FINGERPRINT.fullmatch(conditions.fingerprint) is None
            or not isinstance(conditions.project, str) or not conditions.project):
        return False
    directory_fd = _open_directory(state, create=False)
    if directory_fd is None:
        return False
    name = conditions.fingerprint + '.json'
    try:
        with _locked(directory_fd):
            payload = _read_record(directory_fd, name)
            if (payload is None or payload.get('fingerprint') != conditions.fingerprint
                    or payload.get('project') != conditions.project):
                return False
            try:
                os.unlink(name, dir_fd=directory_fd)
                os.fsync(directory_fd)
            except FileNotFoundError:
                return False
            return True
    finally:
        os.close(directory_fd)


def blocked_message(record, *, copy_id=None):
    """Safe, actionable text; never includes raw errors, environment or secrets."""
    label = CATEGORIES.get(record.get('category'), 'environmental')
    workspace = record.get('workspace_id')
    if isinstance(workspace, str) and _WORKSPACE.fullmatch(workspace):
        reference = (
            'The original retained workspace is ' + workspace +
            '. Inspect it with `deepseek-team workspace show ' + workspace +
            '` and `deepseek-team workspace history ' + workspace +
            '` (add --state-dir when it is not the default). ')
    else:
        reference = ('The original failure did not allocate a workspace; '
                     'inspect the recorded diagnostic reference and correct the environment. ')
    if isinstance(copy_id, str) and _WORKSPACE.fullmatch(copy_id):
        return ('Preparation stopped before using the explicit copy ' + copy_id +
                ': an identical ' + label + ' failure was already recorded for '
                'this project and its source/runtime/tool conditions. ' + reference +
                'The current explicit copy was left untouched and no additional '
                'workspace was allocated and no provider credential was read. '
                'After correcting the environment, start a new logical job '
                '(or a fresh declarative preparation); if an indirect condition changed '
                'or a fresh diagnostic probe is needed, pass --recheck-environment. '
                'Time did not expire this record.')
    return ('Preparation stopped before allocating a workspace: an identical '
            + label + ' failure was already recorded for this project and its '
            'source/runtime/tool conditions. ' + reference +
            'No new workspace was allocated and no provider credential was read. '
            'After correcting the environment, retry through a new logical job '
            '(or a fresh declarative preparation); if an indirect condition changed '
            'or a fresh diagnostic probe is needed, pass --recheck-environment. '
            'Time did not expire this record.')
