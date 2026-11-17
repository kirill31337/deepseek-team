"""The native qwen worker runtime: tool guards, loop protocol and managed runs.

The Qwen (DSH) + DeepSeek combination must work without Codex or Claude Code.
These tests cover the self-contained loop offline (fake provider responses) and
run real managed sandbox executions against a local fixture provider, reusing
the live-delegation base. No public endpoint or real credential is used.
"""
from contextlib import redirect_stdout, redirect_stderr
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import secrets
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from codex_deepseek_team import development, native_worker, relay, worker

from test_live_delegation import LiveBase


class NativeToolTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='dst-native-tools-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / 'a.py').write_text('VALUE = 1\nSECOND = 2\n')
        (self.root / 'sub').mkdir()
        (self.root / 'sub' / 'b.md').write_text('# doc\n')
        (self.root / '.env').write_text('SECRET=fixture\n')

    def test_read_file_numbers_lines_and_bounds(self):
        out = native_worker.tool_read_file(self.root, {'path': 'a.py'})
        self.assertIn('1\tVALUE = 1', out)
        out = native_worker.tool_read_file(self.root, {'path': 'a.py', 'offset': 2, 'limit': 1})
        self.assertIn('2\tSECOND = 2', out)
        self.assertNotIn('VALUE', out)

    def test_paths_outside_checkout_and_credentials_are_refused(self):
        for path in ('../escape.py', '/etc/passwd', '.env', 'sub/../../x'):
            with self.subTest(path=path):
                with self.assertRaises(native_worker.WorkerFailure):
                    native_worker._safe_path(self.root, path)
        out = native_worker._execute_tool(self.root, 'read_file',
                                          json.dumps({'path': '.env'}), False)
        self.assertIn('Error', out)

    def test_glob_and_grep(self):
        out = native_worker.tool_glob_files(self.root, {'pattern': '**/*.py'})
        self.assertIn('a.py', out)
        out = native_worker.tool_grep_search(self.root, {'pattern': 'SECOND', 'include': '*.py'})
        self.assertIn('a.py:2', out)
        self.assertEqual(native_worker.tool_grep_search(self.root, {'pattern': 'absent'}),
                         'No matches.')

    def test_write_and_edit_guard_git_and_matches(self):
        out = native_worker.tool_write_file(self.root, {'path': 'new.py', 'content': 'X = 1\n'})
        self.assertIn('Wrote', out)
        self.assertEqual((self.root / 'new.py').read_text(), 'X = 1\n')
        out = native_worker.tool_edit_file(self.root, {'path': 'new.py', 'old_string': '1',
                                                       'new_string': '2'})
        self.assertIn('Edited', out)
        out = native_worker.tool_edit_file(self.root, {'path': 'new.py', 'old_string': 'absent',
                                                       'new_string': 'x'})
        self.assertIn('not found', out)
        with self.assertRaises(native_worker.WorkerFailure):
            native_worker._safe_path(self.root, '.git/config', write=True)

    def test_run_shell_reports_exit_and_output(self):
        out = native_worker.tool_run_shell(self.root, {'command': 'echo hello; exit 3'})
        self.assertIn('exit code: 3', out)
        self.assertIn('hello', out)

    def test_read_mode_rejects_write_tools(self):
        out = native_worker._execute_tool(self.root, 'write_file',
                                          json.dumps({'path': 'x', 'content': 'y'}), False)
        self.assertIn('unavailable', out)
        out = native_worker._execute_tool(self.root, 'no_such_tool', '{}', True)
        self.assertIn('unknown', out)


class NativeLoopTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='dst-native-loop-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / 'evidence.txt').write_text('EVIDENCE\n')

    def _run(self, responses, writable=False, **kwargs):
        with patch.object(native_worker, '_post', side_effect=responses):
            return native_worker.run_loop('task', endpoint='https://api.deepseek.com',
                                          key='fixture-key', model='deepseek-flash',
                                          effort='high', root=self.root,
                                          writable=writable, **kwargs)

    def test_tool_roundtrip_then_final_answer(self):
        seen = []

        def fake_post(endpoint, key, body):
            seen.append(body)
            if len(seen) == 1:
                return {'status': 'completed', 'output': [
                    {'type': 'function_call', 'call_id': 'c1', 'name': 'read_file',
                     'arguments': json.dumps({'path': 'evidence.txt'})}]}
            return {'status': 'completed', 'output': [
                {'type': 'message', 'content': [{'type': 'output_text', 'text': 'DONE'}]}]}

        answer = self._run(fake_post)
        self.assertEqual(answer, 'DONE')
        self.assertEqual(len(seen), 2)
        outputs = [item for item in seen[1]['input'] if item.get('type') == 'function_call_output']
        self.assertEqual(len(outputs), 1)
        self.assertIn('EVIDENCE', outputs[0]['output'])
        self.assertEqual(seen[0]['reasoning'], {'effort': 'high'})
        self.assertFalse(seen[0]['store'])
        names = {tool['name'] for tool in seen[0]['tools']}
        self.assertEqual(names, {'read_file', 'glob_files', 'grep_search'})

    def test_full_mode_exposes_write_tools(self):
        def fake_post(endpoint, key, body):
            names = {tool['name'] for tool in body['tools']}
            self.assertIn('write_file', names)
            self.assertIn('run_shell', names)
            return {'status': 'completed', 'output': [
                {'type': 'message', 'content': [{'type': 'output_text', 'text': 'OK'}]}]}

        self.assertEqual(self._run(fake_post, writable=True), 'OK')

    def test_failed_incomplete_and_empty_turns_are_rejected(self):
        for response, code in (
            ({'status': 'failed', 'error': {'message': 'boom'}}, 70),
            ({'status': 'incomplete', 'output': []}, 70),
            ({'status': 'completed', 'output': []}, 70),
        ):
            with self.subTest(response=response):
                with self.assertRaises(native_worker.WorkerFailure) as caught:
                    self._run(lambda *a: response)
                self.assertEqual(caught.exception.code, code)

    def test_step_budget_is_enforced(self):
        response = {'status': 'completed', 'output': [
            {'type': 'function_call', 'call_id': 'c', 'name': 'glob_files',
             'arguments': json.dumps({'pattern': '*'})}]}
        with self.assertRaises(native_worker.WorkerFailure) as caught:
            self._run(lambda *a: response, max_steps=2)
        self.assertEqual(caught.exception.code, 70)

    def test_credential_is_redacted_from_tool_output(self):
        def fake_post(endpoint, key, body):
            if not any(item.get('type') == 'function_call_output' for item in body['input']):
                return {'status': 'completed', 'output': [
                    {'type': 'function_call', 'call_id': 'c1', 'name': 'run_shell',
                     'arguments': json.dumps({'command': 'echo fixture-key'})}]}
            return {'status': 'completed', 'output': [
                {'type': 'message', 'content': [{'type': 'output_text', 'text': 'DONE'}]}]}

        self._run(fake_post, writable=True)


class NativeRunnerSurfaceTests(unittest.TestCase):
    def test_qwen_command_and_result_contract(self):
        argv = worker.qwen_command('/usr/bin/python3', '/tmp/native_worker.py', 'high',
                                   mode='full', endpoint='@DEEPSEEK_TEAM_ENDPOINT@')
        self.assertEqual(argv[:3], ['/usr/bin/python3', '-I', '/tmp/native_worker.py'])
        self.assertIn('--mode', argv)
        self.assertIn('full', argv)
        self.assertIn('@DEEPSEEK_TEAM_ENDPOINT@', argv)
        message, errors, completed = worker.runtime_result(
            'qwen', json.dumps({'type': 'result', 'is_error': False, 'result': 'ANSWER'}))
        self.assertEqual((message, errors, completed), ('ANSWER', '', True))
        with self.assertRaises(worker.WorkerError):
            worker.runtime_result('qwen', 'not json')
        with self.assertRaises(worker.WorkerError):
            worker.runtime_result('qwen', json.dumps({'type': 'result'}))

    def test_empty_stdout_keeps_the_real_exit_code(self):
        # A killed or timed-out loop leaves no stdout; the runner must keep the
        # original code (124/130/78) and surface stderr, not report malformed JSON.
        for raw in ('', '   \n'):
            self.assertEqual(worker.runtime_result('qwen', raw), ('', '', False))
        message, errors, completed = worker.runtime_result(
            'qwen', json.dumps({'type': 'result', 'is_error': True,
                                'result': 'Provider returned HTTP 429.'}))
        self.assertEqual((message, completed), ('', False))
        self.assertTrue(worker.RETRYABLE.search(errors))

    def test_main_emits_error_result_and_keeps_code(self):
        def refuse(endpoint, key, body):
            raise native_worker.WorkerFailure(78, 'Provider returned HTTP 429 (rate limit).')

        out, err = io.StringIO(), io.StringIO()
        task = io.StringIO('inspect the checkout')
        with patch.object(native_worker, '_post', side_effect=refuse), \
                patch.dict(os.environ, {'DEEPSEEK_API_KEY': 'fixture-key'}), \
                patch.object(sys, 'stdin', task), \
                redirect_stdout(out), redirect_stderr(err):
            code = native_worker.main(['--mode', 'read'])
        self.assertEqual(code, 78)
        value = json.loads(out.getvalue())
        self.assertTrue(value['is_error'])
        self.assertIn('429', value['result'])
        self.assertNotIn('fixture-key', out.getvalue() + err.getvalue())
        self.assertTrue(worker.RETRYABLE.search(value['result']))

    def test_resolve_runtime_falls_back_to_native_without_clis(self):
        with patch.object(worker.shutil, 'which', return_value=None):
            runtime, binary = worker.resolve_runtime('auto', 'codex', 'claude')
        self.assertEqual(runtime, 'qwen')
        self.assertEqual(binary, str(Path(sys.executable).resolve()))
        runtime, binary = worker.resolve_runtime('qwen')
        self.assertEqual(runtime, 'qwen')

    def test_child_environment_is_minimal(self):
        env = worker.child_environment(Path('/tmp/home'), 'fixture-key', 'qwen', 'high')
        self.assertEqual(env['DEEPSEEK_API_KEY'], 'fixture-key')
        self.assertNotIn('ANTHROPIC_API_KEY', env)
        self.assertNotIn('CODEX_HOME', env)

    def test_managed_command_and_launch_copy(self):
        argv = development.runtime_command('/usr/bin/python3', 'qwen', writable=True)
        self.assertIn('/run/deepseek-team/native_worker.py', argv)
        self.assertIn('@DEEPSEEK_TEAM_ENDPOINT@', argv)
        self.assertIn('full', argv)
        with tempfile.TemporaryDirectory(prefix='dst-native-launch-') as directory:
            control = Path(directory)
            development.write_launch(control, '/usr/bin/python3', 'qwen',
                                     {'PATH': '/usr/bin'}, writable=False)
            self.assertTrue((control / 'native_worker.py').is_file())
            self.assertTrue((control / 'bridge.py').is_file())
            launch = json.loads((control / 'launch.json').read_text())
            self.assertIn('read', launch['command'])


class NativeManagedTests(LiveBase):
    """Real sparse-namespace executions of the native loop against a fixture API."""

    def _exercise(self, writable):
        copy = workspace_create(self.source, self.state)
        evidence = 'EVIDENCE_' + secrets.token_hex(8)
        (copy.path / 'evidence.txt').write_text(evidence)
        calls = []
        tool_results = []
        step = 0

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                nonlocal step
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                calls.append({'path': self.path,
                              'auth': self.headers.get('Authorization') == 'Bearer actual-host-credential'})
                for item in body.get('input', []):
                    if isinstance(item, dict) and item.get('type') == 'function_call_output':
                        tool_results.append(str(item.get('output', '')))
                if step == 0:
                    step += 1
                    if writable:
                        call = {'type': 'function_call', 'call_id': 'c1', 'name': 'write_file',
                                'arguments': json.dumps({'path': 'unlisted.py',
                                                         'content': 'VALUE=42\n'})}
                    else:
                        call = {'type': 'function_call', 'call_id': 'c1', 'name': 'read_file',
                                'arguments': json.dumps({'path': 'evidence.txt'})}
                    output = [call]
                elif step == 1 and writable:
                    step += 1
                    test_code = 'from unlisted import VALUE; assert VALUE == 42; print("LOCAL_TEST_PASSED")'
                    output = [{'type': 'function_call', 'call_id': 'c2', 'name': 'run_shell',
                               'arguments': json.dumps({'command': 'python3 -c ' + repr(test_code)})}]
                elif step == 1 and not writable:
                    step += 1
                    # A write attempt in read mode must be refused by the loop itself.
                    output = [{'type': 'function_call', 'call_id': 'c2', 'name': 'write_file',
                               'arguments': json.dumps({'path': 'forbidden.txt', 'content': 'x'})}]
                else:
                    output = [{'type': 'message', 'content': [
                        {'type': 'output_text', 'text': 'NATIVE_FIXTURE_DONE'}]}]
                payload = json.dumps({'status': 'completed', 'output': output}).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        original_relay = relay.ProviderRelay
        factory = lambda path, key: original_relay(path, key,
                                                   upstream=('http', '127.0.0.1', server.server_port))
        out, err = io.StringIO(), io.StringIO()
        try:
            args = self.args('Exercise the native loop, then report.', runtime='qwen',
                             codex='codex', claude='claude', timeout=90)
            with patch.object(relay, 'ProviderRelay', side_effect=factory), \
                    redirect_stdout(out), redirect_stderr(err):
                code = managed_run(args, self.policy(50 if writable else 25), copy)
            self.assertEqual(code, 0, err.getvalue())
            self.assertIn('NATIVE_FIXTURE_DONE', out.getvalue())
            self.assertGreaterEqual(len(calls), 2, err.getvalue())
            self.assertTrue(all(call['auth'] for call in calls))
            self.assertTrue(all(call['path'] == '/responses' for call in calls))
            if writable:
                self.assertTrue((copy.path / 'unlisted.py').exists())
                self.assertTrue(any('LOCAL_TEST_PASSED' in result for result in tool_results),
                                str(tool_results))
            else:
                self.assertTrue(any(evidence in result for result in tool_results),
                                str(tool_results))
                self.assertTrue(any('unavailable' in result for result in tool_results),
                                str(tool_results))
                self.assertFalse((copy.path / 'forbidden.txt').exists())
            copy.verify()
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_native_qwen_full_access(self):
        self._exercise(True)

    def test_native_qwen_readonly(self):
        self._exercise(False)

    def test_provider_failure_keeps_real_diagnostics_and_kind(self):
        copy = workspace_create(self.source, self.state)

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers['Content-Length']))
                payload = b'{"error":{"message":"rate limited"}}'
                self.send_response(429)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        original_relay = relay.ProviderRelay
        factory = lambda path, key: original_relay(path, key,
                                                   upstream=('http', '127.0.0.1', server.server_port))
        out, err = io.StringIO(), io.StringIO()
        try:
            args = self.args('Inspect, then report.', runtime='qwen',
                             codex='codex', claude='claude', timeout=90)
            with patch.object(relay, 'ProviderRelay', side_effect=factory), \
                    redirect_stdout(out), redirect_stderr(err):
                code = managed_run(args, self.policy(25), copy)
            self.assertEqual(code, 78, err.getvalue())
            self.assertIn('429', err.getvalue())
            self.assertNotIn('malformed JSON', err.getvalue())
            metadata = workspace_load(self.state, copy.id).metadata
            self.assertEqual(metadata['status'], 'failed')
            self.assertEqual(metadata['error_kind'], 'provider')
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


def workspace_create(source, state):
    from codex_deepseek_team import workspace
    return workspace.create(source, state)


def workspace_load(state, copy_id):
    from codex_deepseek_team import workspace
    return workspace.load(state, copy_id)


def managed_run(args, policy, copy):
    from codex_deepseek_team import managed
    return managed.run(args, policy, worker, copy)


if __name__ == '__main__':
    unittest.main()