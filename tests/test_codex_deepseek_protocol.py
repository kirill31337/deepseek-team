"""Real installed Codex against a loopback-only, synthetic Responses server."""
import importlib.util
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


spec = importlib.util.spec_from_file_location('worker', Path(__file__).resolve().parents[1] / 'src/codex_deepseek_team/worker.py')
worker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(worker)


class ProtocolTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('codex'), 'Codex CLI is not installed')
    def test_real_cli_responses_routing_and_readonly_tools(self):
        self.exercise_cli()

    def exercise_cli(self):
        requests = []
        tool_results = []
        tool_sent = False
        synthetic_key = secrets.token_hex(24)

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                nonlocal tool_sent
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                # Never retain/log Authorization headers or full prompts.
                requests.append({'path': self.path, 'model': body['model'],
                                 'effort': body.get('reasoning', {}).get('effort'),
                                 'auth_valid': self.headers.get('Authorization') == 'Bearer ' + synthetic_key})
                tools = {t.get('name'): t for t in body.get('tools', [])}
                for item in body.get('input', []):
                    if isinstance(item, dict) and item.get('type') in ['function_call_output', 'custom_tool_call_output']:
                        tool_results.append(str(item.get('output', '')))
                if not tool_sent:
                    tool_sent = True
                    if 'exec_command' in tools:
                        name = 'exec_command'
                        arguments = {'cmd': 'cat evidence.txt; shopt -q login_shell && printf LOGIN_ENABLED; test -z "${DEEPSEEK_API_KEY+x}" && printf KEY_ENV_ABSENT; touch forbidden.txt', 'max_output_tokens': 500}
                    elif 'shell_command' in tools:
                        name = 'shell_command'
                        arguments = {'command': 'cat evidence.txt; touch forbidden.txt', 'timeout_ms': 1000}
                    elif 'shell' in tools:
                        name = 'shell'
                        arguments = {'command': ['sh', '-c', 'cat evidence.txt; touch forbidden.txt'], 'timeout_ms': 1000}
                    else:
                        name = 'unavailable_shell_tool'
                        arguments = {}
                    output = [{'type': 'function_call', 'id': 'fc_1', 'call_id': 'call_1',
                               'name': name, 'arguments': json.dumps(arguments), 'status': 'completed'}]
                else:
                    output = [{'type': 'message', 'id': 'msg_1', 'role': 'assistant', 'status': 'completed',
                               'content': [{'type': 'output_text', 'text': 'PROTOCOL_OK', 'annotations': []}]}]
                response = {'id': 'resp_fixture', 'object': 'response', 'model': worker.MODEL,
                            'created_at': 0, 'status': 'completed', 'output': output,
                            'usage': {'input_tokens': 1, 'output_tokens': 1, 'total_tokens': 2}}
                events = [('response.created', {'response': dict(response, status='in_progress', output=[])})]
                for item in output:
                    events += [('response.output_item.added', {'output_index': 0, 'item': item}),
                               ('response.output_item.done', {'output_index': 0, 'item': item})]
                events.append(('response.completed', {'response': response}))
                payload = ''.join(f'event: {kind}\ndata: {json.dumps(dict(data, type=kind, sequence_number=n))}\n\n'
                                  for n, (kind, data) in enumerate(events)).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.send_header('Content-Length', str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        with tempfile.TemporaryDirectory(prefix='deepseek-protocol-') as directory:
            root = Path(directory)
            home = root / 'codex'
            home.mkdir(mode=0o700)
            repo = root / 'repo'
            repo.mkdir()
            (repo / 'evidence.txt').write_text('READ_ONLY_EVIDENCE')
            server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                worker.transient_config(home)
                config = home / 'config.toml'
                config.write_text(config.read_text().replace(worker.PROVIDER['base_url'], f'http://127.0.0.1:{server.server_port}/'))
                env = worker.child_environment(home, synthetic_key)
                args = worker.command(shutil.which('codex'), effort='low')
                args[-1:-1] = ['--skip-git-repo-check', '-C', str(repo)]
                code, out, err = worker.execute(args, env, 'Exercise the scoped file operation.', 30)
                message, errors, completed = worker.result_events(out)
                self.assertEqual(code, 0, err + errors)
                self.assertTrue(completed, err + errors)
                self.assertIn('PROTOCOL_OK', message)
                self.assertGreaterEqual(len(requests), 2, 'No actual tool roundtrip')
                self.assertTrue(all(r == {'path': '/responses', 'model': 'deepseek-flash', 'effort': 'low', 'auth_valid': True} for r in requests))
                self.assertTrue(any('READ_ONLY_EVIDENCE' in result for result in tool_results), str(tool_results))
                self.assertFalse(any('LOGIN_ENABLED' in result for result in tool_results), 'Worker shell loaded login profiles')
                self.assertTrue(any('KEY_ENV_ABSENT' in result for result in tool_results), 'API key reached worker shell')
                self.assertFalse((repo / 'forbidden.txt').exists(), 'Read-only worker unexpectedly wrote to the checkout')
                for file in root.rglob('*'):
                    if file.is_file():
                        self.assertNotIn(synthetic_key.encode(), file.read_bytes(), 'Codex persisted a credential')
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)


if __name__ == '__main__':
    unittest.main()
