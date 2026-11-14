"""Versioned Git administration evidence and trusted-index result capture."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import stat
import struct
import subprocess
import tempfile
import unittest
from unittest import mock

from codex_deepseek_team import git_integrity, workspace


ZERO_STAT = (0, 0, 0, 0, 0, 0, 0, 0, 0)


def _digest(data: bytes, object_format: str) -> bytes:
    return hashlib.new(object_format, data).digest()


def _entry(path: bytes, oid: bytes, *, stat=ZERO_STAT, mode=0o100644,
           flags: int = 0, extended: int | None = None) -> bytes:
    name_length = min(len(path), 0xFFF)
    raw = struct.pack('>10I', *(*stat[:6], mode, *stat[6:])) + oid
    raw += struct.pack('>H', flags | name_length)
    if extended is not None:
        raw += struct.pack('>H', extended)
    raw += path
    return raw + b'\0' * (8 - len(raw) % 8)


def _extension(signature: bytes, payload: bytes) -> bytes:
    return signature + struct.pack('>I', len(payload)) + payload


def _index(version: int, object_format: str, entries: list[bytes], *,
           extensions: bytes = b'') -> bytes:
    raw = b'DIRC' + struct.pack('>II', version, len(entries)) + b''.join(entries) + extensions
    return raw + _digest(raw, object_format)


def _checksum(data: bytes, object_format: str) -> bytes:
    return _digest(data, object_format)


def _entry_start(data: bytes, wanted: bytes, object_format: str = 'sha1') -> int:
    if data[:4] != b'DIRC' or struct.unpack('>I', data[4:8])[0] not in (2, 3):
        raise AssertionError('fixture is not a supported index')
    count = struct.unpack('>I', data[8:12])[0]
    hash_length = 20 if object_format == 'sha1' else 32
    cursor = 12
    for _ in range(count):
        start = cursor
        flags = struct.unpack('>H', data[start + 60:start + 62])[0]
        cursor += 62
        if flags & 0x4000:
            cursor += 2
        length = flags & 0xFFF
        if length == 0xFFF:
            end = data.index(b'\0', cursor)
        else:
            end = cursor + length
        path = data[cursor:end]
        # Entry padding aligns the entry length, not its absolute file offset.
        entry_length = ((end - start) // 8 + 1) * 8
        cursor = start + entry_length
        if path == wanted:
            return start
    raise AssertionError(f'index entry not found: {wanted!r}')


def _rewrite_checksum(data: bytes, object_format: str) -> bytes:
    hash_length = hashlib.new(object_format).digest_size
    return data[:-hash_length] + _checksum(data[:-hash_length], object_format)


def _forge_stat(index_path: Path, file_path: Path, name: bytes) -> None:
    data = index_path.read_bytes()
    start = _entry_start(data, name)
    info = file_path.lstat()
    stat = (info.st_ctime_ns // 1_000_000_000,
            info.st_ctime_ns % 1_000_000_000,
            info.st_mtime_ns // 1_000_000_000,
            info.st_mtime_ns % 1_000_000_000,
            info.st_dev, info.st_ino,
            info.st_uid, info.st_gid, info.st_size)
    # ctime/mode/... are ten stat words; mode at +40 remains untouched.
    for offset, value in zip((0, 4, 8, 12, 16, 20, 28, 32, 36), stat):
        data = data[:start + offset] + struct.pack('>I', value & 0xFFFFFFFF) + data[start + offset + 4:]
    index_path.write_bytes(_rewrite_checksum(data, 'sha1'))


class IndexCanonicalizationTests(unittest.TestCase):
    def test_v2_sha1_preserves_meaningful_fields_and_zeroes_only_stat(self):
        stat = (1, 2, 3, 4, 5, 6, 7, 8, 9)
        entry = _entry(b'src/app.py', b'\x11' * 20, stat=stat, mode=0o100755,
                       flags=0x8000 | (2 << 12))
        raw = _index(2, 'sha1', [entry], extensions=_extension(b'TREE', b'tree-data'))
        expected = _index(2, 'sha1', [_entry(
            b'src/app.py', b'\x11' * 20, stat=ZERO_STAT, mode=0o100755,
            flags=0x8000 | (2 << 12))], extensions=_extension(b'TREE', b'tree-data'))
        self.assertEqual(git_integrity.canonical_index(raw, 'sha1'), expected)
        self.assertEqual(git_integrity.canonical_index(expected, 'sha1'), expected)

    def test_v3_sha256_preserves_extended_flags_path_stage_mode_and_extensions(self):
        stat = (11, 12, 13, 14, 15, 16, 17, 18, 19)
        entry = _entry(b'odd name.txt', b'\x22' * 32, stat=stat, mode=0o160000,
                       flags=0x4000 | (3 << 12), extended=0x6000)
        raw = _index(3, 'sha256', [entry],
                     extensions=_extension(b'WXYZ', b'optional-exact') +
                                _extension(b'REUC', b'resolved-exact'))
        canonical = git_integrity.canonical_index(raw, 'sha256')
        expected = _index(3, 'sha256', [_entry(
            b'odd name.txt', b'\x22' * 32, stat=ZERO_STAT, mode=0o160000,
            flags=0x4000 | (3 << 12), extended=0x6000)],
            extensions=_extension(b'WXYZ', b'optional-exact') +
                       _extension(b'REUC', b'resolved-exact'))
        self.assertEqual(canonical, expected)

    def test_unknown_optional_extension_payload_is_compared_exactly(self):
        raw = _index(2, 'sha1', [], extensions=_extension(b'WXYZ', b'payload-one'))
        changed = _index(2, 'sha1', [], extensions=_extension(b'WXYZ', b'payload-two'))
        self.assertEqual(git_integrity.canonical_index(raw, 'sha1'), raw)
        self.assertNotEqual(git_integrity.canonical_index(raw, 'sha1'),
                            git_integrity.canonical_index(changed, 'sha1'))

    def test_v2_and_v3_support_both_repository_object_formats(self):
        for version in (2, 3):
            for object_format, oid_length in (('sha1', 20), ('sha256', 32)):
                with self.subTest(version=version, object_format=object_format):
                    entry = _entry(b'src/app.py', b'\x11' * oid_length, stat=(1,) * 9)
                    raw = _index(version, object_format, [entry])
                    expected = _index(version, object_format, [_entry(
                        b'src/app.py', b'\x11' * oid_length)])
                    self.assertEqual(git_integrity.canonical_index(raw, object_format), expected)

    def test_malformed_and_unsupported_indexes_are_rejected(self):
        good = _index(2, 'sha1', [_entry(b'a', b'\x01' * 20)])
        v4 = _index(4, 'sha1', [])
        split = _index(2, 'sha1', [], extensions=_extension(b'link', b''))
        sparse = _index(2, 'sha1', [], extensions=_extension(b'sdir', b'payload'))
        required = _index(2, 'sha1', [], extensions=_extension(b'zzzz', b'payload'))
        bad_checksum = good[:-1] + bytes([good[-1] ^ 1])
        truncated = good[:-1]
        trailing = _index(2, 'sha1', []) + b'X'
        extension_overrun = _index(2, 'sha1', [])[:-20]
        extension_overrun += b'abcd' + struct.pack('>I', 100) + b'short'
        extension_overrun += _checksum(extension_overrun, 'sha1')
        bad_padding = _index(2, 'sha1', [])
        bad_padding = bad_padding[:-20] + b'bad'
        bad_padding += _checksum(bad_padding, 'sha1')
        reserved_v3 = _index(3, 'sha1', [_entry(
            b'a', b'\x01' * 20, flags=0x4000, extended=0x0001)])
        extended_v2 = _index(2, 'sha1', [_entry(
            b'a', b'\x01' * 20, flags=0x4000, extended=0x6000)])
        unsorted = _index(2, 'sha1', [_entry(b'b', b'\x02' * 20),
                                      _entry(b'a', b'\x01' * 20)])
        duplicate = _index(2, 'sha1', [_entry(b'a', b'\x01' * 20),
                                       _entry(b'a', b'\x02' * 20)])
        for name, data in {
                'header': b'NOPE' + good[4:],
                'v4': v4,
                'split': split,
                'sparse': sparse,
                'required': required,
                'checksum': bad_checksum,
                'truncation': truncated,
                'trailing': trailing,
                'extension-overrun': extension_overrun,
                'padding': bad_padding,
                'reserved-v3-flags': reserved_v3,
                'extended-v2': extended_v2,
                'unsorted': unsorted,
                'duplicate': duplicate}.items():
            with self.subTest(name=name):
                with self.assertRaises(git_integrity.GitIntegrityError):
                    git_integrity.canonical_index(data, 'sha1')

    def test_limit_and_object_format_are_enforced(self):
        raw = _index(2, 'sha1', [])
        with mock.patch.object(git_integrity, 'MAX_INDEX_BYTES', 16):
            with self.assertRaises(git_integrity.GitIntegrityError):
                git_integrity.canonical_index(raw, 'sha1')
        with self.assertRaises(git_integrity.GitIntegrityError):
            git_integrity.canonical_index(raw, 'sha256')
        with self.assertRaises(git_integrity.GitIntegrityError):
            git_integrity.canonical_index(raw, 'sha512')
        with self.assertRaises(git_integrity.GitIntegrityError):
            git_integrity.canonical_index(raw, [])


class RepositoryFixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='dst-git-integrity-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.home = self.root / 'home'
        self.home.mkdir()
        self.state = self.root / 'state'
        self.source = self.root / 'source'
        self.source.mkdir()
        (self.source / 'app.txt').write_text('base\n')
        (self.source / 'nested').mkdir()
        (self.source / 'nested' / 'data.txt').write_text('nested\n')
        self.git(self.source, 'init', '-q', '-b', 'main')
        self.git(self.source, 'add', '-A')
        self.commit(self.source, 'base')
        environment = {
            'HOME': str(self.home),
            'XDG_CONFIG_HOME': str(self.root / 'config'),
            'XDG_STATE_HOME': str(self.root / 'xdg-state'),
            'DEEPSEEK_TEAM_STATE_DIR': str(self.state),
        }
        patch = mock.patch.dict(os.environ, environment)
        patch.start()
        self.addCleanup(patch.stop)
        self.copy = workspace.create(self.source, self.state)

    def environment(self):
        return {
            'PATH': os.environ.get('PATH', '/usr/bin:/bin'),
            'HOME': str(self.home),
            'GIT_CONFIG_NOSYSTEM': '1',
            'GIT_CONFIG_GLOBAL': os.devnull,
            'GIT_TERMINAL_PROMPT': '0',
        }

    def git(self, root: Path, *args: str, ok=(0,)):
        result = subprocess.run(['git', '-c', 'core.hooksPath=' + os.devnull, '-C', str(root),
                                 *args], env=self.environment(), capture_output=True, check=False)
        self.assertIn(result.returncode, ok,
                      result.stderr.decode('utf-8', 'replace'))
        return result.stdout

    def commit(self, root: Path, message: str):
        self.git(root, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
                 'commit', '-qm', message)

    @property
    def index_path(self):
        return self.copy.path / '.git' / 'index'


class WorkspaceIntegrityTests(RepositoryFixture):
    def test_new_copy_records_version_object_format_and_v2_index(self):
        evidence = self.copy.metadata['git_integrity']
        self.assertEqual(evidence['version'], 2)
        self.assertEqual(evidence['object_format'], 'sha1')
        self.assertEqual(self.copy.metadata['git_digest'],
                         git_integrity.legacy_digest(self.copy.path))
        raw = self.index_path.read_bytes()
        self.assertEqual(raw[:4], b'DIRC')
        self.assertEqual(struct.unpack('>I', raw[4:8])[0], 2)
        git_integrity.verify(self.copy.path, evidence)

    def test_status_stat_refresh_preserves_integrity(self):
        before = self.index_path.read_bytes()
        info = (self.copy.path / 'app.txt').stat()
        os.utime(self.copy.path / 'app.txt',
                 ns=(info.st_atime_ns, info.st_mtime_ns + 2_000_000_000))
        self.git(self.copy.path, 'status', '--porcelain=v1')
        after = self.index_path.read_bytes()
        self.assertNotEqual(after, before, 'git status did not refresh the fixture stat cache')
        self.copy.verify()

    def test_read_only_checks_leave_raw_index_source_and_provenance_untouched(self):
        index_before = self.index_path.read_bytes()
        source_before = (self.source / 'app.txt').read_bytes()
        work_before = (self.copy.path / 'app.txt').read_bytes()
        record_before = (self.copy.directory / 'record.json').read_bytes()
        evidence_before = json.loads(json.dumps(self.copy.metadata['git_integrity']))
        self.assertEqual(self.copy.changes()[0], [])
        self.assertEqual(self.copy.diff_bytes(), b'')
        workspace.content_snapshot(self.copy)
        git_integrity.snapshot(self.copy.path)
        git_integrity.verify(self.copy.path, evidence_before)
        self.assertEqual(self.index_path.read_bytes(), index_before)
        self.assertEqual((self.source / 'app.txt').read_bytes(), source_before)
        self.assertEqual((self.copy.path / 'app.txt').read_bytes(), work_before)
        self.assertEqual((self.copy.directory / 'record.json').read_bytes(), record_before)
        self.assertEqual(self.copy.metadata['git_integrity'], evidence_before)

    def test_temporary_trusted_index_is_private_outside_git_and_removed(self):
        raw_before = self.index_path.read_bytes()
        private_root = self.root / 'tmp'
        private_root.mkdir(mode=0o700)
        with mock.patch.object(git_integrity.tempfile, 'gettempdir',
                               return_value=str(private_root)):
            with git_integrity.temporary_index(
                    self.copy.path, self.copy.metadata['base_head']) as env:
                temporary = Path(env['GIT_INDEX_FILE'])
                self.assertTrue(temporary.is_file())
                self.assertEqual(stat.S_IMODE(temporary.parent.stat().st_mode), 0o700)
                self.assertNotEqual(temporary.parent.resolve(),
                                    (self.copy.path / '.git').resolve())
        self.assertEqual(list(private_root.iterdir()), [])
        self.assertEqual(self.index_path.read_bytes(), raw_before)
        with mock.patch.object(git_integrity.tempfile, 'gettempdir',
                               return_value=str(self.copy.path / '.git')):
            with self.assertRaises(git_integrity.GitIntegrityError):
                with git_integrity.temporary_index(
                        self.copy.path, self.copy.metadata['base_head']):
                    self.fail('temporary index was created inside Git metadata')

    def test_trusted_index_entry_failure_uses_workspace_error(self):
        @contextmanager
        def fail_to_enter(*_args, **_kwargs):
            raise git_integrity.GitIntegrityError('private index cannot be created')
            yield  # pragma: no cover

        with mock.patch.object(git_integrity, 'temporary_index', fail_to_enter):
            with self.assertRaises(workspace.WorkspaceError):
                with self.copy._trusted_index():
                    self.fail('failed trusted-index entry became usable')

    def test_forged_stat_cache_cannot_hide_content(self):
        tracked = self.copy.path / 'app.txt'
        tracked.write_text('PWNED\n')
        # Keep the entry non-racy: Git deliberately rehashes an entry whose
        # mtime is not older than the index. A forged cache can avoid that.
        past = tracked.stat().st_mtime_ns - 5_000_000_000
        os.utime(tracked, ns=(past, past))
        _forge_stat(self.index_path, tracked, b'app.txt')
        forged_index = self.index_path.read_bytes()
        # The normalized evidence intentionally accepts stat-only index writes.
        self.copy.verify()
        environment = dict(self.environment(), GIT_OPTIONAL_LOCKS='0')
        normal = subprocess.run(
            ['git', '--no-pager', '-C', str(self.copy.path), 'diff', '--numstat', '-z',
             'HEAD', '--'], env=environment, capture_output=True, check=True).stdout
        self.assertEqual(normal, b'', 'forged stat fixture did not hide the changed bytes')
        names, _ignored = self.copy.changes()
        patch = self.copy.diff_bytes()
        self.assertIn('app.txt', names)
        self.assertIn(b'PWNED', patch)
        self.assertIn(b'base', patch)
        self.assertEqual(self.index_path.read_bytes(), forged_index)
        before = dict(workspace.content_snapshot(self.copy))
        tracked.write_text('second\n')
        after = workspace.content_snapshot(self.copy)
        self.assertNotEqual(before['app.txt'], after['app.txt'])

    def test_prepared_and_worker_deltas_remain_separate(self):
        prepared_source = self.source / 'prepared.properties'
        prepared_source.write_text('coordinator=prepared\n')
        self.assertEqual(workspace.import_paths(self.copy, ['prepared.properties']),
                         ['prepared.properties'])
        prepared, _ignored = self.copy.changes()
        baseline = workspace.content_snapshot(self.copy)
        (self.copy.path / 'app.txt').write_text('worker edit\n')
        worker = workspace.changed_since(self.copy, baseline)
        self.assertEqual(prepared, ['prepared.properties'])
        self.assertEqual(worker, ['app.txt'])
        self.assertEqual((self.copy.path / 'prepared.properties').read_text(),
                         'coordinator=prepared\n')

    def test_assume_unchanged_cannot_hide_result(self):
        tracked = self.copy.path / 'app.txt'
        self.git(self.copy.path, 'update-index', '--assume-unchanged', '--', 'app.txt')
        tracked.write_text('hidden\n')
        with self.assertRaises(workspace.WorkspaceError):
            self.copy.verify()
        with self.assertRaises(workspace.WorkspaceError):
            self.copy.diff_bytes()
        # A trusted index independent of the working index still exposes the bytes.
        with git_integrity.temporary_index(
                self.copy.path, self.copy.metadata['base_head']) as env:
            result = subprocess.run(
                ['git', '--no-pager', '-C', str(self.copy.path), 'diff', '--binary',
                 '--no-ext-diff', '--no-textconv', '--no-renames',
                 self.copy.metadata['base_head'], '--'],
                env=env, capture_output=True, check=True)
        self.assertIn(b'hidden', result.stdout)

    def test_flags_mode_entries_and_git_admin_changes_are_rejected(self):
        index = self.index_path
        head = self.copy.metadata['base_head']
        original_index = index.read_bytes()
        mutations = []

        def assume():
            self.git(self.copy.path, 'update-index', '--assume-unchanged', '--', 'app.txt')

        def skip():
            self.git(self.copy.path, 'update-index', '--skip-worktree', '--', 'app.txt')

        def mode():
            data = original_index
            start = _entry_start(data, b'app.txt')
            data = data[:start + 24] + struct.pack('>I', 0o100755) + data[start + 28:]
            index.write_bytes(_rewrite_checksum(data, 'sha1'))

        def stage():
            data = original_index
            start = _entry_start(data, b'app.txt')
            flags_offset = start + 60
            flags = struct.unpack('>H', data[flags_offset:flags_offset + 2])[0]
            flags = (flags & ~0x3000) | 0x1000
            index.write_bytes(_rewrite_checksum(
                data[:flags_offset] + struct.pack('>H', flags) + data[flags_offset + 2:], 'sha1'))

        def entry_removed():
            self.git(self.copy.path, 'rm', '--cached', '--quiet', '--', 'app.txt')

        def head_file():
            path = self.copy.path / '.git' / 'HEAD'
            path.write_bytes(path.read_bytes().replace(b'refs/heads/', b'refs/heads/forged-', 1))

        def config():
            with (self.copy.path / '.git' / 'config').open('ab') as stream:
                stream.write(b'\n[fixture]\n\tchanged = true\n')

        def exclude():
            path = self.copy.path / '.git' / 'info' / 'exclude'
            path.parent.mkdir(mode=0o700, exist_ok=True)
            path.write_bytes((self.original_exclude or b'') + b'admin-mutation\n')

        def refs():
            (self.copy.path / '.git' / 'refs' / 'heads' / 'extra').write_text(head + '\n')

        for target in (assume, skip, mode, stage, entry_removed, head_file,
                       config, exclude, refs):
            mutations.append((target.__name__, target))
        for name, mutate in mutations:
            with self.subTest(name=name):
                mutate()
                with self.assertRaises(workspace.WorkspaceError):
                    self.copy.verify()
                index.write_bytes(original_index)
                (self.copy.path / '.git' / 'HEAD').write_bytes(self.original_head)
                (self.copy.path / '.git' / 'config').write_bytes(self.original_config)
                exclude_path = self.copy.path / '.git' / 'info' / 'exclude'
                if self.original_exclude is None:
                    exclude_path.unlink(missing_ok=True)
                else:
                    exclude_path.write_bytes(self.original_exclude)
                (self.copy.path / '.git' / 'refs' / 'heads' / 'extra').unlink(missing_ok=True)
                self.copy.verify()

    def setUp(self):
        super().setUp()
        self.original_head = (self.copy.path / '.git' / 'HEAD').read_bytes()
        self.original_config = (self.copy.path / '.git' / 'config').read_bytes()
        exclude = self.copy.path / '.git' / 'info' / 'exclude'
        self.original_exclude = exclude.read_bytes() if exclude.exists() else None

    def test_legacy_digest_upgrades_only_after_exact_match_under_owner_lock(self):
        record_path = self.copy.directory / 'record.json'
        record = json.loads(record_path.read_text())
        legacy = git_integrity.legacy_digest(self.copy.path)
        record.pop('git_integrity', None)
        record['git_digest'] = legacy
        record_path.write_text(json.dumps(record))
        loaded = workspace.load(self.state, self.copy.id)
        loaded.verify()
        self.assertNotIn('git_integrity', loaded.metadata)
        workspace.content_snapshot(loaded)
        self.assertNotIn('git_integrity', loaded.metadata)
        with loaded.lock():
            self.assertEqual(loaded.metadata['git_integrity']['version'], 2)
        saved = json.loads(record_path.read_text())
        self.assertEqual(saved['git_integrity']['version'], 2)
        self.assertEqual(saved['git_digest'], legacy)

    def test_legacy_digest_mismatch_never_upgrades(self):
        record_path = self.copy.directory / 'record.json'
        record = json.loads(record_path.read_text())
        record.pop('git_integrity', None)
        record['git_digest'] = '0' * 64
        record_path.write_text(json.dumps(record))
        loaded = workspace.load(self.state, self.copy.id)
        with self.assertRaises(workspace.WorkspaceError):
            loaded.verify()
        with self.assertRaises(workspace.WorkspaceError):
            with loaded.lock():
                self.fail('mismatched legacy copy became launchable')
        self.assertNotIn('git_integrity', json.loads(record_path.read_text()))

    def test_malformed_legacy_digest_is_rejected_without_typeerror(self):
        record_path = self.copy.directory / 'record.json'
        record = json.loads(record_path.read_text())
        record.pop('git_integrity', None)
        record['git_digest'] = 'not-a-digest-\u00e9'
        record_path.write_text(json.dumps(record))
        loaded = workspace.load(self.state, self.copy.id)
        with self.assertRaises(workspace.WorkspaceError):
            loaded.verify()
        from codex_deepseek_team import workspace_gc
        with self.assertRaises(workspace.WorkspaceError):
            workspace_gc._capture(loaded)
        self.assertNotIn('git_integrity', json.loads(record_path.read_text()))

    def test_malformed_versioned_evidence_is_rejected(self):
        good = self.copy.metadata['git_integrity']
        malformed = (
            None,
            {},
            dict(good, extra=True),
            dict(good, version=1),
            dict(good, version=2.0),
            dict(good, object_format=[]),
            dict(good, digest='0' * 63),
            dict(good, digest='A' * 64),
        )
        for evidence in malformed:
            with self.subTest(evidence=evidence):
                with self.assertRaises(git_integrity.GitIntegrityError):
                    git_integrity.verify(self.copy.path, evidence)

    def test_unsupported_and_malformed_indexes_preserve_raw_copy(self):
        bad = b'DIRC' + struct.pack('>II', 4, 0)
        bad += _checksum(bad, 'sha1')
        self.index_path.write_bytes(bad)
        with self.assertRaises(workspace.WorkspaceError):
            self.copy.verify()
        self.assertEqual(self.index_path.read_bytes(), bad)
        self.assertEqual((self.copy.path / 'app.txt').read_text(), 'base\n')

    def test_capture_refuses_assume_unchanged_hiding(self):
        from codex_deepseek_team import workspace_gc
        self.git(self.copy.path, 'update-index', '--assume-unchanged', '--', 'app.txt')
        (self.copy.path / 'app.txt').write_text('hidden from capture\n')
        with self.assertRaises(workspace.WorkspaceError):
            workspace_gc._capture(self.copy)
        self.assertTrue(self.copy.path.is_dir())

    def test_unprivileged_capture_reads_forged_stat_content(self):
        from tests import test_gc_adversarial
        from codex_deepseek_team import workspace_gc
        test_gc_adversarial._skip_without_namespace(self, 'capture')
        tracked = self.copy.path / 'app.txt'
        tracked.write_text('captured despite forged stat\n')
        _forge_stat(self.index_path, tracked, b'app.txt')
        self.copy.verify()
        patch = workspace_gc._capture(self.copy)
        self.assertIn(b'captured despite forged stat', patch)
        self.assertIn(b'base', patch)


class Sha256RepositoryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='dst-git-sha256-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        environment = {
            'PATH': os.environ.get('PATH', '/usr/bin:/bin'),
            'HOME': str(self.root / 'home'),
            'GIT_CONFIG_NOSYSTEM': '1',
            'GIT_CONFIG_GLOBAL': os.devnull,
            'GIT_TERMINAL_PROMPT': '0',
        }
        self.environment = environment
        (self.repo / 'app.txt').write_text('base\n')
        self.git('init', '-q', '--object-format=sha256', '-b', 'main')
        self.git('add', '-A')
        self.git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
                 'commit', '-qm', 'base')

    def git(self, *args: str):
        result = subprocess.run(['git', '-C', str(self.repo), *args], env=self.environment,
                                capture_output=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr.decode('utf-8', 'replace'))
        return result.stdout

    def test_sha256_snapshot_and_trusted_temporary_index(self):
        head = self.git('rev-parse', '--verify', 'HEAD').strip().decode('ascii')
        evidence = git_integrity.snapshot(self.repo)
        self.assertEqual(evidence['object_format'], 'sha256')
        raw = (self.repo / '.git' / 'index').read_bytes()
        canonical = git_integrity.canonical_index(raw, 'sha256')
        self.assertEqual(git_integrity.canonical_index(canonical, 'sha256'), canonical)
        (self.repo / 'app.txt').write_text('sha256 changed\n')
        index_before = (self.repo / '.git' / 'index').read_bytes()
        with git_integrity.temporary_index(self.repo, head) as env:
            result = subprocess.run(
                ['git', '--no-pager', '-C', str(self.repo), 'diff', '--numstat', '-z',
                 head, '--'], env=env, capture_output=True, check=True)
        self.assertIn(b'app.txt', result.stdout)
        self.assertEqual((self.repo / '.git' / 'index').read_bytes(), index_before)


if __name__ == '__main__':
    unittest.main()
