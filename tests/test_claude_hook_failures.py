"""Public CLI failure behavior for Claude lifecycle hooks."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


class ClaudeHookFailureTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='dst-claude-hook-failures-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        environment = mock.patch.dict(os.environ, {
            'HOME': str(self.root / 'home'),
            'CLAUDE_CONFIG_DIR': str(self.root / 'claude'),
            'XDG_CONFIG_HOME': str(self.root / 'config'),
            'XDG_STATE_HOME': str(self.root / 'state'),
            'DEEPSEEK_TEAM_STATE_DIR': str(self.root / 'team-state'),
            'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'src'),
        })
        environment.start()
        self.addCleanup(environment.stop)

    def cli(self, payload):
        return subprocess.run(
            [sys.executable, '-m', 'codex_deepseek_team',
             'coordinator-hook', '--runtime', 'claude'],
            cwd=self.root, input=payload, text=True, capture_output=True,
            timeout=15)

    def assert_blocked(self, result):
        self.assertEqual(result.returncode, 2, result.stderr + result.stdout)
        self.assertEqual(result.stdout, '')
        self.assertTrue(result.stderr.strip())
        self.assertNotIn('Traceback', result.stderr)

    def test_malformed_and_non_object_json_use_native_blocking_exit_two(self):
        marker = 'PRIVATE-PROMPT-MARKER'
        for payload in ('{"prompt": "' + marker, '[]', '"' + marker + '"', 'null'):
            with self.subTest(payload=payload):
                result = self.cli(payload)
                self.assert_blocked(result)
                self.assertNotIn(marker, result.stderr)
                self.assertIn('json', result.stderr.lower())

    def test_unknown_malformed_action_uses_native_blocking_exit_two(self):
        for payload in (
            {'hook_event_name': 'UnexpectedLifecycleEvent',
             'cwd': str(self.root), 'session_id': 'claude-session'},
            *({'hook_event_name': value, 'cwd': str(self.root),
               'session_id': 'claude-session'} for value in ([], {}, None, True, 1)),
            {'cwd': str(self.root), 'session_id': 'claude-session'},
        ):
            with self.subTest(payload=payload):
                result = self.cli(json.dumps(payload))
                self.assert_blocked(result)
                self.assertIn('safe action', result.stderr.lower())

    def test_pretooluse_processing_error_returns_an_explicit_closed_denial(self):
        result = self.cli(json.dumps({
            'hook_event_name': 'PreToolUse',
            'cwd': 123,
            'session_id': 'claude-session',
            'tool_name': 'Write',
            'tool_input': {'file_path': str(self.root / 'blocked.py')},
        }))
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        body = json.loads(result.stdout)
        specific = body['hookSpecificOutput']
        self.assertEqual(specific['hookEventName'], 'PreToolUse')
        self.assertEqual(specific['permissionDecision'], 'deny')
        self.assertIn('failed closed', specific['permissionDecisionReason'].lower())
        self.assertEqual(result.stderr, '')

    def test_userpromptsubmit_processing_error_blocks_before_model_or_tool_work(self):
        marker = 'PRIVATE-PROMPT-MARKER'
        result = self.cli(json.dumps({
            'hook_event_name': 'UserPromptSubmit',
            'cwd': 123,
            'session_id': 'claude-session',
            'prompt': marker,
        }))
        self.assert_blocked(result)
        diagnostic = result.stderr.lower()
        self.assertIn('userpromptsubmit', diagnostic)
        self.assertIn('coordination ledger', diagnostic)
        self.assertIn('no model or tool work', diagnostic)
        self.assertNotIn(marker.lower(), diagnostic)
        self.assertNotIn('continue', diagnostic)

    def test_session_stop_and_subagent_stop_errors_remain_nonblocking_and_do_not_loop(self):
        for event in ('SessionStart', 'Stop', 'SubagentStop'):
            with self.subTest(event=event):
                result = self.cli(json.dumps({
                    'hook_event_name': event,
                    'cwd': 123,
                    'session_id': 'claude-session',
                }))
                self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
                body = json.loads(result.stdout)
                self.assertTrue(body['continue'])
                self.assertNotIn('decision', body)
                self.assertIn('nonblocking', body['systemMessage'].lower())


if __name__ == '__main__':
    unittest.main()
