"""Explicit, reusable workspace-local toolchain preparation.

A coordinator selects host software directories and explicit non-secret files or
build caches. ``prepare`` copies them into the owned copy (``.deepseek-tools/NAME``
and the declared destinations), materializes symlinks that stay inside the
selected source roots, records private versioned metadata on the copy and
optionally saves a private per-project recipe for replay into a later copy.

Nothing here reads the provider credential, executes a host command, mounts a
host path or interprets a recipe as shell input. Recipes contain only explicit
artifact/environment/probe declarations; ``apply_saved_recipe`` copies those
declarations again and never runs them. Smoke probes are returned by
``probes_for`` and are executed by the namespace runner, not by this module.
"""
from __future__ import annotations

from contextlib import ExitStack, contextmanager, nullcontext
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import time
import uuid

from . import preparation_guard, workspace

TOOLCHAIN_KEY = 'toolchain'
TOOLCHAIN_VERSION = 2
LEGACY_TOOLCHAIN_VERSION = 1
TOOLCHAIN_VERSIONS = frozenset({LEGACY_TOOLCHAIN_VERSION, TOOLCHAIN_VERSION})
RECIPE_KIND = 'toolchain-recipe'
TOOLS_DIRNAME = '.deepseek-tools'
RECIPES_DIRNAME = 'toolchain-recipes'

_TOOL_NAME = re.compile(r'[A-Za-z][A-Za-z0-9_-]{0,63}\Z')
_ALLOWED_VARIABLES = frozenset({'JAVA_HOME', 'ANDROID_HOME', 'ANDROID_SDK_ROOT',
                                'GRADLE_USER_HOME', 'M2_HOME', 'KOTLIN_HOME', 'PATH'})
_PATH_VARIABLES = _ALLOWED_VARIABLES - {'PATH'}
_PLACEHOLDER = '{workspace}'
_BROAD_ROOTS = frozenset(str(path) for path in (
    Path('/'), Path('/usr'), Path('/etc'), Path('/home'), Path('/root'),
    Path('/tmp'), Path('/var'), Path('/run')))
_MAX_LINK_DEPTH = 64
_MAX_PROBES = 64
_MAX_PROBE_LENGTH = 8192
_MAX_RECIPE_BYTES = 1 << 20
_ALLOCATION_BLOCK = 4096

# Debian-style JDK configuration links. Only ordinary files below a concrete
# OpenJDK configuration directory and the exact public truststore may leave the
# selected software root; the whole /etc tree is never exposed.
_JAVA_CONF_RE = re.compile(r'^/etc/java-[0-9]+-openjdk/')
_JAVA_TRUSTSTORE = '/etc/ssl/certs/java/cacerts'

# Debian/OpenJDK also ships non-runtime documentation and source links that
# point at public package locations outside the selected software root. Only
# those exact links are omitted, without following, reading or binding their
# targets; every other unapproved escaping link still fails preparation.
_JAVA_DOC_LINK = 'docs'
_JAVA_SOURCE_LINKS = frozenset({'src.zip', 'lib/src.zip'})
_JAVA_DOC_TARGET = re.compile(r'^/usr/share/doc/openjdk-[0-9]+-')
_JAVA_SOURCE_TARGET = re.compile(r'^/usr/lib/jvm/openjdk-[0-9]+/src\.zip\Z')

_replace = os.replace  # atomic publication seam (patchable in tests)


class ToolchainError(Exception):
    def __init__(self, message: str, code: int = 78):
        self.message, self.code = message, code
        super().__init__(message)


class MissingSourceError(ToolchainError):
    """A declared materialization source is observably absent."""


def _display(value) -> str:
    return workspace._escape_name(str(value))


def _java_external_allowed(value: str) -> bool:
    return value == _JAVA_TRUSTSTORE or _JAVA_CONF_RE.match(value) is not None


def _java_omitted_link(relative: Path, link: Path) -> bool:
    """True for the exact public Debian docs/src.zip links that stay unmaterialized.

    The decision is made lexically from the link itself so the public package
    target is never resolved, read or bound.
    """
    name = relative.as_posix()
    if name != _JAVA_DOC_LINK and name not in _JAVA_SOURCE_LINKS:
        return False
    try:
        target = os.readlink(link)
    except OSError:
        return False
    combined = Path(target) if os.path.isabs(target) else link.parent / target
    lexical = os.path.normpath(str(combined))
    if name == _JAVA_DOC_LINK:
        return _JAVA_DOC_TARGET.match(lexical) is not None
    return _JAVA_SOURCE_TARGET.match(lexical) is not None


def _state_root(copy) -> Path:
    return Path(copy.directory).parent.parent


def _canonical_source(copy) -> Path:
    return Path(os.path.realpath(copy.source))


def _recipe_key(copy) -> str:
    return hashlib.sha256(str(_canonical_source(copy)).encode('utf-8', 'surrogateescape')).hexdigest()


def _recipe_file(copy) -> Path:
    return _state_root(copy) / RECIPES_DIRNAME / (_recipe_key(copy) + '.json')


def _ensure_private_directory(path: Path, *, mode: int = 0o700) -> None:
    try:
        path.mkdir(mode=mode, parents=True, exist_ok=True)
    except OSError:
        raise ToolchainError(f'Cannot create private toolchain state directory: {_display(path)}.') from None
    info = os.lstat(path)
    if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
            or info.st_uid != os.geteuid() or info.st_mode & 0o077):
        raise ToolchainError(f'Private toolchain state directory is unsafe: {_display(path)}.')


def _remove(path: Path) -> None:
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return
    if stat.S_ISDIR(info.st_mode):
        shutil.rmtree(path)
    else:
        os.unlink(path)


def _atomic_private_write(path: Path, content: bytes) -> None:
    temporary = path.parent / ('.' + path.name + '.tmp-' + uuid.uuid4().hex)
    fd = None
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'wb') as stream:
            fd = None
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        _replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if fd is not None:
            os.close(fd)
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _check_placeholders(value: str, *, what: str) -> None:
    remainder = value.replace(_PLACEHOLDER, '')
    if '{' in remainder or '}' in remainder:
        raise ToolchainError(f'{what} may only use the literal {_PLACEHOLDER} placeholder, '
                             'never another placeholder.')


def _normalize_tools(tools) -> dict[str, object]:
    if tools is None:
        return {}
    if not isinstance(tools, dict):
        raise ToolchainError('tools must map short names to explicitly selected software directories.')
    normalized = {}
    for name, source in tools.items():
        if not isinstance(name, str) or _TOOL_NAME.fullmatch(name) is None:
            raise ToolchainError('Tool names must match [A-Za-z][A-Za-z0-9_-]{0,63}.')
        normalized[name] = source
    return normalized


def _normalize_copies(copies) -> list[tuple[object, str]]:
    if copies is None:
        return []
    if not isinstance(copies, (list, tuple)):
        raise ToolchainError('copies must be a list of (host_source, relative_destination) pairs.')
    normalized = []
    for entry in copies:
        if not isinstance(entry, (list, tuple)) or len(entry) != 2:
            raise ToolchainError('Every copies entry must be a (host_source, relative_destination) pair.')
        normalized.append((entry[0], entry[1]))
    return normalized


def _cache_dir_value(value) -> str:
    """Validate one declared mutable cache root lexically inside the copy."""
    if isinstance(value, os.PathLike):
        value = os.fspath(value)
    if (not isinstance(value, str) or not value or '\0' in value
            or '\n' in value or '\r' in value):
        raise ToolchainError('Cache directories must be non-empty relative paths without '
                             'NUL, carriage-return or newline characters.')
    pure = PurePosixPath(value)
    parts = pure.parts
    if (not parts or pure.is_absolute() or '..' in parts
            or any(part in ('', '.') for part in parts)):
        raise ToolchainError(f'Cache directory cannot escape the owned copy: {_display(value)}.')
    if pure.as_posix() != value:
        raise ToolchainError(f'Cache directory must be a normalized relative path: '
                             f'{_display(value)}.')
    if TOOLS_DIRNAME in parts:
        raise ToolchainError('Cache directories cannot overlap the private tool area '
                             f'{TOOLS_DIRNAME}/: {_display(value)}.')
    if '.git' in parts:
        raise ToolchainError(f'Cache directory cannot touch Git metadata: {_display(value)}.')
    if any(workspace._credential_like(Path(*parts[:index + 1]))
           for index in range(len(parts))):
        raise ToolchainError(f'Refusing credential-like cache directory: {_display(value)}.')
    return pure.as_posix()


def _normalize_cache_dirs(cache_dirs) -> list[str]:
    """Return a sorted minimal set of safe workspace-relative cache roots."""
    if cache_dirs is None:
        return []
    if not isinstance(cache_dirs, (list, tuple)):
        raise ToolchainError('cache_dirs must be a list of workspace-relative directories.')
    normalized = {_cache_dir_value(value) for value in cache_dirs}
    minimal = []
    for value in normalized:
        if any(other != value and value.startswith(other + '/') for other in normalized):
            continue
        minimal.append(value)
    return sorted(minimal)


def _cache_directory_state(copy, relative: str) -> str:
    """Inspect a cache path without following any component symlink."""
    current = copy.path
    for part in PurePosixPath(relative).parts:
        current = current / part
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            return 'missing'
        except OSError:
            raise ToolchainError(f'Cache directory cannot be inspected safely: '
                                 f'{_display(current)}.') from None
        if stat.S_ISLNK(info.st_mode):
            raise ToolchainError(f'Cache directory has a symlink component and was not '
                                 f'created: {_display(current)}.')
        if not stat.S_ISDIR(info.st_mode):
            raise ToolchainError(f'Cache directory path is not an ordinary directory: '
                                 f'{_display(current)}.')
    return 'directory'


def _validate_cache_root_tracked(copy, relative: str) -> None:
    tracked = workspace.git(copy.path, 'ls-files', '-z', '--', relative)
    if tracked:
        first = os.fsdecode(tracked.split(b'\0', 1)[0])
        raise ToolchainError(f'Cache directory contains tracked source: {_display(first)}. '
                             'Choose a new or empty cache path; tracked files are never hidden.')


def _declared_cache_coverage(copy, previous_destinations, copy_sources):
    """Classify prepared destinations that may already own cache-root contents."""
    directories = set()
    files = set()
    for destination in previous_destinations:
        path = copy.path / destination
        try:
            info = os.lstat(path)
        except OSError:
            files.add(destination)
            continue
        if stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode):
            directories.add(destination)
        else:
            files.add(destination)
    for source, destination in copy_sources:
        if source.is_dir():
            directories.add(destination)
        else:
            files.add(destination)
    return directories, files


def _covered_by_cache_declaration(relative: str, directories, files) -> bool:
    return (relative in files
            or any(relative == directory or relative.startswith(directory + '/')
                   for directory in directories))


def _reject_unprepared_cache_data(copy, relative: str, directories, files) -> None:
    """Refuse to hide pre-existing files that this preparation did not create."""
    root = copy.path / relative

    def visit(path: Path, current: str) -> None:
        try:
            with os.scandir(path) as iterator:
                entries = list(iterator)
        except OSError:
            raise ToolchainError(f'Cache directory cannot be inspected safely: '
                                 f'{_display(current)}. No data was removed.') from None
        for entry in entries:
            child = current + '/' + entry.name
            child_path = Path(entry.path)
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError:
                raise ToolchainError(f'Cache directory entry cannot be inspected safely: '
                                     f'{_display(child)}. No data was removed.') from None
            relative_path = Path(child)
            if '.git' in relative_path.parts:
                raise ToolchainError(f'Cache directory contains Git metadata: {_display(child)}. '
                                     'No data was removed; use a new workspace copy.')
            if workspace._credential_like(relative_path):
                raise ToolchainError(f'Cache directory contains a credential-like entry: '
                                     f'{_display(child)}. No data was removed; use a new '
                                     'workspace copy.')
            if stat.S_ISLNK(info.st_mode):
                raise ToolchainError(f'Cache directory contains a symlink: {_display(child)}. '
                                     'No data was removed; use a new workspace copy.')
            if stat.S_ISDIR(info.st_mode):
                visit(child_path, child)
                continue
            if not stat.S_ISREG(info.st_mode):
                raise ToolchainError(f'Cache directory contains a special file: '
                                     f'{_display(child)}. No data was removed; use a new '
                                     'workspace copy.')
            if not _covered_by_cache_declaration(child, directories, files):
                raise ToolchainError(
                    f'Cache directory contains existing data that is not a declared prepared '
                    f'copy destination: {_display(child)}. No data was removed; create a new '
                    'workspace copy instead.')

    visit(root, relative)


def _validate_cache_declarations(copy, cache_dirs, *, previous_cache_dirs,
                                 previous_destinations, copy_sources,
                                 execution_started: bool) -> list[str]:
    """Validate the mutable cache set before any filesystem publication."""
    normalized = _normalize_cache_dirs(cache_dirs)
    previous = set(previous_cache_dirs)
    if execution_started and set(normalized) != previous:
        raise ToolchainError(
            'Cache directories are fixed after the first execution. Previous roots: '
            f'{sorted(previous)}; requested roots: {normalized}. Create a new workspace copy '
            'to declare a different mutable cache set; no cache data was removed.')

    directories, files = _declared_cache_coverage(copy, previous_destinations, copy_sources)
    directories.update(previous)
    declared_file_destinations = {destination for source, destination in copy_sources
                                  if not source.is_dir()}
    for relative in normalized:
        state = _cache_directory_state(copy, relative)
        _validate_cache_root_tracked(copy, relative)
        for destination in declared_file_destinations:
            if relative == destination or relative.startswith(destination + '/'):
                raise ToolchainError(
                    f'Cache directory would overlap a prepared file destination: '
                    f'{_display(relative)} -> {_display(destination)}.')
        if state == 'missing':
            continue
        if any(relative == old or relative.startswith(old + '/') for old in previous):
            continue
        _reject_unprepared_cache_data(copy, relative, directories, files)
    return normalized


def _workspace_execution_started(copy) -> bool:
    """True once an execution attempt was begun or reserved for this copy."""
    if copy.metadata.get('operation') == 'execution':
        return True
    from . import workspace_lifecycle
    try:
        lease = workspace_lifecycle.read(copy)
    except workspace.WorkspaceError as error:
        raise ToolchainError(str(error), error.code) from None
    if lease is None:
        return False
    if lease.get('pending_attempt') is not None:
        return True
    return any(attempt.get('operation') == 'execution'
               for attempt in lease.get('attempts', ()))


def _future_preparation_directories(copy, copy_sources, cache_dirs) -> set[Path]:
    """Directories publication will create, including file-target parents."""
    root = Path(copy.path)
    future = set()

    def add_with_parents(path: Path) -> None:
        current = Path(os.path.normpath(path))
        while current != root and current.is_relative_to(root):
            future.add(current)
            current = current.parent

    for source, destination in copy_sources:
        final = root / destination
        if source.is_dir():
            add_with_parents(final)
        add_with_parents(final.parent)
    for cache_dir in cache_dirs:
        add_with_parents(root / cache_dir)
    return future


def _validate_destination(copy, destination) -> str:
    if (not isinstance(destination, str) or not destination or '\0' in destination
            or '\n' in destination or '\r' in destination):
        raise ToolchainError('Copy destinations must be non-empty relative paths without NUL, '
                             'carriage-return or newline characters.')
    pure = PurePosixPath(destination)
    parts = pure.parts
    if (not parts or pure.is_absolute() or '..' in parts
            or any(part in ('', '.') for part in parts)):
        raise ToolchainError(f'Copy destination cannot escape the owned copy: {_display(destination)}.')
    if pure.as_posix() != destination:
        raise ToolchainError(f'Copy destination must be a normalized relative path: {_display(destination)}.')
    if '.git' in parts:
        raise ToolchainError(f'Copy destination cannot touch Git metadata: {_display(destination)}.')
    if parts[0] == TOOLS_DIRNAME:
        raise ToolchainError('Copy destinations cannot overlap the private tool area '
                             f'{TOOLS_DIRNAME}/.')
    if workspace._credential_like(Path(destination)):
        raise ToolchainError(f'Refusing credential-like copy destination: {_display(destination)}.')
    normalized = pure.as_posix()
    tracked = workspace.git(copy.path, 'ls-files', '-z', '--', normalized)
    if tracked:
        raise ToolchainError(f'Copy destination would overwrite tracked source: {_display(normalized)}.')
    return normalized


def _validate_declared_environment(environment) -> dict[str, str]:
    if environment is None:
        return {}
    if not isinstance(environment, dict):
        raise ToolchainError('environment must map allowed variable names to workspace paths.')
    normalized = {}
    for name, value in environment.items():
        if name not in _ALLOWED_VARIABLES:
            raise ToolchainError(f'Environment variable is not an allowed prepared variable: {_display(name)}.')
        if not isinstance(value, str) or not value or '\0' in value:
            raise ToolchainError(f'Invalid value for prepared variable {_display(name)}.')
        _check_placeholders(value, what=f'Prepared variable {_display(name)}')
        if name == 'PATH':
            elements = value.split(':')
            if any(not element for element in elements):
                raise ToolchainError('Prepared PATH must not contain empty elements.')
            for element in elements:
                if _PLACEHOLDER not in element:
                    raise ToolchainError('Prepared PATH elements must be workspace directories using '
                                         f'the literal {_PLACEHOLDER} placeholder.')
        else:
            if _PLACEHOLDER not in value:
                raise ToolchainError(f'Prepared variable {_display(name)} must resolve inside the copy '
                                     f'using the literal {_PLACEHOLDER} placeholder.')
        normalized[name] = value
    return normalized


def _validate_probes(probes) -> list[str]:
    if probes is None:
        return []
    if not isinstance(probes, (list, tuple)):
        raise ToolchainError('probes must be a list of explicit smoke commands.')
    if len(probes) > _MAX_PROBES:
        raise ToolchainError(f'At most {_MAX_PROBES} smoke probes may be prepared.')
    normalized = []
    for probe in probes:
        if not isinstance(probe, str) or not probe.strip() or '\0' in probe:
            raise ToolchainError('Smoke probes must be non-empty command strings without NUL bytes.')
        if len(probe) > _MAX_PROBE_LENGTH:
            raise ToolchainError('Smoke probes are limited to %d characters.' % _MAX_PROBE_LENGTH)
        _check_placeholders(probe, what='Smoke probe')
        normalized.append(probe)
    return normalized


def _validate_host_source(copy, value, *, what: str, require_directory: bool,
                          allow_workspace_input: bool = False) -> Path:
    if isinstance(value, os.PathLike):
        value = os.fspath(value)
    if not isinstance(value, str) or not value or '\0' in value:
        raise ToolchainError(f'{what} must name an existing host path.')
    raw = Path(value)
    try:
        resolved = raw.resolve(strict=True)
    except (FileNotFoundError, NotADirectoryError):
        raise MissingSourceError(
            f'{what} source does not exist or is not readable: {_display(value)}.') from None
    except (OSError, RuntimeError):
        raise ToolchainError(
            f'{what} source does not exist or is not readable: {_display(value)}.') from None
    home = Path.home()
    rejected = set(_BROAD_ROOTS) | {str(home), str(home / '.local')}
    if str(resolved) in rejected:
        raise ToolchainError(f'{what} source is a broad or mixed host location and is never exposed: '
                             f'{_display(resolved)}.')
    if workspace._credential_like(raw) or workspace._credential_like(resolved):
        raise ToolchainError(f'Refusing credential-like {what.lower()} source: {_display(raw)}.')
    copy_root = Path(os.path.realpath(copy.path))
    state_root = Path(os.path.realpath(_state_root(copy)))
    source_root = _canonical_source(copy)
    from . import toolchain_cache
    if toolchain_cache.is_entry_path(state_root, resolved):
        return toolchain_cache.validate_entry(state_root, resolved,
                                              require_directory=require_directory)
    if '.git' in resolved.parts:
        raise ToolchainError(f'Refusing checkout or Git metadata {what.lower()} source: '
                             f'{_display(resolved)}.')
    if resolved == copy_root:
        raise ToolchainError(f'{what} source cannot be the owned copy itself: {_display(resolved)}.')
    if resolved.is_relative_to(copy_root) and not allow_workspace_input:
        raise ToolchainError(f'{what} source cannot be inside the owned copy: {_display(resolved)}.')
    if resolved == state_root or state_root.is_relative_to(resolved):
        raise ToolchainError(f'{what} source cannot overlap private workspace state: {_display(resolved)}.')
    if resolved.is_relative_to(state_root):
        workspaces = state_root / 'workspaces'
        relative = resolved.relative_to(workspaces) if resolved.is_relative_to(workspaces) else None
        ordinary_workspace_input = (allow_workspace_input and relative is not None
                                    and len(relative.parts) >= 3
                                    and relative.parts[1] == 'work'
                                    and '.git' not in relative.parts)
        if not ordinary_workspace_input:
            raise ToolchainError(f'{what} source cannot overlap private workspace state: {_display(resolved)}.')
    if resolved == source_root or source_root.is_relative_to(resolved):
        raise ToolchainError(f'{what} source cannot be the source project root or its parent: '
                             f'{_display(resolved)}.')
    if require_directory and not resolved.is_dir():
        raise ToolchainError(f'{what} source must be an ordinary directory: {_display(value)}.')
    if not require_directory and not (resolved.is_dir() or resolved.is_file()):
        raise ToolchainError(f'{what} source must be an ordinary file or directory: {_display(value)}.')
    return resolved


def _validate_toolchain_metadata(copy, raw, *, check_tools: bool) -> dict:
    if not isinstance(raw, dict):
        raise ToolchainError('Prepared toolchain metadata is malformed; recreate or recover the copy.')
    version = raw.get('version')
    if type(version) is not int or version not in TOOLCHAIN_VERSIONS:
        raise ToolchainError(f'Unsupported prepared toolchain metadata version: {_display(version)}. '
                             'This package cannot interpret it safely.')
    required = {'version', 'prepared_at', 'source_project', 'tools', 'copies', 'environment',
                'probes'}
    if version == TOOLCHAIN_VERSION:
        required.add('cache_dirs')
    allowed = required | {'exclude_ownership'}
    if required - set(raw) or set(raw) - allowed:
        raise ToolchainError('Prepared toolchain metadata has unknown fields; refusing to interpret it.')
    if not isinstance(raw.get('prepared_at'), (int, float)) or isinstance(raw.get('prepared_at'), bool):
        raise ToolchainError('Prepared toolchain metadata has an invalid timestamp.')
    if not isinstance(raw.get('source_project'), str) or not raw['source_project']:
        raise ToolchainError('Prepared toolchain metadata has an invalid source project.')
    tools = raw.get('tools')
    if not isinstance(tools, dict):
        raise ToolchainError('Prepared toolchain metadata has an invalid tool map.')
    checked_tools = {}
    for name, entry in tools.items():
        if not isinstance(name, str) or _TOOL_NAME.fullmatch(name) is None:
            raise ToolchainError('Prepared toolchain metadata has an invalid tool name.')
        if (not isinstance(entry, dict) or set(entry) != {'source', 'files', 'bytes', 'links_skipped'}
                or not isinstance(entry.get('source'), str)
                or not isinstance(entry.get('files'), int) or isinstance(entry.get('files'), bool)
                or not isinstance(entry.get('bytes'), int) or isinstance(entry.get('bytes'), bool)
                or not isinstance(entry.get('links_skipped'), int)
                or isinstance(entry.get('links_skipped'), bool)):
            raise ToolchainError(f'Prepared toolchain metadata for {_display(name)} is malformed.')
        if check_tools:
            destination = copy.path / TOOLS_DIRNAME / name
            try:
                info = os.lstat(destination)
            except OSError:
                raise ToolchainError(f'Prepared tool directory is missing: {_display(destination)}. '
                                     'Recreate the copy or prepare it again explicitly.') from None
            if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
                raise ToolchainError(f'Prepared tool directory is unsafe: {_display(destination)}.')
        checked_tools[name] = dict(entry)
    copies = raw.get('copies')
    if not isinstance(copies, list):
        raise ToolchainError('Prepared toolchain metadata has an invalid copy list.')
    checked_copies = []
    for entry in copies:
        if (not isinstance(entry, dict) or set(entry) != {'source', 'destination'}
                or not isinstance(entry.get('source'), str)):
            raise ToolchainError('Prepared toolchain metadata has a malformed copy entry.')
        destination = _validate_destination(copy, entry.get('destination'))
        checked_copies.append({'source': entry['source'], 'destination': destination})
    environment = _validate_declared_environment(raw.get('environment'))
    probes = _validate_probes(raw.get('probes'))
    if version == TOOLCHAIN_VERSION:
        raw_cache_dirs = raw.get('cache_dirs')
        if not isinstance(raw_cache_dirs, list):
            raise ToolchainError('Prepared toolchain metadata has an invalid cache directory list.')
        cache_dirs = _normalize_cache_dirs(raw_cache_dirs)
    else:
        cache_dirs = []
    ownership = None
    if 'exclude_ownership' in raw:
        ownership = _validate_exclude_ownership(raw['exclude_ownership'])
        declared = set(_exclude_patterns(
            checked_tools, {entry['destination'] for entry in checked_copies}, cache_dirs))
        if set(ownership) - declared:
            raise ToolchainError(
                'Prepared toolchain metadata has inconsistent exclude ownership.')
    return {'version': version, 'prepared_at': raw['prepared_at'],
            'source_project': raw['source_project'], 'tools': checked_tools,
            'copies': checked_copies, 'environment': environment, 'probes': probes,
            'cache_dirs': cache_dirs, 'exclude_ownership': ownership}


def _load_toolchain(copy) -> dict | None:
    raw = copy.metadata.get(TOOLCHAIN_KEY)
    if raw is None:
        return None
    metadata = _validate_toolchain_metadata(copy, raw, check_tools=True)
    _validate_environment_paths(copy, metadata['environment'])
    return metadata


def _workspace_directory_state(copy, expanded: str) -> str:
    """Classify an expanded workspace path without following symlink components."""
    root = Path(copy.path)
    root_real = os.path.realpath(root)
    lexical = Path(os.path.normpath(expanded))
    real = os.path.realpath(expanded)
    if not real.startswith(root_real + os.sep):
        raise ToolchainError('Prepared environment path must resolve strictly inside the '
                             'owned copy.')
    try:
        relative = lexical.relative_to(root)
    except ValueError:
        raise ToolchainError('Prepared environment path must resolve strictly inside the '
                             'owned copy.') from None
    if not relative.parts:
        raise ToolchainError('Prepared environment path must resolve strictly inside the '
                             'owned copy.')
    current = root
    for part in relative.parts:
        current = current / part
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            return 'missing'
        except OSError:
            raise ToolchainError(f'Prepared environment path cannot be inspected safely: '
                                 f'{_display(current)}.') from None
        if stat.S_ISLNK(info.st_mode):
            raise ToolchainError(f'Prepared environment path has a symlink component: '
                                 f'{_display(current)}.')
        if not stat.S_ISDIR(info.st_mode):
            return 'other'
    return 'directory'


def _validate_environment_paths(copy, environment: dict[str, str], *, staged=(),
                                future=()) -> None:
    future = {Path(os.path.normpath(path)) for path in future}
    for name, value in environment.items():
        if name == 'PATH':
            elements = value.split(':')
        else:
            elements = [value]
        for element in elements:
            expanded = element.replace(_PLACEHOLDER, str(copy.path))
            if '\0' in expanded or not os.path.isabs(expanded):
                raise ToolchainError(f'Prepared variable {_display(name)} must resolve to an absolute '
                                     'workspace path.')
            lexical = Path(os.path.normpath(expanded))
            try:
                state = _workspace_directory_state(copy, expanded)
            except ToolchainError as error:
                raise ToolchainError(f'Prepared variable {_display(name)}: {error}') from None
            if state == 'directory':
                continue
            if state == 'missing' and (lexical in future
                                       or _staged_directory_exists(expanded, staged)):
                continue
            if state == 'missing':
                raise ToolchainError(
                    f'Prepared variable {_display(name)} does not resolve to an existing '
                    f'workspace directory or a declared preparation target: {_display(expanded)}. '
                    'Declare its parent with --copy/--cache-dir or use a fresh copy; missing '
                    'descendants and unrelated paths are never created implicitly.')
            raise ToolchainError(
                f'Prepared variable {_display(name)} exists but is not an ordinary workspace '
                f'directory: {_display(expanded)}.')


def _staged_directory_exists(expanded: str, staged) -> bool:
    lexical = Path(os.path.normpath(expanded))
    for final, staging in staged:
        if lexical == final or lexical.is_relative_to(final):
            remainder = lexical.relative_to(final)
            return (staging / remainder).is_dir()
    return False


def _resolve_link(link: Path) -> Path | None:
    seen = set()
    current = link
    for _ in range(_MAX_LINK_DEPTH):
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            return None
        except OSError:
            raise ToolchainError(f'Link target is unavailable: {_display(link)}.') from None
        if not stat.S_ISLNK(info.st_mode):
            return current
        identity = (info.st_dev, info.st_ino)
        if identity in seen:
            raise ToolchainError(f'Cyclic symlink chain: {_display(link)}.')
        seen.add(identity)
        target = Path(os.readlink(current))
        combined = target if target.is_absolute() else current.parent / target
        current = Path(os.path.normpath(combined))
    raise ToolchainError(f'Symlink chain is too deep or cyclic: {_display(link)}.')


def _identity(path: Path):
    info = os.stat(path)
    return (info.st_dev, info.st_ino)


def _within(path: Path, root: Path) -> bool:
    value, base = str(path), str(root)
    return value == base or value.startswith(base + os.sep)


def _copy_file(source: Path, target: Path, stats: dict) -> None:
    fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ToolchainError(f'Only ordinary files may be prepared: {_display(source)}.')
        out = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(out, 'wb') as destination:
            shutil.copyfileobj(stream, destination, 1 << 20)
            os.fchmod(destination.fileno(), stat.S_IMODE(info.st_mode) & 0o777)
    stats['files'] += 1
    stats['bytes'] += info.st_size


def _missing_target(link: Path) -> Path:
    """Resolve a dangling link's lexical target without following it again."""
    seen = set()
    current = link
    for _ in range(_MAX_LINK_DEPTH):
        try:
            info = os.lstat(current)
        except OSError:
            return Path(os.path.realpath(current))
        if not stat.S_ISLNK(info.st_mode):
            return Path(os.path.realpath(current))
        identity = (info.st_dev, info.st_ino)
        if identity in seen:
            return Path(os.path.realpath(current))
        seen.add(identity)
        target = Path(os.readlink(current))
        combined = target if target.is_absolute() else current.parent / target
        current = Path(os.path.normpath(combined))
    return Path(os.path.realpath(current))


def _copy_tree(source: Path, destination: Path, *, java: bool, label: str) -> dict:
    root = Path(os.path.realpath(source))
    stats = {'files': 0, 'bytes': 0, 'links_skipped': 0}
    destination.mkdir(mode=0o700)
    _copy_directory(source, destination, root, java, label, Path('.'), set(), stats)
    return stats


def _copy_directory(source: Path, destination: Path, root: Path, java: bool, label: str,
                    relative: Path, active: set, stats: dict) -> None:
    identity = _identity(source)
    if identity in active:
        raise ToolchainError(f'Cyclic directory materialization in {label}: {_display(relative)}.')
    active.add(identity)
    try:
        try:
            entries = list(os.scandir(source))
        except OSError:
            raise ToolchainError(f'Prepared {label} tree is not readable: {_display(source)}.') from None
        for entry in entries:
            child = relative / entry.name
            target = destination / entry.name
            _copy_entry(entry, Path(entry.path), target, root, java, label, child, active, stats)
    finally:
        active.discard(identity)


def _copy_entry(entry, source: Path, target: Path, root: Path, java: bool, label: str,
                relative: Path, active: set, stats: dict) -> None:
    if entry.name == '.git':
        raise ToolchainError(f'Refusing to prepare Git metadata from {label}: {_display(relative)}.')
    if workspace._credential_like(relative):
        raise ToolchainError(f'Refusing credential-like {label} entry: {_display(relative)}.')
    try:
        info = entry.stat(follow_symlinks=False)
    except OSError:
        raise ToolchainError(f'Prepared {label} entry is unavailable: {_display(relative)}.') from None
    if stat.S_ISLNK(info.st_mode):
        if java and _java_omitted_link(relative, source):
            stats['links_skipped'] += 1
            return
        final = _resolve_link(source)
        if final is None:
            missing = _missing_target(source)
            if _within(missing, root) or (java and _java_external_allowed(str(missing))):
                stats['links_skipped'] += 1
                return
            raise ToolchainError(f'Refusing dangling {label} symlink with an unapproved target: '
                                 f'{_display(relative)} -> {_display(missing)}.')
        real = Path(os.path.realpath(final))
        if workspace._credential_like(real):
            raise ToolchainError(f'Refusing credential-like {label} symlink target: '
                                 f'{_display(relative)}.')
        try:
            node = os.lstat(real)
        except OSError:
            raise ToolchainError(f'Link target is unavailable: {_display(relative)}.') from None
        inside = _within(real, root)
        if stat.S_ISREG(node.st_mode):
            if not inside and not (java and _java_external_allowed(str(real))):
                raise ToolchainError(f'Refusing escaping {label} symlink: {_display(relative)} '
                                     f'-> {_display(real)}.')
            _copy_file(real, target, stats)
            return
        if stat.S_ISDIR(node.st_mode):
            if not inside:
                raise ToolchainError(f'Refusing escaping {label} symlink directory: {_display(relative)} '
                                     f'-> {_display(real)}.')
            target.mkdir(mode=0o700)
            _copy_directory(real, target, root, java, label, relative, active, stats)
            return
        raise ToolchainError(f'Refusing special file behind {label} symlink: {_display(relative)}.')
    if stat.S_ISDIR(info.st_mode):
        target.mkdir(mode=0o700)
        _copy_directory(source, target, root, java, label, relative, active, stats)
        return
    if stat.S_ISREG(info.st_mode):
        _copy_file(source, target, stats)
        return
    raise ToolchainError(f'Refusing special file in {label}: {_display(relative)}.')


def _java_tool(source: Path) -> bool:
    return os.path.lexists(source / 'bin' / 'java')

def _rounded_allocation(size: int) -> int:
    return max(1, ((int(size) + _ALLOCATION_BLOCK - 1) // _ALLOCATION_BLOCK)) * _ALLOCATION_BLOCK


def _add_requirement(total: dict, extra: dict) -> None:
    total['disk_bytes'] += int(extra['disk_bytes'])
    total['inodes'] += int(extra['inodes'])


def _materialization_requirement(source: Path, *, java: bool = False,
                                 label: str = 'source') -> dict:
    """Validate a copy tree without content reads and bound its apparent allocation.

    Sparse files are charged by apparent size rather than allocated blocks, and
    every directory, entry, materialized symlink target and block-rounded file
    contributes so the result is conservative for the destination copy.
    """
    source = Path(source)
    root = Path(os.path.realpath(source))
    requirement = {'disk_bytes': 0, 'inodes': 0}
    active = set()

    def account_file(info) -> None:
        requirement['disk_bytes'] += _rounded_allocation(info.st_size)
        requirement['inodes'] += 1

    def account_directory(info) -> None:
        requirement['disk_bytes'] += _ALLOCATION_BLOCK
        requirement['inodes'] += 1

    def visit_directory(path: Path, relative: Path) -> None:
        identity = _identity(path)
        if identity in active:
            raise ToolchainError(f'Cyclic directory materialization in {label}: {_display(relative)}.')
        active.add(identity)
        try:
            try:
                info = os.stat(path)
            except OSError:
                raise ToolchainError(f'Prepared {label} tree is not readable: {_display(path)}.') from None
            if not stat.S_ISDIR(info.st_mode):
                raise ToolchainError(f'Prepared {label} entry changed type: {_display(relative)}.')
            account_directory(info)
            try:
                entries = list(os.scandir(path))
            except OSError:
                raise ToolchainError(f'Prepared {label} tree is not readable: {_display(path)}.') from None
            for entry in entries:
                visit_entry(entry, Path(entry.path), relative / entry.name)
        finally:
            active.discard(identity)

    def visit_entry(entry, path: Path, relative: Path) -> None:
        if entry.name == '.git':
            raise ToolchainError(f'Refusing to prepare Git metadata from {label}: {_display(relative)}.')
        if workspace._credential_like(relative):
            raise ToolchainError(f'Refusing credential-like {label} entry: {_display(relative)}.')
        try:
            info = entry.stat(follow_symlinks=False)
        except OSError:
            raise ToolchainError(f'Prepared {label} entry is unavailable: {_display(relative)}.') from None
        if stat.S_ISLNK(info.st_mode):
            if java and _java_omitted_link(relative, path):
                return
            final = _resolve_link(path)
            if final is None:
                missing = _missing_target(path)
                if _within(missing, root) or (java and _java_external_allowed(str(missing))):
                    return
                raise ToolchainError(f'Refusing dangling {label} symlink with an unapproved target: '
                                     f'{_display(relative)} -> {_display(missing)}.')
            real = Path(os.path.realpath(final))
            if workspace._credential_like(real):
                raise ToolchainError(f'Refusing credential-like {label} symlink target: '
                                     f'{_display(relative)}.')
            try:
                node = os.lstat(real)
            except OSError:
                raise ToolchainError(f'Link target is unavailable: {_display(relative)}.') from None
            inside = _within(real, root)
            if stat.S_ISREG(node.st_mode):
                if not inside and not (java and _java_external_allowed(str(real))):
                    raise ToolchainError(f'Refusing escaping {label} symlink: {_display(relative)} '
                                         f'-> {_display(real)}.')
                account_file(node)
                return
            if stat.S_ISDIR(node.st_mode):
                if not inside:
                    raise ToolchainError(f'Refusing escaping {label} symlink directory: '
                                         f'{_display(relative)} -> {_display(real)}.')
                visit_directory(real, relative)
                return
            raise ToolchainError(f'Refusing special file behind {label} symlink: {_display(relative)}.')
        if stat.S_ISDIR(info.st_mode):
            visit_directory(path, relative)
            return
        if stat.S_ISREG(info.st_mode):
            account_file(info)
            return
        raise ToolchainError(f'Refusing special file in {label}: {_display(relative)}.')

    try:
        info = os.lstat(root)
    except OSError:
        raise ToolchainError(f'Prepared {label} source is unavailable: {_display(source)}.') from None
    if stat.S_ISDIR(info.st_mode):
        visit_directory(root, Path('.'))
    elif stat.S_ISREG(info.st_mode):
        if workspace._credential_like(root):
            raise ToolchainError(f'Refusing credential-like {label} source: {_display(source)}.')
        account_file(info)
    else:
        raise ToolchainError(f'Prepared {label} source must be an ordinary file or directory.')
    return requirement


def _destination_requirement(copy, destination: str) -> dict:
    """Charge directories that preparation still has to create for a destination."""
    total = {'disk_bytes': 0, 'inodes': 0}
    current = copy.path
    for part in PurePosixPath(destination).parts[:-1]:
        current = current / part
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            total['disk_bytes'] += _ALLOCATION_BLOCK
            total['inodes'] += 1
            continue
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            break
    return total


@contextmanager
def _reserve_materialization(target, requirement: dict):
    """Admit one known materialization through the shared allocation pool."""
    from . import execution_allocation, storage_budget
    try:
        with execution_allocation.reserve(target, disk_bytes=requirement['disk_bytes'],
                                          inodes=requirement['inodes']):
            yield
    except storage_budget.BudgetError as error:
        raise ToolchainError(str(error), error.code) from None


def _managed_source_owner(copy, source):
    source_real = Path(os.path.realpath(source))
    for candidate in (source_real, *source_real.parents):
        if (candidate.name == 'work' and re.fullmatch('[0-9a-f]{32}', candidate.parent.name)
                and candidate.parent.parent.name == 'workspaces'):
            return candidate.parent.parent.parent, candidate.parent.name
    if copy is not None and source_real.is_relative_to(_state_root(copy) / 'workspaces'):
        raise ToolchainError('Managed workspace sources must stay beneath an owned work tree.')
    return None


@contextmanager
def _borrowed_workspace_source(copy, source):
    """Hold another managed copy's raw owner lock while its source is copied."""
    source_real = Path(os.path.realpath(source))
    owner = _managed_source_owner(copy, source)
    if owner is None:
        yield
        return
    state, workspace_id = owner
    if (copy is not None and copy._locked
            and copy.directory == state / 'workspaces' / workspace_id):
        yield
        return
    from . import workspace as workspace_module, workspace_lifecycle, workspace_gc
    try:
        # Prove this is existing managed control state before fence() may create
        # any control file; ordinary lookalike input paths stay untouched.
        workspace_module.load(state, workspace_id)
        with ExitStack() as locks:
            with workspace_lifecycle.fence(state):
                other = workspace_module.load(state, workspace_id)
                locks.enter_context(workspace_gc._owner(other))
            other = workspace_module.load(state, workspace_id)
            other.verify()
            if not source_real.is_relative_to(Path(os.path.realpath(other.path))):
                raise ToolchainError('Managed workspace source changed while it was locked.')
            yield
    except workspace_module.WorkspaceError as error:
        raise ToolchainError('Cannot borrow the source workspace; provide a durable cache '
                             'snapshot instead: ' + str(error), error.code) from None




def _validate_tools_root(copy) -> Path | None:
    """Return the private tool area only when it is an ordinary owned directory."""
    root = copy.path / TOOLS_DIRNAME
    try:
        info = os.lstat(root)
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise ToolchainError(f'Private tool area is unsafe: {_display(root)}.')
    return root


def _ensure_directory_chain(copy, parts: tuple[str, ...], *, create: bool = True) -> None:
    current = copy.path
    for part in parts:
        current = current / part
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            if not create:
                return
            try:
                os.mkdir(current, 0o755)
            except FileExistsError:
                info = os.lstat(current)
            else:
                continue
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise ToolchainError(f'Preparation destination parent is unsafe: {_display(current)}.')


def _validate_exclude_ownership(value) -> dict[str, int]:
    """Validate the private durable record of exclusion lines prepared by us."""
    if not isinstance(value, dict):
        raise ToolchainError('Prepared toolchain metadata has an invalid exclude ownership record.')
    result = {}
    for pattern, count in value.items():
        if (not isinstance(pattern, str) or not pattern.startswith('/')
                or any(character in pattern for character in '\r\n\0')
                or type(count) is not int or count <= 0):
            raise ToolchainError(
                'Prepared toolchain metadata has an invalid exclude ownership record.')
        result[pattern] = count
    return result


def _existing_patterns(copy) -> tuple[set[str], set[str], set[str], dict[str, int] | None]:
    raw = copy.metadata.get(TOOLCHAIN_KEY)
    if raw is None:
        return set(), set(), set(), None
    metadata = _validate_toolchain_metadata(copy, raw, check_tools=False)
    tools = set(metadata['tools'])
    destinations = {entry['destination'] for entry in metadata['copies']}
    return (tools, destinations, set(metadata.get('cache_dirs', [])),
            metadata['exclude_ownership'])


def _exclude_pattern(relative: str) -> str:
    escaped = ''
    for character in relative:
        if character in '\\*?[]':
            escaped += '\\' + character
        else:
            escaped += character
    if escaped.endswith(' '):
        escaped = escaped[:-1] + '\\ '
    return '/' + escaped


def _exclude_patterns(tools, destinations, cache_dirs) -> list[str]:
    patterns = []
    if tools:
        patterns.append(_exclude_pattern(TOOLS_DIRNAME) + '/')
    for destination in sorted(set(destinations)):
        patterns.append(_exclude_pattern(destination))
    for cache_dir in sorted(set(cache_dirs)):
        patterns.append(_exclude_pattern(cache_dir))
    result = []
    seen = set()
    for pattern in patterns:
        if pattern in seen:
            continue
        seen.add(pattern)
        result.append(pattern)
    return result


def _split_lf_lines(content: bytes) -> list[bytes]:
    """Split only at LF and retain each line's original terminator bytes."""
    if not content:
        return []
    parts = content.split(b'\n')
    lines = [part + b'\n' for part in parts[:-1]]
    if parts[-1]:
        lines.append(parts[-1])
    return lines


def _exclude_line_body(line: bytes) -> bytes:
    return line[:-1] if line.endswith(b'\n') else line


def _write_excludes(copy, patterns: list[str], *, owned) -> tuple[bool, dict[str, int]]:
    """Publish generated excludes and return the exact lines preparation owns.

    ``owned`` is the previous durable ownership record, or ``None`` for an
    unrecorded legacy declaration.  Without a record no existing line is ever
    removed: declarations alone do not prove that preparation inserted it.
    """
    git_dir = copy.path / '.git'
    info = os.lstat(git_dir)
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise ToolchainError('Owned copy Git metadata is unsafe; preparation refused.')
    info_dir = git_dir / 'info'
    exclude = info_dir / 'exclude'
    existing = b''
    existing_mode = 0o644
    info_exists = False
    exclude_exists = False
    try:
        info = os.lstat(info_dir)
    except FileNotFoundError:
        pass
    else:
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise ToolchainError('Owned copy Git exclude directory is unsafe; preparation refused.')
        info_exists = True
        try:
            fd = os.open(exclude, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            pass
        else:
            exclude_exists = True
            with os.fdopen(fd, 'rb') as stream:
                stat_info = os.fstat(stream.fileno())
                if (not stat.S_ISREG(stat_info.st_mode) or stat_info.st_nlink != 1
                        or stat_info.st_uid != os.geteuid()):
                    raise ToolchainError(
                        'Owned copy Git exclude file is unsafe; preparation refused.')
                existing = stream.read()
                existing_mode = stat.S_IMODE(stat_info.st_mode)
    lines = _split_lf_lines(existing)
    desired = list(dict.fromkeys(patterns))
    wanted = set(desired)
    removals = {}
    if owned is not None:
        for pattern, count in owned.items():
            if pattern not in wanted:
                removals[pattern.encode('utf-8') + b'\n'] = count
    remaining = []
    removed = False
    for line in lines:
        budget = removals.get(line, 0)
        if budget:
            removals[line] = budget - 1
            removed = True
            continue
        remaining.append(line)
    present = {_exclude_line_body(line) for line in remaining}
    additions = []
    for pattern in desired:
        encoded = pattern.encode('utf-8')
        if encoded in present:
            continue
        additions.append(pattern)
        present.add(encoded)
    changed = removed or bool(additions)
    if changed:
        # Refuse to publish while the pre-change evidence is stale: ownership of
        # the exclude file is the only Git metadata this path may attest.
        try:
            copy.verify()
        except workspace.WorkspaceError as error:
            raise ToolchainError(str(error), error.code) from None
        if not info_exists:
            try:
                os.mkdir(info_dir, 0o755)
            except FileExistsError:
                current = os.lstat(info_dir)
                if not stat.S_ISDIR(current.st_mode) or stat.S_ISLNK(current.st_mode):
                    raise ToolchainError(
                        'Owned copy Git exclude directory is unsafe; preparation refused.')
        content = b''.join(remaining)
        if additions:
            if content and not content.endswith(b'\n'):
                content += b'\n'
            content += b''.join(pattern.encode('utf-8') + b'\n' for pattern in additions)
        temporary = info_dir / ('exclude.tmp-' + uuid.uuid4().hex)
        publish_mode = existing_mode if exclude_exists else 0o644
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     publish_mode)
        try:
            with os.fdopen(fd, 'wb') as stream:
                os.fchmod(stream.fileno(), publish_mode)
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            _replace(temporary, exclude)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
        from . import git_integrity
        try:
            evidence = git_integrity.snapshot(copy.path)
            legacy = workspace._git_digest(copy.path)
        except (git_integrity.GitIntegrityError, workspace.WorkspaceError) as error:
            raise ToolchainError(str(error), error.code) from None
        copy.metadata['git_integrity'] = evidence
        copy.metadata['git_digest'] = legacy
    occurrences = {}
    for line in remaining:
        if line.endswith(b'\n'):
            body = line[:-1]
            occurrences[body] = occurrences.get(body, 0) + 1
    ownership = {}
    for pattern in desired:
        if pattern in additions:
            count = 1
        else:
            count = min((owned or {}).get(pattern, 0),
                        occurrences.get(pattern.encode('utf-8'), 0))
        if count:
            ownership[pattern] = count
    return changed, ownership


@contextmanager
def _recipe_lock(copy):
    directory = _state_root(copy) / RECIPES_DIRNAME
    _ensure_private_directory(directory)
    key = _recipe_key(copy)
    lock = directory / (key + '.lock')
    fd = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or info.st_uid != os.geteuid() or info.st_mode & 0o077):
            raise ToolchainError('Private toolchain recipe lock is unsafe.')
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield directory, key
    finally:
        os.close(fd)


def _prepared_recipe_source(copy, relative: str, *, require_directory: bool) -> Path:
    parts = PurePosixPath(relative).parts
    current = copy.path
    for index, part in enumerate(parts):
        current = current / part
        final = index == len(parts) - 1
        try:
            info = os.lstat(current)
        except OSError:
            raise ToolchainError('A prepared source disappeared before its recipe snapshot could be '
                                 f'stored: {_display(relative)}.') from None
        if info.st_uid != os.geteuid():
            raise ToolchainError(
                f'A prepared source is not an ordinary owned path: {_display(relative)}.')
        if stat.S_ISLNK(info.st_mode):
            if final:
                raise ToolchainError(
                    f'A prepared source is not an ordinary owned path: {_display(relative)}.')
            raise ToolchainError(
                f'A prepared source has an unsafe ancestor: {_display(relative)}.')
        if not final and not stat.S_ISDIR(info.st_mode):
            raise ToolchainError(
                f'A prepared source has an unsafe ancestor: {_display(relative)}.')
    if require_directory and not stat.S_ISDIR(info.st_mode):
        raise ToolchainError(f'A prepared tool destination is not a directory: {_display(relative)}.')
    if not require_directory and not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
        raise ToolchainError(f'A prepared copy destination is not an ordinary file or directory: '
                             f'{_display(relative)}.')
    return current


def _recipe_snapshot_sources(copy, metadata: dict) -> list[dict]:
    """Describe every saved source by its actual, prepared destination."""
    sources = []
    for name, entry in metadata['tools'].items():
        relative = f'{TOOLS_DIRNAME}/{name}'
        sources.append({
            'kind': 'tool',
            'name': name,
            'path': _prepared_recipe_source(copy, relative, require_directory=True),
            'require_directory': True,
        })
    for index, entry in enumerate(metadata['copies']):
        sources.append({
            'kind': 'copy',
            'index': index,
            'path': _prepared_recipe_source(copy, entry['destination'],
                                            require_directory=False),
            'require_directory': False,
        })
    return sources


def _recipe_source_paths(payload) -> list[str]:
    if not isinstance(payload, dict):
        return []
    values = []
    tools = payload.get('tools')
    if isinstance(tools, dict):
        for entry in tools.values():
            if isinstance(entry, dict) and isinstance(entry.get('source'), str):
                values.append(entry['source'])
    copies = payload.get('copies')
    if isinstance(copies, list):
        for entry in copies:
            if isinstance(entry, dict) and isinstance(entry.get('source'), str):
                values.append(entry['source'])
    return values


def _save_recipe(copy, metadata: dict, *, cache_max_bytes: int, cache_max_inodes: int) -> None:
    from . import toolchain_cache
    sources = _recipe_snapshot_sources(copy, metadata)
    saved_at = time.time()
    if not sources:
        payload = {'version': TOOLCHAIN_VERSION, 'kind': RECIPE_KIND,
                   'source_project': metadata['source_project'], 'saved_at': saved_at,
                   'tools': {}, 'copies': [],
                   'environment': metadata['environment'], 'probes': metadata['probes'],
                   'cache_dirs': list(metadata.get('cache_dirs', []))}
        with _recipe_lock(copy) as (directory, key):
            _atomic_private_write(directory / (key + '.json'),
                                  (json.dumps(payload, indent=2, ensure_ascii=True) + '\n').encode())
        return
    with toolchain_cache.snapshot_batch(
            _state_root(copy), max_bytes=cache_max_bytes,
            max_inodes=cache_max_inodes) as batch:
        with _recipe_lock(copy) as (directory, key):
            previous = _read_recipe(copy)
            if previous is not None:
                batch.protect_paths(_recipe_source_paths(previous))
            cached = [batch.snapshot(item['path'], require_directory=item['require_directory'])
                      for item in sources]
            stable_tools = {}
            stable_copies = list(metadata['copies'])
            for item, source in zip(sources, cached):
                if item['kind'] == 'tool':
                    stable = dict(metadata['tools'][item['name']])
                    stable['source'] = str(source)
                    stable_tools[item['name']] = stable
                else:
                    stable = dict(metadata['copies'][item['index']])
                    stable['source'] = str(source)
                    stable_copies[item['index']] = stable
            payload = {'version': TOOLCHAIN_VERSION, 'kind': RECIPE_KIND,
                       'source_project': metadata['source_project'], 'saved_at': saved_at,
                       'tools': stable_tools, 'copies': stable_copies,
                       'environment': metadata['environment'], 'probes': metadata['probes'],
                       'cache_dirs': list(metadata.get('cache_dirs', []))}
            batch.publish()
            _atomic_private_write(directory / (key + '.json'),
                                  (json.dumps(payload, indent=2, ensure_ascii=True) + '\n').encode())


def _source_missing(value) -> Path | None:
    if not isinstance(value, str) or not value or '\0' in value:
        return None
    path = Path(value)
    if not path.is_absolute() or '.git' in path.parts or workspace._credential_like(path):
        return None
    try:
        path.resolve(strict=True)
    except (FileNotFoundError, NotADirectoryError):
        return path
    except (OSError, RuntimeError):
        return None
    return None


def _missing_recipe_source(declarations: dict) -> Path | None:
    for source in declarations['tools'].values():
        missing = _source_missing(source)
        if missing is not None:
            return missing
    for source, _ in declarations['copies']:
        missing = _source_missing(source)
        if missing is not None:
            return missing
    return None


def _validate_safe_before_invalidation(copy, declarations: dict) -> None:
    """Never invalidate a recipe that is unsafe independently of a missing source."""
    for name, source in declarations['tools'].items():
        if _source_missing(source) is None:
            _validate_host_source(copy, source, what=f'Tool {name}', require_directory=True)
    seen = set()
    for source, destination in declarations['copies']:
        normalized = _validate_destination(copy, destination)
        if normalized in seen:
            raise ToolchainError(f'Duplicate copy destination: {_display(normalized)}.')
        for other in seen:
            if normalized.startswith(other + '/') or other.startswith(normalized + '/'):
                raise ToolchainError(f'Overlapping copy destinations: {_display(other)} and '
                                     f'{_display(normalized)}.')
        seen.add(normalized)
        if _source_missing(source) is None:
            _validate_host_source(copy, source, what='Copy', require_directory=False,
                                  allow_workspace_input=True)


def _invalidate_recipe(copy, expected_payload: dict) -> bool:
    """Remove only the exact stale recipe observed, preserving unsafe replacements."""
    with _recipe_lock(copy) as (directory, key):
        try:
            current = _read_recipe(copy)
        except ToolchainError:
            return False
        if current != expected_payload:
            return False
        path = directory / (key + '.json')
        try:
            os.unlink(path)
        except FileNotFoundError:
            return False
        try:
            fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError:
            pass
        return True


def _read_recipe(copy) -> dict | None:
    path = _recipe_file(copy)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    except OSError:
        raise ToolchainError(f'Private toolchain recipe is unsafe or unreadable: {_display(path)}.') from None
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or info.st_uid != os.geteuid() or info.st_mode & 0o077):
            raise ToolchainError('Private toolchain recipe must be an owner-only ordinary file.')
        if info.st_size > _MAX_RECIPE_BYTES:
            raise ToolchainError('Private toolchain recipe is unreasonably large; refusing to interpret it.')
        raw = stream.read(_MAX_RECIPE_BYTES + 1)
    try:
        payload = json.loads(raw.decode('utf-8'))
    except (UnicodeError, ValueError):
        raise ToolchainError('Private toolchain recipe is malformed.') from None
    return payload


def _validate_recipe(copy, payload) -> dict:
    if not isinstance(payload, dict):
        raise ToolchainError('Private toolchain recipe is malformed.')
    version = payload.get('version')
    if type(version) is not int or version not in TOOLCHAIN_VERSIONS:
        raise ToolchainError(f'Unsupported toolchain recipe version: {_display(version)}. '
                             'This package never guesses an unknown recipe format.')
    expected = {'version', 'kind', 'source_project', 'saved_at', 'tools', 'copies',
                'environment', 'probes'}
    if version == TOOLCHAIN_VERSION:
        expected.add('cache_dirs')
    if set(payload) != expected:
        raise ToolchainError('Private toolchain recipe has unknown fields; refusing to replay it.')
    if payload.get('kind') != RECIPE_KIND:
        raise ToolchainError('Private toolchain recipe has an unexpected kind and is not replayed.')
    if payload.get('source_project') != str(_canonical_source(copy)):
        raise ToolchainError('Private toolchain recipe was saved for a different source project.')
    tools = payload.get('tools')
    if not isinstance(tools, dict):
        raise ToolchainError('Private toolchain recipe has an invalid tool map.')
    checked_tools = {}
    for name, entry in tools.items():
        if not isinstance(name, str) or _TOOL_NAME.fullmatch(name) is None:
            raise ToolchainError('Private toolchain recipe contains an invalid tool name.')
        if (not isinstance(entry, dict)
                or set(entry) != {'source', 'files', 'bytes', 'links_skipped'}
                or not isinstance(entry.get('source'), str)
                or not Path(entry['source']).is_absolute() or '\0' in entry['source']
                or not isinstance(entry.get('files'), int) or isinstance(entry.get('files'), bool)
                or not isinstance(entry.get('bytes'), int) or isinstance(entry.get('bytes'), bool)
                or not isinstance(entry.get('links_skipped'), int)
                or isinstance(entry.get('links_skipped'), bool)):
            raise ToolchainError(f'Private toolchain recipe entry for {_display(name)} is malformed.')
        checked_tools[name] = dict(entry)
    copies = payload.get('copies')
    if not isinstance(copies, list):
        raise ToolchainError('Private toolchain recipe has an invalid copy list.')
    checked_copies = []
    for entry in copies:
        if (not isinstance(entry, dict) or set(entry) != {'source', 'destination'}
                or not isinstance(entry.get('source'), str)
                or not Path(entry['source']).is_absolute() or '\0' in entry['source']):
            raise ToolchainError('Private toolchain recipe has a malformed copy declaration.')
        checked_copies.append(dict(entry))
    if version == TOOLCHAIN_VERSION:
        raw_cache_dirs = payload.get('cache_dirs')
        if not isinstance(raw_cache_dirs, list):
            raise ToolchainError('Private toolchain recipe has an invalid cache directory list.')
        cache_dirs = _normalize_cache_dirs(raw_cache_dirs)
    else:
        cache_dirs = []
    return {'version': version, 'source_project': payload['source_project'],
            'tools': {name: entry['source'] for name, entry in checked_tools.items()},
            'copies': [(entry['source'], entry['destination']) for entry in checked_copies],
            'environment': _validate_declared_environment(payload.get('environment')),
            'probes': _validate_probes(payload.get('probes')),
            'cache_dirs': cache_dirs}


def _materialize(copy, declarations: dict, *, recover: bool) -> dict:
    tools = declarations['tools']
    copies = declarations['copies']
    environment = declarations['environment']
    probes = declarations['probes']
    cache_dirs = _normalize_cache_dirs(declarations.get('cache_dirs'))
    metadata_version = declarations.get('version', TOOLCHAIN_VERSION)
    if type(metadata_version) is not int or metadata_version not in TOOLCHAIN_VERSIONS:
        raise ToolchainError(f'Unsupported toolchain metadata version: '
                             f'{_display(metadata_version)}.')

    from . import toolchain_cache
    tool_sources = {}
    for name, source in tools.items():
        tool_sources[name] = _validate_host_source(copy, source, what=f'Tool {name}',
                                                   require_directory=True)
    normalized_copies = []
    seen_destinations = set()
    for source, destination in copies:
        normalized = _validate_destination(copy, destination)
        if normalized in seen_destinations:
            raise ToolchainError(f'Duplicate copy destination: {_display(normalized)}.')
        for other in seen_destinations:
            if normalized.startswith(other + '/') or other.startswith(normalized + '/'):
                raise ToolchainError(f'Overlapping copy destinations: {_display(other)} and '
                                     f'{_display(normalized)}.')
        seen_destinations.add(normalized)
        normalized_copies.append((source, normalized))
    copy_sources = []
    for source, normalized in normalized_copies:
        resolved = _validate_host_source(copy, source, what='Copy',
                                         require_directory=False,
                                         allow_workspace_input=True)
        copy_sources.append((resolved, normalized))

    (previous_tools, previous_destinations, previous_cache_dirs,
     previous_ownership) = _existing_patterns(copy)
    cache_dirs = _validate_cache_declarations(
        copy, cache_dirs,
        previous_cache_dirs=previous_cache_dirs,
        previous_destinations=previous_destinations,
        copy_sources=copy_sources,
        execution_started=_workspace_execution_started(copy))
    tools_root = copy.path / TOOLS_DIRNAME
    declared_destinations = {destination for _, destination in copy_sources}
    removed_tools = sorted(set(previous_tools) - set(tool_sources))
    removed_destinations = sorted(set(previous_destinations) - declared_destinations)
    tools_root_exists = _validate_tools_root(copy) if (tool_sources or copy_sources
                                                       or removed_tools) else None

    # Plan every removal before writing or deleting anything: unsafe destination
    # parents and tracked source (also below .deepseek-tools/NAME) are rejected
    # while the existing copy and any outside symlink target are still intact,
    # including for stale-artifact cleanup and explicit recovery.
    for _, destination in copy_sources:
        _ensure_directory_chain(copy, PurePosixPath(destination).parts[:-1], create=False)
        final = copy.path / destination
        if os.path.lexists(final) and destination not in previous_destinations and not recover:
            raise ToolchainError(f'Copy destination already exists and is not a recorded '
                                 f'preparation artifact: {_display(destination)}. Inspect the '
                                 'copy and explicitly recover to replace it.')
    for destination in removed_destinations:
        _ensure_directory_chain(copy, PurePosixPath(destination).parts[:-1], create=False)
    if tools_root_exists is not None:
        for name in removed_tools:
            if not os.path.lexists(tools_root / name):
                continue
            if workspace.git(copy.path, 'ls-files', '-z', '--', f'{TOOLS_DIRNAME}/{name}'):
                raise ToolchainError('Refusing to remove tracked source beneath the private '
                                     f'tool area: {TOOLS_DIRNAME}/{_display(name)}.')
        for name in tool_sources:
            if workspace.git(copy.path, 'ls-files', '-z', '--', f'{TOOLS_DIRNAME}/{name}'):
                raise ToolchainError('Refusing to replace tracked source beneath the private '
                                     f'tool area: {TOOLS_DIRNAME}/{_display(name)}.')

    stage_root = None
    staged_map = []
    created_tools_root = False
    cache_state = _state_root(copy)
    allocating = bool(tool_sources or copy_sources)
    with ExitStack() as source_locks:
        locked_sources = set()
        for source in list(tool_sources.values()) + [item[0] for item in copy_sources]:
            if toolchain_cache.is_entry_path(cache_state, source):
                continue
            owner_id = _managed_source_owner(copy, source)
            key = owner_id if owner_id not in (None, copy.id) else os.path.realpath(source)
            if key in locked_sources:
                continue
            locked_sources.add(key)
            source_locks.enter_context(_borrowed_workspace_source(copy, source))
        requirement = {'disk_bytes': 4 * _ALLOCATION_BLOCK, 'inodes': 4}
        if allocating:
            for name, source in tool_sources.items():
                _add_requirement(requirement, _materialization_requirement(
                    source, java=_java_tool(source), label='tool'))
            for source, destination in copy_sources:
                if toolchain_cache.is_entry_path(cache_state, source):
                    _add_requirement(
                        requirement,
                        toolchain_cache.materialization_requirement(cache_state, source))
                else:
                    _add_requirement(requirement, _materialization_requirement(
                        source, java=False, label='copy'))
                _add_requirement(requirement, _destination_requirement(copy, destination))
        admission = (_reserve_materialization(copy.path, requirement)
                     if allocating else nullcontext())
        with admission:
            try:
                if allocating:
                    try:
                        os.mkdir(tools_root, 0o755)
                        created_tools_root = True
                    except FileExistsError:
                        _validate_tools_root(copy)
                    stage_root = tools_root / ('.stage-' + uuid.uuid4().hex)
                    stage_root.mkdir(mode=0o700)
                tool_stats = {}
                if tool_sources:
                    (stage_root / 'tools').mkdir(mode=0o700)
                for name, source in tool_sources.items():
                    destination = stage_root / 'tools' / name
                    if toolchain_cache.is_entry_path(cache_state, source):
                        tool_stats[name] = toolchain_cache.materialize(
                            cache_state, source, destination, java=False, label='tool',
                            reserve=False)
                    else:
                        tool_stats[name] = _copy_tree(source, destination,
                                                      java=_java_tool(source), label='tool')
                    staged_map.append((copy.path / TOOLS_DIRNAME / name, destination))
                if copy_sources:
                    (stage_root / 'copies').mkdir(mode=0o700)
                for index, (source, destination) in enumerate(copy_sources):
                    staged = stage_root / 'copies' / str(index)
                    if toolchain_cache.is_entry_path(cache_state, source):
                        toolchain_cache.materialize(cache_state, source, staged,
                                                    java=False, label='copy', reserve=False)
                    elif source.is_dir():
                        _copy_tree(source, staged, java=False, label='copy')
                    else:
                        _copy_file(source, staged, {'files': 0, 'bytes': 0})
                    staged_map.append((copy.path / destination, staged))

                _validate_environment_paths(
                    copy, environment, staged=staged_map,
                    future=_future_preparation_directories(copy, copy_sources, cache_dirs))

                for name in tool_sources:
                    final = tools_root / name
                    if os.path.lexists(final):
                        _remove(final)
                    _replace(stage_root / 'tools' / name, final)
                for index, (source, destination) in enumerate(copy_sources):
                    final = copy.path / destination
                    _ensure_directory_chain(copy, PurePosixPath(destination).parts[:-1])
                    if os.path.lexists(final):
                        _remove(final)
                    _replace(stage_root / 'copies' / str(index), final)
                # A repeated prepare replaces the declaration set exactly: previously
                # recorded artifacts that are no longer declared never linger unrecorded.
                for name in removed_tools:
                    final = tools_root / name
                    if os.path.lexists(final):
                        _remove(final)
                for destination in removed_destinations:
                    stale = copy.path / destination
                    if not os.path.lexists(stale):
                        continue
                    _remove(stale)
                for relative in cache_dirs:
                    _ensure_directory_chain(copy, PurePosixPath(relative).parts)
            finally:
                if stage_root is not None:
                    try:
                        _remove(stage_root)
                    except OSError:
                        pass
                if created_tools_root and os.path.isdir(tools_root):
                    try:
                        os.rmdir(tools_root)
                    except OSError:
                        pass

    metadata = {'version': metadata_version, 'prepared_at': time.time(),
                'source_project': str(_canonical_source(copy)),
                'tools': {name: {'source': str(tool_sources[name]),
                                 'files': tool_stats[name]['files'],
                                 'bytes': tool_stats[name]['bytes'],
                                 'links_skipped': tool_stats[name]['links_skipped']}
                          for name in sorted(tool_sources)},
                'copies': [{'source': str(source), 'destination': destination}
                           for source, destination in copy_sources],
                'environment': dict(environment), 'probes': list(probes)}
    if metadata_version == TOOLCHAIN_VERSION:
        metadata['cache_dirs'] = list(cache_dirs)
    patterns = _exclude_patterns(tool_sources, declared_destinations, cache_dirs)
    changed, ownership = _write_excludes(copy, patterns, owned=previous_ownership)
    if metadata_version == TOOLCHAIN_VERSION:
        metadata['exclude_ownership'] = ownership
    registered = set(copy.metadata.get('prepared_paths', []))
    registered.difference_update(previous_destinations)
    registered.update(entry['destination'] for entry in metadata['copies'])
    if registered:
        copy.metadata['prepared_paths'] = sorted(registered)
    else:
        copy.metadata.pop('prepared_paths', None)
    return metadata


def _result(copy, metadata: dict, *, recipe_saved: bool) -> dict:
    tools = [{'name': name, 'source': entry['source'],
              'destination': f'{TOOLS_DIRNAME}/{name}', 'files': entry['files'],
              'bytes': entry['bytes'], 'links_skipped': entry['links_skipped']}
             for name, entry in sorted(metadata['tools'].items())]
    return {'id': copy.id, 'tools': tools,
            'copies': [dict(entry) for entry in metadata['copies']],
            'cache_dirs': list(metadata.get('cache_dirs', [])),
            'environment': dict(metadata['environment']), 'probes': list(metadata['probes']),
            'read_only_roots': [str(copy.path / TOOLS_DIRNAME / name)
                                for name in sorted(metadata['tools'])],
            'recipe_saved': recipe_saved}


def _prepare_cache_limits(save_project: bool, cache_max_bytes, cache_max_inodes):
    from . import toolchain_cache
    supplied = cache_max_bytes is not None or cache_max_inodes is not None
    if supplied and not save_project:
        raise ToolchainError('--cache-max-bytes and --cache-max-inodes are valid only with '
                             '--save-project.', 64)
    if not save_project:
        return None, None
    if cache_max_bytes is None:
        cache_max_bytes = toolchain_cache.DEFAULT_MAX_BYTES
    if cache_max_inodes is None:
        cache_max_inodes = toolchain_cache.DEFAULT_MAX_INODES
    return toolchain_cache._validate_snapshot_caps(cache_max_bytes, cache_max_inodes)


def _unattested_recipe_source(copy, declarations: dict) -> str | None:
    from . import toolchain_cache
    state = _state_root(copy)
    for source in declarations['tools'].values():
        if not toolchain_cache.is_entry_path(state, source):
            return source
    for source, _ in declarations['copies']:
        if not toolchain_cache.is_entry_path(state, source):
            return source
    return None


def prepare(copy, *, tools=None, copies=None, environment=None, probes=None,
            cache_dirs=None,
            save_project: bool = False, recover: bool = False,
            recheck_environment: bool = False,
            cache_max_bytes: int | None = None,
            cache_max_inodes: int | None = None) -> dict:
    """Own the copy lock, publish declared preparation and record version-2 metadata."""
    cache_max_bytes, cache_max_inodes = _prepare_cache_limits(
        save_project, cache_max_bytes, cache_max_inodes)
    with copy.lock(recover=recover):
        try:
            declarations = {'tools': _normalize_tools(tools), 'copies': _normalize_copies(copies),
                            'environment': _validate_declared_environment(environment),
                            'probes': _validate_probes(probes),
                            'cache_dirs': _normalize_cache_dirs(cache_dirs)}
        except ToolchainError as error:
            copy.begin('preparation')
            _record_failure(copy, error.code)
            raise
        except (OSError, ValueError, shutil.Error, workspace.WorkspaceError) as error:
            copy.begin('preparation')
            _record_failure(copy, 78)
            raise ToolchainError('Toolchain preparation failed; the owned copy is retained for '
                                 'inspection.') from error
        conditions = None
        try:
            conditions = preparation_guard.declarative_conditions(
                copy.source, state=_state_root(copy), declarations=declarations)
            if conditions.fingerprint is not None and not recheck_environment:
                hit = preparation_guard.lookup(_state_root(copy), conditions)
                if hit is not None:
                    raise preparation_guard.PreparationGuardError(
                        preparation_guard.blocked_message(hit))
        except preparation_guard.PreparationGuardError as error:
            # A blocked attempt performs no materialization and does not turn a
            # fresh copy into another failed one; --recheck-environment still
            # requires the normal recover flag for an already-failed copy.
            raise ToolchainError(str(error), error.code) from None
        copy.begin('preparation')
        try:
            metadata = _materialize(copy, declarations, recover=recover)
            copy.metadata[TOOLCHAIN_KEY] = metadata
            copy.save()
            recipe_saved = False
            if save_project:
                _save_recipe(copy, metadata, cache_max_bytes=cache_max_bytes,
                             cache_max_inodes=cache_max_inodes)
                recipe_saved = True
        except MissingSourceError as error:
            _remember_failure(copy, conditions, error)
            _record_failure(copy, error.code)
            raise
        except ToolchainError as error:
            _record_failure(copy, error.code)
            raise
        except (OSError, ValueError, shutil.Error, workspace.WorkspaceError) as error:
            _record_failure(copy, 78)
            raise ToolchainError('Toolchain preparation failed; the owned copy is retained for '
                                 'inspection.') from error
        try:
            copy.finish('succeeded', exit_code=0)
        except (OSError, workspace.WorkspaceError):
            _record_failure(copy, 78)
            raise
        _clear_failure(copy, conditions)
        return _result(copy, metadata, recipe_saved=recipe_saved)


def _remember_failure(copy, conditions, error: MissingSourceError) -> None:
    if conditions is None or conditions.fingerprint is None:
        return
    try:
        preparation_guard.record_failure(
            _state_root(copy), conditions, 'materialization',
            exit_code=error.code, workspace_id=copy.id)
    except (preparation_guard.PreparationGuardError, OSError):
        pass  # Never mask the original preparation failure.


def _clear_failure(copy, conditions) -> None:
    if conditions is None or conditions.fingerprint is None:
        return
    try:
        preparation_guard.clear(_state_root(copy), conditions)
    except (preparation_guard.PreparationGuardError, OSError):
        pass  # Successful preparation is not failed by optional cleanup.


def _record_failure(copy, code: int) -> None:
    try:
        copy.failed('preparation', code)
    except (workspace.WorkspaceError, OSError):
        pass


def apply_saved_recipe(copy) -> dict:
    """Replay a saved declaration recipe while the caller already holds the copy lock."""
    if not getattr(copy, '_locked', False):
        raise ToolchainError('apply_saved_recipe requires the caller to hold the exclusive copy lock.')
    raw = copy.metadata.get(TOOLCHAIN_KEY)
    if raw is not None:
        metadata = _validate_toolchain_metadata(copy, raw, check_tools=True)
        _validate_environment_paths(copy, metadata['environment'])
        return {'applied': False, 'reason': 'already-prepared', 'toolchain': metadata}
    payload = _read_recipe(copy)
    if payload is None:
        return {'applied': False, 'reason': 'no-recipe', 'toolchain': None}
    declarations = _validate_recipe(copy, payload)
    unattested = _unattested_recipe_source(copy, declarations)
    if unattested is not None:
        raise ToolchainError(
            'This version-1 toolchain recipe references a mutable host source that cannot attest '
            f'the original prepared contents: {_display(unattested)}. The recipe was preserved. '
            'Run workspace prepare again with the applicable --tool NAME=HOST_DIR and/or '
            '--copy SOURCE=REL_DEST declarations and --save-project to snapshot every prepared '
            'source into the immutable cache.')
    missing = _missing_recipe_source(declarations)
    if missing is not None:
        _validate_safe_before_invalidation(copy, declarations)
        removed = _invalidate_recipe(copy, payload)
        detail = ('The stale recipe was removed under its lock; '
                  if removed else
                  'The stale recipe could not be removed safely and was preserved; ')
        raise MissingSourceError(
            f'Toolchain recipe source is missing: {_display(missing)}. {detail}'
            'prepare this copy again with available sources and --save-project to refresh it.', 78)
    try:
        metadata = _materialize(copy, declarations, recover=False)
    except ToolchainError as error:
        raise type(error)(
            f'{error.message} The saved recipe was preserved and the owned copy was retained '
            'for inspection. Re-run workspace prepare with the applicable --tool NAME=HOST_DIR '
            'and/or --copy SOURCE=REL_DEST declarations and --save-project to snapshot fresh '
            'sources into the immutable cache, then retry the replay. Resolve any reported '
            'capacity or security condition first.', error.code) from error
    except (OSError, ValueError, shutil.Error, workspace.WorkspaceError) as error:
        raise ToolchainError('Toolchain recipe replay failed; the owned copy is retained for '
                             'inspection.') from error
    copy.metadata[TOOLCHAIN_KEY] = metadata
    copy.save()
    return {'applied': True, 'reason': 'replayed', 'toolchain': metadata,
            **_result(copy, metadata, recipe_saved=False)}


def environment_for(copy, base: dict[str, str]) -> dict[str, str]:
    """Overlay validated prepared workspace variables onto a sanitized base environment."""
    if not isinstance(base, dict):
        raise ToolchainError('environment_for requires a sanitized base environment mapping.')
    result = dict(base)
    metadata = _load_toolchain(copy)
    if metadata is None or not metadata['environment']:
        return result
    prepared_path = None
    for name, value in metadata['environment'].items():
        expanded = value.replace(_PLACEHOLDER, str(copy.path))
        if name == 'PATH':
            prepared_path = expanded
        else:
            result[name] = expanded
    if prepared_path is not None:
        base_path = result.get('PATH', '')
        result['PATH'] = prepared_path + (':' + base_path if base_path else '')
    return result


def probes_for(copy) -> list[str]:
    metadata = _load_toolchain(copy)
    if metadata is None:
        return []
    return [probe.replace(_PLACEHOLDER, str(copy.path)) for probe in metadata['probes']]


def read_only_roots(copy) -> list[Path]:
    metadata = _load_toolchain(copy)
    if metadata is None:
        return []
    return [copy.path / TOOLS_DIRNAME / name for name in sorted(metadata['tools'])]
