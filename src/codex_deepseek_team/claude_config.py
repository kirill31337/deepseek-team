"""Install only DeepSeek Team's command hooks in Claude's user settings.

New definitions use an absolute package entrypoint. The exact legacy bare
command remains recognized only so reinstall/remove can migrate it safely.
"""
import json
import os
from pathlib import Path
import shlex
import shutil
import stat

from .config import ConfigError, atomic_write, read_regular


HOOK_ARGUMENTS = ('coordinator-hook', '--runtime', 'claude')
HOOK_COMMAND = 'deepseek-team ' + ' '.join(HOOK_ARGUMENTS)
HOOK_EVENTS = {
    'SessionStart': 'startup|resume|clear|compact',
    'UserPromptSubmit': None,
    'PreToolUse': '(?:.*[./])?(?:Read|Grep|Glob|Bash|exec_command|shell_command|Edit|Write|NotebookEdit|apply_patch|spawn_agent|send_message|send_input|followup_task|assign_agent_task|resume_agent|Agent|Task)',
    'Stop': None,
}


def claude_home():
    configured = os.environ.get('CLAUDE_CONFIG_DIR')
    return Path(configured).expanduser() if configured else Path.home() / '.claude'


def plans_directory(project_root):
    """Resolve the plan directory from user, project and local settings files.

    This is a narrow exception for native planning, not a full Claude settings
    resolver. Invalid settings never grant an exception to the edit gate.
    """
    directory = claude_home() / 'plans'
    for path in (claude_home() / 'settings.json', project_root / '.claude/settings.json',
                 project_root / '.claude/settings.local.json'):
        try:
            raw = read_regular(path)
            data = json.loads(raw) if raw is not None else {}
        except (ConfigError, ValueError, UnicodeError):
            return None
        if not isinstance(data, dict):
            return None
        if 'plansDirectory' in data:
            value = data['plansDirectory']
            if not isinstance(value, str) or not value.strip():
                return None
            directory = Path(value).expanduser()
    if not directory.is_absolute():
        directory = project_root / directory
    return directory.resolve()


def _lexical_absolute(value):
    """Normalize an absolute path without resolving a console-script symlink."""
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    return Path(os.path.abspath(path))


def resolve_entrypoint():
    """Return the absolute lexical ``deepseek-team`` command for hooks.

    Resolution happens before settings are read or written. A missing console
    script therefore fails with an actionable error and leaves user settings,
    authentication and foreign hooks untouched. The lexical PATH entry is kept
    so a symlink named ``deepseek-team`` remains recognizable when its target has
    a different basename.
    """
    found = shutil.which('deepseek-team')
    if not found:
        raise ConfigError(
            'DeepSeek Team cannot install Claude Code coordination hooks because the '
            '`deepseek-team` executable cannot be resolved on PATH. Install the package '
            'entrypoint (for pipx run `pipx ensurepath`; for uv run `uv tool update-shell`; '
            'or activate the environment that provides it), restart the terminal, and '
            'rerun `deepseek-team hooks install --runtime claude`. No settings were changed.')
    return str(_lexical_absolute(found))


def _render_hook_command(entrypoint):
    return shlex.quote(str(entrypoint)) + ' ' + ' '.join(HOOK_ARGUMENTS)


def hook_command(executable=None):
    """Render a canonical shell-safe absolute hook command."""
    if executable is None:
        path = Path(resolve_entrypoint())
    else:
        path = _lexical_absolute(executable)
    return _render_hook_command(path)


def _group(event, matcher, command):
    """Build one handler; only PreToolUse is a native fail-closed gate."""
    handler = {
        'type': 'command', 'command': command, 'timeout': 10,
        'statusMessage': 'DeepSeek Team coordination policy',
    }
    if event == 'PreToolUse':
        handler['onFailure'] = 'block'
    group = {'hooks': [handler]}
    if matcher is not None:
        group['matcher'] = matcher
    return group


def _read(home):
    path = Path(home) / 'settings.json'
    raw = read_regular(path)
    try:
        data = json.loads(raw) if raw is not None else {}
    except (ValueError, UnicodeError):
        raise ConfigError('Claude settings.json is invalid; existing content was preserved.') from None
    if not isinstance(data, dict) or not isinstance(data.get('hooks', {}), dict):
        raise ConfigError('Claude settings.json and its hooks section must be objects.')
    if any(not isinstance(groups, list) for groups in data.get('hooks', {}).values()):
        raise ConfigError('Claude hook event entries must be arrays; existing content was preserved.')
    return path, raw, data


def _canonical_absolute_owned(command):
    """Return the canonical command only for an exact generated absolute form."""
    if not isinstance(command, str):
        return None
    try:
        argv = shlex.split(command)
    except ValueError:
        return None
    if len(argv) != len(HOOK_ARGUMENTS) + 1 or tuple(argv[1:]) != HOOK_ARGUMENTS:
        return None
    lexical = _lexical_absolute(argv[0])
    if not lexical.is_absolute() or lexical.name != 'deepseek-team':
        return None
    canonical = _render_hook_command(lexical)
    return canonical if command == canonical else None


def _owned(handler):
    """True only for the exact legacy or canonical generated command form.

    Parsing alone is insufficient: shell metacharacters and dynamic
    substitutions are foreign unless the complete command is the exact
    shell-quoted rendering that installation would have written.
    """
    if not isinstance(handler, dict):
        return False
    command = handler.get('command')
    if command == HOOK_COMMAND:
        return True
    return _canonical_absolute_owned(command) is not None


def _strip(data):
    """Preserve other handlers even when they share a group with ours."""
    changed = False
    hooks = data.get('hooks', {})
    for event, groups in list(hooks.items()):
        kept = []
        for group in groups:
            handlers = group.get('hooks') if isinstance(group, dict) else None
            if not isinstance(handlers, list) or not any(_owned(h) for h in handlers):
                kept.append(group)
                continue
            changed = True
            remaining = [h for h in handlers if not _owned(h)]
            if remaining:
                kept.append(dict(group, hooks=remaining))
        if kept or not groups:
            hooks[event] = kept
        else:
            del hooks[event]
    if changed and not hooks:
        data.pop('hooks', None)
    return changed


def _write(path, data, raw):
    updated = (json.dumps(data, indent=2, ensure_ascii=False) + '\n').encode()
    if updated == raw:
        return False
    mode = stat.S_IMODE(path.stat().st_mode) if raw is not None else 0o600
    atomic_write(path, updated, raw, mode)
    return True


def install(home=None, executable=None):
    command = hook_command(executable)
    home = claude_home() if home is None else Path(home)
    path, raw, data = _read(home)
    _strip(data)
    hooks = data.setdefault('hooks', {})
    for event, matcher in HOOK_EVENTS.items():
        hooks.setdefault(event, []).append(_group(event, matcher, command))
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    return _write(path, data, raw)


def remove(home=None):
    path, raw, data = _read(claude_home() if home is None else home)
    if not _strip(data):
        return False
    return _write(path, data, raw)


def _absolute_owned_command(handler):
    """Return the canonical command of an executable owned handler, else ``None``."""
    if not isinstance(handler, dict):
        return None
    command = _canonical_absolute_owned(handler.get('command'))
    if command is None:
        return None
    path = Path(shlex.split(command)[0])
    try:
        if not path.is_file() or not os.access(path, os.X_OK):
            return None
    except OSError:
        return None
    return command


def status(home=None, executable=None):
    """Check the actual event/matcher/handler definitions, not just their count.

    With an explicit executable, or when the current entrypoint resolves on
    PATH, the installed command must match exactly. A read-only status check can
    still recognize a structurally valid absolute definition when PATH itself is
    unavailable; only installation requires a fresh resolution.
    """
    command = None
    if executable is not None:
        command = hook_command(executable)
    else:
        try:
            command = hook_command()
        except ConfigError:
            pass
    _, raw, data = _read(claude_home() if home is None else home)
    if raw is None or data.get('disableAllHooks'):
        return False
    hooks = data.get('hooks', {})
    observed = set()
    for event, matcher in HOOK_EVENTS.items():
        matches = []
        for group in hooks.get(event, []):
            handlers = group.get('hooks') if isinstance(group, dict) else None
            if not isinstance(handlers, list):
                continue
            for handler in handlers:
                if _owned(handler):
                    matches.append(dict(group, hooks=[handler]))
        if command is not None:
            if matches != [_group(event, matcher, command)]:
                return False
            continue
        if len(matches) != 1:
            return False
        installed = _absolute_owned_command(matches[0]['hooks'][0])
        if installed is None or matches != [_group(event, matcher, installed)]:
            return False
        observed.add(installed)
    if command is None and len(observed) != 1:
        return False
    return True
