"""Provider credential location: HOME default, explicit override, fail-closed checks."""
import contextlib
import io
import os
from pathlib import Path
import secrets
import stat
import tempfile
import unittest
from unittest import mock

from codex_deepseek_team import cli, config, managed, worker


class CredentialLocationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.home = self.root / 'home'
        self.home.mkdir(mode=0o700)
        self.override = self.root / 'service/api-key'
        self.key = secrets.token_hex(24)
        self.stored = secrets.token_hex(24)
        environment = mock.patch.dict(os.environ, {'HOME': str(self.home)}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)

    def override_environment(self, path=None):
        return mock.patch.dict(os.environ, {
            config.CREDENTIAL_VARIABLE: str(self.override if path is None else path)})

    def private_directory(self, path):
        path.mkdir(mode=0o700, parents=True)
        os.chmod(path, 0o700)
        return path

    def status(self):
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = cli.main(['auth', 'status'])
        return code, output.getvalue() + errors.getvalue()

    def assert_no_key_material(self, text):
        self.assertNotIn(self.key, text)
        self.assertNotIn(self.stored, text)

    def test_default_location_roundtrip_is_unchanged(self):
        with mock.patch.dict(os.environ, {config.CREDENTIAL_VARIABLE: ''}):
            config.save_key(self.key)
            path = self.home / '.config/codex-deepseek/api-key'
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
            self.assertEqual(config.credential_location().path, path)
            self.assertEqual(worker.load_api_key(), self.key)
            self.assertTrue(config.delete_key())
            self.assertFalse(path.exists())
            self.assertEqual(worker.load_api_key(), '')

    def test_override_roundtrip_stores_private_directory_and_file(self):
        with self.override_environment():
            config.save_key(self.key)
            self.assertEqual(stat.S_IMODE(self.override.parent.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(self.override.stat().st_mode), 0o600)
            self.assertFalse((self.home / '.config').exists())
            self.assertEqual(worker.load_api_key(), self.key)
            self.assertTrue(config.delete_key())
            self.assertFalse(self.override.exists())
            self.assertEqual(worker.load_api_key(), '')

    def test_environment_variable_takes_precedence_over_the_override_file(self):
        with self.override_environment():
            config.save_key(self.stored)
            with mock.patch.dict(os.environ, {'DEEPSEEK_API_KEY': self.key}):
                self.assertEqual(worker.load_api_key(), self.key)
            with mock.patch.dict(os.environ, {'DEEPSEEK_API_KEY': 'not a valid value'}):
                with self.assertRaises(worker.WorkerError) as raised:
                    worker.load_api_key()
            self.assertEqual(raised.exception.code, 78)
            self.assert_no_key_material(raised.exception.message)

    def test_unsafe_override_directory_is_rejected_without_using_the_home_key(self):
        config.save_key(self.key)
        unsafe = self.root / 'shared'
        unsafe.mkdir()
        os.chmod(unsafe, 0o755)
        override = unsafe / 'api-key'
        override.write_text(self.stored)
        os.chmod(override, 0o600)
        with self.override_environment(override):
            with self.assertRaises(worker.WorkerError) as raised:
                worker.load_api_key()
            self.assertEqual(raised.exception.code, 78)
            self.assertIn(str(unsafe), raised.exception.message)
            self.assert_no_key_material(raised.exception.message)
            with self.assertRaises(config.ConfigError) as saved:
                config.save_key(self.key)
            self.assertIn(str(unsafe), str(saved.exception))
            with self.assertRaises(config.ConfigError):
                config.delete_key()
        self.assertEqual((self.home / '.config/codex-deepseek/api-key').read_text(), self.key)

    def test_unsafe_override_file_is_rejected_without_using_the_home_key(self):
        config.save_key(self.key)
        directory = self.private_directory(self.root / 'service')
        self.override.write_text(self.stored)
        os.chmod(self.override, 0o644)
        with self.override_environment():
            with self.assertRaises(worker.WorkerError) as raised:
                worker.load_api_key()
            self.assertEqual(raised.exception.code, 78)
            self.assertIn(str(self.override), raised.exception.message)
            self.assert_no_key_material(raised.exception.message)
            with self.assertRaises(config.ConfigError):
                config.delete_key()
        self.assertEqual((self.home / '.config/codex-deepseek/api-key').read_text(), self.key)
        self.assertEqual(directory, self.override.parent)

    def test_symlinked_override_directory_or_file_is_rejected(self):
        target = self.private_directory(self.root / 'target')
        (target / 'api-key').write_text(self.stored)
        os.chmod(target / 'api-key', 0o600)
        link = self.root / 'linked'
        link.symlink_to(target, target_is_directory=True)
        directory = self.private_directory(self.root / 'service')
        self.override.symlink_to(target / 'api-key')
        for path in (link / 'api-key', self.override):
            with self.subTest(path=str(path)):
                with self.override_environment(path):
                    with self.assertRaises(worker.WorkerError) as raised:
                        worker.load_api_key()
                self.assertEqual(raised.exception.code, 78)
                self.assert_no_key_material(raised.exception.message)
        self.assertTrue((target / 'api-key').exists())
        self.assertTrue(directory.exists())

    def test_relative_or_traversing_override_is_rejected(self):
        for value in ('relative/api-key', './api-key',
                      str(self.root / 'service' / '..' / 'api-key')):
            with self.subTest(value=value):
                with mock.patch.dict(os.environ, {config.CREDENTIAL_VARIABLE: value}):
                    with self.assertRaises(worker.WorkerError) as raised:
                        worker.load_api_key()
                    self.assertEqual(raised.exception.code, 78)
                    self.assertIn(config.CREDENTIAL_VARIABLE, raised.exception.message)
                    self.assert_no_key_material(raised.exception.message)
                    with self.assertRaises(config.ConfigError):
                        config.save_key(self.key)
                    with self.assertRaises(config.ConfigError):
                        config.delete_key()
        self.assertFalse((self.home / '.config').exists())
        self.assertFalse((self.root / 'service').exists())

    def test_missing_override_file_reads_as_absent_without_home_fallback(self):
        config.save_key(self.key)
        self.private_directory(self.root / 'service')
        with self.override_environment():
            self.assertEqual(worker.load_api_key(), '')
            self.assertFalse(config.delete_key())
        with self.override_environment(self.root / 'absent/nested/api-key'):
            self.assertEqual(worker.load_api_key(), '')
            self.assertFalse(config.delete_key())
        self.assertEqual((self.home / '.config/codex-deepseek/api-key').read_text(), self.key)

    def test_invalid_override_content_is_rejected_without_echoing_it(self):
        self.private_directory(self.root / 'service')
        for raw in (b'x' * 4097, 'non-ascii é'.encode(), b'two words', b'two\nlines', b''):
            with self.subTest(raw=raw[:12]):
                self.override.write_bytes(raw)
                os.chmod(self.override, 0o600)
                with self.override_environment():
                    with self.assertRaises(worker.WorkerError) as raised:
                        worker.load_api_key()
                self.assertEqual(raised.exception.code, 78)
                self.assertIn(str(self.override), raised.exception.message)
                if raw:
                    self.assertNotIn(raw.decode('utf-8', errors='replace'),
                                     raised.exception.message)

    def test_status_reports_searched_default_location_and_effective_home(self):
        code, text = self.status()
        self.assertEqual(code, 78)
        self.assertIn(str(self.home / '.config/codex-deepseek/api-key'), text)
        self.assertIn(str(self.home), text)
        self.assert_no_key_material(text)

    def test_status_reports_the_override_file_as_the_source(self):
        with self.override_environment():
            config.save_key(self.key)
            code, text = self.status()
        self.assertEqual(code, 0)
        self.assertIn(str(self.override), text)
        self.assertIn(config.CREDENTIAL_VARIABLE, text)
        self.assert_no_key_material(text)

    def test_status_reports_the_environment_variable_as_the_source(self):
        with mock.patch.dict(os.environ, {'DEEPSEEK_API_KEY': self.key}):
            code, text = self.status()
        self.assertEqual(code, 0)
        self.assertIn('DEEPSEEK_API_KEY', text)
        self.assert_no_key_material(text)

    def test_status_reports_the_override_location_when_no_key_exists(self):
        with self.override_environment():
            code, text = self.status()
        self.assertEqual(code, 78)
        self.assertIn(str(self.override), text)
        self.assertIn(config.CREDENTIAL_VARIABLE, text)
        self.assertIn(str(self.home), text)
        self.assert_no_key_material(text)

    def test_absent_credential_errors_name_searched_locations_not_key_material(self):
        with self.override_environment():
            with self.assertRaises(worker.WorkerError) as raised:
                worker.require_api_key()
            launch = raised.exception.message
            preparation = managed._credential_absent_message(worker)
        for message in (launch, preparation):
            self.assertIn(str(self.override), message)
            self.assertIn(str(self.home), message)
            self.assert_no_key_material(message)

    def test_child_environment_never_forwards_the_override_variable(self):
        with self.override_environment():
            config.save_key(self.key)
            for runtime in ('codex', 'qwen', 'claude'):
                with self.subTest(runtime=runtime):
                    env = worker.child_environment(Path('/tmp/runtime-home'), self.key, runtime)
                    self.assertNotIn(config.CREDENTIAL_VARIABLE, env)
                    self.assertNotIn(str(self.override), repr(env))


if __name__ == '__main__':
    unittest.main()
