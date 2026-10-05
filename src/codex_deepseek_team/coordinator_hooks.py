"""Shared coordination gates for Codex and Claude Code lifecycle hooks."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import time
import uuid

from . import (activation, coordination, coordinator_activity, lessons, project,
                 scope_matching, settings, state_storage)
from .shell_mutation import classify_shell_mutation


def _context(text: str, event: str) -> dict:
    return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}}


def _deny(reason: str) -> dict:
    return {"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason,
    }}


def _sync_lessons_feedback(root, task_id: str) -> None:
    """Retry the durable lessons outbox; a hook never fails on it."""
    try:
        coordination.sync_lessons_feedback(root, task_id)
    except coordination.CoordinationError:
        pass  # The unrecorded event stays queued for the next prompt/status call.


def _lessons_guidance(root) -> str:
    """Advisory root-specific rules for a coordinator prompt or session start.

    Reuses the lessons service renderer; unreadable private state never breaks a
    lifecycle hook. Matching rules reach the coordinator before it chooses a
    brief, while the assignment snapshot records which rules were actually
    selected - it is not proof that the coordinator followed them.
    """
    try:
        return lessons.render_guidance(root)
    except lessons.LessonError:
        return ''


def _lessons_due_reminder(root) -> str:
    """Nonblocking completed-task reminder; empty unless a review is really due."""
    try:
        if not lessons.status(root).get('review_due'):
            return ''
        return lessons.render_guidance(root)
    except lessons.LessonError:
        return ''


def _refresh_diagnostic(problem: str, runtime: str) -> str:
    """Report incomplete refresh without claiming a blocked rollback succeeded."""
    return ('DeepSeek Team could not refresh the managed instruction block(s): ' + problem
            + ' Review the affected files and repair the reported problem, then rerun '
            f'deepseek-team init --coordinator {runtime} . from the repository root. '
            'The current policy below applies to this session.')


def _binding_diagnostic(error, runtime: str) -> str:
    """Startup/prompt diagnostic for a malformed owned binding we never adopt."""
    return ('DeepSeek Team found a malformed managed-block binding for ' + runtime + ': '
            + str(error) + ' This project is not treated as attached and the file was not '
            'modified. Repair or remove the conflicting block, then rerun '
            'deepseek-team init --coordinator ' + runtime + ' . from the repository root '
            'to attach it cleanly.')


def _with_diagnostic(text: str, diagnostic: str) -> str:
    return diagnostic + '\n' + text if diagnostic else text


def _paths_from_apply_patch(command: str) -> list[str]:
    return re.findall(r"^\*\*\* (?:(?:Update|Add|Delete) File|Move to): (.+)$", command, re.M)


def _declared_scopes(root: Path, deliverable: dict) -> list[str]:
    """Canonical project-relative scopes; external scopes never grant authority."""
    found = []
    for raw in deliverable.get("scope", []):
        canonical = scope_matching.canonical(root, raw)
        if canonical is not None:
            found.append(canonical)
    return found


def _mutation(payload: dict) -> tuple[bool, list[str]]:
    tool = coordinator_activity.normalize_tool_name(payload.get("tool_name", ""))
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
    if not coordinator_activity.is_shell_tool(tool):
        return False, []
    if isinstance(data, dict):
        # Codex sends exec_command as ``cmd`` while shell_command uses ``command``.
        for key in ("cmd", "shell", "script"):
            value = data.get(key)
            if not command and isinstance(value, str):
                command = value
    return classify_shell_mutation(command if isinstance(command, str) else "")


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


_EARLY_PLAN_NUDGE = (
    "DeepSeek Team early-planning gate: this is the first recognized source inspection of an "
    "unclassified task. Register a concrete distribution (classification and deliverables) with "
    "`deepseek-team coordination plan --task {task_id}` before further investigation. Status, "
    "bootstrap and instruction reads stay exempt, but another recognized source read is denied "
    "until the task is classified."
)
_EARLY_PLAN_DENY = (
    "DeepSeek Team early-planning gate: this task is still unclassified after an earlier source "
    "inspection was recorded. Register a concrete distribution with "
    "`deepseek-team coordination plan --task {task_id}`; further recognized source reads are "
    "denied until then."
)


def _source_inspection_gate(root, task, payload, cwd):
    """Nudge then deny recognized source inspection of an unclassified task.

    The first recognized read records one ``source_inspection`` coordinator
    event, which also makes the turn count as work for Stop, and returns
    ``additionalContext``. A repeated delivery of the same nonempty
    ``tool_use_id`` is idempotent; any other recognized read is denied until the
    coordinator registers a distribution.
    """
    if task.get('status') != 'planning' or task.get('classification') is not None:
        return {}
    data = payload.get('tool_input')
    verdict = coordinator_activity.classify_inspection(
        str(payload.get('tool_name') or ''), data if isinstance(data, dict) else {},
        root, cwd=cwd)
    if verdict != 'source':
        return {}
    task_id = task['id']
    nudge = _context(_EARLY_PLAN_NUDGE.format(task_id=task_id), 'PreToolUse')
    tool_use_id = str(payload.get('tool_use_id') or '').strip()
    with coordination._lock(root):
        current = coordination.load_task(root, task_id)
        if current.get('classification') is not None or current.get('status') != 'planning':
            return {}
        events = current.get('coordinator_events')
        if not isinstance(events, list):
            events = []
        inspections = [event for event in events
                       if isinstance(event, dict) and event.get('kind') == 'source_inspection']
        if tool_use_id and any(event.get('tool_use_id') == tool_use_id
                               for event in inspections):
            return nudge
        if inspections:
            return _deny(_EARLY_PLAN_DENY.format(task_id=task_id))
        now = time.time()
        events.append({'kind': 'source_inspection', 'paths': [], 'at': now,
                       'tool_use_id': tool_use_id,
                       'tool_name': str(payload.get('tool_name') or '')})
        current['coordinator_events'] = events
        current['updated_at'] = now
        coordination._atomic(coordination._task_path(root, task_id), current)
    return nudge


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
        attached = project.is_attached(root, runtime)
    except project.ProjectError as error:
        # A malformed owned binding is never adopted: surface it on the lifecycle
        # events and leave every related event otherwise untouched.
        if event in ('SessionStart', 'UserPromptSubmit'):
            return _context(_binding_diagnostic(error, runtime), event)
        return {}
    if not attached:
        return {}
    lifecycle = event in ('SessionStart', 'UserPromptSubmit')
    policy = None
    resolve_error = None
    diagnostic = ''
    refresh_changed = False
    if lifecycle:
        # Refresh both existing owned blocks from the current settings before the
        # events that preload project instructions, so a stale or disabled snapshot
        # cannot govern this turn. Off resolves to the disabled policy and bypasses
        # the ledger/routing/delegation gates below.
        try:
            policy = settings.resolve(root)
        except settings.SettingsError as error:
            resolve_error = error
        if policy is not None:
            refresh_changed, problem = project.refresh_attached(root, policy)
            if problem:
                diagnostic = _refresh_diagnostic(problem, runtime)
        enabled = policy.enabled if policy is not None else activation.resolve(root).enabled
        if not enabled:
            return _context(_with_diagnostic(activation.DISABLED_GUIDANCE, diagnostic), event)
        if policy is None:
            raise resolve_error
    elif not activation.resolve(root).enabled:
        return {}
    # Every enabled event must honor an unavailable root before ledger access.
    # Otherwise tool/stop events could revive old tasks after bootstrap declined
    # storage, imposing gates despite its local-continuation guidance.
    try:
        state_storage.prepare_storage()
    except state_storage.StorageError as error:
        return _context(_with_diagnostic(
            'DeepSeek worker storage is unavailable: ' + str(error) + '\n'
            'Resolve storage readiness before planning or launching a worker. '
            'Continue locally while it is unavailable; no worker or provider request '
            'was started by this hook.', diagnostic), event)
    session = str(payload.get("session_id") or "")
    if not session:
        return {}
    if event == "UserPromptSubmit":
        task = coordination.begin_turn(
            root, session_id=session, turn_id=str(payload.get("turn_id") or uuid.uuid4().hex),
            prompt=str(payload.get("prompt") or ""), policy=policy, runtime=runtime)
        coordination.sync_routing_feedback(root, task['id'])
        _sync_lessons_feedback(root, task['id'])
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
            "Supply measured total --cost-usd only when known; never invent subscription costs. "
            "Record worker dispositions with cause/severity/summary/prevention when a result "
            "needs corrections (deepseek-team coordination use --rework-json FILE|-), and "
            "review the private journal when a lessons review is due.\n"
        )
        if refresh_changed or diagnostic:
            # The on-disk snapshot changed or could not be refreshed; inject the
            # complete current policy so a preloaded stale file cannot govern this
            # turn. settings.instructions already carries the reporting/rework text.
            text = text + settings.instructions(policy, runtime)
        else:
            text = text + settings.final_reporting_guidance() + settings.rework_guidance(runtime)
        text = _with_diagnostic(text, diagnostic)
        guidance = _lessons_guidance(root)
        if guidance:
            text = text + "\n" + guidance
        return _context(text, event)
    task = (coordination.active_task(root, session)
            or coordination.latest_task(root, session))
    if event == "SessionStart":
        base = (
            f"DeepSeek Team effective profile: {str(policy.delegation_level) + '%' if policy.delegation_level != 'auto' else 'Auto'}/"
            f"{policy.effective_access}; effort={policy.effort}. "
            f"{'Claude Code' if runtime == 'claude' else 'Codex'} lifecycle enforcement "
            "is active for this explicitly attached project. "
            f"Run assigned workers with --runtime {runtime}. "
        )
        if task:
            base += coordination.summary(task)
            others = [row["id"] for row in coordination.unfinished_tasks(root, session)
                      if row.get("id") != task.get("id")]
            if others:
                base += (" Other unfinished coordination tasks remain in this session: "
                         + ", ".join(others) + ".")
        else:
            base += "No active coordination task is recorded for this session."
        if payload.get("source") == "compact":
            base += " This state is restored from the persistent ledger after compaction."
        # Both runtimes inject the complete current policy on session start, so the
        # current loaded policy overrides any stale snapshot. settings.instructions
        # already carries the shared reporting contract and the graded-review rubric;
        # they are never appended a second time.
        base += '\n' + settings.instructions(policy, runtime)
        base = _with_diagnostic(base, diagnostic)
        guidance = _lessons_guidance(root)
        if guidance:
            base += '\n' + guidance
        return _context(base, event)
    if event == 'PreToolUse':
        native = _check_native_dispatch(root, task, payload)
        if native is not None:
            return native
    if task is None:
        return {}
    coordination.sync_routing_feedback(root, task['id'])
    _sync_lessons_feedback(root, task['id'])
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
            return _source_inspection_gate(root, task, payload, cwd)
        issues = coordination.validate_task(root, task["id"])
        if issues:
            return _deny("DeepSeek Team distribution gate: " + "; ".join(issues))
        if coordinator_activity.normalize_tool_name(payload.get('tool_name')) == 'apply_patch' and not paths:
            return _deny('Patch targets could not be mapped to the registered distribution.')
        protected_kinds = coordination.PROTECTED_COORDINATOR_KINDS
        policy = task.get("policy", {})
        if not paths:
            if coordinator_activity.has_protected_deliverable(task, protected_kinds):
                return _deny(
                    "DeepSeek Team protected-scope gate: an unmapped mutation cannot be checked "
                    "against protected coordinator scope. Target an exact declared decision "
                    "artifact or integration write_scope file instead.")
            if task.get("classification") == "substantial" and (
                    policy.get("delegation_level", 'auto') in ('auto', 75)
                    and policy.get("effective_access") == "full-access"):
                return _deny(
                    "Unscoped mutating Bash cannot be mapped to the registered Auto/75 full-access "
                    "distribution. Revise the plan or use a file edit whose scope can be checked.")
        scope_tasks = coordination.unfinished_tasks(root, session) or [task]
        normalized_paths = []
        for raw in paths:
            # Claude Edit/Write normally provide absolute paths. Resolve from
            # the hook cwd, then compare project-relative scopes. Symlinks may
            # not turn a planned project file into a write outside the project.
            path = scope_matching.canonical(root, raw, cwd=cwd)
            if path is None:
                return _deny('Mutation scope is outside the attached project: ' + raw)
            normalized_paths.append(path)
            # A pending worker owns its registered scope across every unfinished
            # task, so precedence beats coordinator authority for that path.
            for owner in scope_tasks:
                for deliverable in owner.get("deliverables", []):
                    if not any(scope_matching.mutation_overlaps(root, path, scope)
                               for scope in _declared_scopes(root, deliverable)):
                        continue
                    assignment = next((row for row in owner.get("assignments", [])
                                       if row.get("deliverable_id") == deliverable.get("id")), None)
                    if assignment and assignment.get("status") in ("planned", "running"):
                        return _deny(
                            f"Path {path} belongs to pending worker assignment {assignment['id']}; "
                            "wait for/review that result before duplicating implementation.")
            allowed, problem = coordinator_activity.mutation_authorizers(
                root, task, path, protected_kinds=protected_kinds,
                worker_write_kinds=coordination.WORKER_WRITE_KINDS)
            if not allowed:
                if problem:
                    return _deny(problem)
                return _deny(
                    f"Unplanned mutation scope {path}. Revisit the distribution before "
                    "starting a new block of work.")
        for affected in scope_tasks:
            if affected['id'] == task['id'] or any(
                    coordination._mutation_touches_scope(root, normalized_paths, item.get('scope', []))
                    for item in affected.get('deliverables', [])):
                coordination.record_coordinator_event(
                    root, affected['id'], 'mutation_requested', normalized_paths)
        return {}
    if event == "Stop":
        reasons = []
        completed = False
        for unfinished in coordination.unfinished_tasks(root, session):
            # Closing a newer draft or completed plan must not hide older work.
            if coordination.close_unstarted_task(root, unfinished['id']):
                continue
            issues = coordination.validate_task(root, unfinished['id'])
            pending = [a['id'] for a in unfinished.get('assignments', [])
                       if a.get('status') in ('planned', 'running')]
            undisposed = [a['id'] for a in unfinished.get('assignments', [])
                          if a.get('status') in ('succeeded', 'failed') and not a.get('disposition')]
            task_reasons = []
            if issues:
                task_reasons.append('Distribution remains noncompliant: ' + '; '.join(issues))
            if pending:
                task_reasons.append('Worker assignments are still pending: ' + ', '.join(pending))
            if undisposed:
                task_reasons.append('Completed worker results require a recorded disposition: '
                                    + ', '.join(undisposed))
            task_reasons.extend(coordination.completion_issues(unfinished))
            if task_reasons:
                reasons.append(unfinished['id'] + ': ' + ' '.join(task_reasons))
            else:
                coordination.complete_task(root, unfinished['id'])
                completed = True
        if reasons:
            reason = " ".join(reasons)
            if not payload.get("stop_hook_active"):
                return {"decision": "block", "reason": reason}
            # Bound our own retry without vetoing another hook's continuation.
            # The ledger stays unfinished and the runtime receives a warning.
            return {"systemMessage": "DeepSeek Team task remains unfinished. " + reason}
        if completed:
            # Advisory, never a veto: a due lessons review only adds a
            # nonblocking systemMessage at a completed-task boundary.
            reminder = _lessons_due_reminder(root)
            if reminder:
                return {"continue": True,
                        "systemMessage": "DeepSeek Team delegation lessons review is due. "
                                         + reminder}
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
