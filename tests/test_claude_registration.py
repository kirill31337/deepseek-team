"""Focused registration tests for the installed Claude PreToolUse matcher.

These exercise claude_config's install/status APIs against a temporary Claude
home only; no Claude CLI or provider is involved. The matcher must route the
tools DeepSeek Team now inspects (source reads, shells, mutations and native
delegation) while leaving unrelated lifecycle tools alone, and reinstall must
repair an obsolete matcher without disturbing foreign user settings.
"""
import json
import re
import tempfile
import unittest
from pathlib import Path

from codex_deepseek_team import claude_config


INSPECTION = ('Read', 'Grep', 'Glob')
SHELL = ('Bash', 'exec_command', 'shell_command')
MUTATION = ('Edit', 'Write', 'NotebookEdit', 'apply_patch')
NATIVE = ('spawn_agent', 'send_message', 'send_input', 'followup_task',
          'assign_agent_task', 'resume_agent', 'Agent', 'Task')
# Earlier releases only routed shells, explicit mutations and unqualified natives.
OBSOLETE_MATCHER = ('Bash|Edit|Write|NotebookEdit|(?:.*[./])?(?:spawn_agent|'
                    'send_message|send_input|followup_task|assign_agent_task|'
                    'resume_agent|Agent|Task)')


class ClaudeRegistrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='dst-claude-registration-')
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name) / 'home'
        self.home.mkdir()
        self.settings = self.home / 'settings.json'

    def installed_matcher(self):
        claude_config.install(self.home)
        data = json.loads(self.settings.read_text())
        groups = data['hooks']['PreToolUse']
        self.assertEqual([group.get('matcher') for group in groups],
                         [claude_config.HOOK_EVENTS['PreToolUse']])
        return groups[0]['matcher']

    def test_installed_matcher_routes_inspection_shell_mutation_and_native_tools(self):
        matcher = self.installed_matcher()
        for name in INSPECTION + SHELL + MUTATION + NATIVE:
            for spelling in (name, 'collaboration.' + name, 'mcp/tools/' + name):
                with self.subTest(name=spelling):
                    self.assertIsNotNone(re.fullmatch(matcher, spelling))

    def test_installed_matcher_ignores_unrelated_tool_names(self):
        matcher = self.installed_matcher()
        for name in ('Wait', 'Stop', 'SetModel', 'ReadFile', 'BashTool', 'SendMessage'):
            with self.subTest(name=name):
                self.assertIsNone(re.fullmatch(matcher, name))

    def test_install_is_idempotent_and_status_accepts_the_installed_matcher(self):
        self.assertTrue(claude_config.install(self.home))
        self.assertTrue(claude_config.status(self.home))
        first = self.settings.read_bytes()
        self.assertFalse(claude_config.install(self.home))
        self.assertEqual(self.settings.read_bytes(), first)
        self.assertTrue(claude_config.status(self.home))

    def test_install_repairs_an_obsolete_matcher(self):
        self.settings.write_text(json.dumps({'hooks': {'PreToolUse': [{
            'matcher': OBSOLETE_MATCHER,
            'hooks': [{'type': 'command', 'command': claude_config.HOOK_COMMAND,
                       'timeout': 10, 'statusMessage': 'DeepSeek Team coordination policy'}],
        }]}}))
        self.assertFalse(claude_config.status(self.home))
        self.assertTrue(claude_config.install(self.home))
        self.assertTrue(claude_config.status(self.home))
        data = json.loads(self.settings.read_text())
        for name in INSPECTION + SHELL + MUTATION + NATIVE:
            self.assertIsNotNone(re.fullmatch(data['hooks']['PreToolUse'][0]['matcher'], name))

    def test_install_preserves_foreign_hooks_settings_and_explicit_disable(self):
        self.settings.write_text(json.dumps({
            'model': 'user-model',
            'disableAllHooks': True,
            'hooks': {
                'PreToolUse': [{
                    'matcher': 'Read',
                    'hooks': [
                        {'type': 'command', 'command': 'my-hook'},
                        {'type': 'command', 'command': claude_config.HOOK_COMMAND},
                    ],
                }],
                'Stop': [{'hooks': [{'type': 'command', 'command': 'other-hook'}]}],
            },
        }))
        claude_config.install(self.home)
        data = json.loads(self.settings.read_text())
        self.assertEqual(data['model'], 'user-model')
        self.assertTrue(data['disableAllHooks'])
        commands = [handler['command'] for groups in data['hooks'].values()
                    for group in groups for handler in group['hooks']]
        self.assertIn('my-hook', commands)
        self.assertIn('other-hook', commands)
        self.assertEqual(commands.count(claude_config.HOOK_COMMAND),
                         len(claude_config.HOOK_EVENTS))
        # The stale owned handler shared a foreign group; install must not drop
        # the surviving foreign handler from that group.
        self.assertTrue(any('my-hook' in [handler['command'] for handler in group['hooks']]
                            for group in data['hooks']['PreToolUse']))
        # An explicit disable is preserved and status still reports not installed.
        self.assertFalse(claude_config.status(self.home))


if __name__ == '__main__':
    unittest.main()
