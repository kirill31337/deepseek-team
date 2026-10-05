"""Variable-free recovery must persist once and preserve live/retained state."""
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from codex_deepseek_team import (coordination, coordinator_hooks, lessons, project,
                                 state_storage, worker, worker_admission, worker_slots)

SOURCE = Path(__file__).resolve().parents[1] / 'src'


class AutomaticStorageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='dst-auto-storage-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.home = self.root / 'home'; self.home.mkdir()
        self.state = self.home / '.local/state/codex-deepseek'
        self.selector = self.root / 'config/deepseek-team/storage/selection.json'
        patch = mock.patch.dict(os.environ, {
            'HOME': str(self.home), 'XDG_CONFIG_HOME': str(self.root / 'config'),
            'CODEX_HOME': str(self.root / 'codex'),
            'CLAUDE_CONFIG_DIR': str(self.root / 'claude'),
        })
        patch.start(); self.addCleanup(patch.stop)
        for name in ('DEEPSEEK_TEAM_STATE_DIR', 'DEEPSEEK_TEAM_DISABLED', 'CODEX_DEEPSEEK_DISABLED'):
            os.environ.pop(name, None)

    def broken_workspaces(self):
        self.state.mkdir(mode=0o700, parents=True)
        target = self.root / 'retained'; target.mkdir(mode=0o700)
        (target / 'data').write_text('keep old copies')
        (self.state / 'workspaces').symlink_to(target, target_is_directory=True)
        return target

    def process(self, code):
        env = dict(os.environ, PYTHONPATH=str(SOURCE))
        return subprocess.run([sys.executable, '-c', code], env=env,
                              capture_output=True, text=True, timeout=20)

    def test_bootstrap_recovers_link_and_future_commands_share_saved_root(self):
        target = self.broken_workspaces()
        repo = self.root / 'project'; repo.mkdir()
        subprocess.run(['git', 'init', '-q', str(repo)], check=True)
        subprocess.run(['git', '-C', str(repo), '-c', 'user.name=Fixture', '-c',
                        'user.email=fixture@example.invalid', 'commit', '--allow-empty',
                        '-qm', 'base'], check=True)
        project.attach(repo, coordinator='both')
        for runtime in ('codex', 'claude'):
            result = coordinator_hooks.handle(dict(hook_event_name='UserPromptSubmit',
                cwd=str(repo), session_id='automatic-' + runtime, turn_id='first',
                prompt='Inspect source'), runtime=runtime)
            self.assertIn('coordination task task-', result['hookSpecificOutput']['additionalContext'])
        selected = state_storage.state_root()
        self.assertNotEqual(selected, self.state)
        self.assertEqual(coordination._state_root(), selected)
        self.assertEqual(lessons._state_base(), selected)
        command = self.process('from codex_deepseek_team.state_storage import state_root; print(state_root())')
        self.assertEqual(command.returncode, 0, command.stderr)
        self.assertEqual(command.stdout.strip(), str(selected))
        self.assertTrue((self.state / 'workspaces').is_symlink())
        self.assertEqual((target / 'data').read_text(), 'keep old copies')
        self.assertEqual(selected.stat().st_mode & 0o777, 0o700)
        self.assertEqual((selected / 'workspaces').stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.selector.stat().st_mode & 0o777, 0o600)

    def test_healthy_default_needs_no_saved_replacement(self):
        self.assertEqual(state_storage.prepare_storage(), self.state)
        self.assertFalse(self.selector.exists())

    def test_explicit_paths_keep_priority_over_saved_selection(self):
        self.broken_workspaces(); selected = state_storage.prepare_storage()
        explicit = self.root / 'explicit'
        override = self.root / 'environment'
        os.environ['DEEPSEEK_TEAM_STATE_DIR'] = str(override)
        self.assertEqual(state_storage.prepare_storage(explicit), explicit)
        self.assertEqual(state_storage.prepare_storage(), override)
        os.environ.pop('DEEPSEEK_TEAM_STATE_DIR')
        self.assertEqual(state_storage.state_root(), selected)

    def test_explicit_broken_root_is_reported_without_automatic_switch(self):
        self.broken_workspaces()
        with self.assertRaises(state_storage.StorageError):
            state_storage.prepare_storage(self.state)
        self.assertFalse(self.selector.exists())

    def test_active_slot_blocks_switch_until_released(self):
        self.broken_workspaces()
        slot = worker_slots.acquire(self.state, wait=False)
        try:
            with self.assertRaises(state_storage.StorageError) as caught:
                state_storage.prepare_storage()
            self.assertIn('worker', str(caught.exception).lower())
            self.assertFalse(self.selector.exists())
        finally:
            os.close(slot)
        self.assertNotEqual(state_storage.prepare_storage(), self.state)

    def test_live_fifo_ticket_blocks_switch_and_is_preserved(self):
        self.broken_workspaces()
        ticket = self.state / 'ticket-00000000000000000042'
        fd = os.open(ticket, os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            with self.assertRaises(state_storage.StorageError):
                state_storage.prepare_storage()
            self.assertFalse(self.selector.exists())
            self.assertTrue(ticket.exists())
        finally:
            os.close(fd)
        self.assertNotEqual(state_storage.prepare_storage(), self.state)
        self.assertTrue(ticket.exists())

    def test_allocator_contention_blocks_switch(self):
        self.broken_workspaces()
        fd = os.open(self.state / 'allocator.lock', os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            with self.assertRaises(state_storage.StorageError):
                state_storage.prepare_storage()
            self.assertFalse(self.selector.exists())
        finally:
            os.close(fd)

    def test_public_owned_root_recovers_without_chmod(self):
        self.state.mkdir(mode=0o700, parents=True); self.state.chmod(0o755)
        self.assertNotEqual(state_storage.prepare_storage(), self.state)
        self.assertEqual(self.state.stat().st_mode & 0o777, 0o755)

    def test_linked_root_cannot_prove_idle_and_is_not_followed(self):
        self.state.parent.mkdir(parents=True)
        target = self.root / 'retained'; target.mkdir(mode=0o700)
        self.state.symlink_to(target, target_is_directory=True)
        with self.assertRaises(state_storage.StorageError):
            state_storage.prepare_storage()
        self.assertEqual(list(target.iterdir()), [])
        self.assertFalse(self.selector.exists())

    def test_rejected_lock_file_prevents_adopting_an_alternative(self):
        target = self.broken_workspaces()
        (self.state / 'worker-0.lock').symlink_to(target / 'data')
        with self.assertRaises(state_storage.StorageError):
            state_storage.prepare_storage()
        self.assertFalse(self.selector.exists())
        self.assertEqual((target / 'data').read_text(), 'keep old copies')

    def test_saved_selection_corruption_does_not_fall_back_or_overwrite(self):
        self.broken_workspaces(); state_storage.prepare_storage()
        self.selector.write_text('{bad json')
        with self.assertRaises(state_storage.StorageError):
            state_storage.prepare_storage()
        self.assertEqual(self.selector.read_text(), '{bad json')

    def test_saved_selection_symlink_is_rejected_without_reading_target(self):
        self.broken_workspaces(); state_storage.prepare_storage()
        self.selector.unlink()
        target = self.root / 'untrusted'; target.write_text('keep')
        self.selector.symlink_to(target)
        with self.assertRaises(state_storage.StorageError):
            state_storage.state_root()
        self.assertEqual(target.read_text(), 'keep')

    def test_concurrent_bootstraps_publish_one_shared_root(self):
        self.broken_workspaces()
        env = dict(os.environ, PYTHONPATH=str(SOURCE))
        code = 'from codex_deepseek_team.state_storage import prepare_storage; print(prepare_storage())'
        commands = [subprocess.Popen([sys.executable, '-c', code], env=env,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(2)]
        values = []
        for command in commands:
            out, err = command.communicate(timeout=20)
            self.assertEqual(command.returncode, 0, err)
            values.append(out.strip())
        self.assertEqual(values[0], values[1])
        self.assertNotEqual(values[0], str(self.state))

    def test_bootstrap_uses_choice_published_after_its_initial_read(self):
        self.broken_workspaces()
        original = state_storage._prepare_exact
        published = []
        def concurrent_publication(root):
            if root == self.state and not published:
                published.append(None)
                published[0] = state_storage.prepare_storage()
            return original(root)
        with mock.patch.object(state_storage, '_prepare_exact', side_effect=concurrent_publication):
            self.assertEqual(state_storage.prepare_storage(), published[0])

    def test_saved_selection_invalid_path_is_reported_without_traceback(self):
        self.broken_workspaces(); state_storage.prepare_storage()
        value = json.loads(self.selector.read_text())
        value['root'] = '/invalid\x00path'
        self.selector.write_text(json.dumps(value))
        with self.assertRaises(state_storage.StorageError):
            state_storage.prepare_storage()

    def test_saved_selection_hardlink_is_rejected(self):
        self.broken_workspaces(); state_storage.prepare_storage()
        os.link(self.selector, self.root / 'second-link')
        with self.assertRaises(state_storage.StorageError):
            state_storage.state_root()

    def test_saved_selection_public_permissions_are_rejected(self):
        self.broken_workspaces(); state_storage.prepare_storage()
        self.selector.chmod(0o644)
        with self.assertRaises(state_storage.StorageError):
            state_storage.state_root()
        self.assertEqual(self.selector.stat().st_mode & 0o777, 0o644)

    def test_worker_with_old_automatic_selection_is_rejected_before_admission(self):
        self.broken_workspaces()
        with mock.patch.object(sys, 'argv', ['deepseek-team', 'read source']):
            args = worker.parse_args()
        state_storage.prepare_storage()
        with self.assertRaises(worker.WorkerError) as caught:
            worker_admission.acquire(args, worker)
        self.assertIn('storage', str(caught.exception).lower())
        self.assertFalse(any(self.state.glob('ticket-*')))
        self.assertFalse(any(self.state.glob('worker-*.lock')))


if __name__ == '__main__':
    unittest.main()
