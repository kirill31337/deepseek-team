"""Explicit one-project coordinator context for the Codex and Claude hooks.

A lifecycle hook normally serves the Git repository that contains the session
working directory.  The optional ``DEEPSEEK_TEAM_PROJECT_ROOT`` environment
variable binds a session to exactly one existing attached Git root, which also
supports sessions started in a non-Git parent directory.  The selection is an
explicit, validated opt-in: relative values, missing paths, non-repository
directories, subdirectories and unattached roots produce an actionable
diagnostic, and no other project is ever searched, adopted or attached.

The context also records the *real* command working directory
(``tool_input.workdir``/``cwd``, otherwise the event cwd).  Scope checks resolve
relative mutations against that directory, never against the selected root, so
a bound project can not authorize writes that really happen somewhere else.

An unbound session is warned when a tool call explicitly names an attached
enabled repository.  Besides ``file_path``/``path``/``workdir`` keys, recognized
literal shell write paths and ``apply_patch`` targets are candidates too, so a
shell redirection into an attached project is not silently allowed.  Only names
the call itself carries are used: children are never scanned and no project is
adopted or bound.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re

from . import activation, coordinator_activity, project, settings
from .shell_mutation import classify_shell_mutation_scoped


SELECTION_VARIABLE = 'DEEPSEEK_TEAM_PROJECT_ROOT'
_PATH_KEYS = ('file_path', 'notebook_path', 'path')
_DIRECTORY_KEYS = ('workdir', 'cwd')
_RUNTIMES = {'codex': 'Codex', 'claude': 'Claude Code'}
_APPLY_PATCH_TARGET = re.compile(
    r'^\*\*\* (?:(?:Update|Add|Delete) File|Move to): (.+)$', re.M)


@dataclass(frozen=True)
class Target:
    """One path/id a tool call explicitly names, already made absolute."""

    path: Path
    directory: bool


@dataclass(frozen=True)
class Resolution:
    """Selected project root plus the real command working directory.

    ``root`` is ``None`` when this event has no attached project: either the
    session working directory is outside any Git work tree, or the repository
    there is not attached.  ``error`` carries the actionable diagnostic for an
    invalid explicit selection; the session then stays unbound.
    """

    root: Path | None
    bound: bool
    error: str
    event_cwd: Path
    tool_cwd: Path
    targets: tuple[Target, ...]


def _selection_value(environ=None) -> str | None:
    raw = (environ if environ is not None else os.environ).get(SELECTION_VARIABLE)
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return None


def _absolute(value: str, base: Path) -> Path:
    candidate = Path(value)
    return candidate if candidate.is_absolute() else base / candidate


def tool_working_directory(payload: dict, event_cwd: Path) -> Path:
    """The real directory a tool call runs in: workdir/cwd, else the event cwd."""
    data = payload.get('tool_input')
    if isinstance(data, dict):
        for key in _DIRECTORY_KEYS:
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return _absolute(value, event_cwd)
    return event_cwd


def apply_patch_paths(command: str) -> list[str]:
    """Literal paths an apply_patch body names, before any cwd resolution."""
    return _APPLY_PATCH_TARGET.findall(command)


def _shell_command_text(data) -> str:
    """The command text a shell tool call carries, mirroring the hook payload."""
    if isinstance(data, str):
        return data
    if not isinstance(data, dict):
        return ''
    command = data.get('command')
    if isinstance(command, str) and command:
        return command
    for key in ('cmd', 'shell', 'script'):
        value = data.get(key)
        if isinstance(value, str) and value:
            return value
    return ''


def _patch_text(data) -> str:
    """The patch body an apply_patch call carries, whether string or mapping."""
    if isinstance(data, str):
        return data
    if isinstance(data, dict):
        return '\n'.join(data[key] for key in ('command', 'input', 'patch')
                         if isinstance(data.get(key), str))
    return ''


def explicit_targets(payload: dict, event_cwd: Path) -> tuple[Target, ...]:
    """Absolute paths this tool call explicitly names, in declaration order.

    Includes recognized literal shell write paths and apply_patch targets so an
    unbound session cannot silently mutate an attached project through a shell
    redirection.  Relative names resolve against the real tool working
    directory; nothing is discovered by scanning the filesystem.
    """
    data = payload.get('tool_input')
    found = []
    seen = set()

    def add(path: Path, directory: bool) -> None:
        identity = (str(path), directory)
        if identity in seen:
            return
        seen.add(identity)
        found.append(Target(path, directory))

    if isinstance(data, dict):
        for key in _PATH_KEYS + _DIRECTORY_KEYS:
            value = data.get(key)
            if not isinstance(value, str) or not value.strip():
                continue
            add(_absolute(value, event_cwd), key in _DIRECTORY_KEYS)
    tool = coordinator_activity.normalize_tool_name(payload.get('tool_name', ''))
    working = tool_working_directory(payload, event_cwd)
    if tool == 'apply_patch':
        for raw in apply_patch_paths(_patch_text(data)):
            add(_absolute(raw, working), False)
    elif coordinator_activity.is_shell_tool(tool):
        # The scoped classifier resolves one proved absolute ``cd`` prefix and
        # returns no path at all when the real working directory is uncertain.
        mutation, paths, _cwd_uncertain = classify_shell_mutation_scoped(
            _shell_command_text(data))
        if mutation:
            for raw in paths:
                add(_absolute(raw, working), False)
    return tuple(found)


def validate_selection(raw: str, runtime: str) -> tuple[Path | None, str]:
    """Validate one explicit absolute selection; never searches or adopts."""
    candidate = Path(raw)
    if not candidate.is_absolute():
        return None, 'the value is not an absolute path'
    try:
        is_directory = candidate.is_dir()
    except OSError:
        is_directory = False
    if not is_directory:
        return None, 'the path is not an existing directory'
    root = settings.project_root(candidate)
    if root is None:
        return None, 'the path is not inside a Git working copy'
    try:
        same = root.resolve() == candidate.resolve()
    except (OSError, RuntimeError, ValueError):
        same = False
    if not same:
        return None, ('the path is not the Git working-copy root (the root is '
                      + json.dumps(str(root)) + ')')
    try:
        attached = project.is_attached(root, runtime)
    except project.ProjectError as error:
        return None, 'the managed-block binding is malformed: ' + str(error)
    if not attached:
        return None, ('the repository root is not attached for ' + runtime
                      + '; run `deepseek-team init --coordinator ' + runtime
                      + ' .` from that repository root to attach it')
    return root, ''


def resolve(payload: dict, runtime: str, *, environ=None, event_cwd=None) -> Resolution:
    """Resolve the governing project root and the real command working directory."""
    base = Path(event_cwd) if event_cwd is not None else Path(payload.get('cwd') or '.')
    tool = tool_working_directory(payload, base)
    targets = explicit_targets(payload, base)
    raw = _selection_value(environ)
    if raw is None:
        return Resolution(settings.project_root(base), False, '', base, tool, targets)
    root, problem = validate_selection(raw, runtime)
    error = '' if root is not None else binding_diagnostic(raw, problem, runtime)
    return Resolution(root, True, error, base, tool, targets)


def binding_diagnostic(raw: str, problem: str, runtime: str) -> str:
    """Actionable diagnostic for an invalid explicit selection; never a fallback."""
    return (
        'DeepSeek Team coordinator context: ' + SELECTION_VARIABLE + ' is set to '
        + json.dumps(raw) + ', but it is not a usable explicit project selection: '
        + problem + '. This session stays unbound; no other project was searched or '
        'adopted. Set ' + SELECTION_VARIABLE + ' to the absolute existing Git root of a '
        'repository attached for ' + runtime + ' (run `deepseek-team init --coordinator '
        + runtime + ' .` there), or unset it to use the session working directory.')


def selection_diagnostic(root: Path, runtime: str) -> str:
    """Explain how to select an attached project from an unbound parent session."""
    name = _RUNTIMES.get(runtime, runtime)
    return (
        'DeepSeek Team coordinator context: this session is not bound to an attached '
        'project, but this tool call explicitly targets the attached project '
        + json.dumps(str(root)) + '. Its lifecycle policy, task ledger and distribution '
        'gates cannot run from here, so the call was denied instead of being silently '
        'allowed. Start ' + name + ' in that repository (or one of its subdirectories), '
        'or set ' + SELECTION_VARIABLE + '=' + json.dumps(str(root))
        + ' for this session, then retry.')


def target_binding_diagnostic(root: Path, error, runtime: str) -> str:
    """Diagnostic for an explicitly targeted repository with a malformed block."""
    return (
        'DeepSeek Team coordinator context: this tool call targets ' + json.dumps(str(root))
        + ', whose managed-block binding for ' + runtime + ' is malformed: ' + str(error)
        + '. The project is not treated as attached and the file was not modified. '
        'Repair or remove the conflicting block, then rerun `deepseek-team init '
        '--coordinator ' + runtime + ' .` from that repository root.')


def _nearest_directory(path: Path) -> Path | None:
    current = path
    while True:
        try:
            if current.is_dir():
                return current
        except OSError:
            return None
        parent = current.parent
        if parent == current:
            return None
        current = parent


def _target_root(target: Target) -> Path | None:
    """The Git root a named target belongs to, without scanning the workspace."""
    try:
        path = target.path.resolve()
    except (OSError, RuntimeError, ValueError):
        return None
    if target.directory:
        anchor = path
    else:
        try:
            anchor = path if path.is_dir() else path.parent
        except OSError:
            anchor = path.parent
    anchor = _nearest_directory(anchor)
    if anchor is None:
        return None
    return settings.project_root(anchor)


def selection_denial(resolution: Resolution, runtime: str) -> str:
    """Deny reason when an unbound session explicitly targets an attached project.

    Unattached, unrelated and disabled projects stay inert: they return no
    reason, so the caller keeps the session untouched and creates no ledger or
    storage state.
    """
    for target in resolution.targets:
        root = _target_root(target)
        if root is None:
            continue
        try:
            attached = project.is_attached(root, runtime)
        except project.ProjectError as error:
            return target_binding_diagnostic(root, error, runtime)
        if not attached:
            continue
        try:
            enabled = activation.resolve(root).enabled
        except settings.SettingsError:
            continue
        if enabled:
            return selection_diagnostic(root, runtime)
    return ''
