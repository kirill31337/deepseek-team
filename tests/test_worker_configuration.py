"""Concurrency configuration preserves access and takes effect on new jobs."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from codex_deepseek_team import activation, delegation_cli, settings, worker


class WorkerConfigurationTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.root = self.base / 'repo'
        self.root.mkdir()
        subprocess.run(['git', 'init', '-q', str(self.root)], check=True)
        env = patch.dict(os.environ, XDG_CONFIG_HOME=str(self.base / 'config'),
                         DEEPSEEK_TEAM_STATE_DIR=str(self.base / 'state'))
        env.start()
        self.addCleanup(env.stop)

    def test_fresh_policy_exposes_eight_workers_without_granting_write_access(self):
        policy = settings.resolve(self.root)
        self.assertEqual(policy.max_workers, 8)
        self.assertEqual(policy.as_dict()['max_workers'], 8)
        self.assertEqual(policy.effective_access, 'read-only')

    def test_concurrency_precedence_and_partial_updates_preserve_other_fields(self):
        settings.set_values(settings.global_file(), max_workers=12)
        self.assertEqual(settings.resolve(self.root).max_workers, 12)
        target = self.root / settings.PROJECT_FILE
        settings.set_values(target, max_workers=4, access='read-only', effort='high')
        settings.set_values(target, delegation_level=75)
        saved = settings.resolve(self.root)
        self.assertEqual((saved.max_workers, saved.effective_access, saved.effort), (4, 'read-only', 'high'))
        self.assertEqual(settings.resolve(self.root, max_workers=16).max_workers, 16)
        self.assertEqual(settings.resolve(self.root).sources['max_workers'], 'project:' + str(target))

    def test_invalid_limits_cannot_overwrite_saved_policy(self):
        target = self.root / settings.PROJECT_FILE
        settings.set_values(target, max_workers=5)
        before = target.read_bytes()
        for value in (0, -1, 65, True, False, 2.5, '8'):
            with self.subTest(value=value), self.assertRaises(settings.SettingsError):
                settings.set_values(target, max_workers=value)
            self.assertEqual(target.read_bytes(), before)

    def test_public_config_and_worker_options_accept_explicit_capacity(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = delegation_cli.main(['config', 'set', '--project', '--path', str(self.root),
                                        '--max-workers', '16'])
        self.assertEqual(code, 0)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = delegation_cli.main(['config', 'show', '--path', str(self.root), '--json'])
        self.assertEqual(json.loads(out.getvalue())['max_workers'], 16)
        with patch('sys.argv', ['worker', '--max-workers', '6', '--no-wait', 'task']):
            args = worker.parse_args()
        self.assertEqual(args.max_workers, 6)
        self.assertTrue(args.no_wait)

    def test_queued_job_rechecks_disabled_and_revoked_access_before_acquisition(self):
        for change in ('disabled', 'revoked'):
            with self.subTest(change=change):
                activation.set_enabled(self.root, True)
                settings.set_values(self.root / settings.PROJECT_FILE, access='full-access')
                policy = settings.resolve(self.root)
                args = SimpleNamespace(state_dir=self.base / 'state', timeout=0,
                                       no_wait=False, max_workers=1, coord_task=None)
                def waiting(state, **options):
                    if change == 'disabled':
                        activation.set_enabled(self.root, False)
                    else:
                        settings.set_values(self.root / settings.PROJECT_FILE, access='read-only')
                    options['validate']()
                    self.fail('stale queued work acquired a slot')
                with patch.object(worker, 'acquire_slot', side_effect=waiting):
                    with self.assertRaises(worker.WorkerError):
                        worker.acquire_job_slot(args, policy=policy, root=self.root)

    def test_waiting_job_detects_changed_source_head(self):
        def commit(message):
            subprocess.run(['git', '-C', str(self.root), '-c', 'user.name=Tests',
                            '-c', 'user.email=tests@example.invalid', 'commit', '-qm',
                            message, '--allow-empty'], check=True)
        commit('before')
        args = SimpleNamespace(state_dir=self.base / 'state', timeout=0,
                               no_wait=False, max_workers=1, coord_task=None)
        def waiting(state, **options):
            commit('after')
            options['validate']()
            self.fail('stale source acquired a slot')
        with patch.object(worker, 'acquire_slot', side_effect=waiting):
            with self.assertRaisesRegex(worker.WorkerError, 'HEAD'):
                worker.acquire_job_slot(args, policy=settings.resolve(self.root), root=self.root)

    def test_readonly_runner_does_not_read_key_before_a_slot_is_available(self):
        args = SimpleNamespace(runtime='codex', codex='codex', claude='claude',
            os_sandbox='required', task='review', state_dir=self.base / 'state',
            timeout=0, max_workers=1, no_wait=False, effort='low')
        with patch.object(worker, 'resolve_runtime', return_value=('codex', 'codex')), \
             patch.object(worker, 'resolve_os_sandbox', return_value=(None, None)), \
             patch.object(worker, 'provider_config'), \
             patch.object(worker, 'acquire_slot', side_effect=worker.WorkerError(75, 'queued')), \
             patch.object(worker, 'load_api_key', side_effect=AssertionError('credential read before slot')):
            with self.assertRaises(worker.WorkerError):
                worker.run_worker(args)


if __name__ == '__main__':
    unittest.main()
