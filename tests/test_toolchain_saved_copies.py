"""Focused regression tests for cache-backed saved toolchain copies."""
from __future__ import annotations

import contextlib
import hashlib
import io
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

from codex_deepseek_team import delegation_cli, toolchain_cache, toolchains, workspace, workspace_gc


def _git(root, *args, ok=(0,)):
    result = subprocess.run(['git', '-C', str(root), *args], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, check=False)
    if result.returncode not in ok:
        raise AssertionError(result.stderr.decode('utf-8', 'replace'))
    return result.stdout.decode('utf-8', 'replace')


def _commit(root, *args):
    _git(root, '-c', 'user.name=Toolchain', '-c', 'user.email=toolchain@example.test', *args)


class SavedCopyFixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='dst-saved-copies-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.isolated = {
            'HOME': self.root / 'home',
            'CODEX_HOME': self.root / 'codex-home',
            'CLAUDE_CONFIG_DIR': self.root / 'claude-home',
            'XDG_CONFIG_HOME': self.root / 'xdg-config',
            'XDG_STATE_HOME': self.root / 'xdg-state',
            'TMPDIR': self.root / 'tmp',
        }
        for path in self.isolated.values():
            path.mkdir(parents=True)
        self.cwd = self.root / 'cwd'
        self.cwd.mkdir()
        old_cwd = os.getcwd()
        os.chdir(self.cwd)
        self.addCleanup(os.chdir, old_cwd)
        environment = {name: str(path) for name, path in self.isolated.items()}
        patch = mock.patch.dict(os.environ, environment)
        patch.start()
        self.addCleanup(patch.stop)

        self.canaries = {
            self.isolated['CODEX_HOME'] / 'config.toml': 'codex-decoy-canary\n',
            self.isolated['CODEX_HOME'] / 'auth.json': 'codex-auth-decoy-canary\n',
            self.isolated['CLAUDE_CONFIG_DIR'] / 'settings.json': 'claude-decoy-canary\n',
            self.isolated['XDG_CONFIG_HOME'] / 'deepseek' / 'config.json': 'xdg-decoy-canary\n',
            self.isolated['XDG_STATE_HOME'] / 'deepseek' / 'state.json': 'state-decoy-canary\n',
            self.cwd / 'decoy-project.txt': 'cwd-decoy-canary\n',
        }
        for path, content in self.canaries.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        self.addCleanup(self._assert_canaries)

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

    def cache_sources(self, payload):
        tools = [entry['source'] for entry in payload['tools'].values()]
        copies = [entry['source'] for entry in payload['copies']]
        return tools + copies


class CacheBackedSavedCopyTests(SavedCopyFixture):
    def test_all_prepared_sources_are_cached_and_replay_after_host_and_copy_loss(self):
        tool = self.software(content=b'#!/bin/sh\necho original\n')
        source_file = self.input_file()
        toolchains.prepare(
            self.copy, tools={'sdk': tool},
            copies=[(source_file, 'build/local.properties')], save_project=True)

        recipe_file = self.recipe_path(self.copy)
        before = recipe_file.read_bytes()
        payload = json.loads(before)
        self.assertEqual(payload['version'], 1)
        self.assertEqual(set(payload), {'version', 'kind', 'source_project', 'saved_at',
                                        'tools', 'copies', 'environment', 'probes'})
        self.assertEqual(payload['copies'][0]['destination'], 'build/local.properties')
        self.assertNotIn(str(tool), before.decode())
        self.assertNotIn(str(source_file), before.decode())
        self.assertNotIn(str(self.copy.directory), before.decode())
        all_sources = self.cache_sources(payload)
        self.assertEqual(len(all_sources), 2)
        for value in all_sources:
            path = Path(value)
            self.assertEqual(path.parent, self.state / 'toolchain-cache' / 'entries')
            self.assertRegex(path.name, r'^[0-9a-f]{64}$')
            self.assertEqual(path.stat().st_uid, os.geteuid())
            self.assertFalse(path.is_symlink())

        cached_tool = Path(payload['tools']['sdk']['source'])
        cached_copy = Path(payload['copies'][0]['source'])
        self.assertNotEqual(cached_tool.stat().st_ino,
                            (self.copy.path / '.deepseek-tools/sdk').stat().st_ino)
        self.assertNotEqual(cached_copy.stat().st_ino,
                            (self.copy.path / 'build/local.properties').stat().st_ino)

        tool.joinpath('bin/tool').write_bytes(b'#!/bin/sh\necho mutated\n')
        source_file.write_text('sdk.dir=mutated\n')
        try:
            finalized = workspace_gc.finalize(self.copy, reason='focused saved-copy test')
        except workspace.WorkspaceError as error:
            if 'unprivileged namespace' not in str(error):
                raise
            # The worker sandbox cannot run result capture. The failed finalization
            # must preserve the execution tree, not delete durable history to pass.
            self.assertTrue(self.copy.path.is_dir())
            history = workspace_gc.inspect_workspace(self.state, self.copy.id)
            self.assertTrue(history['execution_present'])
        else:
            self.assertEqual(finalized['action'], 'cleaned')
            self.assertFalse(self.copy.path.exists())
            self.assertFalse((self.copy.directory / 'work').exists())
            self.assertTrue(self.copy.directory.is_dir())
            for name in ('record.json', 'result.json', 'changes.patch'):
                self.assertTrue((self.copy.directory / name).is_file(), name)
            history = workspace_gc.inspect_workspace(self.state, self.copy.id)
            self.assertFalse(history['execution_present'])
            self.assertEqual(history['lease']['cleanup']['state'], 'cleaned')
            self.assertEqual(history['result']['workspace_id'], self.copy.id)
        self.assertEqual(recipe_file.read_bytes(), before)

        fresh = self.fresh_copy()
        with fresh.lock():
            result = toolchains.apply_saved_recipe(fresh)
        self.assertTrue(result['applied'])
        self.assertEqual((fresh.path / '.deepseek-tools/sdk/bin/tool').read_bytes(),
                         b'#!/bin/sh\necho original\n')
        self.assertEqual(stat.S_IMODE((fresh.path / '.deepseek-tools/sdk/bin/tool').stat().st_mode),
                         0o755)
        self.assertEqual((fresh.path / 'build/local.properties').read_text(),
                         'sdk.dir=prepared\n')
        self.assertEqual(stat.S_IMODE((fresh.path / 'build/local.properties').stat().st_mode),
                         0o640)
        self.assertNotEqual((fresh.path / '.deepseek-tools/sdk/bin/tool').stat().st_ino,
                            (cached_tool / 'bin/tool').stat().st_ino)
        self.assertNotEqual((fresh.path / 'build/local.properties').stat().st_ino,
                            (cached_copy).stat().st_ino)
        self.assertEqual(self.reloaded(fresh).metadata['toolchain']['version'], 1)
        self.assertEqual(self.reloaded(fresh).metadata['prepared_paths'],
                         ['build/local.properties'])
        self.assertNotIn(str(tool), recipe_file.read_text())
        self.assertNotIn(str(source_file), recipe_file.read_text())

    def test_legacy_external_recipe_is_preserved_but_fresh_replay_is_refused(self):
        tool = self.software()
        fresh = self.fresh_copy()
        recipe_file = self.recipe_path(fresh)
        payload = {
            'version': 1,
            'kind': 'toolchain-recipe',
            'source_project': str(Path(fresh.source).resolve()),
            'saved_at': time.time(),
            'tools': {'sdk': {'source': str(tool), 'files': 3, 'bytes': 32,
                              'links_skipped': 0}},
            'copies': [],
            'environment': {},
            'probes': [],
        }
        recipe_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        recipe_file.parent.chmod(0o700)
        recipe_file.write_text(json.dumps(payload))
        recipe_file.chmod(0o600)
        before = recipe_file.read_bytes()

        with self.assertRaises(toolchains.ToolchainError) as caught:
            with fresh.lock():
                toolchains.apply_saved_recipe(fresh)
        message = str(caught.exception)
        self.assertIn('mutable host', message)
        self.assertIn('--copy', message)
        self.assertIn('--save-project', message)
        self.assertEqual(recipe_file.read_bytes(), before)
        self.assertFalse((fresh.path / '.deepseek-tools').exists())
        self.assertEqual((tool / 'bin/tool').read_text(), '#!/bin/sh\nexit 0\n')

        live = self.fresh_copy()
        toolchains.prepare(live, tools={'sdk': tool})
        with live.lock():
            result = toolchains.apply_saved_recipe(live)
        self.assertFalse(result['applied'])
        self.assertEqual(result['reason'], 'already-prepared')
        self.assertEqual((live.path / '.deepseek-tools/sdk/lib/data.txt').read_text(), 'data\n')

        toolchains.prepare(fresh, tools={'sdk': tool}, save_project=True)
        repaired = json.loads(recipe_file.read_text())
        self.assertTrue(self.cache_sources(repaired))
        for source in self.cache_sources(repaired):
            self.assertTrue(toolchain_cache.is_entry_path(self.state, source))
        replay = self.fresh_copy()
        with replay.lock():
            self.assertTrue(toolchains.apply_saved_recipe(replay)['applied'])

    def test_evicted_new_cache_reference_fails_without_partial_metadata(self):
        tool = self.software()
        toolchains.prepare(self.copy, tools={'sdk': tool}, save_project=True)
        recipe_file = self.recipe_path(self.copy)
        payload = json.loads(recipe_file.read_text())
        cached = Path(payload['tools']['sdk']['source'])
        toolchain_cache.collect(self.state, apply=True, max_bytes=0, max_inodes=0)
        self.assertFalse(cached.exists())
        fresh = self.fresh_copy()
        with self.assertRaises(toolchains.ToolchainError) as caught:
            with fresh.lock():
                toolchains.apply_saved_recipe(fresh)
        self.assertIn('removed', str(caught.exception).lower())
        self.assertNotIn('toolchain', self.reloaded(fresh).metadata)
        self.assertFalse((fresh.path / '.deepseek-tools').exists())


    def test_old_safe_cached_v1_recipe_replays_without_schema_drift(self):
        tool = self.software()
        source_file = self.input_file()
        cached_tool = toolchain_cache.snapshot(self.state, tool)
        cached_file = toolchain_cache.snapshot(self.state, source_file)
        fresh = self.fresh_copy()
        recipe_file = self.recipe_path(fresh)
        payload = {
            'version': 1,
            'kind': 'toolchain-recipe',
            'source_project': str(Path(fresh.source).resolve()),
            'saved_at': time.time(),
            'tools': {'sdk': {'source': str(cached_tool), 'files': 3, 'bytes': 35,
                              'links_skipped': 0}},
            'copies': [{'source': str(cached_file), 'destination': 'build/local.properties'}],
            'environment': {'JAVA_HOME': '{workspace}/.deepseek-tools/sdk'},
            'probes': ['{workspace}/.deepseek-tools/sdk/bin/tool --version'],
        }
        recipe_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        recipe_file.parent.chmod(0o700)
        recipe_file.write_text(json.dumps(payload))
        recipe_file.chmod(0o600)
        before = recipe_file.read_bytes()
        with fresh.lock():
            result = toolchains.apply_saved_recipe(fresh)
        self.assertTrue(result['applied'])
        self.assertEqual(result['toolchain']['version'], 1)
        self.assertEqual(set(json.loads(before)), {'version', 'kind', 'source_project',
                                                   'saved_at', 'tools', 'copies',
                                                   'environment', 'probes'})
        self.assertEqual((fresh.path / '.deepseek-tools/sdk/bin/tool').read_bytes(),
                         b'#!/bin/sh\nexit 0\n')
        self.assertEqual((fresh.path / 'build/local.properties').read_text(), 'sdk.dir=prepared\n')
        self.assertEqual(recipe_file.read_bytes(), before)

    def test_credential_alias_and_escaping_sources_preserve_canaries(self):
        credential = self.root / '.ssh' / 'sdk'
        shutil.copytree(self.software(), credential)
        alias = self.root / 'safe-looking-tool'
        os.symlink(credential, alias)
        with self.assertRaises(toolchains.ToolchainError):
            toolchains.prepare(self.copy, tools={'sdk': alias}, save_project=True)
        self.assertEqual((credential / 'lib/data.txt').read_text(), 'data\n')
        self.assertFalse(self.recipe_path(self.copy).exists())
        self.assertFalse((self.copy.path / '.deepseek-tools').exists())

        outside = self.root / 'outside-canary.txt'
        outside.write_text('outside-canary\n')
        escaping = self.software('escaping')
        os.symlink(outside, escaping / 'lib/escape')
        fresh = self.fresh_copy()
        with self.assertRaises(toolchains.ToolchainError):
            toolchains.prepare(fresh, tools={'sdk': escaping}, save_project=True)
        self.assertEqual(outside.read_text(), 'outside-canary\n')
        self.assertFalse(self.recipe_path(fresh).exists())

    def test_recipe_snapshot_rejects_replaced_destination_ancestor(self):
        tool = self.software()
        source_file = self.input_file()
        outside = self.root / 'outside-build'
        outside.mkdir()
        (outside / 'local.properties').write_text('outside-canary\n')
        outside_file = outside / 'local.properties'
        real_save = toolchains._save_recipe

        def raced_save(copy, metadata, **kwargs):
            build = copy.path / 'build'
            shutil.rmtree(build)
            os.symlink(outside, build)
            return real_save(copy, metadata, **kwargs)

        with mock.patch.object(toolchains, '_save_recipe', side_effect=raced_save):
            with self.assertRaises(toolchains.ToolchainError) as caught:
                toolchains.prepare(self.copy, tools={'sdk': tool},
                                   copies=[(source_file, 'build/local.properties')],
                                   save_project=True)
        self.assertIn('ancestor', str(caught.exception))
        self.assertEqual(outside_file.read_text(), 'outside-canary\n')
        self.assertFalse(self.recipe_path(self.copy).exists())

    def test_batch_protects_prior_and_selected_entries_from_eviction(self):
        old = self.software('old')
        old_cached = toolchain_cache.snapshot(self.state, old)
        report = toolchain_cache.collect(self.state)
        newer = self.software('new', content=b'#!/bin/sh\necho newer\n')
        with self.assertRaises(toolchains.ToolchainError):
            with toolchain_cache.snapshot_batch(
                    self.state, max_bytes=report['bytes'], max_inodes=report['inodes']) as batch:
                batch.protect_paths([old_cached])
                batch.snapshot(newer)
                batch.publish()
        self.assertTrue(toolchain_cache.validate_entry(
            self.state, old_cached, require_directory=True).is_dir())
        self.assertEqual(toolchain_cache.collect(self.state)['entries'], 1)

        with self.assertRaises(toolchains.ToolchainError):
            with toolchain_cache.snapshot_batch(
                    self.state, max_bytes=report['bytes'], max_inodes=report['inodes']) as batch:
                batch.snapshot(old)
                batch.snapshot(newer)
                batch.publish()
        self.assertTrue(toolchain_cache.validate_entry(
            self.state, old_cached, require_directory=True).is_dir())
        self.assertEqual(toolchain_cache.collect(self.state)['entries'], 1)

    def test_existing_batch_honors_the_lowered_shared_ceiling(self):
        selected = self.software('selected')
        cached = toolchain_cache.snapshot(self.state, selected)
        unrelated = toolchain_cache.snapshot(
            self.state, self.software('unrelated', content=b'#!/bin/sh\necho unrelated\n'))
        report = toolchain_cache.collect(self.state)

        with toolchain_cache.snapshot_batch(
                self.state, max_bytes=report['bytes'] - 1,
                max_inodes=report['inodes']) as batch:
            batch.protect_paths([cached])
            batch.snapshot(selected)
            batch.publish()

        self.assertTrue(toolchain_cache.validate_entry(
            self.state, cached, require_directory=True).is_dir())
        self.assertFalse(unrelated.exists())
        remaining = toolchain_cache.collect(self.state)
        self.assertEqual(remaining['entries'], 1)
        self.assertLessEqual(remaining['bytes'], report['bytes'] - 1)
        self.assertLessEqual(remaining['inodes'], report['inodes'])

    def test_batch_validates_every_staged_source_before_publication(self):
        keep = self.software('keep')
        kept = toolchain_cache.snapshot(self.state, keep)
        report = toolchain_cache.collect(self.state)
        good = self.software('good')
        outside = self.root / 'outside-batch-canary.txt'
        outside.write_text('batch-canary\n')
        bad = self.software('bad')
        os.symlink(outside, bad / 'lib/escape')
        with self.assertRaises(toolchains.ToolchainError):
            with toolchain_cache.snapshot_batch(
                    self.state, max_bytes=report['bytes'] * 2,
                    max_inodes=report['inodes'] * 2) as batch:
                batch.snapshot(good)
                batch.snapshot(bad)
                batch.publish()
        self.assertEqual(outside.read_text(), 'batch-canary\n')
        self.assertTrue(toolchain_cache.validate_entry(
            self.state, kept, require_directory=True).is_dir())
        self.assertEqual(toolchain_cache.collect(self.state)['entries'], 1)


class CacheLimitTests(SavedCopyFixture):
    def test_invalid_programmatic_limits_are_rejected_before_materialization(self):
        invalid = (0, -1, True, False, 1.5, '1', 2 ** 63)
        tool = self.software()
        for keyword in ('cache_max_bytes', 'cache_max_inodes'):
            for value in invalid:
                copy = self.fresh_copy()
                with mock.patch.object(toolchains, '_materialize',
                                       side_effect=AssertionError('materialized')):
                    with self.assertRaises(toolchains.ToolchainError) as caught:
                        toolchains.prepare(copy, tools={'sdk': tool},
                                           save_project=True, **{keyword: value})
                self.assertEqual(caught.exception.code, 64)
                self.assertFalse((copy.path / '.deepseek-tools').exists())
        copy = self.fresh_copy()
        with mock.patch.object(toolchains, '_materialize',
                               side_effect=AssertionError('materialized')):
            with self.assertRaises(toolchains.ToolchainError) as caught:
                toolchains.prepare(copy, tools={'sdk': tool},
                                   cache_max_bytes=1024)
        self.assertEqual(caught.exception.code, 64)
        self.assertFalse(self.recipe_path(copy).exists())

    def test_cli_rejects_invalid_or_saveless_cache_limits_before_copy_access(self):
        values = ('0', '-1', '+1', '1.0', 'true', '9223372036854775808', ' 1', '1 ',
                  '9' * 5000)
        for flag in ('--cache-max-bytes', '--cache-max-inodes'):
            for value in values:
                with mock.patch.object(workspace, 'load',
                                       side_effect=AssertionError('copy loaded')):
                    with contextlib.redirect_stderr(io.StringIO()) as stderr:
                        code = delegation_cli.main([
                            'workspace', 'prepare', 'not-loaded', '--state-dir',
                            str(self.state), flag, value, '--save-project'])
                self.assertEqual(code, 64, (flag, value))
                self.assertIn('positive decimal integer', stderr.getvalue())
        with mock.patch.object(workspace, 'load',
                               side_effect=AssertionError('copy loaded')):
            with contextlib.redirect_stderr(io.StringIO()) as stderr:
                code = delegation_cli.main([
                    'workspace', 'prepare', 'not-loaded', '--state-dir', str(self.state),
                    '--cache-max-bytes', '1'])
        self.assertEqual(code, 64)
        self.assertIn('--save-project', stderr.getvalue())

    def test_cli_forwards_valid_cache_limits_with_save_project(self):
        with mock.patch.object(toolchains, 'prepare', return_value={'recipe_saved': True}) as prepare:
            with contextlib.redirect_stdout(io.StringIO()) as stdout:
                code = delegation_cli.main([
                    'workspace', 'prepare', self.copy.id, '--state-dir', str(self.state),
                    '--tool', 'sdk=' + str(self.software()), '--save-project',
                    '--cache-max-bytes', '123456', '--cache-max-inodes', '789'])
        self.assertEqual(code, 0, stdout.getvalue())
        kwargs = prepare.call_args.kwargs
        self.assertEqual(kwargs['cache_max_bytes'], 123456)
        self.assertEqual(kwargs['cache_max_inodes'], 789)
        self.assertTrue(kwargs['save_project'])

    def test_invalid_flag_does_not_create_or_mutate_workspace(self):
        before = self.reloaded(self.copy).metadata
        with contextlib.redirect_stderr(io.StringIO()) as stderr:
            code = delegation_cli.main([
                'workspace', 'prepare', self.copy.id, '--state-dir', str(self.state),
                '--cache-max-bytes', '0', '--save-project'])
        self.assertEqual(code, 64)
        self.assertEqual(self.reloaded(self.copy).metadata, before)
        self.assertFalse((self.copy.path / '.deepseek-tools').exists())
        self.assertFalse(self.recipe_path(self.copy).exists())
        self.assertIn('cache-max-bytes', stderr.getvalue())

    def test_failed_limited_resave_keeps_previous_recipe_and_protected_content(self):
        first_tool = self.software('first', content=b'#!/bin/sh\necho first\n')
        toolchains.prepare(self.copy, tools={'sdk': first_tool}, save_project=True)
        recipe_file = self.recipe_path(self.copy)
        before = recipe_file.read_bytes()
        first_payload = json.loads(before)
        first_cached = Path(first_payload['tools']['sdk']['source'])
        report = toolchain_cache.collect(self.state)
        self.assertGreater(report['bytes'], 0)

        second_tool = self.software('second', content=b'#!/bin/sh\necho second\n')
        with self.assertRaises(toolchains.ToolchainError):
            toolchains.prepare(
                self.copy, tools={'sdk': second_tool}, save_project=True, recover=True,
                cache_max_bytes=report['bytes'], cache_max_inodes=report['inodes'])
        self.assertEqual(recipe_file.read_bytes(), before)
        self.assertTrue(first_cached.is_dir())
        toolchain_cache.validate_entry(self.state, first_cached, require_directory=True)

        fresh = self.fresh_copy()
        with fresh.lock():
            self.assertTrue(toolchains.apply_saved_recipe(fresh)['applied'])
        self.assertEqual((fresh.path / '.deepseek-tools/sdk/bin/tool').read_bytes(),
                         b'#!/bin/sh\necho first\n')

    def test_byte_ceiling_mocks_are_aggregate_not_per_declaration(self):
        first = self.software('first')
        second = self.software('second', content=b'#!/bin/sh\necho second\n')
        real_measure = toolchain_cache._measure
        increment = 1 << 30
        cache_root = str(self.state / 'toolchain-cache')

        def measure(path, *, owned=False, ordinary=False):
            value = real_measure(path, owned=owned, ordinary=ordinary)
            if str(path).startswith(cache_root):
                return value[0] + increment, value[1]
            return value

        with mock.patch.object(toolchain_cache, '_measure', side_effect=measure):
            with self.assertRaises(toolchains.ToolchainError):
                toolchains.prepare(
                    self.copy, tools={'one': first, 'two': second}, save_project=True,
                    cache_max_bytes=3 << 30)
        self.assertFalse(self.recipe_path(self.copy).exists())

    def test_inode_boundary_mocks_apply_to_the_whole_save_batch(self):
        first = self.software('first')
        second = self.software('second', content=b'#!/bin/sh\necho second\n')
        real_measure = toolchain_cache._measure

        def measure(path, *, owned=False, ordinary=False):
            value = real_measure(path, owned=owned, ordinary=ordinary)
            if str(path).startswith(str(self.state / 'toolchain-cache')):
                return value[0], value[1] + 200
            return value

        with mock.patch.object(toolchain_cache, '_measure', side_effect=measure):
            with self.assertRaises(toolchains.ToolchainError):
                toolchains.prepare(
                    self.copy, tools={'one': first, 'two': second}, save_project=True,
                    cache_max_inodes=500)
        self.assertFalse(self.recipe_path(self.copy).exists())


if __name__ == '__main__':
    unittest.main()
