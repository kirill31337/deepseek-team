"""Linux OS sandbox support for DeepSeek worker subprocesses."""
from __future__ import annotations

from dataclasses import dataclass, field
from importlib import resources
import os
from pathlib import Path
import shlex
import shutil
import stat
import subprocess
import tempfile
from typing import Callable, Iterable


PROFILE_NAME = 'deepseek-team-bwrap'
PROFILE_TARGET = Path('/etc/apparmor.d') / PROFILE_NAME
RESTRICTION_PATH = Path('/proc/sys/kernel/apparmor_restrict_unprivileged_userns')
_REQUIRED_BWRAP_FLAGS = frozenset({
    '--die-with-parent', '--new-session', '--unshare-user', '--unshare-pid',
    '--unshare-ipc', '--unshare-uts', '--disable-userns', '--cap-drop',
    '--ro-bind', '--bind', '--proc', '--dev', '--tmpfs', '--chdir',
})
_CREDENTIAL_DIRS = (
    '.ssh', '.gnupg', '.aws', '.azure', '.kube', '.docker', '.codex', '.claude',
    '.local/share/keyrings',
)
_CREDENTIAL_FILES = ('.netrc', '.git-credentials', '.npmrc', '.pypirc')


class SandboxError(Exception):
    def __init__(self, code: int, message: str):
        self.code, self.message = code, message
        super().__init__(message)


@dataclass(frozen=True)
class SandboxBackend:
    prefix: tuple[str, ...]
    bwrap: str
    source: str
    features: frozenset[str] = field(default_factory=frozenset)


def apparmor_restriction(path: Path = RESTRICTION_PATH) -> int | None:
    try:
        value = path.read_text(encoding='ascii').strip()
        return int(value)
    except (OSError, ValueError):
        return None


def _run_probe(runner, args: Iterable[str], timeout: float = 10.0):
    try:
        return runner(list(args), text=True, capture_output=True, timeout=timeout,
                      check=False)
    except (OSError, subprocess.SubprocessError):
        return None


def _probe_command(bwrap: str) -> list[str]:
    return [
        bwrap,
        '--die-with-parent', '--new-session',
        '--unshare-user', '--unshare-pid', '--unshare-ipc', '--unshare-uts',
        '--disable-userns', '--cap-drop', 'ALL',
        '--ro-bind', '/', '/', '--proc', '/proc', '--dev', '/dev',
        '--', '/usr/bin/true',
    ]


def probe_backend(*, which=shutil.which, runner=subprocess.run,
                  restriction_reader=apparmor_restriction) -> SandboxBackend:
    """Select a working bwrap path without weakening host userns policy."""
    bwrap = which('bwrap')
    if not bwrap:
        raise SandboxError(
            78,
            'Bubblewrap worker isolation is required but bwrap is unavailable. '
            'Install bubblewrap; on Ubuntu use `python3 install.py --with-sandbox`.'
        )
    help_result = _run_probe(runner, [bwrap, '--help'], timeout=5)
    if help_result is None or help_result.returncode != 0:
        raise SandboxError(78, 'Bubblewrap could not report its capabilities.')
    help_text = (help_result.stdout or '') + '\n' + (help_result.stderr or '')
    missing = sorted(flag for flag in _REQUIRED_BWRAP_FLAGS if flag not in help_text)
    if missing:
        raise SandboxError(
            78,
            'Bubblewrap lacks required isolation options: ' + ', '.join(missing) +
            '. Upgrade bubblewrap before running workers.'
        )
    features = frozenset(flag for flag in ('--unshare-cgroup-try', '--assert-userns-disabled')
                         if flag in help_text)
    probe = _probe_command(bwrap)
    direct = _run_probe(runner, probe)
    if direct is not None and direct.returncode == 0:
        return SandboxBackend((bwrap,), bwrap, 'direct', features)

    restriction = restriction_reader()
    aa_exec = which('aa-exec') if restriction == 1 else None
    if aa_exec:
        prefix = (aa_exec, '-p', PROFILE_NAME, '--', bwrap)
        wrapped_probe = [*prefix[:-1], *probe]
        apparmor = _run_probe(runner, wrapped_probe)
        if apparmor is not None and apparmor.returncode == 0:
            return SandboxBackend(prefix, bwrap, 'apparmor', features)

    if restriction == 1:
        raise SandboxError(
            78,
            'Bubblewrap is blocked by Ubuntu AppArmor user-namespace policy and the '
            f'{PROFILE_NAME} fallback is unavailable. Run '
            '`deepseek-team sandbox install-apparmor` (or `python3 install.py '
            '--with-sandbox`). Do not disable kernel.apparmor_restrict_unprivileged_userns globally.'
        )
    raise SandboxError(
        78,
        'Bubblewrap could not create the required worker namespace. Check the kernel/userns '
        'policy and run `deepseek-team sandbox status`; unsandboxed fallback is not automatic.'
    )


def prepare_codex_environment(session_home: Path, env: dict[str, str],
                              backend: SandboxBackend) -> dict[str, str]:
    """Force Codex's native Linux sandbox to use exactly the probed bwrap path.

    Codex itself creates the Bubblewrap namespace. Nesting Codex inside another
    user namespace would break that native sandbox on current Linux builds, so we
    put a private `bwrap` shim first on PATH instead. On Ubuntu-restricted hosts
    the shim applies the package named AppArmor profile with aa-exec. The shim
    also injects ``--disable-userns`` once, preventing payload processes from
    creating additional user namespaces after Codex establishes its sandbox.
    """
    session_home = Path(session_home)
    wrapper_dir = session_home / '.deepseek-team-bwrap-bin'
    wrapper = wrapper_dir / 'bwrap'
    try:
        wrapper_dir.mkdir(mode=0o700, parents=False, exist_ok=False)
        command = ' '.join(shlex.quote(part) for part in backend.prefix)
        payload = (
            '#!/bin/sh\n'
            'for arg do\n'
            '  if [ "$arg" = "--disable-userns" ]; then\n'
            '    exec ' + command + ' "$@"\n'
            '  fi\n'
            'done\n'
            'exec ' + command + ' --disable-userns "$@"\n'
        )
        fd = os.open(wrapper, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o700)
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as target:
            target.write(payload)
            target.flush()
            os.fsync(target.fileno())
        info = wrapper.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or
                info.st_uid != os.geteuid() or info.st_mode & 0o077):
            raise OSError('unsafe private bwrap wrapper')
    except OSError:
        raise SandboxError(78, 'Could not create the private Codex bwrap wrapper.') from None
    result = dict(env)
    result['PATH'] = str(wrapper_dir) + os.pathsep + result.get('PATH', '')
    return result


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return path != root
    except ValueError:
        return False


def _runtime_roots(command: list[str], env: dict[str, str], real_home: Path,
                   existing: Callable[[Path], bool]) -> list[Path]:
    candidates: list[Path] = []
    if command:
        executable = Path(command[0])
        if executable.is_absolute():
            candidates.append(executable)
            try:
                candidates.append(Path(os.path.realpath(executable)))
            except OSError:
                pass
    for entry in env.get('PATH', '').split(os.pathsep):
        if entry:
            path = Path(entry)
            if path.is_absolute():
                candidates.append(path)
    for name in ('SSL_CERT_FILE', 'SSL_CERT_DIR'):
        value = env.get(name)
        if value:
            path = Path(value)
            if path.is_absolute():
                candidates.append(path)

    roots: list[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        lexical = Path(os.path.normpath(str(candidate)))
        if not _inside(lexical, real_home):
            continue
        relative = lexical.relative_to(real_home)
        if not relative.parts:
            continue
        root = real_home / relative.parts[0]
        if root in seen or not existing(root):
            continue
        seen.add(root)
        roots.append(root)
    return roots


def wrap_command(command: list[str], *, cwd: Path, session_home: Path,
                 env: dict[str, str], backend: SandboxBackend,
                 real_home: Path | None = None,
                 existing: Callable[[Path], bool] | None = None,
                 is_dir: Callable[[Path], bool] | None = None) -> list[str]:
    """Wrap a Claude Code worker in a minimal bwrap mount/process sandbox."""
    if not command:
        raise SandboxError(64, 'Cannot sandbox an empty worker command.')
    existing = existing or (lambda path: path.exists())
    is_dir = is_dir or (lambda path: path.is_dir())
    cwd = Path(os.path.abspath(cwd))
    session_home = Path(os.path.abspath(session_home))
    real_home = Path(os.path.abspath(real_home or Path.home()))
    if cwd == real_home:
        raise SandboxError(78, 'Refusing to expose the entire real HOME as a worker checkout.')

    args = [
        *backend.prefix,
        '--die-with-parent', '--new-session',
        '--unshare-user', '--unshare-pid', '--unshare-ipc', '--unshare-uts',
    ]
    if '--unshare-cgroup-try' in backend.features:
        args.append('--unshare-cgroup-try')
    args += ['--disable-userns']
    if '--assert-userns-disabled' in backend.features:
        args.append('--assert-userns-disabled')
    args += [
        '--cap-drop', 'ALL',
        '--ro-bind', '/', '/',
        '--proc', '/proc', '--dev', '/dev',
    ]

    # Bubblewrap resolves bind sources from the preserved host /oldroot. This lets
    # us hide HOME first and then expose only runtime roots and the worker checkout.
    if existing(real_home) and is_dir(real_home):
        args += ['--tmpfs', str(real_home)]
    runtime_roots = _runtime_roots(command, env, real_home, existing)
    for root in runtime_roots:
        args += ['--ro-bind', str(root), str(root)]

    # Mask common credential stores after runtime roots so a broad runtime tree
    # such as ~/.local cannot accidentally expose a nested keyring.
    masks = [real_home / name for name in _CREDENTIAL_DIRS]
    config = real_home / '.config'
    if not any(root == config for root in runtime_roots):
        masks.append(config)
    for path in masks:
        if not existing(path) or path in (cwd, session_home):
            continue
        if is_dir(path):
            args += ['--tmpfs', str(path)]
        else:
            args += ['--ro-bind', '/dev/null', str(path)]
    for name in _CREDENTIAL_FILES:
        path = real_home / name
        if existing(path) and path not in (cwd, session_home):
            args += ['--ro-bind', '/dev/null', str(path)]

    # Temporary worker HOME is the only writable home state visible to the CLI.
    args += ['--bind', str(session_home), str(session_home)]
    args += ['--tmpfs', '/tmp']
    if existing(Path('/var/tmp')):
        args += ['--tmpfs', '/var/tmp']

    args += ['--ro-bind', str(cwd), str(cwd), '--chdir', str(cwd), '--', *command]
    return args


def profile_bytes() -> bytes:
    return resources.files('codex_deepseek_team').joinpath(
        'data/apparmor/deepseek-team-bwrap').read_bytes()


def _privileged(command: list[str], use_sudo: bool) -> list[str]:
    if use_sudo and os.geteuid() != 0:
        sudo = shutil.which('sudo')
        if not sudo:
            raise SandboxError(78, 'sudo is required to manage the system AppArmor profile.')
        return [sudo, *command]
    return command


def _system_run(command: list[str], *, use_sudo: bool, runner=subprocess.run) -> None:
    try:
        result = runner(_privileged(command, use_sudo), text=True,
                        capture_output=True, timeout=30, check=False)
    except (OSError, subprocess.SubprocessError):
        raise SandboxError(78, 'AppArmor profile operation could not complete.') from None
    if result.returncode != 0:
        raise SandboxError(78, 'AppArmor profile operation failed; system output was omitted.')


def install_apparmor(*, use_sudo: bool = True, runner=subprocess.run,
                     target: Path = PROFILE_TARGET) -> bool:
    """Install/load only the exact package-owned named profile."""
    expected = profile_bytes()
    if target.is_symlink() or (target.exists() and not target.is_file()):
        raise SandboxError(78, f'Refusing unsafe AppArmor profile target: {target}.')
    if target.exists():
        try:
            current = target.read_bytes()
        except OSError:
            raise SandboxError(78, f'Cannot inspect existing AppArmor profile: {target}.') from None
        if current != expected:
            raise SandboxError(
                78, f'Refusing to overwrite administrator/foreign AppArmor profile: {target}.'
            )
        changed = False
    else:
        changed = True
        with tempfile.NamedTemporaryFile(prefix='deepseek-team-apparmor-', delete=False) as handle:
            handle.write(expected)
            handle.flush()
            os.fsync(handle.fileno())
            temporary = Path(handle.name)
        try:
            _system_run(['install', '-o', 'root', '-g', 'root', '-m', '0644',
                         str(temporary), str(target)], use_sudo=use_sudo, runner=runner)
        finally:
            try:
                temporary.unlink()
            except OSError:
                pass
    parser = shutil.which('apparmor_parser') or '/usr/sbin/apparmor_parser'
    _system_run([parser, '-r', str(target)], use_sudo=use_sudo, runner=runner)
    return changed


def remove_apparmor(*, use_sudo: bool = True, runner=subprocess.run,
                    target: Path = PROFILE_TARGET) -> bool:
    """Unload/remove only an unchanged package-owned profile."""
    if not target.exists() and not target.is_symlink():
        return False
    if target.is_symlink() or not target.is_file():
        raise SandboxError(78, f'Refusing unsafe AppArmor profile target: {target}.')
    try:
        current = target.read_bytes()
    except OSError:
        raise SandboxError(78, f'Cannot inspect existing AppArmor profile: {target}.') from None
    if current != profile_bytes():
        raise SandboxError(
            78, f'Refusing to remove administrator-modified AppArmor profile: {target}.'
        )
    parser = shutil.which('apparmor_parser') or '/usr/sbin/apparmor_parser'
    _system_run([parser, '-R', str(target)], use_sudo=use_sudo, runner=runner)
    _system_run(['rm', '-f', '--', str(target)], use_sudo=use_sudo, runner=runner)
    return True
