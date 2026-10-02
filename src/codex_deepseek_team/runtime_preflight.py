"""Namespace-verified runtime readiness before provider credentials.

Host-only availability is not readiness: a launcher that works on the host may
fail inside the sparse managed worker namespace when a sibling target or helper
is not mounted. Every candidate is therefore probed with ``--version`` and the
required capability help inside the caller-prepared namespace before it can be
selected. These probes are local CLI invocations only; no provider request is
made and no credential is read here.

``context_factory(binary, runtime)`` belongs to the coordinator-owned caller and
returns ``(layout, environment)``: the full namespace argv prefix (ending before
the ``--`` payload separator) and the prepared environment for that candidate.

Probe output flows through the accepted :func:`check_evidence.run_checks`
boundary: each stream is retained in a private temporary evidence directory,
capped at ``MAX_OUTPUT_BYTES``, and the whole process group is cleaned up when a
probe expires. A caller-supplied ``time.monotonic`` deadline is shared by
candidate traversal and both probes of every candidate; its expiry is a code-124
budget error that stops fallback immediately. Without a supplied deadline the
historical 15-second local ceiling applies and a hung probe stays an ordinary
code-78 local timeout.
"""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import shlex
import shutil
import tempfile
import time

from .check_evidence import CheckEvidenceError, MAX_OUTPUT_BYTES, run_checks
from .development import DevelopmentError
from .sandbox import SandboxError
from .workspace import WorkspaceError


PROBE_TIMEOUT = 15.0
EXCERPT_LIMIT = 240
SYSTEM_DIRECTORIES = ('/usr/local/bin', '/usr/bin')
BUDGET_EXIT_CODE = 124

RUNTIME_LABELS = {'codex': 'Codex', 'claude': 'Claude Code'}

# Coordinator exceptions carrying an explicitly safe, human-written
# ``.message``. Anything else that escapes ``context_factory`` (including
# ``OSError``) is reported with a fixed generic explanation so raw exception
# text, which may embed untrusted program output or caller secrets, is never
# echoed into the operator-facing message.
SAFE_CONTEXT_ERRORS = (CheckEvidenceError, DevelopmentError, SandboxError, WorkspaceError)

# Existing capability requirements, unchanged: what the managed runner already
# required from `exec --help` (Codex) and `--help` (Claude Code) output before
# accepting a runtime for isolated non-interactive work.
CAPABILITIES = {
    'codex': (
        ('exec', '--help'),
        ('--strict-config', '--sandbox', '--ephemeral', '--json', '--ignore-rules',
         'danger-full-access'),
    ),
    'claude': (
        ('--help',),
        ('--bare', '--tools', '--allowedTools', '--disallowedTools', '--permission-mode',
         'dontAsk', '--disable-slash-commands', '--setting-sources', '--strict-mcp-config',
         '--mcp-config', '--no-session-persistence', '--output-format'),
    ),
}


class RuntimePreflightError(Exception):
    def __init__(self, message: str, code: int = 78):
        self.message, self.code = message, code
        super().__init__(message)


@dataclass
class RuntimeSelection:
    runtime: str
    binary: str
    layout: list[str]
    environment: dict[str, str]


def _excerpt(text: str, limit: int = EXCERPT_LIMIT) -> str:
    collapsed = ' '.join((text or '').split())
    if len(collapsed) <= limit:
        return collapsed
    head = (limit - 1) // 2
    tail = limit - 1 - head
    return collapsed[:head] + '…' + collapsed[-tail:]


def _budget_error(label: str, binary: str | None = None,
                  probe: str | None = None) -> RuntimePreflightError:
    if binary is None:
        where = 'before the worker namespace could be prepared'
    elif probe:
        where = f'while probing {binary} (`{probe}`)'
    else:
        where = f'while probing {binary}'
    return RuntimePreflightError(
        f'{label} runtime readiness exceeded its shared time budget {where}; '
        'no provider request was made and no fallback was attempted.', BUDGET_EXIT_CODE)


def _check_budget(deadline, label: str) -> None:
    if deadline is not None and time.monotonic() >= deadline:
        raise _budget_error(label)


def _namespace_arguments(args: list[str]) -> list[str]:
    """Prefix for ``run_checks``, which appends its own ``--`` payload separator.

    The managed boundary is ``layout -- /bin/sh -lc COMMAND``; an empty caller
    prefix gets ``/usr/bin/env`` in the executable slot.
    """
    return list(args) if args else ['/usr/bin/env']


def _read_evidence(directory: Path, name) -> str:
    if not isinstance(name, str) or not name or Path(name).name != name:
        return ''
    try:
        payload = (directory / name).read_bytes()
    except OSError:
        return ''
    return payload[:MAX_OUTPUT_BYTES].decode('utf-8', 'replace')


def _run_probe(binary: str, runtime: str, args, env, extra: list[str],
               deadline: float, *, budget_limited: bool) -> str:
    label = RUNTIME_LABELS[runtime]
    display = ' '.join(extra)
    if time.monotonic() >= deadline:
        if budget_limited:
            raise _budget_error(label, binary, display)
        raise RuntimePreflightError(
            f'{label} namespace probe timed out after {PROBE_TIMEOUT:g}s before '
            f'`{display}` could start: {binary}. No provider request was made.', 78)
    command = shlex.join([binary, *extra])
    try:
        directory = Path(tempfile.mkdtemp(prefix='dst-runtime-probe-'))
    except OSError:
        raise RuntimePreflightError(
            f'{label} could not prepare a private probe directory for {binary} '
            f'(probe `{display}`); no credential was read.', 78) from None
    try:
        try:
            rows = run_checks(_namespace_arguments(args), env, [command],
                              timeout=PROBE_TIMEOUT, deadline=deadline,
                              directory=directory, phase='probe')
        except CheckEvidenceError:
            raise RuntimePreflightError(
                f'{label} probe evidence could not be captured safely for {binary} '
                f'(probe `{display}`); no credential was read.', 78) from None
        row = rows[0] if rows else {}
        if row.get('launch_failed'):
            raise RuntimePreflightError(
                f'{label} could not be launched in the worker namespace: {binary} '
                f'(probe `{display}`). Check the executable and its prepared mounts; '
                'no fallback occurred and no credential was read.', 78)
        output = (_read_evidence(directory, row.get('stdout_path'))
                  + _read_evidence(directory, row.get('stderr_path')))
        if row.get('timed_out'):
            if budget_limited:
                raise _budget_error(label, binary, display)
            raise RuntimePreflightError(
                f'{label} namespace probe timed out after {PROBE_TIMEOUT:g}s running '
                f'`{display}` for {binary}. Check the launcher and its prepared '
                'mounts; no credential was read.', 78)
        code = row.get('exit_code')
        if code:
            excerpt = _excerpt(output)
            detail = f': {excerpt}' if excerpt else '.'
            raise RuntimePreflightError(
                f'{label} failed the namespace probe: {binary} `{display}` '
                f'exited {code}{detail}', 78)
        return output
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def probe_runtime(binary: str, runtime: str, args: list[str],
                  env: dict[str, str], *, deadline: float | None = None) -> str:
    """Probe ``--version`` and the required capability help in one namespace.

    Returns the version text. Raises ``RuntimePreflightError`` (code 78) when the
    executable cannot run in the namespace, exceeds the finite local ceiling, or
    lacks required CLI capabilities. ``args`` is the caller's namespace argv
    prefix; an empty prefix runs the executable directly.

    ``deadline`` is an optional ``time.monotonic()`` timestamp shared by both
    probes; its expiry is a code-124 budget error. Without it the local
    ``PROBE_TIMEOUT`` ceiling applies to the pair of probes and a hang remains a
    code-78 local timeout.
    """
    if runtime not in CAPABILITIES:
        raise RuntimePreflightError(f'Unsupported worker runtime: {runtime}.', 64)
    if not isinstance(binary, str) or not binary:
        raise RuntimePreflightError('Runtime preflight requires an executable path.', 78)
    if deadline is not None:
        try:
            deadline = float(deadline)
        except (TypeError, ValueError):
            raise RuntimePreflightError(
                'Runtime preflight deadline must be a time.monotonic timestamp.', 64) from None
    args = list(args or [])
    env = dict(env or {})
    label = RUNTIME_LABELS[runtime]
    local_ceiling = time.monotonic() + PROBE_TIMEOUT
    if deadline is None:
        effective, budget_limited = local_ceiling, False
    else:
        effective = min(deadline, local_ceiling)
        budget_limited = deadline <= local_ceiling
    version = _run_probe(binary, runtime, args, env, ['--version'], effective,
                         budget_limited=budget_limited)
    help_args, required = CAPABILITIES[runtime]
    help_text = _run_probe(binary, runtime, args, env, list(help_args), effective,
                           budget_limited=budget_limited)
    missing = [flag for flag in required if flag not in help_text]
    if missing:
        raise RuntimePreflightError(
            f'{label} capability check failed for {binary}: missing '
            + ', '.join(missing) + f' in `{" ".join(help_args)}` output. '
            'Update the runtime; no fallback occurred.', 78)
    return version.strip()


def _has_separator(value: str) -> bool:
    return os.sep in value or (os.altsep is not None and os.altsep in value)


def _explicit_candidates(value: str) -> list[str]:
    found = shutil.which(value)
    return [found] if found else []


def _default_candidates(value: str) -> list[str]:
    """Executable candidates in PATH order, then the system directories.

    Candidates are deduplicated by resolved identity so a symlinked or repeated
    entry is probed only once; the first PATH occurrence keeps its position.
    """
    directories: list[str] = []
    for entry in os.environ.get('PATH', '').split(os.pathsep):
        if entry and os.path.isabs(entry):
            directories.append(entry)
    directories.extend(SYSTEM_DIRECTORIES)
    candidates: list[str] = []
    seen: set[str] = set()
    for directory in directories:
        candidate = os.path.join(directory, value)
        try:
            key = os.path.realpath(candidate)
        except OSError:
            key = candidate
        if key in seen:
            continue
        seen.add(key)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            candidates.append(candidate)
    return candidates


def _pin_label(runtime: str, explicit: bool) -> str:
    if explicit:
        return f'explicit --{runtime} pin'
    return 'pinned executable path'


def _factory_reason(error: BaseException) -> str:
    """Summarize only coordinator errors with an explicitly safe ``.message``."""
    if isinstance(error, SAFE_CONTEXT_ERRORS):
        message = getattr(error, 'message', None)
        if isinstance(message, str) and message.strip():
            return _excerpt(message)
    return 'the worker namespace context could not be prepared (details withheld)'


def select_runtime(requested: str, codex: str, claude: str,
                   context_factory, *, explicit_codex: bool = False,
                   explicit_claude: bool = False,
                   deadline: float | None = None) -> RuntimeSelection:
    """Select the first runtime that really runs in its prepared namespace.

    ``explicit_codex``/``explicit_claude`` must reflect whether the CLI flag was
    present, even when its value is the default name: an explicit executable is
    probed alone and a failure is fatal, never silently replaced by PATH, system
    or cross-runtime fallback. Default names enumerate deduplicated candidates in
    PATH order, then ``/usr/local/bin`` and ``/usr/bin``. ``runtime='auto'`` may
    try Codex and then Claude, but only past runtimes that were not explicitly
    pinned.

    ``deadline`` is an optional ``time.monotonic()`` timestamp shared by the
    whole traversal, both probes of each candidate and the caller's context
    factory. When it expires, selection fails with code 124 and no further
    candidate or runtime is attempted; without it every probe keeps its local
    ``PROBE_TIMEOUT`` ceiling (code 78) and normal fallback applies.
    """
    if requested not in ('auto', 'codex', 'claude'):
        raise RuntimePreflightError(f'Unsupported worker runtime: {requested}.', 64)
    if deadline is not None:
        try:
            deadline = float(deadline)
        except (TypeError, ValueError):
            raise RuntimePreflightError(
                'Runtime preflight deadline must be a time.monotonic timestamp.', 64) from None
    if requested == 'codex':
        plans = [('codex', codex, explicit_codex)]
    elif requested == 'claude':
        plans = [('claude', claude, explicit_claude)]
    else:
        plans = [('codex', codex, explicit_codex), ('claude', claude, explicit_claude)]

    failures: list[str] = []
    for runtime, value, explicit in plans:
        label = RUNTIME_LABELS[runtime]
        pin = _pin_label(runtime, explicit)
        _check_budget(deadline, label)
        if not isinstance(value, str) or not value:
            if explicit:
                raise RuntimePreflightError(
                    f'{label} executable is unavailable: the {pin} is empty. '
                    'The explicit pin is preserved; no fallback was attempted.', 78)
            failures.append(f'{label} executable is unavailable.')
            continue
        pinned = explicit or _has_separator(value)
        candidates = _explicit_candidates(value) if pinned else _default_candidates(value)
        if not candidates:
            if pinned:
                raise RuntimePreflightError(
                    f'{label} executable is unavailable: the {pin} {value!r} was not found. '
                    'The pin is preserved; no fallback was attempted.', 78)
            failures.append(
                f'{label} executable is unavailable on PATH or in the system directories.')
            continue
        for binary in candidates:
            _check_budget(deadline, label)
            try:
                prepared = context_factory(binary, runtime)
                layout, environment = prepared
                layout = list(layout)
                environment = dict(environment)
            except Exception as error:  # noqa: BLE001 - the caller factory is untrusted
                _check_budget(deadline, label)
                reason = _factory_reason(error)
                if pinned:
                    raise RuntimePreflightError(
                        f'{label} runtime preparation failed for {binary}: {reason}. '
                        f'The {pin} is preserved; no fallback was attempted.', 78) from None
                failures.append(f'{label} preparation failed for {binary}: {reason}')
                continue
            try:
                probe_runtime(binary, runtime, layout, environment, deadline=deadline)
            except RuntimePreflightError as error:
                if error.code == BUDGET_EXIT_CODE:
                    raise
                if pinned:
                    raise RuntimePreflightError(
                        f'{error.message} The {pin} is preserved; no fallback was attempted.',
                        error.code) from None
                failures.append(error.message)
                continue
            return RuntimeSelection(runtime=runtime, binary=binary, layout=layout,
                                    environment=environment)

    bounded = ' '.join(failures[:4])
    if len(failures) > 4:
        bounded += f' (+{len(failures) - 4} more failures)'
    if not bounded:
        bounded = 'no usable runtime candidate was found.'
    raise RuntimePreflightError(
        'Runtime readiness failed before the provider credential: ' + bounded, 78)
