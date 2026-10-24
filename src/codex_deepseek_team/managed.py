"""One managed development job; no scheduler and no automatic writer retry."""
from __future__ import annotations

from contextlib import contextmanager, ExitStack, nullcontext
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time

from . import (check_evidence, check_reuse, coordination, development,
               preparation_guard, prepared_inputs, relay, runtime_preflight,
               settings, toolchains, workspace,
               execution_allocation, storage_budget)


_UNCERTAIN_READINESS_MARKERS = (
    'timed out', 'timeout', 'time budget',
    'could not prepare a private probe directory',
    'evidence could not be captured',
    'could not report its capabilities',
    'could not create the required worker namespace',
    'sandbox probe could not run',
    'namespace context could not be prepared',
)


def _uncertain_readiness(error) -> bool:
    message = str(getattr(error, 'message', error)).lower()
    return any(marker in message for marker in _UNCERTAIN_READINESS_MARKERS)


def _guard_declarations(source, task_id, assignment_id):
    """Non-secret declared dependency/check inputs for the condition fingerprint."""
    if not task_id:
        return {'dependencies': [], 'checks': []}
    try:
        task = coordination.load_task(source, task_id)
        item = coordination.assignment_deliverable(task, assignment_id)
    except (coordination.CoordinationError, OSError):
        return {'dependencies': None}  # Unknown declaration identity: no guard.
    return {'dependencies': item.get('dependencies', []),
            'checks': item.get('checks', [])}


@contextmanager
def _runtime_environment(copy):
    # Both homes and control files belong to the job, including after SIGKILL.
    # A pinned /proc alias keeps AF_UNIX addresses short for nested state roots.
    with tempfile.TemporaryDirectory(prefix='runtime-', dir=copy.directory) as temporary:
        root = Path(temporary)
        with execution_allocation.runtime(root):
            home, control = root / 'home', root / 'control'
            home.mkdir(mode=0o700)
            control.mkdir(mode=0o700)
            fd = os.open(control, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
            try:
                yield home, control, Path('/proc/self/fd') / str(fd) / 'provider.sock'
            finally:
                os.close(fd)


@contextmanager
def _preparation_status(copy):
    """Record failure while holding the copy lock; preserve earlier attempts."""
    try:
        yield
    except BaseException as error:
        _mark_preparation_failed(copy, error)
        raise


def _mark_preparation_failed(copy, error):
    # Preserve earlier execution results when an explicit resume probe fails.
    if (copy.metadata.get('status') == 'ready' or
            (copy.metadata.get('status') == 'succeeded' and
             copy.metadata.get('operation') == 'preparation')):
        code = 130 if isinstance(error, KeyboardInterrupt) else getattr(error, 'code', 71)
        try:
            copy.failed('preparation', code)
        except (workspace.WorkspaceError, OSError):
            pass  # Reconcile the independent coordination record as well.


def run(args, policy: settings.Policy, api, copy=None) -> int:
    """Resolve containment before credential access; preserve every started copy."""
    if args.os_sandbox != 'required':
        raise api.WorkerError(64, 'Managed access requires --os-sandbox required; no unsafe fallback.')
    writable = policy.effective_access == 'full-access'
    if getattr(args, 'attempts_explicit', False) and args.attempts != 1:
        raise api.WorkerError(64, 'Managed copies use one attempt; inspect and explicitly continue instead of retrying.')
    task = args.task if args.task is not None else api.sys.stdin.read()
    requested_effort = getattr(args, 'effort', None)
    effort = api.DEFAULT_EFFORT if requested_effort in (None, 'auto') else api.effort_level(requested_effort)
    if not task.strip():
        raise api.WorkerError(64, 'Pass a task on stdin or as one argument.')
    source = copy.source if copy else Path.cwd()
    coord_task = getattr(args, 'coord_task', None)
    coord_assignment = getattr(args, 'coord_assignment', None)
    linked_correction = False
    if coord_task and coord_assignment:
        try:
            linked = coordination.validate_assignment_start(
                source, coord_task, coord_assignment, explicit_workspace=copy)
        except coordination.CoordinationError as error:
            raise api.WorkerError(error.code, str(error)) from None
        if linked is not None:
            copy = linked
            source = copy.source
            linked_correction = True
    slot = api.acquire_job_slot(args, policy=policy, root=source)
    started = time.monotonic()
    coord_started = False
    runtime = None
    preparation_cause = 'environment'
    # A freshly created copy and a successfully declaratively prepared copy are
    # new jobs even when the caller supplies --workspace. A failed, executing or
    # explicitly resumed copy is never silently treated as a fresh job.
    copy_is_fresh = copy is None or preparation_guard.fresh_copy(copy)
    new_job = copy_is_fresh and not getattr(args, 'resume_after_failure', False)
    guard_conditions = None

    def guard_failure(category, code, *, uncertain=False):
        if (not new_job or guard_conditions is None
                or guard_conditions.fingerprint is None or code in (124, 130)
                or uncertain):
            return
        try:
            preparation_guard.record_failure(
                args.state_dir, guard_conditions, category, exit_code=code,
                workspace_id=copy.id if copy is not None else None)
        except (preparation_guard.PreparationGuardError, OSError):
            print('Preparation guard record was not stored; the observed failure was '
                  'preserved and no automatic retry was attempted.', file=api.sys.stderr)

    early_owner = ExitStack()
    try:
        if copy is not None and coord_task and coord_assignment:
            # Explicit and linked copies need association before guard/sandbox
            # failures too. Keep the owner lock through the eventual execution.
            if not copy._locked:
                early_owner.enter_context(copy.lock(recover=bool(
                    getattr(args, 'resume_after_failure', False) or linked_correction)))
            coordination.assignment_preparing(
                copy.source, coord_task, coord_assignment, copy)
        if new_job:
            try:
                guard_conditions = preparation_guard.worker_conditions(
                    copy.source if copy is not None else source,
                    runtime=args.runtime, codex=args.codex, claude=args.claude,
                    codex_explicit=getattr(args, 'codex_explicit', False),
                    claude_explicit=getattr(args, 'claude_explicit', False),
                    state=args.state_dir,
                    declarations=_guard_declarations(source, coord_task, coord_assignment),
                    copy=copy)
                if (not getattr(args, 'recheck_environment', False)
                        and guard_conditions.fingerprint is not None):
                    blocked = preparation_guard.lookup(
                        args.state_dir, guard_conditions)
                    if blocked is not None:
                        raise api.WorkerError(
                            78, preparation_guard.blocked_message(
                                blocked,
                                copy_id=copy.id if copy is not None else None))
            except preparation_guard.PreparationGuardError as error:
                raise api.WorkerError(error.code, str(error)) from None
        try:
            sb, backend = api.resolve_os_sandbox('required')
        except BaseException as error:
            if isinstance(error, api.WorkerError) and error.code == 78:
                guard_failure('runtime', 78, uncertain=_uncertain_readiness(error))
            raise
        if coord_task and getattr(args, 'resume_after_failure', False):
            recorded = coordination._assignment(
                coordination.load_task(source, coord_task), coord_assignment)
            previous_id = recorded.get('workspace_id')
            if not previous_id:
                raise api.WorkerError(78, 'This failed job has no existing execution copy; '
                                      'inspect preparation evidence and plan a new job.')
            if copy is None:
                copy = workspace.load(args.state_dir, previous_id)
            elif copy.id != previous_id:
                raise api.WorkerError(64, 'Explicit continuation must reuse the job\'s original workspace.')
        if copy is None:
            def associate(allocated):
                if coord_task and coord_assignment:
                    # Even an admission failure during association must retain
                    # corroborating allocation metadata for provider-free cancel.
                    allocated.metadata.update(coord_task=coord_task,
                        coord_assignment=coord_assignment, standalone_job=False)
                    allocated.save()
                    try:
                        coordination.assignment_preparing(
                            allocated.source, coord_task, coord_assignment, allocated)
                    except coordination.CoordinationError as cause:
                        raise workspace.WorkspaceError(str(cause), cause.code) from None
            expected_head = (coordination.load_task(source, coord_task)['base_head']
                             if coord_task else None)
            copy = workspace.create(Path.cwd(), args.state_dir, on_created=associate,
                                    expected_head=expected_head)
        coord_before = None
        coord_item = None
        baseline_checks = []
        probe_checks = []
        input_before = {}
        print(f'Workspace: {copy.id}\nWorking copy: {copy.path}\n'
              'Source: committed HEAD only; source uncommitted changes were not copied or modified.', file=api.sys.stderr)
        recover = bool(getattr(args, 'resume_after_failure', False) or linked_correction)
        with (nullcontext(copy) if copy._locked else copy.lock(recover=recover)), _preparation_status(copy):
            if coord_task and coord_assignment:
                # The owner lock excludes finalization and another start. Recheck
                # task-local continuity before touching preparation metadata.
                try:
                    linked = coordination.validate_assignment_start(
                        copy.source, coord_task, coord_assignment,
                        explicit_workspace=copy if linked_correction else None)
                except coordination.CoordinationError as error:
                    raise api.WorkerError(error.code, str(error)) from None
                if linked_correction and (linked is None or linked.id != copy.id):
                    raise api.WorkerError(
                        64, 'Correction predecessor changed before execution; no copy was prepared.')
                coordination.assignment_preparing(
                    copy.source, coord_task, coord_assignment, copy)
            # Retained preflight failures must remain linked to their assignment,
            # even when runtime selection never reaches the readiness probes.
            copy.metadata.update(coord_task=coord_task, coord_assignment=coord_assignment,
                                 standalone_job=not bool(coord_task))
            copy.save()
            try:
                toolchains.apply_saved_recipe(copy)
            except toolchains.MissingSourceError as error:
                guard_failure('materialization', error.code)
                raise
            with _runtime_environment(copy) as (home, control, socket_path):
                def context_factory(binary, runtime):
                    return development.prepared_context(copy, backend, home, control,
                        binary, runtime, writable=writable, effort=effort, api=api,
                        deadline=args.job_deadline)

                try:
                    selection = runtime_preflight.select_runtime(args.runtime, args.codex,
                        args.claude, context_factory,
                        explicit_codex=getattr(args, 'codex_explicit', args.codex != 'codex'),
                        explicit_claude=getattr(args, 'claude_explicit', args.claude != 'claude'),
                        deadline=args.job_deadline)
                except runtime_preflight.RuntimePreflightError as error:
                    if error.code == 78:
                        guard_failure('runtime', error.code,
                                      uncertain=_uncertain_readiness(error))
                    raise
                runtime, binary = selection.runtime, selection.binary
                layout, env = selection.layout, selection.environment
                preparation_cause = 'admission'
                args.validate_admission()
                preparation_cause = 'environment'
                diagnostics = copy.directory / 'diagnostics'
                probe_checks = development.run_checks(layout, env, toolchains.probes_for(copy),
                    deadline=args.job_deadline, directory=diagnostics, phase='probe')
                copy.metadata.update(runtime=runtime, runtime_binary=binary,
                    runtime_explicit=(getattr(args, runtime + '_explicit',
                        getattr(args, runtime) != runtime)), readiness_checks=probe_checks,
                    baseline_checks=[], verification_cause=None,
                    coord_task=coord_task, coord_assignment=coord_assignment,
                    delegation_level=policy.delegation_level, requested_access=policy.access,
                    effective_access=policy.effective_access)
                copy.save()
                failed_probe = next(
                    (row for row in probe_checks if row['exit_code'] != 0), None)
                if failed_probe is not None:
                    copy.metadata['verification_cause'] = 'probe_failure'
                    copy.save()
                    if not any(row.get('timed_out') for row in probe_checks):
                        guard_failure('probe', failed_probe['exit_code'])
                    raise development.DevelopmentError(
                        'Preparation: a toolchain smoke probe failed; inspect retained diagnostics.',
                        124 if any(row.get('timed_out') for row in probe_checks) else 78)
                # Coordination readiness is checked before provider credentials are read.
                if coord_task:
                    try:
                        coord_item = coordination.ensure_assignment_ready(
                            copy.source, coord_task, coord_assignment, copy, environment=env)
                    except coordination.CoordinationError as error:
                        if error.constraint_code == 'dependency_unavailable':
                            guard_failure('dependency', error.code)
                        raise
                    sandbox_missing = development.missing_requirements(layout, env, coord_item)
                    if sandbox_missing:
                        guard_failure('dependency', 78)
                        coordination.record_constraint(
                            copy.source, coord_task, coord_item['id'],
                            'dependency_unavailable',
                            'sandbox missing: ' + ', '.join(sandbox_missing))
                        raise coordination.CoordinationError(
                            'Preparation sandbox is missing declared dependencies/check runtime: ' +
                            ', '.join(sandbox_missing) +
                            '. Prepare dependencies inside the owned workspace; host-only tools are not exposed.',
                            78, constraint_code='dependency_unavailable')
                commands = coord_item.get('checks', []) if coord_item is not None else []
                checks_reusable = bool(coord_item and coord_item.get('checks_reusable') is True)
                force_checks = bool(getattr(args, 'force_checks', False))
                copy.metadata['verification_commands'] = list(commands)
                baseline_checks = development.run_checks(layout, env, commands,
                    deadline=args.job_deadline, directory=diagnostics, phase='baseline',
                    copy=copy, reusable=checks_reusable, force=force_checks)
                copy.metadata['baseline_checks'] = baseline_checks
                baseline_classification = check_evidence.classify_checks(baseline_checks, [])
                copy.metadata['verification_cause'] = baseline_classification['cause']
                copy.save()
                if baseline_classification['kind'] == 'environment':
                    raise development.DevelopmentError(
                        'Preparation: baseline check could not complete; inspect retained diagnostics.',
                        124 if baseline_classification['cause'] == 'timeout' else 78)
                if coord_task:
                    prepared_changes, _ignored = copy.changes()
                    coord_before = workspace.content_snapshot(copy)
                input_before = prepared_inputs.input_snapshot(copy)
                # Only after the actual namespace/readiness probes have succeeded.
                preparation_cause = 'admission'
                args.validate_admission()
                preparation_cause = 'environment'
                if guard_conditions is not None and guard_conditions.fingerprint is not None:
                    try:
                        preparation_guard.clear(args.state_dir, guard_conditions)
                    except (preparation_guard.PreparationGuardError, OSError):
                        pass  # Successful preparation is not failed by optional cleanup.
                key = api.load_api_key()
                if not key.strip():
                    raise api.WorkerError(78, 'Provider credential is absent; configure it locally. Workspace retained.')
                development.write_launch(control, binary, runtime, env, writable=writable,
                                         effort=effort)
                if coord_task:
                    # Bounded pre-execution evidence for the lessons journal: the
                    # prompt digest plus a redacted brief, the workspace baseline and
                    # the coordinator-prepared content fingerprints. No credential or
                    # raw model output is captured here.
                    fingerprints = {}
                    if coord_before is not None:
                        fingerprints = {name: coord_before[name] for name in prepared_changes
                                        if name in coord_before}
                    execution = {
                        'prompt_sha256': hashlib.sha256(
                            task.encode('utf-8', errors='replace')).hexdigest(),
                        'prompt_brief': api.redact(task, key).strip()[:600],
                        'model': api.MODEL,
                        'base_head': copy.metadata.get('base_head'),
                        'git_digest': copy.metadata.get('git_digest'),
                        'prepared_fingerprints': fingerprints,
                    }
                    coordination.assignment_started(
                        copy.source, coord_task, coord_assignment, copy.id, runtime,
                        prepared_changes, effort=effort, execution=execution,
                        resume_running=getattr(args, 'resume_after_failure', False))
                    coord_started = True
                provider = None
                execution_started = False
                try:
                    copy.begin('execution')
                    copy.metadata.update(delegation_level=policy.delegation_level,
                                         requested_access=policy.access, effective_access=policy.effective_access,
                                         configuration_sources=dict(policy.sources), runtime=runtime,
                                         model=api.MODEL, effort=effort)
                    copy.save()
                    args.validate_admission(started=coord_started)
                    with relay.ProviderRelay(socket_path, key) as provider:
                        timeout = args.job_deadline - time.monotonic() if args.job_deadline else None
                        if timeout is not None and timeout <= 0:
                            raise api.WorkerError(124, 'DeepSeek worker exceeded its total timeout before execution.')
                        execution_started = True
                        code, out, err = api.execute(development.bridge_command(layout), env, task, timeout)
                        message, errors, completed = api.runtime_result(runtime, out)
                        if code == 0 and (not completed or not message.strip()):
                            code = 70
                        check_results = []
                        if code == 0 and coord_item is not None:
                            check_results = development.run_checks(
                                layout, env, commands, deadline=args.job_deadline,
                                directory=diagnostics, phase='post', redact_values=(key,),
                                copy=copy, reusable=checks_reusable, force=force_checks)
                            if any(row['exit_code'] != 0 for row in check_results):
                                code = (124 if any(row.get('timed_out') for row in check_results) else
                                        78 if any(row.get('launch_failed') for row in check_results) else 65)
                                errors = (errors + '\nDeclared workspace verification failed.').strip()
                        classification = check_evidence.classify_checks(
                            baseline_checks, check_results, probes=probe_checks)
                        kind = ('provider' if provider.failures else
                                classification['kind'] if any(row['exit_code'] != 0
                                    for row in check_results) else 'execution')
                        worker_changes = (workspace.changed_since(copy, coord_before)
                                          if coord_before is not None else [])
                        input_changes = prepared_inputs.input_changes(copy, input_before)
                        copy.metadata.update(checks=check_results,
                            verification_cause=classification['cause'] if check_results else None,
                            prepared_input_changes=input_changes)
                        result = copy.finish('succeeded' if code == 0 else 'failed',
                                             error_kind=kind if code else None, exit_code=code)
                        if coord_task:
                            coordination.assignment_finished(
                                copy.source, coord_task, coord_assignment,
                                'succeeded' if code == 0 else 'failed',
                                message if message else errors,
                                worker_changes, check_results,
                                error_kind=kind if code else None,
                                exit_code=code,
                                baseline_checks=baseline_checks,
                                verification_cause=classification['cause'] if check_results else None,
                                prepared_input_changes=input_changes)
                    if code:
                        print(f'{kind.title()} failure (exit {code}); partial files and diff retained. '
                              f'Inspect workspace {copy.id} before --resume-after-failure.', file=api.sys.stderr)
                        # Diagnostics may contain project content; provider secrets
                        # are redacted and raw malformed protocol is never printed.
                        diagnostics = api.redact(err + '\n' + errors, key).strip()
                        if diagnostics:
                            print(diagnostics, file=api.sys.stderr)
                    else:
                        print(api.redact(message, key))
                    print('Workspace result: ' + json.dumps({
                        'id': copy.id, 'status': result['status'],
                        'changed_files': result['changed_files'],
                        'ignored_artifacts': result['ignored_artifacts'],
                        'elapsed_seconds': round(time.monotonic() - started, 3),
                        'queue_seconds': round(args.queue_seconds, 3),
                        'checks_passed': sum(row['exit_code'] == 0 for row in check_results),
                        'checks_failed': sum(row['exit_code'] != 0 for row in check_results),
                    }), file=api.sys.stderr)
                    return code
                except BaseException as error:
                    code = 130 if isinstance(error, KeyboardInterrupt) else getattr(error, 'code', 71)
                    if isinstance(error, KeyboardInterrupt):
                        kind = 'cancelled'
                    elif isinstance(error, workspace.WorkspaceError) and code == 73:
                        kind = 'verification'
                    elif provider is not None and getattr(provider, 'failures', ()):
                        kind = 'provider'
                    elif not execution_started or isinstance(error, check_evidence.CheckEvidenceError):
                        kind = 'environment'
                    else:
                        kind = 'execution'
                    try:
                        copy.finish('failed', error_kind=kind, exit_code=code)
                    except (workspace.WorkspaceError, OSError):
                        try:
                            copy.failed(kind, code)
                        except (workspace.WorkspaceError, OSError):
                            pass  # Still reconcile the independent coordination ledger.
                    if coord_task and coord_started:
                        try:
                            changes = (workspace.changed_since(copy, coord_before)
                                       if coord_before is not None else [])
                        except (workspace.WorkspaceError, OSError):
                            changes = []
                        try:
                            coordination.assignment_finished(
                                copy.source, coord_task, coord_assignment, 'failed',
                                str(getattr(error, 'message', type(error).__name__)),
                                changes, [], error_kind=kind, exit_code=code,
                                baseline_checks=baseline_checks,
                                prepared_input_changes=prepared_inputs.input_changes(copy, input_before))
                        except Exception:
                            pass
                    print(f'{kind.title()} failure; retained workspace {copy.id}. '
                          'Explicit recovery is required; no automatic implementation retry.', file=api.sys.stderr)
                    raise
    except BaseException as error:
        if copy is not None and copy._locked and not coord_started:
            _mark_preparation_failed(copy, error)
        if coord_task and not coord_started:
            code = 130 if isinstance(error, KeyboardInterrupt) else getattr(error, 'code', 71)
            known = isinstance(error, (workspace.WorkspaceError, development.DevelopmentError,
                                       coordination.CoordinationError, api.WorkerError,
                                       runtime_preflight.RuntimePreflightError,
                                       toolchains.ToolchainError, check_evidence.CheckEvidenceError,
                                       check_reuse.CheckReuseError,
                                       storage_budget.BudgetError))
            message = (error.message if known else
                       'Preparation cancelled before model execution.' if code == 130 else
                       'Preparation failed before model execution; inspect the local runtime and filesystem.')
            cause = preparation_cause
            if code == 124 and args.job_deadline and time.monotonic() >= args.job_deadline:
                cause = 'admission'
            if isinstance(error, coordination.CoordinationError):
                cause = 'environment' if error.constraint_code else 'admission'
            try:
                coordination.assignment_preparation_failed(
                    source, coord_task, coord_assignment, message, exit_code=code,
                    workspace_id=copy.id if copy is not None else getattr(error, 'workspace_id', None),
                    runtime=runtime, effort=effort, cause=cause,
                    baseline_checks=copy.metadata.get('baseline_checks', []) if copy else [],
                    verification_cause=copy.metadata.get('verification_cause') if copy else None,
                    readiness_checks=copy.metadata.get('readiness_checks', []) if copy else [])
            except (coordination.CoordinationError, OSError):
                print('Could not record the preparation failure in the coordination ledger; '
                      'inspect the task before another assignment.', file=api.sys.stderr)
        if isinstance(error, (workspace.WorkspaceError, development.DevelopmentError,
                              coordination.CoordinationError, runtime_preflight.RuntimePreflightError,
                              toolchains.ToolchainError, check_evidence.CheckEvidenceError,
                              check_reuse.CheckReuseError,
                              storage_budget.BudgetError)):
            raise api.WorkerError(error.code, str(error)) from None
        raise
    finally:
        try:
            early_owner.close()
        finally:
            os.close(slot)
