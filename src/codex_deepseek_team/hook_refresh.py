"""Refresh already configured coordinator hooks after a package upgrade.

This module never installs a first integration. It recognizes package-owned
records already present in the user's Codex/Claude configuration or DSH profile
patch, migrates their definitions with the existing atomic installers, and
reports every action the user still has to take (native restart/trust or an
explicit enable/install decision).
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import sys

from . import __version__, claude_config, config, dsh_config, worker


SCHEMA_VERSION = 1
EXIT_COMPLETED = 0
EXIT_ACTION_REQUIRED = 3
EXIT_FAILED = 78

_RUNTIME_NAMES = {'codex': 'Codex', 'claude': 'Claude Code', 'qwen': 'Qwen (DSH)'}
_CLI_AUTO_REFRESH = frozenset({
    'config', 'coordination', 'doctor', 'lessons', 'on', 'off', 'routing',
    'sandbox', 'status', 'workspace',
})


def _display(runtime: str) -> str:
    return _RUNTIME_NAMES[runtime]


def _detail(error) -> str:
    text = ' '.join(str(error).split())
    return text[:500] + ('...' if len(text) > 500 else '')


def _action(code: str, runtime: str, message: str, command: str) -> dict:
    return {'code': code, 'runtime': runtime, 'message': message, 'command': command}


def _review_action(runtime: str) -> dict:
    name = _display(runtime)
    if runtime == 'qwen':
        return _action(
            'review_native_hooks', runtime,
            f'Reload the DeepSeek Harness profile (or restart DSH) and start a new {name} '
            'session so the bridge plugin loads from the profile patch. DeepSeek Team cannot '
            'inspect or grant the native profile load. User confirmation remains pending.',
            'deepseek-team hooks status --runtime qwen')
    return _action(
        'review_native_hooks', runtime,
        f'Start a new {name} session and review DeepSeek Team in /hooks. '
        'Approve native hook trust if prompted; DeepSeek Team cannot inspect or '
        'grant native trust. User confirmation remains pending.',
        '/hooks')


def _disabled_action(runtime: str) -> dict:
    name = _display(runtime)
    return _action(
        'enable_disabled_hooks', runtime,
        f'DeepSeek Team {name} hook definitions are configured, but native hooks are '
        'disabled by disableAllHooks. DeepSeek Team preserved that setting and will not '
        f're-enable hooks automatically. Re-enable hooks, then start a new {name} session '
        'and review DeepSeek Team in /hooks. User confirmation remains pending.',
        '/hooks')


def _install_action(runtime: str) -> dict:
    name = _display(runtime)
    tail = ('then reload the DSH profile (or restart DSH) and start a new session.'
            if runtime == 'qwen' else
            f'then start a new {name} session and review DeepSeek Team in /hooks.')
    return _action(
        'install_hooks', runtime,
        f'No existing DeepSeek Team {name} hook integration was found. Refresh never '
        'creates a new integration; configure it explicitly with the command below, '
        + tail + ' User confirmation remains pending.',
        f'deepseek-team hooks install --runtime {runtime}')


def _repair_action(runtime: str, error) -> dict:
    name = _display(runtime)
    tail = ('After repair, reload the DSH profile (or restart DSH) and start a new session.'
            if runtime == 'qwen' else
            f'After repair, start a new {name} session and review DeepSeek Team in /hooks. '
            'Approve native hook trust if prompted.')
    return _action(
        'repair_hooks', runtime,
        f'DeepSeek Team could not refresh the {name} coordination hooks: {_detail(error)}. '
        'Existing configuration was preserved where possible. Repair the reported '
        'problem, then rerun the command below. '
        + tail + ' User confirmation remains pending.',
        f'deepseek-team hooks refresh --runtime {runtime} --notify')


def _rowed(runtime: str, status: str, changed: bool) -> dict:
    return {'runtime': runtime, 'status': status, 'changed': changed}


def _looks_executable(path: Path) -> bool:
    try:
        return path.is_file() and os.access(path, os.X_OK)
    except OSError:
        return False


def _claude_executable(home: Path):
    """Use recorded owned evidence before any PATH coordinator discovery."""
    recorded = claude_config.owned_entrypoints(home)
    if len(recorded) == 1:
        return recorded[0]
    running = Path(sys.argv[0])
    if running.name == 'deepseek-team' and _looks_executable(running):
        return running
    try:
        found = shutil.which('deepseek-team')
    except OSError:
        found = None
    if found:
        return Path(found)
    candidate = Path(sys.executable).parent / 'deepseek-team'
    if _looks_executable(candidate):
        return candidate
    if recorded:
        return recorded[0]
    return None


def _refresh_codex(home: Path):
    """Return a hook row when ownership exists, else ``None`` (quiet no-op)."""
    if not config.codex_hooks_owned(home):
        return None
    changed = config.install_codex_hooks(home)
    return _rowed('codex', 'refreshed' if changed else 'current', changed)


def _refresh_claude(home: Path):
    """Return ``(row, extra_actions)`` or ``None`` for no owned integration."""
    if not claude_config.owned(home):
        return None, []
    if claude_config.disabled(home):
        return _rowed('claude', 'disabled', False), [_disabled_action('claude')]
    executable = _claude_executable(home)
    changed = claude_config.install(home, executable=executable)
    return _rowed('claude', 'refreshed' if changed else 'current', changed), []


def _qwen_executable():
    """Use recorded owned evidence before any PATH coordinator discovery."""
    recorded = dsh_config.owned_entrypoints()
    if len(recorded) == 1:
        return recorded[0]
    running = Path(sys.argv[0])
    if running.name == 'deepseek-team' and _looks_executable(running):
        return running
    try:
        found = shutil.which('deepseek-team')
    except OSError:
        found = None
    if found:
        return Path(found)
    candidate = Path(sys.executable).parent / 'deepseek-team'
    if _looks_executable(candidate):
        return candidate
    if recorded:
        return recorded[0]
    return None


def _refresh_qwen():
    """Return ``(row, extra_actions)`` or ``None`` for no owned integration."""
    if not dsh_config.owned():
        return None, []
    executable = _qwen_executable()
    changed = dsh_config.install(executable=executable)
    return _rowed('qwen', 'refreshed' if changed else 'current', changed), []


def _selected_runtimes(runtime: str) -> tuple[str, ...]:
    if runtime == 'auto':
        return ('codex', 'claude', 'qwen')
    if runtime == 'both':
        return ('codex', 'claude')
    if runtime == 'all':
        return ('codex', 'claude', 'qwen')
    if runtime in ('codex', 'claude', 'qwen'):
        return (runtime,)
    raise ValueError('runtime must be auto, codex, claude, qwen, both or all')


def _status(hooks: list[dict], actions: list[dict]) -> str:
    if any(row.get('status') == 'failed' for row in hooks):
        return 'failed'
    if actions:
        return 'action_required'
    return 'completed'


def refresh_installed(runtime: str = 'auto', *, notify: bool = False,
                      implicit: bool = False) -> dict:
    """Refresh package-owned hook definitions already present in user config.

    ``runtime='auto'`` discovers ownership only from existing configuration files.
    Missing or foreign-only auto integrations are quiet no-ops and are never
    created. Explicit runtimes report missing integrations as user action.
    ``implicit`` marks the best-effort first-use CLI refresh: it never touches
    the DSH profile, whose bridge migration stays an explicit user action.
    """
    selected = _selected_runtimes(runtime)
    if implicit and runtime == 'auto':
        selected = tuple(name for name in selected if name != 'qwen')
    automatic = runtime == 'auto'
    hooks: list[dict] = []
    actions: list[dict] = []
    for name in selected:
        try:
            if name == 'qwen':
                outcome, extra_actions = _refresh_qwen()
            else:
                home = worker.codex_home() if name == 'codex' else claude_config.claude_home()
                if name == 'codex':
                    outcome = _refresh_codex(home)
                    extra_actions = []
                else:
                    outcome, extra_actions = _refresh_claude(home)
        except Exception as error:
            if automatic and name == 'qwen' and isinstance(error, config.ConfigError):
                # Auto discovery stays quiet when DSH is absent or its profile
                # choice is ambiguous; an explicit --runtime qwen still reports it.
                continue
            row = _rowed(name, 'failed', False)
            hooks.append(row)
            actions.append(_repair_action(name, error))
            continue
        if outcome is None:
            if automatic:
                continue
            hooks.append(_rowed(name, 'missing', False))
            actions.append(_install_action(name))
            continue
        hooks.append(outcome)
        actions.extend(extra_actions)
        if outcome['changed'] or (notify and outcome['status'] == 'current'):
            actions.append(_review_action(name))
    return {
        'schema_version': SCHEMA_VERSION,
        'version': __version__,
        'status': _status(hooks, actions),
        'hooks': hooks,
        'required_actions': actions,
        'requires_user_action': bool(actions),
    }


def render_required_actions(report: dict) -> str:
    """Action text shared by explicit output, CLI first use and hook context."""
    actions = report.get('required_actions') or []
    if not actions:
        return ''
    lines = ['DeepSeek Team hook refresh requires user action before it is complete.']
    for action in actions:
        lines.append(str(action.get('message') or '').strip())
        command = str(action.get('command') or '').strip()
        if command:
            lines.append('Command: ' + command)
    lines.append('User confirmation remains pending; an AI agent must relay every '
                 "action faithfully in the user's language.")
    return '\n'.join(line for line in lines if line)


def _failure_report(error, runtime: str) -> dict:
    name = runtime if runtime in ('codex', 'claude', 'qwen') else 'codex'
    action = _repair_action(name, error)
    return {
        'schema_version': SCHEMA_VERSION,
        'version': __version__,
        'status': 'failed',
        'hooks': [_rowed(name, 'failed', False)],
        'required_actions': [action],
        'requires_user_action': True,
    }


def _exit_code(report: dict) -> int:
    if report.get('status') == 'failed':
        return EXIT_FAILED
    if report.get('requires_user_action'):
        return EXIT_ACTION_REQUIRED
    return EXIT_COMPLETED


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog='deepseek-team hooks refresh',
        description='Refresh already configured Codex/Claude/Qwen coordination hooks.')
    parser.add_argument('--runtime', choices=['auto', 'codex', 'claude', 'qwen', 'both', 'all'],
                        default='auto', help='Existing integrations to refresh; default auto.')
    parser.add_argument('--notify', action='store_true',
                        help='Always report restart and native /hooks review guidance.')
    parser.add_argument('--json', action='store_true', help='Print one JSON report to stdout.')
    return parser


def _print_text(report: dict) -> None:
    print(f"DeepSeek Team hook refresh: {report['status']} ({report['version']})")
    for row in report.get('hooks', []):
        change = 'changed' if row.get('changed') else 'unchanged'
        print(f"{_display(row['runtime'])}: {row['status']} ({change})")
    text = render_required_actions(report)
    if text:
        print(text)


def main(argv=None) -> int:
    args = _parser().parse_args(argv)
    try:
        report = refresh_installed(args.runtime, notify=args.notify)
    except Exception as error:
        report = _failure_report(error, args.runtime)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        _print_text(report)
    return _exit_code(report)


def _automatic_allowed(argv) -> bool:
    if not argv:
        return False
    if any(argument in ('-h', '--help', '--version') for argument in argv):
        return False
    first = argv[0]
    if not isinstance(first, str) or first.startswith('-'):
        return False
    return first in _CLI_AUTO_REFRESH


def refresh_for_cli(argv) -> None:
    """Best-effort first-use refresh without stdout or exit-status side effects."""
    argv = list(argv or [])
    if not _automatic_allowed(argv):
        return
    try:
        report = refresh_installed('auto', implicit=True)
        text = render_required_actions(report)
        if text:
            print(text, file=sys.stderr)
    except Exception as error:
        try:
            print('DeepSeek Team hook refresh could not run: ' + _detail(error)
                  + ' The requested command was not changed by this diagnostic.',
                  file=sys.stderr)
        except Exception:
            pass


if __name__ == '__main__':
    raise SystemExit(main())
