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

from . import lessons, scope_matching, settings, state_storage
from . import verification
from .config import sync_directory
from .effort import DEFAULT_EFFORT, normalize_effort, normalize_policy_effort
from .native_delegation import validate_native_exception
from .routing import RoutingService
from .routing_models import (RoutingError, fingerprint, number, validate_features,
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
# Optional plan metadata that also becomes part of a deliverable's identity, so an
# accepted or started scope cannot silently change by replanning. They stay
# optional: read-only protected plans do not need a mutation authorization.
OPTIONAL_DEFINITION_FIELDS = ("integration_of", "write_scope", "decision_artifacts",
                              "correction_of")
_DELIVERABLE_ID = re.compile(r"[A-Za-z0-9_.-]{1,80}")
_CORRECTION_TASK_ID = re.compile(r"task-[0-9a-f]{20}")
_CORRECTION_ASSIGNMENT_ID = re.compile(r"as-[0-9a-f]{16}")
# Stable, human-readable advice for a routing decision that retained the work.
ROUTING_NEXT_STEPS = {
    "protected_kind": "keep this protected responsibility with the coordinator",
    "ineligible_kind": "use a supported deliverable kind or keep this slice with the coordinator",
    "write_requires_full_access": "explicitly opt into full access before delegating a writing "
                                  "slice, or keep it with the coordinator",
    "ineligible_risk": "keep this risky slice with the coordinator or reduce its uncertainty",
    "ineligible_scope_size": "split this large deliverable into bounded slices before delegating",
    "ineligible_coupling": "split this cross-component deliverable into locally coupled slices "
                           "before delegating",
    "ineligible_localization": "use a bounded read-only diagnostic deliverable to localize the "
                               "change before delegating",
    "ineligible_clarity": "use a bounded read-only diagnostic deliverable to clarify the "
                          "requirements before delegating",
    "ineligible_verification": "declare executable checks or a reproducer before delegating",
    "declared_verification_required": "declare executable checks or a reproducer before "
                                      "delegating, or keep this slice with the coordinator",
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
    def __init__(self, message: str, code: int = 78, *, constraint_code: str | None = None):
        self.message, self.code = message, code
        self.constraint_code = constraint_code
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
    return (item.get("routing") or {}).get("requested_executor", item["executor"])


def _started_deliverable_ids(task: dict) -> set[str]:
    started = {row["deliverable_id"] for row in task.get("assignments", [])
               if row.get("status") != "planned"}
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
        rows.append({'id': item.get('id'), 'requested_executor': requested,
                     'resolved_executor': resolved, 'reason_codes': codes,
                     'next_step': _routing_next_step(requested, resolved, codes)})
    return rows


def _normalize_deliverable(raw: dict) -> dict:
    if not isinstance(raw, dict):
        raise CoordinationError("Each deliverable must be an object.", 64)
    required = {"id", "kind", "scope", "executor", "acceptance", "dependencies", "checks"}
    if not required.issubset(raw):
        raise CoordinationError("Deliverables require id/kind/scope/executor/acceptance/dependencies/checks.", 64)
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", str(raw["id"])):
        raise CoordinationError("Invalid deliverable id.", 64)
    if 'checks_reusable' in raw and type(raw['checks_reusable']) is not bool:
        raise CoordinationError('checks_reusable must be a boolean.', 64)
    if raw["executor"] not in ("auto", "worker", "coordinator", "native-agent"):
        raise CoordinationError("Deliverable executor must be auto, worker, coordinator or native-agent.", 64)
    if raw['executor'] == 'worker' and raw['kind'] in PROTECTED_COORDINATOR_KINDS:
        raise CoordinationError('Protected coordinator responsibilities cannot be assigned to a worker.', 64)
    if raw["executor"] == "native-agent":
        reason = raw.get("delegation_reason")
        if not isinstance(reason, str) or len(reason.strip()) < 12:
            raise CoordinationError(
                "native-agent deliverables require a concrete delegation_reason.", 64)
        if raw.get("kind") in PROTECTED_COORDINATOR_KINDS:
            raise CoordinationError(
                "Protected coordinator responsibilities cannot be assigned to a native agent.", 64)
        try:
            validate_native_exception(raw)
        except ValueError as error:
            raise CoordinationError(str(error), 64) from None
    value = dict(raw)
    # Derived state is read only from our ledger, never accepted from a plan.
    for key in ('routing', 'routing_feedback', 'result', 'result_invalidated_at',
                'native_dispatches', 'native_dispatch_started_at'):
        value.pop(key, None)
    for field in OPTIONAL_DEFINITION_FIELDS:
        if field in raw:
            value[field] = _plan_metadata(raw[field], field)
    value["scope"] = list(raw["scope"]) if isinstance(raw["scope"], list) else [str(raw["scope"])]
    value["acceptance"] = list(raw["acceptance"])
    value["dependencies"] = list(raw["dependencies"])
    value["checks"] = list(raw["checks"])
    value["checks_reusable"] = raw.get("checks_reusable", False)
    return value


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
    continuation_reserved = False
    if continuation_row is not None and not prestart_failure:
        current_attempt_id = continuation_row.get('attempt_id')
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
        continuation_reserved = (
            pending_binding == {'task_id': task['id'],
                                'assignment_id': continuation_assignment}
            or running_reservation)
    bound_to_current = ((bound_assignment is not None or continuation_assignment is not None)
                        and not prestart_failure)
    if bound_assignment is not None:
        expected_binding = {'task_id': task['id'], 'assignment_id': bound_assignment}
        current_row = _assignment(task, bound_assignment)
        current_attempt_id = current_row.get('attempt_id')
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
        if not pending_reservation and not running_reservation:
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
        if (not bound_to_current and copy.metadata.get('attempt_id')
                and copy.metadata['attempt_id'] != attempt_id):
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
    if bound_to_current and copy.metadata.get('status') not in ('running', 'succeeded', 'failed'):
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


def _publish_job_finalization(root: Path, task_id: str, assignment_id: str,
                              report: dict) -> None:
    """Idempotently publish one terminal lease across its authoritative lineage."""
    from . import workspace, workspace_gc, workspace_lifecycle

    if not isinstance(report, dict):
        raise CoordinationError('Logical job finalization publication is malformed.', 73)
    project_root = _project_root(root)
    task = load_task(project_root, task_id)
    row = _assignment(task, assignment_id)
    workspace_id = (row.get('workspace_id') or report.get('workspace_id')
                    or report.get('id'))
    if not isinstance(workspace_id, str) or not re.fullmatch('[0-9a-f]{32}', workspace_id):
        raise CoordinationError('Logical job finalization has no execution workspace.', 73)
    if report.get('id') not in (None, workspace_id):
        raise CoordinationError('Logical job finalization workspace does not match.', 73)
    try:
        copy = workspace_gc._load_record(_state_root(), workspace_id)
        if copy.source.resolve() != project_root:
            raise CoordinationError('Logical job workspace belongs to another project.', 78)
        lease = workspace_lifecycle.read(copy)
    except workspace.WorkspaceError as error:
        raise CoordinationError(str(error), error.code) from None
    if lease is None:
        raise CoordinationError('Logical job finalization has no durable lease.', 73)
    if lease['phase'] not in workspace_lifecycle.FINAL_PHASES:
        raise CoordinationError('Logical job is not finalized; publication is deferred.', 75)
    binding = lease.get('binding')
    if (not isinstance(binding, dict) or set(binding) != {'task_id', 'assignment_id'}):
        raise CoordinationError('Finalized logical job has no authoritative assignment binding.', 73)
    try:
        binding_task = load_task(project_root, binding['task_id'])
        _assignment(binding_task, binding['assignment_id'])
    except CoordinationError as error:
        raise CoordinationError(
            'Finalized logical job binding is unavailable: ' + error.message, error.code) from None
    if not _task_belongs_to(binding_task, project_root):
        raise CoordinationError('Finalized logical job binding belongs to another project.', 78)
    chain = _logical_job_chain(
        project_root, binding['task_id'], binding['assignment_id'])
    if (task_id, assignment_id) not in chain:
        raise CoordinationError(
            'Requested assignment is not an authoritative predecessor of this logical job.', 64)
    known_attempts = {
        entry.get('id') for entry in lease.get('attempts', [])
        if isinstance(entry, dict)}
    if lease.get('pending_attempt'):
        known_attempts.add(lease['pending_attempt'].get('id'))
    rows = []
    for chain_task_id, chain_assignment_id in chain:
        chain_task = (task if chain_task_id == task['id']
                      else load_task(project_root, chain_task_id))
        chain_row = _assignment(chain_task, chain_assignment_id)
        if (chain_row.get('job_id') != lease['job_id']
                or chain_row.get('workspace_id') != workspace_id):
            raise CoordinationError(
                'Correction lineage no longer matches the finalized logical job.', 64)
        attempt_id = chain_row.get('attempt_id')
        if attempt_id and attempt_id not in known_attempts:
            raise CoordinationError(
                'Correction lineage attempt is not in the finalized job lease.', 64)
        rows.append((chain_task_id, chain_assignment_id))

    publication = dict(report)
    publication['id'] = workspace_id
    publication['job_id'] = lease['job_id']
    publication.setdefault('at', time.time())
    for chain_task_id, chain_assignment_id in rows:
        with _lock(project_root):
            chain_task = load_task(project_root, chain_task_id)
            chain_row = _assignment(chain_task, chain_assignment_id)
            if (chain_row.get('job_id') != lease['job_id']
                    or chain_row.get('workspace_id') != workspace_id):
                raise CoordinationError(
                    'Correction lineage changed during finalization publication.', 75)
            existing = chain_row.get('job_finalization')
            changed = False
            if existing is None:
                chain_row['job_finalization'] = dict(publication)
                changed = True
            elif (existing.get('job_id') not in (None, lease['job_id'])
                  or existing.get('id') not in (None, workspace_id)):
                raise CoordinationError(
                    'Existing finalization publication belongs to another logical job.', 73)
            if chain_row.pop('job_finalization_request', None) is not None:
                changed = True
            if changed:
                chain_task['updated_at'] = time.time()
                _atomic(_task_path(project_root, chain_task_id), chain_task)


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


def _canonical_features(item, task):
    raw = item.get('features', {})
    if not isinstance(raw, dict):
        raise ValueError('features must be an object')
    defaults = {'kind': item['kind'], 'runtime': task.get('runtime', 'codex'),
                'effort': normalize_effort(task['policy'].get('effort')) or DEFAULT_EFFORT}
    features = validate_features({**defaults, **raw})
    if features['kind'] != item['kind']:
        raise ValueError('feature kind differs from deliverable kind')
    return features


def _logical_definition(item, task):
    definition = {key: item[key] for key in ('id', 'kind', 'scope', 'acceptance',
                                             'dependencies', 'checks')}
    definition['checks_reusable'] = item.get('checks_reusable', False)
    if 'retention' in item:
        definition['retention'] = item['retention']
    for field in OPTIONAL_DEFINITION_FIELDS:
        if field in item:
            definition[field] = item[field]
    if item.get('executor') == 'native-agent':
        definition['delegation_reason'] = item.get('delegation_reason')
        definition['native_exception'] = item.get('native_exception')
    definition['features'] = _canonical_features(item, task)
    definition['requested_executor'] = _requested_executor(item)
    return definition


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
            raise CoordinationError('Duplicate deliverable identity.', 64)
        ids.add(item['id'])
        raw = item.get('features', {})
        defaults = {'kind': item['kind'], 'runtime': task.get('runtime', 'codex'),
                    'effort': normalize_effort(task['policy'].get('effort')) or DEFAULT_EFFORT}
        if not isinstance(raw, dict):
            raise CoordinationError('features must be a structured feature card.', 64)
        item['features'] = validate_features({**defaults, **raw})
        if item['features']['kind'] != item['kind']:
            raise CoordinationError('Feature kind must match deliverable kind.', 64)
        prior = previous.get(item['id'])
        if item['id'] in started and prior and isinstance(prior.get('features'), dict):
            # A saved ledger may still carry the legacy 'medium' spelling; compare the
            # canonical feature card but keep the stored spelling so its routing
            # decision binding stays valid without a destructive rewrite.
            canonical_prior = dict(prior, features=validate_features(prior['features']))
            if _definition(item) != _definition(canonical_prior) or item['executor'] not in (prior['executor'], 'auto'):
                raise CoordinationError('Started deliverable scope and features are immutable; use a new deliverable id.', 64)
            item['features'] = prior['features']
            item['executor'] = prior['executor']
            item['routing'] = prior.get('routing')
            for key in ('routing_feedback', 'result', 'result_invalidated_at',
                        'native_dispatches', 'native_dispatch_started_at'):
                if key in prior:
                    item[key] = prior[key]
            continue
        requested = item['executor']
        if requested == 'auto' and service.config()['mode'] != 'auto':
            raise CoordinationError(
                'executor:auto requires routing mode auto. Explicitly choose '
                '`deepseek-team routing configure --mode auto`, or save a manual '
                '25/50/75 delegation profile before registering new work.', 64)
        from .routing_admission import manual_acceptance
        decision = service.predict(item['features'], access=task['policy']['effective_access'],
                                   binding=_binding(task['id'], item),
                                   verification_ready=bool(item['checks']) or manual_acceptance(item))
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
    if not isinstance(plan, dict):
        raise CoordinationError("Coordination plan must be an object.", 64)
    classification = plan.get("classification")
    if classification not in ("substantial", "small"):
        raise CoordinationError("classification must be substantial or small.", 64)
    deliverables = [_normalize_deliverable(x) for x in plan.get("deliverables", [])]
    if classification == "small":
        if len(deliverables) != 1 or not plan.get("small_evidence"):
            raise CoordinationError("Small tasks require one deliverable and concrete small_evidence.", 64)
        scopes = deliverables[0].get("scope", [])
        if len(scopes) != 1 or any("*" in value or "?" in value or "[" in value for value in scopes):
            raise CoordinationError(
                "Small-task exemption requires one concrete non-wildcard scope; "
                "otherwise register a substantial distribution.", 64)
    root = _project_root(root)
    with _lock(root):
        task = load_task(root, task_id)
        if task.get('schema') != 2:
            raise CoordinationError(
                'Unsupported coordination plan format; existing records are preserved. '
                'Open a new task with the current package.', 78)
        if (_same_logical_plan(task, deliverables, classification, plan.get("small_evidence"))
                and _plan_is_durable(task)):
            # An identical logical distribution is already durable. Do not open
            # the routing store, rewrite the ledger, reset phase or replace IDs.
            return task
        _validate_correction_plan(root, task, deliverables)
        with _routing_batch(root) as service:
            if classification == "substantial" and task['policy'].get('delegation_level') == 'auto':
                # Reject an explicit executor bypass before any durable ledger record
                # or routing decision exists for this revision. Started deliverables
                # are immutable and keep their already-validated saved decision.
                bypasses = _auto_executor_issues(deliverables, _started_deliverable_ids(task))
                if bypasses:
                    raise CoordinationError(
                        'Auto distribution cannot be bypassed: ' + '; '.join(bypasses) + '.', 64)
            try:
                _prepare_routing(root, task, deliverables, service)
            except RoutingError as error:
                raise CoordinationError(error.message, error.code) from None
            previous = {a["deliverable_id"]: a for a in task.get("assignments", [])}
            incoming_ids = {item["id"] for item in deliverables}
            historical_missing = [
                row["deliverable_id"] for row in task.get("assignments", [])
                if row.get("status") not in ("planned",) and row.get("deliverable_id") not in incoming_ids
            ]
            historical_missing.extend(item['id'] for item in task.get('deliverables', [])
                                      if (item.get('routing_feedback') or item.get('result')
                                          or item.get('native_dispatches'))
                                      and item['id'] not in incoming_ids)
            if historical_missing:
                raise CoordinationError(
                    "Plan revision cannot remove started/completed deliverables: " +
                    ", ".join(historical_missing) +
                    ". Add a new deliverable id for revised scope.", 64)
            assignments = []
            for item in deliverables:
                prior = previous.get(item["id"])
                if item["executor"] != "worker":
                    if prior and prior.get("status") != "planned":
                        raise CoordinationError(
                            "Plan revision cannot reassign a started/completed DeepSeek worker deliverable; "
                            "add a new deliverable id for the native/coordinator scope.", 64)
                    continue
                if item["id"] in previous:
                    prior_assignment = previous[item["id"]]
                    if prior_assignment.get('status') == 'planned':
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
    current = settings.resolve(root)
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
            issues.append(f"{item['id']}: current access does not permit worker writes; revise the plan")
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
            if level == 75 and not _retention_ok(task, item):
                issues.append(f"{item['id']}: worker-eligible {kind} retained by coordinator without a supported constraint")
    if level == 75 and access == "full-access" and eligible_count and worker_count == 0:
        issues.append("75/full-access requires worker assignment for separable worker-eligible deliverables")
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
    current = settings.resolve(root)
    if not current.enabled:
        raise CoordinationError('Delegation is disabled; no new worker assignment may start.', 69)
    access = _effective_task_access(task, current)
    if item['kind'] in WORKER_WRITE_KINDS and access != 'full-access':
        raise CoordinationError('Current access does not permit this worker writing assignment; revise the plan.', 78)


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
                        started_at: float) -> dict:
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
    return {
        "deliverable": _deliverable_snapshot(item),
        "features": _bounded_snapshot_value(matching, text=200, items=50),
        "execution": execution_block,
        "lessons": {"version": snapshot["version"], "rule_ids": list(snapshot["rule_ids"]),
                    "rules": _lessons_rules_block(snapshot), "captured": True,
                    "advisory": True},
    }


def _changes_patch_reference(workspace_id):
    """Return a private reference to a retained change patch, or None when unknown."""
    if not isinstance(workspace_id, str) or re.fullmatch(r"[0-9a-f]{32}", workspace_id) is None:
        return None
    from . import workspace as workspace_module
    try:
        copy = workspace_module.load(_state_root(), workspace_id)
    except (workspace_module.WorkspaceError, OSError):
        return None
    try:
        descriptor = os.open(copy.path.parent / "changes.patch",
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


def assignment_started(root: Path, task_id: str, assignment_id: str,
                       workspace_id: str, runtime: str,
                       prepared_changes: list[str], effort: str | None = None,
                       execution: dict | None = None, *, resume_running: bool = False) -> dict:
    """Start one attempt and snapshot its original context before execution.

    ``execution`` is optional, bounded pre-execution evidence supplied by the
    managed runner: the prompt digest and a redacted bounded brief, workspace
    base head/digest, prepared content fingerprints and the actual model. Unknown
    keys are rejected so raw prompts, credentials or model output can never be
    stored here. A resume of the same running attempt keeps its original
    snapshot; a failed explicit continuation keeps the earlier snapshot in
    ``attempt_history``.
    """
    if effort is not None:
        canonical_effort = normalize_effort(effort)
        if canonical_effort is None:
            raise CoordinationError('Worker effort must be low, high or max (legacy medium maps to high).', 64)
        effort = canonical_effort
    identity = None
    has_workspace = (isinstance(workspace_id, str)
                     and re.fullmatch('[0-9a-f]{32}', workspace_id))
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
            history = row.setdefault('attempt_history', [])
            history.append({key: row.get(key) for key in (
                'status', 'workspace_id', 'runtime', 'effort', 'started_at',
                'finished_at', 'checks', 'disposition', 'error_kind', 'exit_code',
                'failure_stage', 'preparation_cause', 'routing_features',
                'routing_decision_id', 'execution_snapshot', 'worker_changes',
                'result_summary', 'baseline_checks', 'verification_cause',
                'prepared_input_changes', 'verification_runs', 'readiness_checks',
                'job_id', 'attempt_id')})

        if copy is not None:
            # Recheck immediately before reservation; a concurrent access change
            # must not be converted into a pending attempt.
            _check_assignment_access(root, task, item)
            try:
                identity = workspace_lifecycle.prepare_attempt(
                    copy, task_id, assignment_id,
                    replace_pending=bool(resume_running))
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
                                       prepared_changes, effort, execution, started_at=now)
        row.update(status='running', workspace_id=workspace_id, runtime=runtime,
                   effort=effort, prepared_changes=sorted(set(prepared_changes)),
                   started_at=now, disposition=None, error_kind=None, exit_code=None,
                   failure_stage=None, preparation_cause=None, queue_state='running',
                   execution_snapshot=snapshot, baseline_checks=[], verification_cause=None,
                   prepared_input_changes=[], verification_runs=[], readiness_checks=[])
        if correction is not None:
            row['correction_of'] = dict(correction)
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
               'launch_failed', 'stdout_sha256', 'stderr_sha256', 'reused', 'reused_from'}
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
                                  readiness_checks: list[dict] | None = None) -> dict:
    """Close a still-planned assignment that failed before any model execution.

    The managed runner calls this when preparation of an owned copy (sandbox,
    declared dependencies or the provider boundary) fails before the runtime
    starts. No model ran, so this never records a start time, duration, cost,
    quality outcome or routing observation: the attempt stays dispositionable
    through :func:`use_result` and the task can then be completed or replanned.

    The transition is atomic and only applies to a planned assignment. Any
    running, already-failed or succeeded assignment is protected: the call is a
    safe no-op so managed exception reconciliation can never rewrite a live or
    previously recorded result, workspace, queue state, disposition or history.
    A runner ``environment_incompatible`` constraint is recorded on the
    deliverable in the same atomic update, except for cancellation (130); an
    already-recorded specific technical constraint is never duplicated. Admission
    refusals are infrastructure outcomes too, but grant no technical-retention
    exception: revoking permissions or timing out cannot justify bypassing policy.
    """
    if isinstance(exit_code, bool) or not isinstance(exit_code, int):
        raise CoordinationError('Preparation failure requires an integer exit code.', 64)
    if cause not in ('environment', 'admission'):
        raise CoordinationError('Unknown preparation failure cause.', 64)
    _validate_verification_cause(verification_cause)
    if baseline_checks is not None:
        _validate_check_evidence(baseline_checks)
    if readiness_checks is not None:
        _validate_check_evidence(readiness_checks)
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
                    identity = lease['job_id'], None
            except workspace.WorkspaceError as error:
                raise CoordinationError(str(error), error.code) from None
    with _lock(root):
        task = load_task(root, task_id)
        row = _assignment(task, assignment_id)
        if row.get('status') != 'planned':
            return task
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
                        evidence: str, *, cancelled=False) -> dict:
    """Explicit final review of one job; parent feature completion is unrelated.

    Feedback remains append-only/repeatable. Finalization separately attests that
    integration is done and no further correction is expected for this job.
    """
    if not isinstance(evidence, str) or not evidence.strip():
        raise CoordinationError('Final job acceptance requires evidence.', 64)
    from . import workspace, workspace_gc, workspace_lifecycle
    task = load_task(root, task_id)
    row = _assignment(task, assignment_id)
    if row.get('job_finalization'):
        _publish_job_finalization(
            root, task_id, assignment_id, row['job_finalization'])
        return load_task(root, task_id)
    disposition = (row.get('disposition') or {}).get('kind')
    if row.get('status') not in ('succeeded', 'failed') or disposition is None:
        raise CoordinationError('Review and disposition the finished result before finalizing.', 64)
    if disposition == 'needs-rework' and not cancelled:
        raise CoordinationError('This job still needs corrections; keep its existing workspace.', 64)
    identifier = row.get('workspace_id')
    if not isinstance(identifier, str) or not re.fullmatch('[0-9a-f]{32}', identifier):
        raise CoordinationError('Assignment has no verified owned execution copy.', 64)
    try:
        copy = workspace_gc._load_record(_state_root(), identifier)
        if copy.source.resolve() != _project_root(root):
            raise CoordinationError('Assignment workspace belongs to another project.', 78)
        def attest():
            lease = workspace_lifecycle.read(copy)
            binding = lease.get('binding') if lease else None
            if binding and binding != {'task_id': task_id, 'assignment_id': assignment_id}:
                raise CoordinationError('Finalize the latest correction assignment of this logical job.', 64)
            with _lock(root):
                current = load_task(root, task_id)
                actual = _assignment(current, assignment_id)
                if actual.get('job_finalization'):
                    return
                decision = (actual.get('disposition') or {}).get('kind')
                if (actual.get('workspace_id') != identifier
                        or actual.get('status') not in ('succeeded', 'failed')
                        or decision is None or (decision == 'needs-rework' and not cancelled)):
                    raise CoordinationError('Final review changed before acceptance; execution copy preserved.', 75)
                actual['job_finalization_request'] = {
                    'at': time.time(), 'evidence': evidence[:2000], 'cancelled': bool(cancelled),
                    'workspace_id': identifier}
                _atomic(_task_path(root, task_id), current)
        report = workspace_gc.finalize(copy, reason=evidence[:1000],
            cancelled=cancelled or disposition == 'rejected', before_finalize=attest)
    except workspace.WorkspaceError as cause:
        raise CoordinationError(str(cause), cause.code) from None
    publication = dict(report, at=time.time(), evidence=evidence[:2000])
    _publish_job_finalization(root, task_id, assignment_id, publication)
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
