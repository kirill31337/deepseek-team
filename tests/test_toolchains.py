"""Boundary tests for explicit workspace-local toolchain preparation.

Real temporary Git copies and fixture software trees are used throughout. No
/etc file is created, no runtime home is touched and no host preparation command
is executed: the Debian Java layout is injected as module constants so the real
containment and materialization logic runs against fixtures.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
import threading
import time
import unittest
from unittest import mock

from codex_deepseek_team import toolchains, workspace

TOOLCHAIN_VERSION = 1


def _git(root, *args, ok=(0,)):
    result = subprocess.run(['git', '-C', str(root), *args], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, check=False)
    if result.returncode not in ok:
        raise AssertionError(result.stderr.decode('utf-8', 'replace'))
    return result.stdout.decode('utf-8', 'replace')


def _commit(root, *args):
    _git(root, '-c', 'user.name=Toolchain', '-c', 'user.email=toolchain@example.test', *args)


class ToolchainFixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='dst-toolchains-')
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
        (self.source / '.gitignore').write_text('local.properties\n')
        (self.source / 'sample.txt').write_text('committed\n')
        _git(self.source, 'init', '-q', '-b', 'main')
        _git(self.source, 'add', '-A')
        _commit(self.source, 'commit', '-qm', 'base')
        self.copy = workspace.create(self.source, self.state)

    def fresh_copy(self):
        return workspace.create(self.source, self.state)

    def software(self, name='sdk', *, java=False, inside_link=False, escape_link=None,
                 cycle=None, fifo=False, credential=None):
        root = self.root / 'software' / name
        (root / 'bin').mkdir(parents=True)
        binary = root / 'bin' / ('java' if java else 'tool')
        binary.write_text('#!/bin/sh\nexit 0\n')
        binary.chmod(0o755)
        (root / 'lib' / 'sub').mkdir(parents=True)
        (root / 'lib' / 'data.txt').write_text('data\n')
        (root / 'lib' / 'sub' / 'inner.txt').write_text('inner\n')
        if inside_link:
            os.symlink('../lib/data.txt', root / 'bin' / 'data-link')
        if escape_link is not None:
            os.symlink(str(escape_link), root / 'lib' / 'escape-link')
        if cycle == 'self':
            os.symlink('self', root / 'lib' / 'self')
        elif cycle == 'dir':
            os.symlink('.', root / 'lib' / 'loop')
        elif cycle == 'pair':
            os.symlink('second', root / 'lib' / 'first')
            os.symlink('first', root / 'lib' / 'second')
        if fifo:
            os.mkfifo(root / 'lib' / 'pipe')
        if credential is not None:
            target = root / credential
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text('fixture credential\n')
        return root

    @contextlib.contextmanager
    def java_layout(self):
        etc = self.root / 'etc-fixture'
        (etc / 'java-17-openjdk' / 'security').mkdir(parents=True)
        (etc / 'ssl' / 'certs' / 'java').mkdir(parents=True)
        (etc / 'java-17-openjdk' / 'security' / 'default.policy').write_text('policy\n')
        (etc / 'ssl' / 'certs' / 'java' / 'cacerts').write_text('truststore\n')
        with mock.patch.object(toolchains, '_JAVA_CONF_RE',
                               re.compile(r'^' + re.escape(str(etc)) + r'/java-[0-9]+-openjdk/')), \
                mock.patch.object(toolchains, '_JAVA_TRUSTSTORE',
                                  str(etc / 'ssl' / 'certs' / 'java' / 'cacerts')):
            yield etc

    def recipe_path(self, copy):
        key = hashlib.sha256(str(Path(copy.source).resolve()).encode()).hexdigest()
        return copy.directory.parent.parent / 'toolchain-recipes' / (key + '.json')

    def write_recipe(self, copy, payload, *, mode=0o600):
        path = self.recipe_path(copy)
        path.parent.mkdir(parents=True, exist_ok=True)
        text = payload if isinstance(payload, str) else json.dumps(payload)
        path.write_text(text)
        path.chmod(mode)
        return path

    def recipe_payload(self, copy, **overrides):
        payload = {'version': TOOLCHAIN_VERSION, 'kind': 'toolchain-recipe',
                   'source_project': str(Path(copy.source).resolve()), 'saved_at': time.time(),
                   'tools': {}, 'copies': [], 'environment': {}, 'probes': []}
        payload.update(overrides)
        return payload

    def reloaded(self, copy):
        return workspace.load(self.state, copy.id)

    def assert_rejected(self, copy, **options):
        with self.assertRaises(toolchains.ToolchainError) as caught:
            toolchains.prepare(copy, **options)
        self.assertEqual(caught.exception.code, 78)
        self.assertNotIn('toolchain', self.reloaded(copy).metadata)
        self.assertEqual(self.reloaded(copy).metadata['status'], 'failed')
        return caught.exception

    def ignored_by_copy(self, copy, relative):
        output = _git(copy.path, 'check-ignore', '-v', '--', relative, ok=(0, 1))
        return output.strip()


class PrepareToolsTests(ToolchainFixture):
    def test_prepare_copies_tool_and_materializes_inside_links(self):
        root = self.software(inside_link=True)
        result = toolchains.prepare(self.copy, tools={'sdk': root})
        destination = self.copy.path / '.deepseek-tools' / 'sdk'
        self.assertEqual((destination / 'bin' / 'tool').read_text(), '#!/bin/sh\nexit 0\n')
        self.assertEqual(stat.S_IMODE((destination / 'bin' / 'tool').stat().st_mode), 0o755)
        link = destination / 'bin' / 'data-link'
        self.assertFalse(link.is_symlink())
        self.assertEqual(link.read_text(), 'data\n')
        self.assertEqual((destination / 'lib' / 'sub' / 'inner.txt').read_text(), 'inner\n')
        metadata = self.reloaded(self.copy).metadata['toolchain']
        self.assertEqual(metadata['version'], TOOLCHAIN_VERSION)
        self.assertEqual(sorted(metadata['tools']), ['sdk'])
        self.assertEqual(metadata['tools']['sdk']['source'], str(root))
        self.assertEqual(metadata['probes'], [])
        self.assertEqual(metadata['environment'], {})
        self.assertEqual(result['tools'][0]['name'], 'sdk')
        self.assertEqual(result['read_only_roots'], [str(destination)])
        self.assertTrue(result['recipe_saved'] is False)
        self.assertEqual(self.reloaded(self.copy).metadata['status'], 'succeeded')
        self.assertEqual(toolchains.read_only_roots(self.copy), [destination])
        self.assertEqual(toolchains.probes_for(self.copy), [])
        self.assertEqual(toolchains.environment_for(self.copy, {'PATH': '/usr/bin'}), {'PATH': '/usr/bin'})
        self.assertIn('/.deepseek-tools/', self.ignored_by_copy(self.copy, '.deepseek-tools/sdk/bin/tool'))
        # The product's tracked ignore file is never modified.
        self.assertEqual((self.copy.path / '.gitignore').read_text(), 'local.properties\n')
        self.assertNotIn('.deepseek-tools', (self.copy.path / '.gitignore').read_text())

    def test_prepare_is_idempotent_for_recorded_declarations(self):
        root = self.software()
        toolchains.prepare(self.copy, tools={'sdk': root})
        (root / 'lib' / 'data.txt').write_text('updated\n')
        toolchains.prepare(self.copy, tools={'sdk': root})
        self.assertEqual((self.copy.path / '.deepseek-tools/sdk/lib/data.txt').read_text(),
                         'updated\n')
        self.assertEqual(self.reloaded(self.copy).metadata['status'], 'succeeded')

    def test_prepare_rejects_escaping_link_before_metadata(self):
        outside = self.root / 'outside.txt'
        outside.write_text('host sentinel\n')
        outside.chmod(0o640)
        root = self.software(escape_link=outside)
        self.assert_rejected(self.copy, tools={'sdk': root})
        self.assertEqual(outside.read_text(), 'host sentinel\n')
        self.assertEqual(stat.S_IMODE(outside.stat().st_mode), 0o640)
        self.assertFalse((self.copy.path / '.deepseek-tools').exists())

    def test_prepare_skips_dangling_links_with_safe_targets(self):
        root = self.software()
        os.symlink('absent.txt', root / 'lib' / 'internal-missing')
        with self.java_layout() as etc:
            java_root = self.software(name='jdk', java=True)
            os.symlink(str(etc / 'java-17-openjdk' / 'security' / 'absent.policy'),
                       java_root / 'lib' / 'policy-missing')
            result = toolchains.prepare(self.copy, tools={'sdk': root, 'jdk': java_root})
        self.assertFalse((self.copy.path / '.deepseek-tools/sdk/lib/internal-missing').exists())
        self.assertFalse((self.copy.path / '.deepseek-tools/jdk/lib/policy-missing').exists())
        entries = {entry['name']: entry for entry in result['tools']}
        self.assertEqual(entries['sdk']['links_skipped'], 1)
        self.assertEqual(entries['jdk']['links_skipped'], 1)
        self.assertEqual(self.reloaded(self.copy).metadata['toolchain']['tools']['sdk']
                         ['links_skipped'], 1)

    def test_prepare_rejects_dangling_unapproved_external_links(self):
        outside = self.root / 'outside'
        outside.mkdir()
        root = self.software()
        os.symlink(str(outside / 'missing.txt'), root / 'lib' / 'escape-missing')
        self.assert_rejected(self.fresh_copy(), tools={'sdk': root})
        java_root = self.software(name='jdk', java=True)
        os.symlink(str(outside / 'missing.txt'), java_root / 'lib' / 'escape-missing')
        self.assert_rejected(self.fresh_copy(), tools={'sdk': java_root})

    def test_prepare_rejects_symlink_cycles(self):
        for cycle in ('self', 'dir', 'pair'):
            copy = self.fresh_copy()
            root = self.software(name='cycle-' + cycle, cycle=cycle)
            self.assert_rejected(copy, tools={'sdk': root})

    def test_prepare_rejects_special_files(self):
        root = self.software(fifo=True)
        self.assert_rejected(self.copy, tools={'sdk': root})

    def test_prepare_rejects_credential_like_entries(self):
        root = self.software(credential='conf/.ssh/id_rsa')
        self.assert_rejected(self.copy, tools={'sdk': root})

    def test_prepare_rejects_symlinked_software_root_with_credential_like_target(self):
        target = self.root / 'secrets' / 'private.key'
        (target / 'bin').mkdir(parents=True)
        (target / 'bin' / 'tool').write_text('#!/bin/sh\nexit 0\n')
        (target / 'canary.txt').write_text('canary\n')
        (self.root / 'software').mkdir(parents=True, exist_ok=True)
        alias = self.root / 'software' / 'sdk-alias'
        os.symlink(str(target), alias)
        self.assert_rejected(self.copy, tools={'sdk': alias})
        self.assertEqual((target / 'canary.txt').read_text(), 'canary\n')

    def test_prepare_rejects_symlink_to_credential_like_file_target(self):
        target = self.root / 'secrets' / 'private.key'
        target.parent.mkdir(parents=True)
        target.write_text('fixture credential\n')
        root = self.software()
        os.symlink(str(target), root / 'lib' / 'plain.txt')
        self.assert_rejected(self.copy, tools={'sdk': root})
        self.assertEqual(target.read_text(), 'fixture credential\n')

    def test_tool_replacement_preserves_tracked_source_beneath_tool_area(self):
        tracked = self.source / '.deepseek-tools' / 'sdk' / 'keep.txt'
        tracked.parent.mkdir(parents=True)
        tracked.write_text('tracked\n')
        _git(self.source, 'add', '-A')
        _commit(self.source, 'commit', '-qm', 'tracked tool area')
        copy = self.fresh_copy()
        with self.assertRaises(toolchains.ToolchainError):
            toolchains.prepare(copy, tools={'sdk': self.software()})
        self.assertEqual((copy.path / '.deepseek-tools' / 'sdk' / 'keep.txt').read_text(),
                         'tracked\n')
        self.assertIn('.deepseek-tools/sdk/keep.txt',
                      _git(copy.path, 'ls-files', '-z').replace('\0', '\n'))

    def test_stale_tool_cleanup_preserves_tracked_source(self):
        tracked = self.source / '.deepseek-tools' / 'other' / 'keep.txt'
        tracked.parent.mkdir(parents=True)
        tracked.write_text('tracked\n')
        _git(self.source, 'add', '-A')
        _commit(self.source, 'commit', '-qm', 'tracked stale tool area')
        copy = self.fresh_copy()
        record = self.reloaded(copy)
        record.metadata['toolchain'] = {
            'version': TOOLCHAIN_VERSION, 'prepared_at': time.time(),
            'source_project': str(Path(copy.source).resolve()),
            'tools': {'other': {'source': '/opt/other', 'files': 0, 'bytes': 0,
                                'links_skipped': 0}},
            'copies': [], 'environment': {}, 'probes': []}
        record.save()
        with self.assertRaises(toolchains.ToolchainError):
            toolchains.prepare(record, tools={'sdk': self.software()})
        self.assertEqual((record.path / '.deepseek-tools' / 'other' / 'keep.txt').read_text(),
                         'tracked\n')

    def test_stale_tool_cleanup_rejects_replaced_private_tool_area(self):
        root = self.software()
        toolchains.prepare(self.copy, tools={'sdk': root})
        outside = self.root / 'outside-tools'
        (outside / 'sdk').mkdir(parents=True)
        (outside / 'sdk' / 'keep.txt').write_text('canary\n')
        shutil.rmtree(self.copy.path / '.deepseek-tools')
        os.symlink(str(outside), self.copy.path / '.deepseek-tools')
        record = self.reloaded(self.copy)
        with self.assertRaises(toolchains.ToolchainError):
            toolchains.prepare(record, recover=True)
        self.assertEqual((outside / 'sdk' / 'keep.txt').read_text(), 'canary\n')

    def test_prepare_rejects_dot_git_entries(self):
        root = self.software()
        (root / '.git').mkdir()
        self.assert_rejected(self.copy, tools={'sdk': root})

    def test_prepare_rejects_invalid_tool_names(self):
        root = self.software()
        for name in ('', '1sdk', 'a/b', '../sdk', 'a.b', 'a' * 65):
            copy = self.fresh_copy()
            self.assert_rejected(copy, tools={name: root})

    def test_prepare_rejects_broad_and_project_roots(self):
        candidates = ['/', '/usr', '/etc', '/home', '/root', '/tmp', '/var', '/run',
                      str(Path.home()), str(Path.home() / '.local'),
                      str(self.source), str(self.state)]
        for candidate in candidates:
            copy = self.fresh_copy()
            self.assert_rejected(copy, tools={'sdk': candidate})

    def test_prepare_rejects_sources_inside_the_owned_copy(self):
        copy = self.fresh_copy()
        self.assert_rejected(copy, tools={'sdk': copy.path})
        copy = self.fresh_copy()
        self.assert_rejected(copy, tools={'sdk': copy.path / 'sample.txt'})

    def test_prepare_accepts_symlinked_software_root(self):
        root = self.software()
        alias = self.root / 'software' / 'sdk-link'
        os.symlink(str(root), alias)
        toolchains.prepare(self.copy, tools={'sdk': alias})
        self.assertTrue((self.copy.path / '.deepseek-tools/sdk/bin/tool').is_file())

    def test_prepare_does_not_modify_source_or_isolated_homes(self):
        before = _git(self.source, 'status', '--porcelain=v1', '--untracked-files=all')
        head = _git(self.source, 'rev-parse', 'HEAD')
        root = self.software(inside_link=True)
        toolchains.prepare(self.copy, tools={'sdk': root})
        self.assertEqual(_git(self.source, 'status', '--porcelain=v1', '--untracked-files=all'), before)
        self.assertEqual(_git(self.source, 'rev-parse', 'HEAD'), head)
        for name in ('home', 'codex', 'claude', 'xdg-config', 'xdg-state'):
            self.assertEqual(list((self.root / name).iterdir()), [])

    def test_declarative_only_preparation_is_valid(self):
        result = toolchains.prepare(self.copy)
        self.assertEqual(result['tools'], [])
        self.assertEqual(result['copies'], [])
        self.assertEqual(result['environment'], {})
        self.assertEqual(result['probes'], [])
        self.assertEqual(result['read_only_roots'], [])
        metadata = self.reloaded(self.copy).metadata['toolchain']
        self.assertEqual(metadata['version'], TOOLCHAIN_VERSION)
        self.assertEqual(metadata['tools'], {})
        self.assertEqual(metadata['copies'], [])
        self.assertEqual(self.reloaded(self.copy).metadata['status'], 'succeeded')
        self.assertEqual(toolchains.environment_for(self.copy, {'PATH': '/usr/bin'}),
                         {'PATH': '/usr/bin'})

    def test_reprepare_removes_stale_recorded_artifacts(self):
        first = self.software(name='first')
        second = self.software(name='second')
        stale_file = self.root / 'inputs' / 'stale.properties'
        stale_file.parent.mkdir(parents=True, exist_ok=True)
        stale_file.write_text('stale\n')
        toolchains.prepare(self.copy, tools={'first': first, 'second': second},
                           copies=[(stale_file, 'prepared/stale.properties')])
        toolchains.prepare(self.copy, tools={'first': first})
        self.assertFalse((self.copy.path / '.deepseek-tools' / 'second').exists())
        self.assertFalse((self.copy.path / 'prepared' / 'stale.properties').exists())
        metadata = self.reloaded(self.copy).metadata['toolchain']
        self.assertEqual(sorted(metadata['tools']), ['first'])
        self.assertEqual(metadata['copies'], [])
        self.assertEqual(toolchains.read_only_roots(
            self.reloaded(self.copy)), [self.copy.path / '.deepseek-tools' / 'first'])

    def test_failed_prepare_retains_copy_for_explicit_recovery(self):
        root = self.software(escape_link=self.source / 'sample.txt')
        self.assert_rejected(self.copy, tools={'sdk': root})
        with self.assertRaises(workspace.WorkspaceError):
            toolchains.prepare(self.copy, tools={'sdk': root})
        toolchains.prepare(self.copy, tools={'sdk': self.software(name='clean')}, recover=True)
        self.assertEqual(self.reloaded(self.copy).metadata['status'], 'succeeded')


class JavaConfigurationTests(ToolchainFixture):
    def test_java_tool_omits_debian_docs_and_source_links_without_following(self):
        root = self.software(name='jdk-omissions', java=True)
        os.symlink('/usr/share/doc/openjdk-17-doc', root / 'docs')
        os.symlink('/usr/lib/jvm/openjdk-17/src.zip', root / 'src.zip')
        os.symlink('/usr/lib/jvm/openjdk-8/src.zip', root / 'lib' / 'src.zip')
        with mock.patch.object(toolchains, '_resolve_link',
                               side_effect=AssertionError('omitted link was followed')):
            result = toolchains.prepare(self.copy, tools={'JAVA_HOME': root})
        destination = self.copy.path / '.deepseek-tools' / 'JAVA_HOME'
        for relative in ('docs', 'src.zip', 'lib/src.zip'):
            self.assertFalse(os.path.lexists(destination / relative), relative)
        entry = {item['name']: item for item in result['tools']}['JAVA_HOME']
        self.assertEqual(entry['links_skipped'], 3)

    def test_java_tool_rejects_unapproved_docs_and_source_links(self):
        cases = [('docs', '/usr/share/doc/other-package'),
                 ('docs', '/usr/share/doc/openjdk-doc'),
                 ('lib/docs', '/usr/share/doc/openjdk-17-doc'),
                 ('src.zip', '/usr/lib/jvm/openjdk-17/lib/src.zip'),
                 ('src.zip', '/usr/lib/jvm/java-17-openjdk/src.zip'),
                 ('lib/src.zip', '/tmp/src.zip')]
        for index, (relative, target) in enumerate(cases):
            root = self.software(name='jdk-reject-' + str(index), java=True)
            destination = root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.symlink(target, destination)
            self.assert_rejected(self.fresh_copy(), tools={'JAVA_HOME': root})

    def test_non_java_tool_rejects_debian_docs_link(self):
        root = self.software(name='sdk-docs')
        os.symlink('/usr/share/doc/openjdk-17-doc', root / 'docs')
        self.assert_rejected(self.copy, tools={'sdk': root})

    def test_non_java_tool_rejects_debian_source_link(self):
        root = self.software(name='sdk-sources')
        os.symlink('/usr/lib/jvm/openjdk-17/src.zip', root / 'src.zip')
        self.assert_rejected(self.copy, tools={'sdk': root})

    def test_java_tool_still_materializes_internal_docs_link(self):
        root = self.software(name='jdk-internal-docs', java=True)
        os.symlink('lib/data.txt', root / 'docs')
        toolchains.prepare(self.copy, tools={'JAVA_HOME': root})
        self.assertEqual((self.copy.path / '.deepseek-tools' / 'JAVA_HOME' / 'docs').read_text(),
                         'data\n')

    def test_default_java_external_patterns_are_narrow(self):
        allowed = ['/etc/ssl/certs/java/cacerts', '/etc/java-17-openjdk/security/cacerts',
                   '/etc/java-8-openjdk/lib/security/cacerts']
        rejected = ['/etc/java-openjdk/x', '/etc/java-17-openjdk', '/etc/java-17-openjdk-evil/x',
                    '/etc/shadow', '/usr/etc/java-17-openjdk/x',
                    '/etc/ssl/certs/java/cacerts.bak', '/etc/ssl/certs/java/other']
        for value in allowed:
            self.assertTrue(toolchains._java_external_allowed(value), value)
        for value in rejected:
            self.assertFalse(toolchains._java_external_allowed(value), value)

    def test_java_tool_materializes_approved_configuration_links(self):
        with self.java_layout() as etc:
            root = self.software(java=True)
            os.symlink(str(etc / 'java-17-openjdk/security/default.policy'),
                       root / 'lib' / 'security-policy')
            os.symlink(str(etc / 'ssl/certs/java/cacerts'), root / 'lib' / 'cacerts')
            toolchains.prepare(self.copy, tools={'JAVA_HOME': root})
            destination = self.copy.path / '.deepseek-tools' / 'JAVA_HOME'
            self.assertEqual((destination / 'lib/security-policy').read_text(), 'policy\n')
            self.assertFalse((destination / 'lib/security-policy').is_symlink())
            self.assertEqual((destination / 'lib/cacerts').read_text(), 'truststore\n')
            self.assertFalse((destination / 'lib/cacerts').is_symlink())

    def test_non_java_tool_cannot_use_java_external_links(self):
        with self.java_layout() as etc:
            root = self.software(java=False)
            os.symlink(str(etc / 'ssl/certs/java/cacerts'), root / 'lib' / 'cacerts')
            self.assert_rejected(self.copy, tools={'sdk': root})

    def test_java_tool_rejects_unapproved_external_links(self):
        outside = self.root / 'secret.txt'
        outside.write_text('sentinel\n')
        root = self.software(java=True, escape_link=outside)
        self.assert_rejected(self.copy, tools={'JAVA_HOME': root})

    def test_java_tool_rejects_symlinked_external_directory(self):
        with self.java_layout() as etc:
            root = self.software(java=True)
            os.symlink(str(etc / 'java-17-openjdk' / 'security'), root / 'lib' / 'security-dir')
            self.assert_rejected(self.copy, tools={'JAVA_HOME': root})


class PrepareCopiesTests(ToolchainFixture):
    def test_copies_file_and_cache_directory_are_ignored_by_the_copy(self):
        source_file = self.root / 'inputs' / 'local.properties'
        source_file.parent.mkdir()
        source_file.write_text('sdk.dir=/opt/sdk\n')
        source_file.chmod(0o640)
        cache = self.root / 'caches' / 'gradle'
        (cache / 'nested').mkdir(parents=True)
        (cache / 'nested' / 'entry.bin').write_bytes(b'cache-bytes')
        os.symlink('nested/entry.bin', cache / 'entry-link')
        result = toolchains.prepare(self.copy, copies=[
            (source_file, 'build/prepared.properties'), (cache, 'caches/gradle')])
        self.assertEqual((self.copy.path / 'build/prepared.properties').read_text(), 'sdk.dir=/opt/sdk\n')
        self.assertEqual(stat.S_IMODE((self.copy.path / 'build/prepared.properties').stat().st_mode), 0o640)
        self.assertEqual((self.copy.path / 'caches/gradle/nested/entry.bin').read_bytes(), b'cache-bytes')
        link = self.copy.path / 'caches/gradle/entry-link'
        self.assertFalse(link.is_symlink())
        self.assertEqual(link.read_bytes(), b'cache-bytes')
        self.assertEqual(sorted(item['destination'] for item in result['copies']),
                         ['build/prepared.properties', 'caches/gradle'])
        self.assertNotIn('build/prepared.properties', self.copy.changes()[0])
        self.assertNotIn('caches/gradle/nested/entry.bin', self.copy.changes()[0])
        self.assertIn('/build/prepared.properties',
                      self.ignored_by_copy(self.copy, 'build/prepared.properties'))
        self.assertEqual(toolchains.read_only_roots(self.copy), [])

    def test_copies_reject_escaping_or_protected_destinations(self):
        source_file = self.root / 'inputs' / 'payload.txt'
        source_file.parent.mkdir()
        source_file.write_text('payload\n')
        destinations = ['../escape.txt', '/absolute.txt', 'a/../../escape.txt', '',
                        '.git/config', '.git/hooks/pre-commit', 'nested/.git/config',
                        '.deepseek-tools/sdk', 'sample.txt']
        for destination in destinations:
            copy = self.fresh_copy()
            with self.assertRaises(toolchains.ToolchainError):
                toolchains.prepare(copy, copies=[(source_file, destination)])
            self.assertFalse((copy.directory / 'escape.txt').exists())
            self.assertNotIn('toolchain', self.reloaded(copy).metadata)

    def test_copies_reject_existing_unrecorded_destination_until_recovery(self):
        source_file = self.root / 'inputs' / 'payload.txt'
        source_file.parent.mkdir()
        source_file.write_text('prepared\n')
        destination = self.copy.path / 'prepared' / 'config.txt'
        destination.parent.mkdir()
        destination.write_text('foreign\n')
        self.assert_rejected(self.copy, copies=[(source_file, 'prepared/config.txt')])
        self.assertEqual(destination.read_text(), 'foreign\n')
        toolchains.prepare(self.copy, copies=[(source_file, 'prepared/config.txt')], recover=True)
        self.assertEqual(destination.read_text(), 'prepared\n')

    def test_copies_reject_credential_like_sources_and_destinations(self):
        secret = self.root / 'inputs' / '.netrc'
        secret.parent.mkdir()
        secret.write_text('fixture\n')
        self.assert_rejected(self.fresh_copy(), copies=[(secret, 'prepared/netrc')])
        self.assert_rejected(self.fresh_copy(), copies=[(secret, '.env')])
        tree = self.root / 'inputs' / 'tree'
        (tree / 'keys').mkdir(parents=True)
        (tree / 'keys' / 'id_rsa').write_text('fixture\n')
        self.assert_rejected(self.fresh_copy(), copies=[(tree, 'prepared/tree')])

    def test_copies_reject_symlink_to_credential_like_target(self):
        secret = self.root / 'secrets' / 'private.key'
        secret.parent.mkdir(parents=True)
        secret.write_text('fixture credential\n')
        alias = self.root / 'inputs' / 'plain.txt'
        alias.parent.mkdir(parents=True)
        os.symlink(str(secret), alias)
        self.assert_rejected(self.copy, copies=[(alias, 'prepared/plain.txt')])
        self.assertEqual(secret.read_text(), 'fixture credential\n')
        self.assertFalse((self.copy.path / 'prepared').exists())

    def test_copies_reject_symlinked_destination_parent_before_removing_canary(self):
        source_file = self.root / 'inputs' / 'payload.txt'
        source_file.parent.mkdir(parents=True)
        source_file.write_text('prepared\n')
        toolchains.prepare(self.copy, copies=[(source_file, 'prepared/config.txt')])
        outside = self.root / 'outside'
        outside.mkdir()
        canary = outside / 'config.txt'
        canary.write_text('canary\n')
        shutil.rmtree(self.copy.path / 'prepared')
        os.symlink(str(outside), self.copy.path / 'prepared')
        record = self.reloaded(self.copy)
        with self.assertRaises(toolchains.ToolchainError):
            toolchains.prepare(record, copies=[(source_file, 'prepared/config.txt')],
                               recover=True)
        self.assertEqual(canary.read_text(), 'canary\n')

    def test_stale_cleanup_validates_parents_before_removing_canaries(self):
        first = self.root / 'inputs' / 'first.txt'
        second = self.root / 'inputs' / 'second.txt'
        first.parent.mkdir(parents=True)
        first.write_text('first\n')
        second.write_text('second\n')
        toolchains.prepare(self.copy, copies=[(first, 'prepared/a.txt'),
                                              (second, 'prepared/b.txt')])
        outside = self.root / 'outside'
        outside.mkdir()
        (outside / 'a.txt').write_text('canary-a\n')
        (outside / 'b.txt').write_text('canary-b\n')
        shutil.rmtree(self.copy.path / 'prepared')
        os.symlink(str(outside), self.copy.path / 'prepared')
        record = self.reloaded(self.copy)
        with self.assertRaises(toolchains.ToolchainError):
            toolchains.prepare(record, recover=True)
        self.assertEqual((outside / 'a.txt').read_text(), 'canary-a\n')
        self.assertEqual((outside / 'b.txt').read_text(), 'canary-b\n')

    def test_copies_reject_dot_and_control_character_destinations(self):
        source_file = self.root / 'inputs' / 'payload.txt'
        source_file.parent.mkdir(parents=True)
        source_file.write_text('payload\n')
        for destination in ('.', 'prepared/lf\nINJECTED', 'prepared/crlf\rINJECTED'):
            copy = self.fresh_copy()
            with self.assertRaises(toolchains.ToolchainError):
                toolchains.prepare(copy, copies=[(source_file, destination)])
            self.assertNotIn('toolchain', self.reloaded(copy).metadata)
            exclude = copy.path / '.git' / 'info' / 'exclude'
            if exclude.exists():
                self.assertNotIn('INJECTED', exclude.read_text())

    def test_copies_register_prepared_paths_and_clear_stale_registration(self):
        source_file = self.root / 'inputs' / 'local.properties'
        source_file.parent.mkdir(parents=True)
        source_file.write_text('sdk.dir=/opt/sdk\n')
        toolchains.prepare(self.copy, copies=[(source_file, 'build/prepared.properties')])
        self.assertEqual(self.reloaded(self.copy).metadata['prepared_paths'],
                         ['build/prepared.properties'])
        self.assertNotIn('build/prepared.properties', self.copy.changes()[0])
        self.assertIn('/build/prepared.properties',
                      self.ignored_by_copy(self.copy, 'build/prepared.properties'))
        record = self.reloaded(self.copy)
        toolchains.prepare(record, recover=True)
        self.assertFalse((record.path / 'build' / 'prepared.properties').exists())
        self.assertNotIn('prepared_paths', self.reloaded(record).metadata)

    def test_prepare_preserves_other_prepared_paths_registrations(self):
        record = self.reloaded(self.copy)
        record.metadata['prepared_paths'] = ['imported/file.txt']
        record.save()
        source_file = self.root / 'inputs' / 'payload.txt'
        source_file.parent.mkdir(parents=True)
        source_file.write_text('payload\n')
        toolchains.prepare(record, copies=[(source_file, 'build/prepared.properties')])
        self.assertEqual(self.reloaded(record).metadata['prepared_paths'],
                         ['build/prepared.properties', 'imported/file.txt'])
        toolchains.prepare(self.reloaded(record), recover=True)
        self.assertEqual(self.reloaded(record).metadata['prepared_paths'],
                         ['imported/file.txt'])

    def test_copies_keep_literal_wildcard_names_escaped_in_excludes(self):
        source_file = self.root / 'inputs' / 'payload.txt'
        source_file.parent.mkdir(parents=True)
        source_file.write_text('payload\n')
        destination = 'prepared/literal * ? [1] trailing '
        toolchains.prepare(self.copy, copies=[(source_file, destination)])
        self.assertEqual((self.copy.path / destination).read_text(), 'payload\n')
        exclude = (self.copy.path / '.git' / 'info' / 'exclude').read_text()
        self.assertIn('/prepared/literal \\* \\? \\[1\\] trailing\\ ', exclude)
        self.assertEqual(self.ignored_by_copy(self.copy, 'prepared/literal x y 1 trailing'), '')

    def test_copies_reject_special_files_cycles_and_escaping_links(self):
        outside = self.root / 'outside.txt'
        outside.write_text('sentinel\n')
        for name, build in [
                ('fifo', lambda path: os.mkfifo(path / 'pipe')),
                ('cycle', lambda path: os.symlink('.', path / 'loop')),
                ('escape', lambda path: os.symlink(str(outside), path / 'escape')),
        ]:
            tree = self.root / 'inputs' / name
            tree.mkdir(parents=True)
            (tree / 'data.txt').write_text('data\n')
            build(tree)
            copy = self.fresh_copy()
            self.assert_rejected(copy, copies=[(tree, 'prepared/' + name)])
        self.assertEqual(outside.read_text(), 'sentinel\n')


class EnvironmentTests(ToolchainFixture):
    def test_environment_expands_placeholders_and_prepends_path(self):
        root = self.software()
        declarations = {'JAVA_HOME': '{workspace}/.deepseek-tools/sdk',
                        'PATH': '{workspace}/.deepseek-tools/sdk/bin',
                        'GRADLE_USER_HOME': '{workspace}/caches/gradle'}
        (self.copy.path / 'caches' / 'gradle').mkdir(parents=True)
        probes = ['{workspace}/.deepseek-tools/sdk/bin/tool --version']
        result = toolchains.prepare(self.copy, tools={'sdk': root}, environment=declarations,
                                    probes=probes)
        self.assertEqual(result['environment'], declarations)
        self.assertEqual(result['probes'], probes)
        environment = toolchains.environment_for(self.copy, {'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'})
        self.assertEqual(environment['JAVA_HOME'], str(self.copy.path / '.deepseek-tools/sdk'))
        self.assertEqual(environment['GRADLE_USER_HOME'], str(self.copy.path / 'caches/gradle'))
        self.assertEqual(environment['PATH'],
                         str(self.copy.path / '.deepseek-tools/sdk/bin') + ':/usr/bin:/bin')
        self.assertEqual(environment['LANG'], 'C.UTF-8')
        self.assertEqual(toolchains.probes_for(self.copy),
                         [str(self.copy.path / '.deepseek-tools/sdk/bin/tool') + ' --version'])
        # Loading metadata never executes a probe or host command.
        marker = self.root / 'probe-marker'
        record = self.reloaded(self.copy)
        record.metadata['toolchain']['probes'] = ['touch ' + str(marker)]
        record.save()
        self.assertEqual(toolchains.probes_for(self.reloaded(self.copy)),
                         ['touch ' + str(marker)])
        self.assertFalse(marker.exists())

    def test_environment_rejects_forbidden_names(self):
        for name in ('HOME', 'LD_PRELOAD', 'LD_LIBRARY_PATH', 'PYTHONPATH', 'PYTHONSTARTUP',
                     'BASH_ENV', 'OPENAI_API_KEY', 'DEEPSEEK_API_KEY', 'ANTHROPIC_API_KEY',
                     'CODEX_HOME', 'CLAUDE_CONFIG_DIR', 'XDG_CONFIG_HOME', 'SHELL', 'GIT_DIR'):
            copy = self.fresh_copy()
            self.assert_rejected(copy, environment={name: '{workspace}/dir'})

    def test_environment_rejects_unsafe_values(self):
        values = ['', '.deepseek-tools/sdk', '/absolute/outside', '{workspace}',
                  '{workspace}/missing', '{workspace}/../escape', '{workspace}/../state',
                  '{home}/sdk', 'x{workspace}y', '{workspace}/.deepseek-tools/sdk\0extra',
                  ['list']]
        for value in values:
            copy = self.fresh_copy()
            self.assert_rejected(copy, environment={'JAVA_HOME': value})

    def test_path_rejects_non_workspace_and_empty_elements(self):
        (self.copy.path / 'prepared').mkdir()
        for value in ('/usr/bin', '{workspace}/prepared:/usr/bin',
                      '{workspace}/prepared:', ':{workspace}/prepared',
                      '{workspace}/prepared::{workspace}/other'):
            copy = self.fresh_copy()
            (copy.path / 'prepared').mkdir()
            self.assert_rejected(copy, environment={'PATH': value})

    def test_environment_rejects_probes_with_unsafe_values(self):
        for probes in ([42], [''], ['   '], ['x\0y'], {'a': 'b'}, ['ok', 3.5]):
            copy = self.fresh_copy()
            self.assert_rejected(copy, probes=probes)

    def test_environment_for_without_preparation_returns_base(self):
        base = {'PATH': '/usr/bin', 'LANG': 'C.UTF-8'}
        self.assertEqual(toolchains.environment_for(self.copy, base), base)
        self.assertEqual(toolchains.probes_for(self.copy), [])
        self.assertEqual(toolchains.read_only_roots(self.copy), [])

    def test_environment_for_revalidates_missing_workspace_paths(self):
        root = self.software()
        toolchains.prepare(self.copy, tools={'sdk': root},
                           environment={'JAVA_HOME': '{workspace}/.deepseek-tools/sdk'})
        shutil.rmtree(self.copy.path / '.deepseek-tools' / 'sdk')
        with self.assertRaises(toolchains.ToolchainError):
            toolchains.environment_for(self.copy, {'PATH': '/usr/bin'})
        with self.assertRaises(toolchains.ToolchainError):
            toolchains.read_only_roots(self.copy)

    def test_tampered_metadata_fails_closed(self):
        root = self.software()
        toolchains.prepare(self.copy, tools={'sdk': root},
                           environment={'JAVA_HOME': '{workspace}/.deepseek-tools/sdk'})
        record = self.reloaded(self.copy)
        record.metadata['toolchain']['version'] = 2
        record.save()
        loaded = self.reloaded(self.copy)
        with self.assertRaises(toolchains.ToolchainError):
            toolchains.environment_for(loaded, {'PATH': '/usr/bin'})
        with self.assertRaises(toolchains.ToolchainError):
            toolchains.probes_for(loaded)
        with self.assertRaises(toolchains.ToolchainError):
            toolchains.read_only_roots(loaded)

    def test_tool_directory_replaced_by_escaping_symlink_fails_closed(self):
        root = self.software()
        toolchains.prepare(self.copy, tools={'sdk': root})
        outside = self.root / 'outside-dir'
        outside.mkdir()
        shutil.rmtree(self.copy.path / '.deepseek-tools' / 'sdk')
        os.symlink(str(outside), self.copy.path / '.deepseek-tools' / 'sdk')
        with self.assertRaises(toolchains.ToolchainError):
            toolchains.read_only_roots(self.copy)


class RecipeTests(ToolchainFixture):
    def test_save_project_recipe_replays_into_a_fresh_copy(self):
        root = self.software(inside_link=True)
        source_file = self.root / 'inputs' / 'local.properties'
        source_file.parent.mkdir()
        source_file.write_text('sdk.dir=prepared\n')
        probes = ['{workspace}/.deepseek-tools/sdk/bin/tool --version']
        toolchains.prepare(self.copy, tools={'sdk': root},
                           copies=[(source_file, 'build/prepared.properties')],
                           environment={'JAVA_HOME': '{workspace}/.deepseek-tools/sdk'},
                           probes=probes, save_project=True)
        recipe = self.recipe_path(self.copy)
        self.assertTrue(recipe.is_file())
        self.assertEqual(stat.S_IMODE(recipe.stat().st_mode), 0o600)
        fresh = self.fresh_copy()
        with fresh.lock():
            result = toolchains.apply_saved_recipe(fresh)
        self.assertTrue(result['applied'])
        self.assertEqual(result['reason'], 'replayed')
        self.assertEqual((fresh.path / '.deepseek-tools/sdk/lib/data.txt').read_text(), 'data\n')
        self.assertEqual((fresh.path / 'build/prepared.properties').read_text(), 'sdk.dir=prepared\n')
        metadata = self.reloaded(fresh).metadata['toolchain']
        self.assertEqual(metadata['version'], TOOLCHAIN_VERSION)
        self.assertEqual(self.reloaded(fresh).metadata['status'], 'ready')
        self.assertEqual(toolchains.probes_for(fresh),
                         [str(fresh.path / '.deepseek-tools/sdk/bin/tool') + ' --version'])
        environment = toolchains.environment_for(fresh, {'PATH': '/usr/bin'})
        self.assertEqual(environment['JAVA_HOME'], str(fresh.path / '.deepseek-tools/sdk'))
        self.assertEqual(toolchains.read_only_roots(fresh),
                         [fresh.path / '.deepseek-tools/sdk'])
        ignored = self.ignored_by_copy(fresh, 'build/prepared.properties')
        self.assertIn('.git/info/exclude', ignored)
        self.assertIn('/build/prepared.properties', ignored)

    def test_recipe_replay_registers_copied_destinations(self):
        source_file = self.root / 'inputs' / 'local.properties'
        source_file.parent.mkdir(parents=True)
        source_file.write_text('sdk.dir=/opt/sdk\n')
        toolchains.prepare(self.copy, copies=[(source_file, 'build/local.properties')],
                           save_project=True)
        fresh = self.fresh_copy()
        with fresh.lock():
            result = toolchains.apply_saved_recipe(fresh)
        self.assertTrue(result['applied'])
        self.assertEqual(self.reloaded(fresh).metadata['prepared_paths'],
                         ['build/local.properties'])

    def test_recipe_replay_never_executes_host_commands(self):
        marker = self.root / 'replay-marker'
        probes = ['touch ' + str(marker)]
        toolchains.prepare(self.copy, probes=probes, save_project=True)
        self.assertFalse(marker.exists())
        fresh = self.fresh_copy()
        with fresh.lock():
            result = toolchains.apply_saved_recipe(fresh)
        self.assertTrue(result['applied'])
        self.assertFalse(marker.exists())
        self.assertEqual(toolchains.probes_for(fresh), probes)

    def test_missing_and_already_prepared_recipes_are_noops(self):
        fresh = self.fresh_copy()
        with fresh.lock():
            result = toolchains.apply_saved_recipe(fresh)
        self.assertEqual(result, {'applied': False, 'reason': 'no-recipe', 'toolchain': None})
        self.assertNotIn('toolchain', self.reloaded(fresh).metadata)

        toolchains.prepare(self.copy, probes=['echo prepared'])
        self.assertFalse(self.recipe_path(self.copy).exists())
        fresh = self.fresh_copy()
        with fresh.lock():
            self.assertEqual(toolchains.apply_saved_recipe(fresh),
                             {'applied': False, 'reason': 'no-recipe', 'toolchain': None})

        root = self.software()
        toolchains.prepare(self.copy, tools={'sdk': root}, save_project=True)
        with self.copy.lock():
            result = toolchains.apply_saved_recipe(self.copy)
        self.assertFalse(result['applied'])
        self.assertEqual(result['reason'], 'already-prepared')
        self.assertEqual(result['toolchain']['version'], TOOLCHAIN_VERSION)

    def test_apply_requires_the_callers_copy_lock(self):
        with self.assertRaises(toolchains.ToolchainError):
            toolchains.apply_saved_recipe(self.copy)

    def test_malformed_or_unsafe_recipes_fail_closed(self):
        payloads = [
            '{',
            self.recipe_payload(self.copy, version=2),
            self.recipe_payload(self.copy, kind='other'),
            self.recipe_payload(self.copy, source_project='/somewhere/else'),
            self.recipe_payload(self.copy, environment={'HOME': '/root'}),
            self.recipe_payload(self.copy, environment={'JAVA_HOME': '/etc'}),
            self.recipe_payload(self.copy, copies=[{'source': '/tmp/x', 'destination': '../escape'}]),
            self.recipe_payload(self.copy, copies=[{'source': '/tmp/x', 'destination': '.git/config'}]),
            self.recipe_payload(self.copy, probes=[1]),
            self.recipe_payload(self.copy, commands=['touch /tmp/unsafe']),
        ]
        for index, payload in enumerate(payloads):
            fresh = self.fresh_copy()
            self.write_recipe(fresh, payload)
            with self.assertRaises(toolchains.ToolchainError):
                with fresh.lock():
                    toolchains.apply_saved_recipe(fresh)
            self.assertNotIn('toolchain', self.reloaded(fresh).metadata)
            self.assertFalse((fresh.path / '.deepseek-tools').exists())
        fresh = self.fresh_copy()
        self.write_recipe(fresh, self.recipe_payload(fresh), mode=0o644)
        with self.assertRaises(toolchains.ToolchainError):
            with fresh.lock():
                toolchains.apply_saved_recipe(fresh)

    def test_recipe_replay_rejects_unsafe_host_source(self):
        fresh = self.fresh_copy()
        self.write_recipe(fresh, self.recipe_payload(fresh, tools={
            'sdk': {'source': '/usr', 'files': 0, 'bytes': 0, 'links_skipped': 0}}))
        with self.assertRaises(toolchains.ToolchainError):
            with fresh.lock():
                toolchains.apply_saved_recipe(fresh)
        self.assertFalse((fresh.path / '.deepseek-tools').exists())

    def test_interrupted_recipe_publication_keeps_the_previous_recipe(self):
        toolchains.prepare(self.copy, probes=['echo first'], save_project=True)
        recipe = self.recipe_path(self.copy)
        before = recipe.read_text()
        real_replace = toolchains._replace

        def fail_recipe_replace(source, destination):
            if str(destination).endswith('.json'):
                raise OSError('injected recipe publication failure')
            return real_replace(source, destination)

        with mock.patch.object(toolchains, '_replace', side_effect=fail_recipe_replace):
            with self.assertRaises(toolchains.ToolchainError):
                toolchains.prepare(self.copy, probes=['echo second'], save_project=True, recover=True)
        self.assertEqual(recipe.read_text(), before)
        self.assertEqual([path.name for path in recipe.parent.iterdir()
                          if '.tmp-' in path.name], [])
        self.assertEqual(self.reloaded(self.copy).metadata['status'], 'failed')
        fresh = self.fresh_copy()
        with fresh.lock():
            result = toolchains.apply_saved_recipe(fresh)
        self.assertTrue(result['applied'])
        self.assertEqual(toolchains.probes_for(fresh), ['echo first'])

    def test_concurrent_readers_never_see_a_partial_recipe(self):
        writer = self.fresh_copy()
        complete = []
        initial = ['echo 0']
        complete.append(json.dumps(initial))
        toolchains.prepare(writer, probes=initial, save_project=True)
        errors = []
        stop = threading.Event()

        def reader():
            recipe = self.recipe_path(writer)
            while not stop.is_set():
                try:
                    payload = json.loads(recipe.read_text())
                except FileNotFoundError:
                    time.sleep(0.001)
                    continue
                except (OSError, ValueError) as error:
                    errors.append(repr(error))
                    return
                if payload.get('version') != TOOLCHAIN_VERSION:
                    errors.append('partial recipe: ' + repr(payload))
                    return
                if json.dumps(payload.get('probes')) not in complete:
                    errors.append('torn probes: ' + repr(payload.get('probes')))

        thread = threading.Thread(target=reader)
        thread.start()
        try:
            for index in range(12):
                probes = ['echo ' + str(step) for step in range(index * 2, index * 2 + 2)]
                complete.append(json.dumps(probes))
                toolchains.prepare(writer, probes=probes, save_project=True)
        finally:
            stop.set()
            thread.join(timeout=10)
        self.assertEqual(errors, [])

    def test_unknown_or_unsafe_existing_metadata_fails_apply(self):
        record = self.reloaded(self.copy)
        record.metadata['toolchain'] = {'version': 99}
        record.save()
        with self.assertRaises(toolchains.ToolchainError):
            with self.copy.lock():
                toolchains.apply_saved_recipe(self.copy)


if __name__ == '__main__':
    unittest.main()
