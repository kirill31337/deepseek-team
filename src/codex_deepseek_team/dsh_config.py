"""Install only DeepSeek Team's DSH bridge plugin in a DeepSeek Harness profile.

The Qwen coordinator runs inside the DeepSeek Harness (DSH). DSH has no
Codex/Claude-style user hooks file; instead its cordis profile loads plugin
rows, and an inserted row's ``name`` may be an absolute filesystem path. The
package therefore ships a dependency-free ESM bridge plugin as package data
and installs exactly one owned, marker-delimited row block into the profile's
``cordis.patch.yml``. DSH treats a patch row that carries ``id`` but no
``insert`` as an *update* of an already existing entry, so a brand-new plugin
must be written as an ``insert:`` patch; the owned block therefore renders one
inserted row. The earlier bare-row shape is still recognized as owned, so an
existing installation is migrated by ``hooks refresh``. No pnpm invocation,
network access, manifest change or foreign-row rewrite is involved: every byte
outside the owned block is preserved verbatim, and the write is atomic with a
concurrent-change check.

The bridge receives the absolute ``deepseek-team`` entrypoint in its row
config and translates DSH agent/tool events into
``deepseek-team coordinator-hook --runtime qwen`` calls. Profile discovery is
explicit: ``DEEPSEEK_TEAM_DSH_PROFILE`` or the single existing profile under
``$DSH_HOME/profiles`` (default ``~/.dsh``); an ambiguous choice is diagnosed,
never guessed.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import stat

from .config import ConfigError, atomic_write, read_regular


PLUGIN_ID = 'deepseek-team-qwen'
START_MARKER = '# deepseek-team:qwen:start'
END_MARKER = '# deepseek-team:qwen:end'
PROFILE_VARIABLE = 'DEEPSEEK_TEAM_DSH_PROFILE'
PATCH_NAME = 'cordis.patch.yml'
# Canonical shape: one inserted plugin row inside the owned marker block.
_NAME_LINE = re.compile(r'      name: (".*")')
_ENTRYPOINT_LINE = re.compile(r'        entrypoint: (".*")')
# Legacy shape: the bare patch row written before the insert fix; recognized
# only as ownership evidence so refresh can migrate it.
_LEGACY_NAME_LINE = re.compile(r'  name: (".*")')
_LEGACY_ENTRYPOINT_LINE = re.compile(r'    entrypoint: (".*")')
_CANONICAL_ID_LINE = '    - id: ' + PLUGIN_ID
_CANONICAL_CONFIG_LINE = '      config:'
_CANONICAL_RUNTIME_LINE = '        runtime: qwen'
_LEGACY_ID_LINE = '- id: ' + PLUGIN_ID
_LEGACY_CONFIG_LINE = '  config:'
_LEGACY_RUNTIME_LINE = '    runtime: qwen'
_FOREIGN_ID = re.compile(r'^\s*-\s+id:\s*' + PLUGIN_ID + r'\s*$', re.M)


def dsh_home() -> Path:
    configured = os.environ.get('DSH_HOME')
    return Path(configured).expanduser() if configured else Path.home() / '.dsh'


def profiles_root() -> Path:
    return dsh_home() / 'profiles'


def plugin_path() -> Path:
    """The packaged bridge module; never resolved through a symlink at install."""
    return Path(__file__).resolve().parent / 'data' / 'dsh-bridge' / 'index.js'


def detect_profile(explicit=None):
    """Return one existing profile directory, or ``None`` when DSH is absent.

    An explicit name (argument or environment) must exist; ambiguity between
    several profiles is an actionable error, never a guess. A profile is a
    directory holding a ``cordis.patch.yml`` or ``package.json``.
    """
    name = explicit if explicit is not None else os.environ.get(PROFILE_VARIABLE)
    root = profiles_root()
    if isinstance(name, str) and name.strip():
        name = name.strip()
        if '/' in name or name in ('.', '..'):
            raise ConfigError('DSH profile name must be a single directory name under '
                              + str(root) + '; no path traversal is accepted.')
        candidate = root / name
        if not candidate.is_dir():
            raise ConfigError('DSH profile ' + json.dumps(name) + ' does not exist under '
                              + str(root) + '. Create it with dsh or pass an existing profile.')
        return candidate
    try:
        entries = sorted(root.iterdir())
    except (FileNotFoundError, NotADirectoryError):
        return None
    except OSError:
        raise ConfigError('DSH profiles directory is inaccessible: ' + str(root)) from None
    found = [entry for entry in entries
             if entry.is_dir() and ((entry / PATCH_NAME).exists()
                                    or (entry / 'package.json').exists())]
    if not found:
        return None
    if len(found) > 1:
        raise ConfigError('Several DSH profiles exist under ' + str(root) + ' ('
                          + ', '.join(entry.name for entry in found)
                          + '); select one explicitly with ' + PROFILE_VARIABLE
                          + '. No profile was chosen automatically.')
    return found[0]


def resolve_entrypoint():
    """Absolute lexical ``deepseek-team`` command; fails before any file write."""
    found = shutil.which('deepseek-team')
    if not found:
        raise ConfigError(
            'DeepSeek Team cannot install the Qwen (DSH) bridge because the '
            '`deepseek-team` executable cannot be resolved on PATH. Install the package '
            'entrypoint (for pipx run `pipx ensurepath`; for uv run `uv tool update-shell`; '
            'or activate the environment that provides it), restart the terminal, and '
            'rerun `deepseek-team hooks install --runtime qwen`. No profile was changed.')
    path = Path(found).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    return str(Path(os.path.abspath(path)))


def _render_block(entrypoint: str, plugin: Path) -> str:
    """The canonical owned block: one ``insert:`` patch adding the bridge row.

    DSH's patch algorithm reads a row with ``id`` but no ``insert`` as an
    update of an existing entry and skips it when no such entry exists, so a
    new plugin row must be inserted explicitly.
    """
    return (START_MARKER + '\n'
            + '- insert:\n'
            + '    - id: ' + PLUGIN_ID + '\n'
            + '      name: ' + json.dumps(str(plugin)) + '\n'
            + '      config:\n'
            + '        entrypoint: ' + json.dumps(str(entrypoint)) + '\n'
            + '        runtime: qwen\n'
            + END_MARKER + '\n')


def _lexical_absolute(value: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    return Path(os.path.abspath(path))


def _locate(text: str):
    starts = text.count(START_MARKER)
    ends = text.count(END_MARKER)
    if not starts and not ends:
        return None
    if starts != 1 or ends != 1:
        raise ConfigError('DSH profile patch has duplicate or unbalanced DeepSeek Team '
                          'qwen markers; existing content was preserved.')
    begin = text.find(START_MARKER)
    finish = text.find(END_MARKER)
    if finish < begin:
        raise ConfigError('DSH profile patch has out-of-order DeepSeek Team qwen markers; '
                          'existing content was preserved.')
    if (begin and text[begin - 1] != '\n') or text[begin + len(START_MARKER):begin + len(START_MARKER) + 1] != '\n':
        raise ConfigError('DeepSeek Team qwen start marker must own its line; '
                          'existing content was preserved.')
    end = finish + len(END_MARKER)
    if text[end:end + 1] not in ('\n', ''):
        raise ConfigError('DeepSeek Team qwen end marker must own its line; '
                          'existing content was preserved.')
    return begin, end + 1 if text[end:end + 1] == '\n' else end


def _strip(text: str) -> tuple[str, bool]:
    span = _locate(text)
    if span is None:
        return text, False
    begin, end = span
    return text[:begin] + text[end:], True


def _parse_owned(block: str):
    """Ownership evidence: exact id, absolute dsh-bridge module, qwen runtime.

    Returns ``(plugin, entrypoint)`` for a structurally owned block, else
    ``None``. Both the canonical ``insert:`` shape and the legacy bare-row
    shape are accepted, so an installation made before the insert fix stays
    package-owned and ``hooks refresh`` can migrate it. Parsing never adopts a
    foreign row that merely shares the id.
    """
    lines = block.split('\n')
    if len(lines) < 8 or lines[0] != START_MARKER or lines[-2] != END_MARKER:
        return None
    body = lines[1:-2]
    if body and body[0] == '- insert:':
        expected = (_CANONICAL_ID_LINE, _CANONICAL_CONFIG_LINE, _CANONICAL_RUNTIME_LINE)
        shape = (_NAME_LINE, _ENTRYPOINT_LINE)
        body = body[1:]
    else:
        expected = (_LEGACY_ID_LINE, _LEGACY_CONFIG_LINE, _LEGACY_RUNTIME_LINE)
        shape = (_LEGACY_NAME_LINE, _LEGACY_ENTRYPOINT_LINE)
    if (len(body) != 5 or body[0] != expected[0] or body[2] != expected[1]
            or body[4] != expected[2]):
        return None
    name = shape[0].fullmatch(body[1])
    entrypoint = shape[1].fullmatch(body[3])
    if name is None or entrypoint is None:
        return None
    try:
        plugin_value = json.loads(name.group(1))
        entrypoint_value = json.loads(entrypoint.group(1))
    except ValueError:
        return None
    if not isinstance(plugin_value, str) or not isinstance(entrypoint_value, str):
        return None
    plugin = _lexical_absolute(plugin_value)
    if plugin.name != 'index.js' or plugin.parent.name != 'dsh-bridge':
        return None
    entrypoint = _lexical_absolute(entrypoint_value)
    if entrypoint.name != 'deepseek-team':
        return None
    return plugin, entrypoint


def _read(profile: Path):
    path = Path(profile) / PATCH_NAME
    raw = read_regular(path)
    if raw is None:
        return path, None, ''
    try:
        text = raw.decode('utf-8')
    except UnicodeError:
        raise ConfigError('DSH profile patch is not valid UTF-8; existing content was preserved.') from None
    return path, raw, text


def _write(path: Path, text: str, raw):
    updated = text.encode('utf-8')
    if updated == raw:
        return False
    mode = stat.S_IMODE(path.stat().st_mode) if raw is not None else 0o600
    atomic_write(path, updated, raw, mode)
    return True


def install(profile=None, executable=None):
    """Add or refresh the owned bridge row; returns True when the file changed."""
    entrypoint = resolve_entrypoint() if executable is None else str(_lexical_absolute(executable))
    plugin = plugin_path()
    if not plugin.is_file():
        raise ConfigError('Packaged DSH bridge plugin is missing: ' + str(plugin))
    selected = detect_profile(profile)
    if selected is None:
        raise ConfigError('No DeepSeek Harness profile was found under ' + str(profiles_root())
                          + '. Install/start DSH once, or set ' + PROFILE_VARIABLE
                          + ' to an existing profile name. No file was changed.')
    path, raw, text = _read(selected)
    stripped, _owned = _strip(text)
    if _FOREIGN_ID.search(stripped):
        raise ConfigError('DSH profile patch already contains a foreign row with id '
                          + PLUGIN_ID + ' outside the DeepSeek Team markers. Repair or '
                          'remove that row manually; no file was changed.')
    if stripped and not stripped.endswith('\n'):
        stripped += '\n'
    updated = stripped + _render_block(entrypoint, plugin)
    return _write(path, updated, raw)


def owned(profile=None) -> bool:
    selected = detect_profile(profile)
    if selected is None:
        return False
    _path, _raw, text = _read(selected)
    span = _locate(text)
    if span is None:
        return False
    block = text[span[0]:span[1]]
    return _parse_owned(block) is not None


def owned_entrypoints(profile=None):
    """Executable entrypoints recorded by the owned row (ownership evidence only)."""
    selected = detect_profile(profile)
    if selected is None:
        return ()
    _path, _raw, text = _read(selected)
    span = _locate(text)
    if span is None:
        return ()
    parsed = _parse_owned(text[span[0]:span[1]])
    if parsed is None:
        return ()
    entrypoint = parsed[1]
    try:
        if entrypoint.is_file() and os.access(entrypoint, os.X_OK):
            return (entrypoint,)
    except OSError:
        pass
    return ()


def status(profile=None, executable=None) -> bool:
    """Whether the owned bridge row is installed in canonical form and usable.

    With a resolvable current entrypoint (or an explicit one) the row must
    match the canonical rendering exactly; without one (an unavailable PATH)
    the row is compared against the canonical rendering of its own recorded
    entrypoint and plugin module, so a legacy bare-row block is reported stale
    instead of current while a read-only status check still survives a missing
    PATH.
    """
    selected = detect_profile(profile)
    if selected is None:
        return False
    _path, _raw, text = _read(selected)
    span = _locate(text)
    if span is None:
        return False
    block = text[span[0]:span[1]]
    parsed = _parse_owned(block)
    if parsed is None:
        return False
    plugin, entrypoint = parsed
    try:
        if not plugin.is_file() or not entrypoint.is_file() or not os.access(entrypoint, os.X_OK):
            return False
    except OSError:
        return False
    expected = None
    if executable is not None:
        expected = str(_lexical_absolute(executable))
    else:
        try:
            expected = resolve_entrypoint()
        except ConfigError:
            expected = None
    if expected is not None:
        return block == _render_block(expected, plugin_path())
    return block == _render_block(str(entrypoint), plugin)


def remove(profile=None) -> bool:
    selected = detect_profile(profile)
    if selected is None:
        return False
    path, raw, text = _read(selected)
    stripped, changed = _strip(text)
    if not changed:
        return False
    return _write(path, stripped, raw)