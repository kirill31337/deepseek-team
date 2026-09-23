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

from codex_deepseek_team import (activation, delegation_cli, development, doctor, project,
                                 sandbox, settings, worker)


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
        self.assertIn('Admission is immediate', text)
        self.assertIn('There is no initial trial quota or periodic coordinator holdout', text)
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
        self.assertIn('Execution capacity is 8', text)
        self.assertIn('additional jobs queue', text)
        self.assertIn('Actual access is read-only', text)
        self.assertNotIn('Allow the worker to create/edit/delete any project files', text)

    def test_invalid_values_fail_closed(self):
        with self.assertRaises(settings.SettingsError):
            settings.resolve(self.repo, delegation_level=40)
        for bad_effort in ('xhigh', 'MAX', 'High', ' ultra', True, 3):
            with self.subTest(effort=bad_effort), self.assertRaises(settings.SettingsError):
                settings.resolve(self.repo, effort=bad_effort)
        (self.repo / settings.PROJECT_FILE).write_text('delegation_level = 60\n')
        with self.assertRaises(settings.SettingsError):
            settings.resolve(self.repo)

    def test_canonical_max_level_is_accepted_and_resolved(self):
        settings.set_values(self.repo / settings.PROJECT_FILE, effort='max')
        policy = settings.resolve(self.repo)
        self.assertEqual(policy.effort, 'max')
        self.assertEqual(policy.as_dict()['effort_mode'], 'forced')
        text = settings.instructions(policy, 'codex')
        self.assertIn('persistently forced to max', text)
        self.assertIn('--effort max', text)

    def test_legacy_medium_alias_normalizes_to_high_without_rewriting_the_file(self):
        file = self.repo / settings.PROJECT_FILE
        file.write_text('effort = "medium"\n')
        policy = settings.resolve(self.repo)
        self.assertEqual(policy.effort, 'high')
        self.assertEqual(policy.as_dict()['effort_mode'], 'forced')
        # A saved legacy spelling stays on disk until a writer actually changes it.
        self.assertIn('effort = "medium"', file.read_text())
        settings.set_values(file, effort='medium')
        self.assertIn('effort = "medium"', file.read_text())
        settings.set_values(file, effort='low')
        self.assertIn('effort = "low"', file.read_text())

    def test_config_cli_accepts_legacy_medium_and_saves_high(self):
        with redirect_stdout(io.StringIO()):
            self.assertEqual(delegation_cli.main([
                'config', 'set', '--project', '--path', str(self.repo), '--effort', 'medium',
            ]), 0)
        self.assertIn('effort = "high"', (self.repo / settings.PROJECT_FILE).read_text())
        self.assertEqual(settings.resolve(self.repo).effort, 'high')
        with redirect_stdout(out := io.StringIO()):
            self.assertEqual(delegation_cli.main([
                'config', 'show', '--effective', '--path', str(self.repo), '--json',
            ]), 0)
        self.assertEqual(json.loads(out.getvalue())['effort'], 'high')

    def test_auto_instructions_recommend_low_high_max_and_explain_legacy_medium(self):
        policy = settings.resolve(self.repo, effort='auto')
        text = settings.instructions(policy, 'codex')
        self.assertIn('--effort low, --effort high or --effort max', text)
        self.assertIn('--effort medium is still accepted', text)
        self.assertIn('high as an execution fallback', text)

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

    def test_worker_parser_accepts_canonical_levels_and_legacy_medium(self):
        for level in ('low', 'high', 'max', 'medium'):
            with self.subTest(level=level):
                self.assertEqual(self.parse('--effort', level).effort, level)
        for bad in ('xhigh', 'none'):
            with self.subTest(level=bad), self.assertRaises(SystemExit):
                self.parse('--effort', bad)

    def test_worker_accepts_auto_and_numeric_levels(self):
        self.assertEqual(self.parse('--delegation-level', 'auto').delegation_level, 'auto')
        self.assertEqual(self.parse('--delegation-level', '25').delegation_level, 25)
        self.assertEqual(self.parse('--delegation-level', '75').delegation_level, 75)

    def test_worker_rejects_other_level_values(self):
        for value in ('AUTO', '30', 'auto '):
            with self.subTest(value=value), self.assertRaises(SystemExit):
                self.parse('--delegation-level', value)


class ManagedCommandEffortTests(RepoCase):
    def test_default_effort_levels_are_canonical(self):
        from codex_deepseek_team import effort as effort_module
        self.assertEqual(worker.DEFAULT_EFFORT, 'high')
        self.assertEqual(effort_module.DEFAULT_EFFORT, 'high')
        self.assertEqual(effort_module.normalize_effort('medium'), 'high')
        self.assertEqual(worker.EFFORT_LEVELS, ('low', 'high', 'max', 'medium'))
        self.assertEqual(settings.EFFORT, ('auto', 'low', 'high', 'max'))
        self.assertIn('medium', settings.EFFORT_CHOICES)

    def test_managed_codex_command_emits_canonical_effort(self):
        for effort, expected in (('max', 'max'), ('medium', 'high'), ('low', 'low'), ('high', 'high')):
            with self.subTest(effort=effort):
                args = development.runtime_command('/usr/bin/codex', 'codex', writable=True, effort=effort)
                self.assertIn(f'model_reasoning_effort="{expected}"', args)

    def test_managed_child_environment_emits_canonical_effort_for_claude(self):
        for effort, expected in (('max', 'max'), ('medium', 'high'), ('low', 'low')):
            with self.subTest(effort=effort):
                env = worker.child_environment(self.home, 'synthetic', 'claude', effort)
                self.assertEqual(env['CLAUDE_CODE_EFFORT_LEVEL'], expected)


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
            with self.assertRaises(SystemExit) as rejected:
                doctor.main(['--offline', '--runtime', 'codex', '--os-sandbox', 'off'])
        self.assertEqual(rejected.exception.code, 2)
        runtime_check.assert_not_called()
        self.assertIn('invalid choice', errors.getvalue())


class ReleaseInstructionContractTests(RepoCase):
    """The 0.8.3 plan-every-subtask and native-exception instruction contract."""

    def test_instructions_require_planning_and_routing_every_delegated_subtask(self):
        for runtime in ('codex', 'claude'):
            policy = settings.resolve(self.repo, delegation_level='auto', access='auto')
            text = settings.instructions(policy, runtime)
            with self.subTest(runtime=runtime):
                self.assertIn('every delegated subtask', text)
                self.assertIn('read-only history, search or review', text)
                self.assertIn('worker decision means DeepSeek', text)
                self.assertIn('executor: "auto"', text)
                self.assertIn('Ordinary short answers', text)
                self.assertIn('wait and control calls create no work', text)
                self.assertIn('already running agent also requires routing', text)
                self.assertNotIn('%', text)

    def test_native_exception_contract_is_documented(self):
        policy = settings.resolve(self.repo, delegation_level=75, access='full-access')
        text = settings.instructions(policy, 'codex')
        self.assertIn('native_exception', text)
        self.assertIn('explicit_user_request', text)
        self.assertIn('native_capability', text)
        self.assertIn('delegation_reason', text)
        self.assertIn('[deepseek-team:TASK_ID:DELIVERABLE_ID]', text)
        self.assertIn('not mechanically proven user provenance', text)
        self.assertIn('publishing stay coordinator-only', text)
        self.assertIn('convenience alone is not a sufficient reason', text)

    def test_hook_coverage_is_not_overclaimed_and_sandbox_is_unchanged(self):
        policy = settings.resolve(self.repo, delegation_level='auto', access='auto')
        for runtime in ('codex', 'claude'):
            text = settings.instructions(policy, runtime)
            with self.subTest(runtime=runtime):
                self.assertIn('only the events the runtime actually delivers', text)
                self.assertIn('universal hook coverage must never be claimed', text)
                self.assertIn('mandatory, unchanged worker OS sandbox', text)
                self.assertNotIn('--os-sandbox off', text)

    def test_packaged_delegation_guidance_carries_the_same_contract(self):
        body = project.DATA_FILE.read_text()
        for marker in (
            'every delegated subtask', 'executor: "auto"', 'worker decision means DeepSeek',
            'native_exception', 'explicit_user_request', 'native_capability',
            '[deepseek-team:TASK_ID:DELIVERABLE_ID]', 'not mechanically proven user provenance',
            'mandatory worker OS sandbox is unchanged',
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, body)

    def test_off_read_only_and_manual_profiles_are_preserved(self):
        settings.set_values(self.repo / settings.PROJECT_FILE,
                            delegation_level=25, access='read-only')
        read_only = settings.instructions(settings.resolve(self.repo), 'codex')
        self.assertIn('Actual access is read-only', read_only)

        auto_read_only = settings.instructions(
            settings.resolve(self.repo, delegation_level='auto', access='read-only'), 'codex')
        self.assertIn('Read-only auto does not delegate writing tasks', auto_read_only)

        settings.set_values(self.repo / settings.PROJECT_FILE,
                            delegation_level=75, access='full-access')
        manual = settings.instructions(settings.resolve(self.repo), 'codex')
        self.assertIn('collect feedback', manual)

        activation.set_enabled(self.repo, False)
        disabled = settings.instructions(settings.resolve(self.repo), 'codex')
        self.assertIn('disabled', disabled.lower())
        self.assertNotIn('native_exception', disabled)
        activation.set_enabled(self.repo, True)


if __name__ == '__main__':
    unittest.main()
