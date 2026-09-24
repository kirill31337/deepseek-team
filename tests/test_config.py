import json
import os
from pathlib import Path
import re
import secrets
import stat
import tempfile
import tomllib
import unittest
from unittest import mock

from codex_deepseek_team import config


CODEX_HOOK = 'deepseek-team coordinator-hook'
# The matcher shipped before source-inspection delivery existed; installing over it
# must repair the managed entry without touching another owner's hooks.
LEGACY_PRETOOLUSE_MATCHER = (
    'Bash|apply_patch|Edit|Write|(?:.*[./])?(?:spawn_agent|send_message|send_input|'
    'followup_task|assign_agent_task|resume_agent|Agent|Task)'
)


def owned_codex_handlers(data, event):
    return [handler for group in data.get('hooks', {}).get(event, [])
            if isinstance(group, dict)
            for handler in group.get('hooks', [])
            if isinstance(handler, dict) and handler.get('command') == CODEX_HOOK]


def owned_codex_groups(data, event):
    return [group for group in data.get('hooks', {}).get(event, [])
            if isinstance(group, dict)
            and any(isinstance(handler, dict) and handler.get('command') == CODEX_HOOK
                    for handler in group.get('hooks', []))]


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

    def test_install_repairs_legacy_managed_matcher_and_preserves_unrelated_hooks(self):
        path = self.home / 'hooks.json'
        legacy_group = {
            'matcher': LEGACY_PRETOOLUSE_MATCHER,
            'hooks': [
                {'type': 'command', 'command': CODEX_HOOK, 'timeout': 10,
                 'statusMessage': 'DeepSeek Team coordination policy'},
                {'type': 'command', 'command': 'user-guard', 'timeout': 30},
            ],
        }
        path.write_text(json.dumps({'hooks': {
            'PreToolUse': [legacy_group, {'hooks': [
                {'type': 'command', 'command': 'user-global'}]}],
            'Stop': [{'hooks': [{'type': 'command', 'command': 'user-stop'}]}],
            'PostToolUse': [{'hooks': [{'type': 'command', 'command': 'user-post'}]}],
        }}, indent=2) + '\n')

        self.assertTrue(config.install_codex_hooks(self.home))
        installed = json.loads(path.read_text())
        for event in config.CODEX_HOOK_EVENTS:
            with self.subTest(event=event):
                self.assertEqual(len(owned_codex_handlers(installed, event)), 1)
        pretooluse = owned_codex_groups(installed, 'PreToolUse')
        self.assertEqual(len(pretooluse), 1)
        self.assertEqual(pretooluse[0]['matcher'],
                         config.CODEX_HOOK_EVENTS['PreToolUse']['matcher'])
        # The shared group still belongs to the other owner: its matcher and handler
        # are byte-identical to the pre-existing definition, so no unrelated hook
        # and no native trust decision is silently rewritten.
        shared = [group for group in installed['hooks']['PreToolUse']
                  if {'type': 'command', 'command': 'user-guard', 'timeout': 30}
                  in group.get('hooks', [])]
        self.assertEqual(len(shared), 1)
        self.assertEqual(shared[0], {'matcher': LEGACY_PRETOOLUSE_MATCHER,
                                     'hooks': [{'type': 'command', 'command': 'user-guard',
                                                'timeout': 30}]})
        self.assertNotIn(CODEX_HOOK, json.dumps(shared))
        self.assertIn({'type': 'command', 'command': 'user-global'},
                      installed['hooks']['PreToolUse'][1]['hooks'])
        self.assertIn({'type': 'command', 'command': 'user-stop'},
                      installed['hooks']['Stop'][0]['hooks'])
        self.assertIn('PostToolUse', installed['hooks'])
        self.assertTrue(config.codex_hooks_status(self.home))

        self.assertFalse(config.install_codex_hooks(self.home))
        repaired = path.read_bytes()
        self.assertFalse(config.install_codex_hooks(self.home))
        self.assertEqual(path.read_bytes(), repaired)

    def test_stale_managed_matcher_is_reported_and_reinstall_repairs_it(self):
        config.install_codex_hooks(self.home)
        path = self.home / 'hooks.json'
        stale = json.loads(path.read_text())
        for group in owned_codex_groups(stale, 'PreToolUse'):
            group['matcher'] = LEGACY_PRETOOLUSE_MATCHER
        path.write_text(json.dumps(stale, indent=2, sort_keys=True) + '\n')

        self.assertFalse(config.codex_hooks_status(self.home))
        self.assertTrue(config.install_codex_hooks(self.home))
        self.assertTrue(config.codex_hooks_status(self.home))
        repaired = json.loads(path.read_text())
        self.assertEqual(owned_codex_groups(repaired, 'PreToolUse')[0]['matcher'],
                         config.CODEX_HOOK_EVENTS['PreToolUse']['matcher'])

    def test_stale_managed_matcher_still_removes_cleanly(self):
        config.install_codex_hooks(self.home)
        path = self.home / 'hooks.json'
        stale = json.loads(path.read_text())
        stale['hooks']['PreToolUse'][0]['matcher'] = LEGACY_PRETOOLUSE_MATCHER
        stale['hooks'].setdefault('SessionStart', []).append(
            {'hooks': [{'type': 'command', 'command': 'user-session'}]})
        path.write_text(json.dumps(stale, indent=2, sort_keys=True) + '\n')

        self.assertTrue(config.remove_codex_hooks(self.home))
        remaining = path.read_text()
        self.assertNotIn(CODEX_HOOK, remaining)
        self.assertIn('user-session', remaining)

    def test_installed_matcher_delivers_inspection_mutation_and_dispatch_events(self):
        self.assertTrue(config.install_codex_hooks(self.home))
        installed = json.loads((self.home / 'hooks.json').read_text())
        matcher = owned_codex_groups(installed, 'PreToolUse')[0]['matcher']
        for name in ('Read', 'Grep', 'Glob', 'Bash', 'exec_command', 'shell_command',
                     'functions.exec_command', 'functions.shell_command', 'functions.Read',
                     'apply_patch', 'Edit', 'Write', 'spawn_agent',
                     'collaboration.spawn_agent'):
            with self.subTest(name=name):
                self.assertIsNotNone(re.fullmatch(matcher, name), name)


class CodexHookMatcherTests(unittest.TestCase):
    """The installed Codex matcher decides which tool events the parent hook sees."""

    def matcher(self):
        return config.CODEX_HOOK_EVENTS['PreToolUse']['matcher']

    def matches(self, name):
        self.assertIsNotNone(re.fullmatch(self.matcher(), name), name)

    def test_delivers_source_inspection_and_shell_events(self):
        for name in ('Read', 'Grep', 'Glob', 'Bash', 'exec_command', 'shell_command'):
            with self.subTest(name=name):
                self.matches(name)

    def test_delivers_qualified_source_inspection_and_shell_names(self):
        for name in ('functions.Read', 'functions.Grep', 'functions.Glob',
                     'functions.exec_command', 'functions.shell_command',
                     'functions.Bash', 'shell.exec_command', 'codex/exec_command',
                     'tools.Grep'):
            with self.subTest(name=name):
                self.matches(name)

    def test_retains_mutation_and_native_dispatch_events(self):
        for name in ('apply_patch', 'Edit', 'Write', 'functions.apply_patch',
                     'spawn_agent', 'collaboration.spawn_agent', 'send_message',
                     'send_input', 'followup_task', 'assign_agent_task', 'resume_agent',
                     'Agent', 'Task'):
            with self.subTest(name=name):
                self.matches(name)

    def test_ignores_control_and_unrelated_tools(self):
        for name in ('wait_agent', 'view_image', 'request_user_input', 'write_stdin',
                     'web_search', 'multi_agent_v1'):
            with self.subTest(name=name):
                self.assertIsNone(re.fullmatch(self.matcher(), name), name)
