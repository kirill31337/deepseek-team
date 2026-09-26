"""Canonical graded-rework bootstrap guidance for both coordinators.

The contract is exercised on temporary repositories through the real helper,
render, attach/refresh and CLI surfaces rather than copied implementation text.
"""
import contextlib
import inspect
import io
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from codex_deepseek_team import activation, cli, settings


# Semantic markers every enabled bootstrap surface must carry.
MARKERS = (
    'Grade each assignment',
    '--rework-json',
    '--quality-json',
    'met',
    'minor_gaps',
    'major_gaps',
    'unusable',
    'unassessable',
    'attribution',
    'worker',
    'shared',
    'coordinator',
    'environment',
    'unknown',
    '(1.0)',
    '(0.8)',
    '(0.3)',
    '(0.0)',
    'declared heuristics, not measured probabilities',
    'neutral for worker quality',
    'cosmetic',
    'legacy binary',
    'lowest scored worker grade is retained',
    'correction brief',
    'links the original assignment',
    'no `off` bypass',
    'access widening',
    'final verification',
)

FOREIGN = b'# Project rules\n\nKeep this text byte-for-byte.\n'


class BootstrapCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='dst-bootstrap-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        subprocess.run(['git', 'init', '-q', str(self.repo)], check=True)
        (self.repo / 'a.py').write_text('VALUE = 1\n')
        subprocess.run(['git', '-C', str(self.repo), 'add', '.'], check=True)
        subprocess.run(['git', '-C', str(self.repo), '-c', 'user.name=Test', '-c',
                        'user.email=test@example.test', 'commit', '-qm', 'base'], check=True)
        (self.root / 'home').mkdir()
        environment = mock.patch.dict(os.environ, {
            'HOME': str(self.root / 'home'),
            'XDG_CONFIG_HOME': str(self.root / 'config'),
            'DEEPSEEK_TEAM_STATE_DIR': str(self.root / 'state'),
        }, clear=False)
        environment.start()
        self.addCleanup(environment.stop)
        self.addCleanup(os.environ.pop, 'DEEPSEEK_TEAM_DISABLED', None)
        os.environ.pop('DEEPSEEK_TEAM_DISABLED', None)

    def assert_markers(self, text, label):
        self.assertIn('--quality-json', text)
        for marker in MARKERS:
            with self.subTest(surface=label, marker=marker):
                self.assertIn(marker, text)

    def run_cli(self, *args):
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = cli.main(list(args))
        return subprocess.CompletedProcess(args, code, output.getvalue(), errors.getvalue())

    def target(self, runtime):
        return self.repo / ('AGENTS.md' if runtime == 'codex' else 'CLAUDE.md')


class HelperContractTests(BootstrapCase):
    def test_signature_is_the_exact_public_contract(self):
        parameters = inspect.signature(settings.rework_guidance).parameters
        self.assertEqual(list(parameters), ['runtime'])
        self.assertEqual(parameters['runtime'].default, 'codex')
        self.assertIsInstance(settings.rework_guidance(), str)
        self.assertIsInstance(settings.rework_guidance('claude'), str)

    def test_helper_rejects_unknown_runtimes(self):
        with self.assertRaises(settings.SettingsError):
            settings.rework_guidance('other')

    def test_helper_carries_the_graded_rework_rubric(self):
        text = settings.rework_guidance('codex')
        self.assert_markers(text, 'helper')
        # The scoring rules are heuristic rubric weights, never a percent metric.
        self.assertNotIn('%', text)

    def test_enabled_instructions_embed_the_helper_once_for_both_runtimes(self):
        for runtime in ('codex', 'claude'):
            helper = settings.rework_guidance(runtime)
            for level, access in (('auto', 'auto'), (25, 'read-only'), (75, 'full-access')):
                policy = settings.resolve(self.repo, delegation_level=level, access=access)
                text = settings.instructions(policy, runtime)
                with self.subTest(runtime=runtime, level=level):
                    self.assertTrue(policy.enabled)
                    self.assertIn(helper.strip(), text)
                    self.assert_markers(text, f'instructions/{runtime}/{level}')
                    self.assertEqual(text.count('--quality-json'), 1)
                    if level == 'auto':
                        self.assertNotIn('%', text)

    def test_disabled_instructions_stay_unchanged_and_omit_the_rubric(self):
        activation.set_enabled(self.repo, False)
        policy = settings.resolve(self.repo, delegation_level='auto', access='auto')
        self.assertFalse(policy.enabled)
        for runtime in ('codex', 'claude'):
            text = settings.instructions(policy, runtime)
            with self.subTest(runtime=runtime):
                self.assertEqual(text, activation.DISABLED_GUIDANCE + '\n')
                self.assertNotIn('quality-json', text)
                self.assertNotIn('rework-json', text)
        # The helper itself remains callable independently of activation.
        self.assertIn('quality-json', settings.rework_guidance())


class InitAttachRefreshTests(BootstrapCase):
    def test_effective_config_instructions_surface_carries_the_rubric(self):
        for runtime in ('codex', 'claude'):
            result = self.run_cli('config', 'show', '--effective', '--path', str(self.repo),
                                  '--instructions', '--runtime', runtime)
            self.assertEqual(result.returncode, 0, result.stderr)
            with self.subTest(runtime=runtime):
                self.assert_markers(result.stdout, f'config-show/{runtime}')

    def test_init_writes_the_rubric_and_preserves_foreign_text_for_both_runtimes(self):
        for runtime in ('codex', 'claude'):
            self.target(runtime).write_bytes(FOREIGN)
            result = self.run_cli('init', '--coordinator', runtime, str(self.repo))
            self.assertEqual(result.returncode, 0, result.stderr)
            body = self.target(runtime).read_bytes()
            with self.subTest(runtime=runtime):
                self.assertTrue(body.startswith(FOREIGN))
                self.assert_markers(body.decode(), f'attach/{runtime}')

    def test_repeated_init_is_idempotent_and_keeps_foreign_text(self):
        for runtime in ('codex', 'claude'):
            self.target(runtime).write_bytes(FOREIGN)
            self.assertEqual(self.run_cli('init', '--coordinator', runtime, str(self.repo)).returncode, 0)
            first = self.target(runtime).read_bytes()
            again = self.run_cli('init', '--coordinator', runtime, str(self.repo))
            with self.subTest(runtime=runtime):
                self.assertEqual(again.returncode, 0, again.stderr)
                self.assertIn('already in the requested state', again.stdout)
                self.assertEqual(self.target(runtime).read_bytes(), first)
                self.assertTrue(self.target(runtime).read_bytes().startswith(FOREIGN))

    def test_project_settings_refresh_rerenders_both_owned_blocks(self):
        for runtime in ('codex', 'claude'):
            self.target(runtime).write_bytes(FOREIGN)
        self.assertEqual(self.run_cli('init', '--coordinator', 'both', str(self.repo)).returncode, 0)
        for runtime in ('codex', 'claude'):
            with self.subTest(runtime=runtime, phase='before'):
                self.assertIn(b'auto (adaptive) / read-only', self.target(runtime).read_bytes())
        result = self.run_cli('config', 'set', '--project', '--path', str(self.repo),
                              '--access', 'full-access')
        self.assertEqual(result.returncode, 0, result.stderr)
        for runtime in ('codex', 'claude'):
            body = self.target(runtime).read_bytes()
            with self.subTest(runtime=runtime, phase='after'):
                self.assertTrue(body.startswith(FOREIGN))
                self.assertIn(b'auto (adaptive) / full-access', body)
                self.assertNotIn(b'auto (adaptive) / read-only', body)
                self.assert_markers(body.decode(), f'refresh/{runtime}')

    def test_off_guidance_replaces_the_rubric_and_re_enable_restores_it(self):
        self.target('codex').write_bytes(FOREIGN)
        self.assertEqual(self.run_cli('init', '--coordinator', 'codex', str(self.repo)).returncode, 0)
        self.assertTrue(self.target('codex').read_bytes().startswith(FOREIGN))
        self.assertEqual(self.run_cli('off', str(self.repo)).returncode, 0)
        refreshed = self.run_cli('config', 'set', '--project', '--path', str(self.repo),
                                 '--access', 'full-access')
        self.assertEqual(refreshed.returncode, 0, refreshed.stderr)
        disabled = self.target('codex').read_bytes()
        self.assertTrue(disabled.startswith(FOREIGN))
        self.assertIn(b'DeepSeek Team is disabled', disabled)
        # ``off`` replaces only the dynamic guidance; the packaged block keeps its
        # static workflow text, so assert the enabled helper section itself is gone.
        self.assertNotIn(b'### Graded review and corrective work', disabled)
        self.assertEqual(self.run_cli('on', str(self.repo)).returncode, 0)
        back = self.run_cli('config', 'set', '--project', '--path', str(self.repo),
                            '--access', 'read-only')
        self.assertEqual(back.returncode, 0, back.stderr)
        restored = self.target('codex').read_bytes()
        self.assertTrue(restored.startswith(FOREIGN))
        self.assertIn(b'### Graded review and corrective work', restored)
        self.assert_markers(restored.decode(), 're-enabled')


if __name__ == '__main__':
    unittest.main()
