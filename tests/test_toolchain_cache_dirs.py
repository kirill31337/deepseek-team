"""T3 controls for staged parents, declared cache directories and v2 recipes.

The tests use real temporary Git workspaces.  They exercise the public prepare
surface and the saved-recipe replay path; private helpers are touched only to
observe content fingerprints or to inject a failed atomic recipe replacement.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import stat
import time
import unittest
from unittest import mock

from codex_deepseek_team import (check_reuse, delegation_cli, git_integrity,
                                 preparation_guard, toolchain_cache, toolchains,
                                 workspace, workspace_gc)
from tests.test_toolchains import ToolchainFixture, _commit, _git


class CacheDirFixture(ToolchainFixture):
    def cli_prepare(self, *options, copy=None):
        copy = self.copy if copy is None else copy
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = delegation_cli.main([
                'workspace', 'prepare', copy.id, '--state-dir', str(self.state),
                *options,
            ])
        return code, stdout.getvalue(), stderr.getvalue()

    def track(self, *relative, content='tracked\n'):
        for value in relative:
            path = self.source / value
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        _git(self.source, 'add', '-A')
        _commit(self.source, 'commit', '-qm', 'tracked fixture')

    def exclude_lines(self, copy):
        path = copy.path / '.git' / 'info' / 'exclude'
        if not path.exists():
            return []
        return path.read_text().splitlines()

    def write_excludes(self, copy, *lines):
        self.write_exclude_bytes(copy, ('\n'.join(lines) + '\n').encode(), mode=0o644)

    def write_exclude_bytes(self, copy, content, *, mode):
        path = copy.path / '.git' / 'info' / 'exclude'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        path.chmod(mode)
        copy.metadata['git_digest'] = workspace._git_digest(copy.path)
        copy.metadata['git_integrity'] = git_integrity.snapshot(copy.path)
        copy.save()

    def mark_executed(self, copy):
        with copy.lock():
            copy.begin('execution')
            copy.finish('succeeded')
        return self.reloaded(copy)

    def write_recipe(self, copy, payload):
        path = self.recipe_path(copy)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.parent.chmod(0o700)
        path.write_text(json.dumps(payload))
        path.chmod(0o600)
        return path

    def write_sized(self, path, size):
        path.parent.mkdir(parents=True, exist_ok=True)
        block = b'0123456789abcdef\n' * 8192
        with path.open('wb') as stream:
            remaining = size
            while remaining > 0:
                chunk = block[:remaining]
                stream.write(chunk)
                remaining -= len(chunk)


class StagedParentTests(CacheDirFixture):
    def test_environment_accepts_staged_target_descendant_and_future_parents(self):
        tree = self.root / 'inputs' / 'tree'
        (tree / 'sub').mkdir(parents=True)
        (tree / 'sub' / 'inner.txt').write_text('inner\n')
        source_file = self.root / 'inputs' / 'file.txt'
        source_file.write_text('file\n')
        environment = {
            'PATH': (f'{{workspace}}/stage/tree:{{workspace}}/stage/tree/sub:'
                     f'{{workspace}}/stage:{{workspace}}/stage/file'),
        }
        result = toolchains.prepare(
            self.copy,
            copies=[(tree, 'stage/tree'), (source_file, 'stage/file/config.txt')],
            environment=environment)
        self.assertEqual(result['environment'], environment)
        for relative in ('stage', 'stage/tree', 'stage/tree/sub', 'stage/file'):
            self.assertTrue((self.copy.path / relative).is_dir(), relative)
        expanded = toolchains.environment_for(self.copy, {'PATH': '/usr/bin'})['PATH']
        self.assertIn(str(self.copy.path / 'stage/tree/sub'), expanded)
        self.assertTrue(expanded.endswith(':/usr/bin'))

    def test_environment_rejects_missing_descendant_and_unrelated_future_parent(self):
        tree = self.root / 'inputs' / 'tree'
        (tree / 'sub').mkdir(parents=True)
        (tree / 'sub' / 'inner.txt').write_text('inner\n')
        for value in ('{workspace}/stage/tree/missing',
                      '{workspace}/stage/tree/../unrelated',
                      '{workspace}/stage/unrelated'):
            copy = self.fresh_copy()
            with self.subTest(value=value):
                message = None
                try:
                    toolchains.prepare(
                        copy, copies=[(tree, 'stage/tree')],
                        environment={'PATH': value})
                except toolchains.ToolchainError as error:
                    message = str(error)
                else:
                    self.fail('missing environment target was accepted')
                self.assertIn('does not resolve', message)
                self.assertIn('--copy', message)
                self.assertFalse((copy.path / 'stage').exists())

    def test_file_target_parent_created_by_publication(self):
        source_file = self.root / 'inputs' / 'settings.xml'
        source_file.parent.mkdir(parents=True)
        source_file.write_text('<settings/>\n')
        result = toolchains.prepare(
            self.copy,
            copies=[(source_file, 'home/config/settings.xml')],
            environment={'JAVA_HOME': '{workspace}/home/config'})
        self.assertEqual((self.copy.path / 'home/config/settings.xml').read_text(),
                         '<settings/>\n')
        self.assertTrue((self.copy.path / 'home/config').is_dir())
        self.assertEqual(result['environment']['JAVA_HOME'],
                         '{workspace}/home/config')


class CacheDeclarationTests(CacheDirFixture):
    def test_foreign_exclude_bytes_crlf_and_mode_survive_cache_insertion(self):
        original = b'foreign-\xff\r\n# keep-this-rule\r\n'
        self.write_exclude_bytes(self.copy, original, mode=0o600)
        copy = self.reloaded(self.copy)
        evidence_before = copy.metadata['git_integrity']
        index = copy.path / '.git' / 'index'
        index_before = index.read_bytes()

        toolchains.prepare(copy, cache_dirs=['cache'])

        exclude = copy.path / '.git' / 'info' / 'exclude'
        published = original + b'/cache\n'
        self.assertEqual(exclude.read_bytes(), published)
        self.assertEqual(stat.S_IMODE(exclude.stat().st_mode), 0o600)
        self.assertEqual(index.read_bytes(), index_before)
        loaded = self.reloaded(copy)
        self.assertEqual(loaded.metadata['toolchain']['exclude_ownership'],
                         {'/cache': 1})
        self.assertNotEqual(loaded.metadata['git_integrity'], evidence_before)
        loaded.verify()

        exclude.write_bytes(original)
        self.assertEqual(git_integrity.snapshot(copy.path), evidence_before)
        exclude.write_bytes(published)
        self.assertEqual(git_integrity.snapshot(copy.path),
                         loaded.metadata['git_integrity'])

    def test_foreign_exclude_without_final_lf_remains_insertion_prefix(self):
        original = b'foreign-\xff\r\n# keep-this-rule'
        self.write_exclude_bytes(self.copy, original, mode=0o600)

        toolchains.prepare(self.reloaded(self.copy), cache_dirs=['cache'])

        exclude = self.copy.path / '.git' / 'info' / 'exclude'
        self.assertEqual(exclude.read_bytes(), original + b'\n/cache\n')
        self.assertEqual(stat.S_IMODE(exclude.stat().st_mode), 0o600)

    def test_owned_removal_matches_only_exact_lf_terminated_occurrence(self):
        toolchains.prepare(self.copy, cache_dirs=['cache'])
        self.assertEqual(self.reloaded(self.copy).metadata['toolchain']
                         ['exclude_ownership'], {'/cache': 1})
        original = b'foreign-\xff\r\n/cache\n/cache\n/cache\r\n/cache'
        self.write_exclude_bytes(self.copy, original, mode=0o600)
        copy = self.reloaded(self.copy)
        index = copy.path / '.git' / 'index'
        index_before = index.read_bytes()

        toolchains.prepare(copy, cache_dirs=[])

        exclude = copy.path / '.git' / 'info' / 'exclude'
        self.assertEqual(exclude.read_bytes(),
                         b'foreign-\xff\r\n/cache\n/cache\r\n/cache')
        self.assertEqual(stat.S_IMODE(exclude.stat().st_mode), 0o600)
        self.assertEqual(index.read_bytes(), index_before)
        self.assertEqual(self.reloaded(copy).metadata['toolchain']
                         ['exclude_ownership'], {})

    def test_foreign_identical_cache_exclude_is_never_removed_as_owned(self):
        # Seed an exclusion that already existed when the copy was first attested:
        # preparation later declares the same text but never inserted that line.
        self.write_excludes(self.copy, '# foreign rule', '/cache')
        copy = self.reloaded(self.copy)
        toolchains.prepare(copy, cache_dirs=['cache'])
        self.assertIn('/cache', self.exclude_lines(copy))
        metadata = self.reloaded(copy).metadata['toolchain']
        self.assertEqual(metadata['cache_dirs'], ['cache'])
        self.assertEqual(metadata['exclude_ownership'], {})

        payload = copy.path / 'cache' / 'runtime.bin'
        payload.write_bytes(b'cache bytes\n')
        toolchains.prepare(self.reloaded(copy), cache_dirs=[])
        lines = self.exclude_lines(self.reloaded(copy))
        self.assertIn('# foreign rule', lines)
        self.assertIn('/cache', lines)
        self.assertEqual(payload.read_bytes(), b'cache bytes\n')
        self.assertEqual(self.reloaded(copy).metadata['toolchain']['cache_dirs'], [])

    def test_owned_declaration_removal_reveals_source_and_preserves_cache_bytes(self):
        index = self.copy.path / '.git' / 'index'
        index_before = index.read_bytes()
        toolchains.prepare(self.copy, cache_dirs=['cache'])
        self.assertEqual(self.reloaded(self.copy).metadata['toolchain']
                         ['exclude_ownership'], {'/cache': 1})
        payload = self.copy.path / 'cache' / 'runtime.bin'
        payload.write_bytes(b'cache bytes\n')

        toolchains.prepare(self.reloaded(self.copy), cache_dirs=[])
        lines = self.exclude_lines(self.reloaded(self.copy))
        self.assertNotIn('/cache', lines)
        self.assertEqual(payload.read_bytes(), b'cache bytes\n')
        self.assertEqual(index.read_bytes(), index_before)
        changed, _ignored = self.reloaded(self.copy).changes()
        self.assertIn('cache/runtime.bin', changed)

    def test_owned_removal_preserves_foreign_duplicate_identical_rule(self):
        toolchains.prepare(self.copy, cache_dirs=['cache'])
        self.write_excludes(self.copy, '/cache', '/cache')
        toolchains.prepare(self.reloaded(self.copy), cache_dirs=[])
        self.assertEqual(self.exclude_lines(self.reloaded(self.copy)), ['/cache'])

    def test_unknown_legacy_ownership_never_removes_existing_rule(self):
        toolchains.prepare(self.copy, cache_dirs=['cache'])
        record = self.reloaded(self.copy)
        record.metadata['toolchain'].pop('exclude_ownership')
        record.save()

        toolchains.prepare(self.reloaded(self.copy), cache_dirs=[])
        self.assertIn('/cache', self.exclude_lines(self.reloaded(self.copy)))
        self.assertEqual(self.reloaded(self.copy).metadata['toolchain']
                         ['exclude_ownership'], {})

    def test_cli_repeatable_cache_dirs_create_roots_metadata_excludes_and_recipe(self):
        code, stdout, stderr = self.cli_prepare(
            '--cache-dir', 'gradle-home',
            '--cache-dir', 'm2-home',
            '--save-project')
        self.assertEqual(code, 0, (stdout, stderr))
        for relative in ('gradle-home', 'm2-home'):
            self.assertTrue((self.copy.path / relative).is_dir(), relative)
        metadata = self.reloaded(self.copy).metadata['toolchain']
        self.assertEqual(metadata['version'], toolchains.TOOLCHAIN_VERSION)
        self.assertEqual(metadata['cache_dirs'], ['gradle-home', 'm2-home'])
        lines = self.exclude_lines(self.copy)
        self.assertIn('/gradle-home', lines)
        self.assertIn('/m2-home', lines)
        recipe = json.loads(self.recipe_path(self.copy).read_text())
        self.assertEqual(recipe['version'], 2)
        self.assertEqual(recipe['cache_dirs'], ['gradle-home', 'm2-home'])
        (self.copy.path / 'gradle-home' / 'runtime-cache.bin').write_bytes(b'cache\n')
        (self.copy.path / 'src').mkdir()
        (self.copy.path / 'src' / 'new.txt').write_text('new\n')
        changed, ignored = self.copy.changes()
        self.assertIn('src/new.txt', changed)
        self.assertNotIn('gradle-home/runtime-cache.bin', changed)
        patch = self.copy.diff_bytes()
        self.assertIn(b'src/new.txt', patch)
        self.assertNotIn(b'gradle-home/runtime-cache.bin', patch)
        self.assertGreaterEqual(ignored, 1)

    def test_cache_dir_rejects_unsafe_and_tracked_roots(self):
        self.track('tracked-cache/keep.txt')
        values = [
            '', '.', './cache', '/tmp/cache', '../cache', 'cache/../other', 'cache//nested',
            'cache/', '.git', '.git/cache', '.deepseek-tools', '.deepseek-tools/cache',
            'cache/.env', 'cache/.env/nested', '.env/cache', '.ssh/cache',
            'keys/id_rsa/cache', 'secrets/id_rsa', 'sample.txt', 'tracked-cache',
        ]
        for value in values:
            copy = self.fresh_copy()
            with self.subTest(value=value):
                with self.assertRaises(toolchains.ToolchainError) as caught:
                    toolchains.prepare(copy, cache_dirs=[value])
                self.assertIn(caught.exception.code, (78, 64))
                self.assertNotIn('toolchain', self.reloaded(copy).metadata)

    def test_cache_dir_rejects_symlink_ancestor_and_final_root(self):
        outside = self.root / 'outside'
        outside.mkdir()
        linked = self.fresh_copy()
        os.symlink(str(outside), linked.path / 'linked')
        with self.assertRaises(toolchains.ToolchainError):
            toolchains.prepare(linked, cache_dirs=['linked/cache'])
        self.assertFalse((outside / 'cache').exists())

        final = self.fresh_copy()
        os.symlink(str(outside), final.path / 'cache')
        with self.assertRaises(toolchains.ToolchainError):
            toolchains.prepare(final, cache_dirs=['cache'])
        self.assertEqual(list(outside.iterdir()), [])

    def test_unprepared_existing_data_is_preserved_with_refusal(self):
        copy = self.fresh_copy()
        cache = copy.path / 'cache'
        cache.mkdir()
        payload = cache / 'unprepared.bin'
        payload.write_bytes(b'preserve me\n')
        with self.assertRaises(toolchains.ToolchainError) as caught:
            toolchains.prepare(copy, cache_dirs=['cache'])
        message = str(caught.exception)
        self.assertIn('new workspace', message)
        self.assertIn('unprepared.bin', message)
        self.assertEqual(payload.read_bytes(), b'preserve me\n')
        self.assertNotIn('toolchain', self.reloaded(copy).metadata)

    def test_existing_tree_covered_by_prepared_copy_destination_is_accepted(self):
        source_file = self.root / 'inputs' / 'prepared.txt'
        source_file.parent.mkdir(parents=True)
        source_file.write_text('prepared\n')
        toolchains.prepare(self.copy, copies=[(source_file, 'cache/prepared.txt')])
        result = toolchains.prepare(
            self.reloaded(self.copy),
            copies=[(source_file, 'cache/prepared.txt')],
            cache_dirs=['cache'])
        self.assertEqual(result['cache_dirs'], ['cache'])
        self.assertEqual((self.copy.path / 'cache/prepared.txt').read_text(), 'prepared\n')
        self.assertEqual(self.reloaded(self.copy).metadata['toolchain']['cache_dirs'], ['cache'])
        self.assertIn('/cache', self.exclude_lines(self.copy))

    def test_repeating_a_declared_root_preserves_pre_execution_data(self):
        toolchains.prepare(self.copy, cache_dirs=['cache'])
        payload = self.copy.path / 'cache' / 'runtime.bin'
        payload.write_bytes(b'runtime\n')
        result = toolchains.prepare(self.reloaded(self.copy), cache_dirs=['cache'])
        self.assertEqual(result['cache_dirs'], ['cache'])
        self.assertEqual(payload.read_bytes(), b'runtime\n')
        self.assertEqual(self.exclude_lines(self.copy).count('/cache'), 1)

    def test_environment_alone_does_not_create_a_cache_exclusion(self):
        source = self.root / 'inputs' / 'prepared.txt'
        source.parent.mkdir(parents=True)
        source.write_text('prepared\n')
        result = toolchains.prepare(
            self.copy, copies=[(source, 'gradle-home/prepared.txt')],
            environment={'GRADLE_USER_HOME': '{workspace}/gradle-home'})
        self.assertEqual(result['cache_dirs'], [])
        lines = self.exclude_lines(self.copy)
        self.assertIn('/gradle-home/prepared.txt', lines)
        self.assertNotIn('/gradle-home', lines)
        (self.copy.path / 'gradle-home' / 'runtime.bin').write_bytes(b'runtime\n')
        changed, _ignored = self.copy.changes()
        self.assertIn('gradle-home/runtime.bin', changed)

    def test_declared_cache_set_changes_preparation_fingerprint(self):
        base = {'tools': {}, 'copies': [], 'environment': {}, 'probes': []}

        def conditions(cache_dirs):
            return preparation_guard.declarative_conditions(
                self.source, state=self.state,
                declarations={**base, 'cache_dirs': cache_dirs})

        empty = conditions([])
        cache = conditions(['cache', 'other'])
        reordered = conditions(['other', 'cache', 'cache'])
        malformed = conditions(['cache', 1])
        self.assertIsNotNone(empty.fingerprint)
        self.assertIsNotNone(cache.fingerprint)
        self.assertNotEqual(empty.fingerprint, cache.fingerprint)
        self.assertEqual(cache.fingerprint, reordered.fingerprint)
        self.assertIsNone(malformed.fingerprint)
        self.assertIn('declarations', malformed.unknown)

    def test_nested_roots_normalize_and_removal_preserves_data_and_foreign_rules(self):
        self.write_excludes(self.copy, '# foreign rule', '/foreign-rule')
        (self.copy.path / 'cache' / 'nested').mkdir(parents=True)
        toolchains.prepare(self.copy, cache_dirs=['cache', 'cache/nested', 'other'])
        metadata = self.reloaded(self.copy).metadata['toolchain']
        self.assertEqual(metadata['cache_dirs'], ['cache', 'other'])
        self.assertTrue((self.copy.path / 'cache/nested').is_dir())
        self.assertTrue((self.copy.path / 'other').is_dir())
        lines = self.exclude_lines(self.copy)
        self.assertIn('/cache', lines)
        self.assertIn('/other', lines)
        self.assertNotIn('/cache/nested', lines)

        payload = self.copy.path / 'other' / 'keep.bin'
        payload.write_bytes(b'cache data\n')
        toolchains.prepare(self.reloaded(self.copy), cache_dirs=['cache'])
        self.assertEqual(payload.read_bytes(), b'cache data\n')
        lines = self.exclude_lines(self.copy)
        self.assertIn('# foreign rule', lines)
        self.assertIn('/foreign-rule', lines)
        self.assertIn('/cache', lines)
        self.assertNotIn('/other', lines)
        changed, _ignored = self.reloaded(self.copy).changes()
        self.assertIn('other/keep.bin', changed)

    def test_cache_set_is_fixed_after_execution_and_same_set_is_idempotent(self):
        toolchains.prepare(self.copy, cache_dirs=['cache/a', 'cache/b'])
        executed = self.mark_executed(self.copy)
        with self.assertRaises(toolchains.ToolchainError) as caught:
            toolchains.prepare(executed, cache_dirs=['cache/a', 'cache/b', 'extra'])
        self.assertIn('fixed after the first execution', str(caught.exception))
        self.assertFalse((executed.path / 'extra').exists())
        self.assertEqual(self.reloaded(executed).metadata['toolchain']['cache_dirs'],
                         ['cache/a', 'cache/b'])

        executed = self.reloaded(executed)
        with self.assertRaises(toolchains.ToolchainError):
            toolchains.prepare(executed, cache_dirs=['cache/a'], recover=True)
        self.assertTrue((executed.path / 'cache/b').is_dir())

        executed = self.reloaded(executed)
        result = toolchains.prepare(
            executed, cache_dirs=['cache/b', 'cache/a', 'cache/a'], recover=True)
        self.assertEqual(result['cache_dirs'], ['cache/a', 'cache/b'])
        self.assertEqual(self.reloaded(executed).metadata['toolchain']['cache_dirs'],
                         ['cache/a', 'cache/b'])

    def test_one_pass_save_and_fresh_replay_with_nested_copies_and_common_parent(self):
        wrapper = self.root / 'inputs' / 'wrapper'
        (wrapper / 'dists').mkdir(parents=True)
        (wrapper / 'dists' / 'gradle.zip').write_bytes(b'wrapper\n')
        modules = self.root / 'inputs' / 'modules'
        (modules / 'org.example').mkdir(parents=True)
        (modules / 'org.example' / 'artifact.jar').write_bytes(b'jar\n')
        result = toolchains.prepare(
            self.copy,
            copies=[(wrapper, 'gradle-home/wrapper'),
                    (modules, 'gradle-home/caches/modules-2')],
            cache_dirs=['gradle-home'],
            environment={'GRADLE_USER_HOME': '{workspace}/gradle-home'},
            save_project=True)
        self.assertEqual(result['cache_dirs'], ['gradle-home'])
        self.assertTrue((self.copy.path / 'gradle-home' / 'wrapper/dists/gradle.zip').is_file())
        self.assertTrue((self.copy.path / 'gradle-home' / 'caches/modules-2/org.example/artifact.jar').is_file())
        recipe = json.loads(self.recipe_path(self.copy).read_text())
        self.assertEqual(recipe['version'], 2)
        self.assertEqual(recipe['cache_dirs'], ['gradle-home'])

        fresh = self.fresh_copy()
        with fresh.lock():
            replay = toolchains.apply_saved_recipe(fresh)
        self.assertTrue(replay['applied'])
        self.assertEqual(replay['cache_dirs'], ['gradle-home'])
        self.assertTrue((fresh.path / 'gradle-home/wrapper/dists/gradle.zip').is_file())
        self.assertTrue((fresh.path / 'gradle-home/caches/modules-2/org.example/artifact.jar').is_file())
        self.assertEqual(toolchains.environment_for(fresh, {'PATH': '/usr/bin'})['GRADLE_USER_HOME'],
                         str(fresh.path / 'gradle-home'))
        self.assertIn('/gradle-home', self.exclude_lines(fresh))
        (fresh.path / 'gradle-home' / 'runtime.bin').write_bytes(b'runtime\n')
        changed, _ignored = fresh.changes()
        self.assertNotIn('gradle-home/runtime.bin', changed)

    def test_existing_whole_root_copy_recipe_remains_replayable(self):
        gradle = self.root / 'inputs' / 'gradle-home'
        (gradle / 'wrapper').mkdir(parents=True)
        (gradle / 'wrapper' / 'gradle.zip').write_bytes(b'zip\n')
        toolchains.prepare(
            self.copy, copies=[(gradle, 'gradle-home')],
            environment={'GRADLE_USER_HOME': '{workspace}/gradle-home'},
            save_project=True)
        fresh = self.fresh_copy()
        with fresh.lock():
            replay = toolchains.apply_saved_recipe(fresh)
        self.assertTrue(replay['applied'])
        self.assertEqual((fresh.path / 'gradle-home/wrapper/gradle.zip').read_bytes(), b'zip\n')
        self.assertIn('/gradle-home', self.exclude_lines(fresh))

    def test_cache_contents_change_copy_and_check_fingerprints(self):
        toolchains.prepare(self.copy, cache_dirs=['cache'])
        record = self.reloaded(self.copy)
        copy_before = preparation_guard._copy_condition(record)
        tree_before = check_reuse._snapshot_tree(record)
        (record.path / 'cache' / 'runtime.bin').write_bytes(b'runtime\n')
        copy_after = preparation_guard._copy_condition(record)
        tree_after = check_reuse._snapshot_tree(record)
        self.assertNotEqual(copy_before, copy_after)
        self.assertNotEqual(tree_before, tree_after)

        (record.path / 'cache' / 'large.bin').write_bytes(b'x' * 4096)
        with mock.patch.object(check_reuse, 'MAX_TREE_BYTES', 1024):
            with self.assertRaises(check_reuse._Ineligible):
                check_reuse._snapshot_tree(record)

    def test_failed_recipe_replace_keeps_previous_recipe(self):
        toolchains.prepare(self.copy, cache_dirs=['first'],
                           probes=['echo first'], save_project=True)
        recipe = self.recipe_path(self.copy)
        before = recipe.read_bytes()
        real_replace = toolchains._replace

        def fail_recipe_replace(source, destination):
            if Path(destination).parent == recipe.parent:
                raise OSError('injected recipe publication failure')
            return real_replace(source, destination)

        with mock.patch.object(toolchains, '_replace', side_effect=fail_recipe_replace):
            with self.assertRaises(toolchains.ToolchainError):
                toolchains.prepare(
                    self.reloaded(self.copy), cache_dirs=['second'],
                    probes=['echo second'], save_project=True)
        self.assertEqual(recipe.read_bytes(), before)
        self.assertTrue((self.copy.path / 'second').is_dir())
        self.assertEqual(json.loads(recipe.read_text())['cache_dirs'], ['first'])

    def test_v1_recipe_replays_as_empty_cache_dirs_and_unknown_version_is_preserved(self):
        tool = self.software()
        cached_tool = toolchain_cache.snapshot(self.state, tool)
        fresh = self.fresh_copy()
        recipe = self.write_recipe(fresh, {
            'version': 1,
            'kind': 'toolchain-recipe',
            'source_project': str(Path(fresh.source).resolve()),
            'saved_at': time.time(),
            'tools': {'sdk': {'source': str(cached_tool), 'files': 3, 'bytes': 35,
                              'links_skipped': 0}},
            'copies': [],
            'environment': {},
            'probes': [],
        })
        before = recipe.read_bytes()
        with fresh.lock():
            result = toolchains.apply_saved_recipe(fresh)
        self.assertTrue(result['applied'])
        self.assertEqual(result['toolchain']['version'], 1)
        self.assertEqual(result['cache_dirs'], [])
        self.assertNotIn('cache_dirs', self.reloaded(fresh).metadata['toolchain'])
        self.assertEqual(recipe.read_bytes(), before)

        unknown = self.fresh_copy()
        unknown_recipe = self.write_recipe(unknown, {
            'version': 99,
            'kind': 'toolchain-recipe',
            'source_project': str(Path(unknown.source).resolve()),
            'saved_at': time.time(),
            'tools': {}, 'copies': [], 'environment': {}, 'probes': [],
        })
        unknown_before = unknown_recipe.read_bytes()
        with self.assertRaises(toolchains.ToolchainError):
            with unknown.lock():
                toolchains.apply_saved_recipe(unknown)
        self.assertEqual(unknown_recipe.read_bytes(), unknown_before)
        self.assertNotIn('toolchain', self.reloaded(unknown).metadata)

    def test_large_non_cache_result_is_still_bounded_by_capture_limit(self):
        from tests import test_gc_adversarial
        test_gc_adversarial._skip_without_namespace(self, 'capture')
        toolchains.prepare(self.copy, cache_dirs=['cache'])
        large = self.copy.path / 'large-result.txt'
        self.write_sized(large, workspace_gc.MAX_PATCH_BYTES + (1 << 20))
        with self.assertRaises(workspace.WorkspaceError) as caught:
            workspace_gc._capture(self.copy)
        self.assertEqual(caught.exception.code, 78)
        self.assertTrue(large.is_file())

    def test_large_cache_sibling_is_excluded_and_new_source_is_captured(self):
        from tests import test_gc_adversarial
        test_gc_adversarial._skip_without_namespace(self, 'capture')
        toolchains.prepare(self.copy, cache_dirs=['gradle-home'])
        cache_file = self.copy.path / 'gradle-home' / 'runtime-cache.bin'
        self.write_sized(cache_file, workspace_gc.MAX_PATCH_BYTES + (1 << 20))
        source = self.copy.path / 'src' / 'new.txt'
        source.parent.mkdir()
        source.write_text('new source\n')
        patch = workspace_gc._capture(self.copy)
        self.assertLessEqual(len(patch), workspace_gc.MAX_PATCH_BYTES)
        self.assertIn(b'src/new.txt', patch)
        self.assertNotIn(b'gradle-home/runtime-cache.bin', patch)
