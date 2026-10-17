"""Provider-free workspace collection and update recovery.

This module intentionally does not import credential, worker or model code.
All deletion decisions remain in :mod:`workspace_gc`; the CLI only resolves a
bounded set of private state roots, invokes collection and renders its report.
"""
from __future__ import annotations

import argparse
import errno
import json
import os
from pathlib import Path
import sys

def _saved_selection_roots():
    """Read only the private selection file; return roots and a deferred error."""
    from . import state_storage, storage_selection
    directory = file_fd = None
    try:
        directory_path = storage_selection._directory()
        directory = state_storage.open_private_directory(
            directory_path, label='Storage selection directory')
    except state_storage.StorageError as error:
        if error.errno == errno.ENOENT:
            return [], []
        return [], [str(error)]
    try:
        try:
            file_fd = storage_selection._file(directory, 'selection.json')
        except FileNotFoundError:
            return [], []
        with os.fdopen(file_fd, 'rb') as stream:
            file_fd = None
            raw = stream.read(8193)
        if len(raw) > 8192:
            raise ValueError('oversized selection')
        value = json.loads(raw)
        if (not isinstance(value, dict)
                or set(value) != {'schema', 'root', 'previous'}
                or type(value.get('schema')) is not int or value['schema'] != 1):
            raise ValueError('selection schema')
        roots = []
        for field in ('root', 'previous'):
            text = value[field]
            if not isinstance(text, str) or not text or '\0' in text:
                raise ValueError('selection path')
            path = Path(text)
            if not path.is_absolute() or '..' in path.parts:
                raise ValueError('selection path')
            roots.append(path)
        return roots, []
    except (OSError, TypeError, ValueError, UnicodeError,
            state_storage.StorageError):
        message = (f'Cannot safely read saved storage selection: '
                   f'{storage_selection._directory() / "selection.json"}. '
                   'The file and retained data were preserved.')
        return [], [message]
    finally:
        if file_fd is not None:
            os.close(file_fd)
        if directory is not None:
            os.close(directory)


def _safe_candidate(path):
    """Saved paths are adopted only when absent or already private and owned."""
    from . import state_storage
    try:
        state_storage.check_private_directory(path)
        return True
    except state_storage.StorageError as error:
        return error.errno == errno.ENOENT


def _add_root(roots, seen, path):
    path = Path(path).absolute()
    key = str(path)
    if key not in seen:
        seen.add(key)
        roots.append(path)


def _known_state_roots(explicit=None):
    from . import state_storage
    if explicit is not None:
        return [Path(explicit).absolute()], []
    roots, seen, errors = [], set(), []
    current = state_storage.state_root()
    if os.environ.get('DEEPSEEK_TEAM_STATE_DIR'):
        _add_root(roots, seen, current)
    elif _safe_candidate(current):
        _add_root(roots, seen, current)
    else:
        errors.append(f'Effective state root is unsafe and was not adopted: {current}')
    default = Path.home() / '.local/state/codex-deepseek'
    if _safe_candidate(default):
        _add_root(roots, seen, default)
    elif current != default and (default.exists() or default.is_symlink()):
        errors.append(f'Default state root is unsafe and was not adopted: {default}')
    saved, saved_errors = _saved_selection_roots()
    errors.extend(saved_errors)
    for path in saved:
        if _safe_candidate(path):
            _add_root(roots, seen, path)
        else:
            errors.append(f'Recorded saved state root is unsafe and was not adopted: {path}')
    return roots, errors


def known_state_roots(explicit=None):
    """Return only explicit, effective, default and validated saved roots."""
    roots, _errors = _known_state_roots(explicit)
    return roots


def _collect_roots(roots, *, apply):
    from . import state_storage, workspace, workspace_gc
    report = {'schema': 1, 'dry_run': not apply,
              'state_roots': [str(root) for root in roots],
              'workspaces': [], 'reclaimed': {'allocated_bytes': 0, 'inodes': 0},
              'errors': []}
    for root in roots:
        try:
            current = workspace_gc.collect(root, apply=apply)
        except (OSError, ValueError, state_storage.StorageError,
                workspace.WorkspaceError) as error:
            report['errors'].append({'state_root': str(root), 'reason': str(error),
                                     'code': getattr(error, 'code', 78)})
            continue
        for item in current['workspaces']:
            row = dict(item)
            row['state_root'] = str(root)
            report['workspaces'].append(row)
        for key in report['reclaimed']:
            report['reclaimed'][key] += current['reclaimed'].get(key, 0)
    return report


def upgrade_cleanup(explicit=None):
    """Apply safe cleanup across every bounded known state root."""
    from . import state_storage
    try:
        roots, errors = _known_state_roots(explicit)
    except state_storage.StorageError as error:
        return {'schema': 1, 'dry_run': False, 'state_roots': [], 'workspaces': [],
                'reclaimed': {'allocated_bytes': 0, 'inodes': 0},
                'errors': [{'state_root': None, 'reason': str(error),
                            'code': error.code}]}
    report = _collect_roots(roots, apply=True)
    report['errors'].extend({'state_root': None, 'reason': error, 'code': 78}
                            for error in errors)
    return report


def _rows_for(report, root):
    return [row for row in report['workspaces']
            if row.get('state_root', root) == root]


def describe(report):
    lines = []
    roots = ([report['state_root']] if 'state_root' in report
             else report.get('state_roots', []))
    for root in roots:
        lines.append('State root: ' + str(root))
        rows = _rows_for(report, root)
        if not rows:
            lines.append('  No retained workspaces.')
        for row in rows:
            reason = row.get('reason', 'no reason recorded')
            lines.append(
                f"  {row.get('id', 'unknown')}: {row.get('action', 'kept')}; "
                f"reason={reason}; allocated_bytes={row.get('allocated_bytes', 0)}; "
                f"inodes={row.get('inodes', 0)}")
    reclaimed = report.get('reclaimed', {})
    lines.append('Reclaimed: allocated_bytes={} inodes={}'.format(
        reclaimed.get('allocated_bytes', 0), reclaimed.get('inodes', 0)))
    for error in report.get('errors', []):
        root = error.get('state_root') or 'saved selection'
        lines.append(f"Deferred {root}: {error.get('reason', 'cleanup not applied')}")
    if report.get('dry_run', True):
        lines.append('Dry run: no execution data was changed.')
    return '\n'.join(lines)


def upgrade_lines(report):
    """Short automatic-upgrade output; live leases stay silent and preserved."""
    cleaned = [row for row in report.get('workspaces', [])
               if row.get('action') == 'cleaned']
    lines = []
    if cleaned:
        reclaimed = report.get('reclaimed', {})
        lines.append('Workspace upgrade cleanup: cleaned {} copy/copies; '
                     'reclaimed allocated_bytes={} inodes={}.'.format(
                         len(cleaned), reclaimed.get('allocated_bytes', 0),
                         reclaimed.get('inodes', 0)))
    for error in report.get('errors', []):
        root = error.get('state_root') or 'saved selection'
        lines.append(f'Workspace upgrade cleanup deferred for {root}: '
                     f"{error.get('reason', 'unknown error')}")
    for row in report.get('workspaces', []):
        reason = str(row.get('reason', ''))
        if row.get('action') != 'kept' or reason.startswith('active logical job lease'):
            continue
        if reason.startswith('legacy worker admission'):
            continue
        lines.append(f"Workspace upgrade cleanup deferred for {row.get('id')}: {reason}")
    return lines


def _parser():
    parser = argparse.ArgumentParser(prog='deepseek-team gc')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--dry-run', dest='apply', action='store_false',
                      help='Report only; this is the default and changes nothing.')
    mode.add_argument('--apply', dest='apply', action='store_true',
                      help='Clean safe owned orphan copies after durable result capture.')
    parser.set_defaults(apply=False)
    parser.add_argument('--state-dir', type=Path,
                        help='Private state root; defaults to DEEPSEEK_TEAM_STATE_DIR, '
                             'saved selection or ~/.local/state/codex-deepseek.')
    parser.add_argument('--all-roots', action='store_true',
                        help='Check the effective, default and validated saved roots.')
    parser.add_argument('--json', action='store_true')
    return parser


def main(argv):
    args = _parser().parse_args(argv)
    try:
        from . import state_storage, workspace, workspace_gc
        if args.all_roots:
            roots, errors = _known_state_roots(args.state_dir)
            report = _collect_roots(roots, apply=args.apply)
            report['errors'].extend({'state_root': None, 'reason': error, 'code': 78}
                                    for error in errors)
        else:
            root = state_storage.state_root(args.state_dir)
            report = workspace_gc.collect(root, apply=args.apply)
    except state_storage.StorageError as error:
        print(str(error), file=sys.stderr)
        return error.code
    except workspace.WorkspaceError as error:
        print(str(error), file=sys.stderr)
        return getattr(error, 'code', 78)
    except OSError:
        print('Cannot collect workspace state; retained data was not changed.', file=sys.stderr)
        return 78
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=True))
    else:
        print(describe(report))
    return 78 if report.get('errors') else 0
