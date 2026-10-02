"""Bounded diagnostic evidence capture and neutral check classification.

This module is provider-free: it only runs already-built namespace arguments
through the same ``args -- /bin/sh -lc COMMAND`` boundary the managed runner
uses.  It never reads credentials, authorization state, the full environment or
raw runtime model output.

``run_checks`` returns legacy ``{command, exit_code}`` rows when no directory is
given.  With a directory it additionally publishes one owner-only, atomically
renamed diagnostic file per stream and returns rows with the schema in
``EVIDENCE_ROW_KEYS``:

* ``phase`` - caller supplied phase label (``baseline``/``post``).
* ``stdout_path``/``stderr_path`` - unique relative file names inside the
  evidence directory, or ``None`` when the command did not run / could not be
  launched (no evidence is claimed in that case).
* ``stdout_truncated``/``stderr_truncated`` - at most ``MAX_OUTPUT_BYTES`` of
  each stream is retained; larger output is drained and discarded.
* ``timed_out`` - the command was killed after the per-command timeout or the
  remaining job deadline; ``exit_code`` is ``TIMEOUT_EXIT_CODE`` (124).
* ``launch_failed`` - the namespace launcher itself could not start; the
  command was not executed and ``exit_code`` is ``LAUNCH_FAILURE_EXIT_CODE``
  (125).
* ``stdout_sha256``/``stderr_sha256`` - digests of the retained bytes, used to
  compare baseline and post failures without publishing content.

Known sensitive ``redact_values`` are replaced with ``[REDACTED]`` in published
evidence, including values split across read chunks, values straddling the
retention limit and trailing partial values at end of stream.  Publication
failures raise ``CheckEvidenceError`` instead of silently returning rows that
claim evidence was stored.

``classify_checks`` is a neutral operational classifier.  It returns exactly
``{'kind', 'cause'}`` where ``kind`` is ``None`` (success), ``environment`` or
``verification``.  Timeout/launch/probe failures are environment evidence.
Ordinary post failures are verification with cause ``unknown``,
``baseline_failure`` or ``regression_candidate``; a matching exit code alone
never proves a matching cause, and no result grades worker quality.
"""
from __future__ import annotations

import hashlib
import os
import re
import selectors
import signal
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

MAX_OUTPUT_BYTES = 65536
READ_CHUNK_SIZE = 65536
TIMEOUT_EXIT_CODE = 124
LAUNCH_FAILURE_EXIT_CODE = 125

LEGACY_ROW_KEYS = ('command', 'exit_code')
EVIDENCE_ROW_KEYS = ('command', 'exit_code', 'phase', 'stdout_path', 'stderr_path',
                     'stdout_truncated', 'stderr_truncated', 'timed_out',
                     'launch_failed', 'stdout_sha256', 'stderr_sha256')

_REDACTED = b'[REDACTED]'
_SAFE_PHASE = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z')


class CheckEvidenceError(Exception):
    """Check evidence could not be prepared or published safely."""

    def __init__(self, message: str, code: int = 78):
        super().__init__(message)
        self.message = message
        self.code = code


def _normalize_secrets(redact_values) -> tuple[bytes, ...]:
    if redact_values is None:
        return ()
    if isinstance(redact_values, (str, bytes)):
        redact_values = (redact_values,)
    secrets = []
    seen = set()
    for value in redact_values:
        if value is None:
            continue
        if isinstance(value, bytes):
            payload = value
        elif isinstance(value, str):
            payload = value.encode('utf-8', 'surrogateescape')
        else:
            continue
        if not payload or payload in seen:
            continue
        seen.add(payload)
        secrets.append(payload)
    secrets.sort(key=len, reverse=True)
    return tuple(secrets)


def _marker_for(secrets: tuple[bytes, ...]) -> bytes:
    if any(secret in _REDACTED for secret in secrets):
        return b''
    return _REDACTED


class _RetainedStream:
    """Redact and retain at most ``limit`` bytes of one process stream."""

    def __init__(self, secrets: tuple[bytes, ...], limit: int = MAX_OUTPUT_BYTES):
        self._secrets = secrets
        self._max_length = max((len(secret) for secret in secrets), default=0)
        self._limit = limit
        self._marker = _marker_for(secrets)
        self._tail = b''
        self._buffer = bytearray()
        self._pattern = (re.compile(b'|'.join(re.escape(secret) for secret in secrets))
                         if secrets else None)
        self.truncated = False

    @property
    def buffer(self) -> bytes:
        return bytes(self._buffer)

    def feed(self, chunk: bytes) -> None:
        if not chunk:
            return
        if not self._secrets:
            self._emit(chunk)
            return
        data = self._tail + chunk
        if len(data) <= self._max_length - 1:
            self._tail = data
            return
        payload, consumed = self._redact_prefix(data)
        self._tail = data[consumed:]
        self._emit(self._scrub(payload))

    def flush(self) -> None:
        data = self._tail
        self._tail = b''
        if not data:
            return
        if not self._secrets:
            self._emit(data)
            return
        trim = 0
        for secret in self._secrets:
            limit = min(len(secret) - 1, len(data))
            for length in range(limit, 0, -1):
                if data.endswith(secret[:length]):
                    trim = max(trim, length)
                    break
        if trim:
            data = data[:-trim] + self._marker
        data = self._trim_partial_suffix(self._scrub(data))
        self._emit(data)

    def _redact_prefix(self, data: bytes) -> tuple[bytes, int]:
        cut = len(data) - (self._max_length - 1)
        if cut <= 0:
            return b'', 0
        parts = []
        position = 0
        consumed = cut
        for match in self._pattern.finditer(data):
            if match.start() >= cut:
                break
            parts.append(data[position:match.start()])
            parts.append(self._marker)
            position = match.end()
            consumed = max(consumed, match.end())
        parts.append(data[position:cut])
        return b''.join(parts), consumed

    def _scrub(self, payload: bytes) -> bytes:
        if not payload or not self._secrets:
            return payload
        for _ in range(8):
            if not any(secret in payload for secret in self._secrets):
                return payload
            payload = self._pattern.sub(self._marker, payload)
        if any(secret in payload for secret in self._secrets):
            return b''
        return payload

    def _emit(self, payload: bytes) -> None:
        if not payload or self.truncated:
            return
        if self._secrets and self._buffer and self._max_length > 1:
            overlap = min(self._max_length - 1, len(self._buffer))
            seam = bytes(self._buffer[-overlap:]) + payload
            scrubbed = self._scrub(seam)
            if scrubbed != seam:
                del self._buffer[len(self._buffer) - overlap:]
                payload = scrubbed
            if not payload:
                return
        space = self._limit - len(self._buffer)
        if space <= 0:
            self.truncated = True
            return
        if len(payload) <= space:
            self._buffer.extend(payload)
            return
        candidate = bytes(self._buffer) + payload[:space]
        candidate = self._trim_partial_suffix(candidate)
        self._buffer = bytearray(candidate)
        self.truncated = True

    def _trim_partial_suffix(self, data: bytes) -> bytes:
        while data:
            removed = False
            for secret in self._secrets:
                limit = min(len(secret) - 1, len(data))
                for length in range(limit, 0, -1):
                    if data.endswith(secret[:length]):
                        data = data[:-length]
                        removed = True
                        break
                if removed:
                    break
            if not removed:
                break
        return data


@dataclass
class _Capture:
    exit_code: int
    stdout: bytes
    stderr: bytes
    stdout_truncated: bool
    stderr_truncated: bool
    timed_out: bool
    launch_failed: bool


def run_checks(args: list[str], env: dict[str, str], commands: list[str],
               timeout: float = 120, *, deadline: float | None = None,
               directory: Path | None = None, phase: str = 'post',
               redact_values=()) -> list[dict]:
    """Run declared checks in order; stop at the first failed command.

    ``args`` is the already-built namespace command prefix, so every check runs
    as ``[*args, '--', '/bin/sh', '-lc', command]``.  ``deadline`` is a
    ``time.monotonic()`` timestamp for the whole job; the effective per-command
    ceiling is the smaller of ``timeout`` and the remaining deadline.
    """
    command_list = [commands] if isinstance(commands, str) else list(commands or ())
    secrets = _normalize_secrets(redact_values)
    target = None
    run_id = None
    if directory is not None:
        if not isinstance(phase, str) or not _SAFE_PHASE.fullmatch(phase):
            raise CheckEvidenceError('Check evidence phase must be a short safe name.')
        target = Path(directory)
        run_id = uuid.uuid4().hex[:12]
        if command_list:
            _prepare_directory(target)
    rows = []
    for index, command in enumerate(command_list):
        remaining = timeout if deadline is None else min(timeout, deadline - time.monotonic())
        if remaining <= 0:
            capture = _Capture(TIMEOUT_EXIT_CODE, b'', b'', False, False, True, False)
            rows.append(_evidence_row(command, phase, run_id, index, capture) if target
                        else {'command': command, 'exit_code': capture.exit_code})
            break
        capture = _execute(args, env, command, remaining, secrets)
        if target is None:
            row = {'command': command, 'exit_code': capture.exit_code}
        else:
            row = _publish_row(target, phase, run_id, index, command, capture)
        rows.append(row)
        if row['exit_code'] != 0:
            break
    return rows


def _execute(args: list[str], env: dict[str, str], command: str, remaining: float,
             secrets: tuple[bytes, ...]) -> _Capture:
    argv = [*args, '--', '/bin/sh', '-lc', command]
    started = time.monotonic()
    limit_at = started + max(remaining, 0.0)
    try:
        process = subprocess.Popen(argv, env=env, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   start_new_session=True)
    except (OSError, subprocess.SubprocessError):
        return _Capture(LAUNCH_FAILURE_EXIT_CODE, b'', b'', False, False, False, True)
    stdout_stream = _RetainedStream(secrets)
    stderr_stream = _RetainedStream(secrets)
    selector = selectors.DefaultSelector()
    open_fds = set()
    timed_out = False
    try:
        for handle, stream in ((process.stdout, stdout_stream), (process.stderr, stderr_stream)):
            descriptor = handle.fileno()
            os.set_blocking(descriptor, False)
            selector.register(descriptor, selectors.EVENT_READ, stream)
            open_fds.add(descriptor)
        while open_fds:
            left = limit_at - time.monotonic()
            if left <= 0:
                timed_out = True
                break
            for key, _ in selector.select(timeout=left):
                if key.fd not in open_fds:
                    continue
                try:
                    chunk = os.read(key.fd, READ_CHUNK_SIZE)
                except (BlockingIOError, InterruptedError):
                    continue
                except OSError:
                    chunk = b''
                if not chunk:
                    open_fds.discard(key.fd)
                    try:
                        selector.unregister(key.fd)
                    except (KeyError, ValueError):
                        pass
                    continue
                key.data.feed(chunk)
        if not timed_out:
            left = limit_at - time.monotonic()
            if left <= 0:
                timed_out = True
            elif process.poll() is None:
                try:
                    process.wait(timeout=left)
                except subprocess.TimeoutExpired:
                    timed_out = True
        if timed_out:
            _kill_process_group(process)
            _drain(selector, open_fds, seconds=1.0)
            exit_code = TIMEOUT_EXIT_CODE
        else:
            exit_code = process.returncode
            if exit_code is None:
                exit_code = process.wait()
    except BaseException:
        _kill_process_group(process)
        raise
    finally:
        selector.close()
        for handle in (process.stdout, process.stderr):
            if handle is not None:
                try:
                    handle.close()
                except OSError:
                    pass
    stdout_stream.flush()
    stderr_stream.flush()
    return _Capture(exit_code, stdout_stream.buffer, stderr_stream.buffer,
                    stdout_stream.truncated, stderr_stream.truncated, timed_out, False)


def _drain(selector, open_fds: set, *, seconds: float) -> set:
    end = time.monotonic() + seconds
    while open_fds:
        left = end - time.monotonic()
        if left <= 0:
            break
        for key, _ in selector.select(timeout=left):
            if key.fd not in open_fds:
                continue
            try:
                chunk = os.read(key.fd, READ_CHUNK_SIZE)
            except (BlockingIOError, InterruptedError):
                continue
            except OSError:
                chunk = b''
            if not chunk:
                open_fds.discard(key.fd)
            else:
                key.data.feed(chunk)
    return open_fds


def _kill_process_group(process) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except OSError:
        pass
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
        except OSError:
            pass
        try:
            process.wait(timeout=5)
        except (subprocess.TimeoutExpired, OSError):
            pass


def _prepare_directory(directory: Path) -> None:
    try:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(directory, 0o700)
    except OSError as error:
        raise CheckEvidenceError(
            f'Check evidence directory is unavailable: {error}') from None


def _publish_row(directory: Path, phase: str, run_id: str, index: int,
                 command: str, capture: _Capture) -> dict:
    if capture.launch_failed:
        return _evidence_row(command, phase, run_id, index, capture)
    stdout_name = f'{phase}-{run_id}-{index:03d}.stdout'
    stderr_name = f'{phase}-{run_id}-{index:03d}.stderr'
    _publish(directory, stdout_name, capture.stdout)
    _publish(directory, stderr_name, capture.stderr)
    return _evidence_row(command, phase, run_id, index, capture,
                         stdout_path=stdout_name, stderr_path=stderr_name)


def _publish(directory: Path, name: str, payload: bytes) -> None:
    temp_path = directory / f'.{name}.tmp-{uuid.uuid4().hex[:8]}'
    descriptor = None
    try:
        descriptor = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        offset = 0
        while offset < len(payload):
            offset += os.write(descriptor, payload[offset:])
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.replace(temp_path, directory / name)
    except OSError as error:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise CheckEvidenceError(
            f'Check evidence could not be stored atomically: {error}') from None
    _fsync_directory(directory)


def _fsync_directory(directory: Path) -> None:
    try:
        descriptor = os.open(directory, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _evidence_row(command: str, phase: str, run_id: str, index: int,
                  capture: _Capture, stdout_path=None, stderr_path=None) -> dict:
    return {
        'command': command,
        'exit_code': capture.exit_code,
        'phase': phase,
        'stdout_path': stdout_path,
        'stderr_path': stderr_path,
        'stdout_truncated': capture.stdout_truncated,
        'stderr_truncated': capture.stderr_truncated,
        'timed_out': capture.timed_out,
        'launch_failed': capture.launch_failed,
        'stdout_sha256': (hashlib.sha256(capture.stdout).hexdigest()
                          if stdout_path is not None else None),
        'stderr_sha256': (hashlib.sha256(capture.stderr).hexdigest()
                          if stderr_path is not None else None),
    }


def classify_checks(baseline: list[dict], post: list[dict], *,
                    probes=()) -> dict:
    """Classify check evidence without grading any worker.

    Returns ``{'kind': ..., 'cause': ...}``.  ``kind`` is ``None`` on success,
    ``environment`` for timeout/launch/probe failure evidence, or
    ``verification`` for ordinary project-check failures.  Verification causes
    are ``unknown``, ``baseline_failure`` (same command already failed in the
    baseline with an identical non-truncated failure signature) or
    ``regression_candidate`` (the same command passed in the baseline).
    ``probes`` may hold failed probe labels or probe result mappings.
    """
    baseline_rows = [row for row in (baseline or ()) if isinstance(row, dict)]
    post_rows = [row for row in (post or ()) if isinstance(row, dict)]
    post_failures = [row for row in post_rows if not _row_ok(row)]
    baseline_failures = [row for row in baseline_rows if not _row_ok(row)]
    for row in post_failures + baseline_failures:
        reason = _environment_reason(row)
        if reason is not None:
            return {'kind': 'environment', 'cause': reason}
    if any(_probe_failed(entry) for entry in (probes or ())):
        return {'kind': 'environment', 'cause': 'probe_failure'}
    if not post_rows:
        if baseline_failures:
            return {'kind': 'verification', 'cause': 'baseline_failure'}
        return {'kind': None, 'cause': None}
    if not post_failures:
        return {'kind': None, 'cause': None}
    causes = {_verification_cause(row, baseline_rows) for row in post_failures}
    if 'regression_candidate' in causes:
        return {'kind': 'verification', 'cause': 'regression_candidate'}
    if causes == {'baseline_failure'}:
        return {'kind': 'verification', 'cause': 'baseline_failure'}
    return {'kind': 'verification', 'cause': 'unknown'}


def _row_exit_code(row: dict):
    for key in ('exit_code', 'returncode', 'code'):
        value = row.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            return value
    return None


def _row_ok(row: dict) -> bool:
    if not isinstance(row, dict):
        return False
    if row.get('launch_failed') or row.get('timed_out'):
        return False
    code = _row_exit_code(row)
    if code is not None:
        return code == 0
    if 'ok' in row:
        return bool(row.get('ok'))
    if 'failed' in row:
        return not bool(row.get('failed'))
    return False


def _environment_reason(row: dict):
    if row.get('launch_failed'):
        return 'launch_failure'
    if row.get('timed_out'):
        return 'timeout'
    explicit = 'launch_failed' in row or 'timed_out' in row
    code = _row_exit_code(row)
    if not explicit and code is not None:
        if code == TIMEOUT_EXIT_CODE:
            return 'timeout'
        if code == LAUNCH_FAILURE_EXIT_CODE:
            return 'launch_failure'
    return None


def _probe_failed(entry) -> bool:
    if entry is None:
        return False
    if isinstance(entry, str):
        return bool(entry.strip())
    if isinstance(entry, dict):
        return not _row_ok(entry)
    if isinstance(entry, (tuple, list)) and len(entry) >= 2 and isinstance(entry[1], int):
        return entry[1] != 0
    return True


def _verification_cause(row: dict, baseline_rows: list[dict]) -> str:
    command = row.get('command')
    matches = [base for base in baseline_rows if base.get('command') == command]
    if not matches:
        return 'unknown'
    failed_baseline = [base for base in matches if not _row_ok(base)]
    passed_baseline = [base for base in matches if _row_ok(base)]
    if passed_baseline and not failed_baseline:
        return 'regression_candidate'
    if any(_same_failure_signature(base, row) for base in failed_baseline):
        return 'baseline_failure'
    return 'unknown'


def _same_failure_signature(baseline_row: dict, post_row: dict) -> bool:
    code = _row_exit_code(baseline_row)
    if code is None or code != _row_exit_code(post_row):
        return False
    for row in (baseline_row, post_row):
        if row.get('stdout_truncated') or row.get('stderr_truncated'):
            return False
    hashes = ((baseline_row.get('stdout_sha256'), baseline_row.get('stderr_sha256')),
              (post_row.get('stdout_sha256'), post_row.get('stderr_sha256')))
    if any(value is None for pair in hashes for value in pair):
        return False
    return hashes[0] == hashes[1]
