"""Worker integration with the Linux OS sandbox layer."""
import io
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest import mock

from codex_deepseek_team import worker


class FakeSandboxModule:
    class SandboxError(Exception):
        def __init__(self, code, message):
            self.code, self.message = code, message

    def __init__(self):
        self.wrap_calls = []
        self.codex_calls = []

    def wrap_command(self, command, **kwargs):
        self.wrap_calls.append((list(command), kwargs))
        return ['/usr/bin/bwrap', '--', *command]

    def prepare_codex_environment(self, session_home, env, backend):
        self.codex_calls.append((Path(session_home), dict(env), backend))
        result = dict(env)
        result['PATH'] = str(Path(session_home) / 'bwrap-bin') + os.pathsep + result.get('PATH', '')
        return result


class WorkerOsSandboxTests(unittest.TestCase):
    def make_args(self, root, **overrides):
        values = dict(
            runtime='claude', codex='codex', claude='claude', os_sandbox='required',
            task='bounded task', state_dir=Path(root) / 'state',
            timeout=0, attempts=1,
        )
        values.update(overrides)
        values['state_dir'].mkdir(mode=0o700, parents=True, exist_ok=True)
        return SimpleNamespace(**values)

    def test_required_sandbox_failure_happens_before_api_key_read(self):
        with tempfile.TemporaryDirectory() as directory:
            args = self.make_args(directory)
            with mock.patch.object(worker, 'resolve_runtime', return_value=('claude', '/usr/bin/claude')), \
                 mock.patch.object(worker, 'resolve_os_sandbox', create=True,
                                   side_effect=worker.WorkerError(78, 'sandbox unavailable')), \
                 mock.patch.object(worker, 'load_api_key', side_effect=AssertionError('key read too early')) as key:
                with self.assertRaises(worker.WorkerError) as caught:
                    worker.run_worker(args)
            self.assertEqual(caught.exception.code, 78)
            key.assert_not_called()

    def test_claude_runtime_is_wrapped_by_outer_bubblewrap(self):
        fake = FakeSandboxModule()
        backend = object()
        with tempfile.TemporaryDirectory() as directory:
            args = self.make_args(directory)
            fd = os.open(os.devnull, os.O_RDONLY)
            self.addCleanup(lambda: _safe_close(fd))
            with mock.patch.object(worker, 'resolve_runtime', return_value=('claude', '/usr/bin/claude')), \
                 mock.patch.object(worker, 'resolve_os_sandbox', create=True, return_value=(fake, backend)), \
                 mock.patch.object(worker, 'load_api_key', return_value='KEY'), \
                 mock.patch.object(worker, 'acquire_slot', return_value=fd), \
                 mock.patch.object(worker, 'execute', return_value=(0, '{"type":"result","is_error":false,"result":"ok"}', '')) as execute:
                with mock.patch('sys.stdout', new=io.StringIO()):
                    code = worker.run_worker(args)
            self.assertEqual(code, 0)
            self.assertEqual(len(fake.wrap_calls), 1)
            wrapped_command, kwargs = fake.wrap_calls[0]
            self.assertEqual(wrapped_command[0], '/usr/bin/claude')
            self.assertNotIn('writable', kwargs)
            self.assertEqual(execute.call_args.args[0][:3], ['/usr/bin/bwrap', '--', '/usr/bin/claude'])
            self.assertEqual(fake.codex_calls, [])

    def test_codex_uses_native_sandbox_with_prepared_bwrap_environment(self):
        fake = FakeSandboxModule()
        backend = object()
        with tempfile.TemporaryDirectory() as directory:
            args = self.make_args(directory, runtime='codex')
            fd = os.open(os.devnull, os.O_RDONLY)
            self.addCleanup(lambda: _safe_close(fd))
            with mock.patch.object(worker, 'resolve_runtime', return_value=('codex', '/usr/bin/codex')), \
                 mock.patch.object(worker, 'resolve_os_sandbox', create=True, return_value=(fake, backend)), \
                 mock.patch.object(worker, 'provider_config', return_value={}), \
                 mock.patch.object(worker, 'load_api_key', return_value='KEY'), \
                 mock.patch.object(worker, 'acquire_slot', return_value=fd), \
                 mock.patch.object(worker, 'execute', return_value=(0, '{"type":"turn.completed"}\n{"type":"item.completed","item":{"type":"agent_message","text":"ok"}}', '')) as execute:
                with mock.patch('sys.stdout', new=io.StringIO()):
                    code = worker.run_worker(args)
            self.assertEqual(code, 0)
            self.assertEqual(fake.wrap_calls, [])
            self.assertEqual(len(fake.codex_calls), 1)
            self.assertTrue(execute.call_args.args[1]['PATH'].endswith(os.pathsep + os.environ.get('PATH', '')) or 'bwrap-bin' in execute.call_args.args[1]['PATH'])

    def test_parse_args_requires_os_sandbox_by_default_and_accepts_explicit_off(self):
        with mock.patch.object(os, 'environ', dict(os.environ)), \
             mock.patch('sys.argv', ['worker.py', 'task']):
            parsed = worker.parse_args()
        self.assertEqual(parsed.os_sandbox, 'required')
        with mock.patch('sys.argv', ['worker.py', '--os-sandbox', 'off', 'task']):
            parsed = worker.parse_args()
        self.assertEqual(parsed.os_sandbox, 'off')


def _safe_close(fd):
    try:
        os.close(fd)
    except OSError:
        pass


if __name__ == '__main__':
    unittest.main()
