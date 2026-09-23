import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import tomllib
import unittest


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.env = dict(os.environ, HOME=str(self.root), CODEX_HOME=str(self.root / 'codex'),
                        PYTHONPATH=str(Path(__file__).resolve().parents[1] / 'src'))
        self.env.pop('DEEPSEEK_API_KEY', None)
        self.env.pop('CODEX_DEEPSEEK_DISABLED', None)
        binary = self.root / 'bin'
        binary.mkdir()
        codex = binary / 'codex'
        codex.write_text('#!/bin/sh\ncase "$1" in\n--version) echo codex-cli-test;;\nexec) echo "--strict-config --ephemeral --json --sandbox --ignore-rules";;\nesac\n')
        codex.chmod(0o755)
        self.env['PATH'] = str(binary) + os.pathsep + self.env['PATH']

    def cli(self, *args, input=''):
        return subprocess.run([sys.executable, '-m', 'codex_deepseek_team', *args],
                              input=input, text=True, capture_output=True, env=self.env, timeout=15, cwd=self.root)

    def test_setup_reset_and_offline_doctor_preserve_custom_primary(self):
        home = self.root / 'codex'
        home.mkdir()
        path = home / 'config.toml'
        original = b'model="another-coordinator"\nmodel_provider="openai"\n'
        path.write_bytes(original)
        r = self.cli('setup', '--configure-only', '--runtime', 'codex', '--no-key')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(tomllib.loads(path.read_text())['model'], 'another-coordinator')
        r = self.cli('doctor', '--offline', '--os-sandbox', 'off')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('another-coordinator', r.stdout)
        self.assertEqual(self.cli('reset').returncode, 0)
        self.assertEqual(path.read_bytes(), original)

    def test_auth_stdin_never_echoes_key_and_can_remove_it(self):
        key = secrets.token_hex(24)
        r = self.cli('auth', 'set', '--stdin', input=key + '\n')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn(key, r.stdout + r.stderr)
        self.assertEqual(self.cli('auth', 'status').returncode, 0)
        self.assertEqual(self.cli('auth', 'remove').returncode, 0)
        self.assertFalse((self.root / '.config/codex-deepseek/api-key').exists())

    def test_worker_subcommand_forwards_arguments_and_missing_key_failure(self):
        r = self.cli('worker', input='synthetic task')
        self.assertEqual(r.returncode, 78, r.stderr)
        r = self.cli('worker', '--write', input='task')
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn('unrecognized arguments: --write', r.stderr)
