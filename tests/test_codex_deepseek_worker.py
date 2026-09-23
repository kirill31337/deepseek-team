"""Offline regression tests; credentials and transport are synthetic."""
import concurrent.futures
import hashlib
import io
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock


from codex_deepseek_team import sandbox, worker


PACKAGE_ROOT = Path(__file__).resolve().parents[1] / 'src'
# Synthetic CLI processes exercise queues, credentials, retries and cleanup. Keep
# their sandbox doubles in tests; the OS-boundary suites use real containment.
TEST_WORKER_LAUNCHER = """
import sys
from unittest import mock
from codex_deepseek_team import sandbox, worker
with mock.patch.object(sandbox, 'probe_backend', return_value=object()), \\
     mock.patch.object(sandbox, 'prepare_codex_environment', side_effect=lambda home, env, backend: env), \\
     mock.patch.object(sandbox, 'wrap_command', side_effect=lambda command, **kwargs: command):
    sys.exit(worker.main())
"""
FAKE = '''#!/usr/bin/env python3
import hashlib, json, os, pathlib, subprocess, sys, time
if "--help" in sys.argv:
    print("--ephemeral --json --strict-config --ignore-rules")
    sys.exit(0)
if "--version" in sys.argv:
    print("codex-cli fixture")
    sys.exit(0)
root = pathlib.Path(__file__).parent
with (root / "calls").open("a") as f:
    f.write(json.dumps({"args":sys.argv[1:], "cwd":os.getcwd(),
        "home":os.environ["CODEX_HOME"],
        "auth_digest":hashlib.sha256(os.environ["DEEPSEEK_API_KEY"].encode()).hexdigest(),
        "auth_exists":(pathlib.Path(os.environ["CODEX_HOME"])/"auth.json").exists(),
        "extra_secret": "OPENAI_API_KEY" in os.environ}) + "\\n")
mode = pathlib.Path(__file__).name.removeprefix("codex-")
prompt = sys.stdin.read()
if mode in ["events", "events-error"]:
    for event in json.loads(prompt):
        print(json.dumps(event))
    sys.exit(42 if mode == "events-error" else 0)
if mode == "recover" and len((root / "calls").read_text().splitlines()) == 1:
    print(json.dumps({"type":"item.completed", "item":{"type":"agent_message", "text":"partial answer"}}))
    print(json.dumps({"type":"turn.failed", "error":{"message":"429 Too Many Requests"}}))
    sys.exit(0)
if mode == "timeout":
    print("safe progress before timeout", file=sys.stderr, flush=True)
    child = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(120)"])
    (root / "child_pid").write_text(str(child.pid))
    time.sleep(120)
if mode == "rate":
    print("429 Too Many Requests", file=sys.stderr)
    sys.exit(1)
if mode == "error":
    print("fixture failure", file=sys.stderr)
    sys.exit(42)
if mode == "secret":
    print(os.environ["DEEPSEEK_API_KEY"], file=sys.stderr)
    prompt = os.environ["DEEPSEEK_API_KEY"]
if mode == "sleep":
    time.sleep(1)
if mode == "hold":
    while not (root / "release").exists():
        time.sleep(0.01)
print(json.dumps({"type":"item.completed", "item":{"type":"reasoning", "text":"PRIVATE_REASONING"}}))
print(json.dumps({"type":"item.completed", "item":{"type":"agent_message", "text":prompt}}))
print(json.dumps({"type":"turn.completed", "usage":{"input_tokens":1,"output_tokens":1}}))
'''


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.user = self.root / 'user'
        self.user.mkdir()
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        self.state = self.root / 'state'
        self.config = self.user / 'config.toml'
        self.config.write_text('''model = "example-coordinator"
model_reasoning_effort = "xhigh"
[model_providers.deepseek]
name = "DeepSeek"
base_url = "https://api.deepseek.com/"
env_key = "DEEPSEEK_API_KEY"
wire_api = "responses"
''')
        self.config_before = self.config.read_bytes()
        (self.user / 'auth.json').write_text('{"synthetic":true}')
        for mode in ['ok', 'timeout', 'rate', 'error', 'secret', 'sleep', 'hold',
                     'events', 'events-error', 'recover']:
            fake = self.root / f'codex-{mode}'
            fake.write_text(FAKE)
            fake.chmod(0o700)
        self.env = dict(os.environ, DEEPSEEK_API_KEY=secrets.token_hex(24),
                        HOME=str(self.user),
                        XDG_CONFIG_HOME=str(self.user / '.config'),
                        PYTHONPATH=str(PACKAGE_ROOT),
                        CODEX_HOME=str(self.user),
                        OPENAI_API_KEY=secrets.token_hex(24))
        self.env.pop('CODEX_DEEPSEEK_DISABLED', None)
        self.env.pop('DEEPSEEK_TEAM_DISABLED', None)

    def run_worker(self, task='fixture task', mode='ok', **kwargs):
        env = dict(self.env)
        env.update(kwargs.pop('env', {}))
        return subprocess.run(
            [sys.executable, '-c', TEST_WORKER_LAUNCHER, '--access', 'read-only',
             '--codex', str(self.root / f'codex-{mode}'),
             '--state-dir', str(self.state), *kwargs.pop('args', [])],
            input=task, text=True, capture_output=True, env=env,
            cwd=self.repo, timeout=15, **kwargs)

    def calls(self):
        p = self.root / 'calls'
        return [json.loads(line) for line in p.read_text().splitlines()] if p.exists() else []

    def test_missing_key_stops_before_codex(self):
        r = self.run_worker(env={'DEEPSEEK_API_KEY': ''})
        self.assertEqual(r.returncode, 78, r.stderr)
        self.assertIn('DEEPSEEK_API_KEY', r.stderr)
        self.env.pop('DEEPSEEK_API_KEY')
        r = self.run_worker()
        self.assertEqual(r.returncode, 78, r.stderr)
        self.assertEqual(self.calls(), [])

    def saved_key(self):
        path = self.user / '.config/codex-deepseek/api-key'
        path.parent.mkdir(mode=0o700, parents=True)
        value = secrets.token_hex(24)
        path.write_text(value + '\n')
        path.chmod(0o600)
        return path, value

    def test_saved_key_works_without_export_and_is_redacted(self):
        path, value = self.saved_key()
        self.env.pop('DEEPSEEK_API_KEY')
        r = self.run_worker(mode='secret')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.calls()[0]['auth_digest'], hashlib.sha256(value.encode()).hexdigest())
        self.assertNotIn(value, r.stdout + r.stderr)
        self.assertIn('[REDACTED]', r.stdout)
        self.assertEqual(path.read_text(), value + '\n')
        self.assertFalse(list(self.state.glob('session-*')))

    def test_environment_overrides_saved_key_even_when_file_is_unsafe(self):
        path, _ = self.saved_key()
        path.chmod(0o644)
        r = self.run_worker()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.calls()[0]['auth_digest'], hashlib.sha256(self.env['DEEPSEEK_API_KEY'].encode()).hexdigest())
        r = self.run_worker(env={'DEEPSEEK_API_KEY': ''})
        self.assertEqual(r.returncode, 78, 'Explicit empty env must not load a saved key')

    def test_unsafe_saved_credentials_fail_closed(self):
        self.env.pop('DEEPSEEK_API_KEY')
        path, value = self.saved_key()
        cases = ['file_permissions', 'directory_permissions', 'symlink', 'directory_symlink',
                 'fifo', 'empty', 'multiline', 'oversized']
        for case in cases:
            with self.subTest(case=case):
                path.parent.chmod(0o700)
                if path.exists() or path.is_symlink():
                    path.unlink()
                path.write_text(value + '\n')
                path.chmod(0o600)
                if case == 'file_permissions':
                    path.chmod(0o644)
                elif case == 'directory_permissions':
                    path.parent.chmod(0o755)
                elif case == 'symlink':
                    target = path.with_name('other-key')
                    path.rename(target)
                    path.symlink_to(target)
                elif case == 'directory_symlink':
                    target = path.parent.with_name('other-credentials')
                    path.parent.rename(target)
                    path.parent.symlink_to(target, target_is_directory=True)
                elif case == 'fifo':
                    path.unlink()
                    os.mkfifo(path, 0o600)
                elif case == 'empty':
                    path.write_text('')
                elif case == 'multiline':
                    path.write_text(value + '\nextra')
                elif case == 'oversized':
                    path.write_text('x' * 5000)
                r = self.run_worker()
                self.assertEqual(r.returncode, 78)
                self.assertNotIn(value, r.stdout + r.stderr)
                self.assertIn('credential', r.stderr.lower())
                if case == 'directory_symlink':
                    path.parent.unlink()
                    target.rename(path.parent)
        self.assertEqual(self.calls(), [])

    def test_invalid_provider_stops_before_codex(self):
        self.config.write_text(self.config.read_text().replace('https://api.deepseek.com/', 'https://example.invalid/'))
        r = self.run_worker()
        self.assertEqual(r.returncode, 78, r.stderr)
        self.assertEqual(self.calls(), [])

    def test_disabled_delegation(self):
        path, _ = self.saved_key()
        path.chmod(0o644)
        self.env.pop('DEEPSEEK_API_KEY')
        r = self.run_worker(env={'DEEPSEEK_TEAM_DISABLED': '1'})
        self.assertEqual(r.returncode, 69)
        self.assertEqual(self.calls(), [])

    def test_obsolete_disable_variable_does_not_prevent_worker_execution(self):
        result = self.run_worker(env={'CODEX_DEEPSEEK_DISABLED': '1'})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'fixture task')

    def test_explicit_routing_isolation_and_no_reasoning_output(self):
        r = self.run_worker()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), 'fixture task')
        c, = self.calls()
        args = c['args']
        self.assertIn('model_provider="deepseek"', args)
        self.assertIn('deepseek-flash', args)
        self.assertIn('model_reasoning_effort="medium"', args)
        self.assertIn('read-only', args)
        self.assertIn('--ephemeral', args)
        self.assertNotIn('fixture task', args)
        self.assertEqual(c['cwd'], str(self.repo))
        self.assertFalse(c['auth_exists'])
        self.assertFalse(c['extra_secret'])
        self.assertFalse(Path(c['home']).exists())
        self.assertEqual(Path(c['home']).parent, self.state)
        self.assertEqual(self.config.read_bytes(), self.config_before)
        self.assertEqual((self.user / 'auth.json').read_text(), '{"synthetic":true}')
        self.assertNotIn('PRIVATE_REASONING', r.stdout + r.stderr)

    def test_saved_forced_effort_applies_without_one_job_override(self):
        config = self.user / '.config/deepseek-team/config.toml'
        config.parent.mkdir(parents=True)
        config.write_text('effort = "high"\n')
        r = self.run_worker(env={'XDG_CONFIG_HOME': str(self.user / '.config')})
        self.assertEqual(r.returncode, 0, r.stderr)
        c, = self.calls()
        self.assertIn('model_reasoning_effort="high"', c['args'])
        self.assertIn('effort: high', r.stderr)

    def test_frontier_can_select_high_effort_without_changing_model(self):
        r = self.run_worker(args=['--effort', 'high'])
        self.assertEqual(r.returncode, 0, r.stderr)
        c, = self.calls()
        self.assertIn('deepseek-flash', c['args'])
        self.assertIn('model_reasoning_effort="high"', c['args'])

    def test_key_is_redacted_from_both_streams(self):
        r = self.run_worker(mode='secret')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn(self.env['DEEPSEEK_API_KEY'], r.stdout + r.stderr)
        self.assertIn('[REDACTED]', r.stdout + r.stderr)

    def test_retry_is_bounded_and_nonretryable_exit_is_preserved(self):
        r = self.run_worker(mode='rate', args=['--attempts', '2'])
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertEqual(len(self.calls()), 2)
        (self.root / 'calls').unlink()
        r = self.run_worker(mode='error')
        self.assertEqual(r.returncode, 42)
        self.assertEqual(len(self.calls()), 1)

    def test_incomplete_answer_is_not_published(self):
        events = [{'type': 'item.completed',
                   'item': {'type': 'agent_message', 'text': 'partial answer'}}]
        r = self.run_worker(task=json.dumps(events), mode='events')
        self.assertEqual(r.returncode, 70, r.stderr)
        self.assertEqual(r.stdout, '')
        self.assertNotIn('partial answer', r.stderr)
        self.assertEqual(len(self.calls()), 1)
        self.assertFalse(list(self.state.glob('session-*')))

    def test_failed_process_does_not_publish_a_completed_message(self):
        events = [{'type': 'item.completed',
                   'item': {'type': 'agent_message', 'text': 'untrusted answer'}},
                  {'type': 'turn.completed'}]
        r = self.run_worker(task=json.dumps(events), mode='events-error')
        self.assertEqual(r.returncode, 42, r.stderr)
        self.assertEqual(r.stdout, '')
        self.assertEqual(len(self.calls()), 1)

    def test_whitespace_only_answer_is_incomplete(self):
        events = [{'type': 'item.completed',
                   'item': {'type': 'agent_message', 'text': ' \n\t'}},
                  {'type': 'turn.completed'}]
        r = self.run_worker(task=json.dumps(events), mode='events')
        self.assertEqual(r.returncode, 70, r.stderr)
        self.assertEqual(r.stdout, '')

    def test_malformed_events_fail_without_traceback_or_raw_output(self):
        invalid = [None, 1, [], 'untrusted event',
                   {'type': 'item.completed', 'item': None},
                   {'type': 'item.completed',
                    'item': {'type': 'agent_message', 'text': {'private': 'untrusted event'}}},
                   {'type': 'error', 'message': ['untrusted event']}]
        for event in invalid:
            with self.subTest(event=event):
                r = self.run_worker(task=json.dumps([event]), mode='events')
                self.assertEqual(r.returncode, 70, r.stderr)
                self.assertEqual(r.stdout, '')
                self.assertNotIn('Traceback', r.stderr)
                self.assertNotIn('untrusted event', r.stderr)
                self.assertFalse(list(self.state.glob('session-*')))

    def test_terminal_failure_overrides_completion_and_redacts_error(self):
        secret = self.env['DEEPSEEK_API_KEY']
        events = [{'type': 'item.completed',
                   'item': {'type': 'agent_message', 'text': 'untrusted answer'}},
                  {'type': 'turn.completed'},
                  {'type': 'turn.failed', 'error': {'message': 'failure ' + secret}}]
        r = self.run_worker(task=json.dumps(events), mode='events')
        self.assertEqual(r.returncode, 70, r.stderr)
        self.assertEqual(r.stdout, '')
        self.assertNotIn(secret, r.stderr)
        self.assertIn('[REDACTED]', r.stderr)

    def test_transient_failure_discards_partial_answer_before_success(self):
        r = self.run_worker(task='complete answer', mode='recover')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), 'complete answer')
        self.assertNotIn('partial answer', r.stdout + r.stderr)
        self.assertEqual(len(self.calls()), 2)
        self.assertFalse(list(self.state.glob('session-*')))

    def test_default_waits_for_result_after_long_elapsed_time(self):
        # A real child takes one second, while the deadline clock sees 1000.
        # The old 180-second default kills it instead of returning its answer.
        clock = time.monotonic
        argv = ['worker', '--access', 'read-only', '--codex', str(self.root / 'codex-sleep'),
                '--state-dir', str(self.state), 'delayed answer']
        with mock.patch.dict(os.environ, self.env, clear=True), \
             mock.patch.object(sys, 'argv', argv), \
             mock.patch.object(sandbox, 'probe_backend', return_value=object()), \
             mock.patch.object(sandbox, 'prepare_codex_environment', side_effect=lambda home, env, backend: env), \
             mock.patch.object(subprocess, '_time', side_effect=lambda: clock() * 1000), \
             mock.patch('sys.stdout', new_callable=io.StringIO) as output, \
             mock.patch('sys.stderr', new_callable=io.StringIO) as errors:
            code = worker.run(worker.parse_args())
        self.assertEqual(code, 0, errors.getvalue())
        self.assertEqual(output.getvalue().strip(), 'delayed answer')
        self.assertEqual(len(self.calls()), 1, 'A working child must not be restarted')

    def test_zero_timeout_waits_for_result(self):
        r = self.run_worker(mode='sleep', args=['--timeout', '0'])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), 'fixture task')

    def test_unlimited_worker_can_be_cancelled_and_cleans_up(self):
        process = subprocess.Popen(
            [sys.executable, '-c', TEST_WORKER_LAUNCHER, '--access', 'read-only',
             '--codex', str(self.root / 'codex-timeout'),
             '--state-dir', str(self.state), 'fixture task'],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=self.env,
            cwd=self.repo)
        try:
            deadline = time.monotonic() + 5
            while not (self.root / 'child_pid').exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue((self.root / 'child_pid').exists(), 'Child did not start')
        finally:
            process.terminate()
            out, err = process.communicate(timeout=5)
        self.assertEqual(process.returncode, 130, err)
        self.assertFalse(list(self.state.glob('session-*')))
        pid = int((self.root / 'child_pid').read_text())
        p = Path(f'/proc/{pid}/stat')
        self.assertTrue(not p.exists() or p.read_text().split()[2] == 'Z')
        self.assertEqual(self.run_worker().returncode, 0, 'Cancellation left the runner unusable')

    def test_timeout_terminates_process_group(self):
        started = time.monotonic()
        r = self.run_worker(mode='timeout', args=['--timeout', '1'])
        self.assertEqual(r.returncode, 124, r.stderr)
        self.assertIn('safe progress before timeout', r.stderr)
        self.assertLess(time.monotonic() - started, 6)
        pid = int((self.root / 'child_pid').read_text())
        p = Path(f'/proc/{pid}/stat')
        self.assertTrue(not p.exists() or p.read_text().split()[2] == 'Z')

    def test_two_independent_workers_and_three_slot_limit(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            pending = []
            try:
                for n in range(3):
                    pending.append(pool.submit(self.run_worker, task=f'task {n}', mode='hold',
                        args=['--max-workers', '3', '--no-wait']))
                    deadline = time.monotonic() + 5
                    while len(self.calls()) < n + 1 and time.monotonic() < deadline:
                        time.sleep(0.01)
                    self.assertEqual(len(self.calls()), n + 1, 'Worker did not acquire its slot')
                refused = self.run_worker(args=['--max-workers', '3', '--no-wait'])
            finally:
                (self.root / 'release').touch()
            results = [future.result() for future in pending] + [refused]
        self.assertEqual(sorted(r.returncode for r in results), [0, 0, 0, 75])
        successes = [r.stdout.strip() for r in results if r.returncode == 0]
        self.assertEqual(len(set(successes)), 3)
        self.assertEqual(len({c['home'] for c in self.calls()}), 3)

    def test_more_jobs_than_capacity_wait_and_all_finish(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
            results = list(pool.map(lambda n: self.run_worker(task=f'queued {n}', mode='sleep',
                args=['--max-workers', '2']), range(5)))
        self.assertEqual([r.returncode for r in results], [0] * 5,
                         '\n'.join(r.stderr for r in results if r.returncode))
        self.assertEqual(len(self.calls()), 5)
        self.assertTrue(any('Queued:' in r.stderr for r in results))


if __name__ == '__main__':
    unittest.main()
