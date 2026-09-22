"""OS sandbox contract for DeepSeek worker subprocesses."""
from pathlib import Path
import subprocess
import tempfile
import unittest

from codex_deepseek_team import sandbox


class FakeRunner:
    def __init__(self, direct=0, apparmor=0, help_text=None):
        self.direct = direct
        self.apparmor = apparmor
        self.help_text = help_text or (
            '--die-with-parent --new-session --unshare-user --unshare-pid '
            '--unshare-ipc --unshare-uts --unshare-cgroup-try --disable-userns '
            '--cap-drop --ro-bind --proc --dev --tmpfs --bind --chdir'
        )
        self.calls = []

    def __call__(self, args, **kwargs):
        args = list(args)
        self.calls.append(args)
        if args[-1:] == ['--help']:
            return subprocess.CompletedProcess(args, 0, self.help_text, '')
        if args[0] == '/usr/bin/aa-exec':
            return subprocess.CompletedProcess(args, self.apparmor, '', 'apparmor blocked' if self.apparmor else '')
        return subprocess.CompletedProcess(args, self.direct, '', 'direct blocked' if self.direct else '')


class RecordingRunner:
    def __init__(self, returncode=0):
        self.returncode = returncode
        self.calls = []

    def __call__(self, args, **kwargs):
        self.calls.append(list(args))
        return subprocess.CompletedProcess(args, self.returncode, '', '')


def fake_which(with_aa=True):
    mapping = {'bwrap': '/usr/bin/bwrap'}
    if with_aa:
        mapping['aa-exec'] = '/usr/bin/aa-exec'
    return mapping.get


class BackendSelectionTests(unittest.TestCase):
    def test_direct_bwrap_is_preferred_over_apparmor_fallback(self):
        runner = FakeRunner(direct=0, apparmor=0)
        backend = sandbox.probe_backend(
            which=fake_which(), runner=runner, restriction_reader=lambda: 1)
        self.assertEqual(backend.source, 'direct')
        self.assertEqual(backend.prefix, ('/usr/bin/bwrap',))
        self.assertFalse(any(call[0] == '/usr/bin/aa-exec' for call in runner.calls))

    def test_apparmor_named_profile_is_used_only_after_direct_probe_fails(self):
        runner = FakeRunner(direct=1, apparmor=0)
        backend = sandbox.probe_backend(
            which=fake_which(), runner=runner, restriction_reader=lambda: 1)
        self.assertEqual(backend.source, 'apparmor')
        self.assertEqual(
            backend.prefix,
            ('/usr/bin/aa-exec', '-p', sandbox.PROFILE_NAME, '--', '/usr/bin/bwrap'))
        aa_calls = [call for call in runner.calls if call[0] == '/usr/bin/aa-exec']
        self.assertEqual(len(aa_calls), 1)

    def test_missing_secure_backend_fails_closed(self):
        runner = FakeRunner(direct=1)
        with self.assertRaises(sandbox.SandboxError) as caught:
            sandbox.probe_backend(
                which=fake_which(with_aa=False), runner=runner,
                restriction_reader=lambda: 1)
        self.assertEqual(caught.exception.code, 78)
        self.assertIn('bubblewrap', caught.exception.message.lower())
        self.assertIn('apparmor', caught.exception.message.lower())

    def test_missing_required_bwrap_features_is_rejected_before_probe(self):
        runner = FakeRunner(help_text='--unshare-user --ro-bind')
        with self.assertRaises(sandbox.SandboxError) as caught:
            sandbox.probe_backend(
                which=fake_which(), runner=runner, restriction_reader=lambda: 0)
        self.assertEqual(caught.exception.code, 78)
        self.assertIn('required', caught.exception.message.lower())


class CommandLayoutTests(unittest.TestCase):
    def backend(self):
        return sandbox.SandboxBackend(('/usr/bin/bwrap',), '/usr/bin/bwrap', 'direct')

    def test_readonly_layout_hides_real_home_and_mounts_worktree_readonly(self):
        cwd = Path('/home/alice/projects/repo')
        session = Path('/home/alice/.local/state/codex-deepseek/session-1')
        env = {
            'HOME': str(session),
            'PATH': '/home/alice/.local/bin:/usr/bin:/bin',
            'ANTHROPIC_AUTH_TOKEN': 'SECRET_VALUE_MUST_NOT_APPEAR_IN_ARGV',
        }
        existing_paths = {
            '/home/alice', '/home/alice/.local', str(session), str(cwd), '/var/tmp',
        }
        args = sandbox.wrap_command(
            ['/home/alice/.local/bin/claude', '--bare'], cwd=cwd,
            session_home=session, env=env,
            backend=self.backend(), real_home=Path('/home/alice'),
            existing=lambda path: str(path) in existing_paths,
            is_dir=lambda _path: True)
        joined = '\0'.join(args)
        for flag in ['--die-with-parent', '--new-session', '--unshare-user',
                     '--unshare-pid', '--unshare-ipc', '--unshare-uts',
                     '--disable-userns', '--cap-drop', '--ro-bind', '--proc',
                     '--dev', '--tmpfs', '--chdir']:
            self.assertIn(flag, args)
        self.assertNotIn('SECRET_VALUE_MUST_NOT_APPEAR_IN_ARGV', joined)
        home_mask = next(i for i in range(len(args) - 1)
                         if args[i:i + 2] == ['--tmpfs', '/home/alice'])
        runtime_mount = next(i for i in range(home_mask + 1, len(args) - 2)
                             if args[i:i + 3] == ['--ro-bind', '/home/alice/.local', '/home/alice/.local'])
        session_mount = next(i for i in range(runtime_mount + 1, len(args) - 2)
                             if args[i:i + 3] == ['--bind', str(session), str(session)])
        worktree_mount = next(i for i in range(session_mount + 1, len(args) - 2)
                              if args[i:i + 3] == ['--ro-bind', str(cwd), str(cwd)])
        self.assertLess(home_mask, runtime_mount)
        self.assertLess(runtime_mount, session_mount)
        self.assertLess(session_mount, worktree_mount)
        self.assertEqual(
            args[-5:], ['--chdir', str(cwd), '--', '/home/alice/.local/bin/claude', '--bare'])

    def test_known_credentials_are_masked_after_runtime_paths(self):
        cwd = Path('/home/alice/project')
        session = Path('/home/alice/.local/state/codex-deepseek/session-2')
        args = sandbox.wrap_command(
            ['/home/alice/.local/bin/codex'], cwd=cwd, session_home=session,
            env={'HOME': str(session), 'PATH': '/home/alice/.local/bin:/usr/bin'},
            backend=self.backend(), real_home=Path('/home/alice'),
            existing=lambda path: str(path) in {
                '/home/alice', '/home/alice/.local', '/home/alice/.local/share/keyrings',
                '/home/alice/.ssh', '/home/alice/.netrc', str(cwd), str(session),
            },
            is_dir=lambda path: str(path) != '/home/alice/.netrc')
        runtime_index = next(i for i in range(len(args) - 2)
                             if args[i:i + 3] == ['--ro-bind', '/home/alice/.local', '/home/alice/.local'])
        keyring_index = next(i for i in range(len(args) - 1)
                             if args[i:i + 2] == ['--tmpfs', '/home/alice/.local/share/keyrings'])
        ssh_index = next(i for i in range(len(args) - 1)
                         if args[i:i + 2] == ['--tmpfs', '/home/alice/.ssh'])
        netrc_index = next(i for i in range(len(args) - 2)
                           if args[i:i + 3] == ['--ro-bind', '/dev/null', '/home/alice/.netrc'])
        self.assertLess(runtime_index, keyring_index)
        self.assertLess(runtime_index, ssh_index)
        self.assertLess(runtime_index, netrc_index)

    def test_refuses_to_mount_entire_real_home_as_checkout(self):
        with self.assertRaises(sandbox.SandboxError) as caught:
            sandbox.wrap_command(['/usr/bin/claude'], cwd=Path('/home/alice'),
                                 session_home=Path('/tmp/session'), env={'HOME': '/tmp/session', 'PATH': '/usr/bin'},
                                 backend=self.backend(), real_home=Path('/home/alice'))
        self.assertEqual(caught.exception.code, 78)


class CodexWrapperTests(unittest.TestCase):
    def test_direct_backend_creates_private_bwrap_shim(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            backend = sandbox.SandboxBackend(('/usr/bin/bwrap',), '/usr/bin/bwrap', 'direct')
            env = sandbox.prepare_codex_environment(home, {'PATH': '/usr/bin:/bin'}, backend)
            wrapper = Path(env['PATH'].split(':', 1)[0]) / 'bwrap'
            self.assertTrue(wrapper.is_file())
            self.assertEqual(wrapper.stat().st_mode & 0o777, 0o700)
            self.assertEqual(wrapper.parent.stat().st_mode & 0o777, 0o700)
            text = wrapper.read_text()
            self.assertIn('/usr/bin/bwrap', text)
            self.assertIn('--disable-userns', text)

    def test_codex_shim_injects_disable_userns_once(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            record = root / 'args.txt'
            fake_bwrap = root / 'system-bwrap'
            fake_bwrap.write_text(
                '#!/bin/sh\nprintf "%s\\n" "$@" > ' + str(record) + '\n')
            fake_bwrap.chmod(0o700)
            home = root / 'session'
            home.mkdir()
            backend = sandbox.SandboxBackend((str(fake_bwrap),), str(fake_bwrap), 'direct')
            env = sandbox.prepare_codex_environment(home, {'PATH': '/usr/bin'}, backend)
            wrapper = Path(env['PATH'].split(':', 1)[0]) / 'bwrap'
            subprocess.run([str(wrapper), '--unshare-user', '--ro-bind', '/', '/', '/bin/true'], check=True)
            args = record.read_text().splitlines()
            self.assertEqual(args.count('--disable-userns'), 1)
            self.assertIn('--unshare-user', args)
            subprocess.run([str(wrapper), '--disable-userns', '--unshare-user', '/bin/true'], check=True)
            args = record.read_text().splitlines()
            self.assertEqual(args.count('--disable-userns'), 1)

    def test_apparmor_backend_shim_selects_named_profile_without_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            backend = sandbox.SandboxBackend(
                ('/usr/bin/aa-exec', '-p', sandbox.PROFILE_NAME, '--', '/usr/bin/bwrap'),
                '/usr/bin/bwrap', 'apparmor')
            env = sandbox.prepare_codex_environment(
                home, {'PATH': '/usr/bin', 'DEEPSEEK_API_KEY': 'DO_NOT_COPY_TO_SCRIPT'}, backend)
            wrapper = Path(env['PATH'].split(':', 1)[0]) / 'bwrap'
            text = wrapper.read_text()
            self.assertIn('aa-exec', text)
            self.assertIn(sandbox.PROFILE_NAME, text)
            self.assertIn('/usr/bin/bwrap', text)
            self.assertIn('--disable-userns', text)
            self.assertNotIn('DO_NOT_COPY_TO_SCRIPT', text)


class AppArmorLifecycleTests(unittest.TestCase):
    def test_profile_bytes_are_named_unconfined_userns_policy(self):
        text = sandbox.profile_bytes().decode()
        self.assertIn('profile deepseek-team-bwrap flags=(unconfined)', text)
        self.assertIn('userns,', text)
        self.assertNotIn('/usr/bin/bwrap', text)

    def test_install_refuses_foreign_existing_profile_without_commands(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / sandbox.PROFILE_NAME
            target.write_text('administrator policy\n')
            runner = RecordingRunner()
            with self.assertRaises(sandbox.SandboxError) as caught:
                sandbox.install_apparmor(use_sudo=False, runner=runner, target=target)
            self.assertEqual(caught.exception.code, 78)
            self.assertEqual(runner.calls, [])
            self.assertEqual(target.read_text(), 'administrator policy\n')

    def test_install_existing_exact_profile_only_reloads_it(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / sandbox.PROFILE_NAME
            target.write_bytes(sandbox.profile_bytes())
            runner = RecordingRunner()
            changed = sandbox.install_apparmor(use_sudo=False, runner=runner, target=target)
            self.assertFalse(changed)
            self.assertEqual(len(runner.calls), 1)
            self.assertIn('-r', runner.calls[0])
            self.assertEqual(runner.calls[0][-1], str(target))

    def test_install_absent_profile_uses_root_owned_0644_copy_then_parser(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / sandbox.PROFILE_NAME
            runner = RecordingRunner()
            changed = sandbox.install_apparmor(use_sudo=False, runner=runner, target=target)
            self.assertTrue(changed)
            self.assertEqual(len(runner.calls), 2)
            install, parser = runner.calls
            self.assertEqual(install[0], 'install')
            self.assertIn('0644', install)
            self.assertEqual(install[-1], str(target))
            self.assertIn('-r', parser)
            self.assertEqual(parser[-1], str(target))

    def test_remove_refuses_modified_or_symlink_profile(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / sandbox.PROFILE_NAME
            runner = RecordingRunner()
            target.write_text('modified')
            with self.assertRaises(sandbox.SandboxError):
                sandbox.remove_apparmor(use_sudo=False, runner=runner, target=target)
            self.assertEqual(runner.calls, [])
            target.unlink()
            foreign = root / 'foreign'
            foreign.write_bytes(sandbox.profile_bytes())
            target.symlink_to(foreign)
            with self.assertRaises(sandbox.SandboxError):
                sandbox.remove_apparmor(use_sudo=False, runner=runner, target=target)
            self.assertEqual(runner.calls, [])

    def test_remove_exact_profile_unloads_before_deleting(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / sandbox.PROFILE_NAME
            target.write_bytes(sandbox.profile_bytes())
            runner = RecordingRunner()
            removed = sandbox.remove_apparmor(use_sudo=False, runner=runner, target=target)
            self.assertTrue(removed)
            self.assertEqual(len(runner.calls), 2)
            self.assertIn('-R', runner.calls[0])
            self.assertEqual(runner.calls[0][-1], str(target))
            self.assertEqual(runner.calls[1][:3], ['rm', '-f', '--'])
            self.assertEqual(runner.calls[1][-1], str(target))


if __name__ == '__main__':
    unittest.main()
