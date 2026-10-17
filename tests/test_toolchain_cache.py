"""Boundary tests for the durable shared toolchain cache and stable recipes."""
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
import uuid

from codex_deepseek_team import toolchain_cache, toolchains, workspace


def _git(root, *args, ok=(0,)):
    result = subprocess.run(['git', '-C', str(root), *args], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, check=False)
    if result.returncode not in ok:
        raise AssertionError(result.stderr.decode('utf-8', 'replace'))
    return result.stdout.decode('utf-8', 'replace')


def _commit(root, *args):
    _git(root, '-c', 'user.name=Toolchain', '-c', 'user.email=toolchain@example.test', *args)


def _tree_fingerprint(path):
    path = Path(path)
    if not os.path.lexists(path):
        return None
    info = os.lstat(path)
    values = {'.': (info.st_mode, info.st_uid, info.st_gid, info.st_size, info.st_mtime_ns,
                    info.st_ino, info.st_nlink,
                    os.readlink(path) if stat.S_ISLNK(info.st_mode) else None)}
    for directory, directories, files in os.walk(path, followlinks=False):
        for name in directories + files:
            item = Path(directory) / name
            info = os.lstat(item)
            values[str(item.relative_to(path))] = (
                info.st_mode, info.st_uid, info.st_gid, info.st_size, info.st_mtime_ns,
                info.st_ino, info.st_nlink, os.readlink(item) if stat.S_ISLNK(info.st_mode) else None)
    return values


class CacheFixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='dst-toolchain-cache-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        isolated = {'HOME': self.root / 'home', 'CODEX_HOME': self.root / 'codex',
                    'CLAUDE_CONFIG_DIR': self.root / 'claude',
                    'XDG_CONFIG_HOME': self.root / 'xdg-config',
                    'XDG_STATE_HOME': self.root / 'xdg-state'}
        for path in isolated.values():
            path.mkdir(parents=True)
        patch = mock.patch.dict(os.environ, {name: str(path) for name, path in isolated.items()})
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

    def fresh_copy(self):
        return workspace.create(self.source, self.state)

    def software(self, name='sdk'):
        root = self.root / 'software' / name
        (root / 'bin').mkdir(parents=True)
        binary = root / 'bin' / 'tool'
        binary.write_bytes(b'#!/bin/sh\nexit 0\n')
        binary.chmod(0o755)
        (root / 'lib' / 'sub').mkdir(parents=True)
        (root / 'lib' / 'data.txt').write_text('data\n')
        (root / 'lib' / 'sub' / 'inner.txt').write_text('inner\n')
        return root

    def recipe_path(self, copy):
        key = hashlib.sha256(str(Path(copy.source).resolve()).encode()).hexdigest()
        return self.state / 'toolchain-recipes' / (key + '.json')

    def reloaded(self, copy):
        return workspace.load(self.state, copy.id)


class SnapshotTests(CacheFixture):
    def test_snapshot_is_content_addressed_immutable_and_reusable(self):
        root = self.software()
        os.symlink('../lib/data.txt', root / 'bin' / 'data-link')
        before = (root / 'bin' / 'tool').read_bytes()
        cached = toolchain_cache.snapshot(self.state, root)

        self.assertTrue(cached.is_dir())
        self.assertFalse(cached.is_symlink())
        self.assertEqual((cached / 'bin' / 'tool').read_bytes(), before)
        self.assertEqual(stat.S_IMODE((cached / 'bin' / 'tool').stat().st_mode), 0o555)
        self.assertEqual(stat.S_IMODE((cached / 'lib' / 'data.txt').stat().st_mode), 0o444)
        self.assertEqual(stat.S_IMODE(cached.stat().st_mode), 0o500)
        self.assertFalse((cached / 'bin' / 'data-link').is_symlink())
        self.assertEqual((cached / 'bin' / 'data-link').read_text(), 'data\n')
        self.assertNotEqual((cached / 'bin' / 'tool').stat().st_ino,
                            (root / 'bin' / 'tool').stat().st_ino)
        self.assertEqual((root / 'bin' / 'tool').read_bytes(), before)
        self.assertEqual(toolchain_cache.snapshot(self.state, root), cached)
        report = toolchain_cache.collect(self.state)
        self.assertEqual(report['entries'], 1)
        self.assertGreater(report['bytes'], 0)
        self.assertGreater(report['inodes'], 0)
        self.assertTrue(report['within_budget'])

    def test_snapshot_file_inputs_preserve_executable_bits(self):
        executable = self.root / 'inputs' / 'runner.sh'
        executable.parent.mkdir(parents=True)
        executable.write_text('#!/bin/sh\nexit 0\n')
        executable.chmod(0o755)
        cached = toolchain_cache.snapshot(self.state, executable)
        self.assertTrue(cached.is_file())
        self.assertEqual(stat.S_IMODE(cached.stat().st_mode), 0o555)
        self.assertEqual(cached.read_text(), '#!/bin/sh\nexit 0\n')
        self.assertEqual(toolchain_cache.collect(self.state)['entries'], 1)

    def test_snapshot_refuses_tampered_reused_entries(self):
        root = self.software()
        cached = toolchain_cache.snapshot(self.state, root)
        binary = cached / 'bin' / 'tool'
        binary.chmod(0o755)
        binary.write_bytes(b'#!/bin/sh\nexit 9\n')
        with self.assertRaises(toolchains.ToolchainError):
            toolchain_cache.snapshot(self.state, root)
        report = toolchain_cache.collect(self.state)
        self.assertEqual(report['entries'], 0)
        self.assertEqual(len(report['invalid_entries']), 1)

    def test_snapshot_never_caches_checkout_metadata(self):
        checkout = self.root / 'checkout'
        (checkout / '.git').mkdir(parents=True)
        (checkout / 'payload.txt').write_text('checkout\n')
        with self.assertRaises(toolchains.ToolchainError):
            toolchain_cache.snapshot(self.state, checkout)
        with self.assertRaises(toolchains.ToolchainError):
            toolchain_cache.snapshot(self.state, checkout / '.git')

    def test_snapshot_applies_credential_and_symlink_materializer_rules(self):
        root = self.software()
        outside = self.root / 'outside.txt'
        outside.write_text('outside\n')
        os.symlink(str(outside), root / 'lib' / 'escape')
        with self.assertRaises(toolchains.ToolchainError):
            toolchain_cache.snapshot(self.state, root)

        clean = self.software(name='clean')
        credential_tree = self.root / 'secrets' / '.aws' / 'sdk'
        shutil.copytree(clean, credential_tree, symlinks=True)
        with self.assertRaises(toolchains.ToolchainError):
            toolchain_cache.snapshot(self.state, credential_tree)
        alias = self.root / 'safe-looking-alias'
        os.symlink(str(credential_tree), alias)
        with self.assertRaises(toolchains.ToolchainError):
            toolchain_cache.snapshot(self.state, alias)

        credential = self.root / 'secrets' / 'private.key'
        credential.parent.mkdir(parents=True, exist_ok=True)
        credential.write_text('credential\n')
        linked = self.software(name='linked-credential')
        os.symlink(str(credential), linked / 'lib' / 'plain.txt')
        with self.assertRaises(toolchains.ToolchainError):
            toolchain_cache.snapshot(self.state, linked)

        exposed = self.root / '.ssh' / 'sdk'
        shutil.copytree(clean, exposed, symlinks=True)
        with self.assertRaises(toolchains.ToolchainError):
            toolchain_cache.snapshot(self.state, exposed)

    def test_snapshot_never_hardlinks_source_data(self):
        root = self.software()
        os.link(root / 'lib' / 'data.txt', root / 'lib' / 'data-alias.txt')
        cached = toolchain_cache.snapshot(self.state, root)
        (root / 'lib' / 'data.txt').write_text('changed\n')
        self.assertEqual((cached / 'lib' / 'data.txt').read_text(), 'data\n')
        self.assertEqual((cached / 'lib' / 'data-alias.txt').read_text(), 'data\n')
        self.assertEqual((cached / 'lib' / 'data.txt').stat().st_nlink, 1)
        self.assertEqual((cached / 'lib' / 'data-alias.txt').stat().st_nlink, 1)
        self.assertNotEqual((cached / 'lib' / 'data.txt').stat().st_ino,
                            (cached / 'lib' / 'data-alias.txt').stat().st_ino)

    def test_snapshot_enforces_cumulative_byte_and_inode_budgets(self):
        first = self.software(name='first')
        second = self.software(name='second')
        (second / 'lib' / 'unique.txt').write_text('second only\n')
        with self.assertRaises(toolchains.ToolchainError):
            toolchain_cache.snapshot(self.state, first, max_bytes=1)
        with self.assertRaises(toolchains.ToolchainError):
            toolchain_cache.snapshot(self.state, first, max_inodes=1)
        cached = toolchain_cache.snapshot(self.state, first)
        report = toolchain_cache.collect(self.state)
        with self.assertRaises(toolchains.ToolchainError):
            toolchain_cache.snapshot(self.state, second, max_bytes=report['bytes'])
        with self.assertRaises(toolchains.ToolchainError):
            toolchain_cache.snapshot(self.state, second, max_inodes=report['inodes'])
        self.assertTrue(cached.is_dir())

        removed = toolchain_cache.collect(self.state, apply=True, max_bytes=0, max_inodes=1)
        self.assertGreaterEqual(len(removed['removed_entries']), 1)
        self.assertEqual(toolchain_cache.collect(self.state)['entries'], 0)


class CacheSafetyRegressionTests(CacheFixture):
    def cache_root(self):
        return self.state / 'toolchain-cache'

    def seed_cache(self):
        cached = toolchain_cache.snapshot(self.state, self.software())
        self.assertTrue(cached.is_dir())
        return cached

    def test_dry_run_absent_state_creates_nothing(self):
        state = self.root / 'never-created'
        before = _tree_fingerprint(self.root)
        report = toolchain_cache.collect(state)
        self.assertEqual(_tree_fingerprint(self.root), before)
        self.assertFalse(state.exists())
        self.assertFalse((state / 'toolchain-cache').exists())
        self.assertEqual(report['entries'], 0)
        self.assertEqual(report['bytes'], 0)
        self.assertEqual(report['inodes'], 0)

    def test_dry_run_complete_empty_layout_does_not_create_a_lock(self):
        state = self.root / 'empty-state'
        root = state / 'toolchain-cache'
        for path in (state, root, root / 'entries', root / 'manifests', root / '.staging'):
            path.mkdir(mode=0o700, parents=not path.exists())
            path.chmod(0o700)
        before = _tree_fingerprint(state)
        report = toolchain_cache.collect(state)
        self.assertEqual(_tree_fingerprint(state), before)
        self.assertFalse((root / 'cache.lock').exists())
        self.assertEqual(report['entries'], 0)

    def test_dry_run_refuses_and_does_not_repair_a_partial_layout(self):
        state = self.root / 'partial-state'
        root = state / 'toolchain-cache'
        (root / 'entries').mkdir(mode=0o700, parents=True)
        state.chmod(0o700)
        root.chmod(0o700)
        (root / 'entries').chmod(0o700)
        before = _tree_fingerprint(state)
        with self.assertRaises(toolchains.ToolchainError):
            toolchain_cache.collect(state)
        self.assertEqual(_tree_fingerprint(state), before)
        self.assertFalse((root / 'manifests').exists())
        self.assertFalse((root / '.staging').exists())
        self.assertFalse((root / 'cache.lock').exists())

    def test_dry_run_populated_cache_changes_no_cache_metadata(self):
        self.seed_cache()
        staging = self.cache_root() / '.staging'
        (staging / '.stage-foreign').mkdir(mode=0o700)
        (staging / '.stage-foreign' / 'canary').write_text('preserve\n')
        before = _tree_fingerprint(self.cache_root())
        report = toolchain_cache.collect(self.state, max_bytes=0, max_inodes=0)
        self.assertEqual(_tree_fingerprint(self.cache_root()), before)
        self.assertGreater(report['bytes'], 0)
        self.assertEqual(report['bytes'], report['entries_bytes'] + report['manifests_bytes']
                         + report['staging_bytes'] + report['lock_bytes'])

    def test_apply_preserves_unknown_and_managed_symlink_staging(self):
        self.seed_cache()
        staging = self.cache_root() / '.staging'
        unknown = staging / '.stage-foreign'
        unknown.mkdir(mode=0o700)
        (unknown / 'canary').write_text('unknown\n')
        outside = self.root / 'outside-staging'
        outside.mkdir()
        (outside / 'canary').write_text('outside\n')
        linked = staging / ('.stage-' + uuid.uuid4().hex)
        os.symlink(outside, linked)
        nested = staging / ('.stage-' + uuid.uuid4().hex)
        nested.mkdir(mode=0o700)
        os.symlink(outside, nested / 'child-link')
        report = toolchain_cache.collect(self.state, apply=True)
        preserved = {item['path']: item['reason'] for item in report['preserved_staging']}
        self.assertIn('.stage-foreign', preserved)
        self.assertIn(linked.name, preserved)
        self.assertIn(nested.name, preserved)
        self.assertTrue((unknown / 'canary').exists())
        self.assertTrue(linked.is_symlink())
        self.assertTrue((nested / 'child-link').is_symlink())
        self.assertEqual((outside / 'canary').read_text(), 'outside\n')

    def test_foreign_owned_staging_is_preserved_on_host(self):
        self.seed_cache()
        staging = self.cache_root() / '.staging'
        foreign = staging / ('.stage-' + uuid.uuid4().hex)
        foreign.mkdir(mode=0o700)
        canary = foreign / 'canary'
        canary.write_text('foreign\n')
        try:
            os.chown(canary, 65534, 65534)
            os.chown(foreign, 65534, 65534)
        except OSError as error:
            self.skipTest(f'chown is unavailable in this namespace ({error}); verify foreign ownership on the host')
        report = toolchain_cache.collect(self.state, apply=True)
        preserved = {item['path']: item['reason'] for item in report['preserved_staging']}
        self.assertIn(foreign.name, preserved)
        self.assertIn('not owned', preserved[foreign.name])
        self.assertTrue(canary.exists())

    def test_narrow_uid_simulation_preserves_managed_staging(self):
        self.seed_cache()
        staging = self.cache_root() / '.staging'
        stage = staging / ('.stage-' + uuid.uuid4().hex)
        stage.mkdir(mode=0o700)
        (stage / 'canary').write_text('narrow uid simulation\n')
        with mock.patch.object(toolchain_cache, '_deletion_owner_uid', return_value=65534):
            report = toolchain_cache.collect(self.state, apply=True)
        preserved = {item['path']: item['reason'] for item in report['preserved_staging']}
        self.assertIn(stage.name, preserved)
        self.assertIn('not owned', preserved[stage.name])
        self.assertTrue((stage / 'canary').exists())

    def test_replaced_staging_layout_and_lock_symlinks_are_refused(self):
        self.seed_cache()
        root = self.cache_root()
        outside = self.root / 'outside-layout'
        outside.mkdir()
        (outside / 'canary').write_text('outside\n')
        original_staging = root / '.staging'
        moved_staging = root / '.staging-original'
        original_staging.rename(moved_staging)
        os.symlink(outside, original_staging)
        with self.assertRaises(toolchains.ToolchainError):
            toolchain_cache.collect(self.state, apply=True)
        self.assertEqual((outside / 'canary').read_text(), 'outside\n')
        original_staging.unlink()
        moved_staging.rename(original_staging)
        lock = root / 'cache.lock'
        real_lock = root / 'cache.lock.real'
        lock.rename(real_lock)
        os.symlink(outside / 'canary', lock)
        with self.assertRaises(toolchains.ToolchainError):
            toolchain_cache.collect(self.state)
        self.assertEqual((outside / 'canary').read_text(), 'outside\n')

    def test_expected_identity_swap_cannot_delete_a_replacement(self):
        self.seed_cache()
        with toolchain_cache.locked(self.state) as layout:
            stage = layout.staging / ('.stage-' + uuid.uuid4().hex)
            stage.mkdir(mode=0o700)
            (stage / 'canary').write_text('keep\n')
            info = os.lstat(stage)
            with self.assertRaises(toolchains.ToolchainError):
                toolchain_cache._remove_cache_path(
                    stage, layout=layout, kind='staging',
                    expected_identity=(info.st_dev, info.st_ino + 1))
            self.assertTrue((stage / 'canary').exists())

    def test_replaced_cache_root_cannot_redirect_removal(self):
        self.seed_cache()
        outside = self.root / 'outside-root'
        outside.mkdir()
        (outside / 'canary').write_text('outside\n')
        with toolchain_cache.locked(self.state) as layout:
            stage = layout.staging / ('.stage-' + uuid.uuid4().hex)
            stage.mkdir(mode=0o700)
            (stage / 'keep').write_text('keep\n')
            moved = layout.root.with_name('toolchain-cache-moved')
            layout.root.rename(moved)
            os.symlink(outside, layout.root)
            try:
                with self.assertRaises(toolchains.ToolchainError):
                    toolchain_cache._remove_cache_path(
                        stage, layout=layout, kind='staging')
            finally:
                layout.root.unlink()
                moved.rename(layout.root)
        self.assertEqual((outside / 'canary').read_text(), 'outside\n')
        self.assertTrue((stage / 'keep').exists())

    def test_missing_mount_identity_fails_closed(self):
        self.seed_cache()
        canary = self.cache_root() / '.staging' / '.stage-foreign'
        canary.mkdir(mode=0o700)
        (canary / 'keep').write_text('keep\n')
        with mock.patch.object(toolchain_cache, '_mount_id',
                               side_effect=toolchains.ToolchainError('mount identity unavailable')):
            with self.assertRaises(toolchains.ToolchainError):
                toolchain_cache.collect(self.state, apply=True)
        self.assertTrue((canary / 'keep').exists())

    def test_interrupted_quarantine_is_recovered_without_touching_unknowns(self):
        self.seed_cache()
        root = self.cache_root()
        stage_purge = root / '.staging' / ('.purge-' + uuid.uuid4().hex)
        stage_purge.mkdir(mode=0o700)
        (stage_purge / 'partial').write_text('partial\n')
        entry_purge = root / 'entries' / ('.purge-' + uuid.uuid4().hex)
        entry_purge.mkdir(mode=0o700)
        (entry_purge / 'partial').write_text('partial\n')
        unknown = root / '.staging' / 'unknown-not-a-temp'
        unknown.mkdir(mode=0o700)
        (unknown / 'keep').write_text('keep\n')
        report = toolchain_cache.collect(self.state, apply=True)
        self.assertFalse(stage_purge.exists())
        self.assertFalse(entry_purge.exists())
        self.assertTrue((unknown / 'keep').exists())
        self.assertIn('unknown-not-a-temp',
                      {item['path'] for item in report['preserved_staging']})

    def test_interrupted_deletion_resumes_from_its_quarantine(self):
        self.seed_cache()
        staging = self.cache_root() / '.staging'
        stage = staging / ('.stage-' + uuid.uuid4().hex)
        stage.mkdir(mode=0o700)
        (stage / 'partial').write_text('partial\n')
        real_unlink = os.unlink
        calls = [0]

        def interrupt_once(path, *args, **kwargs):
            calls[0] += 1
            if calls[0] == 1:
                raise OSError('simulated interrupted deletion')
            return real_unlink(path, *args, **kwargs)

        with mock.patch.object(toolchain_cache.os, 'unlink', side_effect=interrupt_once):
            with self.assertRaises(OSError):
                toolchain_cache.collect(self.state, apply=True)
        quarantines = list(staging.glob('.purge-*'))
        self.assertEqual(len(quarantines), 1)
        self.assertFalse(stage.exists())
        report = toolchain_cache.collect(self.state, apply=True)
        self.assertEqual(report['preserved_staging'], [])
        self.assertFalse(quarantines[0].exists())

    def test_usage_measurement_rejects_read_and_identity_failures(self):
        self.seed_cache()
        with toolchain_cache._inspection(self.state) as layout:
            with mock.patch.object(toolchain_cache.os, 'scandir', side_effect=OSError('read failed')):
                with self.assertRaises(toolchains.ToolchainError):
                    toolchain_cache._measure(layout.entries)
            with mock.patch.object(toolchain_cache, '_owner_uid', return_value=65534):
                with self.assertRaises(toolchains.ToolchainError):
                    toolchain_cache._measure(layout.entries, owned=True)

    def test_layout_creation_refuses_a_symlinked_ancestor(self):
        outside = self.root / 'outside-state'
        outside.mkdir()
        alias = self.root / 'state-alias'
        os.symlink(outside, alias)
        before = _tree_fingerprint(outside)
        with self.assertRaises(toolchains.ToolchainError):
            toolchain_cache.collect(alias / 'state', apply=True)
        self.assertEqual(_tree_fingerprint(outside), before)
        self.assertEqual(list(outside.iterdir()), [])

    def test_nested_bind_mount_is_rejected_when_namespace_is_available(self):
        if shutil.which('unshare') is None:
            self.skipTest('unshare is unavailable; verify nested bind mounts on the host')
        if shutil.which('mount') is None:
            self.skipTest('mount is unavailable; verify nested bind mounts on the host')
        probe = subprocess.run(['unshare', '-m', '--', 'true'], stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, check=False)
        if probe.returncode != 0:
            self.skipTest('a real mount namespace is unavailable; verify nested bind mounts on the host')
        self.seed_cache()
        stage = self.cache_root() / '.staging' / ('.stage-' + uuid.uuid4().hex)
        mounted = stage / 'mounted'
        mounted.mkdir(mode=0o700, parents=True)
        outside = self.root / 'bind-canary'
        outside.mkdir()
        (outside / 'keep').write_text('keep\n')
        script = ('import json, subprocess, sys\n'
                  'from pathlib import Path\n'
                  'from codex_deepseek_team import toolchain_cache, toolchains\n'
                  'stage, outside, state = map(Path, sys.argv[1:4])\n'
                  "subprocess.run(['mount', '--bind', str(outside), str(stage / 'mounted')], check=True)\n"
                  "try:\n"
                  "    report = toolchain_cache.collect(state, apply=True)\n"
                  "    print(json.dumps(report['preserved_staging']))\n"
                  "except toolchains.ToolchainError as error:\n"
                  "    print('refused:', error)\n")
        environment = dict(os.environ)
        environment['PYTHONPATH'] = str(Path(__file__).resolve().parents[1] / 'src')
        result = subprocess.run(['unshare', '-m', '--propagation', 'private', 'python3', '-c', script,
                                 str(stage), str(outside), str(self.state)],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
                                env=environment)
        self.assertEqual(result.returncode, 0, result.stderr.decode('utf-8', 'replace'))
        self.assertTrue((outside / 'keep').exists())
        self.assertTrue(mounted.is_dir())


class StableRecipeTests(CacheFixture):
    def test_recipe_uses_cache_and_survives_deleting_the_execution_tree(self):
        root = self.software()
        execution_input = self.copy.path / 'execution-inputs' / 'local.properties'
        execution_input.parent.mkdir(parents=True)
        execution_input.write_text('sdk.dir=prepared\n')
        execution_input.chmod(0o640)
        toolchains.prepare(
            self.copy, tools={'sdk': root},
            copies=[(execution_input, 'build/local.properties')], save_project=True)

        recipe = json.loads(self.recipe_path(self.copy).read_text())
        cached_tool = Path(recipe['tools']['sdk']['source'])
        cached_copy = Path(recipe['copies'][0]['source'])
        self.assertTrue(cached_tool.is_relative_to(self.state / 'toolchain-cache'))
        self.assertTrue(cached_copy.is_relative_to(self.state / 'toolchain-cache'))
        self.assertNotIn(str(self.copy.directory), json.dumps(recipe))
        self.assertEqual(stat.S_IMODE((cached_tool / 'bin' / 'tool').stat().st_mode), 0o555)

        shutil.rmtree(self.copy.directory)
        shutil.rmtree(root)
        fresh = self.fresh_copy()
        with fresh.lock():
            result = toolchains.apply_saved_recipe(fresh)
        self.assertTrue(result['applied'])
        self.assertEqual((fresh.path / '.deepseek-tools/sdk/bin/tool').read_bytes(),
                         b'#!/bin/sh\nexit 0\n')
        prepared = fresh.path / 'build/local.properties'
        self.assertEqual(prepared.read_text(), 'sdk.dir=prepared\n')
        self.assertEqual(stat.S_IMODE(prepared.stat().st_mode), 0o640)
        self.assertNotEqual((fresh.path / '.deepseek-tools/sdk/bin/tool').stat().st_ino,
                            (cached_tool / 'bin' / 'tool').stat().st_ino)

    def test_missing_permanent_source_invalidates_the_recipe_once(self):
        permanent = self.root / 'permanent' / 'local.properties'
        permanent.parent.mkdir(parents=True)
        permanent.write_text('sdk.dir=host\n')
        toolchains.prepare(self.copy, copies=[(permanent, 'build/local.properties')],
                           save_project=True)
        recipe = self.recipe_path(self.copy)
        payload = json.loads(recipe.read_text())
        self.assertEqual(payload['copies'][0]['source'], str(permanent))

        permanent.unlink()
        fresh = self.fresh_copy()
        with self.assertRaises(toolchains.ToolchainError) as caught:
            with fresh.lock():
                toolchains.apply_saved_recipe(fresh)
        message = str(caught.exception).lower()
        self.assertIn('missing', message)
        self.assertIn('removed', message)
        self.assertFalse(recipe.exists())
        self.assertEqual((self.source / 'sample.txt').read_text(), 'committed\n')
        self.assertEqual((fresh.path / 'sample.txt').read_text(), 'committed\n')

        second = self.fresh_copy()
        with second.lock():
            self.assertEqual(toolchains.apply_saved_recipe(second),
                             {'applied': False, 'reason': 'no-recipe', 'toolchain': None})

    def test_evicted_cached_source_invalidates_the_recipe_clearly(self):
        root = self.software()
        toolchains.prepare(self.copy, tools={'sdk': root}, save_project=True)
        recipe = self.recipe_path(self.copy)
        cached = Path(json.loads(recipe.read_text())['tools']['sdk']['source'])
        collected = toolchain_cache.collect(self.state, apply=True, max_bytes=0, max_inodes=0)
        self.assertIn(cached.name, collected['removed_entries'])
        self.assertFalse(cached.exists())
        fresh = self.fresh_copy()
        with self.assertRaises(toolchains.ToolchainError) as caught:
            with fresh.lock():
                toolchains.apply_saved_recipe(fresh)
        self.assertIn('removed', str(caught.exception).lower())
        self.assertFalse(recipe.exists())

    def test_missing_source_does_not_delete_an_unsafe_recipe(self):
        fresh = self.fresh_copy()
        path = self.recipe_path(fresh)
        payload = {'version': 1, 'kind': 'toolchain-recipe',
                   'source_project': str(Path(fresh.source).resolve()), 'saved_at': time.time(),
                   'tools': {}, 'copies': [{'source': str(self.root / 'absent.txt'),
                                            'destination': '.git/config'}],
                   'environment': {}, 'probes': []}
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload))
        path.chmod(0o600)
        with self.assertRaises(toolchains.ToolchainError):
            with fresh.lock():
                toolchains.apply_saved_recipe(fresh)
        self.assertTrue(path.exists())

    def test_unsafe_recipe_is_preserved_for_inspection(self):
        fresh = self.fresh_copy()
        path = self.recipe_path(fresh)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {'version': 1, 'kind': 'toolchain-recipe',
                   'source_project': str(Path(fresh.source).resolve()), 'saved_at': time.time(),
                   'tools': {'sdk': {'source': '/usr', 'files': 0, 'bytes': 0,
                                     'links_skipped': 0}},
                   'copies': [], 'environment': {}, 'probes': [], 'foreign': True}
        path.write_text(json.dumps(payload))
        path.chmod(0o600)
        with self.assertRaises(toolchains.ToolchainError):
            with fresh.lock():
                toolchains.apply_saved_recipe(fresh)
        self.assertTrue(path.exists())

    def test_already_prepared_tree_is_still_validated(self):
        root = self.software()
        toolchains.prepare(self.copy, tools={'sdk': root})
        outside = self.root / 'outside'
        outside.mkdir()
        shutil.rmtree(self.copy.path / '.deepseek-tools' / 'sdk')
        os.symlink(str(outside), self.copy.path / '.deepseek-tools' / 'sdk')
        with self.assertRaises(toolchains.ToolchainError):
            with self.copy.lock():
                toolchains.apply_saved_recipe(self.copy)


if __name__ == '__main__':
    unittest.main()
