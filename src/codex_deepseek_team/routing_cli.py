"""Command-line interface for project-scoped hybrid delegation routing."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile

from .routing import RoutingService
from .routing_models import (DEFAULT_CONFIG, MAX_JSON_BYTES, RoutingError, identifier, number,
                             read_json)
from .routing_sources import fetch_snapshot


class _RoutingArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        raise RoutingError('Invalid routing arguments; use --help.')


def _add_path(parser: argparse.ArgumentParser) -> None:
    parser.add_argument('--path', type=Path, default=Path.cwd(), metavar='PROJECT',
                        help='Project whose private routing state is used.')


def _add_source_options(parser: argparse.ArgumentParser, *, include_url: bool) -> None:
    parser.add_argument('--source-id', required=True)
    if include_url:
        parser.add_argument('--source-url', required=True)
    parser.add_argument('--sha256', required=True,
                        help='Expected SHA-256 digest of the exact snapshot bytes.')
    parser.add_argument('--reliability', type=float, default=0.5)


def _parser() -> argparse.ArgumentParser:
    parser = _RoutingArgumentParser(
        prog='deepseek-team routing',
        description='Use bounded public evidence and local outcomes to advise delegation.',
    )
    commands = parser.add_subparsers(dest='command', required=True)

    for name in ('status', 'config'):
        command = commands.add_parser(name)
        _add_path(command)
        command.add_argument('--json', action='store_true',
                             help='Emit machine-readable JSON (the default output format).')

    configure = commands.add_parser('configure')
    _add_path(configure)
    configure.add_argument('--mode', choices=('off', 'shadow', 'advisory', 'auto'))
    for key in DEFAULT_CONFIG:
        if key in ('mode', 'require_cost_evidence'):
            continue
        configure.add_argument('--' + key.replace('_', '-'), dest=key, type=float)
    configure.add_argument('--require-cost-evidence', action=argparse.BooleanOptionalAction,
                           default=None)

    imported = commands.add_parser('import', help='Import a local, integrity-pinned evidence snapshot.')
    _add_path(imported)
    imported.add_argument('file', type=Path, metavar='FILE')
    _add_source_options(imported, include_url=True)

    fetched = commands.add_parser('fetch', help='Explicitly fetch and import an HTTPS evidence snapshot.')
    _add_path(fetched)
    fetched.add_argument('url', metavar='URL')
    _add_source_options(fetched, include_url=False)

    recommend = commands.add_parser('recommend')
    _add_path(recommend)
    recommend.add_argument('--file', type=Path,
                           help='Read the feature-card JSON from FILE instead of stdin.')
    recommend.add_argument('--access', choices=('read-only', 'full-access'))

    observe = commands.add_parser('observe')
    _add_path(observe)
    observe.add_argument('--file', type=Path,
                         help='Read the observation JSON from FILE instead of stdin.')

    evaluate = commands.add_parser('evaluate')
    _add_path(evaluate)

    export = commands.add_parser('export')
    _add_path(export)
    export.add_argument('--output', type=Path,
                        help='Write the export to FILE instead of stdout.')

    budget = commands.add_parser('budget')
    budget_commands = budget.add_subparsers(dest='budget_command', required=True)
    reserve = budget_commands.add_parser('reserve')
    _add_path(reserve)
    reserve.add_argument('id')
    reserve.add_argument('--amount-usd', required=True, type=float)
    settle = budget_commands.add_parser('settle')
    _add_path(settle)
    settle.add_argument('id')
    settle.add_argument('--actual-usd', required=True, type=float)
    release = budget_commands.add_parser('release')
    _add_path(release)
    release.add_argument('id')

    recovery = commands.add_parser('recovery')
    recovery_commands = recovery.add_subparsers(dest='recovery_command', required=True)
    recovery_status = recovery_commands.add_parser('status')
    _add_path(recovery_status)
    recovery_release = recovery_commands.add_parser('release')
    _add_path(recovery_release)
    recovery_release.add_argument('id')
    return parser


def _read_limited(path: Path | None, *, label: str) -> bytes:
    if path is None:
        stream = getattr(sys.stdin, 'buffer', sys.stdin)
        data = stream.read(MAX_JSON_BYTES + 1)
        if isinstance(data, str):
            data = data.encode('utf-8')
    else:
        with path.open('rb') as stream:
            data = stream.read(MAX_JSON_BYTES + 1)
    if len(data) > MAX_JSON_BYTES:
        raise RoutingError(f'{label} is too large; the limit is 8 MiB.')
    return data


def _read_object(path: Path | None, *, label: str) -> dict:
    value = read_json(_read_limited(path, label=label))
    if not isinstance(value, dict):
        raise RoutingError(f'{label} must contain one JSON object.')
    return value


def _validate_source_options(args) -> None:
    identifier(args.source_id, 'source_id')
    number(args.reliability, 'reliability', 0, 1)
    if re.fullmatch(r'[0-9a-f]{64}', args.sha256) is None:
        raise RoutingError('Expected hash must be a lowercase SHA-256 digest.')


def _emit(value, stream=None) -> None:
    print(json.dumps(value, indent=2, sort_keys=True, allow_nan=False),
          file=sys.stdout if stream is None else stream)


def _export_target_identity(path: Path):
    try:
        details = os.lstat(path)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(details.st_mode) or details.st_nlink != 1:
        raise RoutingError('Export output must be a regular file with exactly one hard link.')
    return details.st_dev, details.st_ino


def _write_export(path: Path, service: RoutingService) -> None:
    original = _export_target_identity(path)
    descriptor = -1
    temporary = None
    try:
        descriptor, name = tempfile.mkstemp(prefix='.' + path.name + '.', suffix='.tmp',
                                            dir=path.parent)
        temporary = Path(name)
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            descriptor = -1
            service.export_to(stream)
            stream.flush()
            os.fsync(stream.fileno())
        if _export_target_identity(path) != original:
            raise RoutingError('Export output changed while the export was being written.')
        os.replace(temporary, path)
        temporary = None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def main(argv) -> int:
    """Run a routing command; ``argv`` starts with the literal ``routing``."""
    arguments = list(argv)
    if not arguments or arguments[0] != 'routing':
        _emit({'error': 'Routing CLI arguments must start with routing.', 'code': 64}, sys.stderr)
        return 64
    try:
        args = _parser().parse_args(arguments[1:])
        if args.command in ('import', 'fetch'):
            _validate_source_options(args)
        if args.command in ('recommend', 'observe'):
            payload = _read_object(args.file, label=args.command + ' input')
        elif args.command == 'import':
            payload = _read_limited(args.file, label='Evidence snapshot')
        elif args.command == 'fetch':
            payload = fetch_snapshot(args.url)
        else:
            payload = None

        service = RoutingService(args.path)
        if args.command == 'status':
            result = service.status()
        elif args.command == 'config':
            result = service.config()
        elif args.command == 'configure':
            changes = {key: getattr(args, key) for key in DEFAULT_CONFIG
                       if getattr(args, key, None) is not None}
            if not changes:
                raise RoutingError('configure requires at least one setting.')
            result = service.configure(changes)
        elif args.command == 'import':
            result = service.import_snapshot(
                payload, args.source_id, args.source_url, args.sha256, args.reliability)
        elif args.command == 'fetch':
            result = service.import_snapshot(
                payload, args.source_id, args.url, args.sha256, args.reliability)
        elif args.command == 'recommend':
            result = service.predict(payload, access=args.access, record=True)
        elif args.command == 'observe':
            result = service.observe(payload)
        elif args.command == 'evaluate':
            result = service.evaluate()
        elif args.command == 'export':
            if args.output is None:
                service.export_to(sys.stdout)
                return 0
            _write_export(args.output, service)
            result = {'output': str(args.output)}
        elif args.command == 'recovery':
            if args.recovery_command == 'status':
                result = service.recovery_status()
            else:
                result = service.release_recovery(args.id)
        elif args.budget_command == 'reserve':
            result = service.reserve_experiment(args.id, args.amount_usd)
        elif args.budget_command == 'settle':
            result = service.settle_experiment(args.id, args.actual_usd)
        else:
            result = service.release_experiment(args.id)
        _emit(result)
        return 0
    except RoutingError as error:
        _emit({'error': error.message, 'code': error.code}, sys.stderr)
        return error.code
    except (OSError, UnicodeError):
        _emit({'error': 'Routing input/output failed; check paths and permissions.', 'code': 78},
              sys.stderr)
        return 78
    except KeyboardInterrupt:
        _emit({'error': 'Cancelled.', 'code': 130}, sys.stderr)
        return 130
