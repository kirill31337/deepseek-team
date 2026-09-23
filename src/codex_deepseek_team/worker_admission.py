"""Revalidate a queued job before acquiring runtime capacity or credentials."""
import os
from pathlib import Path
import subprocess
import time

from . import coordination, settings
from .routing import RoutingService
from .routing_models import RoutingError


def _head(root):
    if root is None:
        return None
    result = subprocess.run(['git', '-C', str(root), 'rev-parse', 'HEAD'],
                            env=settings.git_environment(), capture_output=True, check=False)
    return result.stdout.strip() if result.returncode == 0 else None


def acquire(args, api, *, policy=None, root=None):
    root = settings.project_root(root or Path.cwd())
    initial = settings.resolve(root)
    policy = policy or initial
    head = _head(root)
    started = time.monotonic()
    args.job_deadline = started + args.timeout if args.timeout else None
    explicit_access = (policy.sources.get('access') == 'cli' or
                       (policy.access == 'auto' and policy.sources.get('delegation_level') == 'cli'))

    def validate(*, started=False):
        try:
            if args.job_deadline is not None and time.monotonic() >= args.job_deadline:
                raise api.WorkerError(124, 'DeepSeek worker exceeded its total timeout before launch.')
            current = settings.resolve(root)
            if not current.enabled:
                raise api.WorkerError(69, 'Delegation was disabled while this job waited.')
            if (policy.effective_access == 'full-access' and current.effective_access != 'full-access'
                    and not (explicit_access and current.as_dict() == initial.as_dict())):
                raise api.WorkerError(78, 'Write access was revoked while this job waited.')
            if _head(root) != head:
                raise api.WorkerError(78, 'Project HEAD changed while this job waited; review and replan it.')
            task_id = getattr(args, 'coord_task', None)
            if task_id:
                task = coordination.load_task(root, task_id)
                row = coordination._assignment(task, args.coord_assignment)
                item = coordination.assignment_deliverable(task, args.coord_assignment)
                coordination._check_assignment_access(root, task, item)
                resuming = getattr(args, 'resume_after_failure', False)
                if (row['status'] != 'planned'
                        and not (resuming and row['status'] in ('failed', 'running'))
                        and not (started and row['status'] == 'running')):
                    raise api.WorkerError(78, 'Assignment is already active or completed; it cannot launch again.')
                if (item.get('routing') or {}).get('requested_executor') == 'auto':
                    RoutingService(root).validate_start(item['routing']['decision_id'],
                        already_running=(resuming or started) and row['status'] == 'running',
                        access=coordination._effective_task_access(task, current))
        except (settings.SettingsError, coordination.CoordinationError, RoutingError) as error:
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
    try:
        slot = api.acquire_slot(args.state_dir,
            limit=getattr(args, 'max_workers', None) or policy.max_workers,
            timeout=args.timeout, wait=not getattr(args, 'no_wait', False),
            on_wait=waiting, validate=validate)
        validate()
        if args.job_deadline is not None and time.monotonic() >= args.job_deadline:
            raise api.WorkerError(124, 'DeepSeek worker exceeded its total timeout while queued.')
        args.queue_seconds = time.monotonic() - started
        queue_state('ready')
        return slot
    except BaseException as error:
        if slot is not None:
            os.close(slot)
        try:
            queue_state('cancelled' if isinstance(error, KeyboardInterrupt) else 'blocked')
        except (coordination.CoordinationError, OSError):
            pass
        raise
