"""Minimal persistent coordinator/worker ledger.

This is intentionally not a scheduler. It records distribution decisions, assignments,
worker results and dispositions so coordinator hooks can enforce process transitions and
restore context after compaction.
"""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import fnmatch
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import shutil
import shlex
import tempfile
import time
from typing import Any

from . import settings
from .config import sync_directory
from .routing import RoutingService
from .routing_models import RoutingError, fingerprint, number, validate_features

WORKER_WRITE_KINDS = {"implementation", "test", "fixture", "documentation", "metadata"}
WORKER_READ_KINDS = {"review", "research", "diagnostic", "test_plan"}
PROTECTED_COORDINATOR_KINDS = {
    "architecture", "security", "integration", "final_verification",
    "commit_push", "production", "secret_signing",
}
TECHNICAL_RETENTION = {
    "runner_unavailable", "dependency_unavailable", "environment_incompatible",
}


class CoordinationError(Exception):
    def __init__(self, message: str, code: int = 78):
        self.message, self.code = message, code
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


def latest_task(root: Path, session_id: str) -> dict | None:
    directory = _project_dir(root) / "tasks"
    if not directory.exists():
        return None
    rows = []
    for path in directory.glob("task-*.json"):
        try:
            value = _read(path)
        except CoordinationError:
            continue
        if value.get("session_id") == session_id:
            rows.append(value)
    return max(rows, key=lambda x: x.get("updated_at", 0), default=None)


def active_task(root: Path, session_id: str) -> dict | None:
    task = latest_task(root, session_id)
    if task is not None and task.get('status') not in ('completed', 'closed'):
        return task
    return None


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
    outcome, recorded_at = 'pending', None
    if isinstance(result, dict):
        outcome, recorded_at = result.get('outcome') or 'pending', result.get('recorded_at')
    elif item.get('executor') == 'coordinator':
        # Older ledgers stored explicit outcomes only in the router outbox.
        feedback = item.get('routing_feedback') or []
        if feedback:
            observation = feedback[-1].get('observation', {})
            if observation.get('action') == 'coordinator':
                outcome = observation.get('outcome') or 'pending'
                recorded_at = observation.get('observed_at')
    if outcome in ('accepted', 'cancelled'):
        for event in task.get('coordinator_events', []):
            if event.get('kind') != 'mutation_requested':
                continue
            at = event.get('at')
            if (isinstance(at, (int, float)) and isinstance(recorded_at, (int, float))
                    and at <= recorded_at):
                continue
            # Older hooks recorded mutations without marking result invalidation.
            # Missing timing/project data cannot establish that acceptance is fresh.
            project = task.get('project')
            if not project or _mutation_touches_scope(
                    Path(project), event.get('paths'), item.get('scope', [])):
                return 'invalidated'
    return outcome


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

    def relative(value):
        # Match the hook's canonical paths, including symlinked scope prefixes.
        # resolve() preserves glob characters; fnmatch below still interprets them.
        return os.path.relpath((root / value).resolve(), root.resolve())

    for path in map(relative, paths):
        for scope in map(relative, scopes):
            if (path == '.' or scope == '.' or fnmatch.fnmatchcase(path, scope)
                    or path.startswith(scope.rstrip('/') + '/')
                    or scope.startswith(path.rstrip('/') + '/')):
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
    value = dict(raw)
    # Derived state is read only from our ledger, never accepted from a plan.
    for key in ('routing', 'routing_feedback', 'result', 'result_evidence', 'result_invalidated_at'):
        value.pop(key, None)
    value["scope"] = list(raw["scope"]) if isinstance(raw["scope"], list) else [str(raw["scope"])]
    value["acceptance"] = list(raw["acceptance"])
    value["dependencies"] = list(raw["dependencies"])
    value["checks"] = list(raw["checks"])
    return value


def _definition(item):
    return {key: item[key] for key in ('id', 'kind', 'scope', 'acceptance',
                                      'dependencies', 'checks', 'features')}


def _binding(task_id, item):
    requested = (item.get('routing') or {}).get('requested_executor', item['executor'])
    return {'task_id': task_id, 'deliverable_id': item['id'],
            'plan_hash': fingerprint(dict(_definition(item), requested_executor=requested))}


def _prepare_routing(root, task, items, service):
    previous = {item['id']: item for item in task.get('deliverables', [])}
    started = {row['deliverable_id'] for row in task.get('assignments', [])
               if row['status'] != 'planned'}
    started.update(item['id'] for item in previous.values()
                   if item.get('routing_feedback') or item.get('result'))
    ids = set()
    for item in items:
        if item['id'] in ids:
            raise CoordinationError('Duplicate deliverable identity.', 64)
        ids.add(item['id'])
        raw = item.get('features', {})
        defaults = {'kind': item['kind'], 'runtime': task.get('runtime', 'codex'),
                    'effort': task['policy'].get('effort') if task['policy'].get('effort') in ('low', 'medium', 'high') else 'medium'}
        if not isinstance(raw, dict):
            raise CoordinationError('features must be a structured feature card.', 64)
        item['features'] = validate_features({**defaults, **raw})
        if item['features']['kind'] != item['kind']:
            raise CoordinationError('Feature kind must match deliverable kind.', 64)
        prior = previous.get(item['id'])
        if item['id'] in started and prior and 'features' in prior:
            if _definition(item) != _definition(prior) or item['executor'] not in (prior['executor'], 'auto'):
                raise CoordinationError('Started deliverable scope and features are immutable; use a new deliverable id.', 64)
            item['executor'] = prior['executor']
            item['routing'] = prior.get('routing')
            for key in ('routing_feedback', 'result', 'result_evidence', 'result_invalidated_at'):
                if key in prior:
                    item[key] = prior[key]
            continue
        requested = item['executor']
        if requested == 'auto' and service.config()['mode'] != 'auto':
            raise CoordinationError('executor:auto requires routing mode auto.', 64)
        from .routing_immediate import manual_acceptance
        decision = service.predict(item['features'], access=task['policy']['effective_access'],
                                   binding=_binding(task['id'], item),
                                   recovery=requested == 'auto' and task['policy']['delegation_level'] == 'auto'
                                   and (bool(item['checks']) or manual_acceptance(item)))
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
        old_deliverables = task.get('deliverables', [])
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
                                  if (item.get('routing_feedback') or item.get('result'))
                                  and item['id'] not in incoming_ids)
        if historical_missing:
            raise CoordinationError(
                "Plan revision cannot remove started/completed worker deliverables: " +
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
        # All routing changes stay uncommitted until the ledger is durable.
        retained_tickets = set()
        for item in deliverables:
            if item.get('routing'):
                info = service.decision(item['routing']['decision_id']).get('recovery') or {}
                if info.get('selected'):
                    retained_tickets.add(info['ticket_id'])
        for item in old_deliverables:
            if item.get('routing'):
                try:
                    info = service.decision(item['routing']['decision_id']).get('recovery') or {}
                except RoutingError as error:
                    if error.code != 66:
                        raise
                    # A crash after ledger rename but before the routing commit
                    # can leave a safe-invalid planned decision. Replanning
                    # replaces it; a rolled-back ticket cannot need release.
                    continue
                if info.get('selected') and info['ticket_id'] not in retained_tickets:
                    service.release_recovery(info['ticket_id'])
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


def validate_task(root: Path, task_id: str) -> list[str]:
    task = load_task(root, task_id)
    if task.get("classification") is None:
        return ["distribution plan is missing"]
    if task["classification"] == "small":
        return []
    policy = task["policy"]
    level = policy['delegation_level']
    # A task's distribution profile is a snapshot across compaction. Current
    # access revocations still apply immediately and cannot force a writer.
    current = settings.resolve(root)
    access = _effective_task_access(task, current)
    issues = []
    worker_count = 0
    worker_write_count = 0
    eligible_count = 0
    eligible_write_count = 0
    for item in task.get("deliverables", []):
        kind, executor = item["kind"], item["executor"]
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
    unchanged_cli_override = explicit_cli and current.as_dict() == task.get('saved_policy')
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
    """Expose scheduling without changing executor, retry history or start state."""
    if state not in ('waiting', 'ready', 'blocked', 'cancelled'):
        raise CoordinationError('Invalid worker queue state.', 64)
    with _lock(root):
        task = load_task(root, task_id)
        row = _assignment(task, assignment_id)
        row['queue_state'] = state
        row['queue_updated_at'] = time.time()
        if state == 'waiting':
            row.setdefault('queued_at', row['queue_updated_at'])
        task['updated_at'] = row['queue_updated_at']
        _atomic(_task_path(root, task_id), task)


def assignment_started(root: Path, task_id: str, assignment_id: str,
                       workspace_id: str, runtime: str,
                       prepared_changes: list[str], effort: str | None = None) -> dict:
    with _lock(root), _routing_batch(root) as service:
        task = load_task(root, task_id)
        row = _assignment(task, assignment_id)
        item = assignment_deliverable(task, assignment_id)
        _check_assignment_access(root, task, item)
        if row['status'] == 'running':
            # The managed runner holds the workspace's exclusive OS lock. An
            # exact explicit continuation can reconcile a crash between the
            # ledger rename and SQLite commit without starting another case.
            expected = dict(workspace_id=workspace_id, runtime=runtime, effort=effort,
                            prepared_changes=sorted(set(prepared_changes)))
            if any(row.get(key) != value for key, value in expected.items()):
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
                service.start_recovery(row['routing_decision_id'], already_running=True)
            return task
        if row["status"] not in ("planned", "failed"):
            raise CoordinationError("Assignment is already active or completed.", 64)
        if row['status'] == 'failed':
            if row.get('disposition') is None:
                raise CoordinationError('Review and disposition the failed attempt before explicit continuation.', 64)
            history = row.setdefault('attempt_history', [])
            history.append({key: row.get(key) for key in ('status', 'workspace_id', 'runtime', 'effort',
                'started_at', 'finished_at', 'checks', 'disposition', 'error_kind', 'exit_code',
                'routing_features', 'routing_decision_id')})
        if item.get('features'):
            features = dict(item['features'], runtime=runtime, effort=effort or 'medium', model='deepseek-flash')
            auto = (item.get('routing') or {}).get('requested_executor') == 'auto'
            if auto and features != item['features']:
                raise CoordinationError('Worker runtime/effort must match its automatic routing decision; replan before starting.', 64)
            try:
                decision = (service.decision(item['routing']['decision_id']) if auto else
                            service.predict(features, access=task['policy']['effective_access']))
                if auto:
                    service.start_recovery(decision['id'],
                        access=_effective_task_access(task, settings.resolve(root)))
            except RoutingError as error:
                raise CoordinationError(error.message, error.code) from None
            row['routing_features'] = features
            row['routing_decision_id'] = decision['id']
        row.update(status="running", workspace_id=workspace_id, runtime=runtime,
                   effort=effort, prepared_changes=sorted(set(prepared_changes)),
                   started_at=time.time(), disposition=None, error_kind=None, exit_code=None,
                   queue_state='running')
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


def use_result(root: Path, task_id: str, assignment_id: str,
               disposition: str, evidence: str, *, cost_usd: float | None = None) -> dict:
    if disposition not in ("incorporated", "reproduced", "rejected", "needs-rework"):
        raise CoordinationError("Invalid result disposition.", 64)
    if not evidence.strip():
        raise CoordinationError("Result disposition requires evidence.", 64)
    with _lock(root):
        task = load_task(root, task_id)
        row = _assignment(task, assignment_id)
        if row.get("status") not in ("succeeded", "failed"):
            raise CoordinationError("Cannot disposition an unfinished assignment.", 64)
        row["disposition"] = {"kind": disposition, "evidence": evidence[:2000],
                              "at": time.time()}
        if row.get('routing_features'):
            outcome = {'incorporated': 'accepted', 'reproduced': 'accepted',
                       'rejected': 'rejected', 'needs-rework': 'rework'}[disposition]
            if row.get('error_kind') in ('provider', 'environment'):
                outcome = 'infrastructure'
            elif row.get('error_kind') == 'cancelled':
                outcome = 'cancelled'
            _queue_feedback(task, row, action='worker', outcome=outcome, cost_usd=cost_usd,
                            features=row['routing_features'], decision_id=row.get('routing_decision_id'),
                            started_at=row.get('started_at', task['created_at']))
        task["updated_at"] = time.time()
        _atomic(_task_path(root, task_id), task)
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
            result = use_result(root, task_id, assignment_id, 'rejected', evidence)
            copy.failed('cancelled', 130)
            return result
    except workspace.WorkspaceError as error:
        raise CoordinationError(error.message, error.code) from None


def _queue_feedback(task, owner, *, action, outcome, cost_usd, features, decision_id, started_at):
    try:
        cost = None if cost_usd is None else number(cost_usd, 'cost_usd')
    except RoutingError as error:
        raise CoordinationError(error.message, error.code) from None
    events = owner.setdefault('routing_feedback', [])
    if events and events[-1]['observation']['outcome'] == outcome and events[-1]['observation']['cost_usd'] == cost:
        return
    now = time.time()
    case_id = fingerprint([task['id'], owner['id']])
    observation = {'id': f'feedback-{case_id[:32]}-{len(events)}', 'case_id': case_id,
                   'origin': 'local', 'features': features, 'action': action, 'outcome': outcome,
                   'observed_at': now, 'cost_usd': cost, 'duration_seconds': max(0., now - started_at),
                   'decision_id': decision_id}
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
        item['result_evidence'] = evidence[:2000]
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
        raise CoordinationError('Workspace source version does not match the coordination task base HEAD.', 78)
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
            parts = shlex.split(command)
        except ValueError:
            raise CoordinationError('Invalid declared verification command.', 64) from None
        if not parts:
            raise CoordinationError('Empty declared verification command.', 64)
        if not _command_available(copy, parts[0]):
            missing.append('check-command:' + parts[0])
    if missing:
        evidence = 'missing declared dependencies/check runtime: ' + ', '.join(sorted(set(missing)))
        record_constraint(root, task_id, item['id'], 'dependency_unavailable', evidence)
        raise CoordinationError(
            'Preparation ' + evidence +
            '. Prepare the owned workspace explicitly; stubs are not equivalent verification.', 78)
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
    return "\n".join(lines)
