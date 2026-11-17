"""Qwen (DSH) CLI surface: hooks commands, refresh reports and diagnostics."""
import os
from pathlib import Path
import shlex
import sys
import tempfile
import unittest
from unittest import mock

from codex_deepseek_team import (cli, doctor, dsh_config, hook_refresh, lessons,
                                 routing_models, settings, upgrade_notices)


def write_launcher(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('#!/bin/sh\nexec ' + shlex.quote(sys.executable) +
                    ' -m codex_deepseek_team "$@"\n', encoding='utf-8')
    path.chmod(0o700)
    return path


class QwenCliTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='dst-qwen-cli-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        previous = os.getcwd()
        self.addCleanup(os.chdir, previous)
        os.chdir(self.root)
        self.bin = self.root / 'bin'
        self.entrypoint = write_launcher(self.bin / 'deepseek-team')
        self.home = self.root / 'home'
        self.home.mkdir()
        self.dsh = self.root / 'dsh-home'
        self.profile = self.dsh / 'profiles' / 'web'
        self.profile.mkdir(parents=True)
        (self.profile / 'package.json').write_text('{}')
        self.patch = self.profile / 'cordis.patch.yml'
        environment = mock.patch.dict(os.environ, {
            'HOME': str(self.home),
            'DSH_HOME': str(self.dsh),
            'DEEPSEEK_TEAM_DSH_PROFILE': '',
            'CODEX_HOME': str(self.root / 'codex-home'),
            'CLAUDE_CONFIG_DIR': str(self.root / 'claude'),
            'XDG_CONFIG_HOME': str(self.root / 'config'),
            'XDG_STATE_HOME': str(self.root / 'xdg-state'),
            'DEEPSEEK_TEAM_STATE_DIR': str(self.root / 'state'),
            'PATH': str(self.bin) + os.pathsep + '/usr/bin:/bin',
            'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'src'),
        })
        environment.start()
        self.addCleanup(environment.stop)

    def test_hooks_install_status_remove_lifecycle(self):
        self.assertEqual(cli.main(['hooks', 'install', '--runtime', 'qwen']), 0)
        self.assertTrue(dsh_config.owned())
        self.assertEqual(cli.main(['hooks', 'status', '--runtime', 'qwen']), 0)
        self.assertEqual(cli.main(['hooks', 'remove', '--runtime', 'qwen']), 0)
        self.assertFalse(dsh_config.owned())
        self.assertEqual(cli.main(['hooks', 'status', '--runtime', 'qwen']), 78)

    def test_reset_removes_only_owned_bridge(self):
        self.patch.write_text('- id: foreign\n  name: /opt/other.js\n')
        cli.main(['hooks', 'install', '--runtime', 'qwen'])
        self.assertEqual(cli.main(['reset', '--runtime', 'qwen']), 0)
        self.assertEqual(self.patch.read_text(), '- id: foreign\n  name: /opt/other.js\n')

    def test_refresh_reports_missing_then_current(self):
        report = hook_refresh.refresh_installed('qwen')
        self.assertEqual(report['hooks'][0]['status'], 'missing')
        self.assertEqual(report['required_actions'][0]['code'], 'install_hooks')
        self.assertEqual(hook_refresh._exit_code(report), hook_refresh.EXIT_ACTION_REQUIRED)
        dsh_config.install()
        report = hook_refresh.refresh_installed('qwen', notify=True)
        self.assertEqual(report['hooks'][0]['status'], 'current')
        self.assertEqual(report['required_actions'][0]['code'], 'review_native_hooks')
        self.assertIn('DSH', report['required_actions'][0]['message'])
        self.assertEqual(hook_refresh._exit_code(report), hook_refresh.EXIT_ACTION_REQUIRED)

    def test_refresh_auto_discovers_owned_qwen_only(self):
        report = hook_refresh.refresh_installed('auto')
        self.assertEqual(report['hooks'], [])
        dsh_config.install()
        report = hook_refresh.refresh_installed('auto')
        self.assertEqual([row['runtime'] for row in report['hooks']], ['qwen'])

    def test_refresh_all_and_both_selectors(self):
        self.assertEqual(hook_refresh._selected_runtimes('both'), ('codex', 'claude'))
        self.assertEqual(hook_refresh._selected_runtimes('all'), ('codex', 'claude', 'qwen'))
        self.assertEqual(hook_refresh._selected_runtimes('auto'), ('codex', 'claude', 'qwen'))

    def test_doctor_runtime_selection_and_status(self):
        self.assertEqual(doctor.selected_runtimes('qwen'), ('qwen',))
        self.assertEqual(doctor.selected_runtimes('all'), ('codex', 'claude', 'qwen'))
        installed, attached, trust = doctor.coordination_status('qwen', root=self.root)
        self.assertEqual(installed, 'missing-or-modified')
        self.assertEqual(attached, 'outside-project')
        self.assertIn('DSH profile', trust)
        dsh_config.install()
        installed, _attached, _trust = doctor.coordination_status('qwen', root=self.root)
        self.assertEqual(installed, 'installed')

    def test_runtime_enums_accept_qwen(self):
        self.assertIn('qwen', routing_models.FEATURE_VALUES['runtime'])
        self.assertIn('qwen', lessons.RUNTIME_VALUES)
        self.assertEqual(settings.worker_runtime('qwen'), 'qwen')
        self.assertEqual(settings.worker_runtime('claude'), 'claude')
        with self.assertRaises(settings.SettingsError):
            settings.instructions(settings.resolve(self.root), 'unknown')

    def test_upgrade_receipt_lives_in_dsh_home(self):
        self.assertEqual(upgrade_notices.receipt_path('qwen'),
                         self.dsh / upgrade_notices.RECEIPT_NAME)


if __name__ == '__main__':
    unittest.main()