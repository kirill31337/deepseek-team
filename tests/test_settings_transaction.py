"""Transactional project refresh and explicit `--path` contracts.

Bug 1: `config set --project` published `.deepseek-team.toml` before refreshing the
managed AGENTS.md/CLAUDE.md blocks, so a symlink or a malformed block left the new
(wider) access in place even though the command reported failure.
Bug 2: an explicit `config show --path` outside any Git working copy silently resolved
the current directory's project settings instead of reporting the invalid path.
"""
import contextlib
import errno
import sys
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest
from unittest import mock

from codex_deepseek_team import delegation_cli, project, settings


START = project.START_MARKER
END = project.END_MARKER

MALFORMED_BLOCKS = {
    'duplicate-markers': b'u\n' + START + b'\nb\n' + END + b'\n' + START + b'\nb\n' + END + b'\n',
    'missing-ownership-metadata': b'u\n' + START + b'\nbody\n' + END + b'\n',
    'unbalanced-markers': b'u\n' + START + b'\nbody\n',
    'missing-start-marker': b'u\nbody\n' + END + b'\n',
    'out-of-order-markers': b'u\n' + END + b'\nbody\n' + START + b'\n',
}

class _FailingReadStream:
    """A real settings stream whose read fails like an injected EIO."""

    def __init__(self, stream, error):
        self._stream = stream
        self._error = error

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return self._stream.__exit__(*exc_info)

    def read(self, size=-1):
        raise self._error

    def close(self):
        self._stream.close()

    def fileno(self):
        return self._stream.fileno()


class TransactionCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='dst-settings-')
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.home = self.base / 'home'
        self.home.mkdir()
        self.repo = self.base / 'repo'
        self.repo.mkdir()
        subprocess.run(['git', 'init', '-q', str(self.repo)], check=True)
        self.outside = self.base / 'outside'
        self.outside.mkdir()
        self.env = mock.patch.dict(os.environ, {
            'HOME': str(self.home), 'XDG_CONFIG_HOME': str(self.base / 'config')})
        self.env.start()
        self.addCleanup(self.env.stop)

    def cli(self, *args, cwd=None):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.chdir(cwd or self.outside):
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = delegation_cli.main(list(args))
        return code, out.getvalue(), err.getvalue()

    def project_file(self, repo=None):
        return (repo or self.repo) / settings.PROJECT_FILE

    def file_bytes(self, name, repo=None):
        return ((repo or self.repo) / name).read_bytes()

    def seed_project(self, **values):
        return settings.set_values(self.project_file(), **values)

    def attach(self, runtime='both'):
        return project.attach(self.repo, runtime)


class ProjectSetTransactionTests(TransactionCase):
    def test_successful_refresh_covers_both_runtimes_and_preserves_foreign_text(self):
        agents = b'# House rules\r\nkeep me\r\n'
        claude = b'# Claude rules\nkeep me too\n'
        self.repo.joinpath('AGENTS.md').write_bytes(agents)
        self.repo.joinpath('CLAUDE.md').write_bytes(claude)
        self.assertTrue(self.attach())
        self.assertIn(b'### Effective delegation profile: auto (adaptive) / read-only',
                      self.file_bytes('AGENTS.md'))

        code, out, err = self.cli('config', 'set', '--project', '--path', str(self.repo),
                                  '--delegation-level', '50', '--access', 'full-access')
        self.assertEqual(code, 0, err)
        policy = settings.resolve(self.repo)
        self.assertEqual((policy.delegation_level, policy.access), (50, 'full-access'))
        for name, foreign in (('AGENTS.md', agents), ('CLAUDE.md', claude)):
            with self.subTest(name=name):
                data = self.file_bytes(name)
                self.assertTrue(data.startswith(foreign), 'foreign text was rewritten')
                self.assertIn(b'<!-- codex-deepseek-team:original:existing-content -->', data)
                self.assertIn(b'### Effective delegation profile: 50% / full-access; effort=auto',
                              data)
                self.assertTrue(data.endswith(END + b'\n'))

    def make_repo(self, name):
        repo = self.base / name
        repo.mkdir()
        subprocess.run(['git', 'init', '-q', str(repo)], check=True)
        return repo

    def test_malformed_block_in_either_runtime_changes_nothing(self):
        for broken in ('AGENTS.md', 'CLAUDE.md'):
            for label, block in MALFORMED_BLOCKS.items():
                with self.subTest(broken=broken, kind=label):
                    repo = self.make_repo(f'repo-{broken}-{label}')
                    repo.joinpath('AGENTS.md').write_bytes(b'# user rules\n')
                    repo.joinpath('CLAUDE.md').write_bytes(b'# user rules\n')
                    project.attach(repo, 'both')
                    healthy = 'CLAUDE.md' if broken == 'AGENTS.md' else 'AGENTS.md'
                    healthy_before = self.file_bytes(healthy, repo)
                    repo.joinpath(broken).write_bytes(block)

                    code, out, err = self.cli(
                        'config', 'set', '--project', '--path', str(repo),
                        '--delegation-level', '75', '--access', 'full-access')
                    self.assertEqual(code, 78, out)
                    self.assertTrue(err.strip())
                    self.assertEqual(self.file_bytes(broken, repo), block)
                    self.assertEqual(self.file_bytes(healthy, repo), healthy_before)
                    self.assertFalse(self.project_file(repo).exists())
                    policy = settings.resolve(repo)
                    self.assertEqual(policy.access, 'auto')
                    self.assertEqual(policy.effective_access, 'read-only')

    def test_symlinked_instruction_file_in_either_runtime_blocks_every_change(self):
        for linked in ('AGENTS.md', 'CLAUDE.md'):
            with self.subTest(linked=linked):
                repo = self.make_repo(f'repo-link-{linked}')
                repo.joinpath('AGENTS.md').write_bytes(b'# user rules\n')
                repo.joinpath('CLAUDE.md').write_bytes(b'# user rules\n')
                project.attach(repo, 'both')
                healthy = 'CLAUDE.md' if linked == 'AGENTS.md' else 'AGENTS.md'
                healthy_before = self.file_bytes(healthy, repo)
                target = repo / linked
                outside = self.base / f'outside-{linked}.md'
                outside.write_bytes(target.read_bytes())
                target.unlink()
                target.symlink_to(outside)
                outside_before = outside.read_bytes()

                code, out, err = self.cli(
                    'config', 'set', '--project', '--path', str(repo), '--access', 'full-access')
                self.assertEqual(code, 78, out)
                self.assertTrue(err.strip())
                self.assertTrue(target.is_symlink())
                self.assertEqual(outside.read_bytes(), outside_before)
                self.assertEqual(self.file_bytes(healthy, repo), healthy_before)
                self.assertFalse(self.project_file(repo).exists())

    def test_failed_second_write_rolls_back_first_target_and_keeps_old_access(self):
        self.attach()
        self.seed_project(access='read-only', delegation_level=75)
        self.repo.joinpath('AGENTS.md').chmod(0o640)
        agents_before = self.file_bytes('AGENTS.md')
        claude_before = self.file_bytes('CLAUDE.md')
        settings_before = self.project_file().read_bytes()
        real = project._write_atomic
        seen = []

        def flaky(target, original, mode, content):
            seen.append(target.name)
            if len(seen) == 2:
                raise project.ProjectError('injected write failure')
            real(target, original, mode, content)

        with mock.patch.object(project, '_write_atomic', flaky):
            code, out, err = self.cli(
                'config', 'set', '--project', '--path', str(self.repo), '--access', 'full-access')
        self.assertEqual(seen[:2], ['AGENTS.md', 'CLAUDE.md'])
        self.assertEqual(code, 78, out)
        self.assertIn('injected write failure', err)
        self.assertNotIn('could not roll back', err)
        self.assertEqual(self.file_bytes('AGENTS.md'), agents_before)
        self.assertEqual(stat.S_IMODE((self.repo / 'AGENTS.md').stat().st_mode), 0o640)
        self.assertEqual(self.file_bytes('CLAUDE.md'), claude_before)
        self.assertEqual(self.project_file().read_bytes(), settings_before)
        policy = settings.resolve(self.repo)
        self.assertEqual(policy.access, 'read-only')
        self.assertEqual(policy.effective_access, 'read-only')

    def test_concurrent_external_edit_is_preserved_and_named(self):
        self.attach()
        self.seed_project(access='read-only')
        settings_before = self.project_file().read_bytes()
        real = project._write_atomic
        agents = self.repo / 'AGENTS.md'

        def racing(target, original, mode, content):
            if target.name == 'CLAUDE.md':
                # The user edits the already-refreshed first target before failure.
                agents.write_bytes(agents.read_bytes() + b'# concurrent user edit\n')
                raise project.ProjectError('injected failure on the second target')
            real(target, original, mode, content)

        with mock.patch.object(project, '_write_atomic', racing):
            code, out, err = self.cli(
                'config', 'set', '--project', '--path', str(self.repo), '--access', 'full-access')
        self.assertEqual(code, 78, out)
        self.assertIn('could not roll back: AGENTS.md', err)
        data = agents.read_bytes()
        self.assertTrue(data.endswith(b'# concurrent user edit\n'))
        self.assertIn(project.START_MARKER, data)
        self.assertEqual(self.project_file().read_bytes(), settings_before)
        self.assertEqual(settings.resolve(self.repo).effective_access, 'read-only')

    def test_concurrent_settings_write_aborts_and_rolls_back_instructions(self):
        self.attach()
        self.seed_project(access='read-only')
        agents_before = self.file_bytes('AGENTS.md')
        external = b'delegation_level = 25\naccess = "read-only"\n'
        real = settings.publish_values

        def racing(path, previous, values):
            path.write_bytes(external)
            real(path, previous, values)

        with mock.patch.object(settings, 'publish_values', racing):
            code, out, err = self.cli('config', 'set', '--project', '--path', str(self.repo),
                                      '--access', 'full-access')
        self.assertEqual(code, 78, out)
        self.assertIn('changed concurrently', err)
        self.assertEqual(self.project_file().read_bytes(), external)
        self.assertEqual(self.file_bytes('AGENTS.md'), agents_before)

    def test_concurrent_chmod_is_preserved_during_rollback(self):
        self.attach()
        self.seed_project(access='read-only')
        settings_before = self.project_file().read_bytes()
        real = project._write_atomic
        agents = self.repo / 'AGENTS.md'
        agents.chmod(0o644)

        def racing(target, original, mode, content):
            if target.name == 'CLAUDE.md':
                agents.chmod(0o600)
                raise project.ProjectError('injected failure on the second target')
            real(target, original, mode, content)

        with mock.patch.object(project, '_write_atomic', racing):
            code, out, err = self.cli('config', 'set', '--project', '--path', str(self.repo),
                                      '--access', 'full-access')
        self.assertEqual(code, 78, out)
        self.assertIn('could not roll back: AGENTS.md', err)
        self.assertEqual(stat.S_IMODE(agents.stat().st_mode), 0o600)
        self.assertEqual(self.project_file().read_bytes(), settings_before)

    def test_post_publication_durability_failure_keeps_matching_instructions(self):
        for operation in ('open', 'fsync', 'close'):
            with self.subTest(operation=operation):
                repo = self.make_repo('durability-' + operation)
                settings.set_values(repo / settings.PROJECT_FILE, access='read-only')
                project.attach(repo, 'both')
                published, failed, directory_fd = False, False, None
                real_replace, real_open = os.replace, os.open
                real_fsync, real_close = os.fsync, os.close

                def replace(source, target, *args, **kwargs):
                    nonlocal published
                    result = real_replace(source, target, *args, **kwargs)
                    if Path(target) == repo / settings.PROJECT_FILE:
                        published = True
                    return result

                def open_directory(path, flags, *args, **kwargs):
                    nonlocal failed, directory_fd
                    if published and not failed and Path(path) == repo:
                        if operation == 'open':
                            failed = True
                            raise OSError('injected directory open failure')
                        directory_fd = real_open(path, flags, *args, **kwargs)
                        return directory_fd
                    return real_open(path, flags, *args, **kwargs)

                def fsync(fd):
                    nonlocal failed
                    if operation == 'fsync' and published and not failed and fd == directory_fd:
                        failed = True
                        raise OSError('injected directory fsync failure')
                    return real_fsync(fd)

                def close(fd):
                    nonlocal failed
                    result = real_close(fd)
                    if operation == 'close' and published and not failed and fd == directory_fd:
                        failed = True
                        raise OSError('injected directory close failure')
                    return result

                with mock.patch.object(os, 'replace', replace), mock.patch.object(os, 'open', open_directory), \
                        mock.patch.object(os, 'fsync', fsync), mock.patch.object(os, 'close', close):
                    code, out, err = self.cli('config', 'set', '--project', '--path', str(repo),
                                              '--access', 'full-access')
                self.assertTrue(failed, 'fixture must fail after publishing settings')
                self.assertEqual(code, 78, out)
                self.assertIn('published', err)
                self.assertEqual(settings.resolve(repo).access, 'full-access')
                for name in ('AGENTS.md', 'CLAUDE.md'):
                    self.assertIn(b'auto (adaptive) / full-access', self.file_bytes(name, repo))

    def test_refresh_leaves_a_second_file_without_a_managed_block_alone(self):
        self.repo.joinpath('AGENTS.md').write_bytes(b'# House rules\n')
        self.attach('codex')
        claude = b'# Untouched claude notes\n'
        self.repo.joinpath('CLAUDE.md').write_bytes(claude)

        code, out, err = self.cli('config', 'set', '--project', '--path', str(self.repo),
                                  '--access', 'full-access')
        self.assertEqual(code, 0, err)
        self.assertEqual(self.file_bytes('CLAUDE.md'), claude)
        self.assertIn(b'### Effective delegation profile: auto (adaptive) / full-access',
                      self.file_bytes('AGENTS.md'))

    def test_transaction_preserves_other_project_and_global_fields(self):
        settings.set_values(settings.global_file(), max_workers=12, effort='low')
        global_before = settings.global_file().read_bytes()
        self.attach()
        self.seed_project(delegation_level=50, effort='high', max_workers=4)

        code, out, err = self.cli('config', 'set', '--project', '--path', str(self.repo),
                                  '--access', 'full-access')
        self.assertEqual(code, 0, err)
        policy = settings.resolve(self.repo)
        self.assertEqual(policy.delegation_level, 50)
        self.assertEqual(policy.effort, 'high')
        self.assertEqual(policy.max_workers, 4)
        self.assertEqual(policy.access, 'full-access')
        self.assertEqual(settings.global_file().read_bytes(), global_before)
        saved = self.project_file().read_text()
        self.assertIn('effort = "high"', saved)
        self.assertIn('max_workers = 4', saved)
        self.assertIn('access = "full-access"', saved)

    def test_transaction_preserves_legacy_effort_normalization(self):
        self.attach()
        code, out, err = self.cli('config', 'set', '--project', '--path', str(self.repo),
                                  '--effort', 'medium')
        self.assertEqual(code, 0, err)
        self.assertIn('effort = "high"', self.project_file().read_text())
        for name in ('AGENTS.md', 'CLAUDE.md'):
            self.assertIn(b'effort=high', self.file_bytes(name))
        values = {'effort': 'medium'}
        candidate = settings.candidate_policy(self.repo, self.project_file(), values)
        self.assertEqual(candidate.effort, 'high')
        self.assertEqual(values, {'effort': 'medium'})

    def test_project_set_without_owned_blocks_creates_no_instruction_files(self):
        code, out, err = self.cli('config', 'set', '--project', '--path', str(self.repo),
                                  '--access', 'full-access')
        self.assertEqual(code, 0, err)
        self.assertEqual(settings.resolve(self.repo).access, 'full-access')
        self.assertFalse((self.repo / 'AGENTS.md').exists())
        self.assertFalse((self.repo / 'CLAUDE.md').exists())

    def test_project_set_without_any_option_reports_usage_error(self):
        code, out, err = self.cli('config', 'set', '--project', '--path', str(self.repo))
        self.assertEqual(code, 78, out)
        self.assertIn('Specify --delegation-level', err)
        self.assertFalse(self.project_file().exists())


class ExplicitConfigPathTests(TransactionCase):
    def test_explicit_path_outside_git_is_an_error(self):
        for path in (self.outside, self.base / 'missing', self.base / 'repo' / 'AGENTS.md'):
            with self.subTest(path=path.name):
                code, out, err = self.cli('config', 'show', '--path', str(path), '--json')
                self.assertEqual(code, 78, out)
                self.assertEqual(out, '')
                self.assertTrue(err.strip())

    def test_omitted_path_outside_git_still_shows_global_and_default(self):
        code, out, err = self.cli('config', 'show', '--json')
        self.assertEqual(code, 0, err)
        shown = json.loads(out)
        self.assertEqual(shown['delegation_level'], 'auto')
        self.assertEqual(shown['sources']['delegation_level'], 'default')
        settings.set_values(settings.global_file(), max_workers=12)
        code, out, err = self.cli('config', 'show', '--json')
        self.assertEqual(code, 0, err)
        shown = json.loads(out)
        self.assertEqual(shown['max_workers'], 12)
        self.assertTrue(shown['sources']['max_workers'].startswith('global:'))

    def test_explicit_path_resolves_repository_root_and_subdirectory(self):
        self.seed_project(delegation_level=50)

        code, out, err = self.cli('config', 'show', '--path', str(self.repo), '--json')
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)['delegation_level'], 50)

        nested = self.repo / 'nested'
        nested.mkdir()
        code, out, err = self.cli('config', 'show', '--path', str(nested), '--json')
        self.assertEqual(code, 0, err)
        shown = json.loads(out)
        self.assertEqual(shown['delegation_level'], 50)
        self.assertTrue(shown['sources']['delegation_level'].startswith('project:'))

    def test_explicit_path_selects_another_repository(self):
        other = self.base / 'other'
        other.mkdir()
        subprocess.run(['git', 'init', '-q', str(other)], check=True)
        self.seed_project(delegation_level=75)
        settings.set_values(other / settings.PROJECT_FILE, delegation_level=25)

        code, out, err = self.cli('config', 'show', '--path', str(other), '--json')
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)['delegation_level'], 25)
        code, out, err = self.cli('config', 'show', '--path', str(self.repo), '--json')
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)['delegation_level'], 75)


class ControlledSettingsReadTests(TransactionCase):
    """Unsafe or malformed layers fail closed without a false fallback."""

    def test_missing_file_keeps_the_default_fallback(self):
        self.assertFalse(self.project_file().exists())
        self.assertEqual(settings.read_values(self.project_file()), {})
        policy = settings.resolve(self.repo)
        self.assertEqual((policy.delegation_level, policy.access,
                          policy.effective_access), ('auto', 'auto', 'read-only'))
        self.assertEqual(policy.sources['access'], 'default')

    def test_auto_access_explanation_tracks_the_effective_profile(self):
        read_only = settings.Policy('auto', 'auto', {
            'access': 'default', 'delegation_level': 'default'})
        full_access = settings.Policy(75, 'auto', {
            'access': 'default', 'delegation_level': 'project:/repo/.deepseek-team.toml'})
        self.assertIn('yields read-only', settings.access_explanation(read_only))
        self.assertIn('yields full-access', settings.access_explanation(full_access))

    def test_invalid_toml_is_a_controlled_settings_error(self):
        self.project_file().write_text('access = "full-access"\n[broken\n')
        with self.assertRaises(settings.SettingsError) as caught:
            settings.resolve(self.repo)
        self.assertIn('Invalid delegation TOML', str(caught.exception))

    def test_symlink_is_refused_without_following_it(self):
        target = self.base / 'real-settings.toml'
        settings.set_values(target, access='full-access')
        self.project_file().symlink_to(target)
        with self.assertRaises(settings.SettingsError) as caught:
            settings.read_values(self.project_file())
        self.assertIn('symbolic link', str(caught.exception).lower())

    def test_hardlink_is_refused_without_using_its_content(self):
        target = self.base / 'hardlinked-settings.toml'
        target.write_text('access = "full-access"\n')
        os.link(target, self.project_file())
        with self.assertRaises(settings.SettingsError) as caught:
            settings.read_values(self.project_file())
        self.assertIn('hard link', str(caught.exception).lower())

    def test_directory_is_refused_as_a_non_file(self):
        self.project_file().mkdir()
        with self.assertRaises(settings.SettingsError) as caught:
            settings.read_values(self.project_file())
        self.assertIn('directory', str(caught.exception).lower())

    def test_fifo_is_refused_as_a_special_file_without_blocking(self):
        os.mkfifo(self.project_file())
        with self.assertRaises(settings.SettingsError) as caught:
            settings.read_values(self.project_file())
        self.assertIn('special file', str(caught.exception).lower())

    def test_permission_error_is_named_without_traceback_or_fallback(self):
        error = PermissionError(errno.EACCES, 'Permission denied',
                                str(self.project_file()))
        with mock.patch.object(settings, '_open_settings', side_effect=error):
            with self.assertRaises(settings.SettingsError) as caught:
                settings.read_values(self.project_file())
        self.assertIn('permission denied', str(caught.exception).lower())
        self.assertIn(str(self.project_file()), str(caught.exception))

    def test_repeated_read_failures_do_not_leak_descriptors(self):
        directory = self.project_file()
        directory.mkdir()
        before = len(os.listdir('/proc/self/fd'))
        for _ in range(25):
            with self.assertRaises(settings.SettingsError):
                settings.read_values(directory)
        after = len(os.listdir('/proc/self/fd'))
        self.assertEqual(after, before)

    def test_config_cli_reports_invalid_toml_without_traceback(self):
        self.project_file().write_text('access = "full-access"\n[broken\n')
        code, out, err = self.cli('config', 'show', '--path', str(self.repo),
                                  '--json')
        self.assertEqual(code, 78)
        self.assertEqual(out, '')
        self.assertIn('Invalid delegation TOML', err)
        self.assertNotIn('Traceback', err)
        self.assertNotIn('"access": "auto"', out)

    def test_config_cli_reports_permission_error_without_traceback(self):
        error = PermissionError(errno.EACCES, 'Permission denied',
                                str(self.project_file()))
        with mock.patch.object(settings, '_open_settings', side_effect=error):
            code, out, err = self.cli('config', 'show', '--path', str(self.repo),
                                      '--json')
        self.assertEqual(code, 78)
        self.assertEqual(out, '')
        self.assertIn('permission denied', err.lower())
        self.assertNotIn('Traceback', err)

    def test_worker_cli_stops_before_provider_on_invalid_settings(self):
        self.project_file().write_text('access = "full-access"\n[broken\n')
        environment = dict(os.environ)
        environment['PYTHONPATH'] = str(Path(__file__).resolve().parents[1] / 'src')
        completed = subprocess.run(
            [sys.executable, '-m', 'codex_deepseek_team', 'worker',
             '--os-sandbox', 'required', 'redacted task'],
            cwd=self.repo, env=environment, text=True, capture_output=True,
            timeout=30)
        self.assertEqual(completed.returncode, 78)
        self.assertIn('Invalid delegation TOML', completed.stderr)
        self.assertNotIn('Traceback', completed.stderr)


    def test_post_open_fstat_eio_is_controlled_and_closes_descriptor(self):
        path = self.project_file()
        path.write_text('access = "full-access"\n')
        original = path.read_bytes()
        before = len(os.listdir('/proc/self/fd'))
        for _ in range(5):
            with mock.patch.object(
                    settings.os, 'fstat',
                    side_effect=OSError(errno.EIO, 'Input/output error')):
                with self.assertRaises(settings.SettingsError) as caught:
                    settings.read_values(path)
            self.assertIn('Cannot safely read settings', str(caught.exception))
            self.assertIn(str(path), str(caught.exception))
        after = len(os.listdir('/proc/self/fd'))
        self.assertEqual(after, before)
        self.assertEqual(path.read_bytes(), original)

    def test_post_open_fdopen_eio_is_controlled_and_closes_descriptor(self):
        path = self.project_file()
        path.write_text('access = "full-access"\n')
        original = path.read_bytes()
        before = len(os.listdir('/proc/self/fd'))
        for _ in range(5):
            with mock.patch.object(
                    settings.os, 'fdopen',
                    side_effect=OSError(errno.EIO, 'Input/output error')):
                with self.assertRaises(settings.SettingsError) as caught:
                    settings.read_values(path)
            self.assertIn('Cannot safely read settings', str(caught.exception))
            self.assertIn(str(path), str(caught.exception))
        after = len(os.listdir('/proc/self/fd'))
        self.assertEqual(after, before)
        self.assertEqual(path.read_bytes(), original)

    def test_post_open_read_eio_is_controlled_and_closes_descriptor(self):
        path = self.project_file()
        path.write_text('access = "full-access"\n')
        original = path.read_bytes()
        real_fdopen = os.fdopen

        def failing_fdopen(fd, mode='r', *args, **kwargs):
            return _FailingReadStream(
                real_fdopen(fd, mode, *args, **kwargs),
                OSError(errno.EIO, 'Input/output error'))

        before = len(os.listdir('/proc/self/fd'))
        for _ in range(5):
            with mock.patch.object(
                    settings.os, 'fdopen', side_effect=failing_fdopen):
                with self.assertRaises(settings.SettingsError) as caught:
                    settings.read_values(path)
            self.assertIn('Cannot safely read settings', str(caught.exception))
            self.assertIn(str(path), str(caught.exception))
        after = len(os.listdir('/proc/self/fd'))
        self.assertEqual(after, before)
        self.assertEqual(path.read_bytes(), original)

    def test_config_cli_reports_post_open_read_eio_without_traceback(self):
        path = self.project_file()
        path.write_text('access = "full-access"\n')
        original = path.read_bytes()
        real_fdopen = os.fdopen

        def failing_fdopen(fd, mode='r', *args, **kwargs):
            return _FailingReadStream(
                real_fdopen(fd, mode, *args, **kwargs),
                OSError(errno.EIO, 'Input/output error'))

        with mock.patch.object(
                settings.os, 'fdopen', side_effect=failing_fdopen):
            code, out, err = self.cli('config', 'show', '--path', str(self.repo),
                                      '--json')
        self.assertEqual(code, 78)
        self.assertEqual(out, '')
        self.assertIn('Cannot safely read settings', err)
        self.assertNotIn('Traceback', err)
        self.assertNotIn('full-access', out)
        self.assertEqual(path.read_bytes(), original)


if __name__ == '__main__':
    unittest.main()
