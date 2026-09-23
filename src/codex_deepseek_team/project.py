"""Idempotent managed blocks for Codex and Claude Code project guidance.

The module writes delegation guidance into a clearly marked, unique block in the
coordinator-native root instruction file. Every byte outside the owned region is
preserved verbatim: line endings are not normalized and user text is not dropped.
"""
import os
from pathlib import Path
import stat
import subprocess
import tempfile

from .config import sync_directory


START_MARKER = b"<!-- codex-deepseek-team:managed-block:start -->"
END_MARKER = b"<!-- codex-deepseek-team:managed-block:end -->"
DATA_FILE = Path(__file__).resolve().parent / "data" / "delegation.md"
DEFAULT_MODE = 0o644
TARGETS = {'codex': 'AGENTS.md', 'claude': 'CLAUDE.md'}


class ProjectError(Exception):
    """Raised for an invalid or ambiguous target; existing content is untouched."""


def _guidance(runtime='codex', root=None, policy=None):
    try:
        body = DATA_FILE.read_bytes()
    except OSError as error:
        raise ProjectError("packaged delegation guidance is unavailable") from error
    from . import settings
    if policy is None:
        try:
            policy = settings.resolve(root)
        except settings.SettingsError as error:
            raise ProjectError(str(error)) from None
    dynamic = settings.instructions(policy, runtime).encode('utf-8')
    return body.replace(b'{runtime}', runtime.encode('ascii')).strip(b"\n") + b'\n\n' + dynamic


def _block_bytes(state, runtime='codex', root=None, policy=None):
    """Render the owned block; a supplied candidate policy skips settings lookup."""
    metadata = b"<!-- codex-deepseek-team:original:" + state + b" -->\n"
    return START_MARKER + b"\n" + metadata + _guidance(runtime, root, policy) + b"\n" + END_MARKER + b"\n"


def _repository_root(root):
    root = Path(root)
    if not root.is_dir():
        raise ProjectError("target must be an existing directory")
    try:
        result = subprocess.run(
            ["git", "-C", os.fspath(root), "rev-parse", "--show-toplevel"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except OSError as error:
        raise ProjectError("cannot run git to verify the repository root") from error
    if result.returncode:
        raise ProjectError("target is not inside a Git repository")
    toplevel = os.fsdecode(result.stdout).strip()
    if not toplevel:
        raise ProjectError("git did not report a repository root")
    if Path(toplevel).resolve() != root.resolve():
        raise ProjectError("target must be the repository root, not a subdirectory")
    return root.resolve()


def _read_agents(target):
    try:
        descriptor = os.open(target, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None, None
    except OSError as error:
        raise ProjectError(f"cannot safely open {target.name}; symbolic links are refused") from error
    info = os.fstat(descriptor)
    if not stat.S_ISREG(info.st_mode):
        os.close(descriptor)
        raise ProjectError(f"{target.name} must be an ordinary file")
    with os.fdopen(descriptor, "rb") as handle:
        return handle.read(), stat.S_IMODE(info.st_mode)


def _locate(content):
    starts = content.count(START_MARKER)
    ends = content.count(END_MARKER)
    if not starts and not ends:
        return None
    if starts != 1 or ends != 1:
        raise ProjectError("instruction file has duplicate or unbalanced managed-block markers")
    begin = content.find(START_MARKER)
    finish = content.find(END_MARKER)
    if finish < begin:
        raise ProjectError("instruction file has out-of-order managed-block markers")
    return begin, finish


def _original_state(content, begin):
    header = content[begin + len(START_MARKER):].split(b"\n", 2)
    if len(header) == 3 and header[0] == b"":
        for state in (b"created", b"existing-empty", b"existing-content"):
            if header[1] == b"<!-- codex-deepseek-team:original:" + state + b" -->":
                return state
    raise ProjectError("managed-block ownership metadata is missing or modified")


def _owned_span(content, begin, finish, state):
    end = finish + len(END_MARKER)
    if content[end:end + 1] != b"\n":
        raise ProjectError("managed-block end separator is missing or modified")
    end += 1
    if state == b"existing-content":
        if not begin or content[begin - 1:begin] != b"\n":
            raise ProjectError("managed-block start separator is missing or modified")
        return begin - 1, end
    return begin, end


def _write_atomic(target, original, mode, content):
    descriptor, temporary = tempfile.mkstemp(
        dir=os.fspath(target.parent), prefix=f".{target.name}.", suffix=".tmp")
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = None
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if _read_agents(target)[0] != original:
            raise ProjectError(f"{target.name} changed while it was being updated; retry")
        os.replace(temporary, target)
        temporary = None
        sync_directory(target.parent)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary is not None:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


def _coordinators(value):
    if value == 'both':
        return ('codex', 'claude')
    if value in TARGETS:
        return (value,)
    raise ProjectError("coordinator must be codex, claude or both")


def _prepare_attach_from(target, original, mode, runtime, repository, policy=None):
    """Pure render of one target's post-attach bytes; validates the owned block."""
    content = original if original is not None else b""
    marks = _locate(content)
    if marks is None:
        state = b"created" if original is None else (b"existing-content" if original else b"existing-empty")
        block = _block_bytes(state, runtime, repository, policy)
        updated = content + (b"\n" if content else b"") + block
    else:
        begin, finish = marks
        state = _original_state(content, begin)
        block = _block_bytes(state, runtime, repository, policy)
        updated = content[:begin] + block + content[_owned_span(content, begin, finish, state)[1]:]
    return target, original, mode, updated


def _prepare_attach(repository, runtime, policy=None):
    target = repository / TARGETS[runtime]
    original, mode = _read_agents(target)
    return _prepare_attach_from(target, original, mode, runtime, repository, policy)


def _prepare_detach(repository, runtime):
    target = repository / TARGETS[runtime]
    original, mode = _read_agents(target)
    if original is None:
        return target, None, mode, None, False
    marks = _locate(original)
    if marks is None:
        return target, original, mode, original, False
    begin, finish = marks
    state = _original_state(original, begin)
    start, end = _owned_span(original, begin, finish, state)
    updated = original[:start] + original[end:]
    remove = not updated and state == b"created"
    return target, original, mode, updated, remove


def is_attached(root, runtime='codex'):
    """Return whether this package owns a managed block for the coordinator."""
    if runtime not in TARGETS:
        raise ProjectError("coordinator must be codex or claude")
    repository = _repository_root(root)
    content, _mode = _read_agents(repository / TARGETS[runtime])
    if content is None:
        return False
    marks = _locate(content)
    if marks is None:
        return False
    begin, finish = marks
    _original_state(content, begin)
    return finish > begin


def attach(root, coordinator='codex', *, policy=None):
    """Add or refresh managed blocks; return True when any target changed."""
    runtimes = _coordinators(coordinator)
    repository = _repository_root(root)
    prepared = [_prepare_attach(repository, runtime, policy) for runtime in runtimes]
    changed = False
    for target, original, mode, updated in prepared:
        if original is not None and updated == original:
            continue
        _write_atomic(target, original, mode if mode is not None else DEFAULT_MODE, updated)
        changed = True
    return changed


def _prepare_owned_refresh(repository, runtime, policy):
    """Prepare an in-place update for a block this package owns, else None."""
    target = repository / TARGETS[runtime]
    original, mode = _read_agents(target)
    if not original or _locate(original) is None:
        return None
    return _prepare_attach_from(target, original, mode, runtime, repository, policy)


def prepare_refresh(root, policy, coordinator='both'):
    """Validate and render refreshes for managed blocks that already exist.

    Every target is read and validated before the caller writes anything, so a
    symlink, a non-regular file or a malformed owned block in either runtime file
    aborts the whole refresh. Missing files and files without a managed block are
    skipped and never created. Returns atomic (target, original, mode, updated)
    tuples in runtime order.
    """
    runtimes = _coordinators(coordinator)
    repository = _repository_root(root)
    prepared = []
    for runtime in runtimes:
        item = _prepare_owned_refresh(repository, runtime, policy)
        if item is not None:
            prepared.append(item)
    return prepared


def rollback_refresh(written):
    """Restore pre-images only for files still byte-identical to what we wrote.

    A file that already matches its pre-image needs no undo. A file whose content
    is no longer exactly what this operation wrote was changed externally and is
    never clobbered. Returns the names of files that could not be restored.
    """
    blocked = []
    for target, original, mode, updated in reversed(written):
        try:
            current, current_mode = _read_agents(target)
        except ProjectError:
            blocked.append(target.name)
            continue
        if current == original:
            continue
        if current != updated or current_mode != mode:
            blocked.append(target.name)
            continue
        try:
            if original is None:
                os.unlink(target)
                sync_directory(target.parent)
            else:
                _write_atomic(target, updated, mode if mode is not None else DEFAULT_MODE, original)
        except (OSError, ProjectError):
            blocked.append(target.name)
    return blocked


def apply_refresh(prepared):
    """Write prepared refreshes in order; on failure roll back this call's writes.

    Returns the items actually written so a caller can undo them when a later step
    of its transaction fails before the commit point. A target whose write failed
    is still offered for rollback when its bytes landed before the failure.
    """
    written = []
    pending = None
    try:
        for item in prepared:
            target, original, mode, updated = item
            if original is not None and updated == original:
                continue
            pending = item
            _write_atomic(target, original, mode if mode is not None else DEFAULT_MODE, updated)
            written.append(item)
            pending = None
    except BaseException as error:
        candidates = written + ([pending] if pending is not None else [])
        blocked = rollback_refresh(candidates)
        message = str(error)
        if blocked:
            message += '; could not roll back: ' + ', '.join(blocked)
        if isinstance(error, (ProjectError, OSError)):
            raise ProjectError(message) from error
        if blocked:
            error.add_note('Could not roll back: ' + ', '.join(blocked))
        raise
    return written


def detach(root, coordinator='codex'):
    """Remove managed blocks; return True when any target changed."""
    runtimes = _coordinators(coordinator)
    repository = _repository_root(root)
    prepared = [_prepare_detach(repository, runtime) for runtime in runtimes]
    changed = False
    for target, original, mode, updated, remove in prepared:
        if original is None or updated == original:
            continue
        if remove:
            if _read_agents(target)[0] != original:
                raise ProjectError(f"{target.name} changed while it was being updated; retry")
            os.unlink(target)
            sync_directory(target.parent)
        else:
            _write_atomic(target, original, mode, updated)
        changed = True
    return changed
