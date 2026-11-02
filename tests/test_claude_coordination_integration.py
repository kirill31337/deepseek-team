"""Real Claude coordinator hooks against a local API fixture; no paid requests."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import http.client
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


DEFAULT_PROMPT = 'Implement the feature and register the distribution.'
MODEL = 'claude-sonnet-4-6'


def classifier_request(*, stream=None, action=None):
    """A minimal synthetic auto-mode classifier request; no live model involved."""
    payload = {
        'model': MODEL,
        'max_tokens': 64,
        'system': [
            {'type': 'text', 'text': 'x-anthropic-billing-header: cc_version=fixture;'},
            {'type': 'text', 'text': (
                '<cc_automode_permissions>\n\nClassification Process\n'
                'Review the last action and answer with <block>yes</block> or '
                '<block>no</block>.')},
        ],
        'messages': [{'role': 'user', 'content': [
            {'type': 'text', 'text': '<transcript>\n'},
            {'type': 'text', 'text': '{"user":"Draft a plan for the fixture."}\n'},
            {'type': 'text', 'text': json.dumps(
                action or {'Write': '/tmp/fixture-plan.md: # Fixture plan\n'}) + '\n'},
            {'type': 'text', 'text': '</transcript>\n'},
            {'type': 'text', 'text': 'Use <thinking> then answer with <block>.'},
        ]}],
    }
    if stream is not None:
        payload['stream'] = stream
    return payload


def post_fixture(server, path, payload):
    connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=5)
    try:
        connection.request('POST', path, body=json.dumps(payload),
                           headers={'Content-Type': 'application/json'})
        response = connection.getresponse()
        return response.status, response.getheader('Content-Type'), response.read().decode()
    finally:
        connection.close()


def system_text(body):
    system = body.get('system')
    if isinstance(system, str):
        return system
    if isinstance(system, list):
        return '\n'.join(
            item.get('text', '') for item in system
            if isinstance(item, dict) and isinstance(item.get('text'), str))
    return ''


def request_text(body):
    parts = [system_text(body)]
    for message in body.get('messages', []):
        content = message.get('content', [])
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            parts.extend(item['text'] for item in content
                         if isinstance(item, dict) and isinstance(item.get('text'), str))
    return '\n'.join(parts)


def advertised_plan_file(body):
    match = re.search(
        r'You should create your plan at (.+?) using the Write tool',
        request_text(body))
    return Path(match.group(1)) if match else None


def is_auto_mode_classifier_request(body):
    text = system_text(body)
    return ('<cc_automode_permissions>' in text
            or 'Classification Process' in text)


def classifier_action(body):
    """Extract the single scripted tool action from the classifier transcript."""
    action = None
    for message in body.get('messages', []):
        content = message.get('content', [])
        for item in content if isinstance(content, list) else []:
            text = item.get('text') if isinstance(item, dict) else None
            if not isinstance(text, str):
                continue
            for line in text.splitlines():
                candidate = line.strip()
                if not (candidate.startswith('{') and candidate.endswith('}')):
                    continue
                try:
                    entry = json.loads(candidate)
                except ValueError:
                    continue
                if not isinstance(entry, dict) or len(entry) != 1:
                    continue
                tool, detail = next(iter(entry.items()))
                if tool != 'user':
                    action = (str(tool), str(detail))
    return action


def message_response(block, message_id, stream):
    """Encode one assistant block as the native JSON or SSE Messages response."""
    stop = 'tool_use' if block['type'] == 'tool_use' else 'end_turn'
    message = {
        'id': message_id, 'type': 'message', 'role': 'assistant',
        'model': MODEL, 'content': [block],
        'stop_reason': stop, 'stop_sequence': None,
        'usage': {'input_tokens': 10, 'output_tokens': 10},
    }
    if not stream:
        return json.dumps(message).encode(), 'application/json'
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
    return payload, 'text/event-stream'


def write_launcher(path):
    """A legitimate in-fixture package entrypoint, not a product-code stub."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('#!/bin/sh\nexec ' + shlex.quote(sys.executable) +
                    ' -m codex_deepseek_team "$@"\n', encoding='utf-8')
    path.chmod(0o700)
    return path


def prepare_workspace(root):
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
    bin_dir = root / 'bin'
    bin_dir.mkdir()
    command = write_launcher(bin_dir / 'deepseek-team')
    env = {
        'PATH': str(bin_dir) + os.pathsep + os.environ.get('PATH', '/usr/bin:/bin'),
        'HOME': str(home), 'CLAUDE_CONFIG_DIR': str(config_dir),
        'CODEX_HOME': str(root / 'codex'),
        'XDG_CONFIG_HOME': str(root / 'config'),
        'XDG_STATE_HOME': str(root / 'xdg-state'),
        'DEEPSEEK_TEAM_STATE_DIR': str(root / 'state'),
        'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'src'),
        'ANTHROPIC_AUTH_TOKEN': 'offline-fixture-only',
        'ANTHROPIC_API_KEY': 'offline-fixture-only',
        'CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC': '1', 'DISABLE_AUTOUPDATER': '1',
    }
    with patch.dict(os.environ, env, clear=True):
        project.attach(repo, coordinator='claude')
        claude_config.install(config_dir)
    return repo, config_dir, command, env


def start_server(scenario, *, classifier_allowed=None):
    """Serve scripted Anthropic messages; no external request leaves localhost."""
    state = {'step': 0, 'task': None, 'contexts': [], 'results': [],
             'classifier_requests': [], 'count_token_requests': 0}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            if self.path.split('?')[0].endswith('/count_tokens'):
                state['count_token_requests'] += 1
                self.respond(json.dumps({'input_tokens': 10}).encode(), 'application/json')
                return
            if is_auto_mode_classifier_request(body):
                action = classifier_action(body)
                allowed = bool(classifier_allowed and action
                               and classifier_allowed(action))
                state['classifier_requests'].append({
                    'action': action, 'allowed': allowed,
                    'stream': bool(body.get('stream')),
                })
                block = {'type': 'text',
                         'text': '<block>no</block>' if allowed else '<block>yes</block>'}
                payload, content_type = message_response(
                    block, f'msg_classifier_{len(state["classifier_requests"])}',
                    body.get('stream'))
                self.respond(payload, content_type)
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
            block = scenario(step, body, state)
            if block is None:
                block = {'type': 'text', 'text': 'CLAUDE_COORDINATION_FIXTURE_DONE'}
            payload, content_type = message_response(
                block, f'msg_{step}', body.get('stream'))
            self.respond(payload, content_type)

        def respond(self, payload, content_type):
            self.send_response(200)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, state


def run_claude(binary, repo, env, *, permission_mode='dontAsk', max_turns=8,
               tools=('Bash', 'Read', 'Write', 'Agent'), setting_sources='user',
               prompt=DEFAULT_PROMPT):
    environment = dict(env)
    return subprocess.run([
        binary, '-p', '--no-session-persistence', '--output-format', 'json',
        '--model', MODEL, '--permission-mode', permission_mode,
        '--tools', ','.join(tools), '--allowedTools', *tools,
        '--strict-mcp-config', '--mcp-config', '{"mcpServers":{}}',
        '--setting-sources', setting_sources, '--disable-slash-commands',
        '--max-turns', str(max_turns),
        '--system-prompt', 'Offline coordinator fixture. Follow hook instructions.',
        prompt,
    ], cwd=repo, env=environment, capture_output=True, text=True, timeout=90)


class FixtureProtocolTests(unittest.TestCase):
    def test_auxiliary_requests_do_not_consume_scripted_turns(self):
        allowed_action = ('Write', '/tmp/fixture-plan.md: # Fixture plan\n')
        scenario_calls = []

        def scenario(*args):
            scenario_calls.append(args)
            return None

        server, thread, state = start_server(
            scenario, classifier_allowed=lambda action: action == allowed_action)
        try:
            status, content_type, raw = post_fixture(
                server, '/v1/messages', classifier_request())
            self.assertEqual(status, 200)
            self.assertIn('application/json', content_type)
            message = json.loads(raw)
            self.assertEqual(message['content'][0]['text'], '<block>no</block>')

            status, content_type, raw = post_fixture(
                server, '/v1/messages', classifier_request(stream=True))
            self.assertEqual(status, 200)
            self.assertIn('text/event-stream', content_type)
            self.assertIn('event: content_block_delta', raw)
            self.assertIn('<block>no</block>', raw)
            self.assertIn('event: message_stop', raw)

            status, _content_type, raw = post_fixture(
                server, '/v1/messages', classifier_request(
                    stream=False, action={'Bash': 'curl https://example.invalid'}))
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(raw)['content'][0]['text'], '<block>yes</block>')

            status, _content_type, raw = post_fixture(
                server, '/v1/messages/count_tokens', {'model': MODEL, 'messages': []})
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(raw), {'input_tokens': 10})
            self.assertEqual(scenario_calls, [])
            self.assertEqual(state['step'], 0)
            self.assertEqual(state['contexts'], [])
            self.assertEqual(state['results'], [])
            self.assertEqual(len(state['classifier_requests']), 3)
            self.assertEqual(state['count_token_requests'], 1)
            self.assertFalse(state['classifier_requests'][-1]['allowed'])
        finally:
            server.shutdown()
            thread.join(timeout=2)
            server.server_close()


class RealClaudeCoordinatorHookTests(unittest.TestCase):
    def require_claude(self):
        binary = shutil.which('claude')
        if not binary:
            if os.environ.get('DEEPSEEK_TEAM_REQUIRE_LIVE') == '1':
                self.fail('Required Claude CLI is unavailable')
            self.skipTest('Claude CLI is not installed')
        return binary

    def run_fixture(self, binary, repo, env, scenario, *, classifier_allowed=None,
                    **run_options):
        server, thread, state = start_server(
            scenario, classifier_allowed=classifier_allowed)
        try:
            live_env = dict(env)
            live_env['ANTHROPIC_BASE_URL'] = f'http://127.0.0.1:{server.server_port}'
            result = run_claude(binary, repo, live_env, **run_options)
            return result, state
        finally:
            server.shutdown()
            thread.join(timeout=2)
            server.server_close()

    def test_context_distribution_denial_and_stop_reach_the_real_runtime(self):
        binary = self.require_claude()
        with tempfile.TemporaryDirectory(prefix='dst-claude-coordinator-') as directory:
            root = Path(directory)
            repo, _config_dir, _command, env = prepare_workspace(root)

            def scenario(step, body, state):
                if step == 0:
                    return {'type': 'tool_use', 'id': 'tool_native', 'name': 'Agent',
                            'input': {'description': 'Inspect history',
                                      'subagent_type': 'general-purpose',
                                      'prompt': 'Inspect Git history independently'}}
                if step == 1:
                    return {'type': 'tool_use', 'id': 'tool_0', 'name': 'Read',
                            'input': {'file_path': str(repo / 'a.py')}}
                if step in (2, 4):
                    return {'type': 'tool_use', 'id': f'tool_{step}', 'name': 'Write',
                            'input': {'file_path': str(repo / 'a.py'),
                                      'content': 'VALUE = 999\n'}}
                if step == 3 and state['task']:
                    plan = {'classification': 'substantial', 'deliverables': [{
                        'id': 'review', 'kind': 'review', 'scope': ['a.py'], 'executor': 'auto',
                        'acceptance': ['review a.py confirms the value stays 1'],
                        'dependencies': [], 'checks': [],
                        'features': {'kind': 'review', 'domain': 'python',
                                     'operation': 'review', 'localization': 'known',
                                     'coupling': 'local', 'verification': 'manual',
                                     'clarity': 'clear', 'risk': 'low',
                                     'scope_size': 'small', 'runtime': 'claude',
                                     'model': 'deepseek-flash', 'effort': 'high',
                                     'context_version': 'project-v1'},
                    }]}
                    shell = ('printf %s ' + shlex.quote(json.dumps(plan)) +
                             ' | deepseek-team coordination plan --task ' + state['task'])
                    return {'type': 'tool_use', 'id': f'tool_{step}', 'name': 'Bash',
                            'input': {'command': shell, 'timeout': 10000}}
                return None

            result, state = self.run_fixture(binary, repo, env, scenario)
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
            self.assertEqual(task['deliverables'][0]['executor'], 'worker')
            self.assertEqual(task['assignments'][0]['status'], 'planned')

    def test_onfailure_blocks_an_unplanned_write_when_entrypoint_cannot_execute(self):
        binary = self.require_claude()
        with tempfile.TemporaryDirectory(prefix='dst-claude-hook-missing-') as directory:
            root = Path(directory)
            repo, _config_dir, command, env = prepare_workspace(root)
            command.unlink()
            created = repo / 'created-by-missing-hook.py'

            def scenario(step, body, state):
                if step == 0:
                    return {'type': 'tool_use', 'id': 'tool_write', 'name': 'Write',
                            'input': {'file_path': str(created), 'content': 'BYPASS = 1\n'}}
                return None

            result, state = self.run_fixture(
                binary, repo, env, scenario, max_turns=4, tools=('Read', 'Write'))
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            self.assertFalse(created.exists(), 'Missing hook entrypoint did not fail closed')
            self.assertGreaterEqual(state['step'], 2)

    def test_internal_hook_error_after_admission_denies_an_unplanned_write(self):
        binary = self.require_claude()
        with tempfile.TemporaryDirectory(prefix='dst-claude-hook-error-') as directory:
            root = Path(directory)
            repo, _config_dir, _command, env = prepare_workspace(root)
            activation_parent = Path(env['XDG_CONFIG_HOME']) / 'deepseek-team'
            created = repo / 'created-by-hook-error.py'

            def scenario(step, body, state):
                if step == 0:
                    # Admission has succeeded and the first main-model turn is
                    # scripted; corrupt activation only before its PreToolUse call.
                    activation_parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                    (activation_parent / 'activation').write_text('not a directory\n')
                    return {'type': 'tool_use', 'id': 'tool_write', 'name': 'Write',
                            'input': {'file_path': str(created), 'content': 'BYPASS = 1\n'}}
                return None

            result, state = self.run_fixture(
                binary, repo, env, scenario, max_turns=4, tools=('Read', 'Write'))
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            self.assertIsNotNone(state['task'], 'Coordinator admission did not complete')
            self.assertFalse(created.exists(), 'Internal hook error did not fail closed')
            results = '\n'.join(state['results']).lower()
            self.assertIn('failed closed', results)
            self.assertIn('requested operation was not approved', results)
            self.assertGreaterEqual(state['step'], 2)

    def test_admission_hook_error_blocks_before_model_or_tool_launch(self):
        binary = self.require_claude()
        with tempfile.TemporaryDirectory(prefix='dst-claude-admission-error-') as directory:
            root = Path(directory)
            repo, _config_dir, _command, env = prepare_workspace(root)
            activation_parent = Path(env['XDG_CONFIG_HOME']) / 'deepseek-team'
            activation_parent.mkdir(mode=0o700, parents=True)
            (activation_parent / 'activation').write_text('not a directory\n')
            created = repo / 'created-before-admission.py'
            scenario_calls = []

            def scenario(step, body, state):
                scenario_calls.append(step)
                return {'type': 'tool_use', 'id': 'tool_write', 'name': 'Write',
                        'input': {'file_path': str(created), 'content': 'BYPASS = 1\n'}}

            result, state = self.run_fixture(
                binary, repo, env, scenario, max_turns=4, tools=('Read', 'Write'))
            output = result.stderr + result.stdout
            self.assertEqual(result.returncode, 0, output)
            self.assertIn('blocked UserPromptSubmit', output)
            self.assertEqual(scenario_calls, [], 'A model request escaped failed admission')
            self.assertEqual(state['step'], 0)
            self.assertIsNone(state['task'])
            self.assertEqual(state['results'], [])
            self.assertFalse(created.exists(), 'A tool ran after failed admission')

    def test_healthy_allowed_write_control_with_hooks_removed(self):
        binary = self.require_claude()
        with tempfile.TemporaryDirectory(prefix='dst-claude-healthy-write-') as directory:
            root = Path(directory)
            repo, config_dir, _command, env = prepare_workspace(root)
            self.assertTrue(claude_config.remove(config_dir), 'Fixture hooks were not removed')
            created = repo / 'healthy-fixture-write.py'
            content = 'HEALTHY_FIXTURE = 1\n'

            def scenario(step, body, state):
                if step == 0:
                    return {'type': 'tool_use', 'id': 'tool_healthy', 'name': 'Write',
                            'input': {'file_path': str(created), 'content': content}}
                return None

            result, state = self.run_fixture(
                binary, repo, env, scenario, max_turns=4, tools=('Read', 'Write'))
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            self.assertEqual(created.read_text(), content,
                             'The fixture could not perform a healthy allowed write')
            self.assertEqual(state['step'], 2, 'Scripted main turns did not both execute')
            self.assertEqual(state['classifier_requests'], [])
            self.assertEqual((repo / 'a.py').read_text(), 'VALUE = 1\n')

    def assert_native_plan_write(self, binary, *, project_local):
        with tempfile.TemporaryDirectory(prefix='dst-claude-plan-') as directory:
            root = Path(directory)
            repo, config_dir, _command, env = prepare_workspace(root)
            sources = 'user'
            if project_local:
                (repo / '.claude').mkdir()
                (repo / '.claude' / 'settings.json').write_text(
                    json.dumps({'plansDirectory': './plans'}))
                expected_plan_dir = repo / 'plans'
                sources = 'user,project'
            else:
                expected_plan_dir = config_dir / 'plans'
            expected_plan_dir.mkdir(parents=True, exist_ok=True)
            plan = None
            content = '# Feature plan\n\nKeep VALUE = 1.\n'

            def scenario(step, body, state):
                nonlocal plan
                if step == 0:
                    plan = advertised_plan_file(body)
                    if plan is None:
                        return None
                    return {'type': 'tool_use', 'id': 'tool_plan', 'name': 'Write',
                            'input': {'file_path': str(plan), 'content': content}}
                return None

            def classifier_allowed(action):
                return (plan is not None and action[0] == 'Write'
                        and action[1].startswith(str(plan) + ':'))

            result, state = self.run_fixture(
                binary, repo, env, scenario, permission_mode='plan', max_turns=4,
                tools=('Read', 'Write'), setting_sources=sources,
                prompt='Draft a plan for the feature without editing source.',
                classifier_allowed=classifier_allowed)
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            self.assertIsNotNone(plan, 'Claude did not advertise a native plan file')
            self.assertEqual(plan.parent, expected_plan_dir)
            self.assertTrue(plan.exists(), 'Native plan write was not created: ' + result.stdout)
            self.assertEqual(plan.read_text(), content)
            self.assertEqual((repo / 'a.py').read_text(), 'VALUE = 1\n')
            self.assertEqual(state['step'], 2,
                             'Classifier response consumed a scripted main turn')
            self.assertEqual(len(state['contexts']), 2)
            self.assertTrue(all(row['allowed'] for row in state['classifier_requests']))

    def test_native_auto_mode_classifier_reply_does_not_consume_scripted_turn(self):
        binary = self.require_claude()
        with tempfile.TemporaryDirectory(prefix='dst-claude-classifier-') as directory:
            root = Path(directory)
            repo, config_dir, _command, env = prepare_workspace(root)
            plan = config_dir / 'plans' / 'feature.md'
            plan.parent.mkdir(parents=True)
            content = '# Feature plan\n\nKeep VALUE = 1.\n'

            def scenario(step, body, state):
                if step == 0:
                    return {'type': 'tool_use', 'id': 'tool_plan', 'name': 'Write',
                            'input': {'file_path': str(plan), 'content': content}}
                return None

            def classifier_allowed(action):
                return action[0] == 'Write' and action[1].startswith(str(plan) + ':')

            result, state = self.run_fixture(
                binary, repo, env, scenario, permission_mode='plan', max_turns=4,
                tools=('Read', 'Write'), classifier_allowed=classifier_allowed,
                prompt='Draft a plan for the feature without editing source.')
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            self.assertEqual(plan.read_text(), content)
            self.assertGreaterEqual(len(state['classifier_requests']), 1)
            self.assertTrue(all(row['allowed'] for row in state['classifier_requests']))
            self.assertEqual(state['step'], 2,
                             'Classifier response consumed a scripted main turn')
            self.assertEqual(len(state['contexts']), 2)
            self.assertEqual((repo / 'a.py').read_text(), 'VALUE = 1\n')

    def test_native_plan_write_uses_default_plans_directory(self):
        self.assert_native_plan_write(self.require_claude(), project_local=False)

    def test_native_plan_write_uses_custom_project_local_plans_directory(self):
        self.assert_native_plan_write(self.require_claude(), project_local=True)


if __name__ == '__main__':
    unittest.main()
