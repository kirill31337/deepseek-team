"""User-facing sandbox setup/status/doctor contract."""
import io
from types import SimpleNamespace
import unittest
from unittest import mock

from codex_deepseek_team import cli, doctor, sandbox


class SandboxCliTests(unittest.TestCase):
    def test_status_reports_backend_and_apparmor_restriction_without_credentials(self):
        backend = sandbox.SandboxBackend(('/usr/bin/bwrap',), '/usr/bin/bwrap', 'direct')
        with mock.patch.object(sandbox, 'probe_backend', return_value=backend), \
             mock.patch.object(sandbox, 'apparmor_restriction', return_value=1), \
             mock.patch('sys.stdout', new_callable=io.StringIO) as output:
            code = cli.main(['sandbox', 'status'])
        self.assertEqual(code, 0)
        text = output.getvalue()
        self.assertIn('direct', text)
        self.assertIn('/usr/bin/bwrap', text)
        self.assertIn('apparmor_restrict_unprivileged_userns=1', text)
        self.assertNotIn('DEEPSEEK_API_KEY', text)

    def test_install_and_remove_dispatch_to_conservative_profile_lifecycle(self):
        with mock.patch.object(sandbox, 'install_apparmor', return_value=True) as install, \
             mock.patch('sys.stdout', new_callable=io.StringIO):
            self.assertEqual(cli.main(['sandbox', 'install-apparmor']), 0)
        install.assert_called_once_with()
        with mock.patch.object(sandbox, 'remove_apparmor', return_value=True) as remove, \
             mock.patch('sys.stdout', new_callable=io.StringIO):
            self.assertEqual(cli.main(['sandbox', 'remove-apparmor']), 0)
        remove.assert_called_once_with()

    def test_sandbox_errors_map_to_exit_78_without_traceback(self):
        with mock.patch.object(sandbox, 'probe_backend',
                               side_effect=sandbox.SandboxError(78, 'safe sandbox failure')), \
             mock.patch('sys.stderr', new_callable=io.StringIO) as errors:
            code = cli.main(['sandbox', 'status'])
        self.assertEqual(code, 78)
        self.assertIn('safe sandbox failure', errors.getvalue())
        self.assertNotIn('Traceback', errors.getvalue())


class DoctorSandboxTests(unittest.TestCase):
    def test_required_doctor_checks_sandbox_before_runtime(self):
        calls = []
        backend = sandbox.SandboxBackend(('/usr/bin/bwrap',), '/usr/bin/bwrap', 'direct')
        def os_check(policy):
            calls.append('sandbox')
            return sandbox, backend
        def runtime_check(_runtime):
            calls.append('runtime')
        with mock.patch.object(doctor.worker, 'resolve_os_sandbox', side_effect=os_check), \
             mock.patch.object(doctor, 'selected_runtimes', return_value=('codex',)), \
             mock.patch.object(doctor, 'check_runtime', side_effect=runtime_check), \
             mock.patch('sys.stdout', new_callable=io.StringIO) as output:
            code = doctor.main(['--offline', '--runtime', 'codex'])
        self.assertEqual(code, 0)
        self.assertEqual(calls, ['sandbox', 'runtime'])
        self.assertIn('direct', output.getvalue())

    def test_doctor_explicit_off_skips_sandbox_probe(self):
        with mock.patch.object(doctor.worker, 'resolve_os_sandbox') as os_check, \
             mock.patch.object(doctor, 'selected_runtimes', return_value=()), \
             mock.patch('sys.stdout', new_callable=io.StringIO):
            code = doctor.main(['--offline', '--runtime', 'codex', '--os-sandbox', 'off'])
        self.assertEqual(code, 0)
        os_check.assert_not_called()

    def test_doctor_reports_codex_coordination_layers_without_claiming_trust(self):
        backend = sandbox.SandboxBackend(('/usr/bin/bwrap',), '/usr/bin/bwrap', 'direct')
        policy = mock.Mock(effective_access='read-only')
        policy.delegation_level = 75
        policy.access = 'read-only'
        policy.sources = {'delegation_level': 'project:x', 'access': 'project:x'}
        with mock.patch.object(doctor, 'resolve_policy', return_value=policy), \
             mock.patch.object(doctor.worker, 'resolve_os_sandbox', return_value=(sandbox, backend)), \
             mock.patch.object(doctor, 'selected_runtimes', return_value=('codex',)), \
             mock.patch.object(doctor, 'check_policy_runtime'), \
             mock.patch.object(doctor, 'coordination_status',
                               return_value=('installed', 'attached', 'native trust must be checked with Codex /hooks')), \
             mock.patch('codex_deepseek_team.settings.describe', return_value='policy'), \
             mock.patch('sys.stdout', new_callable=io.StringIO) as output:
            code = doctor.main(['--offline', '--runtime', 'codex'])
        self.assertEqual(code, 0)
        text = output.getvalue()
        self.assertIn('Coordination hooks: installed', text)
        self.assertIn('Project coordination binding: attached', text)
        self.assertIn('native trust must be checked', text)
        self.assertNotIn('trusted: yes', text.lower())

    def test_doctor_reports_claude_hook_status_without_claiming_effective_trust(self):
        with mock.patch('codex_deepseek_team.claude_config.status', return_value=False):
            installed, _, limitation = doctor.coordination_status('claude')
        self.assertEqual(installed, 'missing-or-disabled')
        self.assertIn('Claude /hooks', limitation)

    def test_live_worker_call_forwards_required_os_sandbox(self):
        with mock.patch.object(doctor.subprocess, 'run', return_value=SimpleNamespace(returncode=0)) as run:
            doctor.call('task', cwd='.', runtime='claude', os_sandbox='required')
        args = run.call_args.args[0]
        self.assertIn('--os-sandbox', args)
        self.assertEqual(args[args.index('--os-sandbox') + 1], 'required')


if __name__ == '__main__':
    unittest.main()
