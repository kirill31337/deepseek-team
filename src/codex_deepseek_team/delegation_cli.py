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
    parser.add_argument('--effort', choices=settings.EFFORT,
                        help='auto lets the frontier coordinator choose per DeepSeek assignment; low/medium/high force a level.')
    parser.add_argument('--max-workers', type=int,
                        help='Maximum concurrently running workers (1..64, default 8); extra jobs queue.')


def _record(copy):
    return dict(copy.metadata, id=copy.id, path=str(copy.path))


def main(argv):
    parser = argparse.ArgumentParser(prog='deepseek-team ' + argv[0])
    subs = parser.add_subparsers(dest='command', required=True)
    if argv[0] == 'config':
        setter = subs.add_parser('set', help='Persist delegation/access/effort fields independently.')
        scope = setter.add_mutually_exclusive_group(required=True)
        scope.add_argument('--project', action='store_true')
        scope.add_argument('--global', dest='global_scope', action='store_true')
        setter.add_argument('--path', type=Path, default=Path.cwd())
        policy_options(setter)
        show = subs.add_parser('show', help='Resolve effective policy and report each source.')
        show.add_argument('--effective', action='store_true')
        show.add_argument('--path', type=Path, default=Path.cwd())
        show.add_argument('--json', action='store_true')
        show.add_argument('--instructions', action='store_true')
        show.add_argument('--runtime', choices=('codex', 'claude'), default='codex')
        policy_options(show)
    else:
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
            root = settings.project_root(args.path, required=getattr(args, 'project', False))
            if args.command == 'set':
                target = root / settings.PROJECT_FILE if args.project else settings.global_file()
                changed = settings.set_values(target, delegation_level=args.delegation_level,
                                              access=args.access, effort=args.effort, max_workers=args.max_workers)
                if args.project:
                    # Refresh only blocks this package already owns. Never add
                    # unsolicited instruction files and never alter user suffixes.
                    for runtime, filename in project.TARGETS.items():
                        file = root / filename
                        content, _ = project._read_agents(file)
                        if content and project.START_MARKER in content:
                            project.attach(root, coordinator=runtime)
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
