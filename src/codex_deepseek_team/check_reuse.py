"""Conservative, workspace-local reuse of explicitly deterministic checks.

Reuse is opt-in and positive-only.  A successful complete command set is
retained only when the project tree, prepared inputs, commands, timeout,
runtime/access identity, layout, environment identity, executable bytes and
evidence directory are unchanged before and after execution.  Unknown input,
credential-like project files, links, special files, races and bounded-out
snapshots make reuse ineligible rather than guessing.

The retained evidence lives in the existing workspace diagnostics directory.
This module validates its paths, ownership, type, mode and stream digests before
a hit, and returns the original rows marked ``reused`` with their cache id in
``reused_from``.  It never substitutes new output for old output.

Environment normalization is intentionally narrow.  The explicit deterministic
check attestation permits these exact variable mappings: ``HOME``,
``CODEX_HOME`` and ``CLAUDE_CONFIG_DIR`` become ``<runtime-home>``;
``TMPDIR``, ``TEMP``, ``TMP`` and ``XDG_RUNTIME_DIR`` become ``<temporary>``.
Layout normalization replaces only the runtime home/temporary roots named by
that environment and the control directory bound at ``/run/deepseek-team``.
The remaining path suffix is retained, project paths stay verbatim, and
``/proc/self/fd`` tokens are resolved to their targets before replacement so
different descriptors never collapse together.  Known provider credential variables are
omitted from the identity: ``DEEPSEEK_API_KEY``, ``OPENAI_API_KEY``,
``ANTHROPIC_API_KEY``, ``ANTHROPIC_AUTH_TOKEN`` and
``CLAUDE_CODE_OAUTH_TOKEN``.  Credential-like values and values passed in
``redact_values`` are redacted from retained check output; ``redact_values`` do
not hide other meaningful environment inputs.  Commands containing a known
credential value are never cached.  Other credential-like or socket environment
names, and arbitrary external inputs, make the run ineligible for reuse.
Executable identity accepts non-writable system binaries owned by root or the
current worker user; project inputs remain current-user-owned.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import time
import uuid

from . import check_evidence, prepared_inputs, verification
from .workspace import WorkspaceError, _credential_like

SCHEMA = 'deepseek-team-check-reuse-v1'
ENTRY_SCHEMA = 'deepseek-team-check-reuse-entry-v1'
METADATA_KEY = 'check_reuse'

MAX_TREE_ENTRIES = 100_000
MAX_TREE_BYTES = 512 * 1024 * 1024
MAX_TREE_DEPTH = 128
MAX_PATH_BYTES = 4096
MAX_COMMANDS = 64
MAX_COMMAND_BYTES = 32 * 1024
MAX_EXECUTABLE_BYTES = 512 * 1024 * 1024
MAX_CACHE_ENTRIES = 8
MAX_CACHE_BYTES = 512 * 1024
MAX_CACHE_SCAN = MAX_CACHE_ENTRIES * 4
CHUNK_SIZE = 1024 * 1024

_SAFE_PHASE = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z')
_SAFE_EVIDENCE_NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z')
_DIGEST = re.compile(r'[0-9a-f]{64}\Z')

_HARNESS_HOME_NAMES = frozenset(('HOME', 'CODEX_HOME', 'CLAUDE_CONFIG_DIR'))
_TEMPORARY_NAMES = frozenset(('TMPDIR', 'TEMP', 'TMP', 'XDG_RUNTIME_DIR'))
_KNOWN_PROVIDER_SECRETS = frozenset((
    'DEEPSEEK_API_KEY', 'OPENAI_API_KEY', 'ANTHROPIC_API_KEY',
    'ANTHROPIC_AUTH_TOKEN', 'CLAUDE_CODE_OAUTH_TOKEN',
))
_CREDENTIAL_NAME = re.compile(
    r'(?:^|_)(?:API_?KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|AUTH|COOKIE|SESSION)(?:_|$)',
    re.IGNORECASE)
_SOCKET_NAMES = frozenset(('SSH_AUTH_SOCK', 'DBUS_SESSION_BUS_ADDRESS', 'DOCKER_HOST'))
_SHELL_BUILTINS = frozenset((
    '.', ':', '[', 'break', 'cd', 'command', 'continue', 'echo', 'eval', 'exec',
    'exit', 'export', 'false', 'getopts', 'hash', 'pwd', 'read', 'readonly',
    'return', 'set', 'shift', 'test', 'times', 'trap', 'true', 'type', 'ulimit',
    'umask', 'unalias', 'unset', 'wait', 'printf', 'kill', 'jobs', 'fg', 'bg',
))


class CheckReuseError(Exception):
    """Reuse metadata or a required safety invariant is unusable."""

    def __init__(self, message: str, code: int = 78):
        self.message, self.code = message, code
        super().__init__(message)


class _Ineligible(Exception):
    """The current inputs cannot be attested as deterministic and bounded."""


def _command_values(commands):
    if isinstance(commands, str):
        values = [commands]
    elif commands is None:
        values = []
    else:
        try:
            values = list(commands)
        except TypeError:
            raise CheckReuseError('Check commands must be a string or an iterable of strings.', 64) from None
    for command in values:
        if not isinstance(command, str) or not command or '\0' in command:
            raise CheckReuseError('Check commands must be non-empty strings without NUL bytes.', 64)
    return values


def _commands(values):
    if len(values) > MAX_COMMANDS:
        raise _Ineligible('too-many-commands')
    total = 0
    for command in values:
        total += len(command.encode('utf-8', 'surrogateescape'))
        if total > MAX_COMMAND_BYTES:
            raise _Ineligible('bounded-commands')
    return values


def _validate_timeout(timeout):
    if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
            or not 0 < float(timeout) < float('inf')):
        raise CheckReuseError('Check reuse timeout must be a positive finite number.', 64)
    return float(timeout)


def _require_lock(copy):
    if not getattr(copy, '_locked', False):
        raise CheckReuseError('Check reuse requires the existing exclusive workspace lock.')


def _within(root: Path, path: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _validate_directory(copy, directory: Path):
    directory = Path(directory)
    resolved = Path(os.path.realpath(directory))
    control = Path(os.path.realpath(copy.directory))
    work = Path(os.path.realpath(copy.path))
    if resolved == work or _within(work, resolved):
        raise CheckReuseError('Check evidence must stay outside the project tree.', 78)
    if not _within(control, resolved):
        raise CheckReuseError('Check evidence must stay inside the workspace control directory.', 78)
    try:
        info = os.lstat(directory)
    except FileNotFoundError:
        return directory
    except OSError:
        raise CheckReuseError('Check evidence directory cannot be inspected safely.', 78) from None
    if (stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.geteuid()):
        raise CheckReuseError('Check evidence directory must be an owner-owned ordinary directory.', 78)
    return directory


def _snapshot_tree(copy) -> str:
    root = Path(copy.path)
    try:
        root_info = os.lstat(root)
    except OSError:
        raise _Ineligible('workspace-unavailable') from None
    if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
        raise _Ineligible('workspace-not-ordinary')
    digest = hashlib.sha256()
    digest.update(b'D\0' + str(stat.S_IMODE(root_info.st_mode)).encode() + b'\0')
    entries = 0
    total = 0

    def account():
        nonlocal entries
        entries += 1
        if entries > MAX_TREE_ENTRIES:
            raise _Ineligible('bounded-entries')

    def walk(directory: Path, prefix: tuple[str, ...]):
        nonlocal total
        if len(prefix) > MAX_TREE_DEPTH:
            raise _Ineligible('bounded-depth')
        try:
            names = sorted(os.listdir(directory), key=os.fsencode)
        except OSError:
            raise _Ineligible('unreadable-directory') from None
        for name in names:
            rel = prefix + (name,)
            rel_bytes = os.fsencode('/'.join(rel))
            if len(rel_bytes) > MAX_PATH_BYTES:
                raise _Ineligible('bounded-path')
            if rel[0] == '.git':
                continue
            if '.git' in rel[1:]:
                raise _Ineligible('nested-git-control-data')
            if _credential_like(Path(*rel)):
                raise _Ineligible('credential-like-input')
            path = directory / name
            try:
                info = os.lstat(path)
            except OSError:
                raise _Ineligible('unstable-entry') from None
            if info.st_uid != os.geteuid():
                raise _Ineligible('foreign-owned-input')
            account()
            if stat.S_ISLNK(info.st_mode):
                raise _Ineligible('linked-input')
            if stat.S_ISDIR(info.st_mode):
                digest.update(b'D\0' + rel_bytes + b'\0' +
                              str(stat.S_IMODE(info.st_mode)).encode() + b'\0' +
                              str(info.st_mtime_ns).encode() + b'\0' +
                              str(info.st_ctime_ns).encode() + b'\0')
                walk(path, rel)
                continue
            if not stat.S_ISREG(info.st_mode):
                raise _Ineligible('special-input')
            if info.st_nlink != 1:
                raise _Ineligible('linked-input')
            if info.st_size > MAX_TREE_BYTES - total:
                raise _Ineligible('bounded-bytes')
            file_digest = hashlib.sha256()
            try:
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
            except OSError:
                raise _Ineligible('unreadable-input') from None
            try:
                opened = os.fstat(fd)
                if ((opened.st_dev, opened.st_ino, opened.st_mode, opened.st_size,
                     opened.st_mtime_ns, opened.st_ctime_ns) !=
                        (info.st_dev, info.st_ino, info.st_mode, info.st_size,
                         info.st_mtime_ns, info.st_ctime_ns)):
                    raise _Ineligible('racy-input')
                while True:
                    chunk = os.read(fd, CHUNK_SIZE)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_TREE_BYTES:
                        raise _Ineligible('bounded-bytes')
                    file_digest.update(chunk)
                closed = os.fstat(fd)
            except OSError:
                raise _Ineligible('unreadable-input') from None
            finally:
                os.close(fd)
            if ((closed.st_dev, closed.st_ino, closed.st_mode, closed.st_size,
                 closed.st_mtime_ns, closed.st_ctime_ns) !=
                    (opened.st_dev, opened.st_ino, opened.st_mode, opened.st_size,
                     opened.st_mtime_ns, opened.st_ctime_ns)):
                raise _Ineligible('racy-input')
            digest.update(b'F\0' + rel_bytes + b'\0' +
                          str(stat.S_IMODE(opened.st_mode)).encode() + b'\0' +
                          str(opened.st_mtime_ns).encode() + b'\0' +
                          str(opened.st_ctime_ns).encode() + b'\0' +
                          file_digest.digest())

    walk(root, ())
    return digest.hexdigest()


def _hash_executable(path: Path, depth: int = 0):
    if depth > 8:
        raise _Ineligible('unresolved-executable')
    try:
        info = os.lstat(path)
    except OSError:
        raise _Ineligible('unresolved-executable') from None
    if stat.S_ISLNK(info.st_mode):
        try:
            link = os.readlink(path)
        except OSError:
            raise _Ineligible('unresolved-executable') from None
        target = Path(os.path.realpath(path))
        target_identity = _hash_executable(target, depth + 1)
        return {'path': str(path), 'link': link, 'target': target_identity}
    if (not stat.S_ISREG(info.st_mode) or info.st_size > MAX_EXECUTABLE_BYTES
            or info.st_uid not in (0, os.geteuid())
            or stat.S_IMODE(info.st_mode) & 0o022):
        raise _Ineligible('unsafe-executable')
    digest = hashlib.sha256()
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        with os.fdopen(fd, 'rb') as stream:
            source = os.fstat(stream.fileno())
            if (source.st_dev, source.st_ino, source.st_mode, source.st_size,
                    source.st_mtime_ns, source.st_ctime_ns) != (
                    info.st_dev, info.st_ino, info.st_mode, info.st_size,
                    info.st_mtime_ns, info.st_ctime_ns):
                raise _Ineligible('racy-executable')
            for chunk in iter(lambda: stream.read(CHUNK_SIZE), b''):
                digest.update(chunk)
            closed = os.fstat(stream.fileno())
    except OSError:
        raise _Ineligible('unreadable-executable') from None
    if ((closed.st_dev, closed.st_ino, closed.st_mode, closed.st_size,
         closed.st_mtime_ns, closed.st_ctime_ns) !=
            (source.st_dev, source.st_ino, source.st_mode, source.st_size,
             source.st_mtime_ns, source.st_ctime_ns)):
        raise _Ineligible('racy-executable')
    return {'path': str(path), 'mode': stat.S_IMODE(info.st_mode),
            'size': info.st_size, 'sha256': digest.hexdigest()}


def _resolve_executable(value: str, copy, env):
    if value in _SHELL_BUILTINS:
        return {'builtin': value}
    if not value or '\0' in value:
        raise _Ineligible('invalid-executable')
    if '/' in value:
        path = Path(value)
        if not path.is_absolute():
            path = Path(copy.path) / path
    else:
        found = shutil.which(value, path=env.get('PATH', ''))
        if found is None or not os.path.isabs(found):
            raise _Ineligible('unresolved-executable')
        path = Path(found)
    return _hash_executable(path)


def _executables(copy, commands, env):
    identities = []
    for command in commands:
        try:
            first = verification.first_executable(command)
        except verification.CheckCommandError:
            raise _Ineligible('invalid-check-command') from None
        identities.append([first, _resolve_executable(first, copy, env)])
    return identities


def _layout_roots(copy, layout, env):
    """Known per-run harness roots, longest first so suffixes stay meaningful."""
    roots = []
    if isinstance(env, dict):
        for name, value in env.items():
            if not isinstance(name, str) or not isinstance(value, str):
                continue
            if name in _HARNESS_HOME_NAMES:
                placeholder = '<runtime-home>'
            elif name in _TEMPORARY_NAMES:
                placeholder = '<temporary>'
            else:
                continue
            normalized = os.path.normpath(value)
            if os.path.isabs(normalized) and normalized != os.sep:
                roots.append((normalized, placeholder))
    for index in range(2, len(layout)):
        if layout[index] != '/run/deepseek-team':
            continue
        option, source = layout[index - 2], layout[index - 1]
        if (isinstance(option, str) and option.startswith('--') and 'bind' in option
                and isinstance(source, str) and os.path.isabs(source)):
            normalized = os.path.normpath(source)
            if normalized != os.sep:
                roots.append((normalized, '<workspace-control>'))
    roots.sort(key=lambda item: (-len(item[0]), item[1]))
    return roots


def _normalize_known_path(value, roots, work):
    candidate = os.path.normpath(value)
    if not os.path.isabs(candidate) or _within(work, Path(candidate)):
        return value
    for root, placeholder in roots:
        if candidate == root:
            return placeholder
        if candidate.startswith(root.rstrip(os.sep) + os.sep):
            return placeholder + '/' + candidate[len(root.rstrip(os.sep)) + 1:]
    return value


def _normalized_layout(copy, layout, env):
    if not isinstance(layout, (list, tuple)) or not layout:
        raise _Ineligible('missing-layout')
    work = Path(os.path.realpath(copy.path))
    roots = _layout_roots(copy, layout, env)
    values = []
    for token in layout:
        if not isinstance(token, str) or '\0' in token:
            raise _Ineligible('invalid-layout')
        if token.startswith('/proc/self/fd/'):
            remainder = token[len('/proc/self/fd/'):]
            number, _, suffix = remainder.partition('/')
            if number.isdigit():
                try:
                    target = os.path.realpath(os.readlink('/proc/self/fd/' + number))
                except OSError:
                    values.append(token)
                    continue
                if suffix:
                    target = os.path.join(target, suffix)
                values.append(_normalize_known_path(target, roots, work))
                continue
        values.append(_normalize_known_path(token, roots, work))
    return values


def _secret_values(redact_values):
    if isinstance(redact_values, (str, bytes)):
        redact_values = (redact_values,)
    result = []
    for value in redact_values or ():
        if isinstance(value, str):
            value = value.encode('utf-8', 'surrogateescape')
        if isinstance(value, bytes) and value:
            result.append(value)
    return tuple(result)


def _environment_identity(env):
    if not isinstance(env, dict):
        raise _Ineligible('invalid-environment')
    normalized = []
    for name, value in env.items():
        if not isinstance(name, str) or not name or '\0' in name or not isinstance(value, str):
            raise _Ineligible('invalid-environment')
        if name in _KNOWN_PROVIDER_SECRETS:
            continue
        if name in _HARNESS_HOME_NAMES:
            normalized.append([name, '<runtime-home>'])
            continue
        if name in _TEMPORARY_NAMES:
            normalized.append([name, '<temporary>'])
            continue
        if name in _SOCKET_NAMES or name.endswith('_SOCK'):
            raise _Ineligible('external-socket-environment')
        if _CREDENTIAL_NAME.search(name):
            raise _Ineligible('credential-like-environment')
        normalized.append([name, value])
    normalized.sort()
    return normalized


def _stable_toolchain(copy):
    raw = copy.metadata.get('toolchain')
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise _Ineligible('invalid-toolchain-metadata')
    value = dict(raw)
    value.pop('prepared_at', None)
    return value


def _identity(copy, layout, env, commands, timeout, directory):
    tree = _snapshot_tree(copy)
    try:
        prepared = prepared_inputs.input_snapshot(copy)
    except (WorkspaceError, OSError):
        raise _Ineligible('prepared-input-unavailable') from None
    metadata = copy.metadata
    canonical = {
        'schema': SCHEMA,
        'base_head': metadata.get('base_head'),
        'git_digest': metadata.get('git_digest'),
        'work_inode': metadata.get('work_inode'),
        'runtime': metadata.get('runtime'),
        'runtime_binary': metadata.get('runtime_binary'),
        'runtime_explicit': metadata.get('runtime_explicit'),
        'effective_access': metadata.get('effective_access', 'full-access'),
        'toolchain': _stable_toolchain(copy),
        'prepared_inputs': prepared,
        'tree': tree,
        'layout': _normalized_layout(copy, layout, env),
        'layout_executable': _resolve_executable(layout[0], copy, env),
        'environment': _environment_identity(env),
        'executables': _executables(copy, commands, env),
        'shell': _hash_executable(Path('/bin/sh')),
        'commands': commands,
        'timeout': timeout,
        'directory': os.path.realpath(directory),
    }
    binary = metadata.get('runtime_binary')
    if isinstance(binary, str) and binary:
        path = Path(binary)
        if not path.is_absolute() and '/' not in binary:
            found = shutil.which(binary, path=env.get('PATH', ''))
            if found is None or not os.path.isabs(found):
                raise _Ineligible('unresolved-runtime-binary')
            path = Path(found)
        canonical['runtime_binary_identity'] = _hash_executable(path)
    try:
        raw = json.dumps(canonical, sort_keys=True, separators=(',', ':'),
                         ensure_ascii=True).encode()
    except (TypeError, ValueError):
        raise _Ineligible('unencodable-identity') from None
    return hashlib.sha256(raw).hexdigest()


def _entries(copy):
    raw = copy.metadata.get(METADATA_KEY)
    if raw is None:
        return []
    if (not isinstance(raw, dict) or raw.get('schema') != SCHEMA
            or not isinstance(raw.get('entries'), list)):
        return []
    if len(raw['entries']) > MAX_CACHE_SCAN:
        return []
    return [entry for entry in raw['entries'] if isinstance(entry, dict)]


def _finite_timestamp(value):
    return (not isinstance(value, bool) and isinstance(value, (int, float))
            and value == value and abs(value) != float('inf') and value >= 0)


def _valid_entry(entry, identity, commands, timeout):
    return (isinstance(entry, dict)
            and set(entry) == {'schema', 'id', 'identity', 'created_at', 'commands',
                               'timeout', 'phase', 'rows'}
            and entry.get('schema') == ENTRY_SCHEMA
            and isinstance(entry.get('id'), str) and re.fullmatch('[0-9a-f]{32}', entry['id'])
            and entry.get('identity') == identity
            and _finite_timestamp(entry.get('created_at'))
            and entry.get('commands') == commands
            and bool(entry.get('commands'))
            and entry.get('timeout') == timeout
            and isinstance(entry.get('phase'), str) and _SAFE_PHASE.fullmatch(entry['phase'])
            and isinstance(entry.get('rows'), list))


def _retainable(entry):
    return (isinstance(entry, dict)
            and set(entry) == {'schema', 'id', 'identity', 'created_at', 'commands',
                               'timeout', 'phase', 'rows'}
            and entry.get('schema') == ENTRY_SCHEMA
            and isinstance(entry.get('id'), str) and re.fullmatch('[0-9a-f]{32}', entry['id'])
            and isinstance(entry.get('identity'), str) and bool(_DIGEST.fullmatch(entry['identity']))
            and _finite_timestamp(entry.get('created_at'))
            and isinstance(entry.get('commands'), list)
            and bool(entry.get('commands'))
            and all(isinstance(command, str) and command for command in entry['commands'])
            and isinstance(entry.get('timeout'), float) and entry['timeout'] > 0
            and isinstance(entry.get('phase'), str) and bool(_SAFE_PHASE.fullmatch(entry['phase']))
            and isinstance(entry.get('rows'), list)
            and all(isinstance(row, dict) for row in entry['rows']))


def _find_entry(copy, identity, commands, timeout):
    matches = [entry for entry in _entries(copy)
               if _valid_entry(entry, identity, commands, timeout)]
    if not matches:
        return None
    return max(matches, key=lambda entry: entry['created_at'])


def _evidence_name(value):
    return (isinstance(value, str) and bool(value) and '\0' not in value
            and '/' not in value and '\\' not in value and value not in ('.', '..')
            and bool(_SAFE_EVIDENCE_NAME.fullmatch(value)))


def _read_evidence(directory: Path, name: str, expected: str, redactions):
    if not _evidence_name(name) or not isinstance(expected, str) or not _DIGEST.fullmatch(expected):
        return None
    path = directory / name
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_size > check_evidence.MAX_OUTPUT_BYTES):
            return None
        digest = hashlib.sha256()
        content = bytearray()
        size = 0
        while True:
            chunk = os.read(fd, CHUNK_SIZE)
            if not chunk:
                break
            size += len(chunk)
            if size > check_evidence.MAX_OUTPUT_BYTES:
                return None
            digest.update(chunk)
            if redactions:
                content.extend(chunk)
        if size != info.st_size:
            return None
        closed = os.fstat(fd)
    except OSError:
        return None
    finally:
        os.close(fd)
    if ((closed.st_dev, closed.st_ino, closed.st_mode, closed.st_size,
         closed.st_mtime_ns, closed.st_ctime_ns) !=
            (info.st_dev, info.st_ino, info.st_mode, info.st_size,
             info.st_mtime_ns, info.st_ctime_ns)):
        return None
    actual = digest.hexdigest()
    # Current publication must not expose newly classified values. Validate
    # the bounded complete stream, including values crossing read boundaries.
    if any(value in content for value in redactions):
        return None
    return actual if actual == expected else None


def _rows_from_entry(entry, commands, directory: Path, redactions):
    if not isinstance(commands, list) or not commands:
        return None
    rows = entry.get('rows')
    if not isinstance(rows, list) or len(rows) != len(commands):
        return None
    try:
        directory_info = os.lstat(directory)
    except OSError:
        return None
    if (not stat.S_ISDIR(directory_info.st_mode) or stat.S_ISLNK(directory_info.st_mode)
            or directory_info.st_uid != os.geteuid()
            or stat.S_IMODE(directory_info.st_mode) != 0o700):
        return None
    result = []
    for row, command in zip(rows, commands):
        if (not isinstance(row, dict) or set(row) != set(check_evidence.EVIDENCE_ROW_KEYS)
                or row.get('command') != command
                or type(row.get('exit_code')) is not int or row.get('exit_code') != 0
                or row.get('timed_out') is not False or row.get('launch_failed') is not False
                or row.get('stdout_truncated') is not False
                or row.get('stderr_truncated') is not False):
            return None
        if not _SAFE_PHASE.fullmatch(str(row.get('phase', ''))):
            return None
        for stream in ('stdout', 'stderr'):
            if _read_evidence(directory, row.get(stream + '_path'),
                              row.get(stream + '_sha256'), redactions) is None:
                return None
        reused = dict(row)
        reused['reused'] = True
        reused['reused_from'] = entry['id']
        result.append(reused)
    return result


def _complete_success(rows, commands):
    return (len(rows) == len(commands) and len(commands) > 0
            and all(isinstance(row, dict)
                    and type(row.get('exit_code')) is int and row.get('exit_code') == 0
                    and row.get('timed_out') is False
                    and row.get('launch_failed') is False
                    and row.get('stdout_truncated') is False
                    and row.get('stderr_truncated') is False
                    for row in rows))


def _publish(copy, identity, commands, timeout, phase, rows):
    entry = {'schema': ENTRY_SCHEMA, 'id': uuid.uuid4().hex, 'identity': identity,
             'created_at': time.time(), 'commands': list(commands), 'timeout': timeout,
             'phase': phase, 'rows': [dict(row) for row in rows]}
    retained = [candidate for candidate in _entries(copy)
                if _retainable(candidate) and candidate.get('identity') != identity]
    retained.append(entry)
    retained.sort(key=lambda candidate: candidate.get('created_at', 0), reverse=True)
    retained = retained[:MAX_CACHE_ENTRIES]
    while retained:
        payload = {'schema': SCHEMA, 'entries': retained}
        try:
            raw = json.dumps(payload, sort_keys=True, ensure_ascii=True).encode()
        except (TypeError, ValueError):
            return
        if len(raw) <= MAX_CACHE_BYTES:
            break
        retained.pop()
    if not retained:
        return
    previous = copy.metadata.get(METADATA_KEY)
    copy.metadata[METADATA_KEY] = payload
    try:
        copy.save()
    except (OSError, WorkspaceError):
        if previous is None:
            copy.metadata.pop(METADATA_KEY, None)
        else:
            copy.metadata[METADATA_KEY] = previous


def _invalidate(copy, identity):
    """Drop a matching success after an executed run cannot confirm it."""
    raw = copy.metadata.get(METADATA_KEY)
    if (not isinstance(raw, dict) or raw.get('schema') != SCHEMA
            or not isinstance(raw.get('entries'), list)):
        return
    if len(raw['entries']) > MAX_CACHE_SCAN:
        return
    previous = copy.metadata.get(METADATA_KEY)
    kept = ([] if identity is None else
            [entry for entry in raw['entries']
             if not (isinstance(entry, dict) and _retainable(entry)
                     and entry.get('identity') == identity)])
    if len(kept) == len(raw['entries']):
        return
    if kept:
        copy.metadata[METADATA_KEY] = {'schema': SCHEMA, 'entries': kept}
    else:
        copy.metadata.pop(METADATA_KEY, None)
    try:
        copy.save()
    except (OSError, WorkspaceError):
        if previous is None:
            copy.metadata.pop(METADATA_KEY, None)
        else:
            copy.metadata[METADATA_KEY] = previous


def _evidence_redactions(env, redact_values):
    """Known credential values that must never enter retained check output."""
    values = list(_secret_values(redact_values))
    if isinstance(env, dict):
        for name, value in env.items():
            if (isinstance(name, str) and isinstance(value, str)
                    and (name in _KNOWN_PROVIDER_SECRETS or _CREDENTIAL_NAME.search(name))):
                payload = value.encode('utf-8', 'surrogateescape')
                if payload:
                    values.append(payload)
    return tuple(dict.fromkeys(values))


def _within_deadline(deadline):
    return deadline is None or time.monotonic() < deadline


def run_checks(copy, layout: list[str], env: dict[str, str], commands: list[str], *,
               timeout: float = 120, deadline: float | None = None,
               directory: Path, phase: str = 'post', reusable: bool = False,
               force: bool = False, redact_values=()) -> list[dict]:
    """Run checks with conservative, evidence-validated reuse when attested.

    ``reusable`` is the explicit deterministic local-check attestation.  Without
    it this function never reuses or publishes cache metadata, but a fresh
    failed observation still invalidates a matching retained success.  ``force``
    bypasses a hit but may refresh positive evidence after a complete unchanged
    success; a failed or unattested forced run invalidates matching evidence
    instead of retaining an unconfirmed success.  A cached hit is never used
    after the shared deadline expires, including expiry during validation.
    Readiness/security probes must continue to call without a copy or with
    ``reusable=False`` and therefore remain direct mandatory executions.
    """
    _require_lock(copy)
    if type(reusable) is not bool or type(force) is not bool:
        raise CheckReuseError('Check reuse and force must be booleans.', 64)
    values = _command_values(commands)
    cacheable = reusable
    bounded = True
    try:
        commands = _commands(values)
    except _Ineligible:
        commands = values
        cacheable = False
        bounded = False
    cacheable = cacheable and bool(commands)
    timeout = _validate_timeout(timeout)
    if not isinstance(phase, str) or not _SAFE_PHASE.fullmatch(phase):
        raise CheckReuseError('Check evidence phase must be a short safe name.')
    directory = _validate_directory(copy, Path(directory))
    redact_values = _secret_values(redact_values)
    redactions = _evidence_redactions(env, redact_values)
    if cacheable and any(secret in command.encode('utf-8', 'surrogateescape')
                         for command in commands for secret in redactions):
        cacheable = False

    identity = None
    if bounded and bool(commands) and _within_deadline(deadline):
        try:
            identity = _identity(copy, layout, env, commands, timeout, directory)
        except _Ineligible:
            identity = None
        if cacheable and identity is not None and not force and _within_deadline(deadline):
            entry = _find_entry(copy, identity, commands, timeout)
            rows = (None if entry is None else
                    _rows_from_entry(entry, commands, directory, redactions))
            if (rows is not None and _within_deadline(deadline)):
                try:
                    unchanged = (_identity(copy, layout, env, commands, timeout,
                                           directory) == identity)
                except _Ineligible:
                    unchanged = False
                if unchanged and _within_deadline(deadline):
                    rows = _rows_from_entry(entry, commands, directory, redactions)
                    if rows is not None and _within_deadline(deadline):
                        return rows

    try:
        rows = check_evidence.run_checks(layout, env, commands, timeout, deadline=deadline,
                                         directory=directory, phase=phase,
                                         redact_values=redactions)
    except BaseException:
        _invalidate(copy, identity)
        raise
    fresh = [dict(row, reused=False) for row in rows]
    if identity is None and not _complete_success(rows, commands):
        # An expired deadline or unknown identity cannot identify one cache key.
        # Conservatively revoke retained successes after a failed observation.
        _invalidate(copy, None)
    if identity is not None:
        publish = False
        if cacheable and _complete_success(rows, commands):
            try:
                unchanged = _identity(copy, layout, env, commands, timeout,
                                      directory) == identity
            except _Ineligible:
                unchanged = False
            publish = unchanged
        if publish:
            _publish(copy, identity, commands, timeout, phase, rows)
        elif cacheable or force or not _complete_success(rows, commands):
            _invalidate(copy, identity)
    return fresh
