"""The Qwen (DSH) hook protocol, exercised through the installed CLI boundary.

Payloads mirror what the DSH bridge plugin sends: DSH tool names (read, write,
edit, bash, terminal_send, subagent, send_message), session ids and turn ids.
The protocol is Claude-compatible, so unsupported events and malformed input
fail closed exactly like the Claude gate.
"""
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from codex_deepseek_team import coordination, project


class QwenHooksTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='dst-qwen-hooks-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        subprocess.run(['git', 'init', '-q', str(self.repo)], check=True)
        (self.repo / 'a.py').write_text('VALUE = 1\n')
        subprocess.run(['git', '-C', str(self.repo), 'add', '.'], check=True)
        subprocess.run(['git', '-C', str(self.repo), '-c', 'user.name=Test', '-c',
                        'user.email=test@example.test', 'commit', '-qm', 'base'], check=True)
        self.home = self.root / 'home'
        self.home.mkdir()
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        self.entrypoint = self.bin / 'deepseek-team'
        self.entrypoint.write_text('#!/bin/sh\nexec ' + shlex.quote(sys.executable) +
                                   ' -m codex_deepseek_team "$@"\n', encoding='utf-8')
        self.entrypoint.chmod(0o700)
        self.state = self.root / 'state'
        environment = mock.patch.dict(os.environ, {
            'HOME': str(self.home),
            'DSH_HOME': str(self.root / 'dsh-home'),
            'CODEX_HOME': str(self.home / '.codex'),
            'XDG_CONFIG_HOME': str(self.root / 'config'),
            'XDG_STATE_HOME': str(self.root / 'xdg-state'),
            'DEEPSEEK_TEAM_STATE_DIR': str(self.state),
            'DEEPSEEK_TEAM_DISABLED': '',
            'PATH': str(self.bin) + os.pathsep + os.environ.get('PATH', '/usr/bin:/bin'),
            'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'src'),
        })
        environment.start()
        self.addCleanup(environment.stop)

    def cli(self, *args, input=''):
        return subprocess.run([sys.executable, '-m', 'codex_deepseek_team', *args],
                              cwd=self.repo, input=input, text=True,
                              capture_output=True, timeout=30)

    def hook(self, event, **extra):
        payload = dict(hook_event_name=event, cwd=str(self.repo), session_id='dsh-session')
        payload.update(extra)
        result = self.cli('coordinator-hook', '--runtime', 'qwen', input=json.dumps(payload))
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        return json.loads(result.stdout) if result.stdout.strip() else {}

    def start(self):
        project.attach(self.repo, coordinator='qwen')
        result = self.hook('UserPromptSubmit', prompt='Implement the feature', turn_id='1')
        context = result['hookSpecificOutput']['additionalContext']
        self.assertIn('coordination task', context.lower())
        self.assertIn('--runtime qwen', context)
        return coordination.latest_task(self.repo, 'dsh-session')

    def plan(self, task, scope='a.py'):
        return coordination.plan_task(self.repo, task['id'], {
            'classification': 'small',
            'small_evidence': 'One constant changes in a single file.',
            'deliverables': [{
                'id': 'change', 'kind': 'implementation', 'executor': 'coordinator',
                'scope': [scope], 'acceptance': ['value checked'],
                'dependencies': [], 'checks': [],
            }],
        })

    def assert_denied(self, result, reason):
        specific = result['hookSpecificOutput']
        self.assertEqual(specific['hookEventName'], 'PreToolUse')
        self.assertEqual(specific['permissionDecision'], 'deny')
        self.assertIn(reason, specific['permissionDecisionReason'].lower())

    def test_session_start_injects_qwen_policy(self):
        project.attach(self.repo, coordinator='qwen')
        result = self.hook('SessionStart', source='startup')
        context = result['hookSpecificOutput']['additionalContext']
        self.assertIn('Qwen (DSH)', context)
        self.assertIn('--runtime qwen', context)
        self.assertIn('DeepSeek Harness', context)

    def test_unplanned_dsh_mutations_are_denied(self):
        self.start()
        for tool, tool_input in (
            ('write', {'file_path': str(self.repo / 'a.py'), 'content': 'VALUE = 2\n'}),
            ('edit', {'file_path': str(self.repo / 'a.py'), 'old_string': '1', 'new_string': '2'}),
            ('bash', {'command': 'echo changed > a.py', 'description': 'write'}),
            ('terminal_send', {'sessionId': 't1', 'text': 'echo changed > a.py'}),
        ):
            with self.subTest(tool=tool):
                result = self.hook('PreToolUse', tool_name=tool, tool_use_id='call-x',
                                   tool_input=tool_input)
                self.assert_denied(result, 'distribution gate')

    def test_planned_coordinator_scope_is_allowed(self):
        task = self.start()
        self.plan(task)
        result = self.hook('PreToolUse', tool_name='edit', tool_use_id='call-1',
                           tool_input={'file_path': str(self.repo / 'a.py'),
                                       'old_string': '1', 'new_string': '2'})
        self.assertEqual(result, {})
        outside = self.hook('PreToolUse', tool_name='write', tool_use_id='call-2',
                            tool_input={'file_path': str(self.repo / 'b.py'), 'content': 'x'})
        self.assert_denied(outside, 'unplanned mutation scope')

    def test_early_planning_gate_recognizes_dsh_read_tools(self):
        self.start()
        first = self.hook('PreToolUse', tool_name='read', tool_use_id='read-1',
                          tool_input={'file_path': str(self.repo / 'a.py')})
        self.assertIn('early-planning', first['hookSpecificOutput']['additionalContext'])
        second = self.hook('PreToolUse', tool_name='read', tool_use_id='read-2',
                           tool_input={'file_path': str(self.repo / 'a.py')})
        self.assert_denied(second, 'early-planning')
        repeated = self.hook('PreToolUse', tool_name='read', tool_use_id='read-1',
                             tool_input={'file_path': str(self.repo / 'a.py')})
        self.assertIn('early-planning', repeated['hookSpecificOutput']['additionalContext'])

    def test_dsh_glob_and_grep_are_inspection_tools(self):
        self.start()
        first = self.hook('PreToolUse', tool_name='glob', tool_use_id='read-glob',
                          tool_input={'pattern': '**/*.py', 'path': str(self.repo)})
        self.assertIn('early-planning', first['hookSpecificOutput']['additionalContext'])
        # Once one inspection is recorded, further recognized source reads are
        # denied until classification; a denial proves recognition as well.
        for tool, tool_input in (
            ('grep', {'pattern': 'VALUE', 'path': str(self.repo)}),
            ('lsp', {'operation': 'hover', 'file_path': str(self.repo / 'a.py'),
                     'line': 1, 'character': 1}),
        ):
            with self.subTest(tool=tool):
                result = self.hook('PreToolUse', tool_name=tool, tool_use_id='read-' + tool,
                                   tool_input=tool_input)
                self.assert_denied(result, 'early-planning')

    def test_terminal_send_reads_are_source_inspection(self):
        self.start()
        result = self.hook('PreToolUse', tool_name='terminal_send', tool_use_id='term-1',
                           tool_input={'sessionId': 't1', 'text': 'cat a.py'})
        self.assertIn('early-planning', result['hookSpecificOutput']['additionalContext'])

    def test_native_dsh_dispatch_requires_marker(self):
        task = self.start()
        self.plan(task)
        result = self.hook('PreToolUse', tool_name='subagent', tool_use_id='call-2',
                           tool_input={'prompt': 'Investigate the module', 'description': 'x'})
        self.assert_denied(result, 'native delegation gate')
        result = self.hook('PreToolUse', tool_name='send_message', tool_use_id='call-3',
                           tool_input={'target': 'helper', 'message': 'continue'})
        self.assert_denied(result, 'native delegation gate')

    def test_approved_native_exception_allows_dsh_subagent(self):
        task = self.start()
        coordination.plan_task(self.repo, task['id'], {
            'classification': 'small',
            'small_evidence': 'One bounded native audit.',
            'deliverables': [{
                'id': 'audit', 'kind': 'research', 'scope': ['a.py'],
                'executor': 'native-agent',
                'delegation_reason': 'Native session history is unavailable to a worker',
                'native_exception': {'code': 'native_capability',
                                     'capability': 'session history access',
                                     'evidence': 'The audit needs native session search tools'},
                'acceptance': ['Return source references'], 'dependencies': [], 'checks': [],
            }],
        })
        marker = '[deepseek-team:' + task['id'] + ':audit]'
        result = self.hook('PreToolUse', tool_name='subagent', tool_use_id='call-4',
                           tool_input={'prompt': marker + ' Perform the scoped audit',
                                       'description': 'audit'})
        self.assertEqual(result, {})
        saved = coordination.load_task(self.repo, task['id'])
        self.assertEqual(saved['coordinator_events'][-1]['kind'], 'native_dispatch')

    def test_child_agent_events_never_touch_the_ledger(self):
        self.start()
        result = self.hook('UserPromptSubmit', prompt='child work', agent_id='child-1')
        self.assertEqual(result, {})
        result = self.hook('PreToolUse', tool_name='write', agent_id='child-1',
                           tool_input={'file_path': str(self.repo / 'a.py'), 'content': 'x'})
        self.assertEqual(result, {})

    def test_stop_blocks_until_disposition_and_then_warns(self):
        task = self.start()
        self.plan(task)
        result = self.hook('Stop')
        self.assertEqual(result.get('decision'), 'block')
        warned = self.hook('Stop', stop_hook_active=True)
        self.assertNotEqual(warned.get('decision'), 'block')
        self.assertIn('unfinished', warned.get('systemMessage', ''))

    def test_unsupported_event_fails_closed(self):
        result = self.cli('coordinator-hook', '--runtime', 'qwen',
                          input=json.dumps({'hook_event_name': 'PostToolUse',
                                            'session_id': 's', 'cwd': str(self.repo)}))
        self.assertEqual(result.returncode, 2)

    def test_malformed_input_fails_closed(self):
        result = self.cli('coordinator-hook', '--runtime', 'qwen', input='not json')
        self.assertEqual(result.returncode, 2)
        self.assertIn('Qwen (DSH)', result.stderr)

    def test_unattached_project_stays_inert(self):
        result = self.hook('PreToolUse', tool_name='write',
                           tool_input={'file_path': str(self.repo / 'a.py'), 'content': 'x'})
        self.assertEqual(result, {})

    def test_init_and_detach_manage_shared_agents_block(self):
        self.assertTrue(project.attach(self.repo, coordinator='qwen'))
        text = (self.repo / 'AGENTS.md').read_text()
        self.assertIn('--runtime qwen', text)
        self.assertIn('Qwen (DSH)', text)
        self.assertTrue(project.is_attached(self.repo, 'qwen'))
        # Codex shares the same file and block.
        self.assertTrue(project.is_attached(self.repo, 'codex'))
        self.assertTrue(project.detach(self.repo, coordinator='qwen'))
        self.assertFalse((self.repo / 'AGENTS.md').exists())

    def test_all_selector_deduplicates_shared_target(self):
        self.assertTrue(project.attach(self.repo, coordinator='all'))
        agents = (self.repo / 'AGENTS.md').read_text()
        claude = (self.repo / 'CLAUDE.md').read_text()
        self.assertEqual(agents.count('codex-deepseek-team:managed-block:start'), 1)
        self.assertEqual(claude.count('codex-deepseek-team:managed-block:start'), 1)
        # The shared AGENTS.md keeps the first selected runtime's rendering.
        self.assertIn('Codex process integration', agents)
        self.assertTrue(project.detach(self.repo, coordinator='all'))
        self.assertFalse((self.repo / 'AGENTS.md').exists())
        self.assertFalse((self.repo / 'CLAUDE.md').exists())


if __name__ == '__main__':
    unittest.main()