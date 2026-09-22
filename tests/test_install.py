"""Installer ownership tests using mocked environments."""
import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

SPEC = importlib.util.spec_from_file_location('team_installer', Path(__file__).resolve().parents[1] / 'install.py')
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.prefix = root / 'package'
        self.bin = root / 'bin'
        self.args = ['--prefix', str(self.prefix), '--bin-dir', str(self.bin)]

    def simulate_venv(self, path):
        (path / 'bin').mkdir(parents=True, exist_ok=True)
        for command in installer.COMMANDS:
            (path / 'bin' / command).write_text('entrypoint')

    def test_install_and_repeat_publish_legacy_and_neutral_entrypoints_without_codex(self):
        calls = []
        def run(args, **kwargs):
            calls.append(list(args))
            return subprocess.CompletedProcess(args, 0, '', '')
        with mock.patch.object(installer.venv.EnvBuilder, 'create', side_effect=self.simulate_venv), \
                mock.patch.object(installer.shutil, 'which', return_value=None), \
                mock.patch.object(installer.subprocess, 'run', side_effect=run):
            self.assertEqual(installer.main(self.args), 0)
            self.assertEqual(installer.main(self.args), 0)
        for command_name in installer.COMMANDS:
            command = self.bin / command_name
            self.assertTrue(command.is_symlink())
            self.assertEqual(command.resolve(), self.prefix / 'venv/bin' / command_name)
        self.assertEqual({p.name for p in self.bin.iterdir()}, set(installer.COMMANDS))

        python = str(self.prefix / 'venv/bin/python')
        expected_pip = [python, '-m', 'pip', 'install', '--upgrade',
                        str(Path(installer.__file__).resolve().parent)]
        self.assertEqual(calls, [expected_pip, expected_pip])
        self.assertFalse(any('hooks' in call for call in calls))

    def test_with_sandbox_is_explicit_and_runs_ubuntu_system_setup_after_install(self):
        calls = []
        def run(args, **kwargs):
            calls.append(list(args))
            return subprocess.CompletedProcess(args, 0, '', '')
        with mock.patch.object(installer.venv.EnvBuilder, 'create', side_effect=self.simulate_venv), \
             mock.patch.object(installer, 'is_ubuntu', return_value=True, create=True), \
             mock.patch.object(installer.shutil, 'which', return_value=None), \
             mock.patch.object(installer.subprocess, 'run', side_effect=run):
            self.assertEqual(installer.main([*self.args, '--with-sandbox']), 0)
        pip = str(self.prefix / 'venv/bin/python')
        team = str(self.prefix / 'venv/bin/deepseek-team')
        self.assertTrue(any(call[:4] == [pip, '-m', 'pip', 'install'] for call in calls))
        self.assertIn(['sudo', 'apt-get', 'install', '-y', 'bubblewrap', 'apparmor'], calls)
        self.assertIn([team, 'sandbox', 'install-apparmor'], calls)
        self.assertIn([team, 'sandbox', 'status'], calls)

    def test_standard_install_with_codex_runs_pip_then_installs_coordination_hooks(self):
        calls = []
        def run(args, **kwargs):
            calls.append(list(args))
            return subprocess.CompletedProcess(args, 0, '', '')
        with mock.patch.object(installer.venv.EnvBuilder, 'create', side_effect=self.simulate_venv), \
             mock.patch.object(installer.shutil, 'which', return_value='/usr/bin/codex'), \
             mock.patch.object(installer.subprocess, 'run', side_effect=run):
            self.assertEqual(installer.main(self.args), 0)

        python = str(self.prefix / 'venv/bin/python')
        team = str(self.prefix / 'venv/bin/deepseek-team')
        expected_pip = [python, '-m', 'pip', 'install', '--upgrade',
                        str(Path(installer.__file__).resolve().parent)]
        expected_hooks = [team, 'hooks', 'install']
        self.assertEqual(calls, [expected_pip, expected_hooks])

    def test_repeat_upgrade_with_codex_repeats_pip_and_same_hook_install_only(self):
        calls = []
        def run(args, **kwargs):
            calls.append(list(args))
            return subprocess.CompletedProcess(args, 0, '', '')
        with mock.patch.object(installer.venv.EnvBuilder, 'create', side_effect=self.simulate_venv), \
             mock.patch.object(installer.shutil, 'which', return_value='/usr/bin/codex'), \
             mock.patch.object(installer.subprocess, 'run', side_effect=run):
            self.assertEqual(installer.main(self.args), 0)
            self.assertEqual(installer.main(self.args), 0)

        python = str(self.prefix / 'venv/bin/python')
        team = str(self.prefix / 'venv/bin/deepseek-team')
        expected_pip = [python, '-m', 'pip', 'install', '--upgrade',
                        str(Path(installer.__file__).resolve().parent)]
        expected_hooks = [team, 'hooks', 'install']
        self.assertEqual(
            calls,
            [expected_pip, expected_hooks, expected_pip, expected_hooks],
        )
        flattened = '\n'.join(' '.join(call) for call in calls)
        self.assertNotIn(' init ', flattened)
        self.assertNotIn('find ', flattened)

    def test_plain_install_never_invokes_privileged_sandbox_setup(self):
        calls = []
        def run(args, **kwargs):
            calls.append(list(args))
            return subprocess.CompletedProcess(args, 0, '', '')
        with mock.patch.object(installer.venv.EnvBuilder, 'create', side_effect=self.simulate_venv), \
             mock.patch.object(installer.shutil, 'which', return_value=None), \
             mock.patch.object(installer.subprocess, 'run', side_effect=run):
            self.assertEqual(installer.main(self.args), 0)
        flattened = '\n'.join(' '.join(call) for call in calls)
        self.assertNotIn('sudo', flattened)
        self.assertNotIn('install-apparmor', flattened)

    def test_with_sandbox_refuses_non_ubuntu_instead_of_guessing_system_policy(self):
        with mock.patch.object(installer.venv.EnvBuilder, 'create', side_effect=self.simulate_venv), \
             mock.patch.object(installer, 'is_ubuntu', return_value=False, create=True), \
             mock.patch.object(installer.subprocess, 'run'):
            self.assertEqual(installer.main([*self.args, '--with-sandbox']), 78)

    def test_foreign_prefix_is_never_modified(self):
        self.prefix.mkdir()
        marker = self.prefix / 'unrelated'
        marker.write_bytes(b'keep')
        with mock.patch.object(installer.venv.EnvBuilder, 'create') as create:
            self.assertEqual(installer.main(self.args), 78)
            create.assert_not_called()
        self.assertEqual(list(self.prefix.iterdir()), [marker])

    def test_foreign_entrypoint_is_never_overwritten(self):
        for command_name in installer.COMMANDS:
            with self.subTest(command=command_name):
                if self.bin.exists():
                    import shutil
                    shutil.rmtree(self.bin)
                if self.prefix.exists():
                    import shutil
                    shutil.rmtree(self.prefix)
                self.bin.mkdir()
                command = self.bin / command_name
                command.write_bytes(b'user program')
                self.assertEqual(installer.main(self.args), 78)
                self.assertEqual(command.read_bytes(), b'user program')
                self.assertFalse(self.prefix.exists())

    def test_symlink_prefix_is_refused(self):
        other = self.prefix.parent / 'other'
        other.mkdir()
        self.prefix.symlink_to(other, target_is_directory=True)
        self.assertEqual(installer.main(self.args), 78)
        self.assertEqual(list(other.iterdir()), [])

    def test_failed_pip_does_not_publish_entrypoints(self):
        with mock.patch.object(installer.venv.EnvBuilder, 'create', side_effect=self.simulate_venv), \
                mock.patch.object(installer.subprocess, 'run', side_effect=installer.subprocess.CalledProcessError(1, 'pip')):
            self.assertEqual(installer.main(self.args), 78)
        for command_name in installer.COMMANDS:
            self.assertFalse((self.bin / command_name).exists())

    def test_recovery_from_interrupted_ownership_marker(self):
        self.prefix.mkdir()
        marker = self.prefix / '.codex-deepseek-team-install'
        for partial in [None, '', installer.OWNER[:8]]:
            with self.subTest(partial=partial):
                if partial is not None:
                    marker.write_text(partial)
                with mock.patch.object(installer.venv.EnvBuilder, 'create', side_effect=self.simulate_venv), \
                        mock.patch.object(installer.shutil, 'which', return_value=None), \
                        mock.patch.object(installer.subprocess, 'run'):
                    self.assertEqual(installer.main(self.args), 0)
                self.assertEqual(marker.read_text(), installer.OWNER)
                import shutil
                shutil.rmtree(self.prefix / 'venv')

    def test_equivalent_prefix_and_relative_command_links_allow_update(self):
        with mock.patch.object(installer.venv.EnvBuilder, 'create', side_effect=self.simulate_venv), \
                mock.patch.object(installer.shutil, 'which', return_value=None), \
                mock.patch.object(installer.subprocess, 'run'):
            self.assertEqual(installer.main(self.args), 0)
            for command_name in installer.COMMANDS:
                command = self.bin / command_name
                command.unlink()
                command.symlink_to('../package/venv/bin/' + command_name)
            args = ['--prefix', str(self.prefix / '../package'), '--bin-dir', str(self.bin)]
            self.assertEqual(installer.main(args), 0)


if __name__ == '__main__':
    unittest.main()
