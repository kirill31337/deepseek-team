"""Real Claude coordinator hooks against a local API fixture; no paid requests."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from codex_deepseek_team import claude_config, coordination, project


class RealClaudeCoordinatorHookTests(unittest.TestCase):
    def test_context_distribution_denial_and_stop_reach_the_real_runtime(self):
        binary = shutil.which('claude')
        if not binary:
            if os.environ.get('DEEPSEEK_TEAM_REQUIRE_LIVE') == '1':
                self.fail('Required Claude CLI is unavailable')
            self.skipTest('Claude CLI is not installed')
        with tempfile.TemporaryDirectory(prefix='dst-claude-coordinator-') as directory:
            root = Path(directory)
            repo = root / 'repo'
            repo.mkdir()
            (repo / 'a.py').write_text('VALUE = 1\n')
            subprocess.run(['git', 'init', '-q', str(repo)], check=True)
            subprocess.run(['git', '-C', str(repo), 'add', '.'], check=True)
            subprocess.run(['git', '-C', str(repo), '-c', 'user.name=Fixture', '-c',
                            'user.email=fixture@example.invalid', 'commit', '-qm', 'base'], check=True)
            home = root / 'home'
            home.mkdir()
            config_dir = home / '.claude'
            state_dir = root / 'state'
            bin_dir = root / 'bin'
            bin_dir.mkdir()
            command = bin_dir / 'deepseek-team'
            command.write_text('#!/bin/sh\nexec ' + shlex.quote(sys.executable) +
                               ' -m codex_deepseek_team "$@"\n')
            command.chmod(0o700)
            env = {
                'PATH': str(bin_dir) + os.pathsep + os.environ.get('PATH', '/usr/bin:/bin'),
                'HOME': str(home), 'CLAUDE_CONFIG_DIR': str(config_dir),
                'XDG_CONFIG_HOME': str(root / 'config'),
                'DEEPSEEK_TEAM_STATE_DIR': str(state_dir),
                'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'src'),
                'ANTHROPIC_AUTH_TOKEN': 'offline-fixture-only',
                'ANTHROPIC_API_KEY': 'offline-fixture-only',
                'CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC': '1', 'DISABLE_AUTOUPDATER': '1',
            }
            with patch.dict(os.environ, env, clear=True):
                project.attach(repo, coordinator='claude')
                claude_config.install(config_dir)

            state = {'step': 0, 'task': None, 'contexts': [], 'results': []}

            class Handler(BaseHTTPRequestHandler):
                def log_message(self, *_):
                    pass

                def do_POST(self):
                    body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                    if self.path.split('?')[0].endswith('/count_tokens'):
                        self.respond(json.dumps({'input_tokens': 10}).encode(), 'application/json')
                        return
                    serialized = json.dumps(body)
                    match = re.search(r'task-[0-9a-f]{20}', serialized)
                    if match:
                        state['task'] = match.group(0)
                    state['contexts'].append(serialized)
                    for message in body.get('messages', []):
                        content = message.get('content', [])
                        for item in content if isinstance(content, list) else []:
                            if item.get('type') == 'tool_result':
                                state['results'].append(str(item.get('content', '')))
                    step = state['step']
                    state['step'] += 1
                    if step == 0:
                        block = {'type': 'tool_use', 'id': 'tool_native', 'name': 'Agent',
                                 'input': {'description': 'Inspect history',
                                           'subagent_type': 'general-purpose',
                                           'prompt': 'Inspect Git history independently'}}
                    elif step == 1:
                        block = {'type': 'tool_use', 'id': 'tool_0', 'name': 'Read',
                                 'input': {'file_path': str(repo / 'a.py')}}
                    elif step in (2, 4):
                        block = {'type': 'tool_use', 'id': f'tool_{step}', 'name': 'Write',
                                 'input': {'file_path': str(repo / 'a.py'), 'content': 'VALUE = 999\n'}}
                    elif step == 3 and state['task']:
                        plan = {'classification': 'substantial', 'deliverables': [{
                            'id': 'review', 'kind': 'review', 'scope': ['a.py'], 'executor': 'worker',
                            'acceptance': ['review the value'], 'dependencies': [], 'checks': [],
                        }]}
                        shell = ('printf %s ' + shlex.quote(json.dumps(plan)) +
                                 ' | deepseek-team coordination plan --task ' + state['task'])
                        block = {'type': 'tool_use', 'id': f'tool_{step}', 'name': 'Bash',
                                 'input': {'command': shell, 'timeout': 10000}}
                    else:
                        block = {'type': 'text', 'text': 'CLAUDE_COORDINATION_FIXTURE_DONE'}
                    stop = 'tool_use' if block['type'] == 'tool_use' else 'end_turn'
                    message = {
                        'id': f'msg_{step}', 'type': 'message', 'role': 'assistant',
                        'model': 'claude-sonnet-4-6', 'content': [block],
                        'stop_reason': stop, 'stop_sequence': None,
                        'usage': {'input_tokens': 10, 'output_tokens': 10},
                    }
                    if not body.get('stream'):
                        self.respond(json.dumps(message).encode(), 'application/json')
                        return
                    if block['type'] == 'tool_use':
                        start = dict(block, input={})
                        delta = {'type': 'input_json_delta', 'partial_json': json.dumps(block['input'])}
                    else:
                        start = dict(block, text='')
                        delta = {'type': 'text_delta', 'text': block['text']}
                    events = [
                        ('message_start', {'message': dict(message, content=[], stop_reason=None)}),
                        ('content_block_start', {'index': 0, 'content_block': start}),
                        ('content_block_delta', {'index': 0, 'delta': delta}),
                        ('content_block_stop', {'index': 0}),
                        ('message_delta', {'delta': {'stop_reason': stop, 'stop_sequence': None},
                                           'usage': {'output_tokens': 10}}),
                        ('message_stop', {}),
                    ]
                    payload = ''.join(f'event: {name}\ndata: {json.dumps(dict(data, type=name))}\n\n'
                                      for name, data in events).encode()
                    self.respond(payload, 'text/event-stream')

                def respond(self, payload, content_type):
                    self.send_response(200)
                    self.send_header('Content-Type', content_type)
                    self.send_header('Content-Length', str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)

            server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.addCleanup(server.server_close)
            try:
                env['ANTHROPIC_BASE_URL'] = f'http://127.0.0.1:{server.server_port}'
                # This is the coordinator, not a worker: user settings and hooks
                # must load. No worker is launched and no worker sandbox is bypassed.
                result = subprocess.run([
                    binary, '-p', '--no-session-persistence', '--output-format', 'json',
                    '--model', 'claude-sonnet-4-6', '--permission-mode', 'dontAsk',
                    '--tools', 'Bash,Read,Write,Agent', '--allowedTools', 'Bash', 'Read', 'Write', 'Agent',
                    '--strict-mcp-config', '--mcp-config', '{"mcpServers":{}}',
                    '--setting-sources', 'user', '--disable-slash-commands', '--max-turns', '8',
                    '--system-prompt', 'Offline coordinator fixture. Follow hook instructions.',
                    'Implement the feature and register the distribution.',
                ], cwd=repo, env=env, capture_output=True, text=True, timeout=60)
                self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
                self.assertIsNotNone(state['task'], 'Hook task id did not reach Claude: ' + result.stdout)
                self.assertGreaterEqual(state['step'], 7, 'Stop did not request a continuation')
                results = '\n'.join(state['results']).lower()
                self.assertIn('native delegation gate', results)
                self.assertIn('distribution gate', results)
                self.assertIn('pending worker assignment', results)
                self.assertEqual((repo / 'a.py').read_text(), 'VALUE = 1\n')
                self.assertTrue(any('Worker assignments are still pending' in c for c in state['contexts']))
                with patch.dict(os.environ, env, clear=True):
                    task = coordination.load_task(repo, state['task'])
                self.assertNotEqual(task['status'], 'completed')
                self.assertEqual(task['assignments'][0]['status'], 'planned')
            finally:
                server.shutdown()
                thread.join(timeout=2)


if __name__ == '__main__':
    unittest.main()
