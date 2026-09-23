"""Credential-free delegation policy; one resolver for every public entry point."""
from __future__ import annotations

from contextlib import contextmanager
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
from .effort import EFFORT_POLICY_CHOICES, EFFORT_POLICY_LEVELS, normalize_policy_effort
from .worker_slots import DEFAULT_MAX_WORKERS, MAX_MAX_WORKERS

PROJECT_FILE = '.deepseek-team.toml'
LEVELS = ('auto', 25, 50, 75)
ACCESS = ('auto', 'read-only', 'full-access')
# Effective saved levels; canonical concrete levels plus auto.
EFFORT = EFFORT_POLICY_LEVELS
# CLI/config surface also accepts the legacy 'medium' alias for 'high'.
EFFORT_CHOICES = EFFORT_POLICY_CHOICES
DEFAULTS = {'delegation_level': 'auto', 'access': 'auto', 'effort': 'auto',
            'max_workers': DEFAULT_MAX_WORKERS}


class SettingsError(Exception):
    """Invalid or unsafe settings; never silently escalate permissions."""


class SettingsPublishedError(SettingsError):
    """Settings were published, but their directory durability is unconfirmed."""


def parse_level(value):
    """Argparse converter: literal lowercase 'auto', or exactly 25/50/75.

    Rejects bools, floats, padded strings and any other spelling so a typo can
    never silently change the delegation profile or widen access.
    """
    if isinstance(value, str) and value == 'auto':
        return 'auto'
    if isinstance(value, str) and value in ('25', '50', '75'):
        return int(value)
    raise ValueError("delegation-level must be 'auto', 25, 50 or 75.")


@dataclass(frozen=True)
class Policy:
    delegation_level: str | int
    access: str
    sources: Mapping[str, str]
    effort: str = 'auto'
    enabled: bool = True
    enabled_source: str = 'default'
    max_workers: int = DEFAULT_MAX_WORKERS

    @property
    def effective_access(self) -> str:
        if self.access != 'auto':
            return self.access
        # Auto and manual 25 stay read-only; only an explicit 50/75 widens access.
        return 'read-only' if self.delegation_level in ('auto', 25) else 'full-access'

    @property
    def adaptive(self) -> bool:
        return self.delegation_level == 'auto'

    def as_dict(self) -> dict:
        return {
            'enabled': self.enabled,
            'enabled_source': self.enabled_source,
            'delegation_level': self.delegation_level,
            'delegation_mode': 'adaptive' if self.adaptive else 'fixed',
            'access': self.access,
            'effective_access': self.effective_access,
            'effort': self.effort,
            'effort_mode': 'frontier-auto' if self.effort == 'auto' else 'forced',
            'sources': dict(self.sources),
            'effective_access_source': (
                f'profile:{self.delegation_level} (access=auto)' if self.access == 'auto'
                else self.sources['access']),
            'max_workers': self.max_workers,
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
        raise SettingsError('Only delegation_level, access, effort and max_workers are allowed in delegation settings.')
    if 'delegation_level' in values:
        level = values['delegation_level']
        valid = (type(level) is str and level == 'auto') or (
            type(level) is int and level in LEVELS)
        if not valid:
            raise SettingsError('delegation_level must be auto, 25, 50 or 75.')
    if 'access' in values and values['access'] not in ACCESS:
        raise SettingsError('access must be auto, read-only or full-access.')
    if 'effort' in values and normalize_policy_effort(values['effort']) is None:
        raise SettingsError('effort must be auto, low, high or max (legacy medium maps to high).')
    if 'max_workers' in values and (type(values['max_workers']) is not int
                                  or not 1 <= values['max_workers'] <= MAX_MAX_WORKERS):
        raise SettingsError('max_workers must be an integer from 1 through 64.')


def _canonicalize_effort(values: dict) -> dict:
    """Normalize an accepted legacy effort spelling before any comparison or write."""
    if 'effort' in values:
        values['effort'] = normalize_policy_effort(values['effort'])
    return values


def read_bytes(path: Path) -> bytes | None:
    """Raw settings bytes; symlinks and hardlinks are refused."""
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


_read = read_bytes  # internal alias kept for callers in sibling modules


def read_values(path: Path) -> dict:
    raw = read_bytes(Path(path))
    try:
        values = tomllib.loads((raw or b'').decode('utf-8'))
    except (ValueError, UnicodeError):
        raise SettingsError(f'Invalid delegation TOML: {path}; file preserved.') from None
    _validate(values)
    return _canonicalize_effort(values)


def _merge_into(values: dict, sources: dict, label: str, path: Path, layer: dict) -> None:
    for key, value in layer.items():
        values[key], sources[key] = value, f'{label}:{path}'


def _snapshot(project: Path | None, values: dict, sources: dict) -> Policy:
    from . import activation
    active = activation.resolve(project)
    return Policy(values['delegation_level'], values['access'], MappingProxyType(dict(sources)),
                  values['effort'], active.enabled, active.source, values['max_workers'])


def resolve(root: Path | None = None, *, delegation_level: str | int | None = None,
            access: str | None = None, effort: str | None = None,
            max_workers: int | None = None,
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
        _merge_into(values, sources, label, path, read_values(path))
    overrides = {k: v for k, v in {
        'delegation_level': delegation_level, 'access': access, 'effort': effort, 'max_workers': max_workers,
    }.items() if v is not None}
    _validate(overrides)
    _canonicalize_effort(overrides)
    for key, value in overrides.items():
        values[key], sources[key] = value, 'cli'
    return _snapshot(project, values, sources)


def candidate_policy(root: Path | None, project_path: Path, project_values: dict, *,
                     global_path: Path | None = None) -> Policy:
    """Freeze the policy a project layer will have once project_values is published.

    The project layer comes from the caller; global settings and activation are
    still resolved from disk. Instructions can be rendered before publication.
    """
    _validate(project_values)
    project_values = _canonicalize_effort(dict(project_values))
    values = dict(DEFAULTS)
    sources = {name: 'default' for name in DEFAULTS}
    global_path = global_file() if global_path is None else Path(global_path)
    _merge_into(values, sources, 'global', global_path, read_values(global_path))
    _merge_into(values, sources, 'project', Path(project_path), dict(project_values))
    return _snapshot(project_root(root), values, sources)


def collect_changes(*, delegation_level: str | int | None = None,
                    access: str | None = None, effort: str | None = None,
                    max_workers: int | None = None) -> dict:
    """Validated partial update; at least one field is required, others stay saved."""
    changes = {k: v for k, v in {
        'delegation_level': delegation_level, 'access': access, 'effort': effort, 'max_workers': max_workers,
    }.items() if v is not None}
    _validate(changes)
    _canonicalize_effort(changes)
    if not changes:
        raise SettingsError('Specify --delegation-level, --access, --effort and/or --max-workers.')
    return changes


def merged_values(values: dict, changes: dict) -> dict:
    """Pure merge of a validated change set over currently saved values."""
    updated = dict(values)
    updated.update(changes)
    return updated


@contextmanager
def settings_lock(path: Path):
    """Hold the exclusive settings lock while a caller prepares related files.

    The lock serializes cooperating writers of the same settings file, so a caller may render
    dependent artifacts from a candidate policy and publish the file last.
    """
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    parent = path.parent.lstat()
    if not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.geteuid():
        raise SettingsError('Settings directory must be an owned ordinary directory.')
    lock = os.open(path.with_name(path.name + '.lock'),
                   os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        info = os.fstat(lock)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or
                info.st_uid != os.geteuid() or info.st_mode & 0o077):
            raise SettingsError('Unsafe settings lock; no settings changed.')
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield path
    finally:
        os.close(lock)


def publish_values(path: Path, previous: bytes | None, values: dict) -> None:
    """Atomic commit of a fully-formed settings file; refuses concurrent writers."""
    path = Path(path)
    raw = ''.join(f'{key} = {json.dumps(values[key])}\n'
                  for key in DEFAULTS if key in values).encode()
    mode = stat.S_IMODE(path.stat().st_mode) if previous is not None else 0o600
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            os.fchmod(stream.fileno(), mode)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        if read_bytes(path) != previous:
            raise SettingsError('Settings changed concurrently; retry the update.')
        os.replace(temporary, path)
        temporary = None
        try:
            fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        except BaseException as error:
            # os.replace is the commit point. Callers must retain dependent
            # artifacts even when durability confirmation fails afterwards.
            raise SettingsPublishedError(
                'Settings were published, but directory durability could not be confirmed.') from error
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def set_values(path: Path, *, delegation_level: str | int | None = None,
               access: str | None = None, effort: str | None = None,
               max_workers: int | None = None) -> bool:
    """Atomic partial update: changing a level never implicitly resets access."""
    changes = collect_changes(delegation_level=delegation_level, access=access,
                              effort=effort, max_workers=max_workers)
    path = Path(path)
    with settings_lock(path):
        previous = read_bytes(path)
        values = read_values(path)
        if all(values.get(key) == value for key, value in changes.items()):
            return False
        publish_values(path, previous, merged_values(values, changes))
        return True


def describe(policy: Policy) -> str:
    data = policy.as_dict()
    if policy.adaptive:
        level = ('delegation_level: auto (adaptive per task; no fixed percentage; '
                 f'source={policy.sources["delegation_level"]})')
    else:
        level = (f'delegation_level: {policy.delegation_level}% '
                 f'(target; source={policy.sources["delegation_level"]})')
    return '\n'.join((
        f'enabled: {str(policy.enabled).lower()} (source={policy.enabled_source})',
        level,
        f'access: {policy.access} (source={policy.sources["access"]})',
        f'effective_access: {policy.effective_access} (source={data["effective_access_source"]})',
        f'effort: {policy.effort} (source={policy.sources.get("effort", "default")}; '
        + ('frontier chooses low/high/max per assignment; legacy medium is accepted as high'
           if policy.effort == 'auto' else 'forced for new DeepSeek jobs') + ')',
        f'max_workers: {policy.max_workers} (source={policy.sources.get("max_workers", "default")}); '
        'additional workers wait in FIFO order; total_timeout: unlimited by default',
    ))


FINAL_REPORTING_GUIDANCE = (
    '### Final reporting of performed work\n'
    'For final summaries of performed work while delegation is enabled, end with short bullets '
    'in the user\'s language that separate work the coordinator completed personally from work '
    'actually delegated to DeepSeek, and name accepted worker results plus any rework, rejection '
    'or failure. Cover the reported task, including work and assignments from earlier turns. '
    'Never describe planned, running, failed or rejected work as completed. If nothing '
    'was delegated for the reported task, say so explicitly. After the bullets, give a coarse approximate '
    'coordinator/DeepSeek split of ACCEPTED WORK as two whole-number percentages totaling 100 percent, in the '
    'user\'s language, and label it exactly "subjective estimate, not measured" (translated into '
    'that language). Judge that split qualitatively from accepted scope and complexity and from '
    'coordinator review and rework. Never derive it from counts of calls, tasks, deliverables, '
    'files, lines, tokens, time or bullets; never reuse a configured Auto/25/50/75 profile; and '
    'never claim measured productivity or money, time or token savings. If even a rough estimate '
    'lacks supporting evidence, report the estimate as unavailable instead of inventing numbers. '
    'With no accepted worker contribution, use 100/0 for accepted work while still disclosing any '
    'failed or rejected attempts. Credit coordinator-native subagent work separately, never as the '
    'coordinator\'s own personal work and never as DeepSeek work; if it is included on the '
    'coordinator side of the split, say so explicitly. This is a reporting instruction only: it '
    'calculates no ratio, records no telemetry, adds no flag and changes no ledger schema, and the '
    'existing ban on percentages calculated from counts still stands. Disabled delegation, '
    'status-only turns and turns without performed work need no performed-work report.\n'
)


def final_reporting_guidance() -> str:
    """Shared coordinator final-summary contract for both runtimes."""
    return FINAL_REPORTING_GUIDANCE


def instructions(policy: Policy, runtime: str = 'codex') -> str:
    if runtime not in ('codex', 'claude'):
        raise SettingsError('Instruction runtime must be codex or claude.')

    if not policy.enabled:
        from .activation import DISABLED_GUIDANCE
        return DISABLED_GUIDANCE + '\n'

    if policy.effort == 'auto':
        effort_guidance = (
            'DeepSeek Team workers always use deepseek-flash. Effort policy is auto, so before each '
            'DeepSeek assignment the frontier coordinator must choose --effort low, --effort high '
            'or --effort max from the assigned task without asking the user: low for bounded/mechanical '
            'work, high for the normal case, and max for difficult debugging, cross-file reasoning '
            'or adversarial review. The legacy spelling --effort medium is still accepted and treated '
            'exactly as high. If a DeepSeek worker is launched directly without a frontier-selected '
            'effort, the runner uses high as an execution fallback only.\n'
        )
        effort_example = 'high'
        effort_note = (
            'Because effort policy is auto, replace high with low or max when the assigned task '
            'warrants it (medium is the accepted legacy alias for high). '
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
        'Plan and route every delegated subtask before assigning another agent, even a small '
        'read-only history, search or review task; a worker decision means DeepSeek. Generic '
        'parallelism, isolated context or convenience alone is not a sufficient reason to use a '
        'native subagent. Ordinary short answers the coordinator gives directly need no fake '
        'worker or plan; wait and control calls create no work, and new work sent to an already '
        'running agent also requires routing.\n'
        'Coordinator-native subagents remain available only as an explicit exception. Represent '
        'that choice in the plan with executor: "native-agent", a concrete delegation_reason, and '
        'a native_exception: either {"code": "explicit_user_request", "evidence": "specific user '
        'request"} or {"code": "native_capability", "capability": "specific capability or tool '
        'unavailable to a DeepSeek worker", "evidence": "why it is required"}. The coordinator '
        'attests this evidence; it is not mechanically proven user provenance. In auto mode use '
        'executor: "auto" and let the router resolve DeepSeek or coordinator. The native prompt or '
        'message must include [deepseek-team:TASK_ID:DELIVERABLE_ID] binding the registered '
        'native-agent scope, and its accepted or cancelled outcome is recorded like any other '
        'deliverable. Native agents complement DeepSeek workers and do not satisfy DeepSeek worker '
        'assignments required by the effective profile. Protected coordinator responsibilities '
        'remain with the coordinator: architecture, security, integration, final verification, '
        'secrets/signing and publishing stay coordinator-only. '
        'DeepSeek workers themselves remain leaf workers and must never delegate.\n'
    )

    full_profiles = {
        'auto': 'Auto delegates suitable work immediately by default. Actively split substantial work into meaningful independent implementation, test, fixture, documentation and review deliverables with executor:auto. Prepare interfaces and acceptance criteria before assigning; do not retain eligible work merely because history is missing or worker slots are busy.',
        25: 'Delegate bounded research, diagnosis and independent review. The coordinator performs the main implementation.',
        50: 'Delegate at least one separable implementation/test/docs slice when such work exists; coordinator defines architecture/interfaces and integrates.',
        75: 'Delegate most separable implementation, tests, fixtures, documentation, non-secret metadata and independent review before doing that same work yourself. Run independent assignments concurrently within the configured capacity; additional jobs queue.',
    }
    read_only_profiles = {
        'auto': 'Auto delegates suitable bounded research, diagnosis, design validation and independent review immediately with executor:auto and explicit acceptance criteria. Read-only auto never delegates writing tasks.',
        25: 'Delegate bounded research, diagnosis and independent review. The coordinator performs the main implementation.',
        50: 'Delegate substantial investigation, design validation, test planning and independent review before the coordinator implements the corresponding changes.',
        75: 'Delegate most separable analysis, diagnostics, design validation, test planning and independent review. Run independent assignments concurrently within the configured capacity; additional jobs queue.',
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
            'Do not expand access merely to satisfy the target profile. '
            + ('Read-only auto does not delegate writing tasks; implementation stays with the coordinator.\n'
               if policy.adaptive else '\n')
        )

    hygiene = (
        '### Workspace and branch hygiene\n'
        'Coordinator process guidance, separate from the worker OS sandbox. Each write-capable '
        'worker already runs in a fully independent Git repository created under the private state '
        'directory by workspace.create; its internal deepseek/<id> ref lives only in that private '
        'copy and is never a branch or worktree of the source project. Reuse that existing '
        'isolation instead of building isolation in the project. Create no synthetic branch, '
        'worktree or commit merely to invoke DeepSeek: a worker launch needs no project branch. '
        'When new coordinator isolation is genuinely required, prefer a detached worktree '
        '(git worktree add --detach) over a named task branch, and record which temporary branch '
        'or worktree the coordinator created and why. After accepted integration, verify a clean '
        'status and commit reachability before removing only the coordinator\'s own temporary '
        'worktree and its fully merged branch. Preserve active, failed, unaccepted and foreign '
        'work: never bulk-prune, force-delete or rewrite work you do not own. This is not worker '
        'OS enforcement, there is no automatic cleanup engine and no broad cleanup command; worker '
        'copies remain retained outside the project for review and recovery and are never deleted '
        'automatically.\n'
    )

    guidance = (
        full_profiles if policy.effective_access == 'full-access' else read_only_profiles
    )[policy.delegation_level]

    if policy.adaptive:
        adaptive = (
            'Delegation level is auto: the profile is adaptive per task, chosen from local outcomes and '
            'bounded external evidence instead of a fixed percentage. New auto plans must record '
            'executor: "auto" together with a "features" card capturing the task before execution: '
            'kind, domain, operation, localization, coupling, verification, clarity, risk, scope_size, '
            'runtime, model, effort and context_version. The coordinator classifies the task; the router '
            'resolves its saved feature card. Workers must not self-select a profile or stage. '
            'Admission is immediate: suitable small/medium low/medium-risk '
            'local/component tasks with clear requirements, known/partial localization and declared '
            'tests or a reproducer can start without prior evidence. Read-only deliverables and '
            'documentation may use explicit manual acceptance criteria. High/protected/unknown-risk, '
            'unbounded or unverifiable work stays with the coordinator. There is no initial trial '
            'quota or periodic coordinator holdout. Missing data and unknown prices do not block '
            'suitable work and never imply measured savings; supported poor economics still veto '
            'delegation. A rejected result or three distinct recent rework cases pause only that '
            'family for the configured cooldown (300 seconds by default). One rework is recorded '
            'without pausing the family; after a pause immediate admission resumes. '
            'There is no automatic paid exploration outside the '
            'normal task stream and no automatic permission widening; keep the resolved access and explicit executor '
            'choices. Inspect adaptive routing state with deepseek-team routing status and adjust it '
            'with deepseek-team routing configure.\n'
        )
    else:
        adaptive = (
            'The fixed delegation target is not a measurement: completed assignments still collect '
            'feedback so manual 25/50/75 behavior can be reviewed, with no invented ratio, call or '
            'token percentages. Explicit executor choices are respected, and a fixed target never '
            'widens the resolved access.\n'
        )

    coordinator = 'Claude' if runtime == 'claude' else 'Codex'
    process = (
            f'{coordinator} process integration: after project init and enabling hooks in the runtime, SessionStart/'
            'UserPromptSubmit provide the current coordination task id. Plan and route every delegated '
            'subtask, even a small read-only history, search or review task, before assigning another '
            'agent; Auto registers executor:auto and a worker decision means DeepSeek. Before coordinator '
            'source edits, submit a concrete JSON distribution with '
            'deepseek-team coordination plan --task TASK_ID; include deliverable id/kind/scope, '
            'executor, acceptance criteria, dependencies and checks. Run each worker assignment with '
            f'deepseek-team worker --runtime {runtime} --effort {effort_example} '
            '--coord-task TASK_ID --coord-assignment ASSIGNMENT_ID. '
            + effort_note +
            'The runner records start/result/workspace/checks automatically. After reviewing a result, '
            'record its use with deepseek-team coordination use. Record verified coordinator and '
            'native-agent outcomes with deepseek-team coordination result; accepted or explicitly '
            'cancelled outcomes are required for completion. Later recognized scope mutations '
            'require fresh acceptance. New substantial scope requires '
            f'a revised plan. {coordinator} PreToolUse blocks recognized source edits while the distribution '
            'is missing/noncompliant, blocks unplanned scope, and blocks duplicate work owned by a '
            'pending worker assignment. This parent launch/message gate covers only the events the '
            'runtime actually delivers, not every possible write; Codex and Claude coverage is verified '
            'separately and universal hook coverage must never be claimed. Where the runtime cannot '
            'intercept an action, these instructions remain authoritative. Hooks do not replace the '
            'mandatory, unchanged worker OS sandbox. '
            'An unplanned turn without recorded work closes without a distribution plan; status '
            'prompts retain existing unfinished tasks. Stop requests continuation for unfinished '
            'assignments/results, including coordinator and native-agent outcomes. On repeated Stop '
            'it warns and keeps the ledger unfinished without vetoing another hook continuation. '
    )
    if runtime == 'codex':
        process += (
            'Native hook trust is controlled by Codex and is not inferred by this package.\n')
    else:
        process += (
            'Claude hooks cover Edit, Write, NotebookEdit and recognized mutating Bash commands. '
            'Stop requests a continuation for pending/undispositioned results; if stop_hook_active '
            'is already true, it warns the user and leaves the task unfinished in the ledger '
            'instead of blocking again. Native subagent hook events do not change the coordinator ledger. '
            'Check enabled hooks through Claude /hooks; --bare or native settings can disable them.\n')

    label = 'auto (adaptive)' if policy.adaptive else f'{policy.delegation_level}%'
    return (
        f'### Effective delegation profile: {label} / {policy.effective_access}; '
        f'effort={policy.effort}\n'
        + common + guidance + '\n' + adaptive + access + hygiene + process
        + f'Execution capacity is {policy.max_workers}; configure max_workers independently of access. '
          'Plan all eligible deliverables; launch independent jobs concurrently and let excess jobs wait '
          'in FIFO order. Capacity is not a reason to retain their implementation with the coordinator. '
          'Report accepted work, verification results and any rework; only report money savings when measured.\n'
        + 'While a worker runs, work only on independent scope. Review the actual diff and recorded '
          'checks without repeating the whole investigation or rewriting correct code. DeepSeek workers never '
          'stage, commit, push, publish, deploy, access production services or delegate.\n'
        + final_reporting_guidance()
    )
