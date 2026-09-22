"""Adapter for supported Codex lifecycle hooks."""
from __future__ import annotations

import json
from pathlib import Path
import re

from . import coordination, project, settings


def _context(text: str, event: str) -> dict:
    return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}}


def _deny(reason: str) -> dict:
    return {"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason,
    }}


def _paths_from_apply_patch(command: str) -> list[str]:
    return re.findall(r"^\*\*\* (?:Update|Add|Delete) File: (.+)$", command, re.M)


def _overlap(path: str, scope: str) -> bool:
    if scope.endswith("*"):
        return path.startswith(scope[:-1])
    return path == scope or path.startswith(scope.rstrip("/") + "/")


def _mutation(payload: dict) -> tuple[bool, list[str]]:
    tool = payload.get("tool_name", "")
    data = payload.get("tool_input") or {}
    command = data.get("command", "") if isinstance(data, dict) else ""
    if tool == "apply_patch":
        return True, _paths_from_apply_patch(command)
    if tool in ("Edit", "Write"):
        if not isinstance(data, dict):
            return True, []
        path = data.get("file_path") or data.get("path")
        return True, [str(path)] if isinstance(path, str) and path else []
    if tool != "Bash":
        return False, []
    if "deepseek-team coordination" in command or "deepseek-team worker" in command:
        return False, []
    patterns = (r"(^|[;&|]\s*)(rm|mv|cp|touch|truncate|patch)\s",
                r"sed\s+[^\n]*-i", r"git\s+apply\b", r"\btee\s",
                r"(^|[^>])>{1,2}\s*[^&]")
    return any(re.search(p, command) for p in patterns), []


def handle(payload: dict) -> dict:
    event = payload.get("hook_event_name")
    cwd = Path(payload.get("cwd") or ".")
    root = settings.project_root(cwd)
    if root is None:
        return {}
    try:
        if not project.is_attached(root, 'codex'):
            return {}
    except project.ProjectError:
        return {}
    session = str(payload.get("session_id") or "")
    if not session:
        return {}
    if event == "UserPromptSubmit":
        policy = settings.resolve(root)
        task = coordination.begin_turn(
            root, session_id=session, turn_id=str(payload.get("turn_id") or "turn"),
            prompt=str(payload.get("prompt") or ""), policy=policy)
        effort_context = (
            "Effort policy is auto: choose low, medium or high for each DeepSeek assignment "
            "from task complexity and pass it explicitly. "
            if policy.effort == "auto" else
            f"Effort policy is forced to {policy.effort}: use that level for new DeepSeek assignments. "
        )
        text = (
            coordination.summary(task) + "\n"
            "Before coordinator source edits for a substantial task, register a concrete "
            "distribution with deepseek-team coordination plan --task " + task["id"] +
            ". For a genuinely small single-output task, record it as small with evidence. "
            "At 75/full-access, worker-eligible implementation/tests/fixtures/docs/metadata "
            "default to DeepSeek unless a supported concrete constraint is recorded. "
            "Coordinator-native subagents are also allowed when the plan uses executor "
            "native-agent with a concrete delegation_reason; they complement and do not replace "
            "required DeepSeek worker assignments. " + effort_context +
            "Do not report a useful-work percentage from counts."
        )
        return _context(text, event)
    task = coordination.latest_task(root, session)
    if event == "SessionStart":
        policy = settings.resolve(root)
        base = (
            f"DeepSeek Team effective profile: {policy.delegation_level}%/"
            f"{policy.effective_access}. Codex lifecycle enforcement is active for this "
            "explicitly attached project. "
        )
        if task:
            base += coordination.summary(task)
        else:
            base += "No active coordination task is recorded for this session."
        if payload.get("source") == "compact":
            base += " This state is restored from the persistent ledger after compaction."
        return _context(base, event)
    if task is None:
        return {}
    if event == "PreToolUse":
        is_mutation, paths = _mutation(payload)
        if not is_mutation:
            return {}
        issues = coordination.validate_task(root, task["id"])
        if issues:
            return _deny("DeepSeek Team distribution gate: " + "; ".join(issues))
        policy = task.get("policy", {})
        if not paths and task.get("classification") == "substantial" and (
                int(policy.get("delegation_level", 25)) >= 75
                and policy.get("effective_access") == "full-access"):
            return _deny(
                "Unscoped mutating Bash cannot be mapped to the registered 75/full-access "
                "distribution. Revise the plan or use a file edit whose scope can be checked.")
        for path in paths:
            matching = [
                deliverable for deliverable in task.get("deliverables", [])
                if any(_overlap(path, scope) for scope in deliverable.get("scope", []))
            ]
            if not matching:
                return _deny(
                    f"Unplanned mutation scope {path}. Revisit the distribution before "
                    "starting a new block of work.")
            for deliverable in matching:
                assignment = next((row for row in task.get("assignments", [])
                                   if row.get("deliverable_id") == deliverable.get("id")), None)
                if assignment and assignment.get("status") in ("planned", "running"):
                    return _deny(
                        f"Path {path} belongs to pending worker assignment {assignment['id']}; "
                        "wait for/review that result before duplicating implementation.")
        coordination.record_coordinator_event(root, task["id"], "mutation_requested", paths)
        return {}
    if event == "Stop":
        issues = coordination.validate_task(root, task["id"])
        pending = [a["id"] for a in task.get("assignments", [])
                   if a.get("status") in ("planned", "running")]
        undisposed = [a["id"] for a in task.get("assignments", [])
                      if a.get("status") in ("succeeded", "failed") and not a.get("disposition")]
        reasons = []
        if issues:
            reasons.append("Distribution remains noncompliant: " + "; ".join(issues))
        if pending:
            reasons.append("Worker assignments are still pending: " + ", ".join(pending))
        if undisposed:
            reasons.append("Completed worker results require a recorded disposition: " + ", ".join(undisposed))
        if reasons:
            reason = " ".join(reasons)
            if not payload.get("stop_hook_active"):
                return {"decision": "block", "reason": reason}
            return {"continue": False, "stopReason": reason, "systemMessage": reason}
        coordination.complete_task(root, task["id"])
        return {"continue": True}
    return {}


def main() -> int:
    import sys
    try:
        payload = json.load(sys.stdin)
        result = handle(payload)
        if result:
            print(json.dumps(result))
        return 0
    except Exception as error:
        print(json.dumps({"systemMessage": f"DeepSeek Team hook error: {type(error).__name__}"}))
        return 1
