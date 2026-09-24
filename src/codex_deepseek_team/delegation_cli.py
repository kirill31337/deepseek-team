"""Settings and owned-copy CLI, kept separate from provider authentication."""
import argparse
import json
from pathlib import Path
import sys
import subprocess

from . import project, settings, workspace


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
        for name in ('create', 'show', 'diff', 'prepare', 'import'):
            sub = subs.add_parser(name)
            sub.add_argument('--state-dir', type=Path, default=Path.home() / '.local/state/codex-deepseek')
            if name == 'create':
                sub.add_argument('path', type=Path, nargs='?', default=Path.cwd())
            else:
                sub.add_argument('id')
            if name in ('create', 'show'):
                sub.add_argument('--json', action='store_true')
            if name == 'prepare':
                sub.add_argument('--resume-after-failure', action='store_true')
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
        else:
            if args.command == 'check':
                report = workspace.check_source(args.path)
                if args.json:
                    print(json.dumps(report, indent=2, ensure_ascii=True))
                else:
                    print(workspace.describe_check(report))
                return 0 if report['eligible'] else 78
            copy = (workspace.create(args.path, args.state_dir) if args.command == 'create'
                    else workspace.load(args.state_dir, args.id))
            if args.command == 'prepare':
                return workspace.prepare(copy, command, recover=args.resume_after_failure)
            if args.command == 'import':
                imported = workspace.import_paths(copy, args.include)
                print('Imported coordinator-prepared source: ' + ', '.join(imported))
                return 0
            if args.command == 'diff':
                print(copy.diff(), end='')
            elif getattr(args, 'json', False):
                print(json.dumps(_record(copy), indent=2))
            else:
                print('Workspace: ' + copy.id + '\nWorking copy: ' + str(copy.path))
                print(json.dumps(copy.metadata, indent=2))
                if args.command == 'create':
                    print('Committed HEAD copied. Source dirty/untracked/ignored files were preserved and not copied.')
        return 0
    except (settings.SettingsError, workspace.WorkspaceError, project.ProjectError) as error:
        print(str(error), file=sys.stderr)
        return getattr(error, 'code', 78)
    except (OSError, subprocess.SubprocessError):
        print('Preparation/configuration failed; inspect owned copy and local filesystem permissions.', file=sys.stderr)
        return 78
    except KeyboardInterrupt:
        print('Cancelled; existing files were preserved.', file=sys.stderr)
        return 130
