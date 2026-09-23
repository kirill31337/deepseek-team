"""Check diagnostics without exposing server bodies or credentials."""
import contextlib
import json
import os
from pathlib import Path
import socket
import secrets
import subprocess
import tempfile
import time
import unittest
import urllib.error
import io
from unittest import mock


from codex_deepseek_team import doctor as check, sandbox


class CheckTests(unittest.TestCase):
    def test_offline_doctor_preserves_any_primary_without_network_or_key_reads(self):
        from codex_deepseek_team import config
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / 'config.toml'
            path.write_text('model="example-primary"\nmodel_provider="custom-provider"\n')
            config.configure(root)
            auth = root / 'auth.json'
            auth.write_text('{"synthetic":true}')
            before = (path.read_bytes(), auth.read_bytes())
            with mock.patch.dict(os.environ, {'CODEX_HOME': directory}), \
                 mock.patch('subprocess.check_output', side_effect=['codex-cli fixture', '--strict-config --ephemeral --json --sandbox --ignore-rules']), \
                 mock.patch.object(check.worker, 'load_api_key') as key, \
                 mock.patch.object(sandbox, 'probe_backend', return_value=sandbox.SandboxBackend(('/usr/bin/bwrap',), '/usr/bin/bwrap', 'direct')), \
                 mock.patch('urllib.request.build_opener') as network, \
                 mock.patch('sys.stdout', new_callable=io.StringIO) as output:
                self.assertEqual(check.main(['--offline', '--access', 'read-only']), 0)
                self.assertIn('example-primary', output.getvalue())
                key.assert_not_called()
                network.assert_not_called()
            self.assertEqual((path.read_bytes(), auth.read_bytes()), before)

    def test_probe_rejects_wrong_served_model(self):
        opener = mock.Mock()
        opener.open.side_effect = [io.BytesIO(b'{"data":[{"id":"deepseek-flash"}]}'),
                                  io.BytesIO(b'data: {"response":{"model":"unexpected"}}\n')]
        with mock.patch.dict(os.environ, {'DEEPSEEK_API_KEY': secrets.token_hex(24)}), \
             mock.patch('urllib.request.build_opener', return_value=opener), \
             mock.patch('sys.stdout', new_callable=io.StringIO):
            self.assertEqual(check.api_probe(), 78)

    def test_live_worker_call_waits_for_delayed_final_answer(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'codex_deepseek_team').mkdir()
            (root / 'codex_deepseek_team/__init__.py').touch()
            (root / 'codex_deepseek_team/worker.py').write_text(
                'import time\ntime.sleep(1)\nprint("delayed answer")\n')
            launch = root / 'launch'
            launch.mkdir()
            (launch / 'codex_deepseek_team.py').write_text('raise RuntimeError("untrusted project code")\n')
            clock = time.monotonic
            with mock.patch.object(check, 'PACKAGE_ROOT', root), \
                 mock.patch.object(subprocess, '_time', side_effect=lambda: clock() * 1000), \
                 mock.patch.dict(os.environ, {'HOME': directory, 'CODEX_HOME': directory, 'DEEPSEEK_API_KEY': ''}):
                result = check.call('synthetic task', cwd=launch)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), 'delayed answer')

    def test_live_probe_runs_as_package_module_in_a_separate_process(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / 'codex_deepseek_team'
            package.mkdir()
            (package / '__init__.py').touch()
            (package / 'doctor.py').write_text(
                'import os, sys\n'
                'assert sys.argv[1:] == ["--api-probe"]\n'
                'assert "PARENT_SECRET" not in os.environ\n'
                'print("synthetic module probe")\n'
                'sys.exit(70)\n')
            launch = root / 'launch'
            launch.mkdir()
            (launch / 'codex_deepseek_team.py').write_text('raise RuntimeError("untrusted project code")\n')
            with contextlib.chdir(launch), \
                 mock.patch.object(check, 'PACKAGE_ROOT', root), \
                 mock.patch.object(check, '_delegation_disabled', return_value=False), \
                 mock.patch.object(check.worker, 'load_api_key', return_value='invalid synthetic key'), \
                 mock.patch.dict(os.environ, {'PARENT_SECRET': 'private'}), \
                 mock.patch('sys.stdout', new_callable=io.StringIO) as output:
                self.assertEqual(check.live_tests(), 70)
            self.assertIn('synthetic module probe', output.getvalue())

    def test_disabled_probe_does_not_read_credentials_or_use_network(self):
        with mock.patch.dict(os.environ, {'DEEPSEEK_TEAM_DISABLED': '1'}), \
             mock.patch.object(check.worker, 'load_api_key', return_value='') as load_key, \
             mock.patch('urllib.request.build_opener') as transport, \
             mock.patch('sys.stdout', new_callable=io.StringIO):
            self.assertEqual(check.api_probe(), 69)
        load_key.assert_not_called()
        transport.assert_not_called()

    def test_probe_uses_saved_credential_without_export_or_secret_output(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / '.config/codex-deepseek/api-key'
            path.parent.mkdir(mode=0o700, parents=True)
            value = secrets.token_hex(24)
            path.write_text(value + '\n')
            path.chmod(0o600)
            env = dict(os.environ, HOME=directory)
            env.pop('DEEPSEEK_API_KEY', None)
            opener = mock.Mock()
            opener.open.side_effect = [
                io.BytesIO(b'{"data":[{"id":"deepseek-flash"}]}'),
                io.BytesIO(b'data: {"response":{"model":"deepseek-flash"}}\n'),
            ]
            with mock.patch.dict(os.environ, env, clear=True), \
                 mock.patch('urllib.request.build_opener', return_value=opener), \
                 mock.patch('sys.stdout', new_callable=io.StringIO) as output:
                self.assertEqual(check.api_probe(), 0)
            self.assertEqual(opener.open.call_count, 2)
            for call in opener.open.call_args_list:
                self.assertEqual(call.args[0].get_header('Authorization'), 'Bearer ' + value)
            self.assertNotIn(value, output.getvalue())

    def test_stream_metadata_does_not_wait_for_completion_or_expose_reasoning(self):
        events = [b': keepalive\n', b'\n',
                  b'data: {"type":"response.created","response":{"model":"deepseek-flash","status":"in_progress"}}\n',
                  b'data: {"type":"response.reasoning_text.delta","delta":"private reasoning"}\n']
        self.assertEqual(check.stream_model(events), 'deepseek-flash')

    def test_unavailable_model_stops_before_inference(self):
        opener = mock.Mock()
        context = mock.MagicMock()
        context.__enter__.return_value = io.BytesIO(b'{"data":[{"id":"another-model"}]}')
        opener.open.return_value = context
        with mock.patch.dict(os.environ, {'DEEPSEEK_API_KEY': secrets.token_hex(24)}), \
             mock.patch('urllib.request.build_opener', return_value=opener), \
             mock.patch('sys.stdout', new_callable=io.StringIO):
            self.assertEqual(check.api_probe(), 78)
        self.assertEqual(opener.open.call_count, 1, 'Unavailable model must not start inference')

    def test_probe_error_classifies_failure_without_server_text(self):
        cases = [(TimeoutError('private error text'), 'TimeoutError'),
                 (urllib.error.URLError(socket.gaierror('private error text')), 'gaierror'),
                 (json.JSONDecodeError('private error text', 'private body', 0), 'InvalidJSONResponse')]
        for error, expected in cases:
            with self.subTest(expected=expected):
                result = check.probe_error(error, 45)
                self.assertIn(expected, result)
                self.assertIn('45.0s', result)
                self.assertNotIn('private', result)

    def test_fingerprint_detects_changes_to_already_untracked_file(self):
        previous = Path.cwd()
        with tempfile.TemporaryDirectory() as directory:
            try:
                os.chdir(directory)
                subprocess.run(['git', 'init', '-q'], check=True)
                path = Path('untracked.txt')
                path.write_text('before')
                fingerprint = check.repository_fingerprint()
                status = subprocess.check_output(['git', 'status', '--porcelain'])
                path.write_text('after')
                self.assertEqual(status, subprocess.check_output(['git', 'status', '--porcelain']))
                self.assertNotEqual(fingerprint, check.repository_fingerprint())
            finally:
                os.chdir(previous)


if __name__ == '__main__':
    unittest.main()
