"""Adapter for supported Codex lifecycle hooks."""
from __future__ import annotations

import json
from pathlib import Path
import re

from . import coordination, settings


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
    session = str(payload.get("session_id") or "")
    if not session:
        return {}
    if event == "UserPromptSubmit":
        policy = settings.resolve(root)
        task = coordination.open_task(
            root, session_id=session, turn_id=str(payload.get("turn_id") or "turn"),
            prompt=str(payload.get("prompt") or ""), policy=policy)
        text = (
            coordination.summary(task) + "\n"
            "Before coordinator source edits for a substantial task, register a concrete "
            "distribution with deepseek-team coordination plan --task " + task["id"] +
            ". For a genuinely small single-output task, record it as small with evidence. "
            "At 75/full-access, worker-eligible implementation/tests/fixtures/docs/metadata "
            "default to DeepSeek unless a supported concrete constraint is recorded. "
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
        for assignment in task.get("assignments", []):
            if assignment.get("status") not in ("planned", "running"):
                continue
            deliverable = next((d for d in task["deliverables"]
                                if d["id"] == assignment["deliverable_id"]), None)
            if not deliverable:
                continue
            for path in paths:
                if any(_overlap(path, scope) for scope in deliverable["scope"]):
                    return _deny(
                        f"Path {path} belongs to pending worker assignment {assignment['id']}; "
                        "wait for/review that result before duplicating implementation.")
        return {}
    if event == "PostToolUse":
        is_mutation, paths = _mutation(payload)
        if is_mutation:
            coordination.record_coordinator_event(root, task["id"], "mutation", paths)
        return {}
    if event == "Stop":
        issues = coordination.validate_task(root, task["id"])
        if issues:
            return {"continue": False, "stopReason": "Distribution remains noncompliant: " + "; ".join(issues)}
        pending = [a["id"] for a in task.get("assignments", [])
                   if a.get("status") in ("planned", "running")]
        if pending:
            return {"continue": False, "stopReason": "Worker assignments are still pending: " + ", ".join(pending)}
        undisposed = [a["id"] for a in task.get("assignments", [])
                      if a.get("status") in ("succeeded", "failed") and not a.get("disposition")]
        if undisposed:
            return {"continue": False, "stopReason": "Completed worker results require a recorded disposition: " + ", ".join(undisposed)}
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
