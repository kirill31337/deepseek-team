"""Setup readiness outside Git and bounded owned-block refresh detection.

Setup is user-level readiness and must work even when an unrelated ancestor
carries a stray or invalid ``.git`` marker. Refresh must touch only owned blocks
that already exist in the nearest actual repository, must never scan children or
attach a fresh project, and must not let an unrelated marker fail setup. Real
corruption in an owned control must still be surfaced.
"""
import contextlib
import io
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from codex_deepseek_team import doctor, onboarding, project, sandbox, settings


FOREIGN_AGENTS = b'# User rules\n\nKeep this text byte-for-byte.\n'
FOREIGN_CLAUDE = b'# User claude rules\n\nKeep this text byte-for-byte too.\n'


class SetupContextCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='dst-setup-ctx-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        # An unrelated common parent that carries a foreign, invalid .git marker.
        self.common = self.root / 'common'
        self.common.mkdir()
        self.stray = self.common / '.git'
        self.stray.mkdir()
        (self.stray / 'not-a-repository').write_text('foreign marker\n')
        self.work = self.common / 'work'
        self.work.mkdir()
        (self.root / 'home').mkdir()
        environment = mock.patch.dict(os.environ, {
            'HOME': str(self.root / 'home'),
            'CODEX_HOME': str(self.root / 'codex'),
            'CLAUDE_CONFIG_DIR': str(self.root / 'claude'),
            'XDG_CONFIG_HOME': str(self.root / 'config'),
            'DEEPSEEK_TEAM_STATE_DIR': str(self.root / 'state'),
            'DEEPSEEK_API_KEY': '',
        }, clear=False)
        environment.start()
        self.addCleanup(environment.stop)
        previous = os.getcwd()
        self.addCleanup(os.chdir, previous)
        os.chdir(self.work)

    def target(self, runtime, where=None):
        base = self.work if where is None else where
        return base / ('AGENTS.md' if runtime == 'codex' else 'CLAUDE.md')

    def make_repository(self, path):
        subprocess.run(['git', 'init', '-q', str(path)], check=True)
        return path

    def set_global(self, **options):
        return settings.set_values(settings.global_file(), **options)

    def run_setup(self, cwd=None):
        backend = sandbox.SandboxBackend(('/test/bwrap',), '/test/bwrap', 'direct')
        output = io.StringIO()
        with mock.patch('shutil.which', side_effect=lambda name: '/test/bin/' + name), \
             mock.patch.object(sandbox, 'probe_backend', return_value=backend), \
             mock.patch.object(onboarding.doctor, 'main', return_value=0), \
             mock.patch.object(onboarding.config, 'codex_hooks_status', return_value=True), \
             contextlib.chdir(cwd or self.work), contextlib.redirect_stdout(output):
            code = onboarding.run(['codex'], no_key=True)
        return code, output.getvalue()


class StrayAncestorSetupTests(SetupContextCase):
    def test_stray_invalid_ancestor_marker_does_not_fail_or_attach(self):
        self.target('codex').write_bytes(FOREIGN_AGENTS)
        self.target('claude').write_bytes(FOREIGN_CLAUDE)

        code, output = self.run_setup()

        self.assertEqual(code, 0, output)
        self.assertNotIn('not inside a Git repository', output)
        self.assertEqual(self.target('codex').read_bytes(), FOREIGN_AGENTS)
        self.assertEqual(self.target('claude').read_bytes(), FOREIGN_CLAUDE)

    def test_unrelated_marker_creates_no_instruction_files_anywhere(self):
        code, output = self.run_setup()

        self.assertEqual(code, 0, output)
        self.assertFalse((self.work / 'AGENTS.md').exists())
        self.assertFalse((self.work / 'CLAUDE.md').exists())
        self.assertFalse((self.common / 'AGENTS.md').exists())
        self.assertFalse((self.common / 'CLAUDE.md').exists())

    def test_unrelated_marker_does_not_resolve_policy_or_refresh(self):
        with mock.patch.object(onboarding.settings, 'resolve',
                               side_effect=AssertionError('policy resolution')), \
             mock.patch.object(onboarding.project, 'refresh_attached',
                               side_effect=AssertionError('owned refresh')):
            self.assertTrue(onboarding._refresh_current_repository())

    def test_refresh_never_touches_ancestor_or_outside_canaries(self):
        (self.common / 'AGENTS.md').write_bytes(FOREIGN_AGENTS)
        canary = self.root / 'canary.txt'
        canary.write_bytes(b'outside canary\n')
        stray_snapshot = (self.stray / 'not-a-repository').read_bytes()

        with mock.patch.object(onboarding.settings, 'resolve',
                               side_effect=AssertionError('policy resolution')), \
             mock.patch.object(onboarding.project, 'refresh_attached',
                               side_effect=AssertionError('owned refresh')):
            self.assertTrue(onboarding._refresh_current_repository())

        self.assertEqual((self.common / 'AGENTS.md').read_bytes(), FOREIGN_AGENTS)
        self.assertEqual(canary.read_bytes(), b'outside canary\n')
        self.assertEqual((self.stray / 'not-a-repository').read_bytes(), stray_snapshot)
        self.assertFalse((self.root / 'codex').exists())
        self.assertFalse((self.root / 'claude').exists())

    def test_child_repository_is_not_scanned_or_refreshed(self):
        child = self.make_repository(self.work / 'child')
        self.set_global(effort='low')
        project.attach(child, coordinator='both')
        self.set_global(effort='max')

        code, output = self.run_setup()

        self.assertEqual(code, 0, output)
        for runtime in ('codex', 'claude'):
            body = self.target(runtime, child).read_bytes()
            self.assertIn(b'effort=low', body)
            self.assertNotIn(b'effort=max', body)


class CurrentRepositoryRefreshTests(SetupContextCase):
    def test_owned_block_in_nested_repository_still_refreshes(self):
        self.make_repository(self.work)
        self.set_global(effort='low')
        project.attach(self.work, coordinator='both')
        self.set_global(effort='max')

        code, output = self.run_setup()

        self.assertEqual(code, 0, output)
        for runtime in ('codex', 'claude'):
            self.assertIn(b'effort=max', self.target(runtime).read_bytes())

    def test_malformed_owned_control_still_fails_setup(self):
        self.make_repository(self.work)
        project.attach(self.work, coordinator='codex')
        target = self.target('codex')
        target.write_bytes(target.read_bytes().replace(
            b'original:created', b'original:modified'))
        before = target.read_bytes()

        code, output = self.run_setup()

        self.assertNotEqual(code, 0, output)
        self.assertIn('Setup incomplete', output)
        self.assertNotIn('Local setup checks passed', output)
        self.assertEqual(target.read_bytes(), before)


class OwnedBlockDetectionTests(SetupContextCase):
    def test_missing_and_unmarked_targets_are_not_owned(self):
        repo = self.make_repository(self.work)
        self.assertFalse(project.owns_block(repo, 'codex'))
        self.assertFalse(project.owns_block(repo, 'claude'))

        self.target('codex').write_bytes(FOREIGN_AGENTS)
        self.assertFalse(project.owns_block(repo, 'codex'))

    def test_owned_block_is_detected_per_runtime(self):
        repo = self.make_repository(self.work)
        project.attach(repo, coordinator='codex')
        self.assertTrue(project.owns_block(repo, 'codex'))
        self.assertFalse(project.owns_block(repo, 'claude'))

    def test_malformed_owned_block_is_surfaced_not_swallowed(self):
        repo = self.make_repository(self.work)
        target = self.target('codex')
        target.write_bytes(project.START_MARKER + b'\n<!-- corrupted -->\n'
                           + project.END_MARKER + b'\n')
        with self.assertRaises(project.ProjectError):
            project.owns_block(repo, 'codex')

    def test_symlinked_target_is_surfaced_not_swallowed(self):
        repo = self.make_repository(self.work)
        outside = self.root / 'outside.md'
        outside.write_bytes(FOREIGN_AGENTS)
        os.symlink(outside, self.target('codex'))
        with self.assertRaises(project.ProjectError):
            project.owns_block(repo, 'codex')
        self.assertEqual(outside.read_bytes(), FOREIGN_AGENTS)

    def test_unknown_runtime_is_rejected(self):
        repo = self.make_repository(self.work)
        with self.assertRaises(project.ProjectError):
            project.owns_block(repo, 'other')


if __name__ == '__main__':
    unittest.main()
