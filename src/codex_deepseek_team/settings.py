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
EFFORT = ('auto', 'low', 'medium', 'high')
DEFAULTS = {'delegation_level': 25, 'access': 'auto', 'effort': 'auto'}


class SettingsError(Exception):
    """Invalid or unsafe settings; never silently escalate permissions."""


@dataclass(frozen=True)
class Policy:
    delegation_level: int
    access: str
    sources: Mapping[str, str]
    effort: str = 'auto'
    enabled: bool = True
    enabled_source: str = 'default'

    @property
    def effective_access(self) -> str:
        if self.access != 'auto':
            return self.access
        return 'read-only' if self.delegation_level == 25 else 'full-access'

    def as_dict(self) -> dict:
        return {
            'enabled': self.enabled,
            'enabled_source': self.enabled_source,
            'delegation_level': self.delegation_level,
            'access': self.access,
            'effective_access': self.effective_access,
            'effort': self.effort,
            'effort_mode': 'frontier-auto' if self.effort == 'auto' else 'forced',
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
        raise SettingsError('Only delegation_level, access and effort are allowed in delegation settings.')
    if 'delegation_level' in values:
        level = values['delegation_level']
        if type(level) is not int or level not in LEVELS:
            raise SettingsError('delegation_level must be the integer 25, 50 or 75.')
    if 'access' in values and values['access'] not in ACCESS:
        raise SettingsError('access must be auto, read-only or full-access.')
    if 'effort' in values and values['effort'] not in EFFORT:
        raise SettingsError('effort must be auto, low, medium or high.')


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
            access: str | None = None, effort: str | None = None,
            global_path: Path | None = None) -> Policy:
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
    overrides = {k: v for k, v in {
        'delegation_level': delegation_level, 'access': access, 'effort': effort,
    }.items() if v is not None}
    _validate(overrides)
    for key, value in overrides.items():
        values[key], sources[key] = value, 'cli'
    from . import activation
    active = activation.resolve(project)
    return Policy(values['delegation_level'], values['access'], MappingProxyType(sources),
                  values['effort'], active.enabled, active.source)


def set_values(path: Path, *, delegation_level: int | None = None,
               access: str | None = None, effort: str | None = None) -> bool:
    """Atomic partial update: changing a level never implicitly resets access."""
    changes = {k: v for k, v in {
        'delegation_level': delegation_level, 'access': access, 'effort': effort,
    }.items() if v is not None}
    _validate(changes)
    if not changes:
        raise SettingsError('Specify --delegation-level, --access and/or --effort.')
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
        f'enabled: {str(policy.enabled).lower()} (source={policy.enabled_source})',
        f'delegation_level: {policy.delegation_level}% (target; source={policy.sources["delegation_level"]})',
        f'access: {policy.access} (source={policy.sources["access"]})',
        f'effective_access: {policy.effective_access} (source={data["effective_access_source"]})',
        f'effort: {policy.effort} (source={policy.sources.get("effort", "default")}; '
        + ('frontier chooses low/medium/high per assignment' if policy.effort == 'auto' else 'forced for new DeepSeek jobs') + ')',
        'max_workers: 3; total_timeout: unlimited by default',
    ))


def instructions(policy: Policy, runtime: str = 'codex') -> str:
    if runtime not in ('codex', 'claude'):
        raise SettingsError('Instruction runtime must be codex or claude.')

    if not policy.enabled:
        from .activation import DISABLED_GUIDANCE
        return DISABLED_GUIDANCE + '\n'

    if policy.effort == 'auto':
        effort_guidance = (
            'DeepSeek Team workers always use deepseek-flash. Effort policy is auto, so before each '
            'DeepSeek assignment the frontier coordinator must choose --effort low, --effort medium '
            'or --effort high from the assigned task without asking the user: low for bounded/mechanical '
            'work, medium for the normal case, and high for difficult debugging, cross-file reasoning '
            'or adversarial review. If a DeepSeek worker is launched directly without a frontier-selected '
            'effort, the runner uses medium as an execution fallback only.\n'
        )
        effort_example = 'medium'
        effort_note = (
            'Because effort policy is auto, replace medium with low or high when the assigned task '
            'warrants it. '
        )
    else:
        effort_guidance = (
            f'DeepSeek Team workers always use deepseek-flash. Effort is persistently forced to '
            f'{policy.effort} by policy (source={policy.sources.get("effort", "default")}); every new '
            f'DeepSeek assignment must use --effort {policy.effort}. The frontier coordinator must '
            'not auto-select another level unless the user supplies an explicit one-job CLI override.\n'
        )
        effort_example = policy.effort
        effort_note = 'The saved effort policy is forced for new jobs. '

    common = (
        'Percentages are target profiles of useful work, not call/token/line quotas. '
        'Do not manufacture tasks to reach a percentage. For a genuinely small single-output '
        'task, record the small classification and concrete scope instead of creating a fake worker. '
        'The coordinator owns architecture, security decisions, final verification, integration, '
        'secrets/signing, commit/push and production actions. These responsibilities do not by '
        'themselves reserve ordinary implementation, tests, fixtures, documentation or non-secret '
        'metadata from workers. Do not calculate an actual useful-work percentage from calls, '
        'deliverable counts, lines or files.\n'
    ) + effort_guidance + (
        'Coordinator-native subagents remain available. Use them only when parallelism, isolated '
        'context or a native capability materially helps. Represent that choice in the plan with '
        'executor: "native-agent" plus a concrete delegation_reason. Native agents complement '
        'DeepSeek workers and do not satisfy DeepSeek worker assignments required by the 50/75 '
        'profiles. Protected coordinator responsibilities remain with the coordinator. DeepSeek '
        'workers themselves remain leaf workers and must never delegate.\n'
    )

    full_profiles = {
        25: 'Delegate bounded research, diagnosis and independent review. The coordinator performs the main implementation.',
        50: 'Delegate at least one separable implementation/test/docs slice when such work exists; coordinator defines architecture/interfaces and integrates.',
        75: 'Delegate most separable implementation, tests, fixtures, documentation, non-secret metadata and independent review before doing that same work yourself. Use up to three workers only for genuinely independent assignments.',
    }
    read_only_profiles = {
        25: 'Delegate bounded research, diagnosis and independent review. The coordinator performs the main implementation.',
        50: 'Delegate substantial investigation, design validation, test planning and independent review before the coordinator implements the corresponding changes.',
        75: 'Delegate most separable analysis, diagnostics, design validation, test planning and independent review. Use up to three read-only workers only for genuinely independent assignments.',
    }

    if policy.effective_access == 'full-access':
        access = (
            'Full-access is development inside an owned isolated copy, not host access. '
            'Allow the worker to create/edit/delete project files in its assigned copy and run '
            'declared local checks. Prepare missing dependencies with workspace prepare. If selected '
            'uncommitted source is required, import only those files with workspace import; it is '
            'recorded as coordinator-prepared source, not worker output. Host SDK/JDK/tools are not '
            'assumed to exist inside the sandbox.\n'
        )
    else:
        access = (
            'Actual access is read-only, regardless of the target level. Assign analysis, diagnostics '
            'and review only; project writes and mutating tests/builds remain coordinator work. '
            'Do not expand access merely to satisfy the target profile.\n'
        )

    guidance = (
        full_profiles if policy.effective_access == 'full-access' else read_only_profiles
    )[policy.delegation_level]

    if runtime == 'codex':
        process = (
            'Codex process integration: after project init and native hook trust, SessionStart/'
            'UserPromptSubmit provide the current coordination task id. For every substantial task, '
            'before coordinator source edits, submit a concrete JSON distribution with '
            'deepseek-team coordination plan --task TASK_ID; include deliverable id/kind/scope, '
            'executor, acceptance criteria, dependencies and checks. Run each worker assignment with '
            f'deepseek-team worker --runtime codex --effort {effort_example} '
            '--coord-task TASK_ID --coord-assignment ASSIGNMENT_ID. '
            + effort_note +
            'The runner records start/result/workspace/checks automatically. After reviewing a result, '
            'record its use with deepseek-team coordination use. New substantial scope requires '
            'a revised plan. Codex PreToolUse technically blocks source mutation while the distribution '
            'is missing/noncompliant, blocks unplanned scope, and blocks duplicate work owned by a '
            'pending worker assignment; Stop prevents silent completion with pending/undispositioned '
            'worker results. Native hook trust is controlled by Codex and is not inferred by this package.\n'
        )
    else:
        process = (
            'Claude coordinator integration is instruction-driven in this release: use the same '
            'distribution principles, resolved effort policy and managed worker runner, but DeepSeek '
            'Team does not claim a Claude PreToolUse technical gate. Worker filesystem/network '
            'permissions remain technically sandboxed; coordinator compliance with distribution '
            'instructions depends on Claude Code.\n'
        )

    return (
        f'### Effective delegation profile: {policy.delegation_level}% / {policy.effective_access}; '
        f'effort={policy.effort}\n'
        + common + guidance + '\n' + access + process
        + 'While a worker runs, work only on independent scope. Review the actual diff and recorded '
          'checks without repeating the whole investigation or rewriting correct code. DeepSeek workers never '
          'stage, commit, push, publish, deploy, access production services or delegate.\n'
    )
