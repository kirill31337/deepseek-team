"""Minimal persistent coordinator/worker ledger.

This is intentionally not a scheduler. It records distribution decisions, assignments,
worker results and dispositions so coordinator hooks can enforce process transitions and
restore context after compaction.
"""
from __future__ import annotations

from contextlib import contextmanager, ExitStack, nullcontext
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import subprocess
import shutil
import tempfile
import time
from typing import Any
import uuid

from . import lessons, scope_matching, settings, state_storage
from . import verification
from .config import sync_directory
from .effort import DEFAULT_EFFORT, normalize_effort, normalize_policy_effort
from .native_delegation import validate_native_exception
from .routing import RoutingService
from .routing_models import (FEATURE_VALUES, REQUIRED_SEMANTIC_FIELDS, RoutingError,
                            feature_value_problems, fingerprint,
                            missing_semantic_fields, number, validate_features,
                            validate_quality)

WORKER_WRITE_KINDS = {"implementation", "test", "fixture", "documentation", "metadata"}
WORKER_READ_KINDS = {"review", "research", "diagnostic", "test_plan"}
PROTECTED_COORDINATOR_KINDS = {
    "architecture", "security", "integration", "final_verification",
    "commit_push", "production", "secret_signing",
}
TECHNICAL_RETENTION = {
    "runner_unavailable", "dependency_unavailable", "environment_incompatible",
}
# Bounded execution evidence accepted by :func:`assignment_started`. Unknown keys
# are rejected so a raw prompt, credential or model output can never be stored by
# accident; ``prompt_sha256`` is validated as a digest on purpose.
EXECUTION_CONTEXT_KEYS = ("prompt_sha256", "prompt_brief", "base_head", "git_digest",
                          "prepared_fingerprints", "model")
LESSONS_EVIDENCE_LIMIT = 4000
OUTCOME_CONTEXT_LIMIT = 512 * 1024
LAUNCH_PHASE_ALLOCATION = 'allocation'
LAUNCH_PHASE_CANCELLED = 'cancelled-before-start'
# Optional plan metadata that also becomes part of a deliverable's identity, so an
# accepted or started scope cannot silently change by replanning. They stay
# optional: read-only protected plans do not need a mutation authorization.
OPTIONAL_DEFINITION_FIELDS = ("integration_of", "write_scope", "decision_artifacts",
                              "correction_of")
DELIVERABLE_REQUIRED_FIELDS = ("id", "kind", "scope", "executor", "acceptance",
                               "dependencies", "checks")
# Every logical field participates in the started-definition identity. New
# logical fields must be appended here so diagnostics and replay stay aligned.
LOGICAL_DEFINITION_FIELDS = (
    "id", "kind", "scope", "acceptance", "dependencies", "checks",
    "checks_reusable", "retention", "features", "requested_executor",
    "integration_of", "write_scope", "decision_artifacts", "correction_of",
    "delegation_reason", "native_exception",
)
_DELIVERABLE_ID = re.compile(r"[A-Za-z0-9_.-]{1,80}")
_CORRECTION_TASK_ID = re.compile(r"task-[0-9a-f]{20}")
_CORRECTION_ASSIGNMENT_ID = re.compile(r"as-[0-9a-f]{16}")
# Stable, human-readable advice for a routing decision that retained the work.
ROUTING_NEXT_STEPS = {
    "protected_kind": "keep this protected responsibility with the coordinator",
    "ineligible_kind": "use a supported deliverable kind or keep this slice with the coordinator",
    "write_requires_full_access": "explicitly opt into full access before delegating a writing "
                                  "slice, or keep it with the coordinator",
    "undeclared_risk": "declare risk in the features card; use unknown only when the risk is "
                       "honestly undeclared",
    "ineligible_risk": "keep this high-risk slice with the coordinator; use a bounded diagnostic "
                       "before delegating if its uncertainty can be reduced",
    "protected_risk": "keep this protected-risk slice with the coordinator",
    "ineligible_scope_size": "split this large deliverable into bounded slices before delegating",
    "ineligible_coupling": "split this cross-component deliverable into locally coupled slices "
                           "before delegating",
    "ineligible_localization": "use a bounded read-only diagnostic deliverable to localize the "
                               "change before delegating",
    "ineligible_clarity": "use a bounded read-only diagnostic deliverable to clarify the "
                          "requirements before delegating",
    "ineligible_verification": "declare real executable checks or a reproducer before "
                               "delegating; an empty checks list is allowed when the scope has no "
                               "runnable check, but never add placeholder commands",
    "declared_verification_required": "declare real executable checks or a reproducer before "
                                      "delegating, or keep this slice with the coordinator; an "
                                      "empty checks list is allowed when no runnable check exists, "
                                      "but never add placeholder commands",
    "ineligible_domain": "use a bounded read-only diagnostic deliverable to establish the domain",
    "ineligible_operation": "use a bounded read-only diagnostic deliverable to establish the operation",
    "ineligible_context_version": "state the exact context version before delegating",
    "ineligible_model": "route worker execution to the deepseek-flash model",
    "quality_failure_cooldown": "inspect the recorded quality failure and replan after the cooldown",
    "insufficient_measured_savings": "keep this slice with the coordinator while measured economics "
                                     "do not support delegation",
    "routing_disabled": "keep this slice with the coordinator or explicitly enable delegation",
}


class CoordinationError(Exception):
    def __init__(self, message: str, code: int = 78, *, constraint_code: str | None = None,
                 problems: list[dict] | None = None):
        self.message, self.code = message, code
        self.constraint_code = constraint_code
        self.problems = list(problems or [])
        super().__init__(message)


def _state_root() -> Path:
    from .state_storage import state_root, StorageError
    try:
        return state_root()
    except StorageError as error:
        raise CoordinationError(str(error), error.code) from None


def _project_root(root: Path) -> Path:
    try:
        resolved = settings.project_root(Path(root), required=True)
    except settings.SettingsError as error:
        raise CoordinationError(str(error)) from None
    assert resolved is not None
    return resolved


def _project_dir(root: Path) -> Path:
    root = _project_root(root)
    state = _state_root()
    key = hashlib.sha256(os.fsencode(str(root))).hexdigest()[:24]
    directory = state / "coordination" / key
    try:
        for path in (state, directory.parent, directory):
            state_storage.check_private_directory(path, create=True)
    except state_storage.StorageError as error:
        raise CoordinationError(str(error), error.code) from None
    marker = directory / "project.json"
    if not marker.exists():
        _atomic(marker, {"path": str(root), "schema": 1})
    return directory


def _task_path(root: Path, task_id: str) -> Path:
    if not re.fullmatch(r"task-[0-9a-f]{20}", task_id):
        raise CoordinationError("Invalid coordination task id.", 64)
    directory = _project_dir(root) / "tasks"
    directory.mkdir(mode=0o700, exist_ok=True)
    return directory / (task_id + ".json")


@contextmanager
def _lock(root: Path):
    directory = _project_dir(root)
    path = directory / "ledger.lock"
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise CoordinationError("Unsafe coordination lock.")
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    raw = (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=True) + "\n").encode()
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            os.fchmod(stream.fileno(), 0o600)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
        sync_directory(path.parent)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _read(path: Path) -> dict:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        raise CoordinationError("Coordination task is missing.", 66) from None
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise CoordinationError("Unsafe coordination state.")
        try:
            value = json.load(stream)
        except (ValueError, UnicodeError):
            raise CoordinationError("Invalid coordination state.") from None
    if not isinstance(value, dict):
        raise CoordinationError("Invalid coordination state.")
    return value


def _head(root: Path) -> str:
    result = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"],
                            text=True, capture_output=True, check=False)
    if result.returncode:
        raise CoordinationError("Cannot resolve project HEAD.")
    return result.stdout.strip()


def open_task(root: Path, *, session_id: str, turn_id: str, prompt: str,
              policy: settings.Policy, runtime: str = 'codex') -> dict:
    root = _project_root(root)
    seed = f"{session_id}\0{turn_id}".encode()
    task_id = "task-" + hashlib.sha256(seed).hexdigest()[:20]
    path = _task_path(root, task_id)
    with _lock(root):
        if path.exists():
            return _read(path)
        now = time.time()
        task = {
            "schema": 2, "id": task_id, "project": str(root),
            "session_id": session_id, "turn_id": turn_id,
            "prompt_sha256": hashlib.sha256(prompt.encode("utf-8", errors="replace")).hexdigest(),
            "base_head": _head(root), "created_at": now, "updated_at": now,
            "status": "planning", "classification": None, "small_evidence": None,
            "policy": policy.as_dict(), "saved_policy": settings.resolve(root).as_dict(),
            "runtime": runtime, "deliverables": [], "assignments": [],
            "coordinator_events": [], "constraints": [],
        }
        _atomic(path, task)
        return task


def load_task(root: Path, task_id: str) -> dict:
    return _read(_task_path(root, task_id))


def _created_at(task: dict) -> float:
    """Finite numeric creation time; missing or invalid values sort as 0."""
    value = task.get("created_at")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return value if isinstance(value, int) or math.isfinite(value) else 0.0


def _creation_order(task: dict):
    """Stable selection key: creation order, then the task id as a tie-break."""
    return (_created_at(task), str(task.get("id") or ""))


def _session_tasks(root: Path, session_id: str) -> list[dict]:
    directory = _project_dir(root) / "tasks"
    if not directory.exists():
        return []
    rows = []
    for path in directory.glob("task-*.json"):
        try:
            value = _read(path)
        except CoordinationError:
            continue
        if value.get("session_id") == session_id:
            rows.append(value)
    return rows


def unfinished_tasks(root: Path, session_id: str) -> list[dict]:
    """Every non completed/closed task for a session, newest creation first.

    Selection is independent of updated_at so a later cost/evidence update on an
    older task cannot hide newer or older unfinished work.
    """
    rows = [task for task in _session_tasks(root, session_id)
            if task.get("status") not in ("completed", "closed")]
    rows.sort(key=_creation_order, reverse=True)
    return rows


def latest_task(root: Path, session_id: str) -> dict | None:
    return max(_session_tasks(root, session_id), key=_creation_order, default=None)


def active_task(root: Path, session_id: str) -> dict | None:
    rows = unfinished_tasks(root, session_id)
    return rows[0] if rows else None


def _unstarted_task(task):
    return (task.get('classification') is None
            and task.get('status') in ('planning', 'closed')
            and not any(task.get(key) for key in (
                'deliverables', 'assignments', 'coordinator_events', 'constraints')))


def close_unstarted_task(root, task_id):
    """Close an untouched conversational draft without claiming work completed."""
    with _lock(root):
        task = load_task(root, task_id)
        if not _unstarted_task(task):
            return False
        if task.get('status') != 'closed':
            now = time.time()
            task.update(status='closed', closed_at=now, updated_at=now)
            _atomic(_task_path(root, task_id), task)
        return True


def _deliverable_outcome(task, item):
    if 'result_invalidated_at' in item:
        return 'invalidated'
    result = item.get('result')
    if not isinstance(result, dict):
        return 'pending'
    return result.get('outcome') or 'pending'


def _current_cancellation(task, item):
    """A current cancellation is historical, not retained active work.

    Outcome freshness still comes from ``_deliverable_outcome`` so a later
    mutation invalidates it. Requiring the same nonempty evidence as the result
    recorder prevents a bare ledger outcome from bypassing distribution checks.
    """
    if _deliverable_outcome(task, item) != 'cancelled':
        return False
    result = item.get('result')
    return (isinstance(result, dict)
            and isinstance(result.get('evidence'), str)
            and bool(result['evidence'].strip()))


def completion_issues(task):
    """Require terminal outcomes for work not tracked by worker assignments."""
    issues = []
    for item in task.get('deliverables', []):
        if item.get('executor') not in ('coordinator', 'native-agent'):
            continue
        outcome = _deliverable_outcome(task, item)
        if outcome not in ('accepted', 'cancelled'):
            issues.append(f"{item['id']}: {item['executor']} outcome is outstanding; "
                          "record an accepted or cancelled result with evidence")
    current_jobs = {}
    for row in task.get('assignments', []):
        if row.get('job_id'):
            current_jobs[row['job_id']] = row
        elif (isinstance(row.get('workspace_id'), str)
              and re.fullmatch('[0-9a-f]{32}', row['workspace_id'])):
            # An incomplete identity is not proof that an owned job is closed.
            current_jobs[('workspace', row['workspace_id'])] = row
    for row in current_jobs.values():
        if not row.get('job_finalization'):
            issues.append(f"{row['id']}: logical job requires explicit finalize or cancel")
    return issues


def _mutation_touches_scope(root, paths, scopes):
    if not paths or not scopes:
        return True
    root = _project_root(root)
    # Reuse the hook's canonicalization and intersection so acceptance can never
    # be invalidated by a different algorithm than the one that authorized it.
    for path in paths:
        canonical_path = scope_matching.canonical(root, path)
        if canonical_path is None:
            continue
        for scope in scopes:
            canonical_scope = scope_matching.canonical(root, scope)
            if (canonical_scope is not None
                    and scope_matching.mutation_overlaps(root, canonical_path, canonical_scope)):
                return True
    return False


def begin_turn(root: Path, *, session_id: str, turn_id: str, prompt: str,
               policy: settings.Policy, runtime: str = 'codex') -> dict:
    """Reuse unfinished work across Stop continuations and compaction."""
    existing = active_task(root, session_id)
    if existing is None:
        task = open_task(root, session_id=session_id, turn_id=turn_id,
                         prompt=prompt, policy=policy, runtime=runtime)
        task.setdefault('turns', [turn_id])
        task.setdefault('prompt_hashes', [task['prompt_sha256']])
        _atomic(_task_path(root, task['id']), task)
        return task
    with _lock(root):
        task = load_task(root, existing['id'])
        turns = task.setdefault('turns', [])
        if turn_id not in turns:
            turns.append(turn_id)
        task.setdefault('prompt_hashes', []).append(
            hashlib.sha256(prompt.encode('utf-8', errors='replace')).hexdigest())
        task['updated_at'] = time.time()
        _atomic(_task_path(root, task['id']), task)
        return task


def complete_task(root: Path, task_id: str) -> dict:
    root = _project_root(root)
    _validate_retained_results(root, task_id)
    _reconcile_job_publications(root, task_id)
    with _lock(root):
        task = load_task(root, task_id)
        issues = validate_task(root, task_id) + completion_issues(task)
        for row in task.get('assignments', []):
            if row.get('status') not in ('succeeded', 'failed'):
                issues.append(f"{row['id']}: worker assignment is unfinished")
            elif not row.get('disposition'):
                issues.append(f"{row['id']}: worker result has no disposition")
        if issues:
            raise CoordinationError('Cannot complete task: ' + '; '.join(issues))
        task['status'] = 'completed'
        task['completed_at'] = time.time()
        task['updated_at'] = task['completed_at']
        _atomic(_task_path(root, task_id), task)
        return task


def _exact_project_path(value, field: str) -> str:
    """Return one exact project-relative path, or reject a malformed entry."""
    if not isinstance(value, str) or not value.strip():
        raise CoordinationError(f"{field} entries must be non-empty project-relative paths.", 64)
    if value != value.strip():
        raise CoordinationError(f"{field} entries must not carry surrounding whitespace.", 64)
    parts = value.split("/")
    if (value.startswith("/") or "\\" in value or re.match(r"[A-Za-z]:", parts[0])
            or any(part in ("", ".", "..") for part in parts)
            or any(char in value for char in "*?[")):
        raise CoordinationError(
            f"{field} entries must be exact project-relative paths without wildcards.", 64)
    return value


def _plan_metadata(value, field: str):
    """Validate one optional plan-metadata field and return its normalized value."""
    if field == "correction_of":
        if not isinstance(value, dict) or set(value) != {"task_id", "assignment_id"}:
            raise CoordinationError(
                "correction_of must contain exactly task_id and assignment_id.", 64)
        task_id = value.get("task_id")
        assignment_id = value.get("assignment_id")
        if (not isinstance(task_id, str) or not _CORRECTION_TASK_ID.fullmatch(task_id)
                or not isinstance(assignment_id, str)
                or not _CORRECTION_ASSIGNMENT_ID.fullmatch(assignment_id)):
            raise CoordinationError(
                "correction_of requires a task-20hex task id and as-16hex assignment id.", 64)
        return {"task_id": task_id, "assignment_id": assignment_id}
    if not isinstance(value, list):
        raise CoordinationError(f"{field} must be a list.", 64)
    if field == "integration_of":
        entries = []
        for entry in value:
            if not isinstance(entry, str) or not _DELIVERABLE_ID.fullmatch(entry):
                raise CoordinationError("integration_of entries must be deliverable ids.", 64)
            entries.append(entry)
        return entries
    return [_exact_project_path(entry, field) for entry in value]


def _requested_executor(item: dict) -> str:
    """The executor the coordinator asked for, before the saved routing decision."""
    routing = item.get("routing") or {}
    if "requested_executor" in routing:
        return routing["requested_executor"]
    return item.get("executor")


def _started_deliverable_ids(task: dict) -> set[str]:
    # A preparation binding freezes identity without claiming execution began.
    started = {row["deliverable_id"] for row in task.get("assignments", [])
               if row.get("status") != "planned" or row.get('job_id')}
    started.update(item["id"] for item in task.get("deliverables", [])
                   if item.get("routing_feedback") or item.get("result")
                   or item.get("native_dispatches") or 'result_invalidated_at' in item)
    return started


def _valid_started_legacy_executor(root: Path, task: dict, item: dict) -> bool:
    """Let an unchanged, already-started pre-enforcement plan finish.

    Legacy explicit executors could override a prediction. Require their actual
    persisted decision and full original binding, rather than trusting a claim
    in the ledger or silently converting them to a new automatic assignment.
    """
    routing = item.get('routing') or {}
    requested = routing.get('requested_executor')
    if (item.get('id') not in _started_deliverable_ids(task)
            or requested not in ('worker', 'coordinator')
            or item.get('executor') != requested):
        return False
    try:
        decision = RoutingService(root).decision(routing.get('decision_id'))
        # An explicit legacy executor was independent of the router's mode and
        # prediction. Preserve its authenticated identity even in advisory,
        # shadow or off mode; automatic assignments still use strict validation.
        return (decision['binding'] == _binding(task['id'], item)
                and decision['features'] == validate_features(item.get('features')))
    except (RoutingError, KeyError, TypeError):
        return False


def _auto_executor_issue(item: dict, requested: str) -> str | None:
    """Return the Auto-profile bypass issue for one deliverable, if any.

    Ordinary worker-eligible read and write kinds must let the saved routing
    decision choose the executor; protected coordinator responsibilities keep an
    explicit coordinator executor. Nothing is converted automatically.
    """
    if item.get("executor") == "native-agent":
        # The existing attested native exception is validated where the
        # deliverable is normalized; it is not an auto-routing bypass.
        return None
    kind = item.get("kind")
    if kind in PROTECTED_COORDINATOR_KINDS:
        if requested != "coordinator":
            return (f"{item['id']}: protected coordinator responsibility {kind} must keep "
                    "executor:'coordinator'")
        return None
    if kind in WORKER_READ_KINDS or kind in WORKER_WRITE_KINDS:
        if requested != "auto":
            return (f"{item['id']}: a substantial Auto-profile {kind} deliverable must request "
                    "executor:'auto' and let the saved routing decision choose worker or "
                    f"coordinator; explicit executor:'{requested}' is not an accepted bypass")
    return None


def _auto_executor_issues(item_list, started_ids) -> list[str]:
    issues = []
    for item in item_list:
        if item["id"] in started_ids:
            # Started deliverables are immutable; their saved routing decision is
            # validated instead of the registration-time request.
            continue
        issue = _auto_executor_issue(item, item["executor"])
        if issue:
            issues.append(issue)
    return issues


def _routing_next_step(requested: str, resolved: str, reason_codes) -> str | None:
    """One concrete next step for work a routing decision retained."""
    if resolved != 'coordinator':
        return None
    for code in reason_codes:
        step = ROUTING_NEXT_STEPS.get(code)
        if step:
            return step
    if requested == 'auto':
        return 'keep this slice with the coordinator'
    return None


def routing_diagnostics(task: dict) -> list[dict]:
    """Concise per-deliverable routing outcome for plan JSON and status text.

    Only identifiers, executors, stable reason codes and one bounded next step are
    exposed; prompts, secrets and raw evidence never appear here.
    """
    rows = []
    for item in task.get('deliverables', []):
        routing = item.get('routing') or {}
        requested = _requested_executor(item)
        resolved = item.get('executor')
        codes = [str(code) for code in (routing.get('reason_codes') or [])]
        # Older decisions used one risk code for unknown, high and protected.
        # Keep the recorded history intact while giving truthful recovery advice.
        features = item.get('features')
        risk = features.get('risk', 'unknown') if isinstance(features, dict) else 'unknown'
        legacy_risk = {'unknown': 'undeclared_risk', 'protected': 'protected_risk'}.get(risk)
        advice_codes = [legacy_risk if code == 'ineligible_risk' and legacy_risk else code
                        for code in codes]
        rows.append({'id': item.get('id'), 'requested_executor': requested,
                     'resolved_executor': resolved, 'reason_codes': codes,
                     'next_step': _routing_next_step(requested, resolved, advice_codes)})
    return rows


def _problem(deliverable_id, field: str, code: str, message: str) -> dict:
    return {'deliverable_id': deliverable_id, 'field': field, 'code': code,
            'message': message}


def _problems_error(problems: list[dict], *, code: int = 64,
                    header: str | None = None) -> None:
    """Raise one controlled error carrying deterministic structured problems."""
    message = '\n'.join(problem['message'] for problem in problems)
    if header:
        message = header + ('\n' + message if message else '')
    raise CoordinationError(message, code, problems=problems)


def _deliverable_prefix(deliverable_id, index: int) -> str:
    if deliverable_id is not None:
        return f'{deliverable_id}: '
    return f'Deliverable {index}: '


def _validate_deliverable(raw, index: int):
    """Collect independent structural/enum problems for one raw deliverable."""
    if not isinstance(raw, dict):
        return None, [_problem(
            None, 'deliverable', 'invalid_type',
            f'Each deliverable must be an object; entry {index} is not an object.')]
    raw_id = raw.get('id')
    valid_id = (isinstance(raw_id, str)
                and bool(_DELIVERABLE_ID.fullmatch(raw_id)))
    deliverable_id = raw_id if valid_id else None
    prefix = _deliverable_prefix(deliverable_id, index)
    problems = []
    required_text = '/'.join(DELIVERABLE_REQUIRED_FIELDS)
    for field in DELIVERABLE_REQUIRED_FIELDS:
        if field in raw:
            continue
        if field == 'checks':
            problems.append(_problem(
                deliverable_id, 'checks', 'missing_field',
                f'{prefix}Deliverables require {required_text}; checks must be present as a '
                'list. An empty list is allowed when no runnable check exists; do not add '
                'placeholder checks.'))
        else:
            problems.append(_problem(
                deliverable_id, field, 'missing_field',
                f'{prefix}Deliverables require {required_text}; missing field {field}.'))
    if 'id' in raw and not valid_id:
        problems.append(_problem(
            deliverable_id, 'id', 'invalid_value',
            f'{prefix}Invalid deliverable id; expected 1-80 letters, digits, dot, dash or underscore.'))

    kind = raw.get('kind')
    if 'kind' in raw:
        if not isinstance(kind, str):
            problems.append(_problem(
                deliverable_id, 'kind', 'invalid_type',
                f'{prefix}Deliverable kind must be a string.'))
        elif kind not in FEATURE_VALUES['kind']:
            choices = ', '.join(sorted(FEATURE_VALUES['kind']))
            problems.append(_problem(
                deliverable_id, 'kind', 'invalid_enum',
                f'{prefix}Deliverable kind must be one of: {choices}.'))

    executor = raw.get('executor')
    executor_choices = ('auto', 'worker', 'coordinator', 'native-agent')
    if 'executor' in raw:
        if not isinstance(executor, str):
            problems.append(_problem(
                deliverable_id, 'executor', 'invalid_type',
                f'{prefix}Deliverable executor must be a string.'))
        elif executor not in executor_choices:
            problems.append(_problem(
                deliverable_id, 'executor', 'invalid_enum',
                f'{prefix}Deliverable executor must be auto, worker, coordinator or native-agent.'))

    if 'checks_reusable' in raw and type(raw['checks_reusable']) is not bool:
        problems.append(_problem(
            deliverable_id, 'checks_reusable', 'invalid_type',
            f'{prefix}checks_reusable must be a boolean.'))

    for field in ('scope', 'acceptance', 'dependencies', 'checks'):
        if field not in raw:
            continue
        value = raw[field]
        if field == 'scope' and isinstance(value, str):
            # One path string is a supported shorthand; normalize it to a
            # one-entry list only after the entry itself validates.
            entries = [value]
        elif isinstance(value, list):
            entries = value
        else:
            shape = 'a list or a single path string' if field == 'scope' else 'a list'
            problems.append(_problem(
                deliverable_id, field, 'invalid_type',
                f'{prefix}{field} must be {shape}.'))
            continue
        if field == 'scope':
            for entry in entries:
                if (not isinstance(entry, str) or not entry
                        or '\x00' in entry):
                    problems.append(_problem(
                        deliverable_id, 'scope', 'invalid_type',
                        f'{prefix}scope entries must be non-empty strings.'))
        elif field in ('acceptance', 'checks'):
            for entry in entries:
                if (not isinstance(entry, str) or not entry.strip()
                        or '\x00' in entry):
                    problems.append(_problem(
                        deliverable_id, field, 'invalid_type',
                        f'{prefix}{field} entries must be non-empty strings '
                        'without NUL.'))
        elif field == 'dependencies':
            for entry in entries:
                if not isinstance(entry, dict):
                    problems.append(_problem(
                        deliverable_id, 'dependencies', 'invalid_type',
                        f'{prefix}Each dependency must be an object.'))
                    continue
                dependency_kind = entry.get('kind')
                if dependency_kind not in ('command', 'path'):
                    problems.append(_problem(
                        deliverable_id, 'dependencies.kind', 'invalid_enum',
                        f'{prefix}Dependencies must use kind=command or kind=path.'))
                dependency_value = entry.get('value')
                if not isinstance(dependency_value, str) or not dependency_value:
                    problems.append(_problem(
                        deliverable_id, 'dependencies.value', 'invalid_type',
                        f'{prefix}Dependency values must be non-empty strings.'))

    if 'features' in raw:
        features = raw['features']
        if not isinstance(features, dict):
            problems.append(_problem(
                deliverable_id, 'features', 'invalid_type',
                f'{prefix}Features must be a structured feature card.'))
        else:
            for issue in feature_value_problems(features):
                problems.append(_problem(
                    deliverable_id, issue['field'], issue['code'],
                    f"{prefix}{issue['field']}: {issue['message']}"))
            feature_kind = features.get('kind')
            if (isinstance(kind, str) and kind in FEATURE_VALUES['kind']
                    and isinstance(feature_kind, str)
                    and feature_kind in FEATURE_VALUES['kind']
                    and feature_kind != kind):
                problems.append(_problem(
                    deliverable_id, 'features.kind', 'invalid_value',
                    f'{prefix}Feature kind must match deliverable kind.'))

    for field in OPTIONAL_DEFINITION_FIELDS:
        if field not in raw:
            continue
        try:
            _plan_metadata(raw[field], field)
        except CoordinationError as error:
            problems.append(_problem(
                deliverable_id, field, 'invalid_metadata',
                f'{prefix}{error.message}'))

    if (executor == 'worker' and isinstance(kind, str)
            and kind in PROTECTED_COORDINATOR_KINDS):
        problems.append(_problem(
            deliverable_id, 'executor', 'protected_assignment',
            f'{prefix}Protected coordinator responsibilities cannot be assigned to a worker.'))
    if executor == 'native-agent':
        reason = raw.get('delegation_reason')
        if not isinstance(reason, str) or len(reason.strip()) < 12:
            problems.append(_problem(
                deliverable_id, 'delegation_reason', 'missing_field',
                f'{prefix}native-agent deliverables require a concrete delegation_reason.'))
        if isinstance(kind, str) and kind in PROTECTED_COORDINATOR_KINDS:
            problems.append(_problem(
                deliverable_id, 'executor', 'protected_assignment',
                f'{prefix}Protected coordinator responsibilities cannot be assigned to a native agent.'))
        try:
            validate_native_exception(raw)
        except ValueError as error:
            problems.append(_problem(
                deliverable_id, 'native_exception', 'invalid_value',
                f'{prefix}{error}'))

    if problems:
        return None, problems

    value = dict(raw)
    # Derived state is read only from our ledger, never accepted from a plan.
    for key in ('routing', 'routing_feedback', 'result', 'result_invalidated_at',
                'native_dispatches', 'native_dispatch_started_at'):
        value.pop(key, None)
    for field in OPTIONAL_DEFINITION_FIELDS:
        if field in raw:
            value[field] = _plan_metadata(raw[field], field)
    value['scope'] = ([raw['scope']] if isinstance(raw['scope'], str)
                      else list(raw['scope']))
    value['acceptance'] = list(raw['acceptance'])
    value['dependencies'] = list(raw['dependencies'])
    value['checks'] = list(raw['checks'])
    value['checks_reusable'] = raw.get('checks_reusable', False)
    return value, []


def _normalize_deliverable(raw: dict) -> dict:
    value, problems = _validate_deliverable(raw, 0)
    if problems:
        _problems_error(problems)
    assert value is not None
    return value


def _validate_plan_payload(plan):
    """Validate plan-level shape and all deliverables before any ledger write."""
    if not isinstance(plan, dict):
        return [], [_problem(None, 'plan', 'invalid_type',
                             'Coordination plan must be an object.')]
    problems = []
    classification = plan.get('classification')
    if classification not in ('substantial', 'small'):
        problems.append(_problem(
            None, 'classification', 'invalid_enum',
            'classification must be substantial or small.'))
    raw_deliverables = plan.get('deliverables', [])
    if not isinstance(raw_deliverables, list):
        problems.append(_problem(
            None, 'deliverables', 'invalid_type', 'deliverables must be a list.'))
        return [], problems
    deliverables = []
    for index, raw in enumerate(raw_deliverables):
        value, item_problems = _validate_deliverable(raw, index)
        if item_problems:
            problems.extend(item_problems)
        elif value is not None:
            deliverables.append(value)
    return deliverables, problems


def _definition(item):
    definition = {key: item[key] for key in ('id', 'kind', 'scope', 'acceptance',
                                             'dependencies', 'checks', 'features')}
    # The reuse attestation belongs to the immutable current definition.
    definition['checks_reusable'] = item.get('checks_reusable', False)
    for field in OPTIONAL_DEFINITION_FIELDS:
        # Only present fields join the identity so historical plans, whose saved
        # routing bindings never carried them, keep validating unchanged.
        if field in item:
            definition[field] = item[field]
    if item.get('executor') == 'native-agent':
        # The native attestation is part of the identity of a native deliverable
        # so a started/attested dispatch cannot be rewritten by replanning.
        definition['delegation_reason'] = item.get('delegation_reason')
        definition['native_exception'] = item.get('native_exception')
    return definition


def _binding(task_id, item):
    requested = (item.get('routing') or {}).get('requested_executor', item['executor'])
    return {'task_id': task_id, 'deliverable_id': item['id'],
            'plan_hash': fingerprint(dict(_definition(item), requested_executor=requested))}


def _task_belongs_to(task, root: Path) -> bool:
    try:
        project = Path(os.path.realpath(str(task.get('project'))))
    except (OSError, ValueError):
        return False
    return project == Path(os.path.realpath(str(root)))


def _authoritative_successful_execution(copy, lease, attempt_id):
    """Keep execution identity authoritative across terminal preparation attempts.

    ``workspace.prepare`` is a provider-free operation on the same logical job and
    updates ``record.json`` to its own attempt. It must not make a succeeded
    execution look stale. Accept only a succeeded execution with no later
    execution, followed by a nonempty chain of completed successful preparations
    whose final attempt is exactly the persisted latest operation. Any pending,
    running, failed or unknown operation in that chain remains invalid.
    """
    attempts = lease.get('attempts', [])
    matches = [index for index, entry in enumerate(attempts)
               if entry.get('id') == attempt_id]
    if len(matches) != 1:
        return False
    execution_index = matches[0]
    execution = attempts[execution_index]
    if execution.get('operation') != 'execution' or execution.get('status') != 'succeeded':
        return False
    if lease.get('pending_attempt'):
        return False
    later = attempts[execution_index + 1:]
    if not later:
        return copy.metadata.get('attempt_id') in (None, attempt_id)
    if any(entry.get('operation') != 'preparation' or entry.get('status') != 'succeeded'
           for entry in later):
        return False
    count = copy.metadata.get('attempt')
    if type(count) is not int or count != len(attempts):
        return False
    return (copy.metadata.get('attempt_id') == later[-1].get('id')
            and copy.metadata.get('operation') == 'preparation'
            and copy.metadata.get('status') == 'succeeded')


def _correction_workspace(root: Path, task: dict, reference: dict, *,
                          explicit_workspace=None, bound_assignment=None,
                          continuation_assignment=None, start_assignment=None):
    """Resolve one reviewed predecessor without allocating or mutating a copy."""
    from . import workspace, workspace_lifecycle

    project_root = _project_root(root)
    if not _task_belongs_to(task, project_root):
        raise CoordinationError('Correction task belongs to another project.', 78)
    predecessor_task_id = reference['task_id']
    predecessor_assignment_id = reference['assignment_id']
    try:
        predecessor_task = load_task(project_root, predecessor_task_id)
    except CoordinationError as error:
        if error.code == 66:
            raise CoordinationError('Correction predecessor task is missing; inspect the ledger.', 64) from None
        raise
    if not _task_belongs_to(predecessor_task, project_root):
        raise CoordinationError('Correction predecessor task belongs to another project.', 78)
    predecessor_row = next(
        (row for row in predecessor_task.get('assignments', [])
         if row.get('id') == predecessor_assignment_id), None)
    if predecessor_row is None:
        raise CoordinationError('Correction predecessor assignment is missing; inspect the ledger.', 64)
    disposition = predecessor_row.get('disposition') or {}
    if disposition.get('kind') != 'needs-rework':
        raise CoordinationError(
            'Correction predecessor must have a reviewed needs-rework disposition.', 64)
    if predecessor_row.get('status') != 'succeeded':
        raise CoordinationError(
            'Correction predecessor must be a succeeded result; failed attempts require explicit resume.',
            64)
    if predecessor_row.get('job_finalization'):
        raise CoordinationError('Correction predecessor job is finalized.', 73)
    identifier = predecessor_row.get('workspace_id')
    if not isinstance(identifier, str) or not re.fullmatch(r'[0-9a-f]{32}', identifier):
        raise CoordinationError('Correction predecessor has no owned execution workspace.', 64)
    history_workspaces = {
        entry.get('workspace_id') for entry in predecessor_row.get('attempt_history', [])
        if isinstance(entry, dict) and entry.get('workspace_id')}
    if history_workspaces - {identifier}:
        raise CoordinationError(
            'Correction predecessor attempt history spans multiple workspaces; inspect the ledger.',
            64)
    current_head = task.get('base_head')
    predecessor_head = predecessor_task.get('base_head')
    if (not isinstance(current_head, str) or not current_head
            or predecessor_head != current_head):
        raise CoordinationError(
            'Correction predecessor uses a different source/base HEAD; start a new job.', 64)
    try:
        copy = workspace.load(_state_root(), identifier)
    except workspace.WorkspaceError as error:
        raise CoordinationError(
            'Correction predecessor workspace is unavailable: ' + error.message,
            error.code) from None
    if copy.source.resolve() != project_root:
        raise CoordinationError('Correction predecessor workspace belongs to another project.', 78)
    if copy.metadata.get('base_head') != current_head:
        raise CoordinationError(
            'Correction predecessor workspace base HEAD is stale; start a new job.', 64)
    try:
        lease = workspace_lifecycle.read(copy)
    except workspace.WorkspaceError as error:
        raise CoordinationError(
            'Correction predecessor lease is unavailable: ' + error.message,
            error.code) from None
    if lease is None:
        raise CoordinationError('Correction predecessor logical job lease is missing.', 73)
    if lease['phase'] in workspace_lifecycle.FINAL_PHASES:
        raise CoordinationError('Correction predecessor workspace is finalized.', 73)
    expected_binding = {'task_id': predecessor_task_id,
                        'assignment_id': predecessor_assignment_id}
    continuation_row = None
    prestart_failure = False
    pending = lease.get('pending_attempt')
    pending_binding = None
    if (isinstance(pending, dict) and set(pending) == {
            'task_id', 'assignment_id', 'id'}
            and re.fullmatch('[0-9a-f]{32}', str(pending.get('id', '')))):
        pending_binding = {'task_id': pending.get('task_id'),
                           'assignment_id': pending.get('assignment_id')}
    if (bound_assignment is None and start_assignment is not None
            and pending_binding == {'task_id': task['id'],
                                    'assignment_id': start_assignment}):
        bound_assignment = start_assignment
    if continuation_assignment is not None:
        continuation_row = _assignment(task, continuation_assignment)
        prestart_failure = (continuation_row.get('status') == 'failed'
                            and continuation_row.get('failure_stage') == 'preparation'
                            and not continuation_row.get('attempt_id'))
    preparing_id = bound_assignment or continuation_assignment or start_assignment
    preparation_reserved = False
    if preparing_id is not None:
        preparing_row = _assignment(task, preparing_id)
        preparing_binding = {'task_id': task['id'], 'assignment_id': preparing_id}
        history = lease.get('binding_history', [])
        preparation_reserved = bool(
            preparing_row.get('status') in ('planned', 'failed')
            and not preparing_row.get('attempt_id')
            and preparing_row.get('workspace_id') in (None, identifier)
            and preparing_row.get('job_id') in (None, lease['job_id'])
            and lease.get('binding') == preparing_binding
            and isinstance(history, list) and len(history) >= 2
            and history[-2:] == [expected_binding, preparing_binding]
            and not lease.get('pending_attempt'))
    continuation_reserved = False
    if continuation_row is not None and not prestart_failure:
        current_attempt_id = continuation_row.get('attempt_id')
        latest_attempt = (lease.get('attempts') or [None])[-1]
        running_attempt = next(
            (attempt for attempt in lease.get('attempts', [])
             if isinstance(attempt, dict) and attempt.get('id') == current_attempt_id),
            None)
        running_reservation = bool(
            continuation_row.get('status') == 'running'
            and isinstance(running_attempt, dict)
            and running_attempt.get('operation') == 'execution'
            and running_attempt.get('status') == 'running'
            and continuation_row.get('workspace_id') in (None, identifier)
            and copy.metadata.get('attempt_id') == current_attempt_id)
        current_attempt_reservation = bool(
            continuation_row.get('status') == 'failed'
            and not lease.get('pending_attempt')
            and lease.get('binding') == {'task_id': task['id'],
                                         'assignment_id': continuation_assignment}
            and isinstance(latest_attempt, dict)
            and latest_attempt.get('id') != current_attempt_id
            and latest_attempt.get('id') == copy.metadata.get('attempt_id')
            and latest_attempt.get('operation') == 'execution'
            and latest_attempt.get('status') == 'running'
            and latest_attempt.get('generation') == lease['generation']
            and latest_attempt.get('check_timeout_seconds')
               == copy.metadata.get('check_timeout_seconds')
            and copy.metadata.get('job_id') == lease['job_id']
            and copy.metadata.get('attempt') == len(lease.get('attempts', []))
            and copy.metadata.get('operation') == 'execution'
            and copy.metadata.get('status') == 'running'
            and copy.metadata.get('work_inode') == lease['work_inode'])
        continuation_reserved = (
            pending_binding == {'task_id': task['id'],
                                'assignment_id': continuation_assignment}
            or running_reservation or current_attempt_reservation)
    bound_to_current = (preparation_reserved or
                        ((bound_assignment is not None or continuation_assignment is not None)
                         and not prestart_failure))
    if preparation_reserved:
        expected_binding = preparing_binding
    elif bound_assignment is not None:
        expected_binding = {'task_id': task['id'], 'assignment_id': bound_assignment}
        current_row = _assignment(task, bound_assignment)
        current_attempt_id = current_row.get('attempt_id')
        latest_attempt = (lease.get('attempts') or [None])[-1]
        running_attempt = next(
            (attempt for attempt in lease.get('attempts', [])
             if isinstance(attempt, dict) and attempt.get('id') == current_attempt_id),
            None)
        running_reservation = bool(
            current_row.get('status') == 'running'
            and isinstance(running_attempt, dict)
            and running_attempt.get('operation') == 'execution'
            and running_attempt.get('status') == 'running'
            and current_row.get('workspace_id') in (None, identifier)
            and copy.metadata.get('attempt_id') == current_attempt_id)
        pending_reservation = pending_binding == {
            'task_id': task['id'], 'assignment_id': bound_assignment}
        current_attempt_reservation = bool(
            current_row.get('status') == 'failed'
            and not lease.get('pending_attempt')
            and lease.get('binding') == expected_binding
            and isinstance(latest_attempt, dict)
            and latest_attempt.get('id') != current_attempt_id
            and latest_attempt.get('id') == copy.metadata.get('attempt_id')
            and latest_attempt.get('operation') == 'execution'
            and latest_attempt.get('status') == 'running'
            and latest_attempt.get('generation') == lease['generation']
            and latest_attempt.get('check_timeout_seconds')
               == copy.metadata.get('check_timeout_seconds')
            and copy.metadata.get('job_id') == lease['job_id']
            and copy.metadata.get('attempt') == len(lease.get('attempts', []))
            and copy.metadata.get('operation') == 'execution'
            and copy.metadata.get('status') == 'running'
            and copy.metadata.get('work_inode') == lease['work_inode'])
        if not (pending_reservation or running_reservation or current_attempt_reservation):
            raise CoordinationError(
                'Correction assignment start did not reserve this predecessor workspace.', 64)
    elif continuation_assignment is not None and not prestart_failure:
        expected_binding = {'task_id': task['id'], 'assignment_id': continuation_assignment}
    actual_binding = lease.get('binding')
    if actual_binding != expected_binding:
        raise CoordinationError(
            'Correction predecessor does not match its lease binding; link the authoritative assignment.',
            64)
    if (lease.get('workspace_id') != identifier
            or copy.metadata.get('job_id') != lease.get('job_id')):
        raise CoordinationError('Correction predecessor workspace/job identity is stale.', 64)
    if predecessor_row.get('job_id') and predecessor_row['job_id'] != lease['job_id']:
        raise CoordinationError('Correction predecessor is not the authoritative logical job.', 64)
    attempt_id = predecessor_row.get('attempt_id')
    known = {entry.get('id') for entry in lease.get('attempts', [])
             if isinstance(entry, dict)}
    if lease.get('pending_attempt'):
        known.add(lease['pending_attempt'].get('id'))
    if attempt_id:
        if attempt_id not in known:
            raise CoordinationError('Correction predecessor attempt is not the current lease attempt.', 64)
        if (not bound_to_current
                and not _authoritative_successful_execution(copy, lease, attempt_id)):
            raise CoordinationError('Correction predecessor execution record is stale.', 64)
    if continuation_assignment is not None:
        current_row = continuation_row or _assignment(task, continuation_assignment)
        if current_row.get('status') not in ('failed', 'running'):
            raise CoordinationError(
                'Correction lineage can bind the current assignment only for explicit resume.', 64)
        if (current_row.get('workspace_id') and current_row['workspace_id'] != identifier):
            raise CoordinationError('Correction assignment is bound to a different workspace.', 64)
        if current_row.get('job_id') and current_row['job_id'] != lease['job_id']:
            raise CoordinationError('Correction assignment is bound to a different logical job.', 64)
        if current_row.get('attempt_id'):
            if current_row['attempt_id'] not in known:
                raise CoordinationError('Correction assignment attempt is not in the lease history.', 64)
            if (copy.metadata.get('attempt_id')
                    and copy.metadata['attempt_id'] != current_row['attempt_id']
                    and not continuation_reserved):
                raise CoordinationError('Correction assignment execution record is stale.', 64)
    if (not bound_to_current and copy.metadata.get('status') != 'succeeded'):
        raise CoordinationError(
            'Correction predecessor workspace does not retain a successful result; inspect it before recovery.',
            64)
    if bound_to_current and copy.metadata.get('status') not in ('ready', 'running', 'succeeded', 'failed'):
        raise CoordinationError(
            'Correction workspace is not in a resumable execution state.', 64)
    try:
        copy.verify()
        info = copy.path.stat()
    except (workspace.WorkspaceError, OSError) as error:
        message = getattr(error, 'message', 'execution tree is unavailable')
        raise CoordinationError('Correction predecessor workspace is unstable: ' + message,
                                getattr(error, 'code', 78)) from None
    if ((info.st_ino, info.st_dev) != (lease.get('work_inode'), lease.get('work_device'))
            or info.st_ino != copy.metadata.get('work_inode')):
        raise CoordinationError('Correction predecessor execution inode is stale.', 73)
    if explicit_workspace is not None:
        explicit_id = getattr(explicit_workspace, 'id', explicit_workspace)
        if explicit_id != identifier:
            raise CoordinationError(
                'Explicit --workspace does not match the assignment correction_of predecessor.',
                64)
    return copy


def _logical_job_chain(root: Path, task_id: str, assignment_id: str):
    """Follow authoritative correction links backwards through one job."""
    project_root = _project_root(root)
    task = load_task(project_root, task_id)
    row = _assignment(task, assignment_id)
    job_id = row.get('job_id')
    workspace_id = row.get('workspace_id')
    if not isinstance(job_id, str) or not re.fullmatch('[0-9a-f]{32}', job_id):
        raise CoordinationError('Assignment has no authoritative logical job identity.', 64)
    if not isinstance(workspace_id, str) or not re.fullmatch('[0-9a-f]{32}', workspace_id):
        raise CoordinationError('Assignment has no authoritative execution workspace.', 64)
    chain = []
    seen = set()
    while True:
        current_key = (task['id'], row['id'])
        if current_key in seen:
            raise CoordinationError('Correction lineage contains a cycle; inspect the ledger.', 64)
        seen.add(current_key)
        if not _task_belongs_to(task, project_root):
            raise CoordinationError('Correction lineage leaves this project.', 78)
        if row.get('job_id') != job_id or row.get('workspace_id') != workspace_id:
            raise CoordinationError(
                'Correction lineage spans a different logical job; inspect the ledger.', 64)
        item = _deliverable(task, row.get('deliverable_id'))
        reference = row.get('correction_of') or (
            item.get('correction_of') if isinstance(item, dict) else None)
        chain.append((task['id'], row['id']))
        if reference is None:
            return chain
        reference = _plan_metadata(reference, 'correction_of')
        task = load_task(project_root, reference['task_id'])
        row = _assignment(task, reference['assignment_id'])


def _finalization_request(request, root, task_id, assignment_id, copy, lease,
                          cancelled):
    """Validate both the old bounded intent and the fully identified new form."""
    allowed = {'at', 'evidence', 'cancelled', 'workspace_id', 'project', 'task_id',
               'assignment_id', 'job_id', 'discard_result', 'reason_code',
               'result_schema', 'discarded'}
    if (not isinstance(request, dict) or set(request) - allowed
            or type(request.get('cancelled')) is not bool
            or request['cancelled'] != cancelled
            or request.get('workspace_id') != copy.id
            or not isinstance(request.get('evidence'), str)
            or not request['evidence'].strip()
            or type(request.get('at')) not in (int, float)
            or not math.isfinite(request['at'])
            or ('discard_result' in request and type(request['discard_result']) is not bool)
            or (request.get('discard_result') and not request['cancelled'])
            or ('discarded' in request and type(request['discarded']) is not bool)
            or ('discarded' in request and 'result_schema' not in request)
            or ('result_schema' in request and request['result_schema'] not in (1, 2))
            or ('reason_code' in request and request['reason_code'] not in
                ('git_integrity_mismatch', 'capture_too_large',
                 'capture_unavailable', 'capture_failed'))
            or ('reason_code' in request and 'result_schema' not in request)
            or (request.get('result_schema') == 1
                and (request.get('discarded') is not False
                     or 'reason_code' in request))
            or (request.get('result_schema') == 2
                and (request.get('discarded') is not True
                     or request.get('reason_code') not in
                     ('git_integrity_mismatch', 'capture_too_large',
                      'capture_unavailable', 'capture_failed')
                     or request.get('discard_result') is not True))):
        raise CoordinationError('Finalization intent is missing or conflicts with this job.', 73)
    expected = {'project': str(root), 'task_id': task_id,
                'assignment_id': assignment_id, 'job_id': lease['job_id']}
    if any(key in request and request[key] != value for key, value in expected.items()):
        raise CoordinationError('Finalization intent identity does not match this job.', 73)
    durable = lease.get('finalization')
    if isinstance(durable, dict):
        requested_discard = bool(request.get('discard_result', False))
        if requested_discard != (durable.get('discard_result') is True):
            raise CoordinationError(
                'Finalization intent conflicts with the durable result handling.', 73)
        if ('discarded' in request and durable.get('discarded') is not None
                and bool(request['discarded']) != bool(durable['discarded'])):
            raise CoordinationError(
                'Finalization intent conflicts with the retained result schema.', 73)
        if ('result_schema' in request and durable.get('result_schema') is not None
                and request['result_schema'] != durable['result_schema']):
            raise CoordinationError(
                'Finalization intent conflicts with the retained result schema.', 73)
        if ('reason_code' in request and durable.get('reason_code') is not None
                and request['reason_code'] != durable['reason_code']):
            raise CoordinationError(
                'Finalization intent conflicts with the discard reason.', 73)


def _cleaned_job_evidence(copy, lease):
    """Validate retained schema 1/2 evidence without trusting a missing tree."""
    from . import workspace, workspace_gc, workspace_lifecycle as lifecycle

    cleanup = lease.get('cleanup')
    if (not isinstance(cleanup, dict) or cleanup.get('state') != 'cleaned'
            or (cleanup.get('device'), cleanup.get('inode')) !=
               (lease['work_device'], lease['work_inode'])
            or not re.fullmatch('[0-9a-f]{32}', str(cleanup.get('name', '')))):
        raise CoordinationError('Logical job has no matching completed cleanup intent.', 73)
    extras = cleanup.get('extras', [])
    if not isinstance(extras, list) or any(not workspace_gc._valid_extra(e) for e in extras):
        raise CoordinationError('Logical job cleanup environment history is malformed.', 73)
    with lifecycle.directory(copy) as fd:
        if lifecycle._exists(fd, 'work') or any(
                workspace_gc._extra_name(name) for name in os.listdir(fd)):
            raise CoordinationError('Cleaned job has replacement execution data; history preserved.', 73)
        try:
            result = workspace_gc.read_result(copy, fd=fd, lease=lease)
        except workspace.WorkspaceError as error:
            raise CoordinationError(str(error), error.code) from None
    if result is None:
        raise CoordinationError('Retained result does not identify the cleaned logical job.', 73)
    finalization = lease.get('finalization')
    if isinstance(finalization, dict) and finalization.get('result_schema') is not None:
        if finalization['result_schema'] != result['schema']:
            raise CoordinationError('Retained result schema conflicts with the logical job lease.', 73)
        if bool(finalization.get('discarded')) != (result['schema'] == 2):
            raise CoordinationError('Retained discard state conflicts with the logical job lease.', 73)
        if (result['schema'] == 2
                and finalization.get('reason_code') != result['capture']['reason_code']):
            raise CoordinationError('Retained discard reason conflicts with the logical job lease.', 73)
        if result['schema'] == 1 and finalization.get('reason_code') is not None:
            raise CoordinationError('A captured result cannot carry a discard reason.', 73)
    state = copy.directory.parent.parent
    with lifecycle.fence(state) as (root_fd, _):
        if lifecycle._exists(root_fd, 'quarantine'):
            qfd = state_storage.open_private_directory(state / 'quarantine', parent=root_fd)
            try:
                if any(lifecycle._exists(qfd, e['name']) for e in [cleanup, *extras]):
                    raise CoordinationError('Cleaned job still has quarantined execution data.', 73)
            finally:
                os.close(qfd)
    return {'schema': result['schema'], 'discarded': result['schema'] == 2,
            'reason_code': (result.get('capture') or {}).get('reason_code')}


def _replay_legacy_cancelled_finalization(root, task_id, assignment_id, copy,
                                          report):
    """Corroborate one completed no-execution cancellation without repairing it.

    The coordinator may have allocated at a newer source base than the task that
    owns the assignment. A normal authority repair would need a request and the
    original A == B baseline, so this narrow replay only accepts the exact
    publication already persisted for a terminal cleaned cancellation.
    """
    from . import workspace_gc, workspace_lifecycle as lifecycle

    task = load_task(root, task_id)
    row = _assignment(task, assignment_id)
    persisted = row.get('job_finalization')
    if persisted is None:
        return False
    lease = lifecycle.read(copy)
    if lease is None or lease.get('phase') != 'cancelled':
        return False
    cleanup = lease.get('cleanup')
    if not isinstance(cleanup, dict):
        raise CoordinationError('Legacy cancellation cleanup evidence is malformed.', 73)
    if cleanup.get('state') != 'cleaned':
        return False
    if (type(lease.get('schema')) is not int or lease['schema'] != 1
            or type(cleanup.get('device')) is not int
            or type(cleanup.get('inode')) is not int
            or cleanup['device'] < 0 or cleanup['inode'] < 0
            or not isinstance(cleanup.get('name'), str)
            or re.fullmatch('[0-9a-f]{32}', cleanup['name']) is None):
        raise CoordinationError('Legacy cancellation cleanup evidence is malformed.', 73)
    extras = cleanup.get('extras', [])
    if (not isinstance(extras, list) or any(
            not isinstance(extra, dict) or not isinstance(extra.get('name'), str)
            for extra in extras)):
        raise CoordinationError('Legacy cancellation cleanup evidence is malformed.', 73)
    if (lease.get('binding') is not None
            or lease.get('binding_history') not in (None, [])
            or lease.get('attempts') != []
            or lease.get('pending_attempt') is not None):
        return False
    lifecycle.authorize_finalization(lease)
    if not isinstance(report, dict) or report != persisted:
        raise CoordinationError(
            'Legacy cancellation replay does not match the saved publication.', 73)

    copy = workspace_gc._load_record(_state_root(), copy.id)
    if (task.get('id') != task_id or not _task_belongs_to(task, root)
            or copy.source.resolve() != root):
        raise CoordinationError('Legacy cancellation belongs to another project/task.', 78)
    task_head = task.get('base_head')
    if (not isinstance(task_head, str)
            or re.fullmatch('(?:[0-9a-f]{40}|[0-9a-f]{64})', task_head) is None):
        raise CoordinationError('Legacy cancellation task has no valid source baseline.', 73)
    if (row.get('workspace_id') != copy.id or row.get('job_id') != lease['job_id']
            or row.get('status') != 'failed'
            or row.get('error_kind') not in ('environment', 'cancelled')
            or row.get('failure_stage') != 'preparation'
            or row.get('job_finalization_request') is not None
            or row.get('correction_of') is not None
            or row.get('started_at') is not None
            or row.get('attempt_id') is not None
            or row.get('execution_snapshot') is not None
            or row.get('attempt_history') not in (None, [])
            or row.get('worker_changes') not in (None, [])
            or row.get('checks') not in (None, [])):
        raise CoordinationError(
            'Legacy cancellation assignment has conflicting execution history.', 73)
    disposition = row.get('disposition')
    if (not isinstance(disposition, dict) or disposition.get('kind') != 'rejected'
            or not isinstance(disposition.get('evidence'), str)
            or not disposition['evidence'].strip()):
        raise CoordinationError('Legacy cancellation has no rejected disposition.', 73)
    item = assignment_deliverable(task, assignment_id)
    if item.get('correction_of') is not None:
        raise CoordinationError('Legacy cancellation is linked to a correction.', 73)

    metadata = copy.metadata
    base_head = metadata.get('base_head')
    if (copy.metadata.get('coord_task') != task_id
            or copy.metadata.get('coord_assignment') != assignment_id
            or metadata.get('job_id') != lease['job_id']
            or metadata.get('work_inode') != lease['work_inode']
            or not isinstance(base_head, str)
            or re.fullmatch('(?:[0-9a-f]{40}|[0-9a-f]{64})', base_head) is None
            or metadata.get('source_head') not in (None, base_head)
            or type(metadata.get('attempt')) is not int or metadata['attempt'] != 0
            or metadata.get('attempt_id') is not None
            or metadata.get('started_at') is not None
            or metadata.get('operation') is not None):
        raise CoordinationError(
            'Legacy cancellation workspace identity or baseline is inconsistent.', 73)
    if os.path.lexists(copy.path):
        raise CoordinationError(
            'Legacy cancellation workspace has replacement execution data; history preserved.', 73)

    finalization = lease.get('finalization')
    if finalization is not None and (not isinstance(finalization, dict)
            or finalization.get('cancelled') is not True
            or finalization.get('discard_result') is not False
            or type(finalization.get('result_schema')) is not int
            or finalization['result_schema'] != 1
            or finalization.get('discarded') is not False
            or finalization.get('reason_code') is not None):
        raise CoordinationError('Legacy cancellation lease decision is inconsistent.', 73)
    cleaned = _cleaned_job_evidence(copy, lease)
    if cleaned['schema'] != 1 or cleaned['discarded']:
        raise CoordinationError('Legacy cancellation retained result is not schema 1.', 73)
    with lifecycle.directory(copy) as fd:
        result = workspace_gc.read_result(copy, fd=fd, lease=lease)
    patch = result.get('patch') if isinstance(result, dict) else None
    if (not isinstance(result, dict) or type(result.get('schema')) is not int
            or result['schema'] != 1
            or result.get('source') != str(copy.source)
            or result.get('workspace_id') != copy.id
            or result.get('base_head') != base_head
            or result.get('job_id') not in (None, lease['job_id'])
            or result.get('attempt_id') is not None
            or not isinstance(patch, dict)
            or type(patch.get('bytes')) is not int or patch['bytes'] != 0
            or patch.get('sha256') != hashlib.sha256(b'').hexdigest()):
        raise CoordinationError(
            'Legacy cancellation retained result does not prove an empty capture.', 73)

    allowed = {'id', 'workspace_id', 'job_id', 'action', 'reason',
               'allocated_bytes', 'inodes', 'cancelled', 'discarded',
               'result_schema', 'reason_code', 'evidence', 'at'}
    if (set(report) - allowed
            or type(report.get('id')) is not str or report['id'] != copy.id
            or type(report.get('job_id')) is not str
            or report['job_id'] != lease['job_id']
            or ('workspace_id' in report
                and (type(report['workspace_id']) is not str
                     or report['workspace_id'] != copy.id))
            or type(report.get('action')) is not str
            or report['action'] not in ('cleaned', 'already-cleaned')
            or ('cancelled' in report and report['cancelled'] is not True)
            or ('discarded' in report and report['discarded'] is not False)
            or ('result_schema' in report
                and (type(report['result_schema']) is not int
                     or report['result_schema'] != 1))
            or report.get('reason_code') is not None
            or not isinstance(report.get('evidence'), str)
            or not report['evidence'].strip()):
        raise CoordinationError(
            'Legacy cancellation publication is malformed or changes its decision.', 73)
    for key in ('allocated_bytes', 'inodes'):
        if key in report and (type(report[key]) is not int or report[key] < 0):
            raise CoordinationError('Legacy cancellation publication usage is malformed.', 73)
    if ('reason' in report and (not isinstance(report['reason'], str)
                                or not report['reason'].strip())
            or ('at' in report and (type(report['at']) not in (int, float)
                                    or not math.isfinite(report['at'])))):
        raise CoordinationError('Legacy cancellation publication metadata is malformed.', 73)
    return True


def _preparation_only_history(copy, lease):
    """Accept actual preparation attempts without fabricating execution history."""
    attempts = lease['attempts']
    if any(entry.get('operation') != 'preparation' or entry.get('status') == 'running'
           for entry in attempts):
        return False
    count = copy.metadata.get('attempt', 0)
    if type(count) is not int or count != len(attempts):
        return False
    expected = attempts[-1]['id'] if attempts else None
    return (copy.metadata.get('attempt_id') == expected
            and (not attempts or copy.metadata.get('operation') == 'preparation'))


def _finalization_authority(root, task_id, assignment_id, copy, *,
                            cancelled=None, recover_binding=False, discarding=None):
    """Resolve publication authority before cleanup and again before publication.

    Caller holds the owner lock, lifecycle fence and project ledger lock. A
    proposed legacy binding is returned in memory; nothing is repaired until
    every identity and history check has succeeded.
    """
    from . import workspace_lifecycle as lifecycle

    task = load_task(root, task_id)
    row = _assignment(task, assignment_id)
    lease = lifecycle.read(copy)
    if (task.get('id') != task_id or not _task_belongs_to(task, root)
            or copy.source.resolve() != root):
        raise CoordinationError('Logical job belongs to another project/task.', 78)
    if (lease is None or row.get('workspace_id') != copy.id
            or row.get('job_id') != lease['job_id']
            or copy.metadata.get('job_id') != lease['job_id']
            or copy.metadata.get('work_inode') != lease['work_inode']):
        raise CoordinationError('Logical job workspace/job identity does not match.', 73)
    lifecycle.authorize_finalization(lease)
    terminal = lease['phase'] in lifecycle.FINAL_PHASES
    evidence = None
    if terminal:
        if cancelled is not None and (lease['phase'] == 'cancelled') != cancelled:
            raise CoordinationError('Finalization cannot change the original terminal decision.', 73)
        finalization = lease.get('finalization')
        if isinstance(finalization, dict):
            if finalization.get('cancelled') != (lease['phase'] == 'cancelled'):
                raise CoordinationError('Lease finalization conflicts with its terminal decision.', 73)
            if discarding is True and finalization.get('discard_result') is not True:
                raise CoordinationError(
                    'Lease finalization conflicts with the requested destructive handling.', 73)
        if lease.get('cleanup', {}).get('state') == 'cleaned':
            evidence = _cleaned_job_evidence(copy, lease)
            if (isinstance(finalization, dict)
                    and finalization.get('discarded') is not None
                    and bool(finalization['discarded']) != evidence['discarded']):
                raise CoordinationError('Lease finalization conflicts with its retained result schema.', 73)
    binding = lease.get('binding')
    repaired = False
    if binding is None:
        # This narrowly repairs the historical pre-model failure. Derive the
        # candidate from durable metadata, never from the supplied assignment ID.
        candidate = {'task_id': copy.metadata.get('coord_task'),
                     'assignment_id': copy.metadata.get('coord_assignment')}
        if (not recover_binding or candidate != {'task_id': task_id, 'assignment_id': assignment_id}
                or row.get('status') != 'failed' or row.get('failure_stage') != 'preparation'
                or row.get('attempt_id') or row.get('started_at') or row.get('execution_snapshot')
                or not _preparation_only_history(copy, lease)
                or lease.get('pending_attempt')
                or row.get('attempt_history')
                or lease.get('binding_history', []) not in ([], [candidate])
                or (terminal and (lease['phase'] != 'cancelled'
                    or lease.get('cleanup', {}).get('state') != 'cleaned'))):
            raise CoordinationError('Missing binding has insufficient or conflicting preparation evidence.', 73)
        if terminal:
            _finalization_request(row.get('job_finalization_request'), root, task_id,
                                  assignment_id, copy, lease,
                                  lease['phase'] == 'cancelled')
        lease = dict(lease)
        lifecycle._record_binding(lease, candidate)
        binding, repaired = candidate, True
    if (not isinstance(binding, dict) or set(binding) != {'task_id', 'assignment_id'}
            or not _CORRECTION_TASK_ID.fullmatch(str(binding.get('task_id', '')))
            or not _CORRECTION_ASSIGNMENT_ID.fullmatch(str(binding.get('assignment_id', '')))):
        raise CoordinationError('Logical job has no authoritative assignment binding.', 73)
    metadata_binding = {'task_id': copy.metadata.get('coord_task'),
                        'assignment_id': copy.metadata.get('coord_assignment')}
    if any(value is not None for value in metadata_binding.values()) and metadata_binding != binding:
        raise CoordinationError('Workspace metadata conflicts with the authoritative assignment binding.', 73)
    chain = _logical_job_chain(root, binding['task_id'], binding['assignment_id'])
    if (task_id, assignment_id) not in chain:
        raise CoordinationError('Requested assignment is not in this logical job lineage.', 64)
    history = lease.get('binding_history', [])
    chronological = [{'task_id': t, 'assignment_id': a} for t, a in reversed(chain)]
    if not isinstance(history, list) or (history and history != chronological[-len(history):]):
        raise CoordinationError('Logical job binding history conflicts with its lineage.', 73)
    known = {entry['id'] for entry in lease['attempts']}
    pending = lease.get('pending_attempt')
    if pending is not None:
        if (not isinstance(pending, dict) or set(pending) != {'task_id', 'assignment_id', 'id'}
                or any(pending.get(k) != v for k, v in binding.items())
                or not re.fullmatch('[0-9a-f]{32}', str(pending.get('id', '')))):
            raise CoordinationError('Logical job has conflicting execution reservation history.', 73)
        known.add(pending['id'])
    if copy.metadata.get('attempt_id') and copy.metadata['attempt_id'] not in known:
        raise CoordinationError('Workspace execution identity is absent from the job history.', 73)
    for chain_task_id, chain_assignment_id in chain:
        chain_task = load_task(root, chain_task_id)
        chain_row = _assignment(chain_task, chain_assignment_id)
        if chain_task.get('id') != chain_task_id or not _task_belongs_to(chain_task, root):
            raise CoordinationError('Logical job lineage belongs to another project/task.', 78)
        if chain_task.get('base_head') != (copy.metadata.get('base_head') or copy.metadata.get('source_head')):
            raise CoordinationError('Logical job lineage has a conflicting source baseline.', 73)
        attempts = chain_row.get('attempt_history', [])
        if not isinstance(attempts, list):
            raise CoordinationError('Logical job assignment history is malformed.', 73)
        snapshots = [chain_row, *attempts]
        for snapshot in snapshots:
            if (not isinstance(snapshot, dict)
                    or snapshot.get('workspace_id') not in (None, copy.id)
                    or snapshot.get('job_id') not in (None, lease['job_id'])
                    or (snapshot.get('attempt_id') and snapshot['attempt_id'] not in known)):
                raise CoordinationError('Logical job assignment/attempt history conflicts with its lease.', 73)
            context = snapshot.get('execution_snapshot')
            if context is not None:
                execution = context.get('execution') if isinstance(context, dict) else None
                if (not isinstance(execution, dict)
                        or execution.get('workspace_id') not in (None, copy.id)
                        or execution.get('job_id') not in (None, lease['job_id'])
                        or (execution.get('attempt_id') and execution['attempt_id'] not in known)):
                    raise CoordinationError('Execution snapshot conflicts with the logical job history.', 73)
        if chain_row.get('workspace_id') != copy.id or chain_row.get('job_id') != lease['job_id']:
            raise CoordinationError('Logical job lineage changed workspace/job identity.', 73)
        publication = chain_row.get('job_finalization')
        if publication is not None:
            decision = (lease['phase'] == 'cancelled') if terminal else bool(cancelled)
            if (not isinstance(publication, dict)
                    or publication.get('id') != copy.id
                    or publication.get('job_id') != lease['job_id']
                    or publication.get('workspace_id', copy.id) != copy.id
                    or publication.get('cancelled', decision) != decision
                    or publication.get('action') not in ('cleaned', 'already-cleaned')
                    or ('discarded' in publication
                        and type(publication['discarded']) is not bool)
                    or (publication.get('discarded') and not publication.get('cancelled'))):
                raise CoordinationError('Existing finalization conflicts with this logical job.', 73)
            if evidence is not None:
                if bool(publication.get('discarded', False)) != evidence['discarded']:
                    raise CoordinationError('Existing finalization conflicts with the retained result schema.', 73)
                if ('result_schema' in publication
                        and publication['result_schema'] != evidence['schema']):
                    raise CoordinationError('Existing finalization conflicts with the retained result schema.', 73)
                if (evidence['discarded']
                        and publication.get('reason_code') not in (None, evidence['reason_code'])):
                    raise CoordinationError('Existing finalization conflicts with the discard reason.', 73)
                if (not evidence['discarded']
                        and publication.get('reason_code') is not None):
                    raise CoordinationError('A captured result cannot carry a discard reason.', 73)
        request = chain_row.get('job_finalization_request')
        if request is not None:
            decision = (lease['phase'] == 'cancelled') if terminal else bool(cancelled)
            _finalization_request(request, root, chain_task_id, chain_assignment_id,
                                  copy, lease, decision)
    return lease, chain, repaired


def _reject_discarded_chain(root: Path, chain: list, evidence: str, *,
                            request: dict | None = None) -> None:
    """Reconcile every current disposition under an explicit job discard intent.

    Callers hold the project ledger lock. Each write is replayable after a
    partial cross-task publication, while previous decisions and grades remain
    in their append-only histories.
    """
    quality = {
        'grade': 'unassessable', 'attribution': 'unknown',
        'evidence': 'The uncapturable result was explicitly discarded; no quality assessment is possible.',
    }
    for chain_task_id, chain_assignment_id in chain:
        task = load_task(root, chain_task_id)
        row = _assignment(task, chain_assignment_id)
        intent_added = request is not None and not row.get('job_finalization_request')
        if intent_added:
            row['job_finalization_request'] = dict(
                request, task_id=chain_task_id, assignment_id=chain_assignment_id)
        if (row.get('disposition') or {}).get('kind') == 'rejected':
            if intent_added:
                task['updated_at'] = time.time()
                _atomic(_task_path(root, chain_task_id), task)
            continue
        if row.get('disposition'):
            row.setdefault('disposition_history', []).append(dict(row['disposition']))
        row['disposition'] = {
            'kind': 'rejected', 'evidence': evidence[:2000], 'at': time.time()}
        if row.get('routing_features'):
            _queue_feedback(
                task, row, action='worker', outcome='rejected', cost_usd=None,
                features=row['routing_features'],
                decision_id=row.get('routing_decision_id'),
                started_at=row.get('started_at', task['created_at']), quality=quality)
        task['updated_at'] = time.time()
        _atomic(_task_path(root, chain_task_id), task)


def _publish_job_finalization(root: Path, task_id: str, assignment_id: str,
                              report: dict) -> None:
    """Publish only a validated, cleaned job; interrupted publication is replayable."""
    from . import workspace, workspace_gc, workspace_lifecycle as lifecycle

    if not isinstance(report, dict):
        raise CoordinationError('Logical job finalization publication is malformed.', 73)
    root = _project_root(root)
    row = _assignment(load_task(root, task_id), assignment_id)
    identifier = row.get('workspace_id')
    try:
        copy = workspace_gc._load_record(_state_root(), identifier)
        with ExitStack() as owned:
            with lifecycle.fence(_state_root()):
                owned.enter_context(workspace_gc._owner(copy))
            with lifecycle.fence(_state_root()), _lock(root):
                discarding = (report.get('discarded')
                              if type(report.get('discarded')) is bool else None)
                if _replay_legacy_cancelled_finalization(
                        root, task_id, assignment_id, copy, report):
                    return
                lease, chain, _ = _finalization_authority(
                    root, task_id, assignment_id, copy, discarding=discarding)
                if lease['phase'] not in lifecycle.FINAL_PHASES:
                    raise CoordinationError('Logical job is not finalized; publication is deferred.', 75)
                evidence = _cleaned_job_evidence(copy, lease)
                for key, value in (('id', copy.id), ('workspace_id', copy.id),
                                   ('job_id', lease['job_id'])):
                    if key in report and report[key] != value:
                        raise CoordinationError('Finalization publication belongs to another logical job.', 73)
                if report.get('cancelled', lease['phase'] == 'cancelled') != (lease['phase'] == 'cancelled'):
                    raise CoordinationError('Finalization publication changes the terminal decision.', 73)
                if ('discarded' in report
                        and bool(report['discarded']) != evidence['discarded']):
                    raise CoordinationError('Finalization publication conflicts with the retained result.', 73)
                if ('result_schema' in report
                        and report['result_schema'] != evidence['schema']):
                    raise CoordinationError('Finalization publication conflicts with the retained result.', 73)
                if (evidence['discarded']
                        and report.get('reason_code') not in (None, evidence['reason_code'])):
                    raise CoordinationError('Finalization publication conflicts with the discard reason.', 73)
                if (not evidence['discarded'] and report.get('reason_code') is not None):
                    raise CoordinationError('A captured result cannot carry a discard reason.', 73)
                existing_reports, requests = [], []
                for chain_task_id, chain_assignment_id in chain:
                    chain_row = _assignment(load_task(root, chain_task_id), chain_assignment_id)
                    if chain_row.get('job_finalization') is not None:
                        existing_reports.append(chain_row['job_finalization'])
                    if chain_row.get('job_finalization_request') is not None:
                        requests.append(chain_row['job_finalization_request'])
                if not existing_reports and not requests:
                    raise CoordinationError('Logical job has no explicit finalization intent.', 73)
                if existing_reports and any(item != existing_reports[0]
                                            for item in existing_reports[1:]):
                    raise CoordinationError('Linked finalization publications are inconsistent.', 73)
                publication = dict(existing_reports[0] if existing_reports else report)
                publication.update(id=copy.id, workspace_id=copy.id, job_id=lease['job_id'])
                publication['discarded'] = evidence['discarded']
                publication['result_schema'] = evidence['schema']
                if evidence['discarded']:
                    publication['reason_code'] = evidence['reason_code']
                else:
                    publication.pop('reason_code', None)
                publication.setdefault('cancelled', lease['phase'] == 'cancelled')
                publication.setdefault('at', time.time())
                if evidence['discarded']:
                    _reject_discarded_chain(root, chain, publication.get('evidence',
                                             'Explicit logical job discard'))
                for chain_task_id, chain_assignment_id in chain:
                    chain_task = load_task(root, chain_task_id)
                    chain_row = _assignment(chain_task, chain_assignment_id)
                    changed = False
                    if chain_row.get('job_finalization') is None:
                        chain_row['job_finalization'] = dict(publication)
                        changed = True
                    if chain_row.pop('job_finalization_request', None) is not None:
                        changed = True
                    if changed:
                        chain_task['updated_at'] = time.time()
                        _atomic(_task_path(root, chain_task_id), chain_task)
    except (workspace.WorkspaceError, state_storage.StorageError) as error:
        raise CoordinationError(str(error), error.code) from None
    if evidence['discarded']:
        for chain_task_id in {identifier for identifier, _assignment_id in chain}:
            sync_routing_feedback(root, chain_task_id)


def _reconcile_job_publications(root: Path, task_id: str) -> None:
    """Repair a publication interrupted after one task ledger was written."""
    from . import workspace, workspace_gc, workspace_lifecycle

    project_root = _project_root(root)
    task = load_task(project_root, task_id)
    for row in task.get('assignments', []):
        if row.get('job_finalization'):
            _publish_job_finalization(
                project_root, task_id, row['id'], row['job_finalization'])
            continue
        identifier = row.get('workspace_id')
        if not isinstance(identifier, str) or not re.fullmatch('[0-9a-f]{32}', identifier):
            continue
        try:
            copy = workspace_gc._load_record(_state_root(), identifier)
            if copy.source.resolve() != project_root:
                continue
            lease = workspace_lifecycle.read(copy)
        except workspace.WorkspaceError:
            continue
        if lease is None or lease['phase'] not in workspace_lifecycle.FINAL_PHASES:
            continue
        request = row.get('job_finalization_request')
        if request is not None:
            if not isinstance(request, dict):
                raise CoordinationError('Malformed saved finalization intent; history preserved.', 73)
            binding = lease.get('binding')
            if (isinstance(binding, dict)
                    and set(binding) == {'task_id', 'assignment_id'}
                    and binding != {'task_id': task_id, 'assignment_id': row['id']}):
                latest = _assignment(load_task(project_root, binding['task_id']),
                                     binding['assignment_id'])
                latest_request = latest.get('job_finalization_request')
                if not isinstance(latest_request, dict):
                    raise CoordinationError('Latest correction has no saved finalization intent.', 73)
                finalize_assignment(
                    project_root, binding['task_id'], binding['assignment_id'],
                    latest_request.get('evidence'),
                    cancelled=latest_request.get('cancelled'),
                    discard_result=latest_request.get('discard_result') is True)
                continue
            finalize_assignment(
                project_root, task_id, row['id'], request.get('evidence'),
                cancelled=request.get('cancelled'),
                discard_result=request.get('discard_result') is True)
            continue
        binding = lease.get('binding')
        if (not isinstance(binding, dict)
                or set(binding) != {'task_id', 'assignment_id'}):
            continue
        try:
            chain = _logical_job_chain(
                project_root, binding['task_id'], binding['assignment_id'])
        except CoordinationError:
            continue
        report = None
        for chain_task_id, chain_assignment_id in chain:
            chain_row = _assignment(
                load_task(project_root, chain_task_id), chain_assignment_id)
            if chain_row.get('job_finalization'):
                report = chain_row['job_finalization']
                break
        if report is not None:
            _publish_job_finalization(
                project_root, binding['task_id'], binding['assignment_id'], report)


def resolve_correction_workspace(root: Path, task_id: str, assignment_id: str, *,
                                 explicit_workspace=None):
    """Return the predecessor copy for a linked correction, otherwise ``None``."""
    task = load_task(root, task_id)
    row = _assignment(task, assignment_id)
    item = assignment_deliverable(task, assignment_id)
    reference = item.get('correction_of')
    if reference is None:
        return None
    copy = _correction_workspace(
        root, task, _plan_metadata(reference, 'correction_of'),
        explicit_workspace=explicit_workspace,
        continuation_assignment=(assignment_id if row.get('status') in ('failed', 'running')
                                 else None),
        start_assignment=assignment_id)
    if row.get('workspace_id') and row['workspace_id'] != copy.id:
        raise CoordinationError('Correction assignment is already bound to a different workspace.', 64)
    return copy


def _overlapping_rework_matches(root: Path, task: dict, item: dict, *,
                                exclude_assignment_id: str | None = None):
    """Return unresolved same-task rework workspaces that overlap one item.

    A saved workspace ID with a missing or unreadable record is unresolved, not
    evidence that the job was finalized. Only a durable final lease (or an
    explicit finalization publication) permits independent overlapping work.
    """
    from . import workspace, workspace_gc, workspace_lifecycle

    project_root = _project_root(root)
    matches = []
    for row in task.get('assignments', []):
        if row.get('id') == exclude_assignment_id:
            continue
        if ((row.get('disposition') or {}).get('kind') != 'needs-rework'
                or row.get('job_finalization')):
            continue
        prior_item = _deliverable(task, row.get('deliverable_id'))
        if prior_item is None:
            continue
        if not any(scope_matching.mutation_overlaps(root, prior_scope, scope)
                   for prior_scope in prior_item.get('scope', [])
                   for scope in item.get('scope', [])):
            continue
        identifier = row.get('workspace_id')
        if not isinstance(identifier, str) or not re.fullmatch('[0-9a-f]{32}', identifier):
            raise CoordinationError(
                f"Reviewed needs-rework assignment {row.get('id')} has no recoverable "
                'execution workspace; inspect it before overlapping work. A missing copy '
                'is not proof that the logical job was finalized.', 78)
        try:
            copy = workspace_gc._load_record(_state_root(), identifier)
            if copy.source.resolve() != project_root:
                raise CoordinationError(
                    'Reviewed needs-rework workspace belongs to another project.', 78)
            lease = workspace_lifecycle.read(copy)
        except workspace.WorkspaceError as error:
            raise CoordinationError(
                f'Reviewed needs-rework workspace {identifier} is unavailable: '
                f'{error.message}. Inspect it before overlapping work; a missing copy '
                'is not proof that the logical job was finalized.', error.code) from None
        if lease is None:
            raise CoordinationError(
                f'Reviewed needs-rework workspace {identifier} has no logical job lease; '
                'inspect it before overlapping work. A missing lease is not proof of '
                'finalization.', 73)
        if lease['phase'] in workspace_lifecycle.FINAL_PHASES:
            continue
        if (row.get('job_id') and row['job_id'] != lease['job_id']):
            raise CoordinationError(
                f'Reviewed needs-rework assignment {row.get("id")} no longer matches its '
                'logical job lease; inspect the ledger before overlapping work.', 64)
        try:
            copy.verify()
        except (workspace.WorkspaceError, OSError) as error:
            message = getattr(error, 'message', 'execution tree is unavailable')
            raise CoordinationError(
                f'Reviewed needs-rework workspace {identifier} is unavailable: {message}. '
                'Inspect it before overlapping work; a missing execution tree is not proof '
                'that the logical job was finalized.', getattr(error, 'code', 78)) from None
        matches.append((row, prior_item))
    return matches


def _reject_unlinked_overlaps(root: Path, task: dict, items: list[dict], *,
                              exclude_assignment_id: str | None = None) -> None:
    for item in items:
        matches = _overlapping_rework_matches(
            root, task, item, exclude_assignment_id=exclude_assignment_id)
        if not matches:
            continue
        labels = [f"{row.get('id')} ({row.get('deliverable_id')})"
                  for row, _prior in matches]
        if len(matches) > 1:
            raise CoordinationError(
                f"Ambiguous correction for new assignment {item['id']}: " +
                ', '.join(labels) +
                ' all have non-final needs-rework workspaces overlapping its scope; '
                'add correction_of for the authoritative predecessor.', 64)
        row = matches[0][0]
        raise CoordinationError(
            f"Reviewed needs-rework workspace from assignment {row.get('id')} overlaps "
            f"new assignment {item['id']}; add correction_of to link the authoritative "
            'predecessor (failed attempts require explicit resume).', 64)


def validate_assignment_start(root: Path, task_id: str, assignment_id: str, *,
                              explicit_workspace=None):
    """Validate continuity before a managed launch allocates or probes anything."""
    task = load_task(root, task_id)
    row = _assignment(task, assignment_id)
    if row.get('job_finalization_request'):
        raise CoordinationError('Logical job has a pending finalization intent; finish or explicitly cancel it before execution.', 73)
    item = assignment_deliverable(task, assignment_id)
    if item.get('correction_of') is not None:
        return resolve_correction_workspace(
            root, task_id, assignment_id, explicit_workspace=explicit_workspace)
    if (row.get('status') != 'running'
            and item.get('executor') in ('worker', 'auto')
            and item.get('kind') in WORKER_WRITE_KINDS):
        _reject_unlinked_overlaps(
            root, task, [item], exclude_assignment_id=row['id'])
    return None


def _validate_correction_plan(root: Path, task: dict, deliverables: list[dict]) -> None:
    """Validate durable links before routing or ledger mutation.

    The unlinked replacement check is intentionally bounded to this task: a new
    worker/auto write deliverable whose scope overlaps one or more reviewed
    needs-rework assignments that still have a non-final workspace lease. It
    rejects one candidate as an explicit-link omission and multiple candidates
    as ambiguous. Non-overlapping work remains independent, and cross-task
    corrections require an explicit ``correction_of`` reference.
    """
    started = _started_deliverable_ids(task)
    worker_deliverables = {row.get('deliverable_id')
                           for row in task.get('assignments', [])}
    references = {}
    for item in deliverables:
        reference = item.get('correction_of')
        if reference is None or item['id'] in started:
            continue
        key = (reference['task_id'], reference['assignment_id'])
        if key in references:
            raise CoordinationError(
                'Two correction deliverables cannot name the same predecessor.', 64)
        references[key] = item
    for (predecessor_task_id, predecessor_assignment_id), item in references.items():
        reference = {'task_id': predecessor_task_id,
                     'assignment_id': predecessor_assignment_id}
        for existing in task.get('deliverables', []):
            if (existing.get('id') != item['id']
                    and existing.get('id') in worker_deliverables
                    and existing.get('correction_of') == reference):
                raise CoordinationError(
                    'Correction predecessor already has a linked successor; link the latest correction assignment.',
                    64)
        _correction_workspace(root, task, reference)

    previous_ids = {item.get('id') for item in task.get('deliverables', [])}
    new_items = [item for item in deliverables
                 if item.get('id') not in previous_ids
                 and item.get('executor') in ('worker', 'auto')
                 and item.get('kind') in WORKER_WRITE_KINDS
                 and item.get('correction_of') is None]
    if not new_items:
        return
    _reject_unlinked_overlaps(root, task, new_items)


def _canonical_features(item, task, *, require_kind: bool = True):
    raw = item.get('features', {})
    if not isinstance(raw, dict):
        raise ValueError('features must be an object')
    defaults = {'kind': item['kind'], 'runtime': task.get('runtime', 'codex'),
                'effort': normalize_effort(task['policy'].get('effort')) or DEFAULT_EFFORT}
    features = validate_features({**defaults, **raw})
    if require_kind and features['kind'] != item['kind']:
        raise ValueError('feature kind differs from deliverable kind')
    return features


def _new_auto_feature_card_problems(task, deliverables):
    """Validate new ordinary substantial Auto cards before any routing side effect.

    Only cards that still request ``executor:auto`` are required to declare the
    complete semantic contract. Protected responsibilities, native exceptions,
    started deliverables and historical replay retain their existing behavior.
    Explicit ``unknown`` values satisfy presence and remain honest declarations.
    """
    started = _started_deliverable_ids(task)
    problems = []
    for item in deliverables:
        if (item.get('executor') != 'auto' or item.get('id') in started
                or item.get('kind') in PROTECTED_COORDINATOR_KINDS):
            continue
        item_id = item.get('id')
        raw = item.get('features')
        if raw is None:
            missing = list(REQUIRED_SEMANTIC_FIELDS)
        elif not isinstance(raw, dict):
            problems.append(_problem(
                item_id, 'features', 'invalid_type',
                f'{item_id}: features must be a structured feature card'))
            continue
        else:
            missing = missing_semantic_fields(raw)
        if missing:
            problems.append(_problem(
                item_id, 'features', 'missing_semantic_fields',
                f"{item_id}: missing semantic feature fields: {', '.join(missing)}"))
            continue
        defaults = {'kind': item['kind'], 'runtime': task.get('runtime', 'codex'),
                    'effort': normalize_effort(task['policy'].get('effort')) or DEFAULT_EFFORT}
        try:
            card = validate_features({**defaults, **raw})
        except RoutingError as error:
            problems.append(_problem(
                item_id, 'features', 'invalid_value',
                f'{item_id}: {error.message}'))
            continue
        if card['kind'] != item['kind']:
            problems.append(_problem(
                item_id, 'features.kind', 'invalid_value',
                f'{item_id}: feature kind must match deliverable kind'))
    return problems


def _logical_definition(item, task):
    """The canonical identity of every logical deliverable field.

    Derived routing decisions, assignment rows, results and dispatch history are
    deliberately absent. Optional fields are present with ``None`` so an omitted
    legacy field and an explicitly null field compare identically.
    """
    definition = {
        'id': item.get('id'),
        'kind': item.get('kind'),
        'scope': item.get('scope'),
        'acceptance': item.get('acceptance'),
        'dependencies': item.get('dependencies'),
        'checks': item.get('checks'),
        'checks_reusable': item.get('checks_reusable', False),
        'retention': item.get('retention'),
        'features': _canonical_features(item, task, require_kind=False),
        'requested_executor': _requested_executor(item),
        'integration_of': item.get('integration_of'),
        'write_scope': item.get('write_scope'),
        'decision_artifacts': item.get('decision_artifacts'),
        'correction_of': item.get('correction_of'),
        'delegation_reason': item.get('delegation_reason'),
        'native_exception': item.get('native_exception'),
    }
    return {field: definition[field] for field in LOGICAL_DEFINITION_FIELDS}


def _fallback_definition_value(item, field, task):
    if field == 'features':
        try:
            return _canonical_features(item, task, require_kind=False)
        except (RoutingError, KeyError, TypeError, ValueError):
            return item.get('features')
    if field == 'requested_executor':
        try:
            return _requested_executor(item)
        except (KeyError, TypeError):
            return item.get('executor')
    if field == 'checks_reusable':
        return item.get(field, False)
    return item.get(field)


def _definition_differences(incoming, saved, task) -> list[str]:
    """Changed logical keys in canonical order, using canonical comparisons."""
    try:
        new = _logical_definition(incoming, task)
        old = _logical_definition(saved, task)
    except (RoutingError, KeyError, TypeError, ValueError):
        return [
            field for field in LOGICAL_DEFINITION_FIELDS
            if _fallback_definition_value(incoming, field, task)
            != _fallback_definition_value(saved, field, task)
        ]
    return [field for field in LOGICAL_DEFINITION_FIELDS if new[field] != old[field]]


def _started_definition_differences(item, prior, task) -> list[str]:
    """Compare a started definition while preserving the allowed ``auto`` alias.

    A started worker may replay ``executor: auto`` (or its recorded executor)
    without being reported as a requested-executor change. A genuinely different
    explicit executor remains a changed ``requested_executor`` key.
    """
    candidate = dict(item)
    prior_requested = _requested_executor(prior)
    if (item.get('executor') == prior.get('executor')
            or (item.get('executor') == 'auto' and prior_requested == 'auto')):
        candidate['routing'] = {'requested_executor': prior_requested}
    return _definition_differences(candidate, prior, task)


def _started_definition_revision_problems(task, deliverables) -> list[dict]:
    """Collect duplicate, started-change and removal problems before routing."""
    problems = []
    previous = {item.get('id'): item for item in task.get('deliverables', [])}
    started = _started_deliverable_ids(task)
    seen = set()
    for item in deliverables:
        item_id = item.get('id')
        if item_id in seen:
            problems.append(_problem(
                item_id, 'id', 'duplicate',
                f'{item_id}: Duplicate deliverable identity.'))
            continue
        seen.add(item_id)
        prior = previous.get(item_id)
        if prior is None or item_id not in started:
            continue
        changed = _started_definition_differences(item, prior, task)
        if changed:
            keys = ', '.join(changed)
            for field in changed:
                problems.append(_problem(
                    item_id, field, 'immutable_definition',
                    f'{item_id}: started deliverable definition is immutable; '
                    f"changed keys: {keys}. Field '{field}' cannot change."))
    incoming = {item.get('id') for item in deliverables}
    missing = []
    for item in task.get('deliverables', []):
        item_id = item.get('id')
        if item_id in started and item_id not in incoming and item_id not in missing:
            missing.append(item_id)
    for row in task.get('assignments', []):
        item_id = row.get('deliverable_id')
        if item_id in started and item_id not in incoming and item_id not in missing:
            missing.append(item_id)
    for item_id in missing:
        problems.append(_problem(
            item_id, 'id', 'immutable_removal',
            f'Plan revision cannot remove started/completed deliverable {item_id}; '
            'add a new deliverable id for revised scope.'))
    return problems


def _same_logical_plan(task, deliverables, classification, small_evidence):
    """Compare the submitted distribution with the saved one before any side effect.

    Derived routing state, assignment rows, results and phase are deliberately
    excluded. The requested executor is compared instead of its resolved value,
    so an identical ``auto`` distribution remains identical after routing.
    """
    try:
        saved = [_logical_definition(item, task) for item in task.get('deliverables', [])]
        incoming = [_logical_definition(item, task) for item in deliverables]
    except (RoutingError, KeyError, TypeError, ValueError):
        return False
    return (task.get('classification') == classification
            and task.get('small_evidence') == small_evidence
            and saved == incoming)


def _plan_is_durable(task):
    """Only the post-routing publication proves a current plan is durable."""
    return task.get('routing_committed') is True


def _prepare_routing(root, task, items, service):
    previous = {item['id']: item for item in task.get('deliverables', [])}
    started = _started_deliverable_ids(task)
    ids = set()
    for item in items:
        if item['id'] in ids:
            _problems_error([_problem(
                item['id'], 'id', 'duplicate',
                f"{item['id']}: Duplicate deliverable identity.")])
        ids.add(item['id'])
        prior = previous.get(item['id'])
        if item['id'] in started and prior:
            # Revision validation already proved the logical definition unchanged.
            # Restore the saved derived routing/result binding byte-for-byte so the
            # original decision stays valid without a destructive rewrite.
            item['features'] = prior.get('features', item.get('features'))
            item['executor'] = prior['executor']
            item['routing'] = prior.get('routing')
            for key in ('routing_feedback', 'result', 'result_invalidated_at',
                        'native_dispatches', 'native_dispatch_started_at'):
                if key in prior:
                    item[key] = prior[key]
            continue
        raw = item.get('features', {})
        defaults = {'kind': item['kind'], 'runtime': task.get('runtime', 'codex'),
                    'effort': normalize_effort(task['policy'].get('effort')) or DEFAULT_EFFORT}
        if not isinstance(raw, dict):
            _problems_error([_problem(
                item['id'], 'features', 'invalid_type',
                f"{item['id']}: features must be a structured feature card.")])
        try:
            item['features'] = validate_features({**defaults, **raw})
        except RoutingError as error:
            _problems_error([_problem(
                item['id'], 'features', 'invalid_value',
                f"{item['id']}: {error.message}")])
        if item['features']['kind'] != item['kind']:
            _problems_error([_problem(
                item['id'], 'features.kind', 'invalid_value',
                f"{item['id']}: Feature kind must match deliverable kind.")])
        requested = item['executor']
        if requested == 'auto' and service.config()['mode'] != 'auto':
            _problems_error([_problem(
                item['id'], 'executor', 'invalid_routing_mode',
                f"{item['id']}: executor:auto requires routing mode auto. Explicitly choose "
                '`deepseek-team routing configure --mode auto`, or save a manual '
                '25/50/75 delegation profile before registering new work.')])
        from .routing_admission import manual_acceptance
        try:
            decision = service.predict(
                item['features'], access=task['policy']['effective_access'],
                binding=_binding(task['id'], item),
                verification_ready=bool(item['checks']) or manual_acceptance(item))
        except RoutingError as error:
            _problems_error([_problem(
                item['id'], 'routing', 'routing_error',
                f"{item['id']}: {error.message}")], code=error.code)
        if requested == 'auto':
            item['executor'] = 'worker' if decision['action'] == 'worker' else 'coordinator'
        item['routing'] = {'decision_id': decision['id'], 'requested_executor': requested,
                           'action': decision['action'], 'reason_codes': decision['reason_codes']}


def _valid_auto_decision(root, task, item):
    routing = item.get('routing') or {}
    if routing.get('requested_executor') != 'auto':
        return False
    service = RoutingService(root)
    decision_id = routing.get('decision_id')
    if not service.validate_plan_decision(
            decision_id, item.get('features'), _binding(task['id'], item),
            executor=item['executor']):
        return False
    decision = service.decision(decision_id)
    return item['executor'] == ('worker' if decision['action'] == 'worker' else 'coordinator')


@contextmanager
def _routing_batch(root):
    try:
        with RoutingService(root).batch() as service:
            yield service
    except RoutingError as error:
        raise CoordinationError(error.message, error.code) from None


def plan_task(root: Path, task_id: str, plan: dict) -> dict:
    deliverables, problems = _validate_plan_payload(plan)
    if problems:
        _problems_error(problems)
    classification = plan.get("classification")
    if classification == "small":
        small_problems = []
        if len(deliverables) != 1 or not plan.get("small_evidence"):
            small_problems.append(_problem(
                None, 'small_evidence', 'missing_field',
                'Small tasks require one deliverable and concrete small_evidence.'))
        if len(deliverables) == 1:
            scopes = deliverables[0].get("scope", [])
            if (len(scopes) != 1
                    or any("*" in value or "?" in value or "[" in value
                           for value in scopes)):
                small_problems.append(_problem(
                    deliverables[0].get('id'), 'scope', 'invalid_value',
                    'Small-task exemption requires one concrete non-wildcard scope; '
                    'otherwise register a substantial distribution.'))
        if small_problems:
            _problems_error(small_problems)
    root = _project_root(root)
    with _lock(root):
        task = load_task(root, task_id)
        if task.get('schema') != 2:
            raise CoordinationError(
                'Unsupported coordination plan format; existing records are preserved. '
                'Open a new task with the current package.', 78)
        replay = _same_logical_plan(task, deliverables, classification,
                                    plan.get("small_evidence"))
        if replay and _plan_is_durable(task):
            # An identical logical distribution is already durable. Do not open
            # the routing store, rewrite the ledger, reset phase or replace IDs.
            return task
        if (not replay and classification == "substantial"
                and task['policy'].get('delegation_level') == 'auto'):
            # A legacy/replayed plan may carry only the old canonical defaults, so
            # strict semantic completeness applies only to genuinely new work.
            card_problems = _new_auto_feature_card_problems(task, deliverables)
            if card_problems:
                _problems_error(
                    card_problems,
                    header=('New substantial Auto plans require complete semantic feature '
                            'cards. Declare the named fields in the features card; explicit '
                            'unknown is allowed when a value is honestly unknown.'))
        _validate_correction_plan(root, task, deliverables)
        revision_problems = _started_definition_revision_problems(task, deliverables)
        if revision_problems:
            _problems_error(revision_problems)
        if classification == "substantial" and task['policy'].get('delegation_level') == 'auto':
            # Reject an explicit executor bypass before any durable ledger record
            # or routing decision exists for this revision. Started deliverables
            # are immutable and keep their already-validated saved decision.
            bypasses = _auto_executor_issues(deliverables, _started_deliverable_ids(task))
            if bypasses:
                _problems_error(
                    [_problem(issue.split(':', 1)[0], 'executor', 'auto_bypass', issue)
                     for issue in bypasses],
                    header='Auto distribution cannot be bypassed.')
        with _routing_batch(root) as service:
            try:
                _prepare_routing(root, task, deliverables, service)
            except CoordinationError:
                raise
            except RoutingError as error:
                raise CoordinationError(error.message, error.code) from None
            previous = {a["deliverable_id"]: a for a in task.get("assignments", [])}
            started = _started_deliverable_ids(task)
            assignments = []
            for item in deliverables:
                prior = previous.get(item["id"])
                if item["executor"] != "worker":
                    if prior and prior.get('deliverable_id') in started:
                        raise CoordinationError(
                            "Plan revision cannot reassign a started/completed DeepSeek worker deliverable; "
                            "add a new deliverable id for the native/coordinator scope.", 64)
                    continue
                if item["id"] in previous:
                    prior_assignment = previous[item["id"]]
                    if prior_assignment.get('status') == 'planned' and item['id'] not in started:
                        if item.get('correction_of') is None:
                            prior_assignment.pop('correction_of', None)
                        else:
                            prior_assignment['correction_of'] = dict(item['correction_of'])
                    assignments.append(prior_assignment)
                    continue
                assignment_id = "as-" + hashlib.sha256(
                    f"{task_id}\0{item['id']}".encode()).hexdigest()[:16]
                assignment = {
                    "id": assignment_id, "deliverable_id": item["id"],
                    "status": "planned", "workspace_id": None, "runtime": None,
                    "effort": None, "prepared_changes": [], "worker_changes": [], "checks": [],
                    "result_summary": None, "disposition": None,
                }
                if item.get('correction_of') is not None:
                    assignment['correction_of'] = dict(item['correction_of'])
                assignments.append(assignment)
            task.pop('routing_committed', None)
            task['schema'] = 2
            task.update(classification=classification,
                        small_evidence=plan.get("small_evidence"),
                        deliverables=deliverables, assignments=assignments,
                        status="planned", updated_at=time.time())
            # Persist the ledger before the surrounding routing transaction commits.
            _atomic(_task_path(root, task_id), task)
        # The marker is published only after the routing transaction commits.
        # If the process stops in between, the next identical plan repairs it
        # instead of treating a partially durable distribution as a no-op.
        task['routing_committed'] = True
        _atomic(_task_path(root, task_id), task)
        return task


def _retention_ok(task: dict, deliverable: dict) -> bool:
    retention = deliverable.get("retention")
    if not isinstance(retention, dict):
        return False
    code = retention.get("code")
    evidence = retention.get("evidence")
    if not isinstance(evidence, str) or len(evidence.strip()) < 5:
        return False
    if code in TECHNICAL_RETENTION:
        return any(
            row.get("deliverable_id") == deliverable.get("id")
            and row.get("code") == code
            and row.get("source") == "runner"
            for row in task.get("constraints", [])
        )
    if code == "secret_or_signing":
        return (deliverable.get("kind") == "metadata"
                and retention.get("sensitive") is True)
    # Semantic "not separable" is represented by classification=small rather
    # than accepted as a deterministic fact on a substantial task.
    return False


def native_exception_issue(item) -> str | None:
    """Return an actionable issue for an unsupported native attestation."""
    try:
        validate_native_exception(item)
    except ValueError as error:
        return f"{item.get('id')}: {error}"
    return None


def native_deliverable_issues(task: dict) -> list[str]:
    issues = []
    for item in task.get('deliverables', []):
        if item.get('executor') != 'native-agent':
            continue
        issue = native_exception_issue(item)
        if issue:
            issues.append(issue)
    return issues


def validate_task(root: Path, task_id: str) -> list[str]:
    task = load_task(root, task_id)
    if task.get("classification") is None:
        return ["distribution plan is missing"]
    # Native attestations are validated for every classification, including
    # small tasks and ledger plans written before this check existed.
    native_issues = native_deliverable_issues(task)
    if task["classification"] == "small":
        return native_issues
    policy = task["policy"]
    level = policy['delegation_level']
    # A task's distribution profile is a snapshot across compaction. Current
    # access revocations still apply immediately and cannot force a writer.
    try:
        current = settings.resolve(root)
    except settings.SettingsError as error:
        raise CoordinationError(
            settings.access_read_failure_message(error), 78) from None
    access = _effective_task_access(task, current)
    issues = list(native_issues)
    worker_count = 0
    worker_write_count = 0
    eligible_count = 0
    eligible_write_count = 0
    for item in task.get("deliverables", []):
        kind, executor = item["kind"], item["executor"]
        if level == 'auto':
            # Forged or historical plans must not keep an explicit executor that
            # bypasses the saved routing decision; started deliverables are
            # validated through their recorded automatic decision instead.
            auto_issue = _auto_executor_issue(item, _requested_executor(item))
            if auto_issue and not _valid_started_legacy_executor(root, task, item):
                issues.append(auto_issue)
        if executor == 'worker' and kind in WORKER_WRITE_KINDS and access != 'full-access':
            issues.append(
                f"{item['id']}: " + settings.write_access_revoked_message(current))
        if (item.get('routing') or {}).get('requested_executor') == 'auto':
            try:
                valid_auto = _valid_auto_decision(root, task, item)
            except (RoutingError, KeyError):
                valid_auto = False
            if not valid_auto:
                issues.append(f"{item['id']}: automatic executor requires its original saved routing decision")
        if executor == "worker":
            worker_count += 1
            if kind in WORKER_WRITE_KINDS:
                worker_write_count += 1
        eligible = kind in WORKER_READ_KINDS if access == "read-only" else (
            kind in WORKER_WRITE_KINDS or kind in WORKER_READ_KINDS)
        if eligible:
            eligible_count += 1
            if kind in WORKER_WRITE_KINDS:
                eligible_write_count += 1
        if executor == "coordinator" and kind in PROTECTED_COORDINATOR_KINDS:
            continue
        if executor == "coordinator" and eligible:
            if access == "read-only" and kind in WORKER_WRITE_KINDS:
                continue
            if (level == 75 and not _retention_ok(task, item)
                    and not _current_cancellation(task, item)):
                issues.append(
                    f"{item['id']}: worker-eligible {kind} retained by coordinator without "
                    "a supported constraint. The router admission does not cancel the manual "
                    "75 retention rule; delegate this work, record a supported retention "
                    "constraint, or keep it within an accepted cancellation. Do not "
                    "reclassify an ordinary implementation as a protected kind to bypass "
                    "the rule.")
    if level == 75 and access == "full-access" and eligible_count and worker_count == 0:
        issues.append(
            "75/full-access requires worker assignment for separable worker-eligible "
            "deliverables; router admission does not cancel the manual 75 retention rule")
    if level == 50 and access == "full-access" and eligible_write_count and worker_write_count == 0:
        issues.append("50/full-access requires at least one worker implementation/test/docs slice when worker-eligible implementation exists")
    return issues


def _assignment(task: dict, assignment_id: str) -> dict:
    for row in task.get("assignments", []):
        if row.get("id") == assignment_id:
            return row
    raise CoordinationError("Unknown coordination assignment.", 64)


def _effective_task_access(task, current):
    policy = task['policy']
    sources = policy.get('sources', {})
    explicit_cli = (sources.get('access') == 'cli' or
                    (policy.get('access') == 'auto' and sources.get('delegation_level') == 'cli'))
    saved = task.get('saved_policy')
    if isinstance(saved, dict) and 'effort' in saved:
        saved = dict(saved, effort=normalize_policy_effort(saved['effort']))
    unchanged_cli_override = explicit_cli and current.as_dict() == saved
    if current.effective_access == 'read-only' and not unchanged_cli_override:
        return 'read-only'
    return policy['effective_access']


def _check_assignment_access(root, task, item):
    try:
        current = settings.resolve(root)
    except settings.SettingsError as error:
        raise CoordinationError(
            settings.access_read_failure_message(error), 78) from None
    if not current.enabled:
        raise CoordinationError('Delegation is disabled; no new worker assignment may start.', 69)
    access = _effective_task_access(task, current)
    if item['kind'] in WORKER_WRITE_KINDS and access != 'full-access':
        raise CoordinationError(settings.write_access_revoked_message(current), 78)


def assignment_queue_state(root: Path, task_id: str, assignment_id: str, state: str) -> None:
    """Expose scheduling without changing executor, retry history or start state.

    Only a still-planned assignment is schedulable. A rejected duplicate launch
    must never relabel a live or finished attempt, so any state update for a
    running, failed or succeeded row is a safe no-op that preserves the original
    queue state and terminal result.
    """
    if state not in ('waiting', 'ready', 'blocked', 'cancelled'):
        raise CoordinationError('Invalid worker queue state.', 64)
    with _lock(root):
        task = load_task(root, task_id)
        row = _assignment(task, assignment_id)
        if row.get('status') != 'planned':
            return
        now = time.time()
        row['queue_state'] = state
        row['queue_updated_at'] = now
        if state == 'waiting':
            row.setdefault('queued_at', now)
        task['updated_at'] = now
        _atomic(_task_path(root, task_id), task)


def _launch_owner_state(owner):
    """Prove a reservation owner dead without trusting a reusable PID alone."""
    from . import workspace_lifecycle
    if not workspace_lifecycle._valid_identity(owner):
        return 'unknown'
    try:
        return workspace_lifecycle.identity_state(owner)
    except (OSError, ValueError):
        return 'unknown'


def _valid_launch_reservation(reservation) -> bool:
    from . import workspace_lifecycle
    return (isinstance(reservation, dict)
            and set(reservation) >= {'token', 'generation', 'owner', 'created_at'}
            and isinstance(reservation.get('token'), str)
            and 0 < len(reservation['token']) <= 128
            and type(reservation.get('generation')) is int
            and reservation['generation'] > 0
            and workspace_lifecycle._valid_identity(reservation.get('owner'))
            and type(reservation.get('created_at')) in (int, float)
            and math.isfinite(reservation['created_at']))


def _same_reservation(left, right) -> bool:
    return (_valid_launch_reservation(left) and _valid_launch_reservation(right)
            and left['token'] == right['token']
            and left['generation'] == right['generation'])


def _launch_refusal(message: str) -> CoordinationError:
    return CoordinationError(message, 75)


def _stop_waiting_guidance(assignment_id: str) -> str:
    return (f'Assignment {assignment_id} has a live or uninspectable launch. Stop the waiting '
            'worker process, wait for it to exit, then retry coordination cancel. Cancellation '
            'does not terminate a process or mutate its active slot.')


def register_launch_reservation(root: Path, task_id: str, assignment_id: str, *,
                                resume: bool = False) -> dict:
    """Publish one pre-queue reservation under the task lock.

    The caller must not hold the queue allocator lock. The identity is captured
    before the reservation becomes durable so PID reuse can be rejected later.
    A live reservation from another process is a duplicate launch.
    """
    from . import workspace_lifecycle
    owner = workspace_lifecycle.process_identity(os.getpid())
    if owner is None:
        raise CoordinationError('Cannot identify the launching process; no reservation was written.', 78)
    root = _project_root(root)
    with _lock(root):
        task = load_task(root, task_id)
        row = _assignment(task, assignment_id)
        assignment_deliverable(task, assignment_id)
        if task.get('status') in ('completed', 'closed'):
            raise CoordinationError('Coordination task is already terminal; no launch is permitted.', 78)
        if row.get('job_finalization_request') or row.get('job_finalization'):
            raise CoordinationError('Logical job has a finalization intent; no launch is permitted.', 73)
        if row.get('error_kind') == 'cancelled' and row.get('failure_stage') == 'before_start':
            raise CoordinationError(
                'Assignment was cancelled before start; no queue ticket, copy or provider launch is permitted.',
                78)
        if row.get('status') not in ('planned', 'failed', 'running'):
            raise CoordinationError('Assignment is already terminal; no launch is permitted.', 78)
        if row.get('status') != 'planned' and not resume:
            raise CoordinationError('Only an explicit resume may launch a failed/running assignment.', 75)
        previous = row.get('launch_reservation')
        if previous is not None and not _valid_launch_reservation(previous):
            raise _launch_refusal(
                'The existing launch reservation is malformed or uninspectable. Stop the process '
                'and inspect the ledger before another launch.')
        generation = row.get('launch_generation', 0)
        if type(generation) is not int or generation < 0:
            raise _launch_refusal('Launch generation is malformed; stop the process and inspect the ledger.')
        if previous is not None:
            previous_state = _launch_owner_state(previous['owner'])
            same_owner = previous['owner'] == owner
            if previous_state == 'unknown':
                raise _launch_refusal(_stop_waiting_guidance(assignment_id))
            if previous_state == 'live' and not same_owner:
                raise _launch_refusal(_stop_waiting_guidance(assignment_id))
            if row.get('launch_phase') == LAUNCH_PHASE_ALLOCATION and row.get('status') == 'planned':
                raise CoordinationError(
                    'Launch crossed the allocation fence; C5 cancellation and a plain relaunch '
                    'are no longer available. Use the existing recovery/finalize path.', 75)
            if row.get('status') == 'running' and not resume:
                raise _launch_refusal(_stop_waiting_guidance(assignment_id))
            generation = max(generation, previous['generation'])
        except_generation = generation + 1
        now = time.time()
        reservation = {'schema': 1, 'task_id': task_id, 'assignment_id': assignment_id,
                       'token': uuid.uuid4().hex,
                       'generation': except_generation, 'owner': owner,
                       'created_at': now}
        if row.get('status') != 'planned':
            # An explicit resume of an already-started or previously failed job
            # keeps its history byte-for-byte unless that resume actually starts
            # a new execution attempt. The reservation is still durable while the
            # launcher is active, and release restores this pre-launch snapshot.
            reservation['resume_snapshot'] = {
                'status': row.get('status'),
                'attempt_id': row.get('attempt_id'),
                'started_at': row.get('started_at'),
                'generation_present': 'launch_generation' in row,
                'generation': row.get('launch_generation'),
            }
        else:
            row.pop('launch_phase', None)
            row['launch_phase'] = None
            row['queue_state'] = None
            row['queue_updated_at'] = now
            row.pop('queued_at', None)
        row['launch_generation'] = except_generation
        row['launch_reservation'] = reservation
        task['updated_at'] = now
        _atomic(_task_path(root, task_id), task)
        return reservation


def _validate_launch_reservation_locked(task, row, reservation):
    if not _valid_launch_reservation(reservation):
        raise CoordinationError('Launch reservation is malformed; no launch is permitted.', 78)
    if row.get('error_kind') == 'cancelled' and row.get('failure_stage') == 'before_start':
        raise CoordinationError(
            'Assignment was cancelled before start; no queue ticket, copy or provider launch is permitted.',
            78)
    current = row.get('launch_reservation')
    if not _same_reservation(current, reservation):
        raise _launch_refusal('Launch reservation changed; stop any stale waiter and launch again.')
    if (reservation.get('task_id') != task.get('id')
            or reservation.get('assignment_id') != row.get('id')):
        raise _launch_refusal('Launch reservation belongs to another assignment.')
    if row.get('launch_generation') != reservation['generation']:
        raise _launch_refusal('Launch generation changed; stop any stale waiter and launch again.')
    if row.get('status') not in ('planned', 'failed', 'running'):
        raise CoordinationError('Assignment is already terminal; no launch is permitted.', 78)
    return row


def validate_launch_reservation(root: Path, task_id: str, assignment_id: str,
                                reservation) -> dict:
    """Read-only reservation check used inside queue allocation callbacks."""
    task = load_task(root, task_id)
    row = _assignment(task, assignment_id)
    return _validate_launch_reservation_locked(task, row, reservation)


def revalidate_launch_reservation(root: Path, task_id: str, assignment_id: str,
                                  reservation) -> dict:
    """Serialize a queued launcher with cancellation after the queue lock is released."""
    root = _project_root(root)
    with _lock(root):
        task = load_task(root, task_id)
        row = _assignment(task, assignment_id)
        return _validate_launch_reservation_locked(task, row, reservation)


def begin_launch_allocation(root: Path, task_id: str, assignment_id: str,
                            reservation) -> dict:
    """Publish the durable fence before the first copy, import or binding.

    C5 cancellation refuses from this point even when no workspace ID has been
    published yet. Repeating the same reservation is idempotent for a managed
    launch that must enter several allocation branches.
    """
    root = _project_root(root)
    with _lock(root):
        task = load_task(root, task_id)
        row = _assignment(task, assignment_id)
        _validate_launch_reservation_locked(task, row, reservation)
        if row.get('launch_phase') == LAUNCH_PHASE_ALLOCATION:
            return task
        if row.get('launch_phase') is not None:
            raise CoordinationError('Launch phase is not preallocation; no copy may be created.', 75)
        if _cancel_touched_reason(row):
            # Allocation already happened (for example, an explicit resume of a
            # retained copy). C5 is already closed by that history; do not add
            # launch bookkeeping to the protected earlier attempt.
            return task
        now = time.time()
        row['launch_phase'] = LAUNCH_PHASE_ALLOCATION
        row['launch_allocation_started_at'] = now
        task['updated_at'] = now
        _atomic(_task_path(root, task_id), task)
        return task


def release_launch_reservation(root: Path, task_id: str, assignment_id: str,
                               reservation) -> dict | None:
    """Clear only this process's unused reservation; allocation history is retained."""
    if reservation is None:
        return None
    root = _project_root(root)
    with _lock(root):
        task = load_task(root, task_id)
        row = _assignment(task, assignment_id)
        current = row.get('launch_reservation')
        if not _same_reservation(current, reservation):
            return task
        now = time.time()
        snapshot = reservation.get('resume_snapshot')
        if snapshot is not None:
            unchanged = (row.get('status') == snapshot.get('status')
                         and row.get('attempt_id') == snapshot.get('attempt_id')
                         and row.get('started_at') == snapshot.get('started_at'))
            if unchanged and row.get('launch_phase') != LAUNCH_PHASE_ALLOCATION:
                row.pop('launch_reservation', None)
                if snapshot.get('generation_present'):
                    row['launch_generation'] = snapshot.get('generation')
                else:
                    row.pop('launch_generation', None)
                task['updated_at'] = now
                _atomic(_task_path(root, task_id), task)
            # If the attempt advanced or crossed the allocation fence, another
            # writer owns that history. A losing launcher must never rewrite it.
            return task
        if row.get('launch_phase') == LAUNCH_PHASE_ALLOCATION:
            return task
        if row.get('status') != 'planned':
            return task
        row.pop('launch_reservation', None)
        row['queue_state'] = 'blocked'
        row['queue_updated_at'] = now
        task['updated_at'] = now
        _atomic(_task_path(root, task_id), task)
        return task


def _cancel_touched_reason(row):
    if row.get('status') == 'running' or row.get('started_at') or row.get('attempt_id'):
        return 'assignment execution has started'
    phase = row.get('launch_phase')
    if phase == LAUNCH_PHASE_ALLOCATION:
        return 'the durable allocation fence was published'
    if phase is not None:
        return 'the launch phase is not preallocation'
    if row.get('workspace_id') or row.get('job_id'):
        return 'a workspace/job is already bound'
    if row.get('prepared_changes') or row.get('worker_changes') or row.get('checks'):
        return 'prepared or worker changes already exist'
    if row.get('execution_snapshot') or row.get('attempt_history'):
        return 'execution history already exists'
    if (row.get('finished_at') is not None or row.get('exit_code') is not None
            or row.get('failure_stage') or row.get('error_kind')
            or row.get('result_summary') or row.get('disposition')):
        return 'result or disposition history already exists'
    if row.get('job_finalization_request') or row.get('job_finalization'):
        return 'job finalization already exists'
    return None


def _queue_artifact_relation(artifact, task_id, assignment_id):
    binding = artifact.get('binding')
    if binding is None:
        return 'ambiguous'
    if (binding['task_id'], binding['assignment_id']) == (task_id, assignment_id):
        return 'related'
    return 'foreign'


def cancel_unstarted_assignment(root: Path, task_id: str, assignment_id: str,
                                evidence: str) -> dict:
    """C5 cancellation for one planned assignment that never started work."""
    if not isinstance(evidence, str) or not evidence.strip():
        raise CoordinationError('Cancellation requires nonempty evidence.', 64)
    evidence = evidence.strip()
    root = _project_root(root)

    with _lock(root):
        task = load_task(root, task_id)
        row = _assignment(task, assignment_id)
        assignment_deliverable(task, assignment_id)
        if (row.get('status') == 'failed' and row.get('error_kind') == 'cancelled'
                and row.get('failure_stage') == 'before_start'
                and (row.get('disposition') or {}).get('kind') == 'rejected'):
            return task
        if row.get('status') != 'planned':
            raise _launch_refusal(
                f'Assignment {assignment_id} is {row.get("status")}; C5 only cancels an untouched '
                'planned assignment. Stop any active launch and use the existing recovery/finalize path.')
        touched = _cancel_touched_reason(row)
        if touched:
            raise _launch_refusal(
                f'Assignment {assignment_id} cannot be C5-cancelled because {touched}. '
                'Stop any active process and use the existing recovery/finalize path.')
        initial_reservation = row.get('launch_reservation')
        if initial_reservation is not None and not _valid_launch_reservation(initial_reservation):
            raise _launch_refusal(
                'The launch reservation is malformed or uninspectable. Stop the process and '
                'inspect the ledger before cancellation.')

    try:
        from . import worker_slots
        artifacts = worker_slots.inspect_bindings(_state_root())
    except Exception as error:
        raise _launch_refusal(
            'Cannot inspect live queue tickets or worker slots safely; stop the waiting process '
            'and retry. ' + str(getattr(error, 'message', error))) from None

    if initial_reservation is not None:
        owner_state = _launch_owner_state(initial_reservation['owner'])
        if owner_state in ('live', 'unknown'):
            detail = 'live' if owner_state == 'live' else 'uninspectable'
            raise _launch_refusal(_stop_waiting_guidance(assignment_id) + f' Owner state is {detail}.')
    for artifact in artifacts:
        relation = _queue_artifact_relation(artifact, task_id, assignment_id)
        if relation == 'ambiguous':
            raise _launch_refusal(
                f'An ambiguous live legacy {artifact["kind"]} has no launch binding; stop the '
                'waiting process, wait for it to exit, then retry cancellation.')
        if relation == 'related':
            raise _launch_refusal(_stop_waiting_guidance(assignment_id))

    with _lock(root):
        task = load_task(root, task_id)
        row = _assignment(task, assignment_id)
        if (row.get('status') == 'failed' and row.get('error_kind') == 'cancelled'
                and row.get('failure_stage') == 'before_start'
                and (row.get('disposition') or {}).get('kind') == 'rejected'):
            return task
        if row.get('status') != 'planned' or _cancel_touched_reason(row):
            raise _launch_refusal(
                'Launch state changed during cancellation; exactly one operation may win. '
                'Stop the process and inspect the assignment before retrying.')
        current_reservation = row.get('launch_reservation')
        if current_reservation is not None and not _same_reservation(
                current_reservation, initial_reservation):
            raise _launch_refusal(
                'A new launch reservation won before cancellation; stop that process first.')
        now = time.time()
        tombstone = {
            'schema': 1, 'state': 'cancelled-before-start', 'at': now,
            'evidence': evidence[:2000],
        }
        if _valid_launch_reservation(initial_reservation):
            tombstone['reservation'] = dict(initial_reservation)
            tombstone['owner_state'] = _launch_owner_state(initial_reservation['owner'])
        row.update(
            status='failed', error_kind='cancelled', failure_stage='before_start',
            queue_state='finished', queue_updated_at=now, finished_at=now,
            result_summary=evidence[:4000],
            disposition={'kind': 'rejected', 'evidence': evidence[:2000], 'at': now},
            prepared_changes=[], worker_changes=[], checks=[],
            launch_phase=LAUNCH_PHASE_CANCELLED, launch_cancelled=tombstone,
        )
        row.pop('launch_reservation', None)
        task['updated_at'] = now
        _atomic(_task_path(root, task_id), task)
        return task


def _lessons_guard(action, *, code: int = 64):
    """Run one lessons service call, converting its errors to coordination errors."""
    try:
        return action()
    except lessons.LessonError as error:
        raise CoordinationError(str(error), code) from None


def _bounded_snapshot_value(value, *, depth: int = 4, text: int = 400, items: int = 50):
    """Deep-copy JSON-ish evidence with hard per-level bounds."""
    if isinstance(value, str):
        return value[:text]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if depth <= 0:
        return None
    if isinstance(value, dict):
        bounded = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= 20:
                break
            bounded[str(key)[:80]] = _bounded_snapshot_value(
                item, depth=depth - 1, text=text, items=items)
        return bounded
    if isinstance(value, (list, tuple)):
        return [_bounded_snapshot_value(item, depth=depth - 1, text=text, items=items)
                for item in list(value)[:items]]
    return str(value)[:text]


def _context_bytes(value) -> int:
    try:
        return len(json.dumps(value, sort_keys=True, separators=(",", ":"),
                              ensure_ascii=True, allow_nan=False, default=str).encode("utf-8"))
    except (TypeError, ValueError, RecursionError):
        raise CoordinationError("Outcome context must be JSON serializable.", 64) from None


def _bounded_outcome_context(context: dict, limit: int = OUTCOME_CONTEXT_LIMIT) -> dict:
    """Keep one outcome event inside the lessons context budget.

    Rule content is preserved whenever it fits; only a pathological ruleset is
    compacted to ids/when/trimmed text and explicitly flagged as truncated.
    """
    if _context_bytes(context) <= limit:
        return context
    context = dict(context)
    block = dict(context.get("lessons") or {})
    rules = []
    for rule in block.get("rules") or []:
        if not isinstance(rule, dict):
            continue
        rules.append({
            "id": str(rule.get("id"))[:lessons.MAX_IDENTIFIER],
            "when": _bounded_snapshot_value(rule.get("when") or {}, depth=2, text=120, items=10),
            "condition": str(rule.get("condition"))[:200],
            "action": str(rule.get("action"))[:200],
            "evidence": [str(case)[:2 * lessons.MAX_IDENTIFIER + 1]
                         for case in (rule.get("evidence") or [])][:lessons.MAX_EVIDENCE_CASES],
        })
    block["rules"] = rules
    block["rules_truncated"] = True
    context["lessons"] = block
    if _context_bytes(context) > limit:
        raise CoordinationError("Outcome context exceeds the lessons context budget.", 64)
    return context


def _execution_inputs(execution) -> dict:
    """Validate optional bounded pre-execution evidence supplied by the runner."""
    if execution is None:
        return {}
    if not isinstance(execution, dict):
        raise CoordinationError("Execution context must be an object.", 64)
    unknown = sorted(set(execution) - set(EXECUTION_CONTEXT_KEYS))
    if unknown:
        raise CoordinationError(
            "Execution context allows only " + ", ".join(EXECUTION_CONTEXT_KEYS) + ".", 64)
    value = {}
    digest = execution.get("prompt_sha256")
    if digest is not None:
        if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise CoordinationError(
                "prompt_sha256 must be the lowercase SHA-256 digest of the task prompt.", 64)
        value["prompt_sha256"] = digest
    brief = execution.get("prompt_brief")
    if brief is not None:
        if not isinstance(brief, str) or "\x00" in brief:
            raise CoordinationError("prompt_brief must be bounded text without NUL bytes.", 64)
        value["prompt_brief"] = brief[:600]
    for key in ("base_head", "git_digest", "model"):
        item = execution.get(key)
        if item is None:
            continue
        if (not isinstance(item, str) or not item.strip() or len(item) > 128
                or "\x00" in item):
            raise CoordinationError(f"{key} must be a bounded non-empty string.", 64)
        value[key] = item
    fingerprints = execution.get("prepared_fingerprints")
    if fingerprints is not None:
        if not isinstance(fingerprints, dict):
            raise CoordinationError("prepared_fingerprints must be an object.", 64)
        bounded = {}
        for name, fingerprint in list(fingerprints.items())[:200]:
            if (not isinstance(name, str) or not name or len(name) > 300 or "\x00" in name
                    or not isinstance(fingerprint, str) or not fingerprint
                    or len(fingerprint) > 128):
                raise CoordinationError(
                    "prepared_fingerprints entries must be bounded strings.", 64)
            bounded[name] = fingerprint
        value["prepared_fingerprints"] = bounded
    return value


def _declared_features(item) -> dict:
    return dict(item.get("features")) if isinstance(item.get("features"), dict) else {}


def _actual_features(item, runtime, effort, execution_inputs) -> dict:
    features = {"kind": item.get("kind")}
    features.update(_declared_features(item))
    features["runtime"] = runtime
    features["effort"] = effort or DEFAULT_EFFORT
    if execution_inputs.get("model"):
        features["model"] = execution_inputs["model"]
    return features


def _lessons_rules_block(snapshot) -> dict:
    """Preserve the exact validated rule snapshot inside the outcome context.

    The lessons service already caps rule identifiers, evidence case references
    and text at its own contract limits; those exact limits are reused here so a
    valid identifier or evidence reference is never silently altered. Larger
    values can only come from a malformed store and stay bounded.
    """
    rules = []
    for rule in snapshot.get("rules") or []:
        if not isinstance(rule, dict):
            continue
        rules.append({
            "id": str(rule.get("id"))[:lessons.MAX_IDENTIFIER],
            "when": _bounded_snapshot_value(rule.get("when") or {}, depth=2, text=120, items=10),
            "condition": str(rule.get("condition") or "")[:lessons.MAX_TEXT],
            "action": str(rule.get("action") or "")[:lessons.MAX_TEXT],
            "evidence": [str(case)[:2 * lessons.MAX_IDENTIFIER + 1]
                         for case in (rule.get("evidence") or [])][:lessons.MAX_EVIDENCE_CASES],
        })
    return rules


def _unknown_lessons_block() -> dict:
    return {"version": None, "rule_ids": [], "rules": [], "captured": False,
            "note": "unknown: this outcome predates pre-execution lesson snapshots"}


def _deliverable_snapshot(item) -> dict:
    declared = _declared_features(item)
    value = {"id": item.get("id"), "kind": item.get("kind"),
             "scope": _bounded_snapshot_value(item.get("scope") or [], text=300, items=200),
             "acceptance": _bounded_snapshot_value(item.get("acceptance") or []),
             "dependencies": _bounded_snapshot_value(item.get("dependencies") or []),
             "checks": _bounded_snapshot_value(item.get("checks") or []),
             "checks_reusable": item.get("checks_reusable", False),
             "features": _bounded_snapshot_value(declared) if declared else None}
    if item.get("correction_of") is not None:
        value["correction_of"] = dict(item["correction_of"])
    return value


def _execution_snapshot(root: Path, task: dict, item: dict, workspace_id: str, runtime: str,
                        prepared_changes, effort: str | None, execution, *,
                        started_at: float, check_timeout_seconds: float | None = None) -> dict:
    """Capture the immutable pre-execution context of one attempt.

    Records the original deliverable, actual runtime/effort, source/workspace and
    prepared evidence, and the advisory rules selected before execution. Missing
    values stay ``None``/unknown instead of being filled in from later state.
    """
    inputs = _execution_inputs(execution)
    matching = _actual_features(item, runtime, effort, inputs)
    snapshot = _lessons_guard(lambda: lessons.snapshot(root, matching))
    execution_block = {
        "workspace_id": workspace_id,
        "runtime": runtime,
        "effort": matching["effort"],
        "model": matching.get("model"),
        "source_head": task.get("base_head"),
        "base_head": inputs.get("base_head"),
        "git_digest": inputs.get("git_digest"),
        "prepared_changes": sorted(set(str(name)[:300] for name in prepared_changes))[:200],
        "prepared_fingerprints": inputs.get("prepared_fingerprints") or {},
        "prompt_sha256": inputs.get("prompt_sha256"),
        "prompt_brief": inputs.get("prompt_brief"),
        "started_at": started_at,
    }
    if check_timeout_seconds is not None:
        execution_block["check_timeout_seconds"] = check_timeout_seconds
    return {
        "deliverable": _deliverable_snapshot(item),
        "features": _bounded_snapshot_value(matching, text=200, items=50),
        "execution": execution_block,
        "lessons": {"version": snapshot["version"], "rule_ids": list(snapshot["rule_ids"]),
                    "rules": _lessons_rules_block(snapshot), "captured": True,
                    "advisory": True},
    }


def _validate_retained_results(root: Path, task_id: str, *,
                               allow_repairable_missing: bool = False) -> None:
    """Fail closed when any durable assignment result uses an unknown schema.

    Status and completion are read-only views, but they must not report a
    clean task when a schema 2/historical result is malformed, a discarded
    manifest claims a current patch, or a future schema is present. Missing
    workspace records and copies without a result stay outside this check.
    """
    from . import workspace as workspace_module
    from . import workspace_gc, workspace_lifecycle

    root = _project_root(root)
    task = load_task(root, task_id)
    seen = set()
    for row in task.get('assignments', []):
        identifier = row.get('workspace_id')
        if (not isinstance(identifier, str)
                or re.fullmatch('[0-9a-f]{32}', identifier) is None
                or identifier in seen):
            continue
        seen.add(identifier)
        try:
            copy = workspace_gc._load_record(_state_root(), identifier)
            if copy.source.resolve() != root:
                continue
            with workspace_lifecycle.directory(copy) as fd:
                lease = workspace_lifecycle.read(copy)
                result = workspace_gc.read_result(copy, fd=fd, lease=lease)
                if (result is None and isinstance(lease, dict)
                        and isinstance(lease.get('finalization'), dict)
                        and 'result_schema' in lease['finalization']):
                    repairable = (allow_repairable_missing
                                  and (lease.get('cleanup') or {}).get('state')
                                  != 'cleaned')
                    if not repairable:
                        raise CoordinationError(
                            'Terminal result manifest is missing; history preserved.', 73)
        except workspace_module.WorkspaceError as error:
            raise CoordinationError(str(error), error.code) from None
        except OSError:
            raise CoordinationError(
                'Cannot inspect a retained assignment result; history preserved.', 73) from None


def _changes_patch_reference(workspace_id):
    """Return a private reference to the current result patch, or None.

    A schema 2 discard keeps historical ``changes.patch`` bytes but never
    presents them as the current result. Unknown result schemas fail closed
    instead of inferring a patch from a filename.
    """
    if not isinstance(workspace_id, str) or re.fullmatch(r"[0-9a-f]{32}", workspace_id) is None:
        return None
    from . import workspace as workspace_module
    from . import workspace_gc, workspace_lifecycle
    try:
        copy = workspace_gc._load_record(_state_root(), workspace_id)
        lease = workspace_lifecycle.read(copy)
        with workspace_lifecycle.directory(copy) as fd:
            execution_present = workspace_lifecycle._exists(fd, 'work')
            result = workspace_gc.read_result(copy, fd=fd, lease=lease)
    except (workspace_module.WorkspaceError, OSError):
        return None
    if result is not None:
        if result.get('schema') != 1:
            return None
        patch = result.get('patch')
        if (not isinstance(patch, dict) or patch.get('file') != 'changes.patch'
                or type(patch.get('bytes')) is not int
                or not isinstance(patch.get('sha256'), str)):
            return None
        return {"file": "changes.patch", "workspace_id": workspace_id,
                "sha256": patch['sha256'], "bytes": patch['bytes']}
    if not execution_present:
        # A cleaned workspace without a schema 1 result has no current patch;
        # arbitrary retained bytes named changes.patch are not evidence.
        return None
    if isinstance(lease, dict):
        finalization = lease.get('finalization')
        if (lease.get('phase') in workspace_lifecycle.FINAL_PHASES
                or (isinstance(finalization, dict)
                    and (finalization.get('discard_result') is True
                         or 'result_schema' in finalization
                         or finalization.get('discarded') is not None))):
            return None
    try:
        descriptor = os.open(copy.directory / "changes.patch",
                             os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600):
            return None
        digest = hashlib.sha256()
        size = 0
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            digest.update(chunk)
    except OSError:
        return None
    finally:
        os.close(descriptor)
    return {"file": "changes.patch", "workspace_id": workspace_id,
            "sha256": digest.hexdigest(), "bytes": size}


def _outcome_context(task: dict, row: dict, item: dict, disposition: str,
                     quality: dict | None = None) -> dict:
    """Assemble one idempotent outcome context from the stored attempt snapshot.

    An explicit graded assessment is part of the context, so the lessons event
    key changes exactly when the assessment changes: an exact replay stays
    idempotent while a corrected assessment is appended as its own event.
    """
    snapshot = row.get("execution_snapshot")
    if isinstance(snapshot, dict):
        deliverable = snapshot.get("deliverable") or _deliverable_snapshot(item)
        features = dict(snapshot.get("features") or {})
        if not features:
            features = _actual_features(item, row.get("runtime") or "unknown",
                                        row.get("effort"), {})
        execution = dict(snapshot.get("execution") or {})
        lessons_block = dict(snapshot.get("lessons") or {}) or _unknown_lessons_block()
    else:
        deliverable = _deliverable_snapshot(item)
        features = _actual_features(item, row.get("runtime") or "unknown",
                                    row.get("effort"), {})
        execution = {
            "workspace_id": row.get("workspace_id"),
            "runtime": row.get("runtime"),
            "effort": row.get("effort"),
            "model": None,
            "source_head": task.get("base_head"),
            "base_head": None,
            "git_digest": None,
            "prepared_changes": sorted(set(row.get("prepared_changes") or [])),
            "prepared_fingerprints": {},
            "prompt_sha256": None,
            "prompt_brief": None,
            "started_at": row.get("started_at"),
        }
        lessons_block = _unknown_lessons_block()
    execution.update(
        finished_at=row.get("finished_at"),
        error_kind=row.get("error_kind"),
        exit_code=row.get("exit_code"),
        result_summary=_bounded_snapshot_value(row.get("result_summary")),
        checks=_bounded_snapshot_value(list(row.get("checks") or [])),
        worker_changes=_bounded_snapshot_value(list(row.get("worker_changes") or []),
                                               text=300, items=200),
        changes_patch=_changes_patch_reference(row.get("workspace_id")),
    )
    context = {
        "disposition": disposition,
        "features": features,
        "deliverable": deliverable,
        "execution": execution,
        "lessons": lessons_block,
    }
    if quality is not None:
        context["quality"] = quality
    return _bounded_outcome_context(context)


def sync_lessons_feedback(root: Path, task_id: str) -> None:
    """Replay the durable lessons outbox; SQLite event keys deduplicate replay."""
    with _lock(root):
        task = load_task(root, task_id)
        changed = False
        for owner in [*task.get("assignments", []), *task.get("deliverables", [])]:
            for entry in owner.get("lessons_feedback", []):
                if not isinstance(entry, dict) or entry.get("recorded"):
                    continue
                event = entry.get("event")
                if not isinstance(event, dict) or set(event) != {
                        "task_id", "assignment_id", "disposition", "evidence", "context", "rework"}:
                    raise CoordinationError(
                        "Unrecorded lessons feedback is malformed; inspect the task ledger.", 78)
                _lessons_guard(lambda event=event: lessons.record_outcome(
                    root, task_id=event["task_id"], assignment_id=event["assignment_id"],
                    disposition=event["disposition"], evidence=event["evidence"],
                    context=event["context"], rework=event.get("rework")))
                entry["recorded"] = True
                changed = True
        if changed:
            _atomic(_task_path(root, task_id), task)


def assignment_preparing(root: Path, task_id: str, assignment_id: str, copy) -> dict:
    """Bind an owned allocation before fallible preparation, without a start.

    The caller holds the copy owner lock, or the creation fence for a newly
    allocated copy. Association and execution have separate durable histories.
    """
    from . import workspace, workspace_lifecycle

    root = _project_root(root)
    with workspace_lifecycle.fence(copy.directory.parent.parent), _lock(root):
        task = load_task(root, task_id)
        row = _assignment(task, assignment_id)
        item = assignment_deliverable(task, assignment_id)
        if row.get('job_finalization_request'):
            raise CoordinationError('Logical job has a pending finalization intent; preparation cannot resume.', 73)
        if task.get('id') != task_id or not _task_belongs_to(task, root):
            raise CoordinationError('Preparation task belongs to another project.', 78)
        _check_assignment_access(root, task, item)
        if row.get('status') not in ('planned', 'failed', 'running') or row.get('job_finalization'):
            raise CoordinationError('Assignment cannot prepare a finalized or completed job.', 73)
        if copy.source.resolve() != root:
            raise CoordinationError('Preparation workspace belongs to another project.', 78)
        try:
            lease = workspace_lifecycle.read(copy)
            if (lease is None or copy.metadata.get('job_id') != lease['job_id']
                    or copy.metadata.get('work_inode') != lease['work_inode']
                    or row.get('workspace_id') not in (None, copy.id)
                    or row.get('job_id') not in (None, lease['job_id'])
                    or (copy.metadata.get('base_head') or copy.metadata.get('source_head'))
                       != task.get('base_head')):
                raise CoordinationError('Preparation workspace/job identity does not match.', 73)
            reference = item.get('correction_of')
            if reference is not None:
                # Validate the predecessor before moving its authoritative binding.
                _correction_workspace(root, task, reference, explicit_workspace=copy.id,
                    start_assignment=assignment_id,
                    continuation_assignment=(assignment_id if row.get('status') in
                                             ('failed', 'running') else None))
            lease = workspace_lifecycle.bind_assignment(
                copy, task_id, assignment_id, previous_binding=reference)
        except workspace.WorkspaceError as cause:
            raise CoordinationError(str(cause), cause.code) from None
        copy.metadata.update(coord_task=task_id, coord_assignment=assignment_id,
                             standalone_job=False)
        copy.save()
        if row.get('workspace_id') == copy.id and row.get('job_id') == lease['job_id']:
            return task
        row.update(workspace_id=copy.id, job_id=lease['job_id'])
        if reference is not None:
            row['correction_of'] = dict(reference)
        task['updated_at'] = time.time()
        _atomic(_task_path(root, task_id), task)
        return task


def _preserve_assignment_attempt(row: dict) -> None:
    row.setdefault('attempt_history', []).append({key: row.get(key) for key in (
        'status', 'workspace_id', 'runtime', 'effort', 'started_at',
        'finished_at', 'checks', 'disposition', 'error_kind', 'exit_code',
        'failure_stage', 'preparation_cause', 'routing_features',
        'routing_decision_id', 'execution_snapshot', 'worker_changes',
        'result_summary', 'baseline_checks', 'verification_cause',
        'prepared_input_changes', 'verification_runs', 'readiness_checks',
        'job_id', 'attempt_id', 'check_timeout_seconds')})


def assignment_started(root: Path, task_id: str, assignment_id: str,
                       workspace_id: str, runtime: str,
                       prepared_changes: list[str], effort: str | None = None,
                       execution: dict | None = None,
                       check_timeout_seconds: float | None = None, *,
                       resume_running: bool = False,
                       attempt_id: str | None = None) -> dict:
    """Start one attempt and snapshot its original context before execution.

    ``execution`` is optional, bounded pre-execution evidence supplied by the
    managed runner: the prompt digest and a redacted bounded brief, workspace
    base head/digest, prepared content fingerprints and the actual model. Unknown
    keys are rejected so raw prompts, credentials or model output can never be
    stored here. A resume of the same running attempt keeps its original
    snapshot; a failed explicit continuation keeps the earlier snapshot in
    ``attempt_history``. When the managed runner has already started the
    workspace execution attempt for its baseline, ``attempt_id`` adopts that
    exact current attempt instead of reserving a second one.
    """
    if check_timeout_seconds is not None:
        from . import check_evidence
        try:
            check_timeout_seconds = check_evidence.validate_check_timeout(
                check_timeout_seconds)
        except check_evidence.CheckEvidenceError as error:
            raise CoordinationError(error.message, error.code) from None
    if effort is not None:
        canonical_effort = normalize_effort(effort)
        if canonical_effort is None:
            raise CoordinationError('Worker effort must be low, high or max (legacy medium maps to high).', 64)
        effort = canonical_effort
    identity = None
    has_workspace = (isinstance(workspace_id, str)
                     and re.fullmatch('[0-9a-f]{32}', workspace_id))
    if attempt_id is not None:
        if (not has_workspace or not isinstance(attempt_id, str)
                or not re.fullmatch('[0-9a-f]{32}', attempt_id)):
            raise CoordinationError(
                'An existing execution attempt requires its exact workspace and attempt identity.', 64)
    from . import workspace, workspace_lifecycle
    # Match the owner -> lifecycle fence -> ledger order used by finalization.
    # No lease is reserved until access and routing have passed under this gate.
    fence = (workspace_lifecycle.fence(_state_root()) if has_workspace
             else nullcontext())
    with fence, _lock(root):
        task = load_task(root, task_id)
        row = _assignment(task, assignment_id)
        item = assignment_deliverable(task, assignment_id)
        correction = item.get('correction_of')
        if has_workspace:
            if row['status'] not in ('planned', 'failed', 'running'):
                raise CoordinationError('Assignment is already active or completed.', 64)
            if (row.get('status') == 'failed' and row.get('workspace_id')
                    and row['workspace_id'] != workspace_id):
                raise CoordinationError(
                    'A logical job continuation must reuse its original workspace.', 64)
            if row.get('job_finalization'):
                raise CoordinationError('This logical job was finalized; plan a new job.', 73)
        if row.get('job_finalization_request'):
            raise CoordinationError('Logical job has a pending finalization intent; execution cannot start.', 73)
        _check_assignment_access(root, task, item)

        copy = None
        if has_workspace:
            if correction is not None:
                try:
                    copy = resolve_correction_workspace(
                        root, task_id, assignment_id, explicit_workspace=workspace_id)
                except workspace.WorkspaceError as cause:
                    raise CoordinationError(str(cause), cause.code) from None
            else:
                try:
                    copy = workspace.load(_state_root(), workspace_id)
                except workspace.WorkspaceError:
                    # Legacy synthetic/nonlocal references have unknown identity. Do
                    # not fabricate provenance or rewrite their historic records.
                    copy = None
                if copy is not None:
                    if copy.source.resolve() != _project_root(root):
                        raise CoordinationError(
                            'Workspace belongs to another source project.', 78)
                    try:
                        previous = workspace_lifecycle.read(copy)
                        binding = previous.get('binding') if previous else None
                        if binding and binding != {'task_id': task_id,
                                                   'assignment_id': assignment_id}:
                            raise CoordinationError(
                                'A new assignment must declare correction_of to reuse another '
                                'assignment workspace.', 64)
                    except workspace.WorkspaceError as cause:
                        raise CoordinationError(str(cause), cause.code) from None
        if (correction is None and row.get('status') != 'running'
                and item.get('executor') in ('worker', 'auto')
                and item.get('kind') in WORKER_WRITE_KINDS):
            _reject_unlinked_overlaps(
                root, task, [item], exclude_assignment_id=assignment_id)

        with _routing_batch(root) as service:
            if row['status'] == 'running':
                # An exact replay returns the original assignment and attempt without
                # rewriting either durable record. Explicit resume_running is the only
                # path that reserves a new attempt.
                expected = dict(workspace_id=workspace_id, runtime=runtime,
                                prepared_changes=sorted(set(prepared_changes)))
                # A running row may predate canonical effort levels ('medium' == 'high').
                effort_matches = (normalize_effort(row.get('effort')) == effort
                                  or (effort is None and row.get('effort') is None))
                if (any(row.get(key) != value for key, value in expected.items())
                        or not effort_matches):
                    raise CoordinationError(
                        'Running assignment continuation must match its original workspace '
                        'and execution conditions.', 64)
                if row.get('routing_decision_id'):
                    try:
                        service.decision(row['routing_decision_id'])
                    except RoutingError as error:
                        if (error.code != 66 or (item.get('routing') or {}).get(
                                'requested_executor') == 'auto'):
                            raise CoordinationError(error.message, error.code) from None
                        # A manual start may have captured its decision in the
                        # interrupted transaction. Recreate before any execution.
                        decision = service.predict(
                            row['routing_features'],
                            access=task['policy']['effective_access'])
                        row['routing_decision_id'] = decision['id']
                        _atomic(_task_path(root, task_id), task)
                    try:
                        service.validate_start(
                            row['routing_decision_id'], already_running=True,
                            access=_effective_task_access(task, settings.resolve(root)))
                    except RoutingError as error:
                        raise CoordinationError(error.message, error.code) from None
                if (attempt_id is not None and resume_running
                        and row.get('attempt_id') == attempt_id):
                    raise CoordinationError(
                        'Explicit process resume requires a new execution attempt.', 64)
                if attempt_id is not None:
                    if copy is None:
                        raise CoordinationError(
                            'The running execution attempt is unavailable for adoption.', 73)
                    try:
                        workspace_lifecycle.prepare_attempt(
                            copy, task_id, assignment_id, reuse_attempt_id=attempt_id)
                        adopted = workspace_lifecycle.read(copy)['attempts'][-1]
                        if adopted.get('check_timeout_seconds') != check_timeout_seconds:
                            raise CoordinationError(
                                'Adopted execution attempt timeout conflicts with the assignment.', 73)
                        if correction is not None:
                            _correction_workspace(
                                root, task, _plan_metadata(correction, 'correction_of'),
                                explicit_workspace=workspace_id,
                                bound_assignment=assignment_id)
                    except workspace.WorkspaceError as cause:
                        raise CoordinationError(str(cause), cause.code) from None
                    if row.get('attempt_id') != attempt_id and not resume_running:
                        raise CoordinationError(
                            'A running assignment cannot adopt a different execution attempt '
                            'without explicit resume.', 64)
                if not resume_running:
                    return task
            if row['status'] not in ('planned', 'failed') and not resume_running:
                raise CoordinationError('Assignment is already active or completed.', 64)
            if row['status'] == 'failed' or (row['status'] == 'running' and resume_running):
                if row['status'] == 'failed' and row.get('disposition') is None:
                    raise CoordinationError(
                        'Review and disposition the failed attempt before explicit '
                        'continuation.', 64)
                if row.get('workspace_id') and row['workspace_id'] != workspace_id:
                    raise CoordinationError(
                        'A logical job continuation must reuse its original workspace; '
                        'new execution needs a separately planned job.', 64)

            if item.get('features'):
                declared = validate_features(item['features'])
                features = validate_features(dict(
                    item['features'], runtime=runtime,
                    effort=effort or DEFAULT_EFFORT, model='deepseek-flash'))
                auto = (item.get('routing') or {}).get('requested_executor') == 'auto'
                if auto and features != declared:
                    raise CoordinationError(
                        'Worker runtime/effort must match its automatic routing decision; '
                        'replan before starting.', 64)
                try:
                    decision = (service.decision(item['routing']['decision_id']) if auto else
                                service.predict(
                                    features, access=task['policy']['effective_access']))
                    if auto:
                        service.validate_start(
                            decision['id'],
                            access=_effective_task_access(task, settings.resolve(root)))
                except RoutingError as error:
                    raise CoordinationError(error.message, error.code) from None
                row['routing_features'] = features
                row['routing_decision_id'] = decision['id']

        if row['status'] == 'failed' or (row['status'] == 'running' and resume_running):
            _preserve_assignment_attempt(row)

        if copy is not None:
            # Recheck immediately before reservation; a concurrent access change
            # must not be converted into a pending attempt.
            _check_assignment_access(root, task, item)
            try:
                identity = workspace_lifecycle.prepare_attempt(
                    copy, task_id, assignment_id,
                    replace_pending=bool(resume_running and attempt_id is None),
                    reuse_attempt_id=attempt_id)
                if attempt_id is not None:
                    adopted = workspace_lifecycle.read(copy)['attempts'][-1]
                    if adopted.get('check_timeout_seconds') != check_timeout_seconds:
                        raise CoordinationError(
                            'Adopted execution attempt timeout conflicts with the assignment.', 73)
                if correction is not None:
                    # The predecessor and its pending/current attempt must still be
                    # the authoritative same-job binding after reservation.
                    _correction_workspace(
                        root, task, _plan_metadata(correction, 'correction_of'),
                        explicit_workspace=workspace_id,
                        bound_assignment=assignment_id)
            except workspace.WorkspaceError as cause:
                raise CoordinationError(str(cause), cause.code) from None

        now = time.time()
        snapshot = _execution_snapshot(root, task, item, workspace_id, runtime,
                                       prepared_changes, effort, execution, started_at=now,
                                       check_timeout_seconds=check_timeout_seconds)
        row.update(status='running', workspace_id=workspace_id, runtime=runtime,
                   effort=effort, prepared_changes=sorted(set(prepared_changes)),
                   started_at=now, disposition=None, error_kind=None, exit_code=None,
                   failure_stage=None, preparation_cause=None, queue_state='running',
                   execution_snapshot=snapshot, baseline_checks=[], verification_cause=None,
                   prepared_input_changes=[], verification_runs=[], readiness_checks=[])
        if correction is not None:
            row['correction_of'] = dict(correction)
        if check_timeout_seconds is not None:
            row['check_timeout_seconds'] = check_timeout_seconds
        if identity:
            row.update(job_id=identity[0], attempt_id=identity[1])
            snapshot['execution'].update(job_id=identity[0], attempt_id=identity[1])
        row.pop('finished_at', None)
        task.update(status='active', updated_at=time.time())
        try:
            _atomic(_task_path(root, task_id), task)
        except OSError:
            raise CoordinationError(
                'Cannot safely access routing state; check ownership, permissions '
                'and database integrity.', 78) from None
        return task


def assignment_finished(root: Path, task_id: str, assignment_id: str,
                        status: str, result_summary: str,
                        worker_changes: list[str], checks: list[dict], *,
                        error_kind: str | None = None, exit_code: int | None = None,
                        baseline_checks: list[dict] | None = None,
                        verification_cause: str | None = None,
                        prepared_input_changes: list[str] | None = None) -> dict:
    if status not in ("succeeded", "failed"):
        raise CoordinationError("Assignment result status must be succeeded or failed.", 64)
    # Reuse-bearing terminal rows share the strict publication contract used
    # for appended verification runs, before any durable assignment mutation.
    _validate_check_evidence([row for row in checks if isinstance(row, dict)
                              and ('reused' in row or 'reused_from' in row)])
    if baseline_checks is not None:
        _validate_check_evidence(baseline_checks)
    _validate_verification_cause(verification_cause)
    if prepared_input_changes is not None:
        _validate_prepared_input_changes(prepared_input_changes)
    with _lock(root):
        task = load_task(root, task_id)
        row = _assignment(task, assignment_id)
        if row.get('status') != 'running':
            raise CoordinationError('Only a running assignment can record a terminal result.', 64)
        if status == 'failed' and error_kind is None:
            error_kind = 'verification' if any(check.get('exit_code') for check in checks) else 'execution'
        if error_kind not in (None, 'verification', 'provider', 'execution', 'environment', 'cancelled'):
            raise CoordinationError('Unknown worker failure classification.', 64)
        row.update(status=status, result_summary=str(result_summary)[:4000],
                   worker_changes=sorted(set(worker_changes)), checks=list(checks),
                   finished_at=time.time(), error_kind=error_kind, exit_code=exit_code,
                   queue_state='finished')
        if baseline_checks is not None:
            row['baseline_checks'] = list(baseline_checks)
        if baseline_checks is not None or verification_cause is not None:
            row['verification_cause'] = verification_cause
        if prepared_input_changes is not None:
            row['prepared_input_changes'] = sorted(set(prepared_input_changes))
        if status == 'failed' and row.get('routing_features'):
            # A failed project check is operational evidence, not a reviewed
            # worker quality grade. Missing environment or an already-red
            # baseline must not create a rejection/cooldown before review.
            outcome = {'verification': 'unknown', 'provider': 'infrastructure',
                       'environment': 'infrastructure', 'cancelled': 'cancelled'}.get(error_kind, 'unknown')
            _queue_feedback(task, row, action='worker', outcome=outcome, cost_usd=None,
                            features=row['routing_features'], decision_id=row.get('routing_decision_id'),
                            started_at=row.get('started_at', task['created_at']))
        task["updated_at"] = time.time()
        _atomic(_task_path(root, task_id), task)
    sync_routing_feedback(root, task_id)
    return load_task(root, task_id)


def _validate_verification_cause(cause) -> None:
    if cause not in (None, 'unknown', 'baseline_failure', 'regression_candidate',
                     'environment', 'timeout', 'launch_failure', 'probe_failure'):
        raise CoordinationError('Unknown verification evidence cause.', 64)


def _validate_prepared_input_changes(names) -> None:
    if not isinstance(names, list) or len(names) > 10000:
        raise CoordinationError('Prepared input changes must be a bounded path list.', 64)
    for name in names:
        if (not isinstance(name, str) or not name or len(name) > 4096 or
                Path(name).is_absolute() or '..' in Path(name).parts or
                '.git' in Path(name).parts or any(char in name for char in '\0\r\n') or
                '.' in name.split('/')):
            raise CoordinationError('Prepared input evidence contains an unsafe path.', 64)


def _validate_check_evidence(rows) -> None:
    allowed = {'command', 'exit_code', 'phase', 'stdout_path', 'stderr_path',
               'stdout_truncated', 'stderr_truncated', 'diagnostic_directory',
               'duration_seconds', 'error_kind', 'launch_error', 'timed_out',
               'launch_failed', 'stdout_sha256', 'stderr_sha256', 'reused', 'reused_from',
               'check_timeout_seconds', 'effective_timeout_seconds'}
    if not isinstance(rows, list) or len(rows) > 1000:
        raise CoordinationError('Verification evidence must be a bounded check list.', 64)
    for row in rows:
        if (not isinstance(row, dict) or set(row) - allowed or
                not isinstance(row.get('command'), str) or not row['command'].strip() or
                '\0' in row['command'] or
                type(row.get('exit_code')) is not int):
            raise CoordinationError('Invalid verification check evidence.', 64)
        for field in ('stdout_truncated', 'stderr_truncated', 'timed_out', 'launch_failed'):
            if field in row and type(row[field]) is not bool:
                raise CoordinationError('Verification flags must be boolean.', 64)
        if 'reused' in row and type(row['reused']) is not bool:
            raise CoordinationError('Verification reused flag must be boolean.', 64)
        if 'reused_from' in row and row.get('reused') is not True:
            raise CoordinationError('Reuse provenance requires a reused result.', 64)
        if row.get('reused') is True:
            if (not isinstance(row.get('reused_from'), str) or
                    re.fullmatch(r'[0-9a-f]{32}', row['reused_from']) is None or
                    row['exit_code'] != 0 or
                    any(row.get(flag) is not False for flag in
                        ('stdout_truncated', 'stderr_truncated', 'timed_out', 'launch_failed')) or
                    any(not row.get(field) for field in
                        ('stdout_path', 'stderr_path', 'stdout_sha256', 'stderr_sha256'))):
                raise CoordinationError('Reused verification requires complete successful evidence.', 64)
        for field in ('stdout_sha256', 'stderr_sha256'):
            if field in row and (not isinstance(row[field], str) or
                    re.fullmatch(r'[0-9a-f]{64}', row[field]) is None):
                raise CoordinationError('Invalid verification output fingerprint.', 64)
        if 'duration_seconds' in row and (type(row['duration_seconds']) not in (int, float) or
                not math.isfinite(row['duration_seconds']) or row['duration_seconds'] < 0):
            raise CoordinationError('Invalid verification duration.', 64)
        if 'check_timeout_seconds' in row and (
                isinstance(row['check_timeout_seconds'], bool)
                or not isinstance(row['check_timeout_seconds'], (int, float))
                or not 0 < row['check_timeout_seconds'] <= 86400):
            raise CoordinationError('Invalid verification check timeout.', 64)
        if 'effective_timeout_seconds' in row:
            if 'check_timeout_seconds' not in row:
                raise CoordinationError('Invalid effective verification check timeout.', 64)
            from . import check_evidence
            try:
                check_evidence.validate_effective_timeout(
                    row['effective_timeout_seconds'], row['check_timeout_seconds'])
            except check_evidence.CheckEvidenceError:
                raise CoordinationError(
                    'Invalid effective verification check timeout.', 64) from None
        for field in ('phase', 'error_kind', 'launch_error'):
            if field in row and (not isinstance(row[field], str) or len(row[field]) > 512 or
                    any(char in row[field] for char in '\0\r\n')):
                raise CoordinationError('Invalid verification diagnostic metadata.', 64)
        for field in ('stdout_path', 'stderr_path', 'diagnostic_directory'):
            value = row.get(field)
            if value is not None and (not isinstance(value, str) or not value or
                    Path(value).is_absolute() or '..' in Path(value).parts):
                raise CoordinationError('Verification diagnostic path must be relative.', 64)
    try:
        size = len(json.dumps(rows, ensure_ascii=True).encode())
    except (TypeError, ValueError):
        raise CoordinationError('Invalid verification evidence values.', 64) from None
    if size > OUTCOME_CONTEXT_LIMIT:
        raise CoordinationError('Verification evidence is too large.', 64)


def record_verification(root: Path, task_id: str, assignment_id: str, run: dict,
                        *, finished_at: float | None = None) -> dict:
    """Append check-only evidence without rewriting an attempt or quality case."""
    allowed = {'id', 'workspace_id', 'status', 'checks', 'verification_cause',
               'prepared_input_changes', 'at', 'baseline_checks'}
    if (not isinstance(run, dict) or set(run) - allowed or
            not isinstance(run.get('id'), str) or
            re.fullmatch(r'[A-Za-z0-9_.-]{1,128}', run['id']) is None or
            run.get('status') not in ('passed', 'failed')):
        raise CoordinationError('Invalid verification-only result.', 64)
    _validate_check_evidence(run.get('checks'))
    if not run['checks'] or (run['status'] == 'failed') != any(row['exit_code'] for row in run['checks']):
        raise CoordinationError('Verification status must match nonempty check evidence.', 64)
    if 'at' in run and (type(run['at']) not in (int, float) or
            not math.isfinite(run['at']) or run['at'] < 0):
        raise CoordinationError('Invalid verification timestamp.', 64)
    if 'baseline_checks' in run:
        _validate_check_evidence(run['baseline_checks'])
    _validate_verification_cause(run.get('verification_cause'))
    _validate_prepared_input_changes(run.get('prepared_input_changes', []))
    if finished_at is not None and (type(finished_at) not in (int, float) or
            not math.isfinite(finished_at) or finished_at < 0):
        raise CoordinationError('Invalid verification attempt identity.', 64)
    with _lock(root):
        task = load_task(root, task_id)
        row = _assignment(task, assignment_id)
        # A pending publication belongs to the observed attempt, even if an
        # explicit continuation has since moved that attempt into history.
        if finished_at is not None and row.get('finished_at') != finished_at:
            matches = [entry for entry in row.get('attempt_history', [])
                       if entry.get('finished_at') == finished_at and
                       entry.get('workspace_id') == run.get('workspace_id')]
            if len(matches) != 1:
                raise CoordinationError('Verification attempt identity is unavailable.', 64)
            row = matches[0]
        if row.get('status') not in ('succeeded', 'failed'):
            raise CoordinationError('Verification-only evidence requires a terminal assignment.', 75)
        if run.get('workspace_id') != row.get('workspace_id'):
            raise CoordinationError('Verification evidence belongs to a different workspace.', 64)
        history = row.setdefault('verification_runs', [])
        existing = next((entry for entry in history if entry.get('id') == run['id']), None)
        if existing is not None:
            if existing != run:
                raise CoordinationError('Verification run identity already has different evidence.', 64)
            return task
        history.append(dict(run))
        task['updated_at'] = time.time()
        _atomic(_task_path(root, task_id), task)
        return task


def assignment_preparation_failed(root: Path, task_id: str, assignment_id: str,
                                  result_summary: str, *,
                                  exit_code: int = 78,
                                  workspace_id: str | None = None,
                                  runtime: str | None = None,
                                  effort: str | None = None,
                                  cause: str = 'environment',
                                  baseline_checks: list[dict] | None = None,
                                  verification_cause: str | None = None,
                                  readiness_checks: list[dict] | None = None,
                                  attempt_id: str | None = None,
                                  check_timeout_seconds: float | None = None,
                                  resume_after_failure: bool = False) -> dict:
    """Close a still-planned assignment that failed before any model execution.

    The managed runner calls this when preparation of an owned copy (sandbox,
    declared dependencies or the provider boundary) fails before the runtime
    starts. No model ran, so this never records a start time, duration, cost,
    quality outcome or routing observation: the attempt stays dispositionable
    through :func:`use_result` and the task can then be completed or replanned.

    The default transition applies only to a planned assignment. An explicit
    continuation from a failed row additionally requires the exact new failed
    lease attempt and its reviewed predecessor, preserved in attempt history.
    A planned row records its first preparation failure through the default
    transition, including when the caller explicitly enabled recovery.
    A runner ``environment_incompatible`` constraint is recorded on the
    deliverable in the same atomic update, except for cancellation (130); an
    already-recorded specific technical constraint is never duplicated. Admission
    refusals are infrastructure outcomes too, but grant no technical-retention
    exception: revoking permissions or timing out cannot justify bypassing policy.
    When a baseline already owned a workspace execution attempt, its exact
    identity and timeout are mirrored here without claiming that the assignment
    itself started or that the model ran.
    """
    if isinstance(exit_code, bool) or not isinstance(exit_code, int):
        raise CoordinationError('Preparation failure requires an integer exit code.', 64)
    if type(resume_after_failure) is not bool:
        raise CoordinationError('Preparation resume flag must be boolean.', 64)
    if cause not in ('environment', 'admission'):
        raise CoordinationError('Unknown preparation failure cause.', 64)
    _validate_verification_cause(verification_cause)
    if baseline_checks is not None:
        _validate_check_evidence(baseline_checks)
    if readiness_checks is not None:
        _validate_check_evidence(readiness_checks)
    if (attempt_id is None) != (check_timeout_seconds is None):
        raise CoordinationError(
            'Preparation attempt identity and timeout must be recorded together.', 64)
    if attempt_id is not None:
        if (not isinstance(attempt_id, str)
                or not re.fullmatch('[0-9a-f]{32}', attempt_id)
                or not isinstance(workspace_id, str)
                or not re.fullmatch('[0-9a-f]{32}', workspace_id)):
            raise CoordinationError(
                'Preparation attempt identity requires its exact workspace.', 64)
        from . import check_evidence
        try:
            check_timeout_seconds = check_evidence.validate_check_timeout(
                check_timeout_seconds)
        except check_evidence.CheckEvidenceError as error:
            raise CoordinationError(error.message, error.code) from None
    canonical_effort = None
    if effort is not None:
        canonical_effort = normalize_effort(effort)
        if canonical_effort is None:
            raise CoordinationError('Worker effort must be low, high or max (legacy medium maps to high).', 64)
    if runtime is not None and (not isinstance(runtime, str) or not runtime.strip()):
        raise CoordinationError('Worker runtime must be a non-empty runtime name.', 64)
    cancelled = exit_code == 130
    summary = str(result_summary)[:4000]
    identity = None
    if isinstance(workspace_id, str) and re.fullmatch('[0-9a-f]{32}', workspace_id):
        from . import workspace, workspace_gc, workspace_lifecycle
        if os.path.lexists(_state_root() / 'workspaces' / workspace_id):
            try:
                copy = workspace_gc._load_record(_state_root(), workspace_id)
                if copy.source.resolve() != _project_root(root):
                    raise CoordinationError('Preparation workspace belongs to another project.', 78)
                lease = workspace_lifecycle.read(copy)
                if lease:
                    expected = {'task_id': task_id, 'assignment_id': assignment_id}
                    metadata_binding = {'task_id': copy.metadata.get('coord_task'),
                                        'assignment_id': copy.metadata.get('coord_assignment')}
                    if (lease.get('binding') == expected
                            or (lease.get('binding') is None and metadata_binding == expected)):
                        if attempt_id is not None:
                            attempts = lease.get('attempts', [])
                            latest = attempts[-1] if attempts else None
                            if (lease.get('binding') != expected
                                    or lease.get('pending_attempt')
                                    or not isinstance(latest, dict)
                                    or latest.get('id') != attempt_id
                                    or latest.get('operation') != 'execution'
                                    or latest.get('generation') != lease['generation']
                                    or latest.get('check_timeout_seconds')
                                       != check_timeout_seconds
                                    or copy.metadata.get('attempt_id') != attempt_id
                                    or copy.metadata.get('job_id') != lease['job_id']
                                    or copy.metadata.get('work_inode') != lease['work_inode']):
                                raise CoordinationError(
                                    'Preparation attempt does not match the retained workspace.',
                                    73)
                            identity = lease['job_id'], attempt_id
                        else:
                            identity = lease['job_id'], None
                    else:
                        # A refused association must not attach an unrelated
                        # standalone/predecessor copy merely from the CLI ID.
                        if attempt_id is not None:
                            raise CoordinationError(
                                'Preparation attempt belongs to another assignment.', 73)
                        workspace_id = None
            except workspace.WorkspaceError as error:
                raise CoordinationError(str(error), error.code) from None
    if resume_after_failure and (attempt_id is None or identity is None):
        raise CoordinationError('Resumed baseline failure requires its exact execution attempt.', 73)
    fence = (workspace_lifecycle.fence(_state_root()) if resume_after_failure
             else nullcontext())
    with fence, _lock(root):
        task = load_task(root, task_id)
        row = _assignment(task, assignment_id)
        if row.get('status') != 'planned':
            if not resume_after_failure:
                return task
            lease = workspace_lifecycle.read(copy)
            attempts = lease.get('attempts', [])
            latest = attempts[-1] if attempts else {}
            if (lease.get('binding') != {'task_id': task_id, 'assignment_id': assignment_id}
                    or lease.get('phase') != 'review'
                    or lease.get('pending_attempt')
                    or latest.get('id') != attempt_id
                    or latest.get('status') != 'failed'
                    or latest.get('check_timeout_seconds') != check_timeout_seconds
                    or row.get('workspace_id') != workspace_id
                    or row.get('job_id') != lease['job_id']
                    or row.get('job_finalization') or row.get('job_finalization_request')):
                raise CoordinationError('Resumed baseline failure no longer owns the current job.', 75)
            if (row.get('attempt_id') == attempt_id
                    and row.get('status') == 'failed'
                    and row.get('failure_stage') == 'preparation'):
                return task
            if (row.get('status') != 'failed' or not row.get('disposition')
                    or len(attempts) < 2
                    or attempts[-2].get('id') != row.get('attempt_id')):
                raise CoordinationError('Resumed baseline failure has no reviewed predecessor.', 75)
            _preserve_assignment_attempt(row)
            for field in ('disposition', 'started_at', 'execution_snapshot',
                          'routing_features', 'routing_decision_id', 'prepared_changes',
                          'prepared_input_changes', 'verification_runs', 'baseline_checks',
                          'verification_cause', 'readiness_checks'):
                row.pop(field, None)
        item = assignment_deliverable(task, assignment_id)
        now = time.time()
        row.update(status='failed', queue_state='finished',
                   error_kind='cancelled' if cancelled else 'environment',
                   failure_stage='preparation', preparation_cause=cause, exit_code=exit_code,
                   result_summary=summary, finished_at=now,
                   worker_changes=[], checks=[])
        if baseline_checks is not None:
            row['baseline_checks'] = list(baseline_checks)
        if readiness_checks is not None:
            row['readiness_checks'] = list(readiness_checks)
        if verification_cause is not None:
            row['verification_cause'] = verification_cause
        if workspace_id is not None:
            row['workspace_id'] = str(workspace_id)
        if identity:
            row['job_id'] = identity[0]
            if identity[1]:
                row['attempt_id'] = identity[1]
        if check_timeout_seconds is not None:
            row['check_timeout_seconds'] = check_timeout_seconds
        if runtime is not None:
            row['runtime'] = runtime
        if canonical_effort is not None:
            row['effort'] = canonical_effort
        if not cancelled and cause == 'environment':
            constraints = task.setdefault('constraints', [])
            recorded = any(
                constraint.get('deliverable_id') == item['id']
                and constraint.get('code') in TECHNICAL_RETENTION
                and constraint.get('source') == 'runner'
                for constraint in constraints)
            if not recorded:
                constraints.append({'deliverable_id': item['id'],
                                    'code': 'environment_incompatible',
                                    'evidence': summary[:1000],
                                    'source': 'runner', 'at': now})
        task['updated_at'] = now
        _atomic(_task_path(root, task_id), task)
        return task


def use_result(root: Path, task_id: str, assignment_id: str,
               disposition: str, evidence: str, *, cost_usd: float | None = None,
               rework: dict | None = None, quality: dict | None = None) -> dict:
    # A correction reopens execution intent. Serialize it with actual final
    # acceptance before writing feedback, so a concurrent finalizer cannot
    # delete the copy between the disposition and its retry lease.
    if disposition in ('incorporated', 'reproduced', 'rejected', 'needs-rework'):
        from . import workspace, workspace_gc, workspace_lifecycle
        row = _assignment(load_task(root, task_id), assignment_id)
        identifier = row.get('workspace_id')
        if (isinstance(identifier, str) and re.fullmatch('[0-9a-f]{32}', identifier)
                and os.path.lexists(_state_root() / 'workspaces' / identifier)):
            try:
                copy = workspace_gc._load_record(_state_root(), identifier)
                with ExitStack() as owned:
                    with workspace_lifecycle.fence(_state_root()):
                        owned.enter_context(workspace_gc._owner(copy))
                        lease = workspace_lifecycle.read(copy)
                        retained = workspace_gc.read_result(copy, lease=lease)
                        if (retained is not None and retained.get('schema') == 2
                                and disposition in ('incorporated', 'reproduced')):
                            raise CoordinationError(
                                'A discarded result cannot be integrated or accepted.', 73)
                        assessment_only = bool(lease and lease['phase'] in workspace_lifecycle.FINAL_PHASES
                            and quality is not None and rework is None
                            and disposition == (row.get('disposition') or {}).get('kind'))
                        if (disposition == 'needs-rework' and lease
                                and lease['phase'] in workspace_lifecycle.FINAL_PHASES and not assessment_only):
                            raise CoordinationError('This job is finalized; plan a new job for further changes.', 73)
                    if copy.source.resolve() != _project_root(root):
                        raise CoordinationError('Result workspace belongs to another project.', 78)
                    result = _use_result(root, task_id, assignment_id, disposition, evidence,
                                         cost_usd=cost_usd, rework=rework, quality=quality,
                                         _finalized_assessment=assessment_only)
                if disposition == 'rejected' and not row.get('job_finalization'):
                    return finalize_assignment(root, task_id, assignment_id, evidence, cancelled=True)
                return result
            except workspace.WorkspaceError as cause:
                raise CoordinationError(str(cause), cause.code) from None
    return _use_result(root, task_id, assignment_id, disposition, evidence,
                       cost_usd=cost_usd, rework=rework, quality=quality)


def _use_result(root: Path, task_id: str, assignment_id: str,
                disposition: str, evidence: str, *, cost_usd: float | None = None,
                rework: dict | None = None, quality: dict | None = None,
                _finalized_assessment: bool = False) -> dict:
    """Record the coordinator disposition and queue its durable lessons event.

    ``rework`` is validated by the lessons service and ``quality`` by the closed
    routing assessment contract before any ledger mutation. An attached
    structured correction is still not a clean result when the outer disposition
    is ``incorporated``/``reproduced``: the routing observation is recorded as
    operational ``rework`` and the service counts that case as rework. An
    explicit assessment replaces the case's legacy binary inference, so an
    explicit ``met`` still earns full quality credit after a corrected result.
    Older compilations without ``rework``/``quality`` stay explicitly unknown;
    provider/environment/cancelled outcomes stay neutral even with an attached
    assessment. Both events are written to durable outboxes in the same atomic
    ledger update and replayed idempotently afterwards, so a crash between the
    stores cannot lose or duplicate one.
    """
    if disposition not in ("incorporated", "reproduced", "rejected", "needs-rework"):
        raise CoordinationError("Invalid result disposition.", 64)
    if not evidence.strip():
        raise CoordinationError("Result disposition requires evidence.", 64)
    try:
        rework = lessons.validate_rework(rework)
    except lessons.LessonError as error:
        raise CoordinationError(str(error), 64) from None
    try:
        quality = validate_quality(quality)
    except RoutingError as error:
        raise CoordinationError(error.message, error.code) from None
    if cost_usd is not None:
        try:
            number(cost_usd, "cost_usd")
        except RoutingError as error:
            raise CoordinationError(error.message, error.code) from None
    recorded_evidence = evidence[:LESSONS_EVIDENCE_LIMIT]
    with _lock(root):
        task = load_task(root, task_id)
        row = _assignment(task, assignment_id)
        if row.get("status") not in ("succeeded", "failed"):
            raise CoordinationError("Cannot disposition an unfinished assignment.", 64)
        frozen = bool(row.get('job_finalization') or row.get('job_finalization_request') or _finalized_assessment)
        if frozen and (disposition != (row.get('disposition') or {}).get('kind') or rework is not None):
            raise CoordinationError('Final job disposition is immutable; append a quality assessment with its original disposition.', 73)
        item = assignment_deliverable(task, assignment_id)
        context = _outcome_context(task, row, item, disposition, quality=quality)
        if not frozen:
            row["disposition"] = {"kind": disposition, "evidence": evidence[:2000],
                                  "at": time.time()}
        if disposition == 'needs-rework' and row.get('job_finalization_request'):
            row.setdefault('job_lifecycle_events', []).append({
                'event': 'finalization-aborted-for-rework', 'at': time.time(),
                'request': row.pop('job_finalization_request')})
        row.setdefault("lessons_feedback", []).append({
            "event": {"task_id": task_id, "assignment_id": assignment_id,
                      "disposition": disposition, "evidence": recorded_evidence,
                      "context": context, "rework": rework},
            "recorded": False,
        })
        if row.get('routing_features'):
            outcome = {'incorporated': 'accepted', 'reproduced': 'accepted',
                       'rejected': 'rejected', 'needs-rework': 'rework'}[disposition]
            # A structured correction on an accepted disposition is an
            # operational rework: it was incorporated but needed correction.
            # An explicit graded assessment still replaces this legacy binary
            # inference, so an explicit ``met`` keeps full quality credit.
            if rework is not None and outcome == 'accepted':
                outcome = 'rework'
            if row.get('error_kind') in ('provider', 'environment'):
                outcome = 'infrastructure'
            elif row.get('error_kind') == 'cancelled':
                outcome = 'cancelled'
            _queue_feedback(task, row, action='worker', outcome=outcome, cost_usd=cost_usd,
                            features=row['routing_features'], decision_id=row.get('routing_decision_id'),
                            started_at=row.get('started_at', task['created_at']), quality=quality)
        task["updated_at"] = time.time()
        _atomic(_task_path(root, task_id), task)
    sync_routing_feedback(root, task_id)
    sync_lessons_feedback(root, task_id)
    if disposition == 'needs-rework' and not _finalized_assessment:
        from . import workspace, workspace_lifecycle
        recorded = _assignment(load_task(root, task_id), assignment_id)
        identifier = recorded.get('workspace_id')
        if isinstance(identifier, str) and re.fullmatch('[0-9a-f]{32}', identifier):
            copy = workspace.load(_state_root(), identifier)
            if copy.source.resolve() != _project_root(root):
                raise CoordinationError('Rework workspace belongs to another project.', 78)
            # No coordination lock is held while acquiring the lifecycle fence.
            workspace_lifecycle.rework(copy)
    return load_task(root, task_id)


def finalize_assignment(root: Path, task_id: str, assignment_id: str,
                        evidence: str, *, cancelled=False,
                        discard_result=False) -> dict:
    """Explicit final review of one job; parent feature completion is unrelated.

    ``discard_result`` is cancellation-only, requires explicit non-default
    evidence and is delegated to the workspace's closed typed-capture protocol.
    A discarded logical job is rejected as a whole: linked corrections share the
    same terminal publication, previous attempts/quality remain append-only and
    no current patch is exposed.
    """
    if not isinstance(evidence, str) or not evidence.strip():
        raise CoordinationError('Final job acceptance requires evidence.', 64)
    if type(discard_result) is not bool:
        raise CoordinationError('discard_result must be a boolean.', 64)
    if discard_result and not cancelled:
        raise CoordinationError('--discard-result is valid only with --cancel.', 64)
    if (discard_result
            and evidence.strip().lower() in ('cancelled', 'accepted')):
        raise CoordinationError(
            'Explicit discard requires non-default review evidence.', 64)
    from . import workspace, workspace_gc, workspace_lifecycle
    root = _project_root(root)
    task = load_task(root, task_id)
    _validate_retained_results(root, task_id, allow_repairable_missing=True)
    row = _assignment(task, assignment_id)
    saved_request = row.get('job_finalization_request')
    if isinstance(saved_request, dict) and saved_request.get('discard_result') is True:
        discard_result = True
    if row.get('job_finalization'):
        if discard_result and row['job_finalization'].get('discarded') is not True:
            raise CoordinationError(
                'A captured finalization cannot be replaced by an explicit discard.', 73)
        _publish_job_finalization(
            root, task_id, assignment_id, row['job_finalization'])
        return load_task(root, task_id)
    disposition = (row.get('disposition') or {}).get('kind')
    if row.get('status') not in ('succeeded', 'failed'):
        raise CoordinationError('Review and disposition the finished result before finalizing.', 64)
    if discard_result:
        if disposition in ('incorporated', 'reproduced'):
            raise CoordinationError(
                'A discarded result cannot remain accepted or integrated.', 73)
    elif disposition is None:
        raise CoordinationError('Review and disposition the finished result before finalizing.', 64)
    if disposition == 'needs-rework' and not cancelled:
        raise CoordinationError('This job still needs corrections; keep its existing workspace.', 64)
    identifier = row.get('workspace_id')
    if not isinstance(identifier, str) or not re.fullmatch('[0-9a-f]{32}', identifier):
        raise CoordinationError('Assignment has no verified owned execution copy.', 64)
    cancelled = bool(cancelled or disposition == 'rejected')
    try:
        copy = workspace_gc._load_record(_state_root(), identifier)
        if copy.source.resolve() != _project_root(root):
            raise CoordinationError('Assignment workspace belongs to another project.', 78)

        def attest():
            with _lock(root):
                current = load_task(root, task_id)
                actual = _assignment(current, assignment_id)
                decision = (actual.get('disposition') or {}).get('kind')
                if discard_result:
                    if (actual.get('workspace_id') != identifier
                            or actual.get('status') not in ('succeeded', 'failed')
                            or decision not in (None, 'rejected', 'needs-rework')
                            or actual.get('job_finalization')):
                        raise CoordinationError(
                            'Final review changed before discard; execution copy preserved.', 75)
                elif (actual.get('workspace_id') != identifier
                        or actual.get('status') not in ('succeeded', 'failed')
                        or decision != disposition or decision is None
                        or (decision == 'needs-rework' and not cancelled)):
                    raise CoordinationError(
                        'Final review changed before acceptance; execution copy preserved.', 75)
                lease, chain, repaired = _finalization_authority(
                    root, task_id, assignment_id, copy,
                    cancelled=cancelled, recover_binding=True,
                    discarding=discard_result)
                if lease['binding'] != {'task_id': task_id, 'assignment_id': assignment_id}:
                    raise CoordinationError('Finalize the latest correction assignment of this logical job.', 64)
                changed = False
                existing = actual.get('job_finalization_request')
                if existing is None:
                    actual['job_finalization_request'] = {
                        'at': time.time(), 'evidence': evidence[:2000],
                        'cancelled': cancelled, 'discard_result': bool(discard_result),
                        'project': str(root), 'task_id': task_id,
                        'assignment_id': assignment_id,
                        'workspace_id': identifier, 'job_id': lease['job_id']}
                    changed = True
                else:
                    _finalization_request(
                        existing, root, task_id, assignment_id, copy, lease, cancelled)
                    if discard_result and existing.get('discard_result') is not True:
                        actual['job_finalization_request'] = dict(
                            existing, evidence=evidence[:2000], at=time.time(),
                            discard_result=True)
                        changed = True
                if repaired:
                    workspace_lifecycle.write(copy, lease)
                if changed:
                    current['updated_at'] = time.time()
                    _atomic(_task_path(root, task_id), current)
                if discard_result:
                    _reject_discarded_chain(
                        root, chain, evidence, request=actual['job_finalization_request'])

        report = workspace_gc.finalize(
            copy, reason=evidence[:1000], cancelled=cancelled,
            before_finalize=attest, discard_result=discard_result)
    except (workspace.WorkspaceError, state_storage.StorageError) as cause:
        raise CoordinationError(str(cause), cause.code) from None
    lease = workspace_lifecycle.read(copy)
    request = _assignment(load_task(root, task_id), assignment_id).get('job_finalization_request')
    publication = dict(
        report, at=request['at'] if request else time.time(),
        evidence=request['evidence'] if request else evidence[:2000],
        job_id=lease['job_id'], cancelled=lease['phase'] == 'cancelled')
    _publish_job_finalization(root, task_id, assignment_id, publication)
    if discard_result:
        sync_routing_feedback(root, task_id)
    return load_task(root, task_id)


def abandon_assignment(root: Path, task_id: str, assignment_id: str,
                       evidence: str, *, confirmed_stopped: bool = False) -> dict:
    """Explicitly close inspected, stopped work after coordinator process loss."""
    if confirmed_stopped is not True or not isinstance(evidence, str) or not evidence.strip():
        raise CoordinationError('Inspect the workspace and verify all worker processes have stopped; provide evidence and --confirmed-stopped.', 64)
    from . import workspace
    task = load_task(root, task_id)
    row = _assignment(task, assignment_id)
    if row.get('status') != 'running' or not row.get('workspace_id'):
        raise CoordinationError('Only an interrupted running assignment can be abandoned.', 64)
    try:
        copy = workspace.load(_state_root(), row['workspace_id'])
        if copy.source.resolve() != _project_root(root):
            raise CoordinationError('Assignment workspace belongs to another project.', 78)
        # Same lock order as the managed runner: workspace, then coordinator.
        # An active owner always blocks abandonment; no automatic termination.
        with copy.lock(recover=True):
            changes, _ = copy.changes()
            assignment_finished(root, task_id, assignment_id, 'failed', evidence,
                                changes, [], error_kind='cancelled', exit_code=130)
            copy.failed('cancelled', 130)
            # Already holding the owner lock; publish without reacquiring it.
            _use_result(root, task_id, assignment_id, 'rejected', evidence)
        return finalize_assignment(root, task_id, assignment_id, evidence, cancelled=True)
    except workspace.WorkspaceError as error:
        raise CoordinationError(error.message, error.code) from None


def _queue_feedback(task, owner, *, action, outcome, cost_usd, features, decision_id, started_at,
                    quality=None):
    """Append one durable routing observation, deduplicating an exact retry.

    A legacy caller that omits ``quality`` keeps the original observation shape
    without a ``quality`` key so stored fingerprints stay compatible. A changed
    assessment is a distinct event: the previous observation is preserved and a
    new one is appended, so an earlier explicit grade can never be silently
    replaced by a later one.
    """
    try:
        cost = None if cost_usd is None else number(cost_usd, 'cost_usd')
    except RoutingError as error:
        raise CoordinationError(error.message, error.code) from None
    events = owner.setdefault('routing_feedback', [])
    if events:
        previous = events[-1].get('observation') or {}
        if (previous.get('outcome') == outcome and previous.get('cost_usd') == cost
                and previous.get('quality') == quality):
            return
    now = time.time()
    case_id = fingerprint([task['id'], owner['id']])
    observation = {'id': f'feedback-{case_id[:32]}-{len(events)}', 'case_id': case_id,
                   'origin': 'local', 'features': features, 'action': action, 'outcome': outcome,
                   'observed_at': now, 'cost_usd': cost, 'duration_seconds': max(0., now - started_at),
                   'decision_id': decision_id}
    if quality is not None:
        observation['quality'] = quality
    events.append({'observation': observation, 'recorded': False})


def sync_routing_feedback(root: Path, task_id: str) -> None:
    """Replay the durable outbox after interruptions; SQLite identities deduplicate it."""
    with _lock(root):
        task = load_task(root, task_id)
        changed = False
        for owner in [*task.get('assignments', []), *task.get('deliverables', [])]:
            for event in owner.get('routing_feedback', []):
                if not event['recorded']:
                    try:
                        RoutingService(root).observe(event['observation'])
                    except RoutingError as error:
                        raise CoordinationError(error.message, error.code) from None
                    event['recorded'] = True
                    changed = True
        if changed:
            _atomic(_task_path(root, task_id), task)


def observe_coordinator_result(root: Path, task_id: str, deliverable_id: str,
                               outcome: str, evidence: str, *, cost_usd: float | None = None) -> dict:
    if outcome not in ('accepted', 'rework', 'rejected', 'infrastructure', 'cancelled', 'unknown') or not evidence.strip():
        raise CoordinationError('Deliverable result requires a valid outcome and verification evidence.', 64)
    with _lock(root):
        task = load_task(root, task_id)
        item = next((item for item in task['deliverables'] if item['id'] == deliverable_id), None)
        if item is None or item['executor'] not in ('coordinator', 'native-agent') or not item.get('features'):
            raise CoordinationError('Result requires a planned coordinator or native-agent deliverable with original features.', 64)
        if item['executor'] == 'coordinator':
            decision_id = (item.get('routing') or {}).get('decision_id')
            _queue_feedback(task, item, action='coordinator', outcome=outcome, cost_usd=cost_usd,
                            features=item['features'], decision_id=decision_id, started_at=task['created_at'])
        elif cost_usd is not None:
            try:
                number(cost_usd, 'cost_usd')
            except RoutingError as error:
                raise CoordinationError(error.message, error.code) from None
        now = time.time()
        item['result'] = {'outcome': outcome, 'evidence': evidence[:2000], 'recorded_at': now}
        if cost_usd is not None:
            item['result']['cost_usd'] = cost_usd
        item.pop('result_invalidated_at', None)
        if task.get('status') == 'completed' and completion_issues(task):
            task['status'] = 'active'
            task.pop('completed_at', None)
        task['updated_at'] = now
        _atomic(_task_path(root, task_id), task)
    sync_routing_feedback(root, task_id)
    return load_task(root, task_id)


def record_constraint(root: Path, task_id: str, deliverable_id: str,
                      code: str, evidence: str, source: str = "runner") -> dict:
    if code not in TECHNICAL_RETENTION:
        raise CoordinationError("Only technical runner constraints may be recorded automatically.", 64)
    if source != "runner" or not evidence.strip():
        raise CoordinationError("Technical constraints require runner evidence.", 64)
    with _lock(root):
        task = load_task(root, task_id)
        if deliverable_id not in {d.get("id") for d in task.get("deliverables", [])}:
            raise CoordinationError("Constraint references unknown deliverable.", 64)
        task.setdefault("constraints", []).append({
            "deliverable_id": deliverable_id,
            "code": code,
            "evidence": evidence[:1000],
            "source": source,
            "at": time.time(),
        })
        task["updated_at"] = time.time()
        _atomic(_task_path(root, task_id), task)
        return task


def record_coordinator_event(root: Path, task_id: str, kind: str,
                             paths: list[str] | None = None) -> None:
    with _lock(root):
        task = load_task(root, task_id)
        now = time.time()
        task.setdefault("coordinator_events", []).append({
            "kind": kind, "paths": list(paths or []), "at": now,
        })
        if kind == 'mutation_requested':
            for item in task.get('deliverables', []):
                if (item.get('executor') in ('coordinator', 'native-agent')
                        and _mutation_touches_scope(root, paths, item.get('scope', []))):
                    item['result_invalidated_at'] = now
            if task.get('status') == 'completed':
                task['status'] = 'active'
                task.pop('completed_at', None)
        task["updated_at"] = now
        _atomic(_task_path(root, task_id), task)


def _deliverable(task: dict, deliverable_id: str) -> dict | None:
    return next((item for item in task.get('deliverables', [])
                 if item.get('id') == deliverable_id), None)


def _native_dispatch_binding(task: dict, tool_use_id: str) -> dict | None:
    for item in task.get('deliverables', []):
        for entry in item.get('native_dispatches', []) or []:
            if isinstance(entry, dict) and entry.get('tool_use_id') == tool_use_id:
                return entry
    return None


def _foreign_dispatch_binding(root: Path, task_id: str, tool_use_id: str) -> dict | None:
    """A platform tool_use_id is idempotent only inside its original binding."""
    directory = _task_path(root, task_id).parent
    if not directory.exists():
        return None
    for path in sorted(directory.glob('task-*.json')):
        if path.name == task_id + '.json':
            continue
        try:
            other = _read(path)
        except CoordinationError:
            continue
        entry = _native_dispatch_binding(other, tool_use_id)
        if entry is not None:
            return entry
    return None


def _conflicting_worker(root: Path, task: dict, item: dict) -> dict | None:
    owners = [task, *(row for row in unfinished_tasks(root, task['session_id'])
                      if row['id'] != task['id'])]
    for owner in owners:
        for row in owner.get('assignments', []):
            if row.get('status') in ('succeeded', 'failed', 'cancelled'):
                continue
            if owner['id'] == task['id'] and row.get('deliverable_id') == item['id']:
                return row
            other = _deliverable(owner, row.get('deliverable_id'))
            if other is None:
                continue
            if (item.get('kind') in WORKER_WRITE_KINDS
                    and other.get('kind') in WORKER_WRITE_KINDS
                    and _mutation_touches_scope(root, item.get('scope', []),
                                                other.get('scope', []))):
                return row
    return None


def authorize_native_dispatch(root: Path, task_id: str, deliverable_id: str,
                              tool_name: str, tool_use_id: str) -> dict:
    """Authorize one native-agent tool dispatch against the ledger.

    The root hook owns prompt parsing; this function only checks the ledger and
    records the binding. It never stores a prompt and never recurses into the
    task lock. Tool use is idempotent for one binding and rejected for another.
    """
    if not isinstance(deliverable_id, str) or not deliverable_id.strip():
        raise CoordinationError(
            'Native dispatch requires the planned deliverable id (missing binding).', 64)
    if not isinstance(tool_use_id, str) or not tool_use_id.strip():
        raise CoordinationError(
            'Native dispatch requires the platform tool_use_id (missing binding).', 64)
    tool_name = tool_name if isinstance(tool_name, str) else ''
    with _lock(root):
        task = load_task(root, task_id)
        item = _deliverable(task, deliverable_id)
        if item is None:
            raise CoordinationError(
                f'Task {task_id} has no planned deliverable {deliverable_id}; '
                'register the native deliverable before dispatching it.', 64)
        existing = _native_dispatch_binding(task, tool_use_id)
        if existing is not None:
            if (existing.get('deliverable_id') != deliverable_id
                    or existing.get('tool_name') != tool_name):
                raise CoordinationError(
                    f"tool_use_id already binds deliverable {existing.get('deliverable_id')}; "
                    'a platform tool id is idempotent only for its original binding and tool.', 64)
        if item.get('executor') != 'native-agent':
            raise CoordinationError(
                f'{deliverable_id} is bound to {item.get("executor")}, not a native agent '
                '(foreign binding).', 64)
        foreign = _foreign_dispatch_binding(root, task_id, tool_use_id)
        if foreign is not None:
            raise CoordinationError(
                f"tool_use_id already binds deliverable {foreign.get('deliverable_id')} "
                f"of task {foreign.get('task_id')}; foreign bindings are rejected.", 64)
        try:
            validate_native_exception(item)
        except ValueError as error:
            raise CoordinationError(f'{deliverable_id}: {error}', 64) from None
        if item.get('kind') in PROTECTED_COORDINATOR_KINDS:
            raise CoordinationError(
                'Protected coordinator responsibilities stay with the coordinator; '
                f'{deliverable_id} cannot be dispatched to a native agent.', 64)
        if item.get('kind') not in WORKER_WRITE_KINDS | WORKER_READ_KINDS:
            raise CoordinationError(
                f"{deliverable_id}: unknown deliverable kind {item.get('kind')!r}; "
                'native dispatch requires a known non-protected kind.', 64)
        current = settings.resolve(root)
        if not current.enabled:
            raise CoordinationError(
                'DeepSeek Team is disabled for this project; the root hook must bypass the '
                'off state explicitly before any native dispatch is authorized.', 69)
        access = _effective_task_access(task, current)
        if item.get('kind') in WORKER_WRITE_KINDS and access != 'full-access':
            raise CoordinationError(
                'Current access is read-only; a native-writing dispatch is not authorized.', 78)
        outcome = _deliverable_outcome(task, item)
        if outcome in ('accepted', 'cancelled'):
            raise CoordinationError(
                f'{deliverable_id} already recorded a terminal {outcome} outcome.', 64)
        if task.get('status') in ('completed', 'closed'):
            raise CoordinationError(
                f"Coordination task is already {task.get('status')}; no native dispatch is authorized.", 64)
        issues = validate_task(root, task_id)
        if issues:
            raise CoordinationError('Native dispatch distribution is noncompliant: ' + '; '.join(issues), 78)
        conflict = _conflicting_worker(root, task, item)
        if conflict is not None:
            raise CoordinationError(
                f"{deliverable_id} conflicts with pending worker assignment {conflict['id']} "
                f"for deliverable {conflict.get('deliverable_id')}; use the worker-owned scope.", 64)
        # Replay is a bookkeeping optimization, never an exemption from current
        # access, completion, or distribution checks.
        if existing is not None:
            return task
        now = time.time()
        entry = {'task_id': task['id'], 'deliverable_id': deliverable_id,
                 'tool_name': tool_name, 'tool_use_id': tool_use_id, 'at': now}
        item.setdefault('native_dispatches', []).append(entry)
        item['native_dispatch_started_at'] = now
        task.setdefault('coordinator_events', []).append(dict(entry, kind='native_dispatch'))
        task['status'] = 'active'
        task['updated_at'] = now
        _atomic(_task_path(root, task_id), task)
        return task


def assignment_deliverable(task: dict, assignment_id: str) -> dict:
    row = _assignment(task, assignment_id)
    for item in task.get('deliverables', []):
        if item.get('id') == row.get('deliverable_id'):
            return item
    raise CoordinationError('Assignment deliverable is missing.', 64)


def _command_available(copy, token: str, environment: dict | None = None) -> bool:
    if token.startswith('./') or '/' in token:
        path = (copy.path / token).resolve() if not Path(token).is_absolute() else Path(token)
        try:
            return path.is_file() and os.access(path, os.X_OK)
        except OSError:
            return False
    search = os.pathsep.join([
        str(copy.path / '.venv/bin'),
        str(copy.path / 'node_modules/.bin'),
        (os.environ if environment is None else environment).get('PATH', ''),
    ])
    return shutil.which(token, path=search) is not None


def ensure_assignment_ready(root: Path, task_id: str, assignment_id: str, copy,
                            *, environment: dict | None = None) -> dict:
    """Fail preparation before provider access when declared prerequisites are absent."""
    task = load_task(root, task_id)
    item = assignment_deliverable(task, assignment_id)
    _check_assignment_access(root, task, item)
    if copy.metadata.get('base_head') != task.get('base_head'):
        record_constraint(root, task_id, item['id'], 'environment_incompatible',
                          'workspace base HEAD does not match coordination task base HEAD')
        raise CoordinationError('Workspace source version does not match the coordination task base HEAD.',
                                78, constraint_code='environment_incompatible')
    missing = []
    for dep in item.get('dependencies', []):
        if not isinstance(dep, dict) or dep.get('kind') not in ('command', 'path'):
            raise CoordinationError('Dependencies must use kind=command or kind=path.', 64)
        value = str(dep.get('value') or '')
        if dep['kind'] == 'command':
            if not value or not _command_available(copy, value, environment):
                missing.append('command:' + value)
        else:
            path = copy.path / value
            if not value or not path.exists():
                missing.append('path:' + value)
    for command in item.get('checks', []):
        try:
            executable = verification.first_executable(command)
        except verification.CheckCommandError as error:
            raise CoordinationError(
                'Empty declared verification command.'
                if error.reason in ('empty', 'assignment-only')
                else 'Invalid declared verification command.', 64) from None
        if not _command_available(copy, executable, environment):
            missing.append('check-command:' + executable)
    if missing:
        evidence = 'missing declared dependencies/check runtime: ' + ', '.join(sorted(set(missing)))
        record_constraint(root, task_id, item['id'], 'dependency_unavailable', evidence)
        raise CoordinationError(
            'Preparation ' + evidence +
            '. Prepare the owned workspace explicitly; stubs are not equivalent verification.',
            78, constraint_code='dependency_unavailable')
    return item


def summary(task: dict) -> str:
    level = task['policy']['delegation_level']
    deliverables = {item.get('id'): item for item in task.get('deliverables', [])}
    lines = [
        f"DeepSeek Team coordination task {task['id']}: "
        f"{str(level) + '%' if level != 'auto' else 'Auto'}/{task['policy']['effective_access']}; "
        f"effort={task['policy'].get('effort', 'auto')}; "
        f"status={task.get('status', 'unknown')}; "
        f"classification={task.get('classification') or 'pending'}.",
    ]
    for row in task.get("assignments", []):
        text = f"- {row['id']} deliverable={row['deliverable_id']} status={row['status']}"
        text += (f"; workspace_id={row.get('workspace_id') or 'none'}"
                 f"; job_id={row.get('job_id') or 'none'}"
                 f"; attempt_id={row.get('attempt_id') or 'none'}")
        item = deliverables.get(row.get('deliverable_id')) or {}
        correction = row.get('correction_of') or item.get('correction_of')
        if isinstance(correction, dict):
            text += (f"; correction_of={correction.get('task_id')}/"
                     f"{correction.get('assignment_id')}")
        if row.get("effort"):
            text += f"; effort={row['effort']}"
        if row.get('queue_state'):
            text += f"; queue={row['queue_state']}"
        if row.get('error_kind'):
            text += f"; error={row['error_kind']}"
        if row.get('failure_stage'):
            text += f"; stage={row['failure_stage']}"
        if row.get('preparation_cause'):
            text += f"; cause={row['preparation_cause']}"
        if row.get('checks'):
            passed = sum(check.get('exit_code') == 0 for check in row['checks'])
            text += f"; checks_passed={passed}; checks_failed={len(row['checks']) - passed}"
        if row.get('worker_changes'):
            text += '; files=' + ', '.join(row['worker_changes'][:10])
        if row.get("result_summary"):
            text += f"; result={row['result_summary'][:500]}"
        if row.get("disposition"):
            text += f"; disposition={row['disposition']['kind']}: {row['disposition']['evidence'][:300]}"
        lines.append(text)
    for item in task.get("deliverables", []):
        if item.get("executor") in ("coordinator", "native-agent"):
            text = (f"- {item['executor']} deliverable={item.get('id')} kind={item.get('kind')}; "
                    f"outcome={_deliverable_outcome(task, item)}")
            if item['executor'] == 'native-agent':
                text += '; reason=' + str(item.get('delegation_reason') or '')[:300]
            lines.append(text)
    for row in routing_diagnostics(task):
        text = (f"- routing deliverable={row['id']} requested={row['requested_executor']} "
                f"resolved={row['resolved_executor']}; "
                f"reasons={', '.join(row['reason_codes']) or 'none'}")
        if row['next_step']:
            text += '; next_step=' + row['next_step']
        lines.append(text)
    return "\n".join(lines)
