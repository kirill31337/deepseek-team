"""Idempotent managed blocks for Codex and Claude Code project guidance.

The module writes delegation guidance into a clearly marked, unique block in the
coordinator-native root instruction file. Every byte outside the owned region is
preserved verbatim: line endings are not normalized and user text is not dropped.
"""
import os
import json
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
CODEX_HOOK_COMMAND = 'deepseek-team coordinator-hook'
CODEX_HOOKS = {
    'SessionStart': {'matcher': '^(startup|resume|compact)$', 'context': True},
    'UserPromptSubmit': {'matcher': None, 'context': True},
    'PreToolUse': {'matcher': '^(Bash|apply_patch)$', 'context': False},
    'PostToolUse': {'matcher': '^(Bash|apply_patch)$', 'context': False},
    'Stop': {'matcher': None, 'context': False},
}


class ProjectError(Exception):
    """Raised for an invalid or ambiguous target; existing content is untouched."""


def _guidance(runtime='codex', root=None):
    try:
        body = DATA_FILE.read_bytes()
    except OSError as error:
        raise ProjectError("packaged delegation guidance is unavailable") from error
    from . import settings
    try:
        policy = settings.resolve(root)
    except settings.SettingsError as error:
        raise ProjectError(str(error)) from None
    dynamic = settings.instructions(policy, runtime).encode('utf-8')
    return body.replace(b'{runtime}', runtime.encode('ascii')).strip(b"\n") + b'\n\n' + dynamic


def _block_bytes(state, runtime='codex', root=None):
    metadata = b"<!-- codex-deepseek-team:original:" + state + b" -->\n"
    return START_MARKER + b"\n" + metadata + _guidance(runtime, root) + b"\n" + END_MARKER + b"\n"


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


def _prepare_attach(repository, runtime):
    target = repository / TARGETS[runtime]
    original, mode = _read_agents(target)
    content = original if original is not None else b""
    marks = _locate(content)
    if marks is None:
        state = b"created" if original is None else (b"existing-content" if original else b"existing-empty")
        block = _block_bytes(state, runtime, repository)
        updated = content + (b"\n" if content else b"") + block
    else:
        begin, finish = marks
        state = _original_state(content, begin)
        block = _block_bytes(state, runtime, repository)
        updated = content[:begin] + block + content[_owned_span(content, begin, finish, state)[1]:]
    return target, original, mode, updated


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


def _codex_hook_group(event, spec):
    handler = {
        'type': 'command',
        'command': CODEX_HOOK_COMMAND,
        'timeout': 10,
        'statusMessage': 'DeepSeek Team coordination policy',
    }
    if spec.get('context'):
        handler['additionalContextLimit'] = 2200
    group = {'hooks': [handler]}
    if spec.get('matcher') is not None:
        group['matcher'] = spec['matcher']
    return group


def _strip_package_hooks(data):
    hooks = data.get('hooks', {})
    if not isinstance(hooks, dict):
        raise ProjectError('.codex/hooks.json hooks must be an object')
    changed = False
    for event in list(hooks):
        groups = hooks[event]
        if not isinstance(groups, list):
            raise ProjectError('.codex/hooks.json event groups must be arrays')
        kept = []
        for group in groups:
            if not isinstance(group, dict):
                kept.append(group)
                continue
            handlers = group.get('hooks')
            if not isinstance(handlers, list):
                kept.append(group)
                continue
            filtered = [h for h in handlers if not (
                isinstance(h, dict) and h.get('command') == CODEX_HOOK_COMMAND)]
            if len(filtered) != len(handlers):
                changed = True
            if filtered:
                copy = dict(group)
                copy['hooks'] = filtered
                kept.append(copy)
        if kept:
            hooks[event] = kept
        else:
            hooks.pop(event, None)
    return changed


def _update_codex_hooks(repository, remove=False):
    directory = repository / '.codex'
    target = directory / 'hooks.json'
    original = None
    mode = DEFAULT_MODE
    if target.exists() or target.is_symlink():
        original, existing_mode = _read_agents(target)
        mode = existing_mode if existing_mode is not None else DEFAULT_MODE
        try:
            data = json.loads((original or b'{}').decode('utf-8'))
        except (ValueError, UnicodeError):
            raise ProjectError('cannot safely update invalid .codex/hooks.json') from None
        if not isinstance(data, dict):
            raise ProjectError('.codex/hooks.json must contain an object')
    else:
        data = {}
    changed = _strip_package_hooks(data)
    hooks = data.setdefault('hooks', {})
    if not remove:
        for event, spec in CODEX_HOOKS.items():
            hooks.setdefault(event, []).append(_codex_hook_group(event, spec))
        changed = True
    if remove and not hooks:
        data.pop('hooks', None)
    if remove and not data:
        if original is not None:
            os.unlink(target)
            sync_directory(directory)
            try:
                directory.rmdir()
            except OSError:
                pass
            return True
        return changed
    raw = (json.dumps(data, indent=2, sort_keys=True) + '\n').encode()
    if original == raw:
        return False
    if not directory.exists():
        directory.mkdir(mode=0o755)
        sync_directory(repository)
    _write_atomic(target, original, mode, raw)
    return True


def attach(root, coordinator='codex'):
    """Add or refresh managed blocks; return True when any target changed."""
    runtimes = _coordinators(coordinator)
    repository = _repository_root(root)
    prepared = [_prepare_attach(repository, runtime) for runtime in runtimes]
    changed = False
    for target, original, mode, updated in prepared:
        if original is not None and updated == original:
            continue
        _write_atomic(target, original, mode if mode is not None else DEFAULT_MODE, updated)
        changed = True
    if 'codex' in runtimes:
        changed = _update_codex_hooks(repository, remove=False) or changed
    return changed


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
    if 'codex' in runtimes:
        changed = _update_codex_hooks(repository, remove=True) or changed
    return changed
, 'context': True},
    'UserPromptSubmit': {'matcher': None, 'context': True},
    'PreToolUse': {'matcher': '^(Bash|apply_patch)

class ProjectError(Exception):
    """Raised for an invalid or ambiguous target; existing content is untouched."""


def _guidance(runtime='codex', root=None):
    try:
        body = DATA_FILE.read_bytes()
    except OSError as error:
        raise ProjectError("packaged delegation guidance is unavailable") from error
    from . import settings
    try:
        policy = settings.resolve(root)
    except settings.SettingsError as error:
        raise ProjectError(str(error)) from None
    dynamic = settings.instructions(policy, runtime).encode('utf-8')
    return body.replace(b'{runtime}', runtime.encode('ascii')).strip(b"\n") + b'\n\n' + dynamic


def _block_bytes(state, runtime='codex', root=None):
    metadata = b"<!-- codex-deepseek-team:original:" + state + b" -->\n"
    return START_MARKER + b"\n" + metadata + _guidance(runtime, root) + b"\n" + END_MARKER + b"\n"


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


def _prepare_attach(repository, runtime):
    target = repository / TARGETS[runtime]
    original, mode = _read_agents(target)
    content = original if original is not None else b""
    marks = _locate(content)
    if marks is None:
        state = b"created" if original is None else (b"existing-content" if original else b"existing-empty")
        block = _block_bytes(state, runtime, repository)
        updated = content + (b"\n" if content else b"") + block
    else:
        begin, finish = marks
        state = _original_state(content, begin)
        block = _block_bytes(state, runtime, repository)
        updated = content[:begin] + block + content[_owned_span(content, begin, finish, state)[1]:]
    return target, original, mode, updated


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


def attach(root, coordinator='codex'):
    """Add or refresh managed blocks; return True when any target changed."""
    runtimes = _coordinators(coordinator)
    repository = _repository_root(root)
    prepared = [_prepare_attach(repository, runtime) for runtime in runtimes]
    changed = False
    for target, original, mode, updated in prepared:
        if original is not None and updated == original:
            continue
        _write_atomic(target, original, mode if mode is not None else DEFAULT_MODE, updated)
        changed = True
    return changed


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
, 'context': False},
    'PostToolUse': {'matcher': '^(Bash|apply_patch)

class ProjectError(Exception):
    """Raised for an invalid or ambiguous target; existing content is untouched."""


def _guidance(runtime='codex', root=None):
    try:
        body = DATA_FILE.read_bytes()
    except OSError as error:
        raise ProjectError("packaged delegation guidance is unavailable") from error
    from . import settings
    try:
        policy = settings.resolve(root)
    except settings.SettingsError as error:
        raise ProjectError(str(error)) from None
    dynamic = settings.instructions(policy, runtime).encode('utf-8')
    return body.replace(b'{runtime}', runtime.encode('ascii')).strip(b"\n") + b'\n\n' + dynamic


def _block_bytes(state, runtime='codex', root=None):
    metadata = b"<!-- codex-deepseek-team:original:" + state + b" -->\n"
    return START_MARKER + b"\n" + metadata + _guidance(runtime, root) + b"\n" + END_MARKER + b"\n"


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


def _prepare_attach(repository, runtime):
    target = repository / TARGETS[runtime]
    original, mode = _read_agents(target)
    content = original if original is not None else b""
    marks = _locate(content)
    if marks is None:
        state = b"created" if original is None else (b"existing-content" if original else b"existing-empty")
        block = _block_bytes(state, runtime, repository)
        updated = content + (b"\n" if content else b"") + block
    else:
        begin, finish = marks
        state = _original_state(content, begin)
        block = _block_bytes(state, runtime, repository)
        updated = content[:begin] + block + content[_owned_span(content, begin, finish, state)[1]:]
    return target, original, mode, updated


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


def attach(root, coordinator='codex'):
    """Add or refresh managed blocks; return True when any target changed."""
    runtimes = _coordinators(coordinator)
    repository = _repository_root(root)
    prepared = [_prepare_attach(repository, runtime) for runtime in runtimes]
    changed = False
    for target, original, mode, updated in prepared:
        if original is not None and updated == original:
            continue
        _write_atomic(target, original, mode if mode is not None else DEFAULT_MODE, updated)
        changed = True
    return changed


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
, 'context': False},
    'Stop': {'matcher': None, 'context': False},
}


class ProjectError(Exception):
    """Raised for an invalid or ambiguous target; existing content is untouched."""


def _guidance(runtime='codex', root=None):
    try:
        body = DATA_FILE.read_bytes()
    except OSError as error:
        raise ProjectError("packaged delegation guidance is unavailable") from error
    from . import settings
    try:
        policy = settings.resolve(root)
    except settings.SettingsError as error:
        raise ProjectError(str(error)) from None
    dynamic = settings.instructions(policy, runtime).encode('utf-8')
    return body.replace(b'{runtime}', runtime.encode('ascii')).strip(b"\n") + b'\n\n' + dynamic


def _block_bytes(state, runtime='codex', root=None):
    metadata = b"<!-- codex-deepseek-team:original:" + state + b" -->\n"
    return START_MARKER + b"\n" + metadata + _guidance(runtime, root) + b"\n" + END_MARKER + b"\n"


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


def _prepare_attach(repository, runtime):
    target = repository / TARGETS[runtime]
    original, mode = _read_agents(target)
    content = original if original is not None else b""
    marks = _locate(content)
    if marks is None:
        state = b"created" if original is None else (b"existing-content" if original else b"existing-empty")
        block = _block_bytes(state, runtime, repository)
        updated = content + (b"\n" if content else b"") + block
    else:
        begin, finish = marks
        state = _original_state(content, begin)
        block = _block_bytes(state, runtime, repository)
        updated = content[:begin] + block + content[_owned_span(content, begin, finish, state)[1]:]
    return target, original, mode, updated


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


def attach(root, coordinator='codex'):
    """Add or refresh managed blocks; return True when any target changed."""
    runtimes = _coordinators(coordinator)
    repository = _repository_root(root)
    prepared = [_prepare_attach(repository, runtime) for runtime in runtimes]
    changed = False
    for target, original, mode, updated in prepared:
        if original is not None and updated == original:
            continue
        _write_atomic(target, original, mode if mode is not None else DEFAULT_MODE, updated)
        changed = True
    return changed


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
