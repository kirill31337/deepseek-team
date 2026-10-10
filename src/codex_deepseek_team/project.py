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
LINKED_MARKER = b"<!-- codex-deepseek-team:guidance:linked -->"
DATA_FILE = Path(__file__).resolve().parent / "data" / "delegation.md"
LINKED_DATA_FILE = Path(__file__).resolve().parent / "data" / "delegation-linked.md"
LINKED_GUIDE = ("docs", "agents", "delegation.md")
DEFAULT_MODE = 0o644
TARGETS = {'codex': 'AGENTS.md', 'claude': 'CLAUDE.md'}


class ProjectError(Exception):
    """Raised for an invalid or ambiguous target; existing content is untouched."""


def _linked_guide_path(root):
    """Validate and return the fixed human-maintained guide; never creates or edits it.

    Refuses symlinked path components, paths that escape the repository root, a
    missing or non-regular file, and empty or non-UTF-8 content. Any refusal
    raises :class:`ProjectError` during the pure prepare phase, so no file has
    been mutated yet.
    """
    if root is None:
        raise ProjectError("linked guidance requires a repository root")
    root_path = Path(root)
    guide = root_path
    for name in LINKED_GUIDE:
        guide = guide / name
        try:
            if guide.is_symlink():
                raise ProjectError("docs/agents/delegation.md must not traverse symbolic links")
        except OSError as error:
            raise ProjectError("cannot inspect docs/agents/delegation.md") from error
    real_root = os.path.realpath(os.fspath(root_path))
    real_guide = os.path.realpath(os.fspath(guide))
    try:
        inside = os.path.commonpath([real_root, real_guide]) == real_root
    except ValueError:
        inside = False
    if not inside:
        raise ProjectError("docs/agents/delegation.md must stay inside the repository")
    try:
        if not guide.exists():
            raise ProjectError("docs/agents/delegation.md is missing; create it or remove the linked marker")
        if not guide.is_file():
            raise ProjectError("docs/agents/delegation.md must be an ordinary file")
        with open(guide, "rb") as handle:
            raw = handle.read()
    except OSError as error:
        raise ProjectError("docs/agents/delegation.md must be an ordinary readable file") from error
    if not raw:
        raise ProjectError("docs/agents/delegation.md must not be empty")
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ProjectError("docs/agents/delegation.md must be nonempty UTF-8 text") from error
    return guide


def _linked_guidance(runtime, root):
    """Render the compact opt-in bootstrap; validates the guide before returning."""
    _linked_guide_path(root)
    try:
        body = LINKED_DATA_FILE.read_bytes()
    except OSError as error:
        raise ProjectError("packaged linked delegation guidance is unavailable") from error
    return LINKED_MARKER + b"\n" + body.replace(b'{runtime}', runtime.encode('ascii')).strip(b"\n")


def _is_linked(span):
    """True only when the standalone marker owns its own line inside the span."""
    return any(line.strip() == LINKED_MARKER for line in span.split(b"\n"))


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


def _block_bytes(state, runtime='codex', root=None, policy=None, *, linked=False):
    """Render the owned block; a supplied candidate policy skips settings lookup.

    ``linked`` selects the compact opt-in bootstrap; it is only ever set from the
    marker already present inside the owned span, never from a CLI flag or schema.
    """
    metadata = b"<!-- codex-deepseek-team:original:" + state + b" -->\n"
    guidance = _linked_guidance(runtime, root) if linked else _guidance(runtime, root, policy)
    return START_MARKER + b"\n" + metadata + guidance + b"\n" + END_MARKER + b"\n"


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
        block = _block_bytes(state, runtime, repository, policy, linked=False)
        updated = content + (b"\n" if content else b"") + block
    else:
        begin, finish = marks
        state = _original_state(content, begin)
        start, end = _owned_span(content, begin, finish, state)
        linked = _is_linked(content[begin:end])
        block = _block_bytes(state, runtime, repository, policy, linked=linked)
        updated = content[:begin] + block + content[end:]
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


def owns_block(root, runtime='codex'):
    """Bounded, Git-free ownership probe used before any policy or refresh work.

    Returns ``True`` only when this runtime's root instruction file already holds
    a managed block this package owns. Exactly that one file is inspected: children
    and other projects are never scanned and nothing is created. The same marker and
    metadata parsing as refresh is reused, so an unmarked or foreign file is not
    adopted, while a genuinely corrupt owned control (duplicate or unbalanced
    markers, modified ownership metadata, a symbolic link or a non-regular file)
    raises :class:`ProjectError` instead of being silently skipped.
    """
    if runtime not in TARGETS:
        raise ProjectError("coordinator must be codex or claude")
    content, _mode = _read_agents(Path(root) / TARGETS[runtime])
    if not content:
        return False
    marks = _locate(content)
    if marks is None:
        return False
    begin, finish = marks
    _original_state(content, begin)
    return finish > begin


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


def refresh_attached(root, policy, coordinator='both'):
    """Refresh only managed blocks that already exist; never create a file.

    Reuses :func:`prepare_refresh` and :func:`apply_refresh` so every target is
    read, owned and validated before anything is written. A partial failure
    attempts to restore pre-images without overwriting concurrent changes;
    blocked rollback is included in the diagnostic. Missing and foreign files without an owned
    block are skipped, so this never attaches an unrelated repository. Returns
    ``(written, problem)`` where ``written`` counts files actually changed and
    ``problem`` is an actionable diagnostic when refresh did not complete.
    """
    try:
        prepared = prepare_refresh(root, policy, coordinator)
    except ProjectError as error:
        return 0, str(error)
    try:
        written = apply_refresh(prepared)
    except ProjectError as error:
        return 0, str(error)
    return len(written), ''


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
