"""First-run setup behavior without modifying the real user's machine."""
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from codex_deepseek_team import cli, config, doctor, onboarding, sandbox, worker


class SetupTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.codex = self.root / 'codex'
        self.claude = self.root / 'claude'
        environment = mock.patch.dict(os.environ, {
            'HOME': str(self.root), 'CODEX_HOME': str(self.codex),
            'CLAUDE_CONFIG_DIR': str(self.claude), 'DEEPSEEK_API_KEY': '',
            'XDG_CONFIG_HOME': str(self.root / 'config'),
        })
        environment.start()
        self.addCleanup(environment.stop)
        self.available = {'codex', 'git', 'deepseek-team'}
        which = mock.patch('shutil.which', side_effect=lambda name: '/test/bin/' + name
                           if name in self.available else None)
        which.start()
        self.addCleanup(which.stop)
        backend = sandbox.SandboxBackend(('/test/bin/bwrap',), '/test/bin/bwrap', 'direct')
        self.probe = mock.patch.object(sandbox, 'probe_backend', return_value=backend).start()
        self.addCleanup(mock.patch.stopall)
        self.diagnostic = mock.patch.object(doctor, 'main', return_value=0).start()

    def call(self, *args):
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            try:
                code = cli.main(['setup', *args])
            except SystemExit as error:
                code = error.code
        return code, output.getvalue() + errors.getvalue()

    def test_default_detects_claude_without_configuring_codex(self):
        self.available = {'claude', 'git', 'deepseek-team'}
        code, output = self.call('--no-key')
        self.assertEqual(code, 0, output)
        self.assertFalse(self.codex.exists())
        settings = json.loads((self.claude / 'settings.json').read_text())
        self.assertIn('hooks', settings)
        self.assertIn('claude', self.diagnostic.call_args.args[0])
        self.assertIn('/path/to/project', output)

    def test_missing_selected_runtime_stops_before_configuration_or_credentials(self):
        self.available.discard('codex')
        with mock.patch.object(worker, 'load_api_key', side_effect=AssertionError('credential read')):
            code, output = self.call('--runtime', 'codex')
        self.assertEqual(code, 78, output)
        self.assertIn('codex', output.lower())
        self.assertFalse(self.codex.exists())

    def test_missing_git_is_actionable_and_preserves_configuration(self):
        self.available.discard('git')
        code, output = self.call('--no-key')
        self.assertEqual(code, 78, output)
        self.assertIn('git', output.lower())
        self.assertFalse(self.codex.exists())

    def test_sandbox_failure_does_not_read_key_or_run_privileged_commands(self):
        self.probe.side_effect = sandbox.SandboxError(78, 'missing bwrap')
        with mock.patch.object(worker, 'load_api_key', side_effect=AssertionError('credential read')), \
             mock.patch('subprocess.run', side_effect=AssertionError('unexpected process')):
            code, output = self.call('--runtime', 'codex')
        self.assertEqual(code, 78, output)
        self.assertIn('--with-sandbox', output)
        self.assertFalse(self.codex.exists())

    def test_no_key_defers_authentication_without_reading_existing_credential(self):
        with mock.patch.object(worker, 'load_api_key', side_effect=AssertionError('credential read')):
            code, output = self.call('--no-key')
        self.assertEqual(code, 0, output)
        self.assertIn('auth set', output)
        self.assertIn('/hooks', output)
        self.assertIn('deferred', output.lower())

    def test_noninteractive_missing_key_reports_incomplete_setup(self):
        with mock.patch.object(worker, 'load_api_key', return_value=''), \
             mock.patch('sys.stdin.isatty', return_value=False):
            code, output = self.call()
        self.assertEqual(code, 78, output)
        self.assertIn('auth set', output)
        self.assertIn('incomplete', output.lower())

    def test_failed_local_diagnostics_do_not_read_credentials(self):
        self.diagnostic.return_value = 78
        with mock.patch.object(worker, 'load_api_key', side_effect=AssertionError('credential read')):
            code, output = self.call()
        self.assertEqual(code, 78, output)
        self.assertIn('incomplete', output.lower())

    def test_removed_configure_only_flag_cannot_bypass_readiness(self):
        self.available.clear()
        code, output = self.call('--configure-only', '--runtime', 'codex', '--no-key')
        self.assertEqual(code, 2, output)
        self.assertIn('unrecognized arguments: --configure-only', output)
        self.assertFalse(self.codex.exists())

    def test_setup_preserves_primary_auth_and_saved_key_on_repetition(self):
        self.codex.mkdir()
        (self.codex / 'config.toml').write_text('model="my-primary"\n')
        (self.codex / 'auth.json').write_text('{"sentinel":"preserve"}')
        config.save_key('synthetic-private-key')
        key_path = self.root / '.config/codex-deepseek/api-key'
        before_key = key_path.read_bytes()
        for _ in range(2):
            code, output = self.call('--no-key')
            self.assertEqual(code, 0, output)
        self.assertIn('model="my-primary"', (self.codex / 'config.toml').read_text())
        self.assertEqual((self.codex / 'auth.json').read_text(), '{"sentinel":"preserve"}')
        self.assertEqual(key_path.read_bytes(), before_key)

    def test_missing_command_path_prevents_hooks_that_cannot_run(self):
        self.available.discard('deepseek-team')
        code, output = self.call('--no-key')
        self.assertEqual(code, 78, output)
        self.assertIn('ensurepath', output)
        self.assertFalse(self.codex.exists())

    def test_disabled_claude_hooks_are_preserved_and_readiness_is_incomplete(self):
        self.available.add('claude')
        self.claude.mkdir()
        settings = self.claude / 'settings.json'
        settings.write_text('{"disableAllHooks": true, "model": "preserve"}')
        with mock.patch.object(worker, 'load_api_key', side_effect=AssertionError('credential read')):
            code, output = self.call('--runtime', 'claude')
        self.assertEqual(code, 78, output)
        self.assertIn('disabled', output)
        self.assertTrue(json.loads(settings.read_text())['disableAllHooks'])
        self.assertEqual(json.loads(settings.read_text())['model'], 'preserve')

    def test_success_states_read_only_and_prints_the_explicit_opt_in(self):
        code, output = self.call('--no-key')
        self.assertEqual(code, 0, output)
        self.assertIn('read-only', output)
        self.assertIn('opt in', output.lower())
        self.assertIn('deepseek-team config set --project --access full-access', output)
        # Setup itself must not persist or widen access.
        self.assertFalse((self.root / 'config' / 'deepseek-team' / 'config.toml').exists())

    def test_success_explains_refreshing_project_instructions_after_upgrade(self):
        code, output = self.call('--no-key')
        self.assertEqual(code, 0, output)
        self.assertIn('After upgrading', output)
        self.assertIn('init --coordinator', output)

    def test_setup_runs_outside_a_project_directory(self):
        previous = os.getcwd()
        self.addCleanup(os.chdir, previous)
        os.chdir(self.root)
        code, output = self.call('--no-key')
        self.assertEqual(code, 0, output)
        self.assertIn('/path/to/project', output)

    def test_working_ubuntu_sandbox_needs_no_privileged_changes(self):
        with mock.patch.object(onboarding.platform, 'freedesktop_os_release', return_value={'ID': 'ubuntu'}), \
             mock.patch('subprocess.run', side_effect=AssertionError('unexpected privileged command')), \
             mock.patch.object(sandbox, 'install_apparmor', side_effect=AssertionError('unexpected profile write')):
            code, output = self.call('--with-sandbox', '--no-key')
        self.assertEqual(code, 0, output)
        self.assertIn('no system changes', output)

    def test_explicit_ubuntu_install_uses_sudo_and_probes_result_before_configuring(self):
        self.available.update(('apt-get', 'sudo'))
        backend = sandbox.SandboxBackend(('/test/bwrap',), '/test/bwrap', 'apparmor')
        self.probe.side_effect = [sandbox.SandboxError(78, 'missing'),
                                 sandbox.SandboxError(78, 'profile required'), backend]
        commands = []
        profile = self.root / 'profile-installed'
        def install_profile():
            self.assertFalse(self.codex.exists())
            profile.write_text('installed')
        with mock.patch.object(onboarding.platform, 'freedesktop_os_release', return_value={'ID': 'ubuntu'}), \
             mock.patch.object(onboarding.os, 'geteuid', return_value=1000), \
             mock.patch('subprocess.run', side_effect=lambda command, **kwargs: commands.append(command)), \
             mock.patch.object(sandbox, 'install_apparmor', side_effect=install_profile):
            code, output = self.call('--with-sandbox', '--no-key')
        self.assertEqual(code, 0, output)
        self.assertEqual(commands, [
            ['/test/bin/sudo', '/test/bin/apt-get', 'update'],
            ['/test/bin/sudo', '/test/bin/apt-get', 'install', '-y', 'bubblewrap', 'apparmor'],
        ])
        self.assertTrue(profile.exists())
        self.assertTrue((self.codex / 'config.toml').exists())

    def test_newly_installed_bubblewrap_can_work_without_loading_apparmor(self):
        self.available.update(('apt-get', 'sudo'))
        backend = sandbox.SandboxBackend(('/test/bwrap',), '/test/bwrap', 'direct')
        for uid, prefix in ((0, []), (1000, ['/test/bin/sudo'])):
            self.probe.side_effect = [sandbox.SandboxError(78, 'missing'), backend, backend]
            with self.subTest(uid=uid), \
                 mock.patch.object(onboarding.platform, 'freedesktop_os_release', return_value={'ID': 'ubuntu'}), \
                 mock.patch.object(onboarding.os, 'geteuid', return_value=uid), \
                 mock.patch('subprocess.run') as run, \
                 mock.patch.object(sandbox, 'install_apparmor', side_effect=AssertionError('unnecessary profile load')):
                code, output = self.call('--with-sandbox', '--no-key')
                self.assertEqual(code, 0, output)
                self.assertIn('Local setup checks passed', output)
                self.assertEqual(run.call_args_list, [
                    mock.call([*prefix, '/test/bin/apt-get', 'update'], check=True),
                    mock.call([*prefix, '/test/bin/apt-get', 'install', '-y', 'bubblewrap', 'apparmor'], check=True),
                ])

    def test_unsupported_system_cannot_trigger_privileged_setup(self):
        with mock.patch.object(onboarding.platform, 'freedesktop_os_release', return_value={'ID': 'fedora'}), \
             mock.patch('subprocess.run', side_effect=AssertionError('unexpected process')):
            code, output = self.call('--with-sandbox', '--no-key')
        self.assertEqual(code, 78, output)
        self.assertIn('only on Ubuntu', output)
        self.assertFalse(self.codex.exists())

    def test_failed_apt_setup_does_not_read_credentials_or_claim_success(self):
        self.available.update(('apt-get', 'sudo'))
        self.probe.side_effect = sandbox.SandboxError(78, 'missing')
        with mock.patch.object(onboarding.platform, 'freedesktop_os_release', return_value={'ID': 'ubuntu'}), \
             mock.patch('subprocess.run', side_effect=OSError('failed')), \
             mock.patch.object(worker, 'load_api_key', side_effect=AssertionError('credential read')):
            code, output = self.call('--with-sandbox')
        self.assertEqual(code, 78, output)
        self.assertIn('package installation failed', output)
        self.assertFalse(self.codex.exists())


if __name__ == '__main__':
    unittest.main()
