"""Reversible provider setup and private credentials; never change primary auth."""
from dataclasses import dataclass
import functools
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import time
import tomllib

BEGIN = b'# >>> codex-deepseek-team provider >>>'
END = b'# <<< codex-deepseek-team provider <<<'


def _worker():
    """Resolve the worker module lazily; it reads this module for credentials.

    A module-level import would make the dependency circular, so provider
    constants and error types are resolved through this single accessor.
    """
    from . import worker
    return worker


@functools.lru_cache(maxsize=1)
def provider_block():
    """Managed provider block built from the single provider definition."""
    return (b'\n\n' + BEGIN + b'\n[model_providers.deepseek]\n' +
            '\n'.join(f'{key} = {json.dumps(value)}'
                      for key, value in _worker().PROVIDER.items()).encode() +
            b'\n' + END + b'\n')


def __getattr__(name):
    # Former module-level names, resolved lazily so the worker import stays one-way.
    if name == 'worker':
        return _worker()
    if name == 'BLOCK':
        return provider_block()
    raise AttributeError(name)


CODEX_HOOK_COMMAND = 'deepseek-team coordinator-hook'
# Shell tool names differ across Codex surfaces: the hook payload uses Bash for the
# interactive shell, while exec_command/shell_command appear on other tool surfaces.
# Source inspection must reach the parent hook on every surface, so mutations,
# inspection and native dispatches share one matcher.
CODEX_SHELL_TOOLS = ('Bash', 'exec_command', 'shell_command')
CODEX_INSPECTION_TOOLS = ('Read', 'Grep', 'Glob')
CODEX_MUTATION_TOOLS = ('apply_patch', 'Edit', 'Write', 'NotebookEdit')
CODEX_NATIVE_TOOLS = ('spawn_agent', 'send_message', 'send_input', 'followup_task',
                      'assign_agent_task', 'resume_agent', 'Agent', 'Task')
# A qualified name such as functions.exec_command or collaboration.spawn_agent is
# normalized by the hook; the matcher keeps the optional namespace prefix.
CODEX_PRETOOLUSE_MATCHER = '(?:.*[./])?(?:' + '|'.join(
    CODEX_SHELL_TOOLS + CODEX_INSPECTION_TOOLS + CODEX_MUTATION_TOOLS + CODEX_NATIVE_TOOLS) + ')'
CODEX_HOOK_EVENTS = {
    'SessionStart': {'matcher': 'startup|resume|clear|compact', 'context': True},
    'UserPromptSubmit': {'matcher': None, 'context': True},
    'PreToolUse': {'matcher': CODEX_PRETOOLUSE_MATCHER, 'context': False},
    'Stop': {'matcher': None, 'context': False},
}

class ConfigError(Exception):
    pass


CREDENTIAL_VARIABLE = 'DEEPSEEK_TEAM_CREDENTIAL_FILE'
DEFAULT_CREDENTIAL_PATH = '.config/codex-deepseek/api-key'
MAX_CREDENTIAL_BYTES = 4096


@dataclass(frozen=True)
class CredentialLocation:
    """One provider credential file shared by reads, writes and deletion."""

    path: Path
    override: bool

    def describe(self):
        return f'{CREDENTIAL_VARIABLE}={self.path}' if self.override else str(self.path)


def credential_location():
    """Environment override > the private HOME default; never silently fall back.

    ``DEEPSEEK_TEAM_CREDENTIAL_FILE`` names the key file itself and fully replaces
    the HOME-derived location. A relative path or parent traversal is rejected
    instead of being resolved against an unpredictable working directory, and an
    invalid override never falls back to the user default.
    """
    configured = os.environ.get(CREDENTIAL_VARIABLE)
    if not configured:
        return CredentialLocation(Path.home() / DEFAULT_CREDENTIAL_PATH, False)
    path = Path(configured)
    if not path.is_absolute() or '..' in path.parts or not path.name:
        raise ConfigError(
            f'{CREDENTIAL_VARIABLE} must be an absolute path to the credential file; '
            f'the ~/{DEFAULT_CREDENTIAL_PATH} default was not used.')
    return CredentialLocation(path, True)


def credential_search_hint():
    """Searched location(s) and effective HOME; never key material or file content."""
    home = Path.home()
    configured = os.environ.get(CREDENTIAL_VARIABLE)
    if configured:
        return (f'{CREDENTIAL_VARIABLE}={configured} '
                f'(replaces the home default), effective HOME={home}')
    return f'{home / DEFAULT_CREDENTIAL_PATH} (home default), effective HOME={home}'


def credential_source():
    """Describe where the effective credential comes from; never its value."""
    environment = _worker().PROVIDER['env_key']
    if environment in os.environ:
        return f'the {environment} environment variable'
    location = credential_location()
    if location.override:
        return f'{location.path} (selected by {CREDENTIAL_VARIABLE})'
    return str(location.path)


def read_regular(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    except OSError:
        raise ConfigError('Refusing an unsafe or inaccessible configuration file.') from None
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise ConfigError('Expected a regular configuration file.')
    with os.fdopen(fd, 'rb') as source:
        return source.read()


def private_directory(path, label='Credential/backup directory'):
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise ConfigError(f'{label} must be owned by you with mode 700, without symlinks.')


def sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_write(path, content, previous, mode=0o600):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as output:
            temporary = Path(output.name)
            os.fchmod(output.fileno(), mode)
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        if read_regular(path) != previous:
            raise ConfigError('File changed concurrently; no update was applied.')
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def parse_config(raw):
    try:
        return tomllib.loads((raw or b'').decode('utf-8'))
    except (ValueError, UnicodeError):
        raise ConfigError('Codex config.toml is invalid; existing content was preserved.') from None


def write_config(home, raw, updated):
    path = home / 'config.toml'
    mode = stat.S_IMODE(path.stat().st_mode) if raw is not None else 0o600
    if raw is not None:
        backups = home / 'codex-deepseek-team-backups'
        private_directory(backups)
        backup = backups / f'config-{time.time_ns()}.toml'
        atomic_write(backup, raw, None)
    atomic_write(path, updated, raw, mode)


def configure(home):
    home = Path(home)
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    raw = read_regular(home / 'config.toml')
    parsed = parse_config(raw)
    providers = parsed.get('model_providers', {})
    if not isinstance(providers, dict):
        raise ConfigError('Invalid model_providers section.')
    if 'deepseek' in providers:
        try:
            _worker().provider_config(home)
        except _worker().WorkerError:
            raise ConfigError('Existing DeepSeek provider conflicts with this package; no settings changed.') from None
        return False
    if raw and (BEGIN in raw or END in raw):
        raise ConfigError('Managed provider markers are inconsistent; no settings changed.')
    updated = (raw or b'') + provider_block()
    parse_config(updated)
    write_config(home, raw, updated)
    return True


def remove_provider(home):
    home = Path(home)
    raw = read_regular(home / 'config.toml')
    if raw is None or (BEGIN not in raw and END not in raw):
        return False
    if raw.count(BEGIN) != 1 or raw.count(END) != 1 or provider_block() not in raw:
        raise ConfigError('Managed provider was edited; refusing to remove user changes.')
    try:
        _worker().provider_config(home)
    except _worker().WorkerError:
        raise ConfigError('Provider was edited; no settings removed.') from None
    updated = raw.replace(provider_block(), b'', 1)
    parse_config(updated)
    write_config(home, raw, updated)
    return True


def _hook_group(spec):
    handler = {
        'type': 'command',
        'command': CODEX_HOOK_COMMAND,
        'timeout': 10,
        'statusMessage': 'DeepSeek Team coordination policy',
    }
    if spec.get('context'):
        handler['additionalContextLimit'] = 2400
    group = {'hooks': [handler]}
    if spec.get('matcher') is not None:
        group['matcher'] = spec['matcher']
    return group


def _owned_codex_hook(handler):
    return isinstance(handler, dict) and handler.get('command') == CODEX_HOOK_COMMAND


def _read_codex_hooks(home):
    """Read hooks.json for ownership inspection without repairing or writing it."""
    raw = read_regular(Path(home) / 'hooks.json')
    if raw is None:
        return None
    try:
        data = json.loads(raw.decode('utf-8'))
    except (ValueError, UnicodeError):
        raise ConfigError('Codex hooks.json is invalid; existing content was preserved.') from None
    if not isinstance(data, dict):
        raise ConfigError('Codex hooks.json must contain an object.')
    hooks = data.get('hooks')
    if hooks is None:
        hooks = {}
    if not isinstance(hooks, dict):
        raise ConfigError('Codex hooks.json hooks must be an object.')
    for groups in hooks.values():
        if not isinstance(groups, list):
            raise ConfigError('Codex hooks.json event entries must be arrays.')
        for group in groups:
            if not isinstance(group, dict):
                raise ConfigError('Codex hooks.json hook groups must be objects.')
            handlers = group.get('hooks')
            if not isinstance(handlers, list):
                raise ConfigError('Codex hooks.json hook handlers must be arrays.')
    return data


def codex_hooks_owned(home):
    """Whether hooks.json contains any package-owned record, stale or current.

    Ownership is deliberately independent of the current matcher/handler shape so
    refresh can migrate older definitions. Missing or foreign-only files are not
    owned; malformed and unsafe files raise ``ConfigError`` without modification.
    """
    data = _read_codex_hooks(home)
    if data is None:
        return False
    hooks = data.get('hooks') or {}
    return any(_owned_codex_hook(handler)
               for groups in hooks.values() for group in groups
               for handler in group.get('hooks', []))


def _strip_codex_hooks(data):
    hooks = data.get('hooks', {})
    if hooks is None:
        hooks = {}
    if not isinstance(hooks, dict):
        raise ConfigError('Codex hooks.json hooks must be an object.')
    changed = False
    for event in list(hooks):
        groups = hooks[event]
        if not isinstance(groups, list):
            raise ConfigError('Codex hooks.json event entries must be arrays.')
        kept = []
        for group in groups:
            if not isinstance(group, dict):
                kept.append(group)
                continue
            handlers = group.get('hooks')
            if not isinstance(handlers, list):
                kept.append(group)
                continue
            filtered = [handler for handler in handlers if not _owned_codex_hook(handler)]
            if len(filtered) != len(handlers):
                changed = True
            if filtered:
                copy = dict(group)
                copy['hooks'] = filtered
                kept.append(copy)
        if kept:
            hooks[event] = kept
        else:
            hooks.pop(event, None)
    if hooks:
        data['hooks'] = hooks
    else:
        data.pop('hooks', None)
    return changed


def install_codex_hooks(home):
    """Install one stable user-level hook definition; projects opt in via init."""
    home = Path(home)
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = home / 'hooks.json'
    raw = read_regular(path)
    try:
        data = json.loads((raw or b'{}').decode('utf-8'))
    except (ValueError, UnicodeError):
        raise ConfigError('Codex hooks.json is invalid; existing content was preserved.') from None
    if not isinstance(data, dict):
        raise ConfigError('Codex hooks.json must contain an object.')
    _strip_codex_hooks(data)
    hooks = data.setdefault('hooks', {})
    for event, spec in CODEX_HOOK_EVENTS.items():
        hooks.setdefault(event, []).append(_hook_group(spec))
    updated = (json.dumps(data, indent=2, sort_keys=True) + '\n').encode()
    if raw == updated:
        return False
    mode = stat.S_IMODE(path.stat().st_mode) if raw is not None else 0o600
    atomic_write(path, updated, raw, mode)
    return True


def remove_codex_hooks(home):
    home = Path(home)
    path = home / 'hooks.json'
    raw = read_regular(path)
    if raw is None:
        return False
    try:
        data = json.loads(raw.decode('utf-8'))
    except (ValueError, UnicodeError):
        raise ConfigError('Codex hooks.json is invalid; no hooks were removed.') from None
    if not isinstance(data, dict):
        raise ConfigError('Codex hooks.json must contain an object.')
    changed = _strip_codex_hooks(data)
    if not changed:
        return False
    if data:
        updated = (json.dumps(data, indent=2, sort_keys=True) + '\n').encode()
        atomic_write(path, updated, raw, stat.S_IMODE(path.stat().st_mode))
    else:
        if read_regular(path) != raw:
            raise ConfigError('Codex hooks.json changed concurrently; retry.')
        path.unlink()
        sync_directory(home)
    return True


def codex_hooks_status(home):
    """Check the installed event/matcher/handler definitions, not just their count.

    A managed entry installed by an older release can carry a stale matcher that no
    longer delivers the events this version depends on. Reporting it as not installed
    lets onboarding and doctor repair it by reinstalling, while unrelated user hooks in
    shared groups are neither claimed nor modified.
    """
    raw = read_regular(Path(home) / 'hooks.json')
    if raw is None:
        return False
    try:
        data = json.loads(raw.decode('utf-8'))
    except (ValueError, UnicodeError):
        return False
    if not isinstance(data, dict):
        return False
    hooks = data.get('hooks', {})
    if not isinstance(hooks, dict):
        return False
    for event, spec in CODEX_HOOK_EVENTS.items():
        groups = hooks.get(event, [])
        if not isinstance(groups, list):
            return False
        matches = []
        for group in groups:
            handlers = group.get('hooks') if isinstance(group, dict) else None
            if not isinstance(handlers, list):
                continue
            for handler in handlers:
                if _owned_codex_hook(handler):
                    matches.append(dict(group, hooks=[handler]))
        if matches != [_hook_group(spec)]:
            return False
    return True


def _credential_failure(location, reason):
    """Describe an unsafe or invalid credential by path and reason, never content."""
    if not location.override:
        return ConfigError('Saved DeepSeek credential is invalid or not private; review '
                           '~/.config/codex-deepseek/api-key (directory 700, file 600, current user owner).')
    return ConfigError(
        f'Saved DeepSeek credential is invalid or not private ({reason}): '
        f'{location.describe()}. The override replaces the home default, so '
        f'~/{DEFAULT_CREDENTIAL_PATH} was not used; require an owned 700 directory '
        'and a 600 regular file for the current user.')


def _open_credential(location):
    """Open the credential without following links; ``None`` when it is absent.

    Ownership, file type and group/other permission bits are validated on the
    opened descriptors, so symlinks, foreign-owned entries and shared permissions
    fail closed instead of being read or written through.
    """
    path = location.path
    try:
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    except OSError:
        raise _credential_failure(location, 'unsafe or inaccessible directory') from None
    try:
        info = os.fstat(directory)
        if info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise _credential_failure(location, 'the directory must be owned by you with mode 700')
        try:
            descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                                 dir_fd=directory)
        except FileNotFoundError:
            return None
        except OSError:
            raise _credential_failure(location, 'unsafe or inaccessible file') from None
    finally:
        os.close(directory)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise _credential_failure(location, 'the file must be a regular file owned by you with mode 600')
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def read_credential(location=None):
    """Read one validated credential; ``None`` means the safe location is empty.

    A missing directory or file is reported as absent only after the directory was
    opened without following links. Error messages never include stored content.
    """
    location = location or credential_location()
    descriptor = _open_credential(location)
    if descriptor is None:
        return None
    with os.fdopen(descriptor, 'rb') as source:
        raw = source.read(MAX_CREDENTIAL_BYTES + 1)
    if len(raw) > MAX_CREDENTIAL_BYTES:
        raise _credential_failure(location, 'the file is larger than 4096 bytes')
    try:
        value = raw.decode('ascii').rstrip('\r\n')
    except UnicodeDecodeError:
        raise _credential_failure(location, 'the file is not single ASCII text') from None
    if not re.fullmatch(r'[!-~]+', value):
        raise _credential_failure(location, 'the file is not a single nonempty value without whitespace')
    return value


def save_key(value):
    value = value.rstrip('\r\n')
    if len(value) > MAX_CREDENTIAL_BYTES or not re.fullmatch(r'[!-~]+', value):
        raise ConfigError('Key must be a single nonempty ASCII value without whitespace, up to 4096 characters.')
    location = credential_location()
    label = (f'{CREDENTIAL_VARIABLE} credential directory {location.path.parent}'
             if location.override else 'Credential/backup directory')
    private_directory(location.path.parent, label)
    previous = read_regular(location.path)
    atomic_write(location.path, value.encode(), previous)


def delete_key():
    location = credential_location()
    directory = location.path.parent
    if not directory.exists():
        return False
    label = (f'{CREDENTIAL_VARIABLE} credential directory {directory}'
             if location.override else 'Credential/backup directory')
    private_directory(directory, label)
    if location.override:
        # The override is validated exactly like a read; the HOME default keeps its
        # existing removal behavior.
        descriptor = _open_credential(location)
        if descriptor is None:
            return False
        os.close(descriptor)
    elif read_regular(location.path) is None:
        return False
    location.path.unlink()
    sync_directory(directory)
    return True
