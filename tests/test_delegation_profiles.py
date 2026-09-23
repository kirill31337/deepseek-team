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
                self.assertEqual(policy.effort, 'auto')
                self.assertEqual(policy.as_dict()['effort_mode'], 'frontier-auto')
                self.assertTrue(policy.as_dict()['percentage_is_target_not_measurement'])

    def test_default_is_adaptive_auto_without_widening_permissions(self):
        self.assertEqual(settings.DEFAULTS['delegation_level'], 'auto')
        self.assertEqual(settings.LEVELS, ('auto', 25, 50, 75))
        policy = settings.resolve(self.repo)
        self.assertEqual(policy.delegation_level, 'auto')
        self.assertEqual(policy.access, 'auto')
        self.assertEqual(policy.effective_access, 'read-only')
        self.assertEqual(policy.sources['delegation_level'], 'default')
        self.assertNotIn('%', settings.describe(policy).splitlines()[1])
        self.assertEqual(policy.as_dict()['delegation_level'], 'auto')

    def test_auto_and_manual_levels_resolve_access_without_widening_fresh_installs(self):
        expected = {'auto': 'read-only', 25: 'read-only', 50: 'full-access', 75: 'full-access'}
        for level, access in expected.items():
            with self.subTest(level=level):
                policy = settings.resolve(self.repo, delegation_level=level, access='auto')
                self.assertEqual(policy.delegation_level, level)
                self.assertEqual(policy.effective_access, access)
        forced = settings.resolve(self.repo, delegation_level='auto', access='full-access')
        self.assertEqual(forced.effective_access, 'full-access')
        self.assertEqual(forced.access, 'full-access')

    def test_manual_numeric_preference_persists_and_stays_numeric(self):
        self.assertTrue(settings.set_values(self.repo / settings.PROJECT_FILE,
                                            delegation_level=50, access='auto'))
        saved = (self.repo / settings.PROJECT_FILE).read_text()
        self.assertIn('delegation_level = 50', saved)
        policy = settings.resolve(self.repo)
        self.assertEqual(policy.delegation_level, 50)
        self.assertEqual(policy.access, 'auto')
        self.assertEqual(policy.effective_access, 'full-access')
        self.assertIn('delegation_level: 50%', settings.describe(policy))
        self.assertTrue(settings.set_values(self.repo / settings.PROJECT_FILE,
                                            delegation_level='auto'))
        saved = (self.repo / settings.PROJECT_FILE).read_text()
        self.assertIn('delegation_level = "auto"', saved)
        self.assertEqual(settings.resolve(self.repo).delegation_level, 'auto')

    def test_invalid_level_types_fail_closed(self):
        for bad in (True, False, 25.0, 50.0, 'AUTO', 'Auto', 'auto ', ' 25', '25', '100',
                    'auto\n', b'auto'):
            with self.subTest(bad=bad), self.assertRaises(settings.SettingsError):
                settings.resolve(self.repo, delegation_level=bad)
        for literal in ('delegation_level = true', 'delegation_level = 25.0',
                        'delegation_level = "25"', 'delegation_level = "AUTO"'):
            (self.repo / settings.PROJECT_FILE).write_text(literal + '\n')
            with self.subTest(toml=literal), self.assertRaises(settings.SettingsError):
                settings.resolve(self.repo)

    def test_parse_level_is_argparse_friendly(self):
        self.assertEqual(settings.parse_level('auto'), 'auto')
        for text, number in (('25', 25), ('50', 50), ('75', 75)):
            with self.subTest(text=text):
                self.assertEqual(settings.parse_level(text), number)
        for bad in ('AUTO', 'Auto', ' auto', 'auto ', '25.0', '0', '100', 'twenty', '', 25, 25.0, True):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                settings.parse_level(bad)

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
        settings.set_values(settings.global_file(), delegation_level=50, access='read-only', effort='high')
        settings.set_values(self.repo / settings.PROJECT_FILE, delegation_level=75, access='auto', effort='auto')

        project_policy = settings.resolve(self.repo)
        self.assertEqual((project_policy.delegation_level, project_policy.access, project_policy.effort),
                         (75, 'auto', 'auto'))
        self.assertTrue(project_policy.sources['delegation_level'].startswith('project:'))
        self.assertTrue(project_policy.sources['access'].startswith('project:'))
        self.assertTrue(project_policy.sources['effort'].startswith('project:'))

        mixed = settings.resolve(self.repo, access='read-only')
        self.assertEqual(mixed.delegation_level, 75)
        self.assertEqual(mixed.access, 'read-only')
        self.assertTrue(mixed.sources['delegation_level'].startswith('project:'))
        self.assertEqual(mixed.sources['access'], 'cli')

        cli = settings.resolve(self.repo, delegation_level=25, access='full-access', effort='low')
        self.assertEqual((cli.delegation_level, cli.effective_access, cli.effort),
                         (25, 'full-access', 'low'))
        self.assertEqual(cli.sources['delegation_level'], 'cli')
        self.assertEqual(cli.sources['access'], 'cli')
        self.assertEqual(cli.sources['effort'], 'cli')

    def test_codex_instructions_require_coordination_protocol_not_percentage_counting(self):
        policy = settings.resolve(self.repo, delegation_level=75, access='full-access')
        text = settings.instructions(policy, 'codex')
        self.assertIn('coordination plan', text)
        self.assertIn('--coord-task', text)
        self.assertIn('coordination use', text)
        self.assertIn('Codex PreToolUse', text)
        self.assertIn('Do not calculate an actual useful-work percentage', text)
        self.assertIn('deepseek-flash', text)
        self.assertIn('Effort policy is auto', text)
        self.assertIn('--effort low', text)
        self.assertIn('native-agent', text)
        self.assertIn('delegation_reason', text)

    def test_auto_instructions_are_adaptive_without_percentage_or_paid_exploration(self):
        policy = settings.resolve(self.repo, delegation_level='auto', access='auto')
        text = settings.instructions(policy, 'codex')
        self.assertIn('Effective delegation profile: auto (adaptive) / read-only', text)
        self.assertNotIn('%', text)
        self.assertIn('adaptive', text)
        self.assertIn('features', text)
        for feature in ('kind', 'domain', 'operation', 'localization', 'coupling', 'verification',
                        'clarity', 'risk', 'scope_size', 'runtime', 'model', 'effort',
                        'context_version'):
            with self.subTest(feature=feature):
                self.assertIn(feature, text)
        self.assertIn('executor', text)
        self.assertIn('routing status', text)
        self.assertIn('routing configure', text)
        self.assertIn('Cold start may abstain', text)
        self.assertIn('Read-only auto does not delegate writing tasks', text)
        self.assertIn('no automatic paid exploration', text.lower())
        self.assertIn('Protected coordinator responsibilities', text)

    def test_manual_instructions_keep_fixed_percentage_and_feedback(self):
        policy = settings.resolve(self.repo, delegation_level=75, access='full-access')
        text = settings.instructions(policy, 'codex')
        self.assertIn('Effective delegation profile: 75% / full-access', text)
        self.assertIn('collect feedback', text)
        self.assertIn('Do not calculate an actual useful-work percentage', text)
        self.assertIn('Explicit executor choices', text)

    def test_forced_effort_policy_is_persisted_and_removes_frontier_choice(self):
        settings.set_values(self.repo / settings.PROJECT_FILE, effort='high')
        policy = settings.resolve(self.repo)
        self.assertEqual(policy.effort, 'high')
        self.assertEqual(policy.as_dict()['effort_mode'], 'forced')
        text = settings.instructions(policy, 'codex')
        self.assertIn('persistently forced to high', text)
        self.assertIn('--effort high', text)
        self.assertNotIn('Effort policy is auto', text)
        settings.set_values(self.repo / settings.PROJECT_FILE, effort='auto')
        self.assertEqual(settings.resolve(self.repo).effort, 'auto')

    def test_claude_instructions_require_runtime_specific_hook_protocol(self):
        policy = settings.resolve(self.repo, delegation_level=75, access='full-access')
        text = settings.instructions(policy, 'claude')
        self.assertIn('Claude PreToolUse', text)
        self.assertIn('coordination plan', text)
        self.assertIn('--runtime claude', text)
        self.assertIn('--coord-task', text)
        self.assertIn('coordination use', text)
        self.assertIn('native-agent', text)
        self.assertIn('Effort policy is auto', text)
        self.assertIn('stop_hook_active', text)
        self.assertNotIn('instruction-driven', text)

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
        with self.assertRaises(settings.SettingsError):
            settings.resolve(self.repo, effort='max')
        (self.repo / settings.PROJECT_FILE).write_text('delegation_level = 60\n')
        with self.assertRaises(settings.SettingsError):
            settings.resolve(self.repo)

    def test_config_cli_sets_fields_independently_and_show_reports_sources(self):
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(delegation_cli.main([
                'config', 'set', '--project', '--path', str(self.repo),
                '--delegation-level', '50', '--access', 'read-only', '--effort', 'high',
            ]), 0)
        with redirect_stdout(out := io.StringIO()):
            self.assertEqual(delegation_cli.main([
                'config', 'set', '--project', '--path', str(self.repo),
                '--delegation-level', '75',
            ]), 0)
        policy = settings.resolve(self.repo)
        self.assertEqual(policy.delegation_level, 75)
        self.assertEqual(policy.access, 'read-only')
        self.assertEqual(policy.effort, 'high')

        with redirect_stdout(out := io.StringIO()):
            self.assertEqual(delegation_cli.main([
                'config', 'show', '--effective', '--path', str(self.repo), '--json',
            ]), 0)
        shown = json.loads(out.getvalue())
        self.assertEqual(shown['delegation_level'], 75)
        self.assertEqual(shown['effective_access'], 'read-only')
        self.assertEqual(shown['effort'], 'high')
        self.assertEqual(shown['effort_mode'], 'forced')
        self.assertIn('project:', shown['sources']['access'])

    def test_config_cli_sets_and_shows_auto_then_numeric_unchanged(self):
        with redirect_stdout(io.StringIO()):
            self.assertEqual(delegation_cli.main([
                'config', 'set', '--project', '--path', str(self.repo),
                '--delegation-level', 'auto',
            ]), 0)
        self.assertIn('delegation_level = "auto"',
                      (self.repo / settings.PROJECT_FILE).read_text())
        policy = settings.resolve(self.repo)
        self.assertEqual(policy.delegation_level, 'auto')
        self.assertEqual(policy.effective_access, 'read-only')
        self.assertIn('delegation_level: auto', settings.describe(policy))

        with redirect_stdout(out := io.StringIO()):
            self.assertEqual(delegation_cli.main([
                'config', 'show', '--effective', '--path', str(self.repo), '--json',
            ]), 0)
        shown = json.loads(out.getvalue())
        self.assertEqual(shown['delegation_level'], 'auto')
        self.assertEqual(shown['effective_access'], 'read-only')

        with redirect_stdout(io.StringIO()):
            self.assertEqual(delegation_cli.main([
                'config', 'set', '--project', '--path', str(self.repo),
                '--delegation-level', '50',
            ]), 0)
        self.assertIn('delegation_level = 50',
                      (self.repo / settings.PROJECT_FILE).read_text())
        self.assertEqual(settings.resolve(self.repo).delegation_level, 50)

    def test_config_cli_rejects_non_literal_level_values(self):
        for value in ('AUTO', 'Auto', '40', '0', '25.0', 'auto '):
            with self.subTest(value=value), self.assertRaises(SystemExit):
                delegation_cli.main([
                    'config', 'set', '--project', '--path', str(self.repo),
                    '--delegation-level', value,
                ])


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


class RemovedLegacyWriterTests(unittest.TestCase):
    def parse(self, *argv):
        with mock.patch.object(sys, 'argv', ['deepseek-team worker', *argv]):
            return worker.parse_args()

    def test_legacy_writer_flags_are_rejected(self):
        for argv in [
            ('--write', 'implement'),
            ('--allow-write', 'src/example.py', 'implement'),
        ]:
            with self.subTest(argv=argv), self.assertRaises(SystemExit):
                self.parse(*argv)



class WorkerLevelParserTests(unittest.TestCase):
    def parse(self, *argv):
        with mock.patch.object(sys, 'argv', ['deepseek-team worker', *argv]):
            return worker.parse_args()

    def test_worker_accepts_auto_and_numeric_levels(self):
        self.assertEqual(self.parse('--delegation-level', 'auto').delegation_level, 'auto')
        self.assertEqual(self.parse('--delegation-level', '25').delegation_level, 25)
        self.assertEqual(self.parse('--delegation-level', '75').delegation_level, 75)

    def test_worker_rejects_other_level_values(self):
        for value in ('AUTO', '30', 'auto '):
            with self.subTest(value=value), self.assertRaises(SystemExit):
                self.parse('--delegation-level', value)


class ManagedBlockTests(RepoCase):
    def test_profile_refresh_preserves_user_text_and_detach(self):
        agents = self.repo / 'AGENTS.md'
        agents.write_text('USER PREFIX\n')
        self.assertTrue(project.attach(self.repo, coordinator='codex'))
        agents.write_text(agents.read_text() + 'USER SUFFIX\n')

        settings.set_values(self.repo / settings.PROJECT_FILE,
                            delegation_level=75, access='read-only', effort='high')
        self.assertTrue(project.attach(self.repo, coordinator='codex'))
        text = agents.read_text()
        self.assertTrue(text.startswith('USER PREFIX\n'))
        self.assertTrue(text.endswith('USER SUFFIX\n'))
        self.assertEqual(text.count(project.START_MARKER.decode()), 1)
        self.assertIn('Effective delegation profile: 75% / read-only; effort=high', text)
        self.assertIn('explicit read-only always remains read-only', text)

        self.assertTrue(project.detach(self.repo, coordinator='codex'))
        self.assertEqual(agents.read_text(), 'USER PREFIX\nUSER SUFFIX\n')


class DoctorPolicyTests(RepoCase):
    def test_doctor_resolver_uses_same_profile_rules(self):
        policy = doctor.resolve_policy(root=self.repo, delegation_level=50, access='auto')
        self.assertEqual(policy.delegation_level, 50)
        self.assertEqual(policy.effective_access, 'full-access')
        self.assertEqual(policy.effort, 'auto')

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
        self.assertIn('effort: auto', output.getvalue())

    def test_doctor_parser_accepts_auto_and_rejects_other_levels(self):
        backend = sandbox.SandboxBackend(('/usr/bin/bwrap',), '/usr/bin/bwrap', 'direct')
        policy = settings.Policy('auto', 'auto',
                                 {'delegation_level': 'cli', 'access': 'cli'})
        with mock.patch.object(doctor, 'resolve_policy', return_value=policy) as resolve, \
             mock.patch.object(doctor.worker, 'resolve_os_sandbox',
                               return_value=(sandbox, backend)), \
             mock.patch.object(doctor, 'selected_runtimes', return_value=()), \
             mock.patch('sys.stdout', new_callable=io.StringIO):
            code = doctor.main(['--offline', '--runtime', 'codex',
                                '--delegation-level', 'auto', '--access', 'auto'])
        self.assertEqual(code, 0)
        self.assertEqual(resolve.call_args.kwargs['delegation_level'], 'auto')
        for value in ('AUTO', '40', 'auto '):
            with self.subTest(value=value), self.assertRaises(SystemExit):
                doctor.main(['--offline', '--delegation-level', value])

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
