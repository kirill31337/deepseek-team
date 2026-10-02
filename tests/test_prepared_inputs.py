"""Prepared ignored inputs: explicit fingerprints without exposing content.

Only explicitly registered ``copy.metadata['prepared_paths']`` entries are
fingerprinted. Unregistered ignored artifacts never leak into prepared-input
evidence, and no input content is ever stored.
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from codex_deepseek_team import prepared_inputs, workspace


class PreparedInputsCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='dst-prepared-')
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.source = self.base / 'source'
        self.source.mkdir()
        (self.source / '.gitignore').write_text(
            'local.properties\nother.ignored\ncaches/\nhostlink\n')
        (self.source / 'sample.txt').write_text('tracked\n')
        self._git('init', '-q')
        self._git('add', '.gitignore', 'sample.txt')
        self._git('-c', 'user.name=Tests', '-c', 'user.email=tests@example.invalid',
                  'commit', '-qm', 'base')
        self.state = self.base / 'state'
        self.copy = workspace.create(self.source, self.state)

    def _git(self, *args):
        subprocess.run(['git', '-C', str(self.source), *args], check=True,
                       capture_output=True)

    def register(self, paths):
        self.copy.metadata['prepared_paths'] = list(paths)

    def write(self, name, content, mode=0o644):
        target = self.copy.path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        target.chmod(mode)
        return target

    # -- registration scope -------------------------------------------------
    def test_unregistered_ignored_file_is_not_fingerprinted(self):
        self.write('local.properties', 'sdk.dir=/host\n')
        self.write('other.ignored', 'noise\n')
        self.assertEqual(prepared_inputs.input_snapshot(self.copy), {})
        self.assertEqual(prepared_inputs.input_changes(self.copy, {}), [])

    def test_unregistered_ignored_change_is_not_reported(self):
        before = prepared_inputs.input_snapshot(self.copy)
        self.write('other.ignored', 'created after baseline\n')
        self.assertEqual(prepared_inputs.input_snapshot(self.copy), {})
        self.assertEqual(prepared_inputs.input_changes(self.copy, before), [])

    # -- change detection ---------------------------------------------------
    def test_registered_ignored_file_change_is_detected_without_content(self):
        secret = 'sdk.dir=/very/secret/host\n'
        self.write('local.properties', secret)
        self.register(['local.properties'])
        before = prepared_inputs.input_snapshot(self.copy)
        self.assertEqual(list(before), ['local.properties'])
        self.assertNotIn('secret', before['local.properties'])
        self.assertNotIn('/very/secret', repr(before))
        self.write('local.properties', 'sdk.dir=/other\n')
        self.assertEqual(prepared_inputs.input_changes(self.copy, before),
                         ['local.properties'])

    def test_unchanged_registered_input_reports_no_change(self):
        self.write('local.properties', 'x=1\n')
        self.register(['local.properties'])
        before = prepared_inputs.input_snapshot(self.copy)
        self.assertEqual(prepared_inputs.input_snapshot(self.copy), before)
        self.assertEqual(prepared_inputs.input_changes(self.copy, before), [])

    def test_mode_change_is_detected(self):
        path = self.write('local.properties', 'x=1\n', mode=0o644)
        self.register(['local.properties'])
        before = prepared_inputs.input_snapshot(self.copy)
        path.chmod(0o600)
        self.assertEqual(prepared_inputs.input_changes(self.copy, before),
                         ['local.properties'])

    def test_deletion_and_recreation_are_reported(self):
        path = self.write('local.properties', 'x=1\n')
        self.register(['local.properties'])
        before = prepared_inputs.input_snapshot(self.copy)
        path.unlink()
        self.assertEqual(
            prepared_inputs.input_snapshot(self.copy)['local.properties'], 'missing')
        self.assertEqual(prepared_inputs.input_changes(self.copy, before),
                         ['local.properties'])
        self.write('local.properties', 'x=2\n')
        self.assertEqual(prepared_inputs.input_changes(self.copy, before),
                         ['local.properties'])

    def test_registered_missing_input_has_missing_marker(self):
        self.register(['never-created.properties'])
        self.assertEqual(prepared_inputs.input_snapshot(self.copy),
                         {'never-created.properties': 'missing'})

    def test_registered_directory_is_bounded_not_enumerated(self):
        self.write('caches/secret.txt', 'sensitive\n')
        self.register(['caches'])
        snapshot = prepared_inputs.input_snapshot(self.copy)
        self.assertEqual(list(snapshot), ['caches'])
        (self.copy.path / 'caches/secret.txt').write_text('changed\n')
        self.assertEqual(prepared_inputs.input_changes(self.copy, snapshot), [])

    def test_imported_paths_registration_is_fingerprinted(self):
        (self.source / 'local.properties').write_text('sdk.dir=/host\n')
        workspace.import_paths(self.copy, ['local.properties'])
        before = prepared_inputs.input_snapshot(self.copy)
        self.assertIn('local.properties', before)
        (self.copy.path / 'local.properties').write_text('sdk.dir=/usr\n')
        self.assertEqual(prepared_inputs.input_changes(self.copy, before),
                         ['local.properties'])

    # -- rejection before reading ------------------------------------------
    def test_malformed_registrations_are_rejected(self):
        for bad in ['local.properties', [1], [''], ['../escape'], ['/etc/passwd'],
                    ['.git/config'], ['a/../../b'], ['\x00bad']]:
            self.register(bad)
            with self.assertRaises(workspace.WorkspaceError):
                prepared_inputs.input_snapshot(self.copy)

    def test_credential_like_registration_is_rejected_before_reading(self):
        self.write('auth.json', '{"token":"topsecret"}\n')
        self.register(['auth.json'])
        with self.assertRaises(workspace.WorkspaceError):
            prepared_inputs.input_snapshot(self.copy)
        self.register([Path('keys') / 'server.key'])
        with self.assertRaises(workspace.WorkspaceError):
            prepared_inputs.input_snapshot(self.copy)

    def test_escaping_absolute_symlink_is_rejected(self):
        outside = self.base / 'outside.properties'
        outside.write_text('host secret\n')
        (self.copy.path / 'local.properties').symlink_to(outside)
        self.register(['local.properties'])
        with self.assertRaises(workspace.WorkspaceError):
            prepared_inputs.input_snapshot(self.copy)
        self.assertEqual(outside.read_text(), 'host secret\n')

    def test_escaping_relative_symlink_is_rejected(self):
        outside = self.base / 'outside.properties'
        outside.write_text('host secret\n')
        (self.copy.path / 'local.properties').symlink_to(
            os.path.relpath(outside, self.copy.path))
        self.register(['local.properties'])
        with self.assertRaises(workspace.WorkspaceError):
            prepared_inputs.input_snapshot(self.copy)
        self.assertEqual(outside.read_text(), 'host secret\n')

    def test_symlinked_parent_escape_is_rejected_before_reading(self):
        outside_dir = self.base / 'hostdata'
        outside_dir.mkdir()
        secret = outside_dir / 'secret.txt'
        secret.write_text('host secret\n')
        (self.copy.path / 'hostlink').symlink_to(outside_dir)
        self.register(['hostlink/secret.txt'])
        with self.assertRaises(workspace.WorkspaceError):
            prepared_inputs.input_snapshot(self.copy)
        self.assertEqual(secret.read_text(), 'host secret\n')

    def test_internal_symlink_is_recorded_without_following(self):
        target = self.copy.path / 'sample.txt'
        link = self.copy.path / 'local.properties'
        link.symlink_to('sample.txt')
        self.register(['local.properties'])
        before = prepared_inputs.input_snapshot(self.copy)
        self.assertIn('local.properties', before)
        # Changing the linked file's content must not alter the link fingerprint.
        target.write_text('changed tracked content\n')
        self.assertEqual(prepared_inputs.input_changes(self.copy, before), [])
        # Retargeting the link is a change.
        link.unlink()
        (self.copy.path / 'other.internal').write_text('x\n')
        link.symlink_to('other.internal')
        self.assertEqual(prepared_inputs.input_changes(self.copy, before),
                         ['local.properties'])

    def test_non_mapping_baseline_is_rejected(self):
        with self.assertRaises(workspace.WorkspaceError):
            prepared_inputs.input_changes(self.copy, None)


if __name__ == '__main__':
    unittest.main()
