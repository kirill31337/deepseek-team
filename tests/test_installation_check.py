"""Focused tests for the real-installer lifecycle checker."""
import contextlib
import importlib.util
import io
import os
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts' / 'check_installation.py'


def load_checker():
    spec = importlib.util.spec_from_file_location('installation_check', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class InstallationCheckTests(unittest.TestCase):
    def test_isolated_environment_keeps_tools_but_scrubs_credentials_and_overrides(self):
        checker = load_checker()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            inherited = {
                'PATH': '/usr/local/bin:/usr/bin',
                'LANG': 'C.UTF-8',
                'SSL_CERT_FILE': '/certs/ca.pem',
                'HOME': '/real/home',
                'PYTHONPATH': '/source/checkout',
                'DEEPSEEK_API_KEY': 'must-not-leak',
                'ANTHROPIC_API_KEY': 'must-not-leak',
                'ANTHROPIC_AUTH_TOKEN': 'must-not-leak',
                'OPENAI_API_KEY': 'must-not-leak',
                'AWS_ACCESS_KEY_ID': 'must-not-leak',
                'GITHUB_TOKEN': 'must-not-leak',
                'HTTPS_PROXY': 'https://user:secret@proxy.invalid',
                'DEEPSEEK_TEAM_STATE_DIR': '/real/state',
                'DEEPSEEK_TEAM_DISABLED': '1',
                'CODEX_HOME': '/real/codex',
                'CLAUDE_CONFIG_DIR': '/real/claude',
                'PIPX_HOME': '/real/pipx',
                'UV_TOOL_DIR': '/real/uv',
            }

            isolated = checker.isolated_environment(root, inherited)

            self.assertEqual(isolated['PATH'], inherited['PATH'])
            self.assertEqual(isolated['SSL_CERT_FILE'], inherited['SSL_CERT_FILE'])
            self.assertEqual(isolated['HOME'], str(root / 'home'))
            self.assertEqual(isolated['XDG_CONFIG_HOME'], str(root / 'xdg' / 'config'))
            self.assertEqual(isolated['CODEX_HOME'], str(root / 'codex'))
            self.assertEqual(isolated['CLAUDE_CONFIG_DIR'], str(root / 'claude'))
            self.assertEqual(isolated['PIPX_HOME'], str(root / 'pipx' / 'home'))
            self.assertEqual(isolated['UV_TOOL_DIR'], str(root / 'uv' / 'tools'))
            for name in (
                    'PYTHONPATH', 'DEEPSEEK_API_KEY', 'ANTHROPIC_API_KEY',
                    'ANTHROPIC_AUTH_TOKEN', 'OPENAI_API_KEY',
                    'AWS_ACCESS_KEY_ID', 'GITHUB_TOKEN', 'HTTPS_PROXY',
                    'DEEPSEEK_TEAM_STATE_DIR', 'DEEPSEEK_TEAM_DISABLED'):
                self.assertNotIn(name, isolated)

    def test_relative_wheel_is_resolved_to_an_absolute_existing_file(self):
        checker = load_checker()
        with tempfile.TemporaryDirectory() as temporary:
            wheel = Path(temporary) / 'package.whl'
            wheel.write_bytes(b'wheel fixture')
            relative = os.path.relpath(wheel, Path.cwd())

            resolved = checker.wheel_path(relative)

            self.assertEqual(resolved, wheel.resolve())
            self.assertTrue(resolved.is_absolute())

    def test_only_current_wheel_is_accepted(self):
        checker = load_checker()
        with tempfile.TemporaryDirectory() as temporary:
            wheel = Path(temporary) / 'package.whl'
            wheel.touch()
            with contextlib.redirect_stderr(io.StringIO()) as errors, \
                 self.assertRaises(SystemExit) as result:
                checker.parse_args(['--installer', 'pip', '--wheel', str(wheel),
                                    '--previous-wheel', str(wheel)])
            self.assertEqual(result.exception.code, 2)
            self.assertIn('unrecognized arguments: --previous-wheel', errors.getvalue())

    def test_external_state_sentinels_cover_credentials_configuration_and_history(self):
        checker = load_checker()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            env = checker.isolated_environment(root, {})
            sentinels = checker.create_sentinels(root, env)
            expected = [
                Path(env['HOME']) / '.config/codex-deepseek/api-key',
                Path(env['HOME']) / '.local/state/codex-deepseek/history.json',
                Path(env['CODEX_HOME']) / 'config.toml',
                Path(env['CODEX_HOME']) / 'auth.json',
                Path(env['CODEX_HOME']) / 'hooks.json',
                Path(env['CLAUDE_CONFIG_DIR']) / 'settings.json',
                Path(env['CLAUDE_CONFIG_DIR']) / '.credentials.json',
                root / 'project' / 'AGENTS.md',
                root / 'project' / '.deepseek-team.toml',
            ]
            for path in expected:
                self.assertIn(path, sentinels)
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(expected[0].parent.stat().st_mode & 0o777, 0o700)
            checker.assert_sentinels(sentinels, 'installation')
            expected[0].write_bytes(b'changed fixture')
            with self.assertRaisesRegex(checker.CheckError, 'sentinel changed'):
                checker.assert_sentinels(sentinels, 'replacement')
            expected[0].unlink()
            with self.assertRaisesRegex(checker.CheckError, 'sentinel was removed'):
                checker.assert_sentinels(sentinels, 'uninstall')


if __name__ == '__main__':
    unittest.main()
