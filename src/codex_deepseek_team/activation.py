"""Local, persistent per-checkout activation, separate from shared project policy."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile

from . import settings
from .config import sync_directory

DISABLE_VARIABLE = 'DEEPSEEK_TEAM_DISABLED'
DISABLED_GUIDANCE = (
    'DeepSeek Team is disabled for this project. Continue locally without DeepSeek '
    'delegation or coordination-plan requirements. Do not re-enable it unless the '
    'user asks. Running workers are not cancelled; inspect their results before '
    'integrating changes. Use deepseek-team on to enable it again.'
)


@dataclass(frozen=True)
class Activation:
    enabled: bool
    saved_enabled: bool
    source: str

    def as_dict(self) -> dict:
        return dict(enabled=self.enabled, saved_enabled=self.saved_enabled, source=self.source)


def _directory() -> Path:
    return settings.global_file().parent / 'activation'


def _check_directory(directory: Path) -> None:
    # Never follow a substituted package directory when reading or saving state.
    for path in (directory.parent, directory):
        try:
            info = path.lstat()
        except FileNotFoundError:
            continue
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
            raise settings.SettingsError('Activation directory must be an owned ordinary directory.')


def state_file(root: Path) -> Path:
    key = hashlib.sha256(os.fsencode(str(root.resolve()))).hexdigest()
    return _directory() / (key + '.json')


def _saved(root: Path | None) -> tuple[bool, str]:
    if root is None:
        return True, 'default'
    path = state_file(root)
    _check_directory(path.parent)
    raw = settings._read(path)
    if raw is None:
        return True, 'default'
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError):
        raise settings.SettingsError('Invalid activation state; file preserved.') from None
    if (not isinstance(value, dict) or set(value) != {'enabled'}
            or type(value['enabled']) is not bool):
        raise settings.SettingsError('Invalid activation state; expected a boolean enabled field.')
    return value['enabled'], str(path)


def resolve(root: Path | None = None) -> Activation:
    root = settings.project_root(root)
    saved, source = _saved(root)
    if os.environ.get(DISABLE_VARIABLE) == '1':
        return Activation(False, saved, 'environment:' + DISABLE_VARIABLE)
    return Activation(saved, saved, source)


def set_enabled(root: Path, enabled: bool) -> None:
    root = settings.project_root(root, required=True)
    if type(enabled) is not bool:
        raise settings.SettingsError('Activation requires a boolean value.')
    path = state_file(root)
    _check_directory(path.parent)
    # Validate existing data before replacing it; do not repair corrupt state silently.
    _saved(root)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _check_directory(path.parent)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write((json.dumps({'enabled': enabled}) + '\n').encode())
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def describe(value: Activation) -> str:
    return (f'DeepSeek Team: {"on" if value.enabled else "off"} (source={value.source})\n'
            f'Saved project state: {"on" if value.saved_enabled else "off"}')
