"""CLI for persistent coordination task state."""
import argparse
import json
from pathlib import Path
import sys

from . import coordination


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
            }, indent=2))
            return 78 if issues else 0
        if args.command == 'use':
            task = coordination.use_result(args.path, args.task, args.assignment,
                                           args.disposition, args.evidence)
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
        if args.json:
            print(json.dumps(task, indent=2))
        else:
            print(coordination.summary(task))
            issues = coordination.validate_task(args.path, task['id'])
            if issues:
                print('Distribution issues:')
                for issue in issues:
                    print('- ' + issue)
        return 0
    except coordination.CoordinationError as error:
        print(error.message, file=sys.stderr)
        return error.code
