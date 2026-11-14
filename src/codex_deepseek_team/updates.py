"""Safely upgrade the installed package and refresh configured coordinator hooks."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import hashlib
import importlib.metadata as metadata
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tomllib


DISTRIBUTION = 'deepseek-team'
MODULE = 'codex_deepseek_team'
SCHEMA_VERSION = 1
EXIT_OK = 0
EXIT_ACTION_REQUIRED = 3
EXIT_FAILURE = 78
MAX_METADATA_BYTES = 256 * 1024
MAX_HOOK_OUTPUT_BYTES = 1024 * 1024
MAX_DIAGNOSTIC_CHARS = 12_000
MAX_INSTALLER_CHARS = 128
_SUPPORTED_PIP_INSTALLERS = frozenset({'pip'})
_RELEASE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9.!+_-]*$')
_NORMALIZE_NAME = re.compile(r'[-_.]+')
_URL_CREDENTIALS = re.compile(r'(?i)(\b[a-z][a-z0-9+.-]*://)[^/\s@]+@')
_SECRET_ASSIGNMENT = re.compile(
    r'(?i)\b(password|passwd|token|secret|api[-_]?key|authorization|credential)\b'
    r'(\s*[:=]\s*)[^\r\n]+')
_SECRET_QUERY = re.compile(
    r'(?i)([?&](?:password|passwd|token|secret|api[-_]?key|credential)=)[^&\s]+')


class UpdateError(RuntimeError):
    def __init__(self, message: str, *, recovery: list[str] | None = None):
        super().__init__(message)
        self.message = message
        self.code = EXIT_FAILURE
        self.recovery = list(recovery or ())


class _ManagerError(UpdateError):
    def __init__(self, message: str, *, changed, exit_code, recovery=None):
        super().__init__(message, recovery=recovery)
        self.changed = changed
        self.exit_code = exit_code


class _HookRefreshError(UpdateError):
    def __init__(self, message: str, *, report=None, recovery=None):
        super().__init__(message, recovery=recovery)
        self.report = report


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        raise UpdateError('Invalid update arguments; use deepseek-team update --help.')


@dataclass(frozen=True)
class Installation:
    manager: str
    executable: str
    interpreter: str
    previous_version: str
    prefix: str = ''


def _parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(
        prog='deepseek-team update',
        description='Upgrade DeepSeek Team with its installation manager, then refresh '
                    'existing coordinator hooks in a fresh process.',
    )
    parser.add_argument('--manager', choices=('auto', 'pip', 'pipx', 'uv'), default='auto',
                        help='Installation manager; auto verifies installation provenance.')
    parser.add_argument('--runtime', choices=('auto', 'codex', 'claude', 'both'), default='auto',
                        help='Coordinator hooks to refresh; default auto uses owned records.')
    parser.add_argument('--version', metavar='VERSION',
                        help='Install an explicit release, for example 0.12.0.')
    parser.add_argument('--json', action='store_true',
                        help='Emit one machine-readable report on stdout.')
    return parser


def _option_value(arguments: list[str], name: str):
    for index, value in enumerate(arguments):
        if value == name and index + 1 < len(arguments):
            return arguments[index + 1]
        if value.startswith(name + '='):
            return value.split('=', 1)[1]
    return None


def validate_release(value):
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 200 or not _RELEASE.fullmatch(value):
        raise UpdateError(
            'Invalid version. Use a plain release such as 0.12.0; option, shell and '
            'command syntax are not accepted.')
    return value


def _normalized_name(value: str) -> str:
    return _NORMALIZE_NAME.sub('-', value).lower()


def _requirement_name(value):
    if isinstance(value, dict):
        for key in ('name', 'requirement', 'package', 'package_or_url'):
            if key in value:
                return _requirement_name(value[key])
        return None
    if not isinstance(value, str):
        return None
    match = re.match(r'^\s*([A-Za-z0-9][A-Za-z0-9._-]*)', value)
    return _normalized_name(match.group(1)) if match else None


def _source_requirement(value) -> bool:
    if not isinstance(value, str):
        return False
    text = value.strip()
    lowered = text.lower()
    if lowered.startswith(('file:', 'git+', 'hg+', 'svn+', 'bzr+', '.', '/', '~')):
        return True
    if '://' in text:
        return True
    if ' @ ' in text:
        target = text.split(' @ ', 1)[1].strip().lower()
        return target.startswith(('.', '/', '~', 'file:')) or ':' in target
    return False


def _read_metadata(path: Path, label: str) -> str:
    try:
        details = path.lstat()
    except FileNotFoundError:
        raise UpdateError(f'{label} is missing: {path}.') from None
    except OSError:
        raise UpdateError(f'Cannot safely inspect {label}: {path}.') from None
    if stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode):
        raise UpdateError(f'{label} must be a regular file, not a link or special file: {path}.')
    if details.st_uid != os.getuid():
        raise UpdateError(f'{label} is not owned by the current user: {path}.')
    if details.st_size > MAX_METADATA_BYTES:
        raise UpdateError(f'{label} is unexpectedly large: {path}.')
    descriptor = -1
    try:
        flags = os.O_RDONLY | getattr(os, 'O_CLOEXEC', 0) | getattr(os, 'O_NOFOLLOW', 0)
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, 'rb') as stream:
            descriptor = -1
            raw = stream.read(MAX_METADATA_BYTES + 1)
    except OSError:
        raise UpdateError(f'Cannot safely read {label}: {path}.') from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if len(raw) > MAX_METADATA_BYTES:
        raise UpdateError(f'{label} is unexpectedly large: {path}.')
    try:
        return raw.decode('utf-8')
    except UnicodeError:
        raise UpdateError(f'{label} is not valid UTF-8: {path}.') from None


def _marker_present(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    except OSError:
        raise UpdateError(f'Cannot safely inspect installation metadata: {path}.') from None
    return True


def _pipx_package_name(path: Path) -> str:
    try:
        data = json.loads(_read_metadata(path, 'pipx metadata'))
    except (TypeError, ValueError):
        raise UpdateError(f'Pipx metadata is malformed: {path}.') from None
    if not isinstance(data, dict):
        raise UpdateError(f'Pipx metadata is malformed: {path}.')
    main = data.get('main_package')
    if isinstance(main, dict):
        source = main.get('package_or_url', main.get('url'))
        if _source_requirement(source):
            raise UpdateError(
                'Unsupported editable or local-source pipx installation. Reinstall DeepSeek '
                'Team from PyPI with pipx before updating; the package was not changed.')
    name = _requirement_name(main) if main is not None else None
    if name is None:
        raise UpdateError(
            'Pipx metadata does not identify the installed package; the update was not started.')
    if name != DISTRIBUTION:
        raise UpdateError(
            'The running pipx environment belongs to another package; refusing to update. '
            'Reinstall DeepSeek Team with pipx and rerun from that installation.')
    return name


def _uv_package_name(path: Path) -> str:
    try:
        data = tomllib.loads(_read_metadata(path, 'uv tool receipt'))
    except (TypeError, ValueError, tomllib.TOMLDecodeError):
        raise UpdateError(f'uv tool receipt is malformed: {path}.') from None
    tool = data.get('tool') if isinstance(data, dict) else None
    if not isinstance(tool, dict):
        raise UpdateError(f'uv tool receipt is malformed: {path}.')
    requirements = tool.get('requirements')
    if isinstance(requirements, dict):
        requirements = [requirements]
    if not isinstance(requirements, list) or not requirements:
        requirements = tool.get('packages')
        if isinstance(requirements, dict):
            requirements = [requirements]
    names = []
    if isinstance(requirements, list):
        for item in requirements:
            if isinstance(item, dict):
                source = item.get('source')
                if (item.get('editable') is True or 'path' in item or 'url' in item
                        or (isinstance(source, dict)
                            and ('path' in source or 'url' in source))):
                    raise UpdateError(
                        'Unsupported editable or local-source uv tool installation. Reinstall '
                        'DeepSeek Team from PyPI with `uv tool install deepseek-team` before '
                        'updating; the package was not changed.')
            if _source_requirement(item):
                raise UpdateError(
                    'Unsupported editable or local-source uv tool installation. Reinstall '
                    'DeepSeek Team from PyPI with `uv tool install deepseek-team` before '
                    'updating; the package was not changed.')
        names = [_requirement_name(item) for item in requirements]
    direct_name = _requirement_name(tool.get('name')) if 'name' in tool else None
    if direct_name is not None:
        names.append(direct_name)
    names = [name for name in names if name is not None]
    if not names:
        raise UpdateError(
            'uv tool receipt does not identify the installed package; the update was not started.')
    if DISTRIBUTION not in names:
        raise UpdateError(
            'The running uv tool environment belongs to another package; refusing to update. '
            'Reinstall DeepSeek Team with `uv tool install deepseek-team` and rerun.')
    return DISTRIBUTION


def _distribution():
    try:
        distribution = metadata.distribution(DISTRIBUTION)
    except metadata.PackageNotFoundError:
        raise UpdateError(
            'Installed deepseek-team distribution metadata is unavailable. Reinstall the '
            'package with pip, pipx or uv before updating.') from None
    except (OSError, ValueError, ImportError):
        raise UpdateError(
            'Installed deepseek-team distribution metadata cannot be read safely; '
            'the package was not changed.') from None
    version = getattr(distribution, 'version', None)
    if not isinstance(version, str) or not version.strip():
        raise UpdateError('Installed deepseek-team distribution has no usable version metadata.')
    return distribution


def _verify_distribution(distribution) -> str:
    package_file = Path(__file__).resolve().with_name('__init__.py')
    try:
        located = Path(distribution.locate_file(f'{MODULE}/__init__.py')).resolve()
    except (OSError, TypeError, ValueError):
        raise UpdateError(
            'Cannot verify that the running module belongs to the installed distribution; '
            'the package was not changed.') from None
    if located != package_file:
        raise UpdateError(
            'The running code does not belong to the installed deepseek-team distribution '
            '(source checkout or stale import). Reinstall the package and rerun from the '
            'installed command; the package was not changed.')
    try:
        direct_url = distribution.read_text('direct_url.json')
    except (OSError, KeyError, TypeError, ValueError):
        raise UpdateError(
            'Cannot safely read installation provenance; the package was not changed.') from None
    if direct_url and direct_url.strip():
        try:
            details = json.loads(direct_url)
        except (TypeError, ValueError):
            raise UpdateError(
                'Installation provenance is malformed; the package was not changed.') from None
        if not isinstance(details, dict):
            raise UpdateError(
                'Installation provenance is malformed; the package was not changed.')
        if details.get('vcs_info'):
            kind = 'VCS or source'
        elif isinstance(details.get('dir_info'), dict):
            kind = 'editable or local-source'
        else:
            kind = 'direct/local-source'
        raise UpdateError(
            f'Unsupported {kind} installation of DeepSeek Team. Reinstall from PyPI with '
            'pipx, `uv tool install deepseek-team`, or pip in a clean virtual environment '
            'before updating; the package was not changed.')
    return distribution.version


def _verify_pip_installer(distribution) -> None:
    try:
        installer = distribution.read_text('INSTALLER')
    except (OSError, KeyError, TypeError, ValueError, UnicodeError):
        installer = None
    if not isinstance(installer, str) or not installer.strip():
        raise UpdateError(
            'Installed deepseek-team has no INSTALLER provenance in its distribution '
            'metadata. Plain pip updates require an installer receipt; reinstall with pip '
            'in a clean virtual environment; the package was not changed.')
    installer = installer.strip()
    if (len(installer) > MAX_INSTALLER_CHARS
            or installer.lower() not in _SUPPORTED_PIP_INSTALLERS):
        raise UpdateError(
            'Installed deepseek-team has unknown or unsupported INSTALLER provenance. '
            'Plain pip updates accept an INSTALLER receipt from pip; reinstall DeepSeek '
            'Team with pip in a clean virtual environment; the package was not changed.')


def _find_manager(name: str, environ) -> str:
    path = environ.get('PATH') if environ is not None else None
    try:
        found = shutil.which(name, path=path)
    except (OSError, TypeError, ValueError):
        found = None
    if not found:
        raise UpdateError(
            f'The {name} installation was detected, but `{name}` is not available on PATH. '
            f'Restore {name} on PATH and rerun; the package was not changed.')
    if not os.path.isabs(found):
        raise UpdateError(
            f'The {name} executable resolved through a relative PATH entry. Use an absolute '
            'PATH entry and rerun; the package was not changed.')
    return found


def detect_installation(manager='auto', *, environ=None, prefix=None, base_prefix=None,
                        executable=None) -> Installation:
    if manager not in ('auto', 'pip', 'pipx', 'uv'):
        raise UpdateError('Invalid manager; use auto, pip, pipx or uv.')
    prefix_text = str(sys.prefix if prefix is None else prefix)
    base_text = str(sys.base_prefix if base_prefix is None else base_prefix)
    interpreter = str(sys.executable if executable is None else executable)
    if not prefix_text or prefix_text == base_text:
        raise UpdateError(
            'Unsupported pip installation: DeepSeek Team is running outside a virtual '
            'environment. Use pipx, `uv tool install deepseek-team`, or pip in a virtual '
            'environment; the package was not changed.')
    if (not os.path.isabs(prefix_text) or not os.path.isabs(interpreter)
            or '\0' in prefix_text or '\0' in interpreter):
        raise UpdateError(
            'The virtual environment or interpreter path is not a safe absolute path; '
            'the package was not changed.')

    root = Path(prefix_text)
    pipx_path = root / 'pipx_metadata.json'
    uv_path = root / 'uv-receipt.toml'
    pipx_present = _marker_present(pipx_path)
    uv_present = _marker_present(uv_path)
    if pipx_present and uv_present:
        raise UpdateError(
            'Ambiguous installation provenance: both pipx and uv tool metadata are present. '
            'Reinstall DeepSeek Team with one manager and rerun; the package was not changed.')
    if pipx_present:
        _pipx_package_name(pipx_path)
        detected = 'pipx'
    elif uv_present:
        _uv_package_name(uv_path)
        detected = 'uv'
    else:
        detected = 'pip'
    if manager != 'auto' and manager != detected:
        raise UpdateError(
            f'Conflicting manager: --manager {manager} was requested, but installation '
            f'provenance is {detected}. Rerun with --manager {detected} or auto; the package '
            'was not changed.')

    distribution = _distribution()
    previous_version = _verify_distribution(distribution)
    if detected == 'pip':
        _verify_pip_installer(distribution)
    if detected == 'pip':
        manager_executable = interpreter
    else:
        manager_executable = _find_manager(detected, environ)
    return Installation(detected, manager_executable, interpreter, previous_version,
                        prefix_text)


def _installation_prefix(installation: Installation) -> Path:
    text = installation.prefix
    if not text:
        text = str(Path(installation.interpreter).parent.parent)
    if not text or not os.path.isabs(text) or '\0' in text:
        raise UpdateError(
            'The virtual environment path is not safe for update locking; '
            'the package was not changed.')
    return Path(os.path.realpath(text))


def _is_within(path: Path, parent: Path) -> bool:
    try:
        return os.path.commonpath((str(path), str(parent))) == str(parent)
    except ValueError:
        return False


def _update_lock_path(installation: Installation) -> Path:
    from . import state_storage

    prefix = _installation_prefix(installation)
    try:
        root = state_storage.state_root()
        lock_directory = root / 'updates'
    except state_storage.StorageError as error:
        raise UpdateError(error.message) from None
    resolved_directory = Path(os.path.realpath(lock_directory))
    if _is_within(resolved_directory, prefix):
        raise UpdateError(
            'The update lock directory resolves inside the virtual environment being '
            'replaced. Set DEEPSEEK_TEAM_STATE_DIR outside that environment; the package '
            'was not changed.')
    key = hashlib.sha256(os.fsencode(str(prefix))).hexdigest()[:32]
    return lock_directory / f'{key}.lock'


def _lock_identity(details) -> tuple[int, int]:
    return details.st_dev, details.st_ino


def _validate_lock_file(details, path: Path) -> None:
    mode = stat.S_IMODE(details.st_mode)
    if stat.S_ISLNK(details.st_mode):
        reason = 'is a symbolic link'
    elif not stat.S_ISREG(details.st_mode):
        reason = 'is not an ordinary file'
    elif details.st_uid != os.geteuid():
        reason = 'is owned by another user'
    elif details.st_nlink != 1:
        reason = 'has multiple hard links'
    elif mode != 0o600:
        reason = f'has unsafe permissions ({mode:o}; expected 600)'
    else:
        return
    raise UpdateError(
        f'The update lock {reason}: {path}. Preserve it for inspection, remove or repair '
        'only the unsafe lock entry, then rerun; the package was not changed.')


def _close_quietly(descriptor) -> None:
    if descriptor is None:
        return
    try:
        os.close(descriptor)
    except OSError:
        pass


@contextmanager
def _update_lock(installation: Installation):
    from . import state_storage

    lock_path = _update_lock_path(installation)
    directory_fd = lock_fd = None
    acquired = False
    try:
        try:
            directory_fd = state_storage.open_private_directory(
                lock_path.parent, create=True, label='Update lock directory')
        except state_storage.StorageError as error:
            raise UpdateError(error.message) from None
        existing = None
        try:
            existing = os.stat(lock_path.name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        if existing is not None:
            _validate_lock_file(existing, lock_path)
        flags = (os.O_CREAT | os.O_RDWR | getattr(os, 'O_CLOEXEC', 0)
                 | getattr(os, 'O_NOFOLLOW', 0))
        try:
            lock_fd = os.open(lock_path.name, flags, 0o600, dir_fd=directory_fd)
        except OSError:
            raise UpdateError(
                f'The update lock could not be opened safely: {lock_path}. Preserve it for '
                'inspection and remove or repair only an unsafe lock entry; the package was '
                'not changed.') from None
        opened = os.fstat(lock_fd)
        _validate_lock_file(opened, lock_path)
        if existing is not None and _lock_identity(existing) != _lock_identity(opened):
            raise UpdateError(
                f'The update lock changed while it was being opened: {lock_path}. '
                'The package was not changed.')
        listed = os.stat(lock_path.name, dir_fd=directory_fd, follow_symlinks=False)
        if _lock_identity(listed) != _lock_identity(opened):
            raise UpdateError(
                f'The update lock changed while it was being opened: {lock_path}. '
                'The package was not changed.')
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise UpdateError(
                'Another deepseek-team update is already running for this installation. '
                'Wait for it to finish and rerun; no package manager was started.') from None
        except OSError:
            raise UpdateError(
                f'The update lock could not be acquired safely: {lock_path}. '
                'The package was not changed.') from None
        listed = os.stat(lock_path.name, dir_fd=directory_fd, follow_symlinks=False)
        if _lock_identity(listed) != _lock_identity(opened):
            raise UpdateError(
                f'The update lock changed after it was acquired: {lock_path}. '
                'The package was not changed.')
        acquired = True
        yield
    except UpdateError:
        raise
    except OSError:
        if acquired:
            raise
        raise UpdateError(
            f'The update lock could not be inspected safely: {lock_path}. '
            'The package was not changed.') from None
    finally:
        _close_quietly(lock_fd)
        _close_quietly(directory_fd)


def manager_command(installation: Installation, release=None) -> list[str]:
    release = validate_release(release)
    package = DISTRIBUTION if release is None else f'{DISTRIBUTION}=={release}'
    if installation.manager == 'pip':
        return [installation.interpreter, '-m', 'pip', 'install', '--upgrade', package]
    if installation.manager == 'pipx':
        if release is None:
            return [installation.executable, 'upgrade', DISTRIBUTION]
        return [installation.executable, 'install', '--force', package]
    if installation.manager == 'uv':
        if release is None:
            return [installation.executable, 'tool', 'upgrade', DISTRIBUTION]
        return [installation.executable, 'tool', 'install', '--force', package]
    raise UpdateError('Unsupported installation manager; the package was not changed.')


def _output_text(value) -> str:
    if value is None:
        return ''
    if isinstance(value, bytes):
        return value.decode('utf-8', errors='replace')
    return str(value)


def _sanitize(text: str) -> str:
    text = text.replace('\x00', '')
    text = _URL_CREDENTIALS.sub(r'\1[redacted]@', text)
    text = _SECRET_ASSIGNMENT.sub(r'\1\2[redacted]', text)
    text = _SECRET_QUERY.sub(r'\1[redacted]', text)
    return text


def _diagnostic(stdout: str, stderr: str) -> str:
    parts = [part.rstrip() for part in (stdout, stderr) if part and part.strip()]
    text = _sanitize('\n'.join(parts))
    if len(text) > MAX_DIAGNOSTIC_CHARS:
        text = '...[earlier output truncated]...\n' + text[-MAX_DIAGNOSTIC_CHARS:]
    return text


def _human_command(command: list[str]) -> str:
    return _sanitize(shlex.join(command))


def _run_manager(installation: Installation, command: list[str]) -> None:
    try:
        completed = subprocess.run(
            command, shell=False, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, errors='replace', check=False)
    except (OSError, subprocess.SubprocessError, ValueError):
        raise _ManagerError(
            f'The {installation.manager} package update could not be started; '
            'the package was not changed.',
            changed=False, exit_code=None, recovery=[_human_command(command)]) from None
    diagnostic = _diagnostic(_output_text(completed.stdout), _output_text(completed.stderr))
    if diagnostic:
        print(diagnostic, file=sys.stderr)
    if completed.returncode != 0:
        code = completed.returncode
        raise _ManagerError(
            f'The {installation.manager} package update failed with exit code {code}; '
            'the manager started, so the package may have been partially changed and its '
            'final state is unknown. Hook refresh was not attempted. Inspect the sanitized '
            'manager diagnostic and the installation, then rerun the update command or '
            'reinstall DeepSeek Team with the reported manager.',
            changed=None, exit_code=code, recovery=[_human_command(command)])


def hook_refresh_command(runtime: str) -> list[str]:
    return [sys.executable, '-m', f'{MODULE}.hook_refresh', '--runtime', runtime,
            '--notify', '--json']


def _refresh_recovery_command(runtime: str) -> str:
    return f'deepseek-team hooks refresh --runtime {runtime} --notify --json'


def _validated_hook_report(completed, runtime: str):
    stdout = _output_text(completed.stdout)
    if stdout.strip():
        if len(stdout) > MAX_HOOK_OUTPUT_BYTES:
            raise _HookRefreshError(
                'The package update succeeded, but the post-upgrade hooks refresh returned '
                'an oversized report. The package changed and hook refresh remains incomplete.',
                recovery=[_refresh_recovery_command(runtime)])
        try:
            report = json.loads(stdout)
        except (TypeError, ValueError):
            report = None
    else:
        report = None
    if report is not None:
        valid = (isinstance(report, dict)
                 and type(report.get('schema_version')) is int
                 and report.get('schema_version') == SCHEMA_VERSION
                 and isinstance(report.get('version'), str) and bool(report['version'])
                 and report.get('status') in ('completed', 'action_required', 'failed')
                 and isinstance(report.get('hooks'), list)
                 and isinstance(report.get('required_actions'), list)
                 and isinstance(report.get('requires_user_action'), bool))
        if valid:
            for action in report['required_actions']:
                valid = (isinstance(action, dict)
                         and all(isinstance(action.get(key), str)
                                 for key in ('code', 'runtime', 'message', 'command')))
                if not valid:
                    break
        if not valid:
            report = None
    if report is None:
        raise _HookRefreshError(
            'The package update succeeded, but the post-upgrade hooks refresh did not return '
            'the required JSON contract. The package changed and hook refresh remains '
            'incomplete.',
            recovery=[_refresh_recovery_command(runtime)])
    if completed.returncode not in (EXIT_OK, EXIT_ACTION_REQUIRED):
        raise _HookRefreshError(
            'The package update succeeded, but the post-upgrade hooks refresh failed. '
            'The package changed and hook refresh remains incomplete.',
            report=report, recovery=[_refresh_recovery_command(runtime)])
    if report['status'] == 'failed':
        raise _HookRefreshError(
            'The package update succeeded, but the post-upgrade hooks refresh reported a '
            'failure. The package changed and hook refresh remains incomplete.',
            report=report, recovery=[_refresh_recovery_command(runtime)])
    if completed.returncode == EXIT_ACTION_REQUIRED and not (
            report['status'] == 'action_required' or report['required_actions']
            or report['requires_user_action']):
        raise _HookRefreshError(
            'The package update succeeded, but the post-upgrade hooks refresh returned an '
            'inconsistent action-required report. The package changed and hook refresh '
            'remains incomplete.',
            report=report, recovery=[_refresh_recovery_command(runtime)])
    return report


def _refresh_hooks(runtime: str):
    command = hook_refresh_command(runtime)
    environment = dict(os.environ)
    environment.pop('PYTHONPATH', None)
    environment.pop('PYTHONHOME', None)
    try:
        completed = subprocess.run(
            command, shell=False, cwd='/', stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            errors='replace', check=False, env=environment)
    except (OSError, subprocess.SubprocessError, ValueError):
        raise _HookRefreshError(
            'The package update succeeded, but the post-upgrade hooks refresh could not be '
            'started. The package changed and hook refresh remains incomplete.',
            recovery=[_refresh_recovery_command(runtime)]) from None
    diagnostic = _diagnostic('', _output_text(completed.stderr))
    if diagnostic:
        print(diagnostic, file=sys.stderr)
    return _validated_hook_report(completed, runtime)


def _recovery_action(runtime: str) -> dict:
    command = _refresh_recovery_command(runtime)
    return {
        'code': 'hook_refresh_incomplete',
        'runtime': runtime,
        'message': (
            'The package update completed, but hook refresh remains incomplete. Run the '
            'recovery command before reporting completion or starting a new session.'),
        'command': command,
    }


def _package_update(previous, current, status, *, changed, manager_exit_code=None):
    return {
        'status': status,
        'changed': None if changed is None else bool(changed),
        'ok': status in ('updated', 'unchanged', 'succeeded'),
        'previous_version': previous,
        'current_version': current,
        'manager_exit_code': manager_exit_code,
    }


def _report(*, manager, previous_version, current_version, status, package_update,
            hooks=None, hook_refresh=None, required_actions=None, requires_user_action=None,
            error=None, message='', recovery_commands=None):
    actions = list(required_actions or ())
    requires = bool(requires_user_action) or bool(actions)
    return {
        'schema_version': SCHEMA_VERSION,
        'version': current_version if current_version is not None else previous_version,
        'status': status,
        'manager': manager,
        'previous_version': previous_version,
        'current_version': current_version,
        'package_update': package_update,
        'hooks': list(hooks or ()),
        'hook_refresh': hook_refresh,
        'required_actions': actions,
        'requires_user_action': requires,
        'error': error,
        'message': message,
        'recovery_commands': list(recovery_commands or ()),
    }


def _failure_report(*, manager, previous_version, current_version, package_update, error,
                    recovery_commands=None, required_actions=None, hooks=None,
                    hook_refresh=None, message=''):
    return _report(
        manager=manager, previous_version=previous_version,
        current_version=current_version, status='failed', package_update=package_update,
        hooks=hooks, hook_refresh=hook_refresh, required_actions=required_actions,
        requires_user_action=bool(required_actions), error=error, message=message,
        recovery_commands=recovery_commands)


def _render(report: dict) -> str:
    lines = ['DeepSeek Team package update', 'Status: ' + report['status']]
    if report.get('manager'):
        lines.append('Manager: ' + report['manager'])
    package = report['package_update']
    previous = report.get('previous_version') or 'unknown'
    current = report.get('current_version') or 'unknown'
    lines.append(f"Package update: {package['status']} ({previous} -> {current})")
    if report.get('error'):
        lines.append('Error: ' + report['error'])
    if report.get('recovery_commands'):
        lines.append('Recovery commands:')
        lines.extend('  ' + command for command in report['recovery_commands'])
    actions = report.get('required_actions') or []
    if actions:
        lines.append('Required user actions:')
        for action in actions:
            lines.append(f"  [{action['runtime']}] {action['code']}: {action['message']}")
            lines.append('    Run: ' + action['command'])
        lines.append('User confirmation remains pending. AI agents must relay every required '
                     'action faithfully in the user\'s language before reporting completion.')
    else:
        lines.append('Required user actions: none.')
    return '\n'.join(lines)


def _emit(report: dict, json_mode: bool) -> None:
    if json_mode:
        print(json.dumps(report, indent=2, sort_keys=True, ensure_ascii=True, allow_nan=False))
    else:
        print(_render(report))


def _run_update(installation: Installation, runtime: str, release, json_mode: bool):
    command = manager_command(installation, release)
    try:
        _run_manager(installation, command)
    except UpdateError as error:
        changed = getattr(error, 'changed', False)
        exit_code = getattr(error, 'exit_code', None)
        current = installation.previous_version if changed is False else None
        report = _failure_report(
            manager=installation.manager, previous_version=installation.previous_version,
            current_version=current,
            package_update=_package_update(
                installation.previous_version, current, 'failed', changed=changed,
                manager_exit_code=exit_code),
            error=error.message, recovery_commands=error.recovery,
            message=(
                'The package manager did not start; the package was not changed and hook '
                'refresh was not attempted.' if changed is False else
                'The package manager started but did not complete successfully; the final '
                'package state is unknown and hook refresh was not attempted.'))
        _emit(report, json_mode)
        return EXIT_FAILURE

    try:
        refresh = _refresh_hooks(runtime)
    except UpdateError as error:
        hook_report = getattr(error, 'report', None)
        actions = list(hook_report.get('required_actions', ())) if hook_report else []
        if not actions:
            actions = [_recovery_action(runtime)]
        hooks = hook_report.get('hooks', ()) if hook_report else ()
        current = hook_report.get('version') if hook_report else None
        changed = current != installation.previous_version if current else True
        report = _failure_report(
            manager=installation.manager, previous_version=installation.previous_version,
            current_version=current,
            package_update=_package_update(
                installation.previous_version, current, 'succeeded', changed=changed,
                manager_exit_code=EXIT_OK),
            error=error.message, required_actions=actions, hooks=hooks,
            hook_refresh=hook_report,
            recovery_commands=error.recovery or [_refresh_recovery_command(runtime)],
            message='The package update completed, but hook refresh remains incomplete.')
        _emit(report, json_mode)
        return EXIT_FAILURE

    current = refresh['version']
    changed = current != installation.previous_version
    actions = refresh['required_actions']
    requires = bool(refresh['requires_user_action'] or actions
                    or refresh['status'] == 'action_required')
    status = 'action_required' if requires else 'completed'
    package_status = 'updated' if changed else 'unchanged'
    report = _report(
        manager=installation.manager, previous_version=installation.previous_version,
        current_version=current, status=status,
        package_update=_package_update(
            installation.previous_version, current, package_status, changed=changed,
            manager_exit_code=EXIT_OK),
        hooks=refresh['hooks'], hook_refresh=refresh, required_actions=actions,
        requires_user_action=requires,
        message=('Package update and hook refresh completed with user actions pending.'
                 if requires else 'Package update and hook refresh completed.'),
    )
    _emit(report, json_mode)
    return EXIT_ACTION_REQUIRED if requires else EXIT_OK


def main(argv=None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0] == 'update':
        arguments = arguments[1:]
    json_mode = '--json' in arguments
    manager_hint = _option_value(arguments, '--manager')
    if manager_hint not in ('pip', 'pipx', 'uv'):
        manager_hint = None
    installation = None
    try:
        args = _parser().parse_args(arguments)
        json_mode = args.json
        release = validate_release(args.version)
        installation = detect_installation(args.manager)
        with _update_lock(installation):
            locked_installation = detect_installation(args.manager)
            if _update_lock_path(locked_installation) != _update_lock_path(installation):
                raise UpdateError(
                    'The target installation changed while acquiring its update lock. '
                    'Rerun the update command; no package manager was started.')
            installation = locked_installation
            return _run_update(installation, args.runtime, release, json_mode)
    except UpdateError as error:
        previous = installation.previous_version if installation is not None else None
        manager = installation.manager if installation is not None else manager_hint
        if manager == 'auto':
            manager = None
        report = _failure_report(
            manager=manager, previous_version=previous, current_version=previous,
            package_update=_package_update(
                previous, previous, 'not_attempted', changed=False),
            error=error.message, recovery_commands=error.recovery,
            message='The package was not changed; the update did not start.')
        _emit(report, json_mode)
        return error.code
    except KeyboardInterrupt:
        print('Cancelled; the package update was not retried.', file=sys.stderr)
        return 130
