"""CLI for the private delegation lessons journal and versioned rules.

Read commands create no state. ``review`` prints the review bundle and
``review --apply FILE|-`` applies one exact validated payload. This module never
calls a model, never executes instructions and never writes inside the project.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from . import lessons, settings

MAX_INPUT_BYTES = 8 * 1024 * 1024
_STATE_PREFIXES = (
    'Lessons state', 'Lessons database', 'Lessons lock', 'Lessons markdown',
    'Cannot safely', 'Unsupported lessons state', 'Stored lessons state',
    'Unexpected lessons database schema', 'Lessons state path',
)


def _error_code(error) -> int:
    message = str(error)
    return 78 if message.startswith(_STATE_PREFIXES) else 64


def _root(value) -> Path:
    location = Path(value)
    root = settings.project_root(location)
    if root is not None:
        return root
    resolved = location.resolve()
    if not resolved.is_dir():
        raise lessons.LessonError('--path must name an existing project directory.')
    return resolved


def _emit(value) -> None:
    print(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=True, allow_nan=False))


def _read_payload(source):
    try:
        if source == '-':
            stream = getattr(sys.stdin, 'buffer', sys.stdin)
            raw = stream.read(MAX_INPUT_BYTES + 1)
            if isinstance(raw, str):
                raw = raw.encode('utf-8')
        else:
            with Path(source).open('rb') as stream:
                raw = stream.read(MAX_INPUT_BYTES + 1)
    except OSError:
        raise lessons.LessonError('Cannot read the review payload file; nothing was changed.') from None
    if len(raw) > MAX_INPUT_BYTES:
        raise lessons.LessonError('Review payload exceeds the 8 MiB limit; nothing was changed.')
    try:
        return json.loads(raw.decode('utf-8'))
    except (ValueError, UnicodeError, RecursionError):
        raise lessons.LessonError('Review payload must be one JSON object; nothing was changed.') from None


def _counts_line(counts) -> str:
    return (f"Cases: {counts['cases']} total (clean {counts['clean']}, "
            f"rework {counts['rework']}, rejected {counts['rejected']}, "
            f"unknown attribution {counts['unknown_attribution']})")


def _due_line(state) -> str:
    if state['review_due']:
        return 'Review due: yes - ' + '; '.join(state['due_reasons'])
    return 'Review due: no'


def _render_status(state) -> str:
    return '\n'.join((
        f"Delegation lessons version: {state['version']}",
        _counts_line(state['counts']),
        f"Cases since review: {state['cases_since_review']} "
        f"(journal head {state['through_event']})",
        _due_line(state),
        'Rules file: ' + state['rules_file'],
    ))


def _render_journal(events) -> str:
    if not events:
        return 'No delegation outcome events are recorded.'
    lines = []
    for event in events:
        line = f"{event['seq']:07d} {event['case_id']} {event['disposition']}"
        rework = event.get('rework')
        if isinstance(rework, dict):
            line += f" cause={rework['cause']} severity={rework['severity']}"
        evidence = ' '.join(str(event.get('evidence') or '').split())
        if evidence:
            line += ' evidence=' + evidence[:120]
        lines.append(line)
    return '\n'.join(lines)


def _render_review(bundle) -> str:
    lines = [
        f"Review bundle: expected_version={bundle['expected_version']} "
        f"reviewed_through={bundle['reviewed_through']} journal_head={bundle['through_event']}",
        _counts_line(bundle['counts']),
        _due_line(bundle),
        'Active rules: ' + (', '.join(rule['id'] for rule in bundle['rules']) or 'none'),
        'Apply the reviewed payload with: deepseek-team lessons review --apply FILE',
    ]
    return '\n'.join(lines)


def _render_rules(snapshot, rules_file=None) -> str:
    """Render the committed rules from one canonical snapshot.

    The derived Markdown mirror can lag behind canonical state after a committed
    review whose publish failed, so it is never the source of truth for this
    read command. The full rules (id, match, condition, action and evidence) come
    from the same canonical snapshot as the JSON view, and rendering is pure: it
    never reads or regenerates the mirror and never creates state.
    """
    lines = [f"Delegation lessons version: {snapshot['version']}"]
    rules = snapshot.get('rules') or []
    if not rules:
        lines.append('No active delegation lessons.')
        return '\n'.join(lines)
    lines.append(f"Active rules: {len(rules)}")
    for rule in rules:
        when = ", ".join(f"{key}={rule['when'][key]}" for key in sorted(rule['when'])) or "any task"
        lines.extend([
            '',
            f"[{rule['id']}]",
            f"When: {when}",
            f"Condition: {rule['condition']}",
            f"Action: {rule['action']}",
            f"Evidence: {', '.join(rule['evidence'])}",
        ])
    return '\n'.join(lines)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog='deepseek-team lessons',
        description='Inspect the private rework journal and apply reviewed delegation lessons.')
    subparsers = parser.add_subparsers(dest='command', required=True)
    for name, help_text in (('status', 'Show version, case counts and review due state.'),
                            ('journal', 'Print the append-only outcome journal.'),
                            ('rules', 'Print the current reviewed rules/Markdown.')):
        sub = subparsers.add_parser(name, help=help_text)
        sub.add_argument('--path', type=Path, default=Path.cwd(), metavar='PROJECT',
                         help='Project whose private lessons state is used.')
        sub.add_argument('--json', action='store_true', help='Emit stable machine-readable JSON.')
    review = subparsers.add_parser('review', help='Print the review bundle or apply one payload.')
    review.add_argument('--path', type=Path, default=Path.cwd(), metavar='PROJECT')
    review.add_argument('--json', action='store_true', help='Emit stable machine-readable JSON.')
    review.add_argument('--apply', metavar='FILE',
                        help="Apply one validated review payload from FILE, or - for stdin.")
    return parser


def main(argv) -> int:
    arguments = list(argv)
    if arguments and arguments[0] == 'lessons':
        arguments = arguments[1:]
    json_mode = '--json' in arguments
    try:
        args = _parser().parse_args(arguments)
        root = _root(args.path)
        if args.command == 'status':
            state = _status(root)
            _emit(state) if args.json else print(_render_status(state))
        elif args.command == 'journal':
            events = lessons.journal(root)
            _emit(events) if args.json else print(_render_journal(events))
        elif args.command == 'rules':
            snapshot_block = lessons.snapshot(root)
            state = _status(root)
            if args.json:
                _emit({'version': snapshot_block['version'],
                       'rule_ids': snapshot_block['rule_ids'],
                       'rules': snapshot_block['rules'],
                       'rules_file': state['rules_file']})
            else:
                print(_render_rules(snapshot_block))
        elif args.apply is not None:
            payload = _read_payload(args.apply)
            state = lessons.apply_review(root, payload)
            _emit(state) if args.json else print(_render_status(state))
        else:
            bundle = lessons.review_bundle(root)
            _emit(bundle) if args.json else print(_render_review(bundle))
        return 0
    except lessons.LessonError as error:
        return _report(error, _error_code(error), json_mode)
    except OSError:
        return _report('Lessons input/output failed; check paths and permissions.', 78, json_mode)
    except KeyboardInterrupt:
        return _report('Cancelled.', 130, json_mode)


def _status(root):
    """Read status through the service only; no CLI-side counting."""
    return lessons.status(root)


def _report(message, code, json_mode) -> int:
    if json_mode:
        print(json.dumps({'error': str(message), 'code': code}, sort_keys=True), file=sys.stderr)
    else:
        print(str(message), file=sys.stderr)
    return code
