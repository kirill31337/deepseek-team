"""Versioned Git administrative evidence and trusted-index content access.

The working index is worker-controlled.  It may be used as a source of paths,
but its stat cache is never trusted as evidence about file content.  Diff,
capture, and attribution therefore seed a fresh private index from the already
verified baseline commit.  The same representation canonicalizes only the nine
stat fields allowed by the remediation contract; every other index field and
extension payload remains significant.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import hmac
import os
from pathlib import Path
import re
import shutil
import stat
import struct
import subprocess
import tempfile

from .settings import git_environment

VERSION = 2
MAX_INDEX_BYTES = 64 * 1024 * 1024
_INDEX_HEADER = 12
_OBJECT_FORMATS = {'sha1': 20, 'sha256': 32}
_HEX_DIGEST = re.compile(r'[0-9a-f]{64}\Z')
_HEAD = re.compile(r'(?:[0-9a-f]{40}|[0-9a-f]{64})\Z')
_SPLIT_SPARSE = {b'link', b'sdir'}


class GitIntegrityError(Exception):
    """Git administrative state is malformed, unsupported, changed or uninspectable.

    ``kind`` keeps the capture layer's closed failure mapping structural: a
    verified mismatch is not the same branch as an index format that this
    release cannot inspect.  Callers never classify by exception text.
    """

    code = 73

    def __init__(self, message: str, *, kind: str = 'integrity_mismatch'):
        self.message = message
        self.kind = kind
        super().__init__(message)


def _reject(message: str, *, kind: str = 'integrity_mismatch') -> None:
    raise GitIntegrityError(message, kind=kind)


def _read_file(path: Path, *, maximum: int | None = None,
               kind: str = 'integrity_mismatch') -> bytes:
    fd = None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or info.st_uid != os.geteuid()):
            _reject('Git administrative data is not an owned ordinary file; copy retained.')
        if maximum is not None and info.st_size > maximum:
            _reject('Git administrative data exceeds its supported bound; copy retained.',
                    kind=kind)
        with os.fdopen(fd, 'rb') as stream:
            fd = None
            data = stream.read() if maximum is None else stream.read(maximum + 1)
        if maximum is not None and len(data) > maximum:
            _reject('Git administrative data exceeds its supported bound; copy retained.',
                    kind=kind)
        return data
    except GitIntegrityError:
        raise
    except OSError:
        _reject('Git administrative data cannot be read safely; copy retained.',
                kind='uninspectable')
    finally:
        if fd is not None:
            os.close(fd)


def _metadata_directory(root: Path) -> Path:
    metadata = Path(root) / '.git'
    try:
        if metadata.is_symlink() or not metadata.is_dir():
            _reject('Git metadata is not an ordinary directory; copy retained.')
    except OSError:
        _reject('Git metadata cannot be inspected safely; copy retained.',
                kind='uninspectable')
    return metadata


def _object_format(root: Path) -> str:
    try:
        result = subprocess.run(
            ['git', '--no-pager', '-C', str(root), 'config', '--local', '--no-includes',
             '--get', 'extensions.objectFormat'],
            env=git_environment(), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        _reject('Git object format cannot be determined safely; copy retained.',
                kind='uninspectable')
    if result.returncode == 1 and not result.stdout:
        return 'sha1'
    if result.returncode != 0:
        _reject('Git object format cannot be determined safely; copy retained.',
                kind='uninspectable')
    try:
        value = result.stdout.decode('ascii').strip()
    except UnicodeError:
        _reject('Git object format is malformed; copy retained.')
    if value not in _OBJECT_FORMATS:
        _reject('Git object format is unsupported; copy retained.', kind='unsupported')
    return value


def _hash(data: bytes, object_format: str) -> bytes:
    try:
        return hashlib.new(object_format, data).digest()
    except (ValueError, TypeError):
        _reject('Git object format is unsupported; copy retained.')


def _path_is_safe(path: bytes) -> bool:
    if not path or path.startswith(b'/') or b'\0' in path:
        return False
    return all(part not in {b'', b'.', b'..'} for part in path.split(b'/'))


def _canonical(data: bytes, object_format: str) -> tuple[bytes, int]:
    if not isinstance(object_format, str) or object_format not in _OBJECT_FORMATS:
        _reject('Git object format is unsupported; copy retained.', kind='unsupported')
    if not isinstance(data, bytes):
        _reject('Git index must be bytes.')
    if len(data) > MAX_INDEX_BYTES:
        _reject('Git index exceeds the 64 MiB supported bound; copy retained.',
                kind='unsupported')
    hash_length = _OBJECT_FORMATS[object_format]
    if len(data) < _INDEX_HEADER + hash_length:
        _reject('Git index is truncated; copy retained.')
    body, checksum = data[:-hash_length], data[-hash_length:]
    if _hash(body, object_format) != checksum:
        _reject('Git index checksum is invalid; copy retained.')
    if body[:4] != b'DIRC':
        _reject('Git index header is invalid; copy retained.')
    version, count = struct.unpack('>II', body[4:12])
    if version == 4:
        _reject('Git index v4 is unsupported; copy retained.', kind='unsupported')
    if version not in (2, 3):
        _reject('Git index version is unsupported; copy retained.', kind='unsupported')

    canonical = bytearray(body)
    cursor = _INDEX_HEADER
    fixed_size = 40 + hash_length + 2
    previous = None
    for _number in range(count):
        start = cursor
        if start + fixed_size > len(body):
            _reject('Git index entry is truncated; copy retained.')
        flags = struct.unpack('>H', body[start + 40 + hash_length:start + 42 + hash_length])[0]
        cursor = start + fixed_size
        if flags & 0x4000:
            if version != 3 or cursor + 2 > len(body):
                _reject('Git index extended flags are malformed; copy retained.')
            extended = struct.unpack('>H', body[cursor:cursor + 2])[0]
            cursor += 2
            if extended & ~0x6000:
                _reject('Git index extended flags are unsupported; copy retained.')

        length = flags & 0x0FFF
        path_start = cursor
        if length == 0x0FFF:
            end = body.find(b'\0', path_start)
            if end < 0 or end - path_start < 0x0FFF:
                _reject('Git index path is malformed; copy retained.')
        else:
            end = path_start + length
            if end > len(body):
                _reject('Git index path is truncated; copy retained.')
        path = body[path_start:end]
        if not _path_is_safe(path):
            _reject('Git index path is unsafe or malformed; copy retained.')
        stage = (flags >> 12) & 0x3
        ordering = (path, stage)
        if previous is not None and ordering <= previous:
            _reject('Git index entries are unsorted or duplicated; copy retained.')
        previous = ordering

        entry_length = ((end - start) // 8 + 1) * 8
        entry_end = start + entry_length
        if entry_end > len(body):
            _reject('Git index entry padding is truncated; copy retained.')
        padding = body[end:entry_end]
        if not 1 <= len(padding) <= 8 or any(padding):
            _reject('Git index entry padding is malformed; copy retained.')
        cursor = entry_end

        # ctime/mtime/dev/ino (24 bytes) and uid/gid/size (12 bytes) are the
        # only mutable stat cache fields.  Mode at 24..28 remains significant.
        canonical[start:start + 24] = b'\0' * 24
        canonical[start + 28:start + 40] = b'\0' * 12

    while cursor < len(body):
        if cursor + 8 > len(body):
            _reject('Git index extension header is truncated; copy retained.')
        signature = body[cursor:cursor + 4]
        if signature in _SPLIT_SPARSE:
            _reject('Split and sparse Git indexes are unsupported; copy retained.',
                    kind='unsupported')
        # Git ignores an unknown extension whose first byte is uppercase; any
        # other unknown signature is mandatory and must not be ignored. Known
        # optional payloads still participate byte-for-byte in the digest.
        if not 65 <= signature[0] <= 90:
            _reject('Git index has an unknown mandatory extension; copy retained.',
                    kind='unsupported')
        size = struct.unpack('>I', body[cursor + 4:cursor + 8])[0]
        end = cursor + 8 + size
        if end > len(body):
            _reject('Git index extension is truncated; copy retained.')
        cursor = end

    return bytes(canonical) + _hash(bytes(canonical), object_format), version


def canonical_index(data: bytes, object_format: str) -> bytes:
    """Validate one v2/v3 index and zero only its permissible stat fields."""
    return _canonical(data, object_format)[0]


def _frame(relative: str, data: bytes) -> bytes:
    encoded = os.fsencode(relative)
    return (b'F\0' + struct.pack('>Q', len(encoded)) + encoded +
            struct.pack('>Q', len(data)) + data)


def snapshot(root: Path, *, object_format: str | None = None) -> dict:
    """Return version-2 evidence over normalized Git administrative state."""
    metadata = _metadata_directory(root)
    object_format = _object_format(Path(root)) if object_format is None else object_format
    if not isinstance(object_format, str) or object_format not in _OBJECT_FORMATS:
        _reject('Git object format is unsupported; copy retained.', kind='unsupported')
    digest = hashlib.sha256()
    digest.update(b'deepseek-team-git-integrity\0' + bytes([VERSION]) + b'\0')
    digest.update(object_format.encode('ascii') + b'\0')
    saw_index = False

    def walk_error(error):
        _reject('Git administrative data cannot be inspected safely; copy retained.',
                kind='uninspectable')

    try:
        for directory, dirs, files in os.walk(metadata, followlinks=False, onerror=walk_error):
            current = Path(directory)
            relative_dir = current.relative_to(metadata)
            if relative_dir == Path('.'):
                dirs[:] = sorted(name for name in dirs if name != 'objects')
            else:
                dirs.sort()
            for name in dirs:
                if (current / name).is_symlink():
                    _reject('Git administrative data contains a symlink; copy retained.')
            for name in sorted(files):
                item = current / name
                if item.is_symlink():
                    _reject('Git administrative data contains a symlink; copy retained.')
                relative = item.relative_to(metadata).as_posix()
                if relative == 'index':
                    raw = _read_file(item, maximum=MAX_INDEX_BYTES, kind='unsupported')
                    content = canonical_index(raw, object_format)
                    saw_index = True
                else:
                    content = _read_file(item)
                digest.update(_frame(relative, content))
    except GitIntegrityError:
        raise
    except OSError:
        _reject('Git administrative data cannot be inspected safely; copy retained.',
                kind='uninspectable')
    if not saw_index:
        _reject('Git index is missing; copy retained.')
    return {'version': VERSION, 'object_format': object_format,
            'digest': digest.hexdigest()}


def legacy_digest(root: Path) -> str:
    """Compute the pre-versioned raw digest for strict legacy comparison only."""
    metadata = _metadata_directory(root)
    digest = hashlib.sha256()

    def walk_error(error):
        _reject('Git administrative data cannot be inspected safely; copy retained.',
                kind='uninspectable')

    try:
        for directory, dirs, files in os.walk(metadata, followlinks=False, onerror=walk_error):
            dirs[:] = sorted(name for name in dirs if name != 'objects')
            for name in dirs:
                if (Path(directory) / name).is_symlink():
                    _reject('Git administrative data contains a symlink; copy retained.')
            for name in sorted(files):
                item = Path(directory) / name
                digest.update(os.fsencode(str(item.relative_to(metadata))) + b'\0')
                digest.update(_read_file(item))
    except GitIntegrityError:
        raise
    except OSError:
        _reject('Git administrative data cannot be inspected safely; copy retained.',
                kind='uninspectable')
    return digest.hexdigest()


def legacy_matches(root: Path, expected: str) -> bool:
    """Strictly compare legacy evidence; malformed values are never coerced."""
    if not isinstance(expected, str) or _HEX_DIGEST.fullmatch(expected) is None:
        _reject('Legacy Git integrity evidence is malformed; copy retained.')
    return hmac.compare_digest(legacy_digest(root), expected)


def verify(root: Path, evidence: dict) -> None:
    """Refuse a copy unless current normalized evidence exactly matches."""
    if (not isinstance(evidence, dict)
            or set(evidence) != {'version', 'object_format', 'digest'}
            or type(evidence.get('version')) is not int
            or evidence.get('version') != VERSION
            or not isinstance(evidence.get('object_format'), str)
            or evidence.get('object_format') not in _OBJECT_FORMATS
            or not isinstance(evidence.get('digest'), str)
            or _HEX_DIGEST.fullmatch(evidence['digest']) is None):
        _reject('Git integrity evidence is malformed; copy retained.')
    current = snapshot(root, object_format=evidence['object_format'])
    if any(current[key] != evidence[key] for key in ('version', 'object_format', 'digest')):
        _reject('Git administrative state changed; result rejected and copy retained.')


def _inside(candidate: Path, parent: Path) -> bool:
    try:
        return os.path.commonpath((str(candidate), str(parent))) == str(parent)
    except ValueError:
        return False


@contextmanager
def temporary_index(root: Path, base_head: str, *, object_format: str | None = None):
    """Yield an environment whose private index is seeded from trusted HEAD.

    The caller must already have verified that ``base_head`` is the owned copy's
    baseline.  The temporary directory is private, outside ``.git``, and removed
    on every exit; the working index and work tree are never updated.
    """
    root = Path(root)
    object_format = _object_format(root) if object_format is None else object_format
    if (not isinstance(object_format, str) or object_format not in _OBJECT_FORMATS
            or not isinstance(base_head, str) or not _HEAD.fullmatch(base_head)):
        _reject('Trusted baseline or object format is malformed; copy retained.',
                kind='unsupported' if (not isinstance(object_format, str)
                    or object_format not in _OBJECT_FORMATS) else 'integrity_mismatch')
    expected_length = 40 if object_format == 'sha1' else 64
    if len(base_head) != expected_length:
        _reject('Trusted baseline does not match the repository object format.')
    root_real = Path(os.path.realpath(root))
    temp_parent = Path(os.path.realpath(tempfile.gettempdir()))
    metadata_real = Path(os.path.realpath(_metadata_directory(root)))
    if _inside(temp_parent, root_real) or _inside(temp_parent, metadata_real):
        _reject('Private temporary index directory would be inside the owned copy; copy retained.')
    try:
        directory = tempfile.mkdtemp(prefix='deepseek-git-index-', dir=str(temp_parent))
    except OSError:
        _reject('Cannot create a private index for the verified baseline; copy retained.',
                kind='uninspectable')
    try:
        env = git_environment()
        env['GIT_INDEX_FILE'] = str(Path(directory) / 'index')
        try:
            result = subprocess.run(
                ['git', '--no-pager', '-c', 'core.fsmonitor=false',
                 '-c', 'core.hooksPath=' + os.devnull, '-c', 'core.attributesFile=' + os.devnull,
                 '-c', 'diff.autoRefreshIndex=false', '-C', str(root), 'read-tree', base_head],
                env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError):
            _reject('Cannot seed a private index from the verified baseline; copy retained.',
                    kind='uninspectable')
        if result.returncode != 0:
            _reject('Cannot seed a private index from the verified baseline; copy retained.',
                    kind='uninspectable')
        yield env
    finally:
        shutil.rmtree(directory, ignore_errors=True)


__all__ = ['GitIntegrityError', 'MAX_INDEX_BYTES', 'VERSION', 'canonical_index',
           'legacy_digest', 'legacy_matches', 'snapshot', 'temporary_index', 'verify']
