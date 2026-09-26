"""CLI for persistent coordination task state."""
import argparse
import json
from pathlib import Path
import sys

from . import coordination


MAX_FEEDBACK_BYTES = 64 * 1024


def _read_feedback(source, label):
    """Read one structured feedback object from FILE or stdin before any mutation.

    The read is bounded, duplicate object keys are rejected for both feedback
    inputs and ``null`` keeps the same explicit-omission meaning as an absent
    flag. Nothing is read for the second stdin source: the caller rejects two
    ``-`` sources before either one is opened.
    """
    try:
        if source == '-':
            stream = getattr(sys.stdin, 'buffer', sys.stdin)
            raw = stream.read(MAX_FEEDBACK_BYTES + 1)
            if isinstance(raw, str):
                raw = raw.encode('utf-8')
        else:
            with Path(source).open('rb') as handle:
                raw = handle.read(MAX_FEEDBACK_BYTES + 1)
    except (OSError, UnicodeError, ValueError):
        raise coordination.CoordinationError(
            f'Cannot read the {label} JSON input; nothing was changed.', 64) from None
    if not isinstance(raw, (bytes, bytearray)):
        raise coordination.CoordinationError(
            f'Cannot read the {label} JSON input; nothing was changed.', 64)
    if len(raw) > MAX_FEEDBACK_BYTES:
        raise coordination.CoordinationError(
            f'{label.capitalize()} JSON exceeds the 64 KiB limit; nothing was changed.', 64)
    def reject_duplicates(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('duplicate JSON field')
            result[key] = value
        return result
    try:
        payload = json.loads(raw.decode('utf-8'), object_pairs_hook=reject_duplicates)
    except (ValueError, UnicodeError, RecursionError):
        raise coordination.CoordinationError(
            f'{label.capitalize()} JSON must be one JSON object; nothing was changed.', 64) from None
    if payload is not None and not isinstance(payload, dict):
        raise coordination.CoordinationError(
            f'{label.capitalize()} JSON must be one object or null; nothing was changed.', 64)
    return payload


def main(argv):
    parser = argparse.ArgumentParser(prog='deepseek-team coordination')
    subs = parser.add_subparsers(dest='command', required=True)

    plan = subs.add_parser('plan')
    plan.add_argument('--path', type=Path, default=Path.cwd())
    plan.add_argument('--task', required=True)

    status = subs.add_parser('status')
    status.add_argument('--path', type=Path, default=Path.cwd())
    status.add_argument('--task')
    status.add_argument('--session')
    status.add_argument('--json', action='store_true')

    use = subs.add_parser('use')
    use.add_argument('--path', type=Path, default=Path.cwd())
    use.add_argument('--task', required=True)
    use.add_argument('--assignment', required=True)
    use.add_argument('--disposition', required=True,
                     choices=['incorporated', 'reproduced', 'rejected', 'needs-rework'])
    use.add_argument('--evidence', required=True)
    use.add_argument('--cost-usd', type=float,
                     help='Measured total cost including worker, review and rework; omit if unknown.')
    use.add_argument('--rework-json', metavar='FILE',
                     help="Structured cause/severity/summary/prevention correction as JSON "
                          "from FILE, or - for stdin. Older omissions stay unknown.")
    use.add_argument('--quality-json', metavar='FILE',
                     help="Explicit graded assessment {grade, attribution, evidence} as JSON "
                          "from FILE, or - for stdin. Grades: met (1.0), minor_gaps (0.8), "
                          "major_gaps (0.3), unusable (0.0), unassessable (neutral). Null keeps "
                          "the legacy binary reading. At most one feedback flag may read stdin.")

    result = subs.add_parser('result', help='Record a verified coordinator or native-agent outcome and optional total cost.')
    result.add_argument('--path', type=Path, default=Path.cwd())
    result.add_argument('--task', required=True)
    result.add_argument('--deliverable', required=True)
    result.add_argument('--outcome', required=True,
                        choices=['accepted', 'rework', 'rejected', 'infrastructure', 'cancelled', 'unknown'])
    result.add_argument('--evidence', required=True)
    result.add_argument('--cost-usd', type=float,
                        help='Measured total cost; omit when there is no defensible dollar cost.')

    abandon = subs.add_parser(
        'abandon', help='Cancel an orphaned running assignment after inspecting it and stopping its processes.')
    abandon.add_argument('--path', type=Path, default=Path.cwd())
    abandon.add_argument('--task', required=True)
    abandon.add_argument('--assignment', required=True)
    abandon.add_argument('--evidence', required=True)
    abandon.add_argument(
        '--confirmed-stopped', action='store_true', required=True,
        help='Attest that remaining worker processes were stopped and verified stopped.')

    args = parser.parse_args(argv[1:])
    try:
        if args.command == 'plan':
            try:
                payload = json.loads(sys.stdin.read())
            except (ValueError, RecursionError):
                raise coordination.CoordinationError('coordination plan reads one JSON object from stdin.', 64) from None
            task = coordination.plan_task(args.path, args.task, payload)
            issues = coordination.validate_task(args.path, args.task)
            print(json.dumps({
                'task': task['id'],
                'assignments': task['assignments'],
                'issues': issues,
                'diagnostics': coordination.routing_diagnostics(task),
            }, indent=2))
            return 78 if issues else 0
        if args.command == 'use':
            if args.rework_json == '-' and args.quality_json == '-':
                raise coordination.CoordinationError(
                    'Only one feedback JSON source may read stdin (-); pass a file for the '
                    'other. Nothing was changed.', 64)
            rework = (_read_feedback(args.rework_json, 'rework')
                      if args.rework_json else None)
            quality = (_read_feedback(args.quality_json, 'quality')
                       if args.quality_json else None)
            task = coordination.use_result(args.path, args.task, args.assignment,
                                           args.disposition, args.evidence,
                                           cost_usd=args.cost_usd, rework=rework, quality=quality)
            print(coordination.summary(task))
            return 0
        if args.command == 'result':
            task = coordination.observe_coordinator_result(args.path, args.task, args.deliverable,
                        args.outcome, args.evidence, cost_usd=args.cost_usd)
            print(coordination.summary(task))
            return 0
        if args.command == 'abandon':
            task = coordination.abandon_assignment(
                args.path, args.task, args.assignment, args.evidence,
                confirmed_stopped=args.confirmed_stopped)
            print(coordination.summary(task))
            return 0
        if args.task:
            task = coordination.load_task(args.path, args.task)
        elif args.session:
            task = coordination.latest_task(args.path, args.session)
            if task is None:
                raise coordination.CoordinationError('No coordination task for that session.', 66)
        else:
            raise coordination.CoordinationError('status requires --task or --session.', 64)
        coordination.sync_routing_feedback(args.path, task['id'])
        coordination.sync_lessons_feedback(args.path, task['id'])
        if args.json:
            print(json.dumps(task, indent=2))
        else:
            print(coordination.summary(task))
            closed_draft = task.get('status') == 'closed' and coordination._unstarted_task(task)
            issues = [] if closed_draft else coordination.validate_task(args.path, task['id'])
            if issues:
                print('Distribution issues:')
                for issue in issues:
                    print('- ' + issue)
        return 0
    except coordination.CoordinationError as error:
        print(error.message, file=sys.stderr)
        return error.code
