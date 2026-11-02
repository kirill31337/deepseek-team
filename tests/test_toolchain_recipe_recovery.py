"""Recovery diagnostics for cache-backed saved toolchain recipe replay."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import time
import unittest
from unittest import mock

from codex_deepseek_team import toolchains, workspace


def _git(root, *args, ok=(0,)):
    result = subprocess.run(['git', '-C', str(root), *args], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, check=False)
    if result.returncode not in ok:
        raise AssertionError(result.stderr.decode('utf-8', 'replace'))
    return result.stdout.decode('utf-8', 'replace')


def _commit(root, *args):
    _git(root, '-c', 'user.name=Toolchain', '-c', 'user.email=toolchain@example.test', *args)


class RecipeRecoveryFixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='dst-recipe-recovery-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        isolated = {
            'HOME': self.root / 'home',
            'CODEX_HOME': self.root / 'codex-home',
            'CLAUDE_CONFIG_DIR': self.root / 'claude-home',
            'XDG_CONFIG_HOME': self.root / 'xdg-config',
            'XDG_STATE_HOME': self.root / 'xdg-state',
            'TMPDIR': self.root / 'tmp',
        }
        for path in isolated.values():
            path.mkdir(parents=True)
        self.canaries = {
            isolated['CODEX_HOME'] / 'config.toml': 'codex-decoy-canary\n',
            isolated['CODEX_HOME'] / 'auth.json': 'codex-auth-decoy-canary\n',
            isolated['CLAUDE_CONFIG_DIR'] / 'settings.json': 'claude-decoy-canary\n',
            isolated['XDG_CONFIG_HOME'] / 'deepseek' / 'config.json': 'xdg-decoy-canary\n',
            isolated['XDG_STATE_HOME'] / 'deepseek' / 'state.json': 'state-decoy-canary\n',
        }
        for path, content in self.canaries.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        self.addCleanup(self._assert_canaries)

        environment = {name: str(path) for name, path in isolated.items()}
        patch = mock.patch.dict(os.environ, environment)
        patch.start()
        self.addCleanup(patch.stop)

        self.state = self.root / 'state'
        self.source = self.root / 'source'
        self.source.mkdir()
        (self.source / 'sample.txt').write_text('committed\n')
        _git(self.source, 'init', '-q', '-b', 'main')
        _git(self.source, 'add', '-A')
        _commit(self.source, 'commit', '-qm', 'base')
        self.copy = workspace.create(self.source, self.state)

    def _assert_canaries(self):
        for path, content in self.canaries.items():
            self.assertEqual(path.read_text(), content, path)

    def fresh_copy(self):
        return workspace.create(self.source, self.state)

    def reloaded(self, copy):
        return workspace.load(self.state, copy.id)

    def software(self, name='sdk', *, content=b'#!/bin/sh\nexit 0\n', mode=0o755):
        root = self.root / 'software' / name
        (root / 'bin').mkdir(parents=True)
        binary = root / 'bin' / 'tool'
        binary.write_bytes(content)
        binary.chmod(mode)
        (root / 'lib' / 'sub').mkdir(parents=True)
        (root / 'lib' / 'data.txt').write_text('data\n')
        (root / 'lib' / 'sub' / 'inner.txt').write_text('inner\n')
        return root

    def input_file(self, name='local.properties', content='sdk.dir=prepared\n', mode=0o640):
        path = self.root / 'inputs' / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        path.chmod(mode)
        return path

    def recipe_path(self, copy):
        key = hashlib.sha256(str(Path(copy.source).resolve()).encode()).hexdigest()
        return self.state / 'toolchain-recipes' / (key + '.json')

    def cache_manifest(self, cached):
        return self.state / 'toolchain-cache' / 'manifests' / (Path(cached).name + '.json')

    def write_recipe(self, copy, *, tools, copies):
        recipe_file = self.recipe_path(copy)
        payload = {
            'version': 1,
            'kind': 'toolchain-recipe',
            'source_project': str(Path(copy.source).resolve()),
            'saved_at': time.time(),
            'tools': tools,
            'copies': copies,
            'environment': {},
            'probes': [],
        }
        recipe_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        recipe_file.parent.chmod(0o700)
        recipe_file.write_text(json.dumps(payload))
        recipe_file.chmod(0o600)
        return recipe_file

    def assert_replay_guidance(self, message):
        self.assertIn('saved recipe was preserved', message)
        self.assertIn('owned copy was retained', message)
        self.assertIn('--tool NAME=HOST_DIR', message)
        self.assertIn('--copy SOURCE=REL_DEST', message)
        self.assertIn('--save-project', message)


class RecipeRecoveryDiagnosticsTests(RecipeRecoveryFixture):
    def test_missing_manifest_preserves_recipe_and_payload_with_guidance(self):
        tool = self.software(content=b'#!/bin/sh\necho original\n')
        toolchains.prepare(self.copy, tools={'sdk': tool}, save_project=True)
        recipe_file = self.recipe_path(self.copy)
        before = recipe_file.read_bytes()
        cached = Path(json.loads(before)['tools']['sdk']['source'])
        manifest = self.cache_manifest(cached)
        self.assertTrue(manifest.is_file())
        manifest.unlink()

        fresh = self.fresh_copy()
        with self.assertRaises(toolchains.ToolchainError) as caught:
            with fresh.lock():
                toolchains.apply_saved_recipe(fresh)

        message = str(caught.exception)
        self.assertIn('no manifest', message)
        self.assert_replay_guidance(message)
        self.assertEqual(caught.exception.code, 78)
        self.assertEqual(recipe_file.read_bytes(), before)
        self.assertTrue(cached.is_dir())
        self.assertEqual((cached / 'bin/tool').read_bytes(), b'#!/bin/sh\necho original\n')
        self.assertFalse((fresh.path / '.deepseek-tools').exists())
        self.assertNotIn('toolchain', self.reloaded(fresh).metadata)

    def test_altered_cached_content_preserves_recipe_and_reports_original_error(self):
        tool = self.software()
        toolchains.prepare(self.copy, tools={'sdk': tool}, save_project=True)
        recipe_file = self.recipe_path(self.copy)
        before = recipe_file.read_bytes()
        cached = Path(json.loads(before)['tools']['sdk']['source'])
        changed = cached / 'lib/data.txt'
        original_mode = stat.S_IMODE(changed.stat().st_mode)
        changed.chmod(0o600)
        changed.write_bytes(b'evil\n')
        changed.chmod(original_mode)

        fresh = self.fresh_copy()
        with self.assertRaises(toolchains.ToolchainError) as caught:
            with fresh.lock():
                toolchains.apply_saved_recipe(fresh)

        message = str(caught.exception)
        self.assertIn('content does not match its manifest', message)
        self.assert_replay_guidance(message)
        self.assertEqual(caught.exception.code, 78)
        self.assertEqual(recipe_file.read_bytes(), before)
        self.assertTrue(cached.is_dir())
        self.assertEqual(changed.read_bytes(), b'evil\n')
        self.assertFalse((fresh.path / '.deepseek-tools').exists())

    def test_known_missing_payload_still_invalidates_only_the_stale_recipe(self):
        tool = self.software()
        toolchains.prepare(self.copy, tools={'sdk': tool}, save_project=True)
        recipe_file = self.recipe_path(self.copy)
        cached = Path(json.loads(recipe_file.read_bytes())['tools']['sdk']['source'])
        for directory in (cached, *(path for path in cached.rglob('*') if path.is_dir())):
            directory.chmod(0o700)
        shutil.rmtree(cached)

        fresh = self.fresh_copy()
        with self.assertRaises(toolchains.MissingSourceError) as caught:
            with fresh.lock():
                toolchains.apply_saved_recipe(fresh)

        message = str(caught.exception)
        self.assertIn('source is missing', message)
        self.assertIn('stale recipe was removed', message)
        self.assertEqual(caught.exception.code, 78)
        self.assertFalse(recipe_file.exists())
        self.assertFalse((fresh.path / '.deepseek-tools').exists())
        self.assertNotIn('toolchain', self.reloaded(fresh).metadata)

    def test_legacy_tool_recipe_guides_tool_flag_and_preserves_recipe(self):
        tool = self.software()
        fresh = self.fresh_copy()
        recipe_file = self.write_recipe(
            fresh,
            tools={'sdk': {'source': str(tool), 'files': 3, 'bytes': 32,
                           'links_skipped': 0}},
            copies=[])
        before = recipe_file.read_bytes()

        with self.assertRaises(toolchains.ToolchainError) as caught:
            with fresh.lock():
                toolchains.apply_saved_recipe(fresh)

        message = str(caught.exception)
        self.assertIn('mutable host', message)
        self.assertIn('--tool NAME=HOST_DIR', message)
        self.assertIn('--copy SOURCE=REL_DEST', message)
        self.assertIn('--save-project', message)
        self.assertEqual(recipe_file.read_bytes(), before)
        self.assertEqual((tool / 'bin/tool').read_bytes(), b'#!/bin/sh\nexit 0\n')
        self.assertFalse((fresh.path / '.deepseek-tools').exists())

    def test_legacy_copy_recipe_guides_copy_flag_and_preserves_recipe(self):
        source_file = self.input_file()
        fresh = self.fresh_copy()
        recipe_file = self.write_recipe(
            fresh, tools={},
            copies=[{'source': str(source_file), 'destination': 'build/local.properties'}])
        before = recipe_file.read_bytes()

        with self.assertRaises(toolchains.ToolchainError) as caught:
            with fresh.lock():
                toolchains.apply_saved_recipe(fresh)

        message = str(caught.exception)
        self.assertIn('mutable host', message)
        self.assertIn('--tool NAME=HOST_DIR', message)
        self.assertIn('--copy SOURCE=REL_DEST', message)
        self.assertIn('--save-project', message)
        self.assertEqual(recipe_file.read_bytes(), before)
        self.assertEqual(source_file.read_text(), 'sdk.dir=prepared\n')
        self.assertFalse((fresh.path / 'build/local.properties').exists())

    def test_replay_wrapper_preserves_capacity_and_security_error_codes(self):
        toolchains.prepare(self.copy, tools={'sdk': self.software()}, save_project=True)
        fresh = self.fresh_copy()
        for message, code in (('capacity budget exceeded for replay', 75),
                              ('private tool area is unsafe', 78)):
            with self.subTest(message=message):
                original = toolchains.ToolchainError(message, code)
                with mock.patch.object(toolchains, '_materialize', side_effect=original):
                    with self.assertRaises(toolchains.ToolchainError) as caught:
                        with fresh.lock():
                            toolchains.apply_saved_recipe(fresh)
                self.assertIn(message, str(caught.exception))
                self.assert_replay_guidance(str(caught.exception))
                self.assertEqual(caught.exception.code, code)
                self.assertIs(caught.exception.__cause__, original)


if __name__ == '__main__':
    unittest.main()
