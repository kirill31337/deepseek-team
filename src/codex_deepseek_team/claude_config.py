"""Install only DeepSeek Team's command hooks in Claude's user settings."""
import json
import os
from pathlib import Path
import stat

from .config import ConfigError, atomic_write, read_regular


HOOK_COMMAND = 'deepseek-team coordinator-hook --runtime claude'
HOOK_EVENTS = {
    'SessionStart': 'startup|resume|clear|compact',
    'UserPromptSubmit': None,
    'PreToolUse': 'Bash|Edit|Write|NotebookEdit|(?:.*[./])?(?:spawn_agent|send_message|send_input|followup_task|assign_agent_task|resume_agent|Agent|Task)',
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


def _group(matcher):
    group = {'hooks': [{
        'type': 'command', 'command': HOOK_COMMAND, 'timeout': 10,
        'statusMessage': 'DeepSeek Team coordination policy',
    }]}
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


def _owned(handler):
    return isinstance(handler, dict) and handler.get('command') == HOOK_COMMAND


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


def install(home=None):
    home = claude_home() if home is None else Path(home)
    path, raw, data = _read(home)
    _strip(data)
    hooks = data.setdefault('hooks', {})
    for event, matcher in HOOK_EVENTS.items():
        hooks.setdefault(event, []).append(_group(matcher))
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    return _write(path, data, raw)


def remove(home=None):
    path, raw, data = _read(claude_home() if home is None else home)
    if not _strip(data):
        return False
    return _write(path, data, raw)


def status(home=None):
    """Check the actual event/matcher/handler definitions, not just their count."""
    _, raw, data = _read(claude_home() if home is None else home)
    if raw is None or data.get('disableAllHooks'):
        return False
    hooks = data.get('hooks', {})
    for event, matcher in HOOK_EVENTS.items():
        expected = _group(matcher)
        matches = []
        for group in hooks.get(event, []):
            handlers = group.get('hooks') if isinstance(group, dict) else None
            if not isinstance(handlers, list):
                continue
            for handler in handlers:
                if _owned(handler):
                    matches.append(dict(group, hooks=[handler]))
        if matches != [expected]:
            return False
    return True
