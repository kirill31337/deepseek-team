import os
from pathlib import Path
import secrets
import stat
import tempfile
import tomllib
import unittest
from unittest import mock

from codex_deepseek_team import config


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)

    def test_setup_preserves_primary_other_providers_and_auth_and_is_reversible(self):
        original = b'# custom settings\nmodel="my-primary-model"\nmodel_provider="custom"\n[model_providers.custom]\nname="Custom"\nbase_url="https://example.invalid"\n'
        path = self.home / 'config.toml'
        path.write_bytes(original)
        auth = self.home / 'auth.json'
        auth.write_bytes(b'{"synthetic":true}')
        self.assertTrue(config.configure(self.home))
        installed = path.read_bytes()
        self.assertTrue(installed.startswith(original))
        parsed = tomllib.loads(installed.decode())
        self.assertEqual(parsed['model'], 'my-primary-model')
        self.assertEqual(parsed['model_provider'], 'custom')
        self.assertIn('deepseek', parsed['model_providers'])
        self.assertFalse(config.configure(self.home))
        self.assertEqual(path.read_bytes(), installed)
        self.assertTrue(config.remove_provider(self.home))
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(auth.read_bytes(), b'{"synthetic":true}')

    def test_setup_handles_empty_home_and_does_not_claim_preexisting_provider(self):
        self.assertTrue(config.configure(self.home))
        path = self.home / 'config.toml'
        parsed = tomllib.loads(path.read_text())
        self.assertNotIn('model', parsed)
        path.write_text('[model_providers.deepseek]\nname="DeepSeek"\nbase_url="https://api.deepseek.com/"\nenv_key="DEEPSEEK_API_KEY"\nwire_api="responses"\n')
        before = path.read_bytes()
        self.assertFalse(config.configure(self.home))
        self.assertFalse(config.remove_provider(self.home))
        self.assertEqual(path.read_bytes(), before)

    def test_conflicting_provider_and_symlink_are_not_overwritten(self):
        path = self.home / 'config.toml'
        original = b'[model_providers.deepseek]\nbase_url="https://example.invalid"\n'
        path.write_bytes(original)
        with self.assertRaises(config.ConfigError):
            config.configure(self.home)
        self.assertEqual(path.read_bytes(), original)
        path.rename(self.home / 'other.toml')
        path.symlink_to(self.home / 'other.toml')
        with self.assertRaises(config.ConfigError):
            config.configure(self.home)
        self.assertEqual((self.home / 'other.toml').read_bytes(), original)

    def test_remove_refuses_user_modified_managed_block(self):
        config.configure(self.home)
        path = self.home / 'config.toml'
        path.write_text(path.read_text().replace('45000', '90000'))
        before = path.read_bytes()
        with self.assertRaises(config.ConfigError):
            config.remove_provider(self.home)
        self.assertEqual(path.read_bytes(), before)

    def test_key_storage_is_private_and_loadable_and_rejects_unsafe_directory(self):
        key = secrets.token_hex(24)
        with mock.patch.dict(os.environ, {'HOME': str(self.home)}, clear=True):
            config.save_key(key)
            path = self.home / '.config/codex-deepseek/api-key'
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
            self.assertEqual(config.worker.load_api_key(), key)
            self.assertTrue(config.delete_key())
            self.assertFalse(path.exists())
            path.parent.chmod(0o755)
            with self.assertRaises(config.ConfigError):
                config.save_key(key)

    def test_codex_hooks_install_is_user_level_stable_and_reversible(self):
        custom = {
            "hooks": {
                "SessionStart": [{"hooks": [{"type": "command", "command": "custom-hook"}]}]
            }
        }
        (self.home / "hooks.json").write_text(__import__("json").dumps(custom))
        self.assertTrue(config.install_codex_hooks(self.home))
        installed = __import__("json").loads((self.home / "hooks.json").read_text())
        text = (self.home / "hooks.json").read_text()
        self.assertIn("custom-hook", text)
        self.assertIn("deepseek-team coordinator-hook", text)
        self.assertIn("SessionStart", installed["hooks"])
        self.assertIn("UserPromptSubmit", installed["hooks"])
        self.assertIn("PreToolUse", installed["hooks"])
        self.assertIn("Stop", installed["hooks"])
        self.assertNotIn("PostToolUse", [
            key for key, groups in installed["hooks"].items()
            if any(h.get("command") == "deepseek-team coordinator-hook"
                   for g in groups for h in g.get("hooks", []) if isinstance(h, dict))
        ])
        before = (self.home / "hooks.json").read_bytes()
        self.assertFalse(config.install_codex_hooks(self.home))
        self.assertEqual((self.home / "hooks.json").read_bytes(), before)
        self.assertTrue(config.remove_codex_hooks(self.home))
        self.assertIn("custom-hook", (self.home / "hooks.json").read_text())
        self.assertNotIn("deepseek-team coordinator-hook", (self.home / "hooks.json").read_text())

    def test_invalid_key_does_not_replace_existing_key(self):
        key = secrets.token_hex(24)
        with mock.patch.dict(os.environ, {'HOME': str(self.home)}, clear=True):
            config.save_key(key)
            for invalid in ['', 'two lines\nsecret', 'with space', 'é', 'x' * 4097]:
                with self.subTest(value=invalid[:12]):
                    with self.assertRaises(config.ConfigError):
                        config.save_key(invalid)
                    self.assertEqual(config.worker.load_api_key(), key)

    def test_maximum_length_key_roundtrips(self):
        with mock.patch.dict(os.environ, {'HOME': str(self.home)}, clear=True):
            key = 'x' * 4096
            config.save_key(key)
            self.assertEqual(config.worker.load_api_key(), key)
