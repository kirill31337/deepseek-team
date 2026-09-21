"""User-facing CLI for setup, project instructions and isolated DeepSeek workers."""
import argparse
import getpass
from pathlib import Path
import shutil
import sys

from . import __version__


def _runtimes(value):
    if value == 'both':
        return ('codex', 'claude')
    if value in ('codex', 'claude'):
        return (value,)
    if value == 'auto':
        found = tuple(name for name in ('codex', 'claude') if shutil.which(name))
        if found:
            return found
        raise ValueError('Neither Codex nor Claude Code is available for --runtime auto.')
    raise ValueError('runtime must be codex, claude, both or auto')


def _sandbox_status(sandbox):
    backend = sandbox.probe_backend()
    restriction = sandbox.apparmor_restriction()
    print(f'Bubblewrap: {backend.bwrap}')
    print(f'OS sandbox backend: {backend.source}')
    if restriction is None:
        print('kernel.apparmor_restrict_unprivileged_userns: unavailable')
    else:
        print(f'kernel.apparmor_restrict_unprivileged_userns={restriction}')
    if backend.source == 'apparmor':
        print(f'AppArmor profile: {sandbox.PROFILE_NAME} (selected with aa-exec)')
    return 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if sys.platform != 'linux':
        print('This release supports Linux with Python 3.11+ and Codex and/or Claude Code.', file=sys.stderr)
        return 78
    from . import config, doctor, sandbox, worker
    if argv and argv[0] == 'coordinator-hook':
        from . import codex_hooks
        return codex_hooks.main()
    if argv and argv[0] == 'coordination':
        from . import coordination_cli
        return coordination_cli.main(argv)
    if argv and argv[0] in ('config', 'workspace'):
        from . import delegation_cli
        return delegation_cli.main(argv)
    if argv and argv[0] == 'worker':
        original = sys.argv
        try:
            sys.argv = ['deepseek-team worker', *argv[1:]]
            return worker.main()
        finally:
            sys.argv = original
    if argv and argv[0] == 'doctor':
        return doctor.main(argv[1:])
    parser = argparse.ArgumentParser(prog='deepseek-team', description=__doc__)
    parser.add_argument('--version', action='version', version=f'deepseek-team {__version__}')
    commands = parser.add_subparsers(dest='command', required=True)
    setup = commands.add_parser('setup', help='Configure DeepSeek without changing coordinator auth or primary model.')
    setup.add_argument('--runtime', choices=['codex', 'claude', 'both', 'auto'], default='codex',
                       help='Coordinator runtime(s) to prepare; default codex preserves legacy behavior.')
    setup.add_argument('--no-key', action='store_true', help='Do not prompt for the shared DeepSeek credential.')
    reset = commands.add_parser('reset', help='Remove only package-owned coordinator configuration; retain keys and primary auth.')
    reset.add_argument('--runtime', choices=['codex', 'claude', 'both', 'auto'], default='codex')
    for name, help_text in [('init', 'Attach delegation instructions to a Git repository.'),
                            ('detach', 'Remove the managed instructions and preserve user content.')]:
        sub = commands.add_parser(name, help=help_text)
        sub.add_argument('--coordinator', choices=['codex', 'claude', 'both'], default='codex',
                         help='Instruction file(s) to manage; default codex preserves legacy behavior.')
        sub.add_argument('path', nargs='?', type=Path, default=Path.cwd())
    auth = commands.add_parser('auth', help='Manage the private user-level DeepSeek credential.')
    auth_commands = auth.add_subparsers(dest='auth_command', required=True)
    setter = auth_commands.add_parser('set', help='Read the key from a hidden terminal prompt.')
    setter.add_argument('--stdin', action='store_true', help='Read from a pipe; never pass a key as a command argument.')
    auth_commands.add_parser('status', help='Report whether a usable key is present, without displaying it.')
    auth_commands.add_parser('remove', help='Delete the saved key; environment overrides are unaffected.')
    hooks_cmd = commands.add_parser('hooks', help='Manage stable user-level Codex coordination hooks.')
    hooks_commands = hooks_cmd.add_subparsers(dest='hooks_command', required=True)
    hooks_commands.add_parser('install', help='Install/update package-owned user-level Codex hooks.')
    hooks_commands.add_parser('status', help='Report whether the exact package hook set is installed.')
    hooks_commands.add_parser('remove', help='Remove only package-owned Codex hook handlers.')
    sandbox_cmd = commands.add_parser('sandbox', help='Inspect or manage Linux Bubblewrap/AppArmor isolation.')
    sandbox_commands = sandbox_cmd.add_subparsers(dest='sandbox_command', required=True)
    sandbox_commands.add_parser('status', help='Probe Bubblewrap and the effective AppArmor/userns backend.')
    sandbox_commands.add_parser('install-apparmor', help='Install/reload only the package-owned named AppArmor profile.')
    sandbox_commands.add_parser('remove-apparmor', help='Remove only an unchanged package-owned AppArmor profile.')
    commands.add_parser('config', help='Manage delegation profiles; use config --help.')
    commands.add_parser('workspace', help='Prepare and inspect owned development copies; use workspace --help.')
    commands.add_parser('doctor', help='Check local setup; use doctor --help for runtime/live options.')
    commands.add_parser('worker', help='Run a worker; use worker --help for runtime/read/write options.')
    args = parser.parse_args(argv)
    try:
        if args.command == 'setup':
            runtimes = _runtimes(args.runtime)
            if 'codex' in runtimes:
                changed = config.configure(worker.codex_home())
                hooks_changed = config.install_codex_hooks(worker.codex_home())
                print('DeepSeek Codex provider configured.' if changed else 'Compatible DeepSeek Codex provider already configured.')
                print('Codex coordination hooks installed.' if hooks_changed else 'Codex coordination hooks already installed.')
                print('Codex primary model and OpenAI authentication were preserved.')
                print('Codex requires one native hook review/trust via /hooks; the stable definition persists across package updates.')
            if 'claude' in runtimes:
                print('Claude Code runtime uses an isolated DeepSeek child environment; Claude configuration/auth were not changed.')
            if not args.no_key and not worker.load_api_key().strip():
                if sys.stdin.isatty():
                    config.save_key(getpass.getpass('DeepSeek API key (hidden): '))
                    print('Key stored privately.')
                else:
                    print('Set your key with: deepseek-team auth set')
        elif args.command == 'reset':
            runtimes = _runtimes(args.runtime)
            if 'codex' in runtimes:
                changed = config.remove_provider(worker.codex_home())
                hooks_changed = config.remove_codex_hooks(worker.codex_home())
                print('Managed Codex provider removed.' if changed else 'No package-owned Codex provider block to remove.')
                print('Managed Codex coordination hooks removed.' if hooks_changed else 'No package-owned Codex hooks to remove.')
            if 'claude' in runtimes:
                print('No package-owned Claude configuration exists; nothing was changed.')
        elif args.command in ['init', 'detach']:
            from . import project
            try:
                changed = (project.attach if args.command == 'init' else project.detach)(
                    args.path, coordinator=args.coordinator)
            except project.ProjectError as error:
                print(str(error), file=sys.stderr)
                return 78
            print('Project instructions updated.' if changed else 'Project instructions already in the requested state.')
        elif args.command == 'auth':
            if args.auth_command == 'set':
                if not args.stdin and not sys.stdin.isatty():
                    raise config.ConfigError('Use a terminal prompt or --stdin; never put the key in command arguments.')
                value = sys.stdin.read(4097) if args.stdin else getpass.getpass('DeepSeek API key (hidden): ')
                config.save_key(value)
                print('Key stored privately; value omitted.')
            elif args.auth_command == 'remove':
                print('Saved key removed.' if config.delete_key() else 'No saved key was present.')
            else:
                present = bool(worker.load_api_key().strip())
                print('Key available; value omitted.' if present else 'No key configured.')
                return 0 if present else 78
        elif args.command == 'hooks':
            home = worker.codex_home()
            if args.hooks_command == 'install':
                changed = config.install_codex_hooks(home)
                print('Codex coordination hooks installed.' if changed else 'Codex coordination hooks already installed.')
                print('Review/trust this stable user-level definition once in Codex with /hooks.')
            elif args.hooks_command == 'remove':
                print('Codex coordination hooks removed.' if config.remove_codex_hooks(home)
                      else 'No package-owned Codex coordination hooks were present.')
            else:
                installed = config.codex_hooks_status(home)
                print('Codex coordination hooks installed; native trust state is managed by Codex /hooks.'
                      if installed else 'Codex coordination hooks are not installed.')
                return 0 if installed else 78
        elif args.command == 'sandbox':
            if args.sandbox_command == 'status':
                return _sandbox_status(sandbox)
            if args.sandbox_command == 'install-apparmor':
                changed = sandbox.install_apparmor()
                print('DeepSeek Team AppArmor profile installed and loaded.' if changed
                      else 'DeepSeek Team AppArmor profile already matched and was reloaded.')
            else:
                changed = sandbox.remove_apparmor()
                print('DeepSeek Team AppArmor profile unloaded and removed.' if changed
                      else 'No package-owned AppArmor profile was present.')
        return 0
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 78
    except sandbox.SandboxError as error:
        print(error.message, file=sys.stderr)
        return error.code
    except (config.ConfigError, worker.WorkerError) as error:
        print(error.message if isinstance(error, worker.WorkerError) else str(error), file=sys.stderr)
        return error.code if isinstance(error, worker.WorkerError) else 78
    except (OSError, EOFError):
        print('Operation could not complete; check local paths, permissions and terminal input.', file=sys.stderr)
        return 78
    except KeyboardInterrupt:
        print('Cancelled.', file=sys.stderr)
        return 130
