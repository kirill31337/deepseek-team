"""Focused registration tests for the Qwen (DSH) bridge profile row.

These exercise dsh_config's install/status/remove APIs against a temporary DSH
home only; no DSH process or provider is involved. Installation must resolve an
absolute package entrypoint, preserve every foreign patch byte, refuse ambiguous
profiles and unsafe files, and migrate only the package-owned marker block.
"""
import json
import os
from pathlib import Path
import shlex
import sys
import tempfile
import unittest
from unittest import mock

from codex_deepseek_team import dsh_config
from codex_deepseek_team.config import ConfigError


FOREIGN_PATCH = (
    '# user profile patch\n'
    '- id: agent-default-model\n'
    '  config:\n'
    '    provider: qwen-cloud\n'
    '    model: qwen4-max\n'
    '- id: my-plugin\n'
    "  name: '/opt/plugins/mine.js'\n"
)


def write_launcher(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('#!/bin/sh\nexec ' + shlex.quote(sys.executable) +
                    ' -m codex_deepseek_team "$@"\n', encoding='utf-8')
    path.chmod(0o700)
    return path


def legacy_block(entrypoint, plugin):
    """The pre-fix bare-row rendering: an id-only row that DSH skips as an update.

    ``_render_block`` no longer emits this shape, so it is written literally
    here; it must still count as package-owned evidence so that
    ``hooks refresh`` migrates an existing installation.
    """
    return (dsh_config.START_MARKER + '\n'
            + '- id: deepseek-team-qwen\n'
            + '  name: ' + json.dumps(str(plugin)) + '\n'
            + '  config:\n'
            + '    entrypoint: ' + json.dumps(str(entrypoint)) + '\n'
            + '    runtime: qwen\n'
            + dsh_config.END_MARKER + '\n')


class QwenRegistrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='dst-qwen-registration-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        previous = os.getcwd()
        self.addCleanup(os.chdir, previous)
        os.chdir(self.root)
        self.bin = self.root / 'entry point'
        self.entrypoint = write_launcher(self.bin / 'deepseek-team')
        self.home = self.root / 'home'
        self.home.mkdir()
        self.dsh = self.root / 'dsh-home'
        self.profile = self.dsh / 'profiles' / 'web'
        self.profile.mkdir(parents=True)
        (self.profile / 'package.json').write_text('{"name": "dsh-profile-web"}')
        self.patch = self.profile / 'cordis.patch.yml'
        environment = mock.patch.dict(os.environ, {
            'HOME': str(self.home),
            'DSH_HOME': str(self.dsh),
            'DEEPSEEK_TEAM_DSH_PROFILE': '',
            'CODEX_HOME': str(self.root / 'codex-home'),
            'XDG_CONFIG_HOME': str(self.root / 'xdg-config'),
            'XDG_STATE_HOME': str(self.root / 'xdg-state'),
            'DEEPSEEK_TEAM_STATE_DIR': str(self.root / 'state'),
            'PATH': str(self.bin) + os.pathsep + '/usr/bin:/bin',
        })
        environment.start()
        self.addCleanup(environment.stop)

    def test_install_creates_owned_row_and_preserves_foreign_bytes(self):
        self.patch.write_text(FOREIGN_PATCH)
        self.patch.chmod(0o640)
        self.assertTrue(dsh_config.install())
        text = self.patch.read_text()
        self.assertTrue(text.startswith(FOREIGN_PATCH))
        self.assertIn(dsh_config.START_MARKER, text)
        self.assertIn('- id: deepseek-team-qwen', text)
        self.assertIn(str(dsh_config.plugin_path()), text)
        self.assertIn(str(self.entrypoint), text)
        self.assertEqual(self.patch.stat().st_mode & 0o777, 0o640)
        self.assertTrue(dsh_config.owned())
        self.assertTrue(dsh_config.status())

    def test_install_is_idempotent(self):
        self.assertTrue(dsh_config.install())
        first = self.patch.read_bytes()
        self.assertFalse(dsh_config.install())
        self.assertEqual(self.patch.read_bytes(), first)

    def test_remove_preserves_foreign_content_and_is_idempotent(self):
        self.patch.write_text(FOREIGN_PATCH)
        dsh_config.install()
        self.assertTrue(dsh_config.remove())
        self.assertEqual(self.patch.read_text(), FOREIGN_PATCH)
        self.assertFalse(dsh_config.remove())
        self.assertFalse(dsh_config.owned())
        self.assertFalse(dsh_config.status())

    def test_missing_patch_file_is_created_and_removed_cleanly(self):
        self.assertTrue(dsh_config.install())
        self.assertTrue(self.patch.is_file())
        self.assertTrue(dsh_config.remove())
        self.assertEqual(self.patch.read_text(), '')

    def test_refresh_migrates_stale_entrypoint(self):
        dsh_config.install()
        stale = self.root / 'old' / 'deepseek-team'
        write_launcher(stale)
        dsh_config.install(executable=stale)
        self.assertFalse(dsh_config.status())  # current PATH entrypoint differs
        self.assertTrue(dsh_config.status(executable=stale))
        self.assertEqual(dsh_config.owned_entrypoints(), (stale,))
        dsh_config.install()  # refresh back to the resolved PATH entrypoint
        self.assertTrue(dsh_config.status())

    def test_status_accepts_recorded_entrypoint_when_path_unavailable(self):
        dsh_config.install()
        with mock.patch.dict(os.environ, {'PATH': '/nonexistent'}):
            self.assertTrue(dsh_config.status())

    def test_duplicate_markers_are_refused_without_mutation(self):
        dsh_config.install()
        original = self.patch.read_bytes()
        self.patch.write_bytes(original + original[original.find(dsh_config.START_MARKER.encode()):])
        with self.assertRaises(ConfigError):
            dsh_config.install()
        with self.assertRaises(ConfigError):
            dsh_config.remove()

    def test_out_of_order_markers_are_refused(self):
        self.patch.write_text(dsh_config.END_MARKER + '\n' + dsh_config.START_MARKER + '\n')
        with self.assertRaises(ConfigError):
            dsh_config.install()

    def test_marker_must_own_its_line(self):
        self.patch.write_text('x ' + dsh_config.START_MARKER + '\n- id: foreign\n')
        with self.assertRaises(ConfigError):
            dsh_config.install()

    def test_foreign_row_with_same_id_is_refused(self):
        self.patch.write_text('- id: deepseek-team-qwen\n  name: /opt/other/index.js\n')
        with self.assertRaises(ConfigError):
            dsh_config.install()
        self.assertFalse(dsh_config.owned())

    def test_symlinked_patch_is_refused(self):
        target = self.root / 'real.yml'
        target.write_text(FOREIGN_PATCH)
        self.patch.symlink_to(target)
        with self.assertRaises(ConfigError):
            dsh_config.install()
        self.assertEqual(target.read_text(), FOREIGN_PATCH)

    def test_ambiguous_profiles_require_explicit_selection(self):
        other = self.dsh / 'profiles' / 'headless'
        other.mkdir()
        (other / 'package.json').write_text('{}')
        with self.assertRaises(ConfigError):
            dsh_config.install()
        with mock.patch.dict(os.environ, {'DEEPSEEK_TEAM_DSH_PROFILE': 'headless'}):
            self.assertTrue(dsh_config.install())
            self.assertTrue((other / 'cordis.patch.yml').is_file())
        self.assertFalse((self.profile / 'cordis.patch.yml').exists())

    def test_explicit_profile_name_rejects_traversal(self):
        with mock.patch.dict(os.environ, {'DEEPSEEK_TEAM_DSH_PROFILE': '../escape'}):
            with self.assertRaises(ConfigError):
                dsh_config.install()

    def test_absent_dsh_home_is_a_clean_noop(self):
        with mock.patch.dict(os.environ, {'DSH_HOME': str(self.root / 'absent')}):
            self.assertIsNone(dsh_config.detect_profile())
            self.assertFalse(dsh_config.owned())
            self.assertFalse(dsh_config.status())
            self.assertFalse(dsh_config.remove())
            with self.assertRaises(ConfigError):
                dsh_config.install()

    def test_entrypoint_resolution_failure_changes_nothing(self):
        with mock.patch.dict(os.environ, {'PATH': '/nonexistent'}):
            with self.assertRaises(ConfigError):
                dsh_config.install()
        self.assertFalse(self.patch.exists())

    def test_paths_with_spaces_are_quoted(self):
        self.assertTrue(dsh_config.install())
        text = self.patch.read_text()
        self.assertIn('name: "' + str(dsh_config.plugin_path()) + '"', text)
        self.assertIn('entrypoint: "' + str(self.entrypoint) + '"', text)

    def test_installed_block_is_an_insert_patch(self):
        self.assertTrue(dsh_config.install())
        text = self.patch.read_text()
        begin = text.index(dsh_config.START_MARKER)
        end = text.index(dsh_config.END_MARKER) + len(dsh_config.END_MARKER)
        if text[end:end + 1] == '\n':
            end += 1
        block = text[begin:end]
        lines = block.split('\n')
        self.assertEqual(lines[0], dsh_config.START_MARKER)
        self.assertEqual(lines[1], '- insert:')
        self.assertEqual(lines[2], '    - id: deepseek-team-qwen')
        self.assertEqual(lines[3], '      name: "' + str(dsh_config.plugin_path()) + '"')
        self.assertEqual(lines[4], '      config:')
        self.assertEqual(lines[5], '        entrypoint: "' + str(self.entrypoint) + '"')
        self.assertEqual(lines[6], '        runtime: qwen')
        self.assertEqual(lines[7], dsh_config.END_MARKER)
        self.assertEqual(
            block, dsh_config._render_block(str(self.entrypoint), dsh_config.plugin_path()))

    def test_legacy_bare_row_block_is_owned_and_migrated(self):
        plugin = dsh_config.plugin_path()
        legacy = legacy_block(self.entrypoint, plugin)
        self.patch.write_text(FOREIGN_PATCH + legacy)
        # Legacy ownership evidence keeps refresh able to migrate the row.
        self.assertTrue(dsh_config.owned())
        self.assertEqual(dsh_config.owned_entrypoints(), (self.entrypoint,))
        self.assertFalse(dsh_config.status())  # legacy shape is stale
        self.assertTrue(dsh_config.install())
        text = self.patch.read_text()
        self.assertTrue(text.startswith(FOREIGN_PATCH))
        self.assertEqual(text[len(FOREIGN_PATCH):],
                         dsh_config._render_block(str(self.entrypoint), plugin))
        self.assertIn('- insert:', text)
        self.assertTrue(dsh_config.status())
        self.assertFalse(dsh_config.install())  # migration is idempotent

    def test_status_no_path_fallback_requires_canonical_block(self):
        self.assertTrue(dsh_config.install())
        canonical = self.patch.read_text()
        with mock.patch.dict(os.environ, {'PATH': '/nonexistent'}):
            self.assertTrue(dsh_config.status())
        self.patch.write_text(
            FOREIGN_PATCH + legacy_block(self.entrypoint, dsh_config.plugin_path()))
        with mock.patch.dict(os.environ, {'PATH': '/nonexistent'}):
            self.assertTrue(dsh_config.owned())
            self.assertFalse(dsh_config.status())
        self.patch.write_text(canonical)
        with mock.patch.dict(os.environ, {'PATH': '/nonexistent'}):
            self.assertTrue(dsh_config.status())


if __name__ == '__main__':
    unittest.main()
