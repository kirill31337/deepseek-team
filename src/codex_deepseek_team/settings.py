"""Credential-free delegation policy; one resolver for every public entry point."""
from __future__ import annotations

from dataclasses import dataclass
import fcntl
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import tomllib
from types import MappingProxyType
from typing import Mapping

PROJECT_FILE = '.deepseek-team.toml'
LEVELS = (25, 50, 75)
ACCESS = ('auto', 'read-only', 'full-access')
DEFAULTS = {'delegation_level': 25, 'access': 'auto'}


class SettingsError(Exception):
    """Invalid or unsafe settings; never silently escalate permissions."""


@dataclass(frozen=True)
class Policy:
    delegation_level: int
    access: str
    sources: Mapping[str, str]

    @property
    def effective_access(self) -> str:
        if self.access != 'auto':
            return self.access
        return 'read-only' if self.delegation_level == 25 else 'full-access'

    def as_dict(self) -> dict:
        return {
            'delegation_level': self.delegation_level,
            'access': self.access,
            'effective_access': self.effective_access,
            'sources': dict(self.sources),
            'effective_access_source': (
                f'profile:{self.delegation_level} (access=auto)' if self.access == 'auto'
                else self.sources['access']),
            'max_workers': 3,
            'percentage_is_target_not_measurement': True,
        }


def global_file() -> Path:
    root = Path(os.environ.get('XDG_CONFIG_HOME') or Path.home() / '.config')
    if not root.is_absolute():
        raise SettingsError('XDG_CONFIG_HOME must be absolute.')
    return root / 'deepseek-team' / 'config.toml'


def git_environment() -> dict[str, str]:
    env = {k: os.environ[k] for k in ('PATH', 'HOME', 'LANG', 'LC_ALL') if k in os.environ}
    env.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull,
               GIT_OPTIONAL_LOCKS='0', GIT_TERMINAL_PROMPT='0', GIT_NO_REPLACE_OBJECTS='1')
    return env


def project_root(path: Path | None = None, *, required: bool = False) -> Path | None:
    path = Path.cwd() if path is None else Path(path)
    try:
        result = subprocess.run(['git', '-C', str(path), 'rev-parse', '--show-toplevel'],
                                env=git_environment(), capture_output=True, timeout=10,
                                check=False)
        if result.returncode == 0 and result.stdout.strip():
            return Path(os.fsdecode(result.stdout).strip()).resolve()
    except (OSError, subprocess.SubprocessError):
        pass
    if required:
        raise SettingsError('Project settings require an existing Git working copy.')
    return None


def _validate(values: dict) -> None:
    if set(values) - set(DEFAULTS):
        raise SettingsError('Only delegation_level and access are allowed in delegation settings.')
    if 'delegation_level' in values:
        level = values['delegation_level']
        if type(level) is not int or level not in LEVELS:
            raise SettingsError('delegation_level must be the integer 25, 50 or 75.')
    if 'access' in values and values['access'] not in ACCESS:
        raise SettingsError('access must be auto, read-only or full-access.')


def _read(path: Path) -> bytes | None:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    except OSError:
        raise SettingsError(f'Cannot safely read settings: {path}. Symlinks are refused.') from None
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise SettingsError(f'Settings must be an ordinary non-hardlinked file: {path}.')
        raw = stream.read(65537)
    if len(raw) > 65536:
        raise SettingsError(f'Settings file is too large: {path}.')
    return raw


def read_values(path: Path) -> dict:
    raw = _read(Path(path))
    try:
        values = tomllib.loads((raw or b'').decode('utf-8'))
    except (ValueError, UnicodeError):
        raise SettingsError(f'Invalid delegation TOML: {path}; file preserved.') from None
    _validate(values)
    return values


def resolve(root: Path | None = None, *, delegation_level: int | None = None,
            access: str | None = None, global_path: Path | None = None) -> Policy:
    """Resolve both fields independently and freeze a new-job policy snapshot."""
    values = dict(DEFAULTS)
    sources = {name: 'default' for name in DEFAULTS}
    global_path = global_file() if global_path is None else Path(global_path)
    project = project_root(root)
    layers = [('global', global_path)]
    if project is not None:
        layers.append(('project', project / PROJECT_FILE))
    for label, path in layers:
        for key, value in read_values(path).items():
            values[key], sources[key] = value, f'{label}:{path}'
    overrides = {k: v for k, v in {'delegation_level': delegation_level, 'access': access}.items()
                 if v is not None}
    _validate(overrides)
    for key, value in overrides.items():
        values[key], sources[key] = value, 'cli'
    return Policy(values['delegation_level'], values['access'], MappingProxyType(sources))


def set_values(path: Path, *, delegation_level: int | None = None,
               access: str | None = None) -> bool:
    """Atomic partial update: changing a level never implicitly resets access."""
    changes = {k: v for k, v in {'delegation_level': delegation_level, 'access': access}.items()
               if v is not None}
    _validate(changes)
    if not changes:
        raise SettingsError('Specify --delegation-level and/or --access.')
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    parent = path.parent.lstat()
    if not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.geteuid():
        raise SettingsError('Settings directory must be an owned ordinary directory.')
    lock = os.open(path.with_name(path.name + '.lock'),
                   os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    temporary = None
    try:
        info = os.fstat(lock)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or
                info.st_uid != os.geteuid() or info.st_mode & 0o077):
            raise SettingsError('Unsafe settings lock; no settings changed.')
        fcntl.flock(lock, fcntl.LOCK_EX)
        previous = _read(path)
        values = read_values(path)
        if all(values.get(k) == v for k, v in changes.items()):
            return False
        values.update(changes)
        raw = ''.join(f'{key} = {json.dumps(values[key])}\n'
                      for key in DEFAULTS if key in values).encode()
        mode = stat.S_IMODE(path.stat().st_mode) if previous is not None else 0o600
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            os.fchmod(stream.fileno(), mode)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        if _read(path) != previous:
            raise SettingsError('Settings changed concurrently; retry the update.')
        os.replace(temporary, path)
        fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        return True
    finally:
        os.close(lock)
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def describe(policy: Policy) -> str:
    data = policy.as_dict()
    return '\n'.join((
        f'delegation_level: {policy.delegation_level}% (target; source={policy.sources["delegation_level"]})',
        f'access: {policy.access} (source={policy.sources["access"]})',
        f'effective_access: {policy.effective_access} (source={data["effective_access_source"]})',
        'max_workers: 3; total_timeout: unlimited by default',
    ))


def instructions(policy: Policy, runtime: str = 'codex') -> str:
    if runtime not in ('codex', 'claude'):
        raise SettingsError('Instruction runtime must be codex or claude.')
    common = (
        'Percentages are target profiles of useful work, not call/token/line quotas. '
        'Do not manufacture tasks to reach a percentage. For tiny or inseparable tasks, '
        'delegate less and briefly explain why. The coordinator owns architecture, '
        'security decisions, final verification and integration.\n'
    )
    full_profiles = {
        25: 'Delegate bounded research, diagnosis and independent review. The coordinator performs the main implementation.',
        50: 'Delegate independent implementation slices and their tests before implementing the same work yourself. Define architecture, interfaces and acceptance criteria first.',
        75: 'Delegate most separable implementation, tests, documentation and independent review before doing that same work yourself. Use up to three workers only for genuinely independent assignments.',
    }
    read_only_profiles = {
        25: 'Delegate bounded research, diagnosis and independent review. The coordinator performs the main implementation.',
        50: 'Delegate substantial investigation, design validation, test planning and independent review before the coordinator implements the corresponding changes.',
        75: 'Delegate most separable analysis, diagnostics, design validation, test planning and independent review. Use up to three read-only workers only for genuinely independent assignments.',
    }
    if policy.effective_access == 'full-access':
        access = (
            'Full-access is development inside an owned isolated copy, not host access. '
            'Prepare a copy with `deepseek-team workspace create` and dependencies with '
            '`deepseek-team workspace prepare ID -- COMMAND ...` yourself; never ask the '
            'user to perform this routine preparation. Alternatively worker auto-creates '
            'a copy from committed HEAD. Uncommitted source work is NOT copied or cleaned. '
            'Assign sufficient context, a goal and acceptance criteria. Allow the worker '
            'to create/edit/delete any project files and run local tests/builds. Reuse '
            '`--workspace ID` for iterations; inspect failed copies before explicitly '
            'using `--resume-after-failure`. Never automatically redo a failed implementation.\n'
        )
    else:
        access = (
            'Actual access is read-only, regardless of the target level. Assign analysis, '
            'diagnostics and review only; no project writes or mutating tests/builds. '
            'The coordinator performs implementation. Do not override an explicit '
            'read-only setting merely to meet the target percentage.\n'
        )
    guidance = (full_profiles if policy.effective_access == 'full-access'
                else read_only_profiles)[policy.delegation_level]
    return (f'### Effective delegation profile: {policy.delegation_level}% / {policy.effective_access}\n'
            + common + guidance + '\n' + access
            + 'While a worker runs, work on independent tasks. Wait for its completed result; '
            'silence alone is not failure. Review the actual diff and evidence without '
            'repeating the whole investigation or rewriting correct code. Workers do not '
            'stage, commit, push, publish, deploy, access production services or delegate.\n'
            + f'Run: `deepseek-team worker --runtime {runtime}`; retain the required OS sandbox.\n')
