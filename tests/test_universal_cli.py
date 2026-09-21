"""User-facing universal coordinator/runtime behavior."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import tomllib
import unittest


class UniversalCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / 'home'
        self.home.mkdir()
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        self.env = dict(os.environ, HOME=str(self.home), CODEX_HOME=str(self.home / 'codex'),
                        PYTHONPATH=str(Path(__file__).resolve().parents[1] / 'src'))
        self.env.pop('DEEPSEEK_API_KEY', None)
        self.env.pop('DEEPSEEK_TEAM_DISABLED', None)
        self.env.pop('CODEX_DEEPSEEK_DISABLED', None)
        codex = self.bin / 'codex'
        codex.write_text('#!/bin/sh\ncase "$1" in\n--version) echo codex-cli-test;;\nexec) echo "--strict-config --ephemeral --json --sandbox --ignore-rules";;\nesac\n')
        codex.chmod(0o755)
        self.claude = self.bin / 'claude'
        self.claude.write_text('#!/bin/sh\ncase "$1" in\n--version) echo claude-code-test;;\n--help) echo "--bare --print --output-format --no-session-persistence --permission-mode --tools --allowedTools --disallowedTools";;\nesac\n')
        self.claude.chmod(0o755)
        self.env['PATH'] = str(self.bin) + os.pathsep + self.env['PATH']

    def cli(self, *args, input=''):
        return subprocess.run([sys.executable, '-m', 'codex_deepseek_team', *args],
                              input=input, text=True, capture_output=True,
                              env=self.env, timeout=15)

    def git_repo(self):
        repo = self.root / 'repo'
        repo.mkdir()
        subprocess.run(['git', 'init', '-q', str(repo)], check=True)
        return repo

    def test_claude_only_setup_does_not_create_or_edit_codex_config(self):
        result = self.cli('setup', '--runtime', 'claude', '--no-key')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.home / 'codex/config.toml').exists())
        self.assertIn('Claude', result.stdout)

    def test_both_setup_preserves_primary_and_adds_only_codex_provider(self):
        codex_home = self.home / 'codex'
        codex_home.mkdir()
        config = codex_home / 'config.toml'
        config.write_text('model="primary"\nmodel_provider="openai"\n')
        result = self.cli('setup', '--runtime', 'both', '--no-key')
        self.assertEqual(result.returncode, 0, result.stderr)
        parsed = tomllib.loads(config.read_text())
        self.assertEqual(parsed['model'], 'primary')
        self.assertIn('deepseek', parsed['model_providers'])
        self.assertIn('Claude', result.stdout)

    def test_init_both_and_detach_both_use_native_instruction_files(self):
        repo = self.git_repo()
        result = self.cli('init', '--coordinator', 'both', str(repo))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('worker --runtime codex', (repo / 'AGENTS.md').read_text())
        self.assertIn('worker --runtime claude', (repo / 'CLAUDE.md').read_text())
        result = self.cli('detach', '--coordinator', 'both', str(repo))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((repo / 'AGENTS.md').exists())
        self.assertFalse((repo / 'CLAUDE.md').exists())

    def test_claude_offline_doctor_never_requires_codex_config(self):
        result = self.cli('doctor', '--runtime', 'claude', '--offline', '--os-sandbox', 'off')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('claude-code-test', result.stdout)
        self.assertFalse((self.home / 'codex/config.toml').exists())

    def test_claude_doctor_requires_disallowed_tools_support(self):
        self.claude.write_text('#!/bin/sh\ncase "$1" in\n--version) echo claude-code-old;;\n--help) echo "--bare --print --output-format --no-session-persistence --permission-mode --tools --allowedTools";;\nesac\n')
        self.claude.chmod(0o755)
        result = self.cli('doctor', '--runtime', 'claude', '--offline', '--os-sandbox', 'off')
        self.assertEqual(result.returncode, 78, result.stdout + result.stderr)
        self.assertIn('required', result.stderr.lower())

    def test_neutral_disable_switch_stops_before_runtime_or_credentials(self):
        env = dict(self.env, DEEPSEEK_TEAM_DISABLED='1')
        result = subprocess.run(
            [sys.executable, '-m', 'codex_deepseek_team', 'worker', '--runtime', 'claude'],
            input='task', text=True, capture_output=True, env=env, timeout=15)
        self.assertEqual(result.returncode, 69, result.stderr)
        self.assertIn('disabled', result.stderr.lower())


class PackagingTests(unittest.TestCase):
    def test_package_exposes_legacy_and_neutral_console_commands(self):
        root = Path(__file__).resolve().parents[1]
        data = tomllib.loads((root / 'pyproject.toml').read_text())
        scripts = data['project']['scripts']
        self.assertEqual(scripts['codex-deepseek-team'], 'codex_deepseek_team.cli:main')
        self.assertEqual(scripts['deepseek-team'], 'codex_deepseek_team.cli:main')
        self.assertEqual(data['project']['version'], '0.4.0')
        self.assertIn('data/apparmor/*', data['tool']['setuptools']['package-data']['codex_deepseek_team'])
        urls = data['project']['urls']
        self.assertEqual(urls['Repository'], 'https://github.com/kirill31337/deepseek-team')
        self.assertEqual(urls['Issues'], 'https://github.com/kirill31337/deepseek-team/issues')
        readme = (root / 'README.md').read_text()
        self.assertIn('git clone https://github.com/kirill31337/deepseek-team.git', readme)
        self.assertNotIn('github.com/kirill31337/codex-deepseek-team', readme)


if __name__ == '__main__':
    unittest.main()
