"""Minimal persistent coordinator/worker ledger.

This is intentionally not a scheduler. It records distribution decisions, assignments,
worker results and dispositions so coordinator hooks can enforce process transitions and
restore context after compaction.
"""
from __future__ import annotations

from contextlib import contextmanager
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

from . import lessons, scope_matching, settings
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
OPTIONAL_DEFINITION_FIELDS = ("integration_of", "write_scope", "decision_artifacts")
_DELIVERABLE_ID = re.compile(r"[A-Za-z0-9_.-]{1,80}")
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
    configured = os.environ.get("DEEPSEEK_TEAM_STATE_DIR")
    return Path(configured).absolute() if configured else Path.home() / ".local/state/codex-deepseek"


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
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        os.chmod(state, 0o700)
    except OSError:
        pass
    info = state.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
            or info.st_mode & 0o077):
        raise CoordinationError("Coordination state root must be private and user-owned.")
    key = hashlib.sha256(os.fsencode(str(root))).hexdigest()[:24]
    directory = state / "coordination" / key
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        os.chmod(directory.parent, 0o700)
        os.chmod(directory, 0o700)
    except OSError:
        pass
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
            "schema": 1, "id": task_id, "project": str(root),
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


def _plan_metadata(value, field: str) -> list:
    """Validate one optional plan-metadata field and return its normalized list."""
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
    return value


def _definition(item):
    definition = {key: item[key] for key in ('id', 'kind', 'scope', 'acceptance',
                                             'dependencies', 'checks', 'features')}
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
    if not service.validate_plan_decision(decision_id, item.get('features'), _binding(task['id'], item), executor=item['executor']):
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
    with _lock(root), _routing_batch(root) as service:
        task = load_task(root, task_id)
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
                assignments.append(previous[item["id"]])
                continue
            assignment_id = "as-" + hashlib.sha256(
                f"{task_id}\0{item['id']}".encode()).hexdigest()[:16]
            assignments.append({
                "id": assignment_id, "deliverable_id": item["id"],
                "status": "planned", "workspace_id": None, "runtime": None,
                "effort": None, "prepared_changes": [], "worker_changes": [], "checks": [],
                "result_summary": None, "disposition": None,
            })
        task.update(classification=classification,
                    small_evidence=plan.get("small_evidence"),
                    deliverables=deliverables, assignments=assignments,
                    status="planned", updated_at=time.time())
        # Persist the ledger before the surrounding routing transaction commits.
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
    return {"id": item.get("id"), "kind": item.get("kind"),
            "scope": _bounded_snapshot_value(item.get("scope") or [], text=300, items=200),
            "acceptance": _bounded_snapshot_value(item.get("acceptance") or []),
            "dependencies": _bounded_snapshot_value(item.get("dependencies") or []),
            "checks": _bounded_snapshot_value(item.get("checks") or []),
            "features": _bounded_snapshot_value(declared) if declared else None}


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
                       execution: dict | None = None) -> dict:
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
    with _lock(root), _routing_batch(root) as service:
        task = load_task(root, task_id)
        row = _assignment(task, assignment_id)
        item = assignment_deliverable(task, assignment_id)
        _check_assignment_access(root, task, item)
        if row['status'] == 'running':
            # The managed runner holds the workspace's exclusive OS lock. An
            # exact explicit continuation can reconcile a crash between the
            # ledger rename and SQLite commit without starting another case.
            expected = dict(workspace_id=workspace_id, runtime=runtime,
                            prepared_changes=sorted(set(prepared_changes)))
            # A running row may predate canonical effort levels ('medium' == 'high').
            effort_matches = (normalize_effort(row.get('effort')) == effort
                              or (effort is None and row.get('effort') is None))
            if any(row.get(key) != value for key, value in expected.items()) or not effort_matches:
                raise CoordinationError('Running assignment continuation must match its original workspace and execution conditions.', 64)
            if row.get('routing_decision_id'):
                try:
                    service.decision(row['routing_decision_id'])
                except RoutingError as error:
                    if error.code != 66 or (item.get('routing') or {}).get('requested_executor') == 'auto':
                        raise
                    # A manual start may have captured its decision in the
                    # interrupted transaction. Recreate before any execution.
                    decision = service.predict(row['routing_features'], access=task['policy']['effective_access'])
                    row['routing_decision_id'] = decision['id']
                    _atomic(_task_path(root, task_id), task)
                service.validate_start(row['routing_decision_id'], already_running=True,
                    access=_effective_task_access(task, settings.resolve(root)))
            return task
        if row["status"] not in ("planned", "failed"):
            raise CoordinationError("Assignment is already active or completed.", 64)
        if row['status'] == 'failed':
            if row.get('disposition') is None:
                raise CoordinationError('Review and disposition the failed attempt before explicit continuation.', 64)
            history = row.setdefault('attempt_history', [])
            history.append({key: row.get(key) for key in ('status', 'workspace_id', 'runtime', 'effort',
                'started_at', 'finished_at', 'checks', 'disposition', 'error_kind', 'exit_code',
                'failure_stage', 'preparation_cause', 'routing_features', 'routing_decision_id',
                'execution_snapshot', 'worker_changes', 'result_summary')})
        if item.get('features'):
            declared = validate_features(item['features'])
            features = validate_features(dict(item['features'], runtime=runtime,
                                              effort=effort or DEFAULT_EFFORT, model='deepseek-flash'))
            auto = (item.get('routing') or {}).get('requested_executor') == 'auto'
            if auto and features != declared:
                raise CoordinationError('Worker runtime/effort must match its automatic routing decision; replan before starting.', 64)
            try:
                decision = (service.decision(item['routing']['decision_id']) if auto else
                            service.predict(features, access=task['policy']['effective_access']))
                if auto:
                    service.validate_start(decision['id'],
                        access=_effective_task_access(task, settings.resolve(root)))
            except RoutingError as error:
                raise CoordinationError(error.message, error.code) from None
            row['routing_features'] = features
            row['routing_decision_id'] = decision['id']
        now = time.time()
        snapshot = _execution_snapshot(root, task, item, workspace_id, runtime,
                                       prepared_changes, effort, execution, started_at=now)
        row.update(status="running", workspace_id=workspace_id, runtime=runtime,
                   effort=effort, prepared_changes=sorted(set(prepared_changes)),
                   started_at=now, disposition=None, error_kind=None, exit_code=None,
                   failure_stage=None, preparation_cause=None, queue_state='running',
                   execution_snapshot=snapshot)
        task.update(status="active", updated_at=time.time())
        _atomic(_task_path(root, task_id), task)
        return task


def assignment_finished(root: Path, task_id: str, assignment_id: str,
                        status: str, result_summary: str,
                        worker_changes: list[str], checks: list[dict], *,
                        error_kind: str | None = None, exit_code: int | None = None) -> dict:
    if status not in ("succeeded", "failed"):
        raise CoordinationError("Assignment result status must be succeeded or failed.", 64)
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
        if status == 'failed' and row.get('routing_features'):
            outcome = {'verification': 'rejected', 'provider': 'infrastructure',
                       'environment': 'infrastructure', 'cancelled': 'cancelled'}.get(error_kind, 'unknown')
            _queue_feedback(task, row, action='worker', outcome=outcome, cost_usd=None,
                            features=row['routing_features'], decision_id=row.get('routing_decision_id'),
                            started_at=row.get('started_at', task['created_at']))
        task["updated_at"] = time.time()
        _atomic(_task_path(root, task_id), task)
    sync_routing_feedback(root, task_id)
    return load_task(root, task_id)


def assignment_preparation_failed(root: Path, task_id: str, assignment_id: str,
                                  result_summary: str, *,
                                  exit_code: int = 78,
                                  workspace_id: str | None = None,
                                  runtime: str | None = None,
                                  effort: str | None = None,
                                  cause: str = 'environment') -> dict:
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
    canonical_effort = None
    if effort is not None:
        canonical_effort = normalize_effort(effort)
        if canonical_effort is None:
            raise CoordinationError('Worker effort must be low, high or max (legacy medium maps to high).', 64)
    if runtime is not None and (not isinstance(runtime, str) or not runtime.strip()):
        raise CoordinationError('Worker runtime must be a non-empty runtime name.', 64)
    cancelled = exit_code == 130
    summary = str(result_summary)[:4000]
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
        if workspace_id is not None:
            row['workspace_id'] = str(workspace_id)
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
        item = assignment_deliverable(task, assignment_id)
        context = _outcome_context(task, row, item, disposition, quality=quality)
        row["disposition"] = {"kind": disposition, "evidence": evidence[:2000],
                              "at": time.time()}
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
            result = use_result(root, task_id, assignment_id, 'rejected', evidence)
            copy.failed('cancelled', 130)
            return result
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


def _command_available(copy, token: str) -> bool:
    if token.startswith('./') or '/' in token:
        path = (copy.path / token).resolve() if not Path(token).is_absolute() else Path(token)
        try:
            return path.is_file() and os.access(path, os.X_OK)
        except OSError:
            return False
    search = os.pathsep.join([
        str(copy.path / '.venv/bin'),
        str(copy.path / 'node_modules/.bin'),
        os.environ.get('PATH', ''),
    ])
    return shutil.which(token, path=search) is not None


def ensure_assignment_ready(root: Path, task_id: str, assignment_id: str, copy) -> dict:
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
            if not value or not _command_available(copy, value):
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
        if not _command_available(copy, executable):
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
    lines = [
        f"DeepSeek Team coordination task {task['id']}: "
        f"{str(level) + '%' if level != 'auto' else 'Auto'}/{task['policy']['effective_access']}; "
        f"effort={task['policy'].get('effort', 'auto')}; "
        f"status={task.get('status', 'unknown')}; "
        f"classification={task.get('classification') or 'pending'}.",
    ]
    for row in task.get("assignments", []):
        text = f"- {row['id']} deliverable={row['deliverable_id']} status={row['status']}"
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
