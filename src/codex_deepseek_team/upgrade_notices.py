"""Bounded in-session package-upgrade notices stored in runtime homes.

The native lifecycle hooks cannot infer which package version a long-running
session loaded.  This module records the version observed by each coordinator
session and, after a package replacement, gives the already-installed hook one
fresh-process refresh instruction to relay.  It only reads hook ownership and
writes its own private receipt; project and native configuration are untouched.
"""
from __future__ import annotations

import contextlib
import copy
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import stat
import sys
import time

from . import __version__, claude_config, config, dsh_config, worker


SCHEMA_VERSION = 1
RECEIPT_NAME = 'deepseek-team-upgrade-notices.json'
LOCK_NAME = '.deepseek-team-upgrade-notices.lock'
MAX_SESSIONS = 64
MAX_RECEIPT_BYTES = 64 * 1024
_IMPORTED_VERSION = __version__
_SUPPORTED_EVENTS = frozenset({
    'SessionStart', 'UserPromptSubmit', 'PreToolUse', 'Stop',
})
_CONTEXT_EVENTS = frozenset({'SessionStart', 'UserPromptSubmit'})
_SUBAGENT_KEYS = (
    'agent_id', 'agentId', 'subagent_id', 'subagentId', 'parent_session_id',
    'parent_tool_use_id',
)


class ReceiptError(Exception):
    """A private receipt could not be read or published safely."""


def _package_version():
    package = sys.modules.get(__package__)
    package_version = getattr(package, '__version__', __version__)
    if __version__ != _IMPORTED_VERSION:
        return str(__version__)
    return str(package_version)


def receipt_path(runtime):
    """Return the owner-controlled receipt location for one runtime home."""
    if runtime == 'codex':
        return worker.codex_home() / RECEIPT_NAME
    if runtime == 'claude':
        return Path(claude_config.claude_home()) / RECEIPT_NAME
    if runtime == 'qwen':
        return dsh_config.dsh_home() / RECEIPT_NAME
    raise ValueError('Unsupported coordinator runtime.')


def _is_native_subagent(payload, runtime):
    if any(payload.get(key) for key in _SUBAGENT_KEYS):
        return True
    if payload.get('subagent') is True or payload.get('is_subagent') is True:
        return True
    source = payload.get('source')
    if isinstance(source, str) and source.lower() in ('subagent', 'worker'):
        return True
    worker_marker = os.environ.get('DEEPSEEK_TEAM_WORKER')
    return isinstance(worker_marker, str) and worker_marker.lower() in ('1', 'true', 'yes')


def _owned_codex_integration():
    path = worker.codex_home() / 'hooks.json'
    try:
        raw = config.read_regular(path)
        data = json.loads(raw) if raw is not None else None
    except (config.ConfigError, OSError, ValueError, UnicodeError):
        return False
    if not isinstance(data, dict):
        return False
    hooks = data.get('hooks')
    if not isinstance(hooks, dict):
        return False
    recognizer = getattr(config, '_owned_codex_hook', None)
    if recognizer is None:
        recognizer = lambda handler: (isinstance(handler, dict) and
                                      handler.get('command') == config.CODEX_HOOK_COMMAND)
    for groups in hooks.values():
        if not isinstance(groups, list):
            continue
        for group in groups:
            handlers = group.get('hooks') if isinstance(group, dict) else None
            if isinstance(handlers, list) and any(recognizer(handler) for handler in handlers):
                return True
    return False


def _owned_claude_integration():
    path = Path(claude_config.claude_home()) / 'settings.json'
    try:
        raw = config.read_regular(path)
        data = json.loads(raw) if raw is not None else None
    except (config.ConfigError, OSError, ValueError, UnicodeError):
        return False
    if not isinstance(data, dict) or data.get('disableAllHooks') is True:
        return False
    hooks = data.get('hooks')
    if not isinstance(hooks, dict):
        return False
    recognizer = getattr(claude_config, '_owned', None)
    if recognizer is None:
        recognizer = lambda handler: (isinstance(handler, dict) and
                                      handler.get('command') == claude_config.HOOK_COMMAND)
    for groups in hooks.values():
        if not isinstance(groups, list):
            continue
        for group in groups:
            handlers = group.get('hooks') if isinstance(group, dict) else None
            if isinstance(handlers, list) and any(recognizer(handler) for handler in handlers):
                return True
    return False


def _owned_qwen_integration():
    try:
        return dsh_config.owned()
    except (config.ConfigError, OSError, ValueError):
        return False


def _has_owned_integration(runtime):
    if runtime == 'codex':
        return _owned_codex_integration()
    if runtime == 'qwen':
        return _owned_qwen_integration()
    return _owned_claude_integration()


def _session_key(session):
    digest = hashlib.sha256(('deepseek-team-session\0' + session).encode('utf-8')).hexdigest()
    return digest


def _validate_state(value):
    if not isinstance(value, dict):
        raise ReceiptError('receipt is not a JSON object')
    schema = value.get('schema_version')
    if isinstance(schema, bool) or schema != SCHEMA_VERSION:
        raise ReceiptError('receipt schema is not supported')
    if not isinstance(value.get('global_version'), str) or not value['global_version']:
        raise ReceiptError('receipt has no valid global package version')
    sessions = value.get('sessions', {})
    if not isinstance(sessions, dict):
        raise ReceiptError('receipt sessions are not an object')
    initial = value.get('initial_review_delivered', False)
    if not isinstance(initial, bool):
        raise ReceiptError('receipt initial-review marker is invalid')
    observed_at = value.get('global_observed_at')
    if observed_at is not None and (isinstance(observed_at, bool)
                                    or not isinstance(observed_at, (int, float))
                                    or not math.isfinite(observed_at)):
        raise ReceiptError('receipt global observation time is invalid')
    for key, row in sessions.items():
        if not isinstance(key, str) or not isinstance(row, dict):
            raise ReceiptError('receipt session record is invalid')
        if not isinstance(row.get('version'), str) or not row['version']:
            raise ReceiptError('receipt session has no package version')
        seen_at = row.get('observed_at')
        if seen_at is not None and (isinstance(seen_at, bool)
                                    or not isinstance(seen_at, (int, float))
                                    or not math.isfinite(seen_at)):
            raise ReceiptError('receipt session observation time is invalid')
        for field in ('delivered_version', 'previous_version'):
            if field in row and not isinstance(row[field], str):
                raise ReceiptError('receipt session version metadata is invalid')
    state = dict(value)
    state['sessions'] = dict(sessions)
    state['initial_review_delivered'] = initial
    return state


def _read_receipt(path):
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except FileNotFoundError:
        return None, None
    except OSError:
        raise ReceiptError('receipt is inaccessible or is a symbolic link') from None
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) & 0o077 or info.st_nlink != 1):
            raise ReceiptError('receipt is not an owner-only regular file')
        if info.st_size > MAX_RECEIPT_BYTES:
            raise ReceiptError('receipt is too large')
        with os.fdopen(descriptor, 'rb', closefd=False) as source:
            raw = source.read(MAX_RECEIPT_BYTES + 1)
    except OSError:
        raise ReceiptError('receipt could not be read safely') from None
    finally:
        os.close(descriptor)
    if len(raw) > MAX_RECEIPT_BYTES:
        raise ReceiptError('receipt is too large')
    try:
        value = json.loads(raw.decode('utf-8'))
    except (ValueError, UnicodeError):
        raise ReceiptError('receipt is malformed JSON') from None
    return _validate_state(value), raw


@contextlib.contextmanager
def _receipt_lock(path):
    lock_path = path.with_name(LOCK_NAME)
    try:
        descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
                             0o600)
    except OSError:
        raise ReceiptError('receipt lock is unsafe or inaccessible') from None
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) & 0o077):
            raise ReceiptError('receipt lock is not an owner-only regular file')
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _new_state(version, observed_at):
    return {
        'schema_version': SCHEMA_VERSION,
        'global_version': version,
        'global_observed_at': observed_at,
        'initial_review_delivered': False,
        'sessions': {},
    }


def _prune_sessions(state, version):
    sessions = state.get('sessions', {})
    if len(sessions) <= MAX_SESSIONS:
        return
    ordered = sorted(
        sessions.items(),
        key=lambda item: (item[1].get('version') == version,
                          float(item[1].get('observed_at') or 0)),
        reverse=True,
    )
    state['sessions'] = dict(ordered[:MAX_SESSIONS])


def _publish(path, state, previous):
    state = dict(state)
    state['schema_version'] = SCHEMA_VERSION
    _prune_sessions(state, state.get('global_version'))
    encoded = (json.dumps(state, sort_keys=True, separators=(',', ':')) + '\n').encode('utf-8')
    if len(encoded) > MAX_RECEIPT_BYTES:
        raise ReceiptError('receipt would exceed its safe size limit')
    try:
        config.atomic_write(path, encoded, previous, mode=0o600)
    except (config.ConfigError, OSError) as error:
        message = str(error).strip() or 'receipt could not be published atomically'
        raise ReceiptError(message) from None


def _update(path, update):
    """Apply one updater under the private lock, retrying a safe CAS race."""
    for _attempt in range(3):
        with _receipt_lock(path):
            state, previous = _read_receipt(path)
            updated = update(state)
            if updated is None:
                return
            try:
                _publish(path, updated, previous)
                return
            except ReceiptError as error:
                if 'changed concurrently' not in str(error):
                    raise
    raise ReceiptError('receipt changed concurrently; delivery remains pending')


def _observe(path, session_key, version, observed_at):
    def update(state):
        if state is None:
            state = _new_state(version, observed_at)
        current_global_at = float(state.get('global_observed_at') or 0)
        if observed_at >= current_global_at:
            state['global_version'] = version
            state['global_observed_at'] = observed_at
        row = state['sessions'].get(session_key)
        if row is None or observed_at >= float(row.get('observed_at') or 0):
            state['sessions'][session_key] = {
                'version': version,
                'delivered_version': version,
                'observed_at': observed_at,
            }
        state['initial_review_delivered'] = True
        return state
    _update(path, update)


def _best_effort_observe(path, session_key, version, observed_at):
    try:
        _observe(path, session_key, version, observed_at)
    except ReceiptError:
        pass


def _action_text(runtime):
    return (
        'Run `deepseek-team hooks refresh --runtime ' + runtime + ' --notify --json` '
        'now in a fresh process, exactly as a post-upgrade check. Relay every '
        '`required_actions` entry faithfully to the user, including restarting or '
        'starting a new session and reviewing DeepSeek Team in `/hooks`. Native hook '
        'review/trust remains pending; do not approve native trust, restart the '
        'runtime, or grant any confirmation from this hook. Report any refresh failure '
        'without claiming the user actions are complete.'
    )


def _notice_text(kind, runtime, version, previous_version):
    if kind == 'upgrade':
        lead = ('DeepSeek Team changed from ' + str(previous_version) + ' to ' + version
                + ' while this coordinator session was running. Refresh the freshly '
                  'installed package before ending this turn.')
    elif kind == 'unknown-session':
        lead = ('DeepSeek Team is running package version ' + version + ', but this '
                'coordinator session has no prior package-version observation. Another '
                'session may have observed it first; verify this session with the freshly '
                'installed package before continuing.')
    elif kind == 'unsafe-receipt':
        lead = ('DeepSeek Team could not safely read its private in-session upgrade receipt '
                'for package version ' + version + '. The existing receipt was preserved. '
                'Run the fresh-package check once without modifying the receipt, hook '
                'configuration, or project files.')
    else:
        lead = ('DeepSeek Team found no prior in-session package receipt for version '
                + version + '. On this first installation or direct package upgrade, verify '
                  'the freshly installed package and its existing integration before ending '
                  'this turn.')
    return lead + '\n\n' + _action_text(runtime)


def _make_notice(kind, payload, runtime, version, previous_version, receipt_error=None):
    session = str(payload.get('session_id') or '')
    text = _notice_text(kind, runtime, version, previous_version)
    notice = {
        'schema_version': SCHEMA_VERSION,
        'kind': kind,
        'runtime': runtime,
        'event': str(payload.get('hook_event_name') or ''),
        'session_key': _session_key(session),
        'version': version,
        'previous_version': previous_version,
        'observed_at': time.time(),
        'receipt_path': str(receipt_path(runtime)),
        'message': text,
    }
    if receipt_error:
        notice['receipt_error'] = receipt_error
    return notice


def prepare_notice(payload, runtime) -> dict | None:
    """Observe a healthy lifecycle or return one pending notice for this event.

    A lifecycle event seeds a new session without producing noise.  A direct
    PreToolUse/Stop with no session observation remains conservative even when a
    different session already recorded the current global package version.
    """
    if not isinstance(payload, dict) or runtime not in ('codex', 'claude', 'qwen'):
        return None
    event = payload.get('hook_event_name')
    if event not in _SUPPORTED_EVENTS or _is_native_subagent(payload, runtime):
        return None
    session = payload.get('session_id')
    if not isinstance(session, str) or not session.strip():
        return None
    if not _has_owned_integration(runtime):
        return None
    version = _package_version()
    if not version:
        return None
    path = receipt_path(runtime)
    key = _session_key(session)
    observed_at = time.time()
    try:
        state, _raw = _read_receipt(path)
    except ReceiptError as error:
        return _make_notice('unsafe-receipt', payload, runtime, version, None, str(error))

    if state is None:
        if event in _CONTEXT_EVENTS:
            _best_effort_observe(path, key, version, observed_at)
            return None
        return _make_notice('initial', payload, runtime, version, None)

    row = state['sessions'].get(key)
    if event in _CONTEXT_EVENTS:
        if row is None:
            _best_effort_observe(path, key, version, observed_at)
            return None
        if row['version'] == version:
            if state.get('global_version') != version:
                _best_effort_observe(path, key, version, observed_at)
            return None
        return _make_notice('upgrade', payload, runtime, version, row['version'])

    if row is None:
        kind = ('unknown-session' if state.get('initial_review_delivered')
                else 'initial')
        return _make_notice(kind, payload, runtime, version, None)
    if row['version'] != version:
        return _make_notice('upgrade', payload, runtime, version, row['version'])
    return None


def _prepend(target, key, text):
    existing = target.get(key)
    if isinstance(existing, str) and existing:
        if existing == text or existing.startswith(text + '\n\n'):
            return
        target[key] = text + '\n\n' + existing
    else:
        target[key] = text


def _annotate_stop(result, payload, text):
    existing_reason = result.get('reason')
    if result.get('decision') == 'block':
        _prepend(result, 'reason', text)
        return result
    if payload.get('stop_hook_active'):
        _prepend(result, 'systemMessage', text)
        result.setdefault('continue', True)
        return result
    reason = existing_reason if isinstance(existing_reason, str) and existing_reason else ''
    result['decision'] = 'block'
    result['reason'] = reason
    _prepend(result, 'reason', text)
    result.pop('continue', None)
    return result


def annotate_result(result, payload, runtime, notice) -> dict:
    """Prepend a notice to the native response without changing gate decisions."""
    if not isinstance(result, dict):
        result = {}
    else:
        result = copy.deepcopy(result)
    if not isinstance(notice, dict):
        return result
    text = notice.get('message')
    if not isinstance(text, str) or not text:
        return result
    event = payload.get('hook_event_name') if isinstance(payload, dict) else None
    if event is None:
        event = notice.get('event')
    if event in _CONTEXT_EVENTS:
        specific = result.setdefault('hookSpecificOutput', {})
        if not isinstance(specific, dict):
            specific = result['hookSpecificOutput'] = {}
        specific.setdefault('hookEventName', event)
        _prepend(specific, 'additionalContext', text)
        return result
    if event == 'PreToolUse':
        specific = result.setdefault('hookSpecificOutput', {})
        if not isinstance(specific, dict):
            specific = result['hookSpecificOutput'] = {}
        specific.setdefault('hookEventName', 'PreToolUse')
        if specific.get('permissionDecision') == 'deny':
            _prepend(specific, 'permissionDecisionReason', text)
        else:
            _prepend(specific, 'additionalContext', text)
        return result
    if event == 'Stop':
        return _annotate_stop(result, payload if isinstance(payload, dict) else {}, text)
    return result


def mark_delivered(notice) -> None:
    """Record delivery after the one native JSON response has been flushed."""
    if notice is None:
        return
    if not isinstance(notice, dict):
        raise ReceiptError('notice is not a mapping')
    runtime = notice.get('runtime')
    if runtime not in ('codex', 'claude', 'qwen'):
        raise ReceiptError('notice has no supported runtime')
    version = notice.get('version')
    key = notice.get('session_key')
    if not isinstance(version, str) or not version:
        raise ReceiptError('notice has no package version')
    if not isinstance(key, str) or len(key) != 64:
        raise ReceiptError('notice has no valid session key')
    try:
        observed_at = float(notice.get('observed_at'))
    except (TypeError, ValueError):
        raise ReceiptError('notice has no valid observation time') from None
    previous_version = notice.get('previous_version')
    if previous_version is not None and not isinstance(previous_version, str):
        previous_version = None
    path = receipt_path(runtime)

    def update(state):
        if state is None:
            state = _new_state(version, observed_at)
        current_global_at = float(state.get('global_observed_at') or 0)
        if observed_at >= current_global_at:
            state['global_version'] = version
            state['global_observed_at'] = observed_at
        row = state['sessions'].get(key)
        if row is None or observed_at >= float(row.get('observed_at') or 0):
            record = {
                'version': version,
                'delivered_version': version,
                'observed_at': observed_at,
            }
            if previous_version is not None:
                record['previous_version'] = previous_version
            state['sessions'][key] = record
        state['initial_review_delivered'] = True
        return state

    _update(path, update)
