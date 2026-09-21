"""Delegation profile/configuration contracts independent of paid provider calls."""
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from contextlib import redirect_stdout

from codex_deepseek_team import delegation_cli, development, doctor, project, sandbox, settings, worker


class RepoCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='dst-policy-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / 'home'
        self.home.mkdir()
        self.config = self.root / 'config'
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        subprocess.run(['git', 'init', '-q', str(self.repo)], check=True)
        self.env = mock.patch.dict(
            os.environ,
            {'HOME': str(self.home), 'XDG_CONFIG_HOME': str(self.config)},
        )
        self.env.start()
        self.addCleanup(self.env.stop)


class PolicyResolutionTests(RepoCase):
    def test_profiles_resolve_auto_access(self):
        expected = {25: 'read-only', 50: 'full-access', 75: 'full-access'}
        for level, access in expected.items():
            with self.subTest(level=level):
                policy = settings.resolve(self.repo, delegation_level=level, access='auto')
                self.assertEqual(policy.delegation_level, level)
                self.assertEqual(policy.access, 'auto')
                self.assertEqual(policy.effective_access, access)
                self.assertTrue(policy.as_dict()['percentage_is_target_not_measurement'])

    def test_explicit_access_is_independent_of_level(self):
        self.assertEqual(
            settings.resolve(self.repo, delegation_level=75, access='read-only').effective_access,
            'read-only',
        )
        self.assertEqual(
            settings.resolve(self.repo, delegation_level=25, access='full-access').effective_access,
            'full-access',
        )

    def test_cli_project_global_default_precedence_and_sources(self):
        settings.set_values(settings.global_file(), delegation_level=50, access='read-only')
        settings.set_values(self.repo / settings.PROJECT_FILE, delegation_level=75, access='auto')

        project_policy = settings.resolve(self.repo)
        self.assertEqual((project_policy.delegation_level, project_policy.access), (75, 'auto'))
        self.assertTrue(project_policy.sources['delegation_level'].startswith('project:'))
        self.assertTrue(project_policy.sources['access'].startswith('project:'))

        mixed = settings.resolve(self.repo, access='read-only')
        self.assertEqual(mixed.delegation_level, 75)
        self.assertEqual(mixed.access, 'read-only')
        self.assertTrue(mixed.sources['delegation_level'].startswith('project:'))
        self.assertEqual(mixed.sources['access'], 'cli')

        cli = settings.resolve(self.repo, delegation_level=25, access='full-access')
        self.assertEqual((cli.delegation_level, cli.effective_access), (25, 'full-access'))
        self.assertEqual(cli.sources['delegation_level'], 'cli')
        self.assertEqual(cli.sources['access'], 'cli')

    def test_read_only_override_keeps_high_delegation_target_without_write_authority(self):
        policy = settings.resolve(self.repo, delegation_level=75, access='read-only')
        text = settings.instructions(policy, 'codex')
        self.assertIn('Delegate most separable analysis', text)
        self.assertIn('up to three read-only workers', text)
        self.assertIn('Actual access is read-only', text)
        self.assertNotIn('Allow the worker to create/edit/delete any project files', text)

    def test_invalid_values_fail_closed(self):
        with self.assertRaises(settings.SettingsError):
            settings.resolve(self.repo, delegation_level=40)
        (self.repo / settings.PROJECT_FILE).write_text('delegation_level = 60\n')
        with self.assertRaises(settings.SettingsError):
            settings.resolve(self.repo)

    def test_config_cli_sets_fields_independently_and_show_reports_sources(self):
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(delegation_cli.main([
                'config', 'set', '--project', '--path', str(self.repo),
                '--delegation-level', '50', '--access', 'read-only',
            ]), 0)
        with redirect_stdout(out := io.StringIO()):
            self.assertEqual(delegation_cli.main([
                'config', 'set', '--project', '--path', str(self.repo),
                '--delegation-level', '75',
            ]), 0)
        policy = settings.resolve(self.repo)
        self.assertEqual(policy.delegation_level, 75)
        self.assertEqual(policy.access, 'read-only')

        with redirect_stdout(out := io.StringIO()):
            self.assertEqual(delegation_cli.main([
                'config', 'show', '--effective', '--path', str(self.repo), '--json',
            ]), 0)
        shown = json.loads(out.getvalue())
        self.assertEqual(shown['delegation_level'], 75)
        self.assertEqual(shown['effective_access'], 'read-only')
        self.assertIn('project:', shown['sources']['access'])


class ManagedRuntimeMountTests(RepoCase):
    def test_home_npm_runtime_mounts_package_not_entire_user_prefix(self):
        prefix = self.home / '.local'
        package = prefix / 'lib/node_modules/@vendor/runtime'
        package.mkdir(parents=True)
        target = package / 'cli.js'
        target.write_text('console.log("fixture")\n')
        launcher = prefix / 'bin/runtime'
        launcher.parent.mkdir(parents=True)
        launcher.symlink_to(Path('../lib/node_modules/@vendor/runtime/cli.js'))
        node = prefix / 'bin/node'
        node.write_text('#!/bin/sh\n')
        node.chmod(0o700)

        with mock.patch.object(development.sys, 'executable', '/usr/bin/python3'), \
             mock.patch.object(development.sys, 'base_prefix', '/usr'), \
             mock.patch.object(development.shutil, 'which', return_value=str(node)):
            roots = development.runtime_roots([str(launcher)])

        self.assertNotIn(prefix, roots)
        self.assertIn(launcher, roots)
        self.assertIn(package, roots)
        self.assertIn(node, roots)
        for root in roots:
            self.assertNotEqual(root, self.home)


class LegacyCompatibilityTests(unittest.TestCase):
    def parse(self, *argv):
        with mock.patch.object(sys, 'argv', ['deepseek-team worker', *argv]):
            return worker.parse_args()

    def test_legacy_exact_file_writer_still_parses_without_new_settings(self):
        args = self.parse('--write', '--allow-write', 'src/example.py', 'implement')
        self.assertTrue(args.write)
        self.assertEqual(args.allow_write, ['src/example.py'])
        self.assertIsNone(args.access)
        self.assertIsNone(args.delegation_level)
        self.assertEqual(args.attempts, 1)

    def test_conflicting_legacy_and_new_access_is_rejected(self):
        with self.assertRaises(SystemExit):
            self.parse('--write', '--allow-write', 'src/example.py',
                       '--access', 'full-access', 'implement')
        with self.assertRaises(SystemExit):
            self.parse('--write', '--allow-write', 'src/example.py',
                       '--workspace', '0' * 32, 'implement')


class ManagedBlockTests(RepoCase):
    def test_profile_refresh_preserves_user_text_and_detach(self):
        agents = self.repo / 'AGENTS.md'
        agents.write_text('USER PREFIX\n')
        self.assertTrue(project.attach(self.repo, coordinator='codex'))
        agents.write_text(agents.read_text() + 'USER SUFFIX\n')

        settings.set_values(self.repo / settings.PROJECT_FILE,
                            delegation_level=75, access='read-only')
        self.assertTrue(project.attach(self.repo, coordinator='codex'))
        text = agents.read_text()
        self.assertTrue(text.startswith('USER PREFIX\n'))
        self.assertTrue(text.endswith('USER SUFFIX\n'))
        self.assertEqual(text.count(project.START_MARKER.decode()), 1)
        self.assertIn('Effective delegation profile: 75% / read-only', text)
        self.assertIn('explicit read-only always remains read-only', text)

        self.assertTrue(project.detach(self.repo, coordinator='codex'))
        self.assertEqual(agents.read_text(), 'USER PREFIX\nUSER SUFFIX\n')


class DoctorPolicyTests(RepoCase):
    def test_doctor_resolver_uses_same_profile_rules(self):
        policy = doctor.resolve_policy(root=self.repo, delegation_level=50, access='auto')
        self.assertEqual(policy.delegation_level, 50)
        self.assertEqual(policy.effective_access, 'full-access')

    def test_doctor_reports_policy_and_checks_matching_runtime_surface(self):
        policy = settings.Policy(50, 'auto',
                                 {'delegation_level': 'cli', 'access': 'cli'})
        backend = sandbox.SandboxBackend(('/usr/bin/bwrap',), '/usr/bin/bwrap', 'direct')
        with mock.patch.object(doctor, 'resolve_policy', return_value=policy), \
             mock.patch.object(doctor.worker, 'resolve_os_sandbox',
                               return_value=(sandbox, backend)), \
             mock.patch.object(doctor, 'selected_runtimes', return_value=('claude',)), \
             mock.patch.object(doctor, 'check_policy_runtime') as runtime_check, \
             mock.patch('sys.stdout', new_callable=io.StringIO) as output:
            code = doctor.main(['--offline', '--runtime', 'claude',
                                '--delegation-level', '50', '--access', 'auto'])
        self.assertEqual(code, 0)
        runtime_check.assert_called_once_with('claude', policy)
        self.assertIn('delegation_level: 50%', output.getvalue())
        self.assertIn('effective_access: full-access', output.getvalue())

    def test_doctor_refuses_full_access_with_os_sandbox_off(self):
        policy = settings.Policy(75, 'full-access',
                                 {'delegation_level': 'cli', 'access': 'cli'})
        with mock.patch.object(doctor, 'resolve_policy', return_value=policy), \
             mock.patch.object(doctor, 'check_policy_runtime') as runtime_check, \
             mock.patch('sys.stderr', new_callable=io.StringIO) as errors:
            code = doctor.main(['--offline', '--runtime', 'codex',
                                '--os-sandbox', 'off'])
        self.assertEqual(code, 64)
        runtime_check.assert_not_called()
        self.assertIn('full-access', errors.getvalue())


if __name__ == '__main__':
    unittest.main()
