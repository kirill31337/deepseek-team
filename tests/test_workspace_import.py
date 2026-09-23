"""Imports must never follow worker-controlled paths into host files."""
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from codex_deepseek_team import development, sandbox, workspace


class WorkspaceImportTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='dst-import-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / 'source'
        self.source.mkdir()
        self.state = self.root / 'state'
        self.original = self.source / 'sample.txt'
        self.original.write_text('prepared source\n')
        for args in [('init', '-q'), ('add', '.'),
                     ('-c', 'user.name=Test', '-c', 'user.email=test@example.test',
                      'commit', '-qm', 'fixture')]:
            subprocess.run(['git', '-C', str(self.source), *args],
                           check=True, capture_output=True)
        self.copy = workspace.create(self.source, self.state)
        self.target = self.copy.path / 'sample.txt'
        self.outside = self.root / 'outside.txt'
        self.outside.write_text('host sentinel\n')
        self.outside.chmod(0o640)

    def assert_outside_preserved(self):
        self.assertEqual(self.outside.read_text(), 'host sentinel\n')
        self.assertEqual(stat.S_IMODE(self.outside.stat().st_mode), 0o640)

    def test_normal_import_preserves_mode_and_records_preparation(self):
        self.original.write_text('changed source\n')
        self.original.chmod(0o751)
        self.assertEqual(workspace.import_paths(self.copy, ['sample.txt']), ['sample.txt'])
        self.assertEqual(self.target.read_text(), 'changed source\n')
        self.assertEqual(stat.S_IMODE(self.target.stat().st_mode), 0o751)
        self.assertEqual(workspace.load(self.state, self.copy.id).metadata['prepared_paths'],
                         ['sample.txt'])
        self.copy.verify()

    def test_new_nested_destination_is_created_inside_copy(self):
        source = self.source / 'nested' / 'with space.txt'
        source.parent.mkdir()
        source.write_text('nested source')
        workspace.import_paths(self.copy, ['nested/with space.txt'])
        self.assertEqual((self.copy.path / 'nested/with space.txt').read_text(), 'nested source')

    def test_partial_import_records_files_published_before_later_failure(self):
        self.original.write_text('new prepared source\n')
        os.mkfifo(self.source / 'pipe', 0o600)
        with self.assertRaises(workspace.WorkspaceError):
            workspace.import_paths(self.copy, ['sample.txt', 'pipe'])
        self.assertEqual(self.target.read_text(), 'new prepared source\n')
        self.assertEqual(workspace.load(self.state, self.copy.id).metadata['prepared_paths'],
                         ['sample.txt'])
        self.assertFalse((self.copy.path / 'pipe').exists())

    def test_directory_sync_failure_still_records_published_file(self):
        self.original.write_text('published before sync failure\n')
        original_fsync = os.fsync
        directory_inode = self.copy.path.stat().st_ino

        def fail_directory_sync(fd):
            if os.fstat(fd).st_ino == directory_inode:
                raise OSError('injected directory sync failure')
            return original_fsync(fd)

        with patch.object(workspace.os, 'fsync', side_effect=fail_directory_sync):
            with self.assertRaises(workspace.WorkspaceError):
                workspace.import_paths(self.copy, ['sample.txt'])
        self.assertEqual(self.target.read_text(), 'published before sync failure\n')
        self.assertEqual(workspace.load(self.state, self.copy.id).metadata['prepared_paths'],
                         ['sample.txt'])

    def test_nul_path_is_rejected_before_any_file_is_imported(self):
        self.original.write_text('must not be imported\n')
        with self.assertRaises(workspace.WorkspaceError) as caught:
            workspace.import_paths(self.copy, ['sample.txt', 'bad\0path'])
        self.assertEqual(caught.exception.code, 64)
        self.assertEqual(self.target.read_text(), 'prepared source\n')
        self.assertNotIn('prepared_paths', workspace.load(self.state, self.copy.id).metadata)

    def test_destination_symlink_cannot_overwrite_host_file(self):
        self.target.unlink()
        self.target.symlink_to(self.outside)
        with self.assertRaises(workspace.WorkspaceError):
            workspace.import_paths(self.copy, ['sample.txt'])
        self.assert_outside_preserved()
        self.assertTrue(self.target.is_symlink())

    def test_broken_destination_symlink_cannot_create_host_file(self):
        missing = self.root / 'missing.txt'
        self.target.unlink()
        self.target.symlink_to(missing)
        with self.assertRaises(workspace.WorkspaceError):
            workspace.import_paths(self.copy, ['sample.txt'])
        self.assertFalse(missing.exists())

    def test_hardlinked_destination_cannot_change_external_inode(self):
        self.target.unlink()
        os.link(self.outside, self.target)
        with self.assertRaises(workspace.WorkspaceError):
            workspace.import_paths(self.copy, ['sample.txt'])
        self.assert_outside_preserved()

    def test_source_parent_symlink_cannot_import_external_data(self):
        (self.source / 'linked').symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(workspace.WorkspaceError):
            workspace.import_paths(self.copy, ['linked/outside.txt'])
        self.assertFalse((self.copy.path / 'linked/outside.txt').exists())
        self.assert_outside_preserved()

    def test_destination_parent_symlink_cannot_create_external_directories(self):
        nested = self.source / 'linked/new/sample.txt'
        nested.parent.mkdir(parents=True)
        nested.write_text('prepared nested source')
        external = self.root / 'external-directory'
        external.mkdir()
        (self.copy.path / 'linked').symlink_to(external, target_is_directory=True)
        with self.assertRaises(workspace.WorkspaceError):
            workspace.import_paths(self.copy, ['linked/new/sample.txt'])
        self.assertEqual(list(external.iterdir()), [])

    def test_git_metadata_cannot_be_imported(self):
        before = (self.copy.path / '.git/config').read_bytes()
        with self.assertRaises(workspace.WorkspaceError):
            workspace.import_paths(self.copy, ['.git/config'])
        self.assertEqual((self.copy.path / '.git/config').read_bytes(), before)
        self.copy.verify()

    def test_source_symlink_fifo_and_credential_names_are_rejected(self):
        linked = self.source / 'linked.txt'
        linked.symlink_to(self.outside)
        os.mkfifo(self.source / 'pipe', 0o600)
        (self.source / '.env').write_text('synthetic secret')
        for name in ('linked.txt', 'pipe', '.env', '../outside.txt', str(self.outside)):
            with self.subTest(name=name), self.assertRaises(workspace.WorkspaceError):
                workspace.import_paths(self.copy, [name])
        self.assert_outside_preserved()

    def test_destination_fifo_is_rejected_without_blocking(self):
        self.target.unlink()
        os.mkfifo(self.target, 0o600)
        with self.assertRaises(workspace.WorkspaceError):
            workspace.import_paths(self.copy, ['sample.txt'])
        self.assertTrue(stat.S_ISFIFO(self.target.lstat().st_mode))

    def test_failed_atomic_replace_leaves_destination_and_metadata_unchanged(self):
        self.original.write_text('new data')
        with patch.object(workspace.os, 'replace', side_effect=OSError('injected failure')):
            with self.assertRaises(workspace.WorkspaceError):
                workspace.import_paths(self.copy, ['sample.txt'])
        self.assertEqual(self.target.read_text(), 'prepared source\n')
        self.assertNotIn('prepared_paths', workspace.load(self.state, self.copy.id).metadata)
        self.assertEqual(sorted(p.name for p in self.copy.path.iterdir()), ['.git', 'sample.txt'])

    def test_concurrent_destination_edit_is_preserved(self):
        copy_bytes = workspace.shutil.copyfileobj

        def edit_during_copy(source, destination):
            copy_bytes(source, destination)
            self.target.write_text('concurrent edit\n')

        with patch.object(workspace.shutil, 'copyfileobj', side_effect=edit_during_copy):
            with self.assertRaises(workspace.WorkspaceError):
                workspace.import_paths(self.copy, ['sample.txt'])
        self.assertEqual(self.target.read_text(), 'concurrent edit\n')
        self.assertNotIn('prepared_paths', workspace.load(self.state, self.copy.id).metadata)
        self.assertEqual(sorted(p.name for p in self.copy.path.iterdir()), ['.git', 'sample.txt'])

    def test_real_sandbox_link_cannot_cross_import_boundary(self):
        try:
            backend = sandbox.probe_backend()
        except sandbox.SandboxError as error:
            if os.environ.get('DEEPSEEK_TEAM_REQUIRE_LIVE') == '1':
                self.fail(str(error))
            self.skipTest('Live Bubblewrap unavailable: ' + str(error))
        home, control = self.root / 'home', self.root / 'control'
        home.mkdir()
        control.mkdir()
        args = development.layout(backend, self.copy.path, home, control,
                                  ['/usr/bin/python3'], writable=True)
        script = ("import pathlib,sys; target=pathlib.Path(sys.argv[1]); "
                  "assert not target.exists(); p=pathlib.Path('sample.txt'); "
                  "p.unlink(); p.symlink_to(target)")
        subprocess.run([*args, '--', '/usr/bin/python3', '-c', script, str(self.outside)],
                       env={'HOME': str(home), 'PATH': '/usr/bin:/bin'},
                       check=True, capture_output=True, timeout=15)
        with self.assertRaises(workspace.WorkspaceError):
            workspace.import_paths(self.copy, ['sample.txt'])
        self.assert_outside_preserved()


if __name__ == '__main__':
    unittest.main()
