"""Settings and owned-copy CLI, kept separate from provider authentication."""
import argparse
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import subprocess

from . import (check_evidence, check_reuse, lessons, project, runtime_preflight, settings,
               state_storage, toolchains, worker, workspace)


def _lessons_guidance(root):
    """Read-only root-specific advisory guidance; never hides instructions on error."""
    if root is None:
        return ''
    try:
        return lessons.render_guidance(root)
    except lessons.LessonError as error:
        return 'Delegation lessons state is unavailable: ' + str(error)


def policy_options(parser):
    parser.add_argument('--delegation-level', type=settings.parse_level,
                        choices=settings.LEVELS,
                        help='auto adapts per task; 25/50/75 force a fixed profile.')
    parser.add_argument('--access', choices=settings.ACCESS)
    parser.add_argument('--effort', choices=settings.EFFORT_CHOICES,
                        help='auto lets the frontier coordinator choose per DeepSeek assignment; '
                             'low/high/max force a canonical level. The legacy medium spelling is '
                             'accepted and saved as high.')
    parser.add_argument('--max-workers', type=int,
                        help='Maximum concurrently running workers (1..64, default 8); extra jobs queue.')


def _record(copy):
    return dict(copy.metadata, id=copy.id, path=str(copy.path))


def _state_root(value):
    try:
        return state_storage.state_root(value)
    except state_storage.StorageError as error:
        raise workspace.WorkspaceError(str(error), error.code) from None


def _inspect_all(value):
    from . import workspace_gc
    state = _state_root(value)
    if not state.exists() and not state.is_symlink():
        return []
    root_fd = copies_fd = None
    try:
        try:
            root_fd = state_storage.open_private_directory(state)
            copies_fd = state_storage.open_private_directory(state / 'workspaces', parent=root_fd)
        except state_storage.StorageError as error:
            if error.errno == errno.ENOENT and not (state / 'workspaces').exists():
                return []
            raise workspace.WorkspaceError(str(error), error.code) from None
        identifiers = [name for name in os.listdir(copies_fd)
                       if re.fullmatch('[0-9a-f]{32}', name)]
        return [workspace_gc.inspect_workspace(state, identifier)
                for identifier in sorted(identifiers)]
    finally:
        if copies_fd is not None:
            os.close(copies_fd)
        if root_fd is not None:
            os.close(root_fd)


def _retained_patch(value, identifier, inspection):
    from . import workspace_gc, workspace_lifecycle
    state = _state_root(value)
    copy = workspace_gc._load_record(state, identifier)
    with workspace_lifecycle.directory(copy) as fd:
        raw = workspace_lifecycle.read_file(
            fd, 'changes.patch', maximum=workspace_gc.MAX_PATCH_BYTES)
    if raw is None:
        raise workspace.WorkspaceError('This cleaned workspace has no retained result patch.', 73)
    result = inspection.get('result') or {}
    patch = result.get('patch') if isinstance(result, dict) else None
    if (not isinstance(patch, dict) or patch.get('file') != 'changes.patch'
            or patch.get('bytes') != len(raw)
            or patch.get('sha256') != hashlib.sha256(raw).hexdigest()):
        raise workspace.WorkspaceError('Retained result patch failed its integrity check.', 73)
    return raw.decode('utf-8', errors='replace')


def _show_inspection(inspection):
    print('Workspace: ' + inspection['id'])
    state = ('execution data cleaned' if not inspection['execution_present']
             else 'execution data present')
    print('Working copy: ' + inspection['path'] + ' (' + state + ')')
    print(json.dumps(inspection['metadata'], indent=2))
    result = inspection.get('result')
    if result:
        print('Result patch: ' + json.dumps(result.get('patch', {}), sort_keys=True))


def _show_history(inspection):
    print('Workspace: ' + inspection['id'])
    print('Execution: ' + ('present' if inspection['execution_present'] else 'cleaned'))
    lease = inspection.get('lease')
    if lease is None:
        print('Lease: absent (legacy record)')
    else:
        print('Lease phase: ' + lease['phase'])
        print('Final reason: ' + str(lease.get('final_reason') or '-'))
        for attempt in lease.get('attempts', []):
            print('Attempt: {} {} {}'.format(
                attempt.get('operation', '-'), attempt.get('status', '-'),
                attempt.get('id', '-')))
        for event in lease.get('events', []):
            print('Event: {} {}'.format(event.get('event', '-'), event.get('at', '-')))
    result = inspection.get('result')
    if result:
        print('Result patch: ' + json.dumps(result.get('patch', {}), sort_keys=True))


def _show_action(report):
    print('Workspace: ' + report['id'])
    print('Action: ' + report['action'])
    print('Reason: ' + str(report.get('reason') or '-'))
    print('Allocated bytes: ' + str(report.get('allocated_bytes', 0)))
    print('Inodes: ' + str(report.get('inodes', 0)))


def _set_project(root, target, **options):
    """Publish project settings and the owned instruction blocks as one transaction.

    The settings lock is held across candidate-policy computation, preparation and
    validation of every existing managed block, the instruction writes and the final
    settings publication. Instructions are updated first and settings published last
    as the commit point; any earlier failure restores the managed files this call
    already wrote and leaves the saved policy untouched.
    """
    changes = settings.collect_changes(**options)
    with settings.settings_lock(target):
        previous = settings.read_bytes(target)
        values = settings.read_values(target)
        candidate = settings.merged_values(values, changes)
        changed = candidate != values
        policy = settings.candidate_policy(root, target, candidate)
        prepared = project.prepare_refresh(root, policy)
        written = project.apply_refresh(prepared)
        if not changed:
            return False
        try:
            settings.publish_values(target, previous, candidate)
        except settings.SettingsPublishedError:
            # The new policy is visible; keep instructions consistent with it.
            raise
        except BaseException as error:
            blocked = project.rollback_refresh(written)
            if not blocked:
                raise
            raise project.ProjectError(f'{error}; could not roll back: ' + ', '.join(blocked)) from error
        return True


def main(argv):
    parser = argparse.ArgumentParser(prog='deepseek-team ' + argv[0])
    subs = parser.add_subparsers(dest='command', required=True)
    if argv[0] == 'config':
        setter = subs.add_parser('set', help='Persist delegation/access/effort fields independently.')
        scope = setter.add_mutually_exclusive_group(required=True)
        scope.add_argument('--project', action='store_true')
        scope.add_argument('--global', dest='global_scope', action='store_true')
        setter.add_argument('--path', type=Path)
        policy_options(setter)
        show = subs.add_parser('show', help='Resolve effective policy and report each source.')
        show.add_argument('--effective', action='store_true')
        show.add_argument('--path', type=Path)
        show.add_argument('--json', action='store_true')
        show.add_argument('--instructions', action='store_true')
        show.add_argument('--runtime', choices=('codex', 'claude'), default='codex')
        policy_options(show)
    else:
        check = subs.add_parser('check', help='Preflight committed HEAD for preparation '
                                             'blockers; allocates no state or copy.')
        check.add_argument('path', type=Path, nargs='?', default=Path.cwd())
        check.add_argument('--json', action='store_true')
        for name in ('create', 'show', 'diff', 'prepare', 'import', 'verify',
                     'finalize', 'cancel', 'recover', 'resume', 'list', 'history'):
            sub = subs.add_parser(name)
            sub.add_argument('--state-dir', type=Path,
                             help='Private state root; defaults to DEEPSEEK_TEAM_STATE_DIR '
                                  'or ~/.local/state/codex-deepseek.')
            if name == 'create':
                sub.add_argument('path', type=Path, nargs='?', default=Path.cwd())
            elif name != 'list':
                sub.add_argument('id')
            if name in ('create', 'show', 'verify', 'list', 'history', 'finalize',
                        'cancel', 'recover', 'resume'):
                sub.add_argument('--json', action='store_true')
            if name in ('finalize', 'cancel'):
                sub.add_argument('--reason', default='accepted' if name == 'finalize' else 'cancelled')
            if name == 'prepare':
                sub.add_argument('--resume-after-failure', action='store_true')
                sub.add_argument('--recheck-environment', action='store_true',
                                 help='Allow one fresh declarative preparation observation '
                                      'despite an unchanged recorded environmental failure; '
                                      'it never bypasses recovery, admission or sandbox policy.')
                for option in ('tool', 'copy', 'env'):
                    sub.add_argument('--' + option, action='append', default=[], metavar='NAME=VALUE')
                sub.add_argument('--probe', action='append', default=[], metavar='COMMAND')
                sub.add_argument('--save-project', action='store_true')
            if name == 'verify':
                sub.add_argument('--check', action='append', metavar='COMMAND')
                sub.add_argument('--timeout', type=float, default=120,
                                 help='Per-command ceiling in seconds (default 120).')
                sub.add_argument('--reuse-checks', action='store_true',
                                 help='Attest deterministic local checks may reuse validated '
                                      'successful evidence while all inputs are unchanged.')
                sub.add_argument('--force', action='store_true',
                                 help='Execute checks even when validated reusable evidence exists.')
            if name == 'import':
                sub.add_argument('--include', action='append', required=True, metavar='FILE',
                                 help='Explicit repository-relative dirty file to copy; repeat as needed.')
        # A literal -- separates the trusted coordinator command from our options.
    command = []
    inputs = list(argv[1:])
    if argv[0] == 'workspace' and inputs[:1] == ['prepare'] and '--' in inputs:
        split = inputs.index('--')
        inputs, command = inputs[:split], inputs[split + 1:]
    args = parser.parse_args(inputs)
    try:
        if argv[0] == 'config':
            explicit_path = args.path is not None
            location = Path.cwd() if args.path is None else args.path
            root = settings.project_root(location, required=getattr(args, 'project', False))
            if args.command == 'show' and explicit_path and root is None:
                # An explicit path names the project to inspect; never fall back to CWD.
                raise settings.SettingsError(
                    '--path must name an existing Git working copy; omit --path to use the current directory.')
            if args.command == 'set':
                target = root / settings.PROJECT_FILE if args.project else settings.global_file()
                if args.project:
                    # Refresh only blocks this package already owns. Never add
                    # unsolicited instruction files and never alter user suffixes.
                    changed = _set_project(root, target, delegation_level=args.delegation_level,
                                           access=args.access, effort=args.effort,
                                           max_workers=args.max_workers)
                else:
                    changed = settings.set_values(target, delegation_level=args.delegation_level,
                                                  access=args.access, effort=args.effort,
                                                  max_workers=args.max_workers)
                print('Settings file: ' + str(target))
                print('Configuration updated.' if changed else 'Configuration already matches.')
                print('Applies to new jobs only; running processes are unchanged.')
                # Do not apply setter values a second time as artificial CLI overrides.
                policy = settings.resolve(root)
            else:
                policy = settings.resolve(root, delegation_level=args.delegation_level,
                                          access=args.access, effort=args.effort, max_workers=args.max_workers)
            if getattr(args, 'json', False):
                print(json.dumps(policy.as_dict(), indent=2))
            else:
                print(settings.describe(policy))
                if getattr(args, 'instructions', False):
                    print(settings.instructions(policy, args.runtime))
                    if policy.enabled:
                        guidance = _lessons_guidance(root)
                        if guidance:
                            print(guidance)
        else:
            if args.command == 'check':
                report = workspace.check_source(args.path)
                if args.json:
                    print(json.dumps(report, indent=2, ensure_ascii=True))
                else:
                    print(workspace.describe_check(report))
                return 0 if report['eligible'] else 78
            if args.command == 'list':
                reports = _inspect_all(args.state_dir)
                if args.json:
                    print(json.dumps(reports, indent=2, ensure_ascii=True))
                elif not reports:
                    print('No retained workspaces.')
                else:
                    for inspection in reports:
                        execution = 'present' if inspection['execution_present'] else 'cleaned'
                        lease = inspection.get('lease') or {}
                        print('{}: status={}; execution={}; lease={}'.format(
                            inspection['id'], inspection['metadata'].get('status', 'unknown'),
                            execution, lease.get('phase', 'absent')))
                return 0
            if args.command == 'show':
                from . import workspace_gc
                inspection = workspace_gc.inspect_workspace(args.state_dir, args.id)
                if args.json:
                    print(json.dumps(inspection, indent=2, ensure_ascii=True))
                else:
                    _show_inspection(inspection)
                return 0
            if args.command == 'history':
                from . import workspace_gc
                inspection = workspace_gc.inspect_workspace(args.state_dir, args.id)
                if args.json:
                    print(json.dumps(inspection, indent=2, ensure_ascii=True))
                else:
                    _show_history(inspection)
                return 0
            if args.command == 'diff':
                from . import workspace_gc
                inspection = workspace_gc.inspect_workspace(args.state_dir, args.id)
                if inspection['execution_present']:
                    copy = workspace.load(_state_root(args.state_dir), args.id)
                    print(copy.diff(), end='')
                else:
                    print(_retained_patch(args.state_dir, args.id, inspection), end='')
                return 0
            if args.command in ('finalize', 'cancel', 'recover', 'resume'):
                from . import workspace_gc
                copy = workspace_gc._load_record(_state_root(args.state_dir), args.id)
                if args.command == 'finalize':
                    report = workspace_gc.finalize(copy, reason=args.reason)
                elif args.command == 'cancel':
                    report = workspace_gc.cancel(copy, reason=args.reason)
                elif args.command == 'recover':
                    report = workspace_gc.recover(copy)
                else:
                    inspection = workspace_gc.inspect_workspace(args.state_dir, args.id)
                    lease = inspection.get('lease')
                    if lease and lease['phase'] in ('finalized', 'cancelled'):
                        raise workspace.WorkspaceError(
                            'A finalized workspace cannot be resumed; create a new copy.', 73)
                    if not inspection['execution_present']:
                        raise workspace.WorkspaceError(
                            'This workspace has no execution tree to resume.', 73)
                    report = workspace_gc.recover(copy)
                if getattr(args, 'json', False):
                    print(json.dumps(report, indent=2, ensure_ascii=True))
                else:
                    _show_action(report)
                return 0
            copy = (workspace.create(args.path, args.state_dir) if args.command == 'create'
                    else workspace.load(args.state_dir, args.id))
            if args.command == 'prepare':
                declarative = bool(args.tool or args.copy or args.env or args.probe or args.save_project)
                if declarative:
                    def pairs(values):
                        result = []
                        for value in values:
                            if '=' not in value:
                                raise workspace.WorkspaceError('Preparation options require NAME=VALUE.', 64)
                            name, content = value.split('=', 1)
                            if not name or not content:
                                raise workspace.WorkspaceError('Preparation names and values must not be empty.', 64)
                            result.append((name, content))
                        return result
                    tools = dict(pairs(args.tool))
                    environment = dict(pairs(args.env))
                    copies = pairs(args.copy)
                    toolchains.prepare(copy, tools=tools, copies=copies, environment=environment,
                        probes=args.probe, save_project=args.save_project,
                        recover=args.resume_after_failure,
                        recheck_environment=args.recheck_environment)
                if command:
                    if args.recheck_environment:
                        raise workspace.WorkspaceError(
                            '--recheck-environment applies only to declarative preparation; '
                            'legacy host preparation commands are never classified.', 64)
                    return workspace.prepare(copy, command, recover=args.resume_after_failure)
                if not declarative:
                    raise workspace.WorkspaceError('workspace prepare requires options or -- COMMAND.', 64)
                print('Prepared toolchains and project inputs for workspace ' + copy.id + '.')
                return 0
            if args.command == 'verify':
                result = workspace.verify_checks(
                    copy, args.check, timeout=args.timeout,
                    reuse_checks=args.reuse_checks, force=args.force)
                if args.json:
                    print(json.dumps(result, indent=2))
                else:
                    print('Verification: ' + result['status'] + '; original worker result retained.')
                    print('Diagnostic files: ' + str(copy.directory / 'diagnostics'))
                if result['status'] == 'passed':
                    return 0
                return 124 if any(row.get('timed_out') for row in result['checks']) else 65
            if args.command == 'import':
                imported = workspace.import_paths(copy, args.include)
                print('Imported coordinator-prepared source: ' + ', '.join(imported))
                return 0
            if getattr(args, 'json', False):
                print(json.dumps(_record(copy), indent=2))
            else:
                print('Workspace: ' + copy.id + '\nWorking copy: ' + str(copy.path))
                print(json.dumps(copy.metadata, indent=2))
                if args.command == 'create':
                    print('Committed HEAD copied. Source dirty/untracked/ignored files were preserved and not copied.')
        return 0
    except (settings.SettingsError, workspace.WorkspaceError, project.ProjectError,
            runtime_preflight.RuntimePreflightError, toolchains.ToolchainError,
            check_evidence.CheckEvidenceError, check_reuse.CheckReuseError,
            worker.WorkerError) as error:
        print(str(error), file=sys.stderr)
        return getattr(error, 'code', 78)
    except (OSError, subprocess.SubprocessError):
        print('Preparation/configuration failed; inspect owned copy and local filesystem permissions.', file=sys.stderr)
        return 78
    except KeyboardInterrupt:
        print('Cancelled; existing files were preserved.', file=sys.stderr)
        return 130
