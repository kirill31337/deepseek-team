"""Revalidate a queued job before acquiring runtime capacity or credentials."""
import os
from pathlib import Path
import subprocess
import time

from . import coordination, settings, state_storage
from .routing import RoutingService
from .routing_models import RoutingError


def _head(root):
    if root is None:
        return None
    result = subprocess.run(['git', '-C', str(root), 'rev-parse', 'HEAD'],
                            env=settings.git_environment(), capture_output=True, check=False)
    return result.stdout.strip() if result.returncode == 0 else None


def _unavailable_message(assignment_id, row):
    """Actionable rejection that never promises an automatic retry."""
    status = row['status']
    if (row.get('error_kind') == 'cancelled'
            and row.get('failure_stage') == 'before_start'):
        return (f'Assignment {assignment_id} was cancelled before start; no queue ticket, copy '
                'or provider launch is permitted. Register new scope in a new coordination task.')
    if status == 'failed':
        if row.get('failure_stage') == 'preparation':
            recovery = (
                'No workspace was created. Correct the source or environment and register a new coordination task.'
                if not row.get('workspace_id') else
                'Inspect any retained copy with workspace show. An incomplete baseline needs a fresh copy; '
                'a verified baseline may be prepared explicitly. If HEAD changed, start a new coordination task.')
            return (f'Assignment {assignment_id} already has a recorded failure during preparation. '
                    'Review the ledger result and record a disposition with coordination use. '
                    + recovery + ' Nothing was retried automatically.')
        return (f'Assignment {assignment_id} already has a recorded failure; a plain relaunch is '
                'rejected. Inspect the retained workspace and ledger result, record a disposition '
                'with the coordination use command, then continue explicitly with '
                '--resume-after-failure. Nothing was retried automatically.')
    if status == 'succeeded':
        return (f'Assignment {assignment_id} already has a recorded result; it cannot launch again. '
                'Review that result and register new scope instead of relaunching it.')
    if status == 'running':
        return (f'Assignment {assignment_id} is already running; a duplicate launch is rejected. '
                'Inspect the live workspace instead of starting a second worker.')
    return f'Assignment {assignment_id} is already active or completed; it cannot launch again.'


def acquire(args, api, *, policy=None, root=None):
    root = settings.project_root(root or Path.cwd())
    try:
        initial = settings.resolve(root)
    except settings.SettingsError as error:
        raise api.WorkerError(
            78, settings.access_read_failure_message(error)) from None
    policy = policy or initial
    head = _head(root)
    started = time.monotonic()
    args.job_deadline = started + args.timeout if args.timeout else None
    explicit_access = (policy.sources.get('access') == 'cli' or
                       (policy.access == 'auto' and policy.sources.get('delegation_level') == 'cli'))

    def validate(*, started=False):
        try:
            requested = getattr(args, 'state_dir_requested', args.state_dir)
            if state_storage.state_root(requested) != args.state_dir:
                raise api.WorkerError(78, 'Worker storage selection changed before admission; '
                                     'start a new job using the saved storage root.')
            if args.job_deadline is not None and time.monotonic() >= args.job_deadline:
                raise api.WorkerError(124, 'DeepSeek worker exceeded its total timeout before launch.')
            try:
                current = settings.resolve(root)
            except settings.SettingsError as error:
                raise api.WorkerError(
                    78, settings.access_read_failure_message(error)) from None
            if not current.enabled:
                raise api.WorkerError(69, 'Delegation was disabled while this job waited.')
            if (policy.effective_access == 'full-access' and current.effective_access != 'full-access'
                    and not (explicit_access and current.as_dict() == initial.as_dict())):
                raise api.WorkerError(
                    78, settings.write_access_revoked_message(current))
            if _head(root) != head:
                raise api.WorkerError(78, 'Project HEAD changed while this job waited; review and replan it.')
            task_id = getattr(args, 'coord_task', None)
            if task_id:
                reservation = getattr(args, 'launch_reservation', None)
                if reservation is not None:
                    coordination.validate_launch_reservation(
                        root, task_id, args.coord_assignment, reservation)
                task = coordination.load_task(root, task_id)
                row = coordination._assignment(task, args.coord_assignment)
                item = coordination.assignment_deliverable(task, args.coord_assignment)
                coordination._check_assignment_access(root, task, item)
                resuming = getattr(args, 'resume_after_failure', False)
                if (row['status'] != 'planned'
                        and not (resuming and row['status'] in ('failed', 'running'))
                        and not (started and row['status'] == 'running')):
                    raise api.WorkerError(78, _unavailable_message(args.coord_assignment, row))
                if (item.get('routing') or {}).get('requested_executor') == 'auto':
                    RoutingService(root).validate_start(item['routing']['decision_id'],
                        already_running=(resuming or started) and row['status'] == 'running',
                        access=coordination._effective_task_access(task, current))
        except (settings.SettingsError, coordination.CoordinationError, RoutingError,
                state_storage.StorageError) as error:
            raise api.WorkerError(getattr(error, 'code', 78), str(error)) from None

    def waiting():
        queue_state('waiting')
        print('Queued: waiting for a DeepSeek worker slot; assignment remains delegated.',
              file=api.sys.stderr, flush=True)

    def queue_state(state):
        if getattr(args, 'coord_task', None):
            coordination.assignment_queue_state(root, args.coord_task, args.coord_assignment, state)

    args.validate_admission = validate
    slot = None
    task_id = getattr(args, 'coord_task', None)
    reservation = None
    try:
        # Preserve the existing actionable admission errors before publishing
        # any reservation state. Registration is a mutation; a duplicate or
        # otherwise ineligible launch must remain side-effect free.
        validate()
        if task_id:
            try:
                reservation = coordination.register_launch_reservation(
                    root, task_id, args.coord_assignment,
                    resume=bool(getattr(args, 'resume_after_failure', False)))
            except coordination.CoordinationError as error:
                raise api.WorkerError(error.code, str(error)) from None
            args.launch_reservation = reservation
        slot = api.acquire_slot(args.state_dir,
            limit=getattr(args, 'max_workers', None) or policy.max_workers,
            timeout=args.timeout, wait=not getattr(args, 'no_wait', False),
            on_wait=waiting, validate=validate, binding=reservation)
        if reservation is not None:
            # The queue allocator lock has been released. This task-lock pass is
            # the serialization point with C5 cancellation before allocation.
            coordination.revalidate_launch_reservation(
                root, task_id, args.coord_assignment, reservation)
        validate()
        if args.job_deadline is not None and time.monotonic() >= args.job_deadline:
            raise api.WorkerError(124, 'DeepSeek worker exceeded its total timeout while queued.')
        args.queue_seconds = time.monotonic() - started
        queue_state('ready')
        return slot
    except BaseException as error:
        if slot is not None:
            os.close(slot)
        if reservation is not None:
            try:
                coordination.release_launch_reservation(
                    root, task_id, args.coord_assignment, reservation)
            except (coordination.CoordinationError, OSError):
                pass
        try:
            queue_state('cancelled' if isinstance(error, KeyboardInterrupt) else 'blocked')
        except (coordination.CoordinationError, OSError):
            pass
        raise


def release(args, *, root=None) -> None:
    """Release this process's unused admission reservation during launch cleanup."""
    reservation = getattr(args, 'launch_reservation', None)
    task_id = getattr(args, 'coord_task', None)
    assignment_id = getattr(args, 'coord_assignment', None)
    if not (reservation and task_id and assignment_id):
        return
    try:
        coordination.release_launch_reservation(
            settings.project_root(root or Path.cwd()), task_id, assignment_id, reservation)
    except (coordination.CoordinationError, OSError, settings.SettingsError):
        pass
    finally:
        args.launch_reservation = None
