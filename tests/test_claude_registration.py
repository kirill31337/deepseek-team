"""Focused registration tests for the installed Claude hooks.

These exercise claude_config's install/status/remove APIs against a temporary
Claude home only; no Claude CLI or provider is involved. The matcher must route
the tools DeepSeek Team now inspects (source reads, shells, mutations and native
delegation) while leaving unrelated lifecycle tools alone. Installation must
resolve an absolute package entrypoint, migrate only package-owned legacy
definitions, preserve foreign settings and quote paths containing spaces.
"""
import json
import os
from pathlib import Path
import re
import shlex
import sys
import tempfile
import unittest
from unittest import mock

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
FOREIGN_COMMANDS = (
    'my-hook',
    claude_config.HOOK_COMMAND + ' --custom',
    '/opt/other-team coordinator-hook --runtime claude',
    'deepseek-team coordinator-hook --runtime codex',
    'python -m codex_deepseek_team coordinator-hook --runtime claude',
)
FOREIGN_SHELL_COMMANDS = (
    '/tmp/$(id)/deepseek-team coordinator-hook --runtime claude',
    '/tmp/foo;bar/deepseek-team coordinator-hook --runtime claude',
    '/tmp/a&b/deepseek-team coordinator-hook --runtime claude',
    '/tmp/`id`/deepseek-team coordinator-hook --runtime claude',
)


def write_launcher(path):
    """A legitimate in-fixture entrypoint that invokes this package."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('#!/bin/sh\nexec ' + shlex.quote(sys.executable) +
                    ' -m codex_deepseek_team "$@"\n', encoding='utf-8')
    path.chmod(0o700)
    return path


class ClaudeRegistrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='dst-claude-registration-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        previous = os.getcwd()
        self.addCleanup(os.chdir, previous)
        os.chdir(self.root)
        self.bin = self.root / 'entry point'
        self.entrypoint = write_launcher(self.bin / 'deepseek-team')
        self.home = self.root / 'home'
        self.home.mkdir()
        self.claude = self.root / 'claude'
        self.settings = self.claude / 'settings.json'
        environment = mock.patch.dict(os.environ, {
            'HOME': str(self.home),
            'CODEX_HOME': str(self.root / 'codex-home'),
            'CLAUDE_CONFIG_DIR': str(self.claude),
            'XDG_CONFIG_HOME': str(self.root / 'xdg-config'),
            'XDG_STATE_HOME': str(self.root / 'xdg-state'),
            'DEEPSEEK_TEAM_STATE_DIR': str(self.root / 'state'),
            'PATH': str(self.bin) + os.pathsep + '/usr/bin:/bin',
        })
        environment.start()
        self.addCleanup(environment.stop)

    def installed_matcher(self):
        claude_config.install(self.claude)
        data = json.loads(self.settings.read_text())
        groups = data['hooks']['PreToolUse']
        self.assertEqual([group.get('matcher') for group in groups],
                         [claude_config.HOOK_EVENTS['PreToolUse']])
        return groups[0]['matcher']

    def commands(self):
        data = json.loads(self.settings.read_text())
        return [handler['command'] for groups in data.get('hooks', {}).values()
                for group in groups for handler in group['hooks']]

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

    def test_install_is_idempotent_and_status_accepts_the_installed_definitions(self):
        self.assertTrue(claude_config.install(self.claude))
        self.assertTrue(claude_config.status(self.claude))
        first = self.settings.read_bytes()
        self.assertFalse(claude_config.install(self.claude))
        self.assertEqual(self.settings.read_bytes(), first)
        self.assertTrue(claude_config.status(self.claude))

    def test_install_migrates_legacy_and_absolute_definitions_with_an_absolute_quoted_entrypoint(self):
        self.claude.mkdir()
        legacy_group = {
            'matcher': 'Read',
            'hooks': [
                {'type': 'command', 'command': claude_config.HOOK_COMMAND},
                {'type': 'command',
                 'command': '/opt/old-venv/bin/deepseek-team coordinator-hook --runtime claude'},
                *({'type': 'command', 'command': command} for command in FOREIGN_COMMANDS),
            ],
        }
        self.settings.write_text(json.dumps({
            'model': 'user-model',
            'permissions': {'deny': ['Bash(rm *)']},
            'hooks': {'PreToolUse': [legacy_group]},
        }))
        auth = self.claude / '.credentials.json'
        auth.write_bytes(b'private authentication fixture')

        self.assertTrue(claude_config.install(self.claude))
        self.assertTrue(claude_config.status(self.claude))
        expected = claude_config.hook_command(self.entrypoint)
        self.assertEqual(expected, shlex.quote(str(self.entrypoint.resolve())) +
                         ' coordinator-hook --runtime claude')
        self.assertIn('entry point', expected)

        data = json.loads(self.settings.read_text())
        self.assertEqual(data['model'], 'user-model')
        self.assertEqual(data['permissions'], {'deny': ['Bash(rm *)']})
        self.assertEqual(auth.read_bytes(), b'private authentication fixture')
        for event, groups in data['hooks'].items():
            handlers = [handler for group in groups for handler in group['hooks']]
            owned = [handler for handler in handlers if handler.get('command') == expected]
            self.assertEqual(len(owned), 1, event)
            self.assertEqual(owned[0].get('onFailure'),
                             'block' if event == 'PreToolUse' else None)
        commands = self.commands()
        self.assertNotIn(claude_config.HOOK_COMMAND, commands)
        self.assertNotIn('/opt/old-venv/bin/deepseek-team coordinator-hook --runtime claude',
                         commands)
        for command in FOREIGN_COMMANDS:
            self.assertIn(command, commands)

        first = self.settings.read_bytes()
        self.assertFalse(claude_config.install(self.claude))
        self.assertEqual(self.settings.read_bytes(), first)

    def test_remove_preserves_truly_foreign_hooks_and_exact_disable(self):
        self.claude.mkdir()
        settings = {
            'model': 'preserve-me',
            'disableAllHooks': True,
            'hooks': {
                'PreToolUse': [{
                    'matcher': 'Read',
                    'hooks': [
                        {'type': 'command', 'command': claude_config.HOOK_COMMAND},
                        {'type': 'command', 'command': claude_config.hook_command(
                            self.entrypoint)},
                        *({'type': 'command', 'command': command}
                          for command in FOREIGN_COMMANDS),
                    ],
                }],
                'Stop': [{'hooks': [
                    {'type': 'command',
                     'command': '/opt/old-venv/bin/deepseek-team coordinator-hook --runtime claude'},
                    {'type': 'command', 'command': 'other-hook'},
                ]}],
            },
        }
        self.settings.write_text(json.dumps(settings))
        auth = self.claude / '.credentials.json'
        auth.write_bytes(b'private authentication fixture')

        self.assertTrue(claude_config.remove(self.claude))
        self.assertFalse(claude_config.remove(self.claude))
        data = json.loads(self.settings.read_text())
        self.assertEqual(data['model'], 'preserve-me')
        self.assertTrue(data['disableAllHooks'])
        self.assertEqual(auth.read_bytes(), b'private authentication fixture')
        commands = self.commands()
        self.assertNotIn(claude_config.HOOK_COMMAND, commands)
        self.assertNotIn(claude_config.hook_command(self.entrypoint), commands)
        for command in FOREIGN_COMMANDS:
            self.assertIn(command, commands)
        self.assertIn('other-hook', commands)

    def test_install_repairs_an_obsolete_matcher(self):
        self.settings.parent.mkdir(parents=True)
        self.settings.write_text(json.dumps({'hooks': {'PreToolUse': [{
            'matcher': OBSOLETE_MATCHER,
            'hooks': [{'type': 'command', 'command': claude_config.HOOK_COMMAND,
                       'timeout': 10, 'statusMessage': 'DeepSeek Team coordination policy'}],
        }]}}))
        self.assertFalse(claude_config.status(self.claude))
        self.assertTrue(claude_config.install(self.claude))
        self.assertTrue(claude_config.status(self.claude))
        data = json.loads(self.settings.read_text())
        for name in INSPECTION + SHELL + MUTATION + NATIVE:
            self.assertIsNotNone(re.fullmatch(data['hooks']['PreToolUse'][0]['matcher'], name))

    def test_status_requires_the_pretooluse_onfailure_block(self):
        self.assertTrue(claude_config.install(self.claude))
        data = json.loads(self.settings.read_text())
        handler = data['hooks']['PreToolUse'][0]['hooks'][0]
        self.assertEqual(handler['onFailure'], 'block')
        del handler['onFailure']
        self.settings.write_text(json.dumps(data))
        self.assertFalse(claude_config.status(self.claude))
        self.assertTrue(claude_config.install(self.claude))
        self.assertTrue(claude_config.status(self.claude))
        data = json.loads(self.settings.read_text())
        self.assertEqual(data['hooks']['PreToolUse'][0]['hooks'][0]['onFailure'], 'block')

    def test_install_fails_before_writing_when_entrypoint_cannot_be_resolved(self):
        self.claude.mkdir()
        original = b'{"model":"user-model","hooks":{"Stop":[]}}\n'
        self.settings.write_bytes(original)
        self.settings.chmod(0o640)
        auth = self.claude / '.credentials.json'
        auth.write_bytes(b'private authentication fixture')
        empty = self.root / 'empty-bin'
        empty.mkdir()
        with mock.patch.dict(os.environ, {'PATH': str(empty)}):
            with self.assertRaises(claude_config.ConfigError) as raised:
                claude_config.install(self.claude)
        message = str(raised.exception)
        self.assertIn('deepseek-team', message)
        self.assertIn('PATH', message)
        self.assertIn('no settings were changed', message.lower())
        self.assertEqual(self.settings.read_bytes(), original)
        self.assertEqual(self.settings.stat().st_mode & 0o777, 0o640)
        self.assertEqual(auth.read_bytes(), b'private authentication fixture')

    def test_install_preserves_foreign_hooks_settings_and_explicit_disable(self):
        self.claude.mkdir()
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
        claude_config.install(self.claude)
        data = json.loads(self.settings.read_text())
        self.assertEqual(data['model'], 'user-model')
        self.assertTrue(data['disableAllHooks'])
        commands = self.commands()
        self.assertIn('my-hook', commands)
        self.assertIn('other-hook', commands)
        self.assertEqual(commands.count(claude_config.hook_command(self.entrypoint)),
                         len(claude_config.HOOK_EVENTS))
        # The stale owned handler shared a foreign group; install must not drop
        # the surviving foreign handler from that group.
        self.assertTrue(any('my-hook' in [handler['command'] for handler in group['hooks']]
                            for group in data['hooks']['PreToolUse']))
        # An explicit disable is preserved and status still reports not installed.
        self.assertFalse(claude_config.status(self.claude))

    def test_status_recognizes_an_absolute_definition_without_path_and_detects_stale_paths(self):
        self.assertTrue(claude_config.install(self.claude))
        empty = self.root / 'empty-bin'
        empty.mkdir()
        with mock.patch.dict(os.environ, {'PATH': str(empty)}):
            self.assertTrue(claude_config.status(self.claude))
        other_bin = self.root / 'other-bin'
        other = write_launcher(other_bin / 'deepseek-team')
        with mock.patch.dict(os.environ, {'PATH': str(other_bin)}):
            self.assertFalse(claude_config.status(self.claude))
            self.assertTrue(claude_config.install(self.claude))
            self.assertTrue(claude_config.status(self.claude))
        self.assertIn(claude_config.hook_command(other), self.commands())
        self.assertNotIn(claude_config.hook_command(self.entrypoint), self.commands())

    def test_status_reports_a_missing_absolute_entrypoint_as_unhealthy(self):
        self.assertTrue(claude_config.install(self.claude))
        self.entrypoint.unlink()
        empty = self.root / 'empty-bin'
        empty.mkdir()
        with mock.patch.dict(os.environ, {'PATH': str(empty)}):
            self.assertFalse(claude_config.status(self.claude))

    def test_symlink_alias_keeps_the_lexical_console_script_path(self):
        target = write_launcher(self.root / 'different-target-name')
        alias_bin = self.root / 'alias bin'
        alias_bin.mkdir()
        alias = alias_bin / 'deepseek-team'
        alias.symlink_to(target)
        with mock.patch.dict(os.environ, {'PATH': str(alias_bin)}):
            self.assertTrue(claude_config.install(self.claude))
            expected = shlex.quote(str(alias)) + ' coordinator-hook --runtime claude'
            self.assertIn(expected, self.commands())
            self.assertNotIn(shlex.quote(str(target)), '\n'.join(self.commands()))
            first = self.settings.read_bytes()
            self.assertFalse(claude_config.install(self.claude))
            self.assertEqual(self.settings.read_bytes(), first)
            empty = self.root / 'empty-bin'
            empty.mkdir()
            with mock.patch.dict(os.environ, {'PATH': str(empty)}):
                self.assertTrue(claude_config.status(self.claude))
            self.assertTrue(claude_config.status(self.claude))
        self.assertTrue(claude_config.remove(self.claude))
        self.assertNotIn(expected, self.commands())

    def test_shell_metacharacter_commands_remain_foreign_and_preserved(self):
        self.claude.mkdir()
        self.settings.write_text(json.dumps({'hooks': {'Stop': [{
            'hooks': [{'type': 'command', 'command': command}
                      for command in FOREIGN_SHELL_COMMANDS],
        }]}}))
        self.assertTrue(claude_config.install(self.claude))
        self.assertTrue(claude_config.status(self.claude))
        commands = self.commands()
        for command in FOREIGN_SHELL_COMMANDS:
            self.assertIn(command, commands)
        first = self.settings.read_bytes()
        self.assertFalse(claude_config.install(self.claude))
        self.assertEqual(self.settings.read_bytes(), first)
        self.assertTrue(claude_config.remove(self.claude))
        commands = self.commands()
        for command in FOREIGN_SHELL_COMMANDS:
            self.assertIn(command, commands)


if __name__ == '__main__':
    unittest.main()
