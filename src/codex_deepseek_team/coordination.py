"""Minimal persistent coordinator/worker ledger.

This is intentionally not a scheduler. It records distribution decisions, assignments,
worker results and dispositions so Codex hooks can enforce process transitions and
restore context after compaction.
"""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
import time
from typing import Any

from . import settings

WORKER_WRITE_KINDS = {"implementation", "test", "fixture", "documentation", "metadata"}
WORKER_READ_KINDS = {"review", "research", "diagnostic", "test_plan"}
PROTECTED_COORDINATOR_KINDS = {
    "architecture", "security", "integration", "final_verification",
    "commit_push", "production", "secret_signing",
}
ACCEPTED_RETENTION = {
    "user_explicit", "secret_or_signing", "runner_unavailable",
    "dependency_unavailable", "environment_incompatible", "not_separable",
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
    key = hashlib.sha256(os.fsencode(str(root))).hexdigest()[:24]
    directory = _state_root() / "coordination" / key
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
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
              policy: settings.Policy) -> dict:
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
            "policy": policy.as_dict(), "deliverables": [], "assignments": [],
            "coordinator_events": [],
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


def _normalize_deliverable(raw: dict) -> dict:
    if not isinstance(raw, dict):
        raise CoordinationError("Each deliverable must be an object.", 64)
    required = {"id", "kind", "scope", "executor", "acceptance", "dependencies", "checks"}
    if not required.issubset(raw):
        raise CoordinationError("Deliverables require id/kind/scope/executor/acceptance/dependencies/checks.", 64)
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", str(raw["id"])):
        raise CoordinationError("Invalid deliverable id.", 64)
    if raw["executor"] not in ("worker", "coordinator"):
        raise CoordinationError("Deliverable executor must be worker or coordinator.", 64)
    value = dict(raw)
    value["scope"] = list(raw["scope"]) if isinstance(raw["scope"], list) else [str(raw["scope"])]
    value["acceptance"] = list(raw["acceptance"])
    value["dependencies"] = list(raw["dependencies"])
    value["checks"] = list(raw["checks"])
    return value


def plan_task(root: Path, task_id: str, plan: dict) -> dict:
    if not isinstance(plan, dict):
        raise CoordinationError("Coordination plan must be an object.", 64)
    classification = plan.get("classification")
    if classification not in ("substantial", "small"):
        raise CoordinationError("classification must be substantial or small.", 64)
    deliverables = [_normalize_deliverable(x) for x in plan.get("deliverables", [])]
    if classification == "small" and (len(deliverables) != 1 or not plan.get("small_evidence")):
        raise CoordinationError("Small tasks require one deliverable and concrete small_evidence.", 64)
    root = _project_root(root)
    with _lock(root):
        task = load_task(root, task_id)
        old = {a["deliverable_id"]: a for a in task.get("assignments", [])
               if a.get("status") not in ("planned",)}
        assignments = []
        for item in deliverables:
            if item["executor"] != "worker":
                continue
            if item["id"] in old:
                assignments.append(old[item["id"]])
                continue
            assignment_id = "as-" + hashlib.sha256(
                f"{task_id}\0{item['id']}".encode()).hexdigest()[:16]
            assignments.append({
                "id": assignment_id, "deliverable_id": item["id"],
                "status": "planned", "workspace_id": None, "runtime": None,
                "prepared_changes": [], "worker_changes": [], "checks": [],
                "result_summary": None, "disposition": None,
            })
        task.update(classification=classification,
                    small_evidence=plan.get("small_evidence"),
                    deliverables=deliverables, assignments=assignments,
                    status="planned", updated_at=time.time())
        _atomic(_task_path(root, task_id), task)
        return task


def _retention_ok(deliverable: dict) -> bool:
    retention = deliverable.get("retention")
    if not isinstance(retention, dict):
        return False
    code = retention.get("code")
    evidence = retention.get("evidence")
    return code in ACCEPTED_RETENTION and isinstance(evidence, str) and len(evidence.strip()) >= 5


def validate_task(root: Path, task_id: str) -> list[str]:
    task = load_task(root, task_id)
    if task.get("classification") is None:
        return ["distribution plan is missing"]
    if task["classification"] == "small":
        return []
    policy = task["policy"]
    level = int(policy["delegation_level"])
    access = policy["effective_access"]
    issues = []
    worker_count = 0
    eligible_count = 0
    for item in task.get("deliverables", []):
        kind, executor = item["kind"], item["executor"]
        if executor == "worker":
            worker_count += 1
        eligible = kind in WORKER_READ_KINDS if access == "read-only" else (
            kind in WORKER_WRITE_KINDS or kind in WORKER_READ_KINDS)
        if eligible:
            eligible_count += 1
        if executor == "coordinator" and kind in PROTECTED_COORDINATOR_KINDS:
            continue
        if executor == "coordinator" and eligible:
            if access == "read-only" and kind in WORKER_WRITE_KINDS:
                continue
            if level >= 75 and not _retention_ok(item):
                issues.append(f"{item['id']}: worker-eligible {kind} retained by coordinator without a supported constraint")
    if level >= 75 and access == "full-access" and eligible_count and worker_count == 0:
        issues.append("75/full-access requires worker assignment for separable worker-eligible deliverables")
    if level == 50 and access == "full-access" and eligible_count >= 2 and worker_count == 0:
        issues.append("50/full-access requires at least one worker implementation slice when separable work exists")
    return issues


def _assignment(task: dict, assignment_id: str) -> dict:
    for row in task.get("assignments", []):
        if row.get("id") == assignment_id:
            return row
    raise CoordinationError("Unknown coordination assignment.", 64)


def assignment_started(root: Path, task_id: str, assignment_id: str,
                       workspace_id: str, runtime: str,
                       prepared_changes: list[str]) -> dict:
    with _lock(root):
        task = load_task(root, task_id)
        row = _assignment(task, assignment_id)
        if row["status"] not in ("planned", "failed"):
            raise CoordinationError("Assignment is already active or completed.", 64)
        row.update(status="running", workspace_id=workspace_id, runtime=runtime,
                   prepared_changes=sorted(set(prepared_changes)),
                   started_at=time.time())
        task.update(status="active", updated_at=time.time())
        _atomic(_task_path(root, task_id), task)
        return task


def assignment_finished(root: Path, task_id: str, assignment_id: str,
                        status: str, result_summary: str,
                        worker_changes: list[str], checks: list[dict]) -> dict:
    if status not in ("succeeded", "failed"):
        raise CoordinationError("Assignment result status must be succeeded or failed.", 64)
    with _lock(root):
        task = load_task(root, task_id)
        row = _assignment(task, assignment_id)
        row.update(status=status, result_summary=str(result_summary)[:4000],
                   worker_changes=sorted(set(worker_changes)), checks=list(checks),
                   finished_at=time.time())
        task["updated_at"] = time.time()
        _atomic(_task_path(root, task_id), task)
        return task


def use_result(root: Path, task_id: str, assignment_id: str,
               disposition: str, evidence: str) -> dict:
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
        task["updated_at"] = time.time()
        _atomic(_task_path(root, task_id), task)
        return task


def record_coordinator_event(root: Path, task_id: str, kind: str,
                             paths: list[str] | None = None) -> None:
    with _lock(root):
        task = load_task(root, task_id)
        task.setdefault("coordinator_events", []).append({
            "kind": kind, "paths": list(paths or []), "at": time.time(),
        })
        task["updated_at"] = time.time()
        _atomic(_task_path(root, task_id), task)


def summary(task: dict) -> str:
    lines = [
        f"DeepSeek Team coordination task {task['id']}: "
        f"{task['policy']['delegation_level']}%/{task['policy']['effective_access']}; "
        f"classification={task.get('classification') or 'pending'}.",
    ]
    for row in task.get("assignments", []):
        text = f"- {row['id']} deliverable={row['deliverable_id']} status={row['status']}"
        if row.get("result_summary"):
            text += f"; result={row['result_summary'][:500]}"
        if row.get("disposition"):
            text += f"; disposition={row['disposition']['kind']}: {row['disposition']['evidence'][:300]}"
        lines.append(text)
    return "\n".join(lines)
