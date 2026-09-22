#!/usr/bin/env python3
"""Install this checkout into a dedicated user venv; optional Ubuntu sandbox setup."""
import argparse
import os
from pathlib import Path
import subprocess
import sys
import shutil
import tempfile
import venv

OWNER = 'codex-deepseek-team installer v1\n'
COMMAND = 'codex-deepseek-team'
COMMANDS = (COMMAND, 'deepseek-team')


def check_prefix(prefix, marker):
    if prefix.is_symlink() or (prefix.exists() and not prefix.is_dir()):
        raise ValueError('Installation directory belongs to another tool; choose a new --prefix.')
    if not prefix.exists():
        return
    if marker.is_symlink() or (marker.exists() and not marker.is_file()):
        raise ValueError('Refusing an unsafe installation ownership marker.')
    contents = marker.read_text() if marker.exists() else ''
    if contents == OWNER:
        return
    if not OWNER.startswith(contents) or set(prefix.iterdir()) - {marker}:
        raise ValueError('Installation directory belongs to another tool; choose a new --prefix.')


def _links(prefix, bin_dir):
    environment = prefix / 'venv'
    return [(bin_dir / command, environment / 'bin' / command) for command in COMMANDS]


def is_ubuntu(path=Path('/etc/os-release')):
    try:
        values = {}
        for line in path.read_text(encoding='utf-8').splitlines():
            if '=' not in line:
                continue
            key, value = line.split('=', 1)
            values[key] = value.strip().strip('"\'')
        return values.get('ID', '').lower() == 'ubuntu' or 'ubuntu' in values.get('ID_LIKE', '').lower().split()
    except OSError:
        return False


def _install_ubuntu_sandbox(team_command):
    """Explicit privileged path; never called by a plain package install."""
    subprocess.run(['sudo', 'apt-get', 'install', '-y', 'bubblewrap', 'apparmor'], check=True)
    subprocess.run([str(team_command), 'sandbox', 'install-apparmor'], check=True)
    subprocess.run([str(team_command), 'sandbox', 'status'], check=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prefix', type=Path, default=Path.home() / '.local/share/codex-deepseek-team')
    parser.add_argument('--bin-dir', type=Path, default=Path.home() / '.local/bin')
    parser.add_argument('--with-sandbox', action='store_true',
                        help='Ubuntu: install bubblewrap/apparmor and the package named AppArmor profile using sudo.')
    args = parser.parse_args(argv)
    if sys.platform != 'linux' or sys.version_info < (3, 11):
        print('Linux and Python 3.11+ are required.', file=sys.stderr)
        return 78
    if args.with_sandbox and not is_ubuntu():
        print('--with-sandbox currently manages system packages/AppArmor only on Ubuntu; install Bubblewrap according to your distribution and run deepseek-team sandbox status.', file=sys.stderr)
        return 78
    prefix = Path(os.path.abspath(args.prefix.expanduser()))
    bin_dir = Path(os.path.abspath(args.bin_dir.expanduser()))
    marker = prefix / '.codex-deepseek-team-install'
    published = []
    try:
        check_prefix(prefix, marker)
        links = _links(prefix, bin_dir)
        for binary, entrypoint in links:
            if binary.exists() or binary.is_symlink():
                if not binary.is_symlink() or binary.resolve() != entrypoint.resolve():
                    raise ValueError(f'Command {binary.name} already exists outside this installation; choose a different --bin-dir.')
        prefix.mkdir(parents=True, exist_ok=True)
        if not marker.exists() or marker.read_text() != OWNER:
            with marker.open('w') as output:
                output.write(OWNER)
                output.flush()
                os.fsync(output.fileno())
        environment = prefix / 'venv'
        if environment.is_symlink():
            raise ValueError('Refusing a symlinked environment.')
        venv.EnvBuilder(with_pip=True).create(environment)
        subprocess.run([str(environment / 'bin/python'), '-m', 'pip', 'install', '--upgrade',
                        str(Path(__file__).resolve().parent)], check=True)
        for _binary, entrypoint in links:
            if not entrypoint.is_file():
                raise ValueError(f'Installation did not produce the expected {entrypoint.name} command.')
        bin_dir.mkdir(parents=True, exist_ok=True)
        for binary, entrypoint in links:
            if not binary.is_symlink():
                with tempfile.TemporaryDirectory(prefix='.team-link-', dir=bin_dir) as temporary:
                    link = Path(temporary) / binary.name
                    link.symlink_to(entrypoint)
                    os.link(link, binary, follow_symlinks=False)
                    published.append((binary, entrypoint))
            if binary.resolve() != entrypoint.resolve():
                raise ValueError(f'Command {binary.name} changed concurrently; the other command was preserved.')
        if shutil.which('codex'):
            hook_result = subprocess.run(
                [str(environment / 'bin/deepseek-team'), 'hooks', 'install'],
                check=False)
            if hook_result.returncode:
                print('Warning: Codex coordination hooks could not be installed; run deepseek-team setup --runtime codex --no-key after fixing ~/.codex/hooks.json.', file=sys.stderr)
        if shutil.which('claude'):
            hook_result = subprocess.run(
                [str(environment / 'bin/deepseek-team'), 'hooks', 'install', '--runtime', 'claude'],
                check=False)
            if hook_result.returncode:
                print('Warning: Claude coordination hooks could not be installed; check Claude settings.json and run deepseek-team setup --runtime claude --no-key.', file=sys.stderr)
        if args.with_sandbox:
            _install_ubuntu_sandbox(environment / 'bin/deepseek-team')
        print('Installed commands: ' + ', '.join(str(binary) for binary, _ in links))
        if args.with_sandbox:
            print('Ubuntu Bubblewrap/AppArmor worker isolation installed and probed.')
        else:
            print(f'Before running workers, verify isolation: {bin_dir / "deepseek-team"} sandbox status')
            print('On Ubuntu, rerun `python3 install.py --with-sandbox` if Bubblewrap is blocked by AppArmor userns policy.')
        print(f'Next: {bin_dir / "deepseek-team"} setup')
        print(f'Ensure {bin_dir} is in PATH, then run deepseek-team init in a Git repository.')
        return 0
    except ValueError as error:
        for binary, entrypoint in reversed(published):
            try:
                if binary.is_symlink() and binary.resolve() == entrypoint.resolve():
                    binary.unlink()
            except OSError:
                pass
        print(str(error), file=sys.stderr)
    except (OSError, subprocess.CalledProcessError):
        for binary, entrypoint in reversed(published):
            try:
                if binary.is_symlink() and binary.resolve() == entrypoint.resolve():
                    binary.unlink()
            except OSError:
                pass
        print('Installation failed; check Python venv/pip support, network, sudo/system packages and directory permissions. Rerun after fixing the error.', file=sys.stderr)
    return 78


if __name__ == '__main__':
    raise SystemExit(main())
