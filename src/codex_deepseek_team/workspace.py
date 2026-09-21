"""Owned independent Git copies: no cleaning/adoption of the user's checkout."""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
import time
import uuid

from .settings import git_environment, project_root, SettingsError

SCHEMA = 'deepseek-team-workspace-v1'
DEFAULT_STATE = Path.home() / '.local/state/codex-deepseek'


class WorkspaceError(Exception):
    def __init__(self, message: str, code: int = 78):
        self.code, self.message = code, message
        super().__init__(message)


def _private(path: Path, *, create: bool = False) -> None:
    if create:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
            or info.st_mode & 0o077):
        raise WorkspaceError(f'Workspace control directory must be private and owned: {path}.')


def _read(path: Path) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid():
            raise WorkspaceError(f'Unsafe workspace metadata: {path.name}.')
        return stream.read()


def _atomic(path: Path, content: bytes) -> None:
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def git(root: Path, *args: str, ok=(0,)) -> bytes:
    result = subprocess.run(
        ['git', '--no-pager', '-c', 'core.fsmonitor=false', '-c', 'core.filemode=true',
         '-c', 'core.hooksPath=' + os.devnull, '-c', 'core.attributesFile=' + os.devnull,
         '-c', 'core.ignoreStat=false', '-C', str(root), *args],
        env=git_environment(), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    if result.returncode not in ok:
        raise WorkspaceError('Git preparation/verification failed; the owned copy is retained.')
    return result.stdout


def _git_digest(path: Path) -> str:
    """Protect administrative state; immutable objects are not rehashed each run."""
    metadata = path / '.git'
    if metadata.is_symlink() or not metadata.is_dir():
        raise WorkspaceError('Git metadata is not an ordinary directory; copy retained.', 73)
    digest = hashlib.sha256()
    for directory, dirs, files in os.walk(metadata, followlinks=False):
        dirs[:] = sorted(name for name in dirs if name != 'objects')
        for name in dirs:
            if (Path(directory) / name).is_symlink():
                raise WorkspaceError('Symlinked Git metadata; copy retained.', 73)
        for name in sorted(files):
            item = Path(directory) / name
            digest.update(os.fsencode(str(item.relative_to(metadata))) + b'\0')
            digest.update(_read(item))
    return digest.hexdigest()


def _check_source_tree(path: Path) -> None:
    for entry in git(path, 'ls-tree', '-r', '-z', 'HEAD').split(b'\0'):
        if not entry:
            continue
        header, _, raw_name = entry.partition(b'\t')
        name = Path(os.fsdecode(raw_name))
        if header.startswith(b'160000 '):
            raise WorkspaceError('Preparation: submodules need a coordinator-prepared standalone source copy.')
        lower = name.name.lower()
        secret = (lower in {'.env', 'auth.json', 'api-key', '.npmrc', '.pypirc', '.netrc',
                             '.git-credentials', 'id_rsa', 'id_ed25519'}
                  or (lower.startswith('.env.') and lower not in {'.env.example', '.env.sample', '.env.template'})
                  or name.suffix.lower() in {'.pem', '.key', '.p12', '.pfx', '.jks', '.keystore'}
                  or any(part.lower() in {'.ssh', '.aws', '.azure', '.kube', '.gnupg'} for part in name.parts))
        if secret:
            raise WorkspaceError('Preparation: committed credential-like files cannot be exposed; '
                                 'the coordinator must provide a sanitized source copy.')


class Workspace:
    def __init__(self, directory: Path, metadata: dict):
        self.directory = directory
        self.path = directory / 'work'
        self.id = directory.name
        self.metadata = metadata
        self.source = Path(metadata['source'])
        self._locked = False

    def save(self) -> None:
        _atomic(self.directory / 'record.json',
                (json.dumps(self.metadata, indent=2, ensure_ascii=True) + '\n').encode())

    def verify(self) -> None:
        if (self.path.is_symlink() or not self.path.is_dir()
                or self.path.stat().st_ino != self.metadata.get('work_inode')):
            raise WorkspaceError('Workspace directory was replaced; foreign work is never adopted.', 73)
        if _git_digest(self.path) != self.metadata.get('git_digest'):
            raise WorkspaceError('Worker Git metadata changed; result rejected and copy retained.', 73)
        if git(self.path, 'rev-parse', 'HEAD').strip().decode('ascii') != self.metadata['base_head']:
            raise WorkspaceError('Worker changed its baseline commit; copy retained.', 73)

    @contextmanager
    def lock(self, *, recover: bool = False):
        fd = os.open(self.directory / 'owner.lock',
                     os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or
                    info.st_uid != os.geteuid() or info.st_mode & 0o077):
                raise WorkspaceError('Unsafe workspace ownership lock.')
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise WorkspaceError('This copy already has an active owner; choose another copy.', 75) from None
            fresh = load(self.directory.parent.parent, self.id)
            self.metadata = fresh.metadata
            self.verify()
            if self.metadata['status'] not in ('ready', 'succeeded') and not recover:
                raise WorkspaceError('Copy has failed/interrupted or partial work. Inspect workspace show/diff, '
                                     'then explicitly use --resume-after-failure; no automatic retry.')
            self._locked = True
            yield self
        finally:
            self._locked = False
            os.close(fd)

    def begin(self, kind: str) -> None:
        if not self._locked:
            raise WorkspaceError('An exclusive workspace lock is required.')
        self.metadata.update(status='running', operation=kind,
                             attempt=self.metadata.get('attempt', 0) + 1,
                             started_at=time.time(), error_kind=None, exit_code=None)
        self.save()

    def changes(self) -> tuple[list[str], int]:
        self.verify()
        names = git(self.path, 'diff', '--no-ext-diff', '--no-textconv', '--no-renames',
                    '--name-only', '-z', 'HEAD', '--')
        names += git(self.path, 'ls-files', '--others', '--exclude-standard', '-z')
        ignored = git(self.path, 'ls-files', '--others', '--ignored', '--exclude-standard', '-z')
        return sorted({os.fsdecode(n) for n in names.split(b'\0') if n}), sum(bool(n) for n in ignored.split(b'\0'))

    def diff(self) -> str:
        self.verify()
        patch = git(self.path, 'diff', '--binary', '--full-index', '--no-ext-diff',
                    '--no-textconv', '--no-renames', 'HEAD', '--')
        for raw_name in git(self.path, 'ls-files', '--others', '--exclude-standard', '-z').split(b'\0'):
            if raw_name:
                patch += git(self.path, 'diff', '--no-index', '--binary', '--no-ext-diff',
                             '--no-textconv', '--', '/dev/null', os.fsdecode(raw_name), ok=(0, 1))
        return patch.decode('utf-8', errors='replace')

    def finish(self, status: str, *, error_kind: str | None = None, exit_code: int = 0) -> dict:
        if not self._locked:
            raise WorkspaceError('An exclusive workspace lock is required.')
        names, ignored = self.changes()
        self.metadata.update(status=status, error_kind=error_kind, exit_code=exit_code,
                             changed_files=names, ignored_artifacts=ignored,
                             finished_at=time.time())
        self.save()
        _atomic(self.directory / 'changes.patch', self.diff().encode())
        return dict(self.metadata)

    def failed(self, kind: str, exit_code: int) -> None:
        """Record a failure even when Git verification prevents a safe diff."""
        self.metadata.update(status='failed', error_kind=kind, exit_code=exit_code,
                             finished_at=time.time())
        self.save()


def content_snapshot(copy: Workspace) -> dict[str, str]:
    """Hash visible tracked/untracked project content for worker attribution."""
    copy.verify()
    names = git(copy.path, 'ls-files', '--cached', '--others', '--exclude-standard', '-z')
    result = {}
    for raw in names.split(b'\0'):
        if not raw:
            continue
        name = os.fsdecode(raw)
        path = copy.path / name
        digest = hashlib.sha256()
        try:
            info = path.lstat()
        except FileNotFoundError:
            result[name] = 'missing'
            continue
        digest.update(str(stat.S_IMODE(info.st_mode)).encode() + b'\0')
        if path.is_symlink():
            digest.update(b'L' + os.fsencode(os.readlink(path)))
        elif path.is_file():
            digest.update(b'F')
            with path.open('rb') as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                    digest.update(chunk)
        else:
            digest.update(b'O')
        result[name] = digest.hexdigest()
    return result


def changed_since(copy: Workspace, before: dict[str, str]) -> list[str]:
    after = content_snapshot(copy)
    return sorted(name for name in set(before) | set(after)
                  if before.get(name) != after.get(name))


def create(source: Path, state: Path = DEFAULT_STATE) -> Workspace:
    try:
        source = project_root(source, required=True)
    except SettingsError as error:
        raise WorkspaceError(f'Preparation: {error}') from None
    state = Path(state).absolute()
    _private(state, create=True)
    root = state / 'workspaces'
    _private(root, create=True)
    directory = root / uuid.uuid4().hex
    directory.mkdir(mode=0o700)
    work = directory / 'work'
    work.mkdir(mode=0o700)
    metadata = {'schema': SCHEMA, 'source': str(source), 'status': 'preparing',
                'attempt': 0, 'created_at': time.time(), 'work_inode': work.stat().st_ino}
    copy = Workspace(directory, metadata)
    copy.save()
    try:
        head = git(source, 'rev-parse', '--verify', 'HEAD').strip().decode('ascii')
        if not re.fullmatch(r'[0-9a-f]{40,64}', head):
            raise WorkspaceError('Preparation requires a committed source HEAD.')
        git(work, 'init', '-q', '--template=', '-b', 'deepseek/' + copy.id)
        git(work, 'fetch', '-q', '--depth=1', '--no-tags', '--no-recurse-submodules',
            '--', str(source), head)
        git(work, 'reset', '--soft', 'FETCH_HEAD')  # only this newly-created, owned repository
        _check_source_tree(work)
        git(work, 'reset', '--hard', 'HEAD')       # never used when reopening a copy
        (work / '.git/FETCH_HEAD').unlink(missing_ok=True)
        metadata.update(base_head=head, git_digest=_git_digest(work), status='ready',
                        changed_files=[], ignored_artifacts=0)
        copy.save()
        return copy
    except (WorkspaceError, OSError, UnicodeError) as error:
        copy.failed('preparation', 78)
        message = str(error) if isinstance(error, WorkspaceError) else 'Cannot create the private Git copy.'
        raise WorkspaceError(f'{message} Retained workspace {copy.id}: {work}') from None


def load(state: Path, identifier: str) -> Workspace:
    if not re.fullmatch(r'[0-9a-f]{32}', identifier):
        raise WorkspaceError('--workspace must be an owned workspace ID, never a foreign directory.')
    state = Path(state).absolute()
    directory = state / 'workspaces' / identifier
    try:
        for path in (state, state / 'workspaces', directory):
            _private(path)
        metadata = json.loads(_read(directory / 'record.json'))
        if (not isinstance(metadata, dict) or metadata.get('schema') != SCHEMA
                or not isinstance(metadata.get('source'), str)
                or not Path(metadata['source']).is_absolute()):
            raise ValueError('invalid marker')
        copy = Workspace(directory, metadata)
        if copy.path.is_symlink() or not copy.path.is_dir() or copy.path.stat().st_ino != metadata['work_inode']:
            raise ValueError('replaced copy')
        return copy
    except (OSError, ValueError, KeyError, TypeError):
        raise WorkspaceError('Workspace is missing, unsafe or foreign; no files were changed.') from None


def _credential_like(name: Path) -> bool:
    lower = name.name.lower()
    return (lower in {'.env', 'auth.json', 'api-key', '.npmrc', '.pypirc', '.netrc',
                      '.git-credentials', 'id_rsa', 'id_ed25519'}
            or (lower.startswith('.env.') and lower not in {'.env.example', '.env.sample', '.env.template'})
            or name.suffix.lower() in {'.pem', '.key', '.p12', '.pfx', '.jks', '.keystore'}
            or any(part.lower() in {'.ssh', '.aws', '.azure', '.kube', '.gnupg'} for part in name.parts))


def import_paths(copy: Workspace, paths: list[str]) -> list[str]:
    """Explicitly copy selected source files into an owned workspace as coordinator preparation."""
    if not paths:
        raise WorkspaceError('workspace import requires at least one --include FILE.', 64)
    imported = []
    with copy.lock():
        source_root = copy.source.resolve()
        work_root = copy.path.resolve()
        for value in paths:
            rel = Path(value)
            if rel.is_absolute() or '..' in rel.parts or not rel.parts:
                raise WorkspaceError('Imported paths must be repository-relative ordinary files.', 64)
            if _credential_like(rel):
                raise WorkspaceError(f'Refusing credential-like source path: {rel}.', 78)
            source = source_root / rel
            try:
                info = source.lstat()
            except FileNotFoundError:
                raise WorkspaceError(f'Selected source path does not exist: {rel}.', 66) from None
            if source.is_symlink() or not stat.S_ISREG(info.st_mode):
                raise WorkspaceError(f'Selected source path must be an ordinary non-symlink file: {rel}.', 78)
            destination = work_root / rel
            parent = destination.parent
            parent.mkdir(parents=True, exist_ok=True)
            if not parent.resolve().is_relative_to(work_root):
                raise WorkspaceError('Imported path escaped the owned workspace.', 78)
            shutil.copyfile(source, destination)
            os.chmod(destination, stat.S_IMODE(info.st_mode))
            imported.append(rel.as_posix())
        copy.metadata['prepared_paths'] = sorted(set(copy.metadata.get('prepared_paths', [])) | set(imported))
        copy.save()
    return sorted(imported)


def prepare(copy: Workspace, command: list[str], *, recover: bool = False) -> int:
    """An explicit coordinator action; never load executable hooks from settings."""
    if not command:
        raise WorkspaceError('workspace prepare requires -- COMMAND [ARG ...].', 64)
    with copy.lock(recover=recover):
        copy.begin('preparation')
        home = copy.directory / 'preparation-home'
        _private(home, create=True)
        env = git_environment()
        env.update(HOME=str(home), PIP_NO_INPUT='1')
        try:
            result = subprocess.run(command, cwd=copy.path, env=env, check=False)
            copy.finish('succeeded' if result.returncode == 0 else 'failed',
                        error_kind=None if result.returncode == 0 else 'preparation',
                        exit_code=result.returncode)
            return result.returncode
        except BaseException:
            copy.failed('preparation', 78)
            raise
