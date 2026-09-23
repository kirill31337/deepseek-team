"""User-facing universal coordinator/runtime behavior."""
import contextlib
import io
import os
import shlex
from pathlib import Path
import subprocess
import sys
import tempfile
import tomllib
import unittest
from unittest import mock

from codex_deepseek_team import cli, sandbox


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
                        CLAUDE_CONFIG_DIR=str(self.home / '.claude'),
                        XDG_CONFIG_HOME=str(self.home / 'config'),
                        PYTHONPATH=str(Path(__file__).resolve().parents[1] / 'src'))
        self.env.pop('DEEPSEEK_API_KEY', None)
        self.env.pop('DEEPSEEK_TEAM_DISABLED', None)
        codex = self.bin / 'codex'
        codex.write_text('#!/bin/sh\ncase "$1" in\n--version) echo codex-cli-test;;\nexec) echo "--strict-config --ephemeral --json --sandbox --ignore-rules";;\nesac\n')
        codex.chmod(0o755)
        self.claude = self.bin / 'claude'
        self.claude.write_text('#!/bin/sh\ncase "$1" in\n--version) echo claude-code-test;;\n--help) echo "--bare --print --output-format --no-session-persistence --permission-mode --tools --allowedTools --disallowedTools";;\nesac\n')
        self.claude.chmod(0o755)
        entrypoint = self.bin / 'deepseek-team'
        entrypoint.write_text('#!/bin/sh\nexec ' + shlex.quote(sys.executable) +
                              ' -m codex_deepseek_team "$@"\n')
        entrypoint.chmod(0o755)
        self.env['PATH'] = str(self.bin) + os.pathsep + self.env['PATH']

    def cli(self, *args, input='', cwd=None):
        return subprocess.run([sys.executable, '-m', 'codex_deepseek_team', *args],
                              input=input, text=True, capture_output=True,
                              env=self.env, timeout=15, cwd=cwd or self.root)

    def local_cli(self, *args, cwd=None):
        output, errors = io.StringIO(), io.StringIO()
        backend = sandbox.SandboxBackend(('/test/bwrap',), '/test/bwrap', 'direct')
        with mock.patch.dict(os.environ, self.env, clear=True), \
             mock.patch.object(sandbox, 'probe_backend', return_value=backend) as probe, \
             contextlib.chdir(cwd or self.root), \
             contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = cli.main(list(args))
        probe.assert_called()
        return subprocess.CompletedProcess(args, code, output.getvalue(), errors.getvalue())

    def git_repo(self):
        repo = self.root / 'repo'
        repo.mkdir()
        subprocess.run(['git', 'init', '-q', str(repo)], check=True)
        return repo

    def test_claude_only_setup_does_not_create_or_edit_codex_config(self):
        result = self.local_cli('setup', '--runtime', 'claude', '--no-key')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.home / 'codex/config.toml').exists())
        self.assertIn('Claude', result.stdout)

    def test_both_setup_preserves_primary_and_adds_only_codex_provider(self):
        codex_home = self.home / 'codex'
        codex_home.mkdir()
        config = codex_home / 'config.toml'
        config.write_text('model="primary"\nmodel_provider="openai"\n')
        result = self.local_cli('setup', '--runtime', 'both', '--no-key')
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
        result = self.local_cli('doctor', '--runtime', 'claude', '--offline')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('claude-code-test', result.stdout)
        self.assertFalse((self.home / 'codex/config.toml').exists())

    def test_claude_doctor_reports_installed_hooks_and_project_binding(self):
        repo = self.git_repo()
        self.assertEqual(self.local_cli('setup', '--runtime', 'claude', '--no-key').returncode, 0)
        self.assertEqual(self.cli('init', '--coordinator', 'claude', str(repo)).returncode, 0)
        result = self.local_cli('doctor', '--runtime', 'claude', '--offline', cwd=repo)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Coordination hooks: installed', result.stdout)
        self.assertIn('Project coordination binding: attached', result.stdout)
        self.assertIn('Claude /hooks', result.stdout)

    def test_hooks_auto_and_both_manage_both_detected_runtimes(self):
        self.assertEqual(self.cli('hooks', 'install', '--runtime', 'auto').returncode, 0)
        self.assertEqual(self.cli('hooks', 'status', '--runtime', 'both').returncode, 0)
        self.assertEqual(self.cli('hooks', 'remove', '--runtime', 'both').returncode, 0)
        self.assertEqual(self.cli('hooks', 'status', '--runtime', 'both').returncode, 78)

    def test_claude_doctor_requires_disallowed_tools_support(self):
        self.claude.write_text('#!/bin/sh\ncase "$1" in\n--version) echo claude-code-old;;\n--help) echo "--bare --print --output-format --no-session-persistence --permission-mode --tools --allowedTools";;\nesac\n')
        self.claude.chmod(0o755)
        result = self.local_cli('doctor', '--runtime', 'claude', '--offline')
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
    def test_package_exposes_only_current_console_command(self):
        root = Path(__file__).resolve().parents[1]
        data = tomllib.loads((root / 'pyproject.toml').read_text())
        scripts = data['project']['scripts']
        self.assertEqual(scripts, {'deepseek-team': 'codex_deepseek_team.cli:main'})
        from codex_deepseek_team import __version__
        self.assertEqual(data['project']['version'], __version__)
        self.assertIn('data/apparmor/*', data['tool']['setuptools']['package-data']['codex_deepseek_team'])
        urls = data['project']['urls']
        self.assertEqual(urls['Repository'], 'https://github.com/kirill31337/deepseek-team')
        self.assertEqual(urls['Issues'], 'https://github.com/kirill31337/deepseek-team/issues')
        readme = (root / 'README.md').read_text()
        self.assertIn('git clone https://github.com/kirill31337/deepseek-team.git', readme)
        self.assertNotIn('github.com/kirill31337/codex-deepseek-team', readme)


if __name__ == '__main__':
    unittest.main()
