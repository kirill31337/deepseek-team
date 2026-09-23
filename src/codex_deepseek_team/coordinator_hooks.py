"""Shared coordination gates for Codex and Claude Code lifecycle hooks."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import uuid

from . import activation, coordination, project, settings
from .shell_mutation import classify_shell_mutation


def _context(text: str, event: str) -> dict:
    return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}}


def _deny(reason: str) -> dict:
    return {"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason,
    }}


def _paths_from_apply_patch(command: str) -> list[str]:
    return re.findall(r"^\*\*\* (?:(?:Update|Add|Delete) File|Move to): (.+)$", command, re.M)


def _overlap(path: str, scope: str, root: Path, *, descendants: bool = False) -> bool:
    declared = Path(scope)
    declared = declared if declared.is_absolute() else root / declared
    try:
        scope = declared.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return False
    if scope == '.' or (descendants and path == '.'):
        return True
    if scope.endswith("*"):
        return (path.startswith(scope[:-1])
                or (descendants and scope[:-1].startswith(path.rstrip("/") + "/")))
    return (path == scope or path.startswith(scope.rstrip("/") + "/")
            or (descendants and scope.startswith(path.rstrip("/") + "/")))


def _mutation(payload: dict) -> tuple[bool, list[str]]:
    tool = payload.get("tool_name", "")
    data = payload.get("tool_input") or {}
    command = data.get("command", "") if isinstance(data, dict) else ""
    if tool == "apply_patch":
        if isinstance(data, str):
            patch = data
        elif isinstance(data, dict):
            patch = '\n'.join(data[key] for key in ('command', 'input', 'patch')
                              if isinstance(data.get(key), str))
        else:
            patch = ''
        return True, _paths_from_apply_patch(patch)
    if tool in ("Edit", "Write", "NotebookEdit"):
        if not isinstance(data, dict):
            return True, []
        path = data.get("file_path") or data.get("notebook_path") or data.get("path")
        return True, [str(path)] if isinstance(path, str) and path else []
    if tool != "Bash":
        return False, []
    return classify_shell_mutation(command)


_NATIVE_START = frozenset(('spawn_agent', 'Agent', 'Task'))
_NATIVE_MESSAGE = frozenset(('send_message', 'send_input', 'followup_task',
                             'assign_agent_task', 'resume_agent'))
_NATIVE_CONTROLS = frozenset(('id', 'agent_id', 'receiver', 'recipient', 'target',
                              'interrupt', 'agent_ids', 'ids'))
_NATIVE_BINDING = re.compile(
    r'\[deepseek-team:(task-[0-9a-f]{20}):([A-Za-z0-9_.-]{1,80})\]')


def _native_work(payload: dict) -> tuple[bool, str]:
    """Recognize parent dispatches; empty resume/wait controls are not new work."""
    name = str(payload.get('tool_name', '')).replace('/', '.').rsplit('.', 1)[-1]
    if name not in _NATIVE_START | _NATIVE_MESSAGE:
        return False, ''
    data = payload.get('tool_input')
    if not isinstance(data, dict):
        return True, ''
    supplied = [data[key] for key in ('prompt', 'message', 'input', 'items') if key in data]
    has_work = name in _NATIVE_START or any(
        value for key, value in data.items() if key not in _NATIVE_CONTROLS)
    # Accept supported text items, but never treat an unknown nonempty shape as
    # a control-only message. Markers hidden in arbitrary blobs are not bindings.
    texts = []
    for value in supplied:
        if isinstance(value, str):
            texts.append(value)
        elif isinstance(value, list):
            texts.extend(item['text'] for item in value
                         if isinstance(item, dict) and item.get('type') in ('text', 'input_text')
                         and isinstance(item.get('text'), str))
    text = '\n'.join(texts)
    return has_work, text


def _check_native_dispatch(root, task, payload):
    is_work, text = _native_work(payload)
    if not is_work:
        return None
    prefix = 'DeepSeek Team native delegation gate: '
    if task is None:
        return _deny(prefix + 'register a coordination task and route the subtask before delegation.')
    bindings = _NATIVE_BINDING.findall(text)
    if len(bindings) != 1 or text.count('[deepseek-team:') != 1:
        return _deny(prefix + 'route this subtask with executor:auto first. An approved native '
                     'exception requires exactly one [deepseek-team:TASK_ID:DELIVERABLE_ID] '
                     'marker in its prompt or message; small/read-only work is included.')
    task_id, deliverable_id = bindings[0]
    if task_id != task['id']:
        return _deny(prefix + 'the native dispatch marker must refer to the active coordination task.')
    try:
        coordination.authorize_native_dispatch(
            root, task_id, deliverable_id, str(payload.get('tool_name', '')),
            str(payload.get('tool_use_id') or ''))
    except coordination.CoordinationError as error:
        return _deny(prefix + str(error))
    return {}


def handle(payload: dict, runtime: str = 'codex') -> dict:
    if runtime not in ('codex', 'claude'):
        raise ValueError('Unsupported coordinator runtime.')
    # Claude also invokes user hooks inside native subagents. The main thread
    # owns this ledger; subagents must not open or complete its task.
    if runtime == 'claude' and payload.get('agent_id'):
        return {}
    event = payload.get("hook_event_name")
    cwd = Path(payload.get("cwd") or ".")
    root = settings.project_root(cwd)
    if root is None:
        return {}
    try:
        if not project.is_attached(root, runtime):
            return {}
    except project.ProjectError:
        return {}
    if not activation.resolve(root).enabled:
        if event in ('SessionStart', 'UserPromptSubmit'):
            return _context(activation.DISABLED_GUIDANCE, event)
        return {}
    session = str(payload.get("session_id") or "")
    if not session:
        return {}
    if event == "UserPromptSubmit":
        policy = settings.resolve(root)
        task = coordination.begin_turn(
            root, session_id=session, turn_id=str(payload.get("turn_id") or uuid.uuid4().hex),
            prompt=str(payload.get("prompt") or ""), policy=policy, runtime=runtime)
        coordination.sync_routing_feedback(root, task['id'])
        effort_context = (
            "Effort policy is auto: choose low, high or max for each DeepSeek assignment "
            "from task complexity and pass it explicitly (the legacy medium spelling is "
            "accepted as high). "
            if policy.effort == "auto" else
            f"Effort policy is forced to {policy.effort}: use that level for new DeepSeek assignments. "
        )
        text = (
            coordination.summary(task) + "\n"
            "Before delegating any subtask (including small/read-only research), or before "
            "coordinator source edits for a substantial task, register a concrete "
            "distribution with deepseek-team coordination plan --task " + task["id"] +
            ". For a genuinely small single-output task, record it as small with evidence. "
            "At 75/full-access, worker-eligible implementation/tests/fixtures/docs/metadata "
            "default to DeepSeek unless a supported concrete constraint is recorded. "
            "In Auto route delegated work with executor:auto first. Native-agent exceptions require "
            "delegation_reason and native_exception for explicit_user_request or native_capability "
            "with evidence; parallelism/isolated context alone is insufficient. Include "
            "[deepseek-team:TASK_ID:DELIVERABLE_ID] in an approved native prompt/message. "
            "New work sent to an existing agent also requires routing. Simple coordinator-only "
            "answers need no artificial worker. " + effort_context +
            f"Run assigned workers with --runtime {runtime}. "
            "Do not report a useful-work percentage from counts. "
            "Capture a structured features card before execution. In delegation Auto, use executor:auto "
            "to resolve each eligible task from local learning and bounded public evidence. Saved manual "
            "25/50/75 profiles retain priority and continue learning from outcomes. Routing never grants access. "
            "Record worker acceptance/rework via coordination use and verified coordinator/native-agent "
            "outcomes via coordination result before completion. An unplanned turn with no work closes "
            "without a distribution plan; an existing unfinished task remains open. "
            "Supply measured total --cost-usd only when known; never invent subscription costs.\n"
            + settings.final_reporting_guidance()
        )
        return _context(text, event)
    task = coordination.latest_task(root, session)
    if event == "SessionStart":
        policy = settings.resolve(root)
        base = (
            f"DeepSeek Team effective profile: {str(policy.delegation_level) + '%' if policy.delegation_level != 'auto' else 'Auto'}/"
            f"{policy.effective_access}; effort={policy.effort}. "
            f"{'Claude Code' if runtime == 'claude' else 'Codex'} lifecycle enforcement "
            "is active for this explicitly attached project. "
            f"Run assigned workers with --runtime {runtime}. "
        )
        if task:
            base += coordination.summary(task)
        else:
            base += "No active coordination task is recorded for this session."
        if payload.get("source") == "compact":
            base += " This state is restored from the persistent ledger after compaction."
        if runtime == 'claude':
            # Claude SessionStart already appends settings.instructions, which now
            # includes the shared reporting contract; do not duplicate the full text.
            base += '\n' + settings.instructions(policy, runtime)
        else:
            base += '\n' + settings.final_reporting_guidance()
        return _context(base, event)
    if event == 'PreToolUse':
        native = _check_native_dispatch(root, task, payload)
        if native is not None:
            return native
    if task is None:
        return {}
    coordination.sync_routing_feedback(root, task['id'])
    if runtime == 'claude' and payload.get('permission_mode') == 'plan':
        # Native plan files are written before a source-work distribution exists.
        # Do not exempt source edits merely because Claude is in plan mode.
        if event == 'Stop':
            return {}
        if event == 'PreToolUse' and payload.get('tool_name') in ('Edit', 'Write'):
            from . import claude_config
            data = payload.get('tool_input') or {}
            path = data.get('file_path') if isinstance(data, dict) else None
            directory = claude_config.plans_directory(root)
            if isinstance(path, str) and directory is not None:
                candidate = Path(path)
                candidate = (candidate if candidate.is_absolute() else cwd / candidate).resolve()
                if candidate.parent == directory and candidate.suffix == '.md':
                    return {}
    if event == "PreToolUse":
        is_mutation, paths = _mutation(payload)
        if not is_mutation:
            return {}
        issues = coordination.validate_task(root, task["id"])
        if issues:
            return _deny("DeepSeek Team distribution gate: " + "; ".join(issues))
        if payload.get('tool_name') == 'apply_patch' and not paths:
            return _deny('Patch targets could not be mapped to the registered distribution.')
        policy = task.get("policy", {})
        if not paths and task.get("classification") == "substantial" and (
                policy.get("delegation_level", 'auto') in ('auto', 75)
                and policy.get("effective_access") == "full-access"):
            return _deny(
                "Unscoped mutating Bash cannot be mapped to the registered Auto/75 full-access "
                "distribution. Revise the plan or use a file edit whose scope can be checked.")
        normalized_paths = []
        for path in paths:
            # Claude Edit/Write normally provide absolute paths. Resolve from
            # the hook cwd, then compare project-relative scopes. Symlinks may
            # not turn a planned project file into a write outside the project.
            candidate = Path(path)
            candidate = candidate if candidate.is_absolute() else cwd / candidate
            try:
                path = candidate.resolve().relative_to(root.resolve()).as_posix()
            except ValueError:
                return _deny('Mutation scope is outside the attached project: ' + path)
            normalized_paths.append(path)
            matching = [
                deliverable for deliverable in task.get("deliverables", [])
                if any(_overlap(path, scope, root) for scope in deliverable.get("scope", []))
            ]
            if not matching:
                return _deny(
                    f"Unplanned mutation scope {path}. Revisit the distribution before "
                    "starting a new block of work.")
            # A directory write also conflicts with worker-owned descendants.
            for deliverable in task.get("deliverables", []):
                if not any(_overlap(path, scope, root, descendants=True)
                           for scope in deliverable.get("scope", [])):
                    continue
                assignment = next((row for row in task.get("assignments", [])
                                   if row.get("deliverable_id") == deliverable.get("id")), None)
                if assignment and assignment.get("status") in ("planned", "running"):
                    return _deny(
                        f"Path {path} belongs to pending worker assignment {assignment['id']}; "
                        "wait for/review that result before duplicating implementation.")
        coordination.record_coordinator_event(root, task["id"], "mutation_requested", normalized_paths)
        return {}
    if event == "Stop":
        # A conversational/read-only turn need not invent implementation work.
        # The ledger guard refuses closure if a plan or work already exists.
        if coordination.close_unstarted_task(root, task["id"]):
            return {"continue": True}
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
        reasons.extend(coordination.completion_issues(task))
        if reasons:
            reason = " ".join(reasons)
            if not payload.get("stop_hook_active"):
                return {"decision": "block", "reason": reason}
            # Bound our own retry without vetoing another hook's continuation.
            # The ledger stays unfinished and the runtime receives a warning.
            return {"systemMessage": "DeepSeek Team task remains unfinished. " + reason}
        coordination.complete_task(root, task["id"])
        return {"continue": True}
    return {}


def main(argv=None) -> int:
    import sys
    parser = argparse.ArgumentParser(prog='deepseek-team coordinator-hook')
    parser.add_argument('--runtime', choices=['codex', 'claude'], default='codex')
    args = parser.parse_args(argv)
    try:
        payload = json.load(sys.stdin)
        result = handle(payload, runtime=args.runtime)
        if result:
            print(json.dumps(result))
        return 0
    except Exception as error:
        print(json.dumps({"systemMessage": f"DeepSeek Team hook error: {type(error).__name__}"}))
        return 1
