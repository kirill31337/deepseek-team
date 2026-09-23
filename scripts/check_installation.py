#!/usr/bin/env python3
"""Exercise current-package installation, replacement, and removal with a real installer."""
from __future__ import annotations

import argparse
from email.parser import BytesParser
from pathlib import Path, PurePosixPath
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile


DIST_NAME = 'deepseek-team'
ENTRYPOINTS = ('deepseek-team',)
RESOURCES = (
    'codex_deepseek_team/data/delegation.md',
    'codex_deepseek_team/data/apparmor/deepseek-team-bwrap',
)
_NORMALIZE_NAME = re.compile(r'[-_.]+')
_SAFE_VERSION = re.compile(r'^[A-Za-z0-9][A-Za-z0-9.!+_-]*$')


class CheckError(RuntimeError):
    """An installation lifecycle check failed."""


def wheel_path(value: str) -> Path:
    path = Path(value)
    try:
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise argparse.ArgumentTypeError(f'wheel is not accessible: {error}') from None
    if not resolved.is_file() or resolved.suffix != '.whl':
        raise argparse.ArgumentTypeError('wheel must be an existing .whl file')
    return resolved


def wheel_identity(path: Path) -> tuple[str, str]:
    """Read project name/version from wheel metadata without importing its code."""
    try:
        with zipfile.ZipFile(path) as archive:
            candidates = []
            for info in archive.infolist():
                parts = PurePosixPath(info.filename).parts
                if len(parts) == 2 and parts[0].endswith('.dist-info') and parts[1] == 'METADATA':
                    candidates.append(info)
            if len(candidates) != 1:
                raise CheckError(f'{path.name}: expected exactly one top-level wheel METADATA file')
            metadata_info = candidates[0]
            if metadata_info.file_size > 1024 * 1024:
                raise CheckError(f'{path.name}: wheel METADATA is unexpectedly large')
            metadata = BytesParser().parsebytes(archive.read(metadata_info))
    except (OSError, zipfile.BadZipFile, RuntimeError) as error:
        if isinstance(error, CheckError):
            raise
        raise CheckError(f'{path.name}: cannot read wheel metadata: {error}') from None

    name = metadata.get('Name', '').strip()
    version = metadata.get('Version', '').strip()
    normalized = _NORMALIZE_NAME.sub('-', name).lower()
    if normalized != DIST_NAME:
        raise CheckError(f'{path.name}: expected distribution {DIST_NAME}, found {name or "no name"}')
    if len(version) > 200 or not _SAFE_VERSION.fullmatch(version):
        raise CheckError(f'{path.name}: invalid or missing wheel version metadata')
    return normalized, version


def isolated_environment(root: Path, inherited: dict[str, str] | os._Environ[str] | None = None) -> dict[str, str]:
    """Return an installer environment rooted entirely below *root*."""
    env = dict(os.environ if inherited is None else inherited)
    exact = {
        'PYTHONPATH', 'PYTHONHOME', 'VIRTUAL_ENV', 'CONDA_PREFIX',
        'GIT_ASKPASS', 'SSH_ASKPASS', 'SSH_AUTH_SOCK', 'NETRC',
        'GOOGLE_APPLICATION_CREDENTIALS', 'HTTP_PROXY', 'HTTPS_PROXY',
        'ALL_PROXY', 'NO_PROXY',
    }
    prefixes = ('ANTHROPIC_', 'OPENAI_', 'DEEPSEEK_', 'CODEX_', 'CLAUDE_',
                'PIP_', 'PIPX_', 'UV_', 'AWS_', 'AZURE_', 'GOOGLE_',
                'GITHUB_', 'GITLAB_', 'HF_', 'HUGGINGFACE_', 'TWINE_', 'NPM_')
    suffixes = ('_API_KEY', '_AUTH_TOKEN', '_ACCESS_TOKEN', '_TOKEN',
                '_PASSWORD', '_SECRET', '_CREDENTIALS')
    for name in list(env):
        upper = name.upper()
        if upper in exact or upper.startswith(prefixes) or upper.endswith(suffixes):
            env.pop(name, None)

    env.update({
        'HOME': str(root / 'home'),
        'USERPROFILE': str(root / 'home'),
        'XDG_CONFIG_HOME': str(root / 'xdg' / 'config'),
        'XDG_CACHE_HOME': str(root / 'xdg' / 'cache'),
        'XDG_DATA_HOME': str(root / 'xdg' / 'data'),
        'XDG_STATE_HOME': str(root / 'xdg' / 'state'),
        'XDG_RUNTIME_DIR': str(root / 'xdg' / 'runtime'),
        'CODEX_HOME': str(root / 'codex'),
        'CLAUDE_CONFIG_DIR': str(root / 'claude'),
        'PIPX_HOME': str(root / 'pipx' / 'home'),
        'PIPX_BIN_DIR': str(root / 'pipx' / 'bin'),
        'PIPX_MAN_DIR': str(root / 'pipx' / 'man'),
        'PIPX_DEFAULT_PYTHON': str(Path(sys.executable).resolve()),
        'PIPX_FETCH_PYTHON': 'never',
        'PIPX_DEFAULT_BACKEND': 'pip',
        'UV_TOOL_DIR': str(root / 'uv' / 'tools'),
        'UV_TOOL_BIN_DIR': str(root / 'uv' / 'bin'),
        'UV_CACHE_DIR': str(root / 'uv' / 'cache'),
        'UV_PYTHON': str(Path(sys.executable).resolve()),
        'UV_PYTHON_DOWNLOADS': 'never',
        'UV_OFFLINE': '1',
        'UV_NO_CONFIG': '1',
        'PIP_CONFIG_FILE': os.devnull,
        'PIP_DISABLE_PIP_VERSION_CHECK': '1',
        'PIP_NO_INPUT': '1',
        'PIP_NO_INDEX': '1',
        'PYTHONNOUSERSITE': '1',
        'GIT_CONFIG_NOSYSTEM': '1',
        'GIT_CONFIG_GLOBAL': os.devnull,
        'GIT_TERMINAL_PROMPT': '0',
        'TMPDIR': str(root / 'tmp'),
    })
    return env


def _run(args: list[str], *, env: dict[str, str], cwd: Path, phase: str) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(args, env=env, cwd=cwd, text=True,
                                capture_output=True, check=False)
    except OSError as error:
        raise CheckError(f'{phase}: could not start command: {error}') from None
    if result.returncode:
        details = (result.stderr or result.stdout).strip()
        if len(details) > 4000:
            details = details[-4000:]
        suffix = f': {details}' if details else ''
        raise CheckError(f'{phase}: command exited {result.returncode}{suffix}')
    return result


def _tool_executable(name: str) -> Path:
    found = shutil.which(name)
    if not found:
        raise CheckError(f'{name} executable was not found on PATH')
    path = Path(found).resolve()
    if not path.is_file() or not os.access(path, os.X_OK):
        raise CheckError(f'{name} executable is not an executable file: {path}')
    return path


class Installer:
    def __init__(self, name: str, executable: Path, root: Path, env: dict[str, str], cwd: Path):
        self.name = name
        self.executable = executable
        self.root = root
        self.env = env
        self.cwd = cwd

    @property
    def bin_dir(self) -> Path:
        if self.name == 'pip':
            return self.root / 'pip-venv' / 'bin'
        return Path(self.env['PIPX_BIN_DIR'] if self.name == 'pipx' else self.env['UV_TOOL_BIN_DIR'])

    @property
    def python(self) -> Path:
        if self.name == 'pip':
            return self.root / 'pip-venv' / 'bin' / 'python'
        manager_home = Path(self.env['PIPX_HOME']) / 'venvs' if self.name == 'pipx' else Path(self.env['UV_TOOL_DIR'])
        return manager_home / DIST_NAME / 'bin' / 'python'

    @property
    def commands(self) -> tuple[Path, ...]:
        return tuple(self.bin_dir / name for name in ENTRYPOINTS)

    def install(self, wheel: Path, *, replacement: bool) -> None:
        if self.name == 'pip':
            if not self.python.exists():
                _run([str(self.executable), '-m', 'venv', str(self.root / 'pip-venv')],
                     env=self.env, cwd=self.cwd, phase='pip environment creation')
            args = [str(self.python), '-m', 'pip', 'install', '--no-index', '--no-deps']
            if replacement:
                args.append('--force-reinstall')
            args.append(str(wheel))
        elif self.name == 'pipx':
            args = [str(self.executable), 'install', '--python', str(Path(sys.executable).resolve()),
                    '--fetch-python=never', '--backend=pip', '--skip-maintenance',
                    '--pip-args=--no-index --no-deps']
            if replacement:
                args.append('--force')
            args.append(str(wheel))
        else:
            args = [str(self.executable), 'tool', 'install', '--python', str(Path(sys.executable).resolve()),
                    '--no-python-downloads', '--offline', '--no-index', '--no-config']
            if replacement:
                args.append('--force')
            args.append(str(wheel))
        _run(args, env=self.env, cwd=self.cwd,
             phase=f'{self.name} {"replacement" if replacement else "install"}')

    def uninstall(self) -> None:
        if self.name == 'pip':
            args = [str(self.python), '-m', 'pip', 'uninstall', '-y', DIST_NAME]
        elif self.name == 'pipx':
            args = [str(self.executable), 'uninstall', '--skip-maintenance', DIST_NAME]
        else:
            args = [str(self.executable), 'tool', 'uninstall', '--no-config', DIST_NAME]
        _run(args, env=self.env, cwd=self.cwd, phase=f'{self.name} uninstall')


_RESOURCE_CHECK = r'''
from importlib import metadata
from pathlib import Path
import sys

distribution = metadata.distribution(sys.argv[1])
if distribution.version != sys.argv[2]:
    raise SystemExit(f'installed version {distribution.version!r}, expected {sys.argv[2]!r}')
entrypoints = {entry.name: entry.value for entry in distribution.entry_points
               if entry.group == 'console_scripts'}
if entrypoints != {'deepseek-team': 'codex_deepseek_team.cli:main'}:
    raise SystemExit(f'unexpected console scripts: {entrypoints!r}')
prefix = Path(sys.prefix).resolve()
for relative in sys.argv[3:]:
    resource = Path(distribution.locate_file(relative)).resolve()
    if not resource.is_relative_to(prefix):
        raise SystemExit(f'resource resolved outside installed environment: {relative}')
    if not resource.is_file() or resource.stat().st_size == 0:
        raise SystemExit(f'missing or empty installed resource: {relative}')
'''


def verify_install(installer: Installer, version: str) -> None:
    if not installer.python.is_file():
        raise CheckError(f'{installer.name} did not create an installed Python environment')
    _run([str(installer.python), '-I', '-c', _RESOURCE_CHECK,
          DIST_NAME, version, *RESOURCES], env=installer.env, cwd=installer.cwd,
         phase=f'{installer.name} installed metadata/resources verification')
    expected_version = f'deepseek-team {version}'
    for command in installer.commands:
        if not command.exists():
            raise CheckError(f'{installer.name} did not publish {command.name}')
        version_result = _run([str(command), '--version'], env=installer.env,
                              cwd=installer.cwd, phase=f'{command.name} --version')
        if version_result.stdout.strip() != expected_version:
            raise CheckError(
                f'{command.name} --version returned {version_result.stdout.strip()!r}; '
                f'expected {expected_version!r}')
        help_result = _run([str(command), '--help'], env=installer.env,
                           cwd=installer.cwd, phase=f'{command.name} --help')
        if 'usage:' not in help_result.stdout.lower():
            raise CheckError(f'{command.name} --help did not return argparse help')


def create_sentinels(root: Path, env: dict[str, str]) -> dict[Path, bytes]:
    sentinels = {
        Path(env['HOME']) / '.installation-check-user-file': b'user file\n',
        Path(env['XDG_CONFIG_HOME']) / 'deepseek-team' / 'config.toml':
            b'delegation_level = "auto"\n',
        Path(env['HOME']) / '.config/codex-deepseek/api-key': b'synthetic-private-key\n',
        Path(env['HOME']) / '.local/state/codex-deepseek/history.json': b'{"history":"preserve"}\n',
        Path(env['CODEX_HOME']) / 'config.toml': b'model = "user-choice"\n',
        Path(env['CODEX_HOME']) / 'auth.json': b'{"authentication":"preserve"}\n',
        Path(env['CODEX_HOME']) / 'hooks.json': b'{"hooks":{}}\n',
        Path(env['CLAUDE_CONFIG_DIR']) / 'settings.json': b'{"theme":"user-choice"}\n',
        Path(env['CLAUDE_CONFIG_DIR']) / '.credentials.json': b'{"authentication":"preserve"}\n',
        root / 'project' / 'AGENTS.md': b'# User project instructions\n',
        root / 'project' / '.deepseek-team.toml':
            b'delegation_level = "auto"\naccess = "read-only"\n',
    }
    for path, content in sentinels.items():
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.write_bytes(content)
        path.chmod(0o600)
    return sentinels


def assert_sentinels(sentinels: dict[Path, bytes], phase: str) -> None:
    for path, expected in sentinels.items():
        try:
            actual = path.read_bytes()
        except OSError as error:
            raise CheckError(f'{phase}: external configuration sentinel was removed: {path.name}: {error}') from None
        if actual != expected:
            raise CheckError(f'{phase}: external configuration sentinel changed: {path.name}')


def manager_executable(name: str) -> Path:
    if name == 'pip':
        executable = Path(sys.executable).resolve()
        if not executable.is_file() or not os.access(executable, os.X_OK):
            raise CheckError(f'current Python is not executable: {executable}')
        return executable
    return _tool_executable(name)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--installer', required=True, choices=('pip', 'pipx', 'uv'))
    parser.add_argument('--wheel', required=True, type=wheel_path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        _name, current_version = wheel_identity(args.wheel)
        executable = manager_executable(args.installer)
        with tempfile.TemporaryDirectory(prefix=f'deepseek-team-{args.installer}-') as temporary:
            root = Path(temporary)
            env = isolated_environment(root)
            for key in ('HOME', 'XDG_CONFIG_HOME', 'XDG_CACHE_HOME', 'XDG_DATA_HOME',
                        'XDG_STATE_HOME', 'XDG_RUNTIME_DIR', 'CODEX_HOME',
                        'CLAUDE_CONFIG_DIR', 'PIPX_HOME', 'PIPX_BIN_DIR',
                        'PIPX_MAN_DIR', 'UV_TOOL_DIR', 'UV_TOOL_BIN_DIR',
                        'UV_CACHE_DIR', 'TMPDIR'):
                Path(env[key]).mkdir(parents=True, exist_ok=True)
            Path(env['XDG_RUNTIME_DIR']).chmod(0o700)
            sentinels = create_sentinels(root, env)
            cwd = root / 'project'
            installer = Installer(args.installer, executable, root, env, cwd)
            env['PATH'] = str(installer.bin_dir) + os.pathsep + env.get('PATH', '')

            installer.install(args.wheel, replacement=False)
            verify_install(installer, current_version)
            assert_sentinels(sentinels, 'initial install')

            installer.install(args.wheel, replacement=True)
            verify_install(installer, current_version)
            assert_sentinels(sentinels, 'replacement')

            published = installer.commands
            installer.uninstall()
            for command in published:
                if command.exists() or command.is_symlink():
                    raise CheckError(f'{args.installer} uninstall left entrypoint behind: {command.name}')
            assert_sentinels(sentinels, 'uninstall')

        print(f'PASS: {args.installer} install/replacement/uninstall lifecycle; installed version {current_version}; '
              'command/resources verified and external configuration preserved')
        return 0
    except CheckError as error:
        print(f'installation check failed: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
