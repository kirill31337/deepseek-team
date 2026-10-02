"""Behavioral tests for bounded check evidence and neutral classification.

The tests run real disposable ``/bin/sh`` commands through the same
``args -- /bin/sh -lc COMMAND`` boundary used by the managed runner.  The
``env`` utility stands in for the namespace launcher so no nested Bubblewrap
is required; nested-sandbox limitations are reported by the runtime-preflight
tests instead.
"""
from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import stat
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from codex_deepseek_team import check_evidence

LAUNCHER = shutil.which('env') or '/usr/bin/env'


class CheckEvidenceTests(unittest.TestCase):
    def execute(self, commands, **kwargs):
        env = kwargs.pop('env', None)
        if env is None:
            env = os.environ.copy()
        return check_evidence.run_checks([LAUNCHER], env, commands, **kwargs)

    @staticmethod
    def process_is_gone(pid, timeout=3.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                with open(f'/proc/{pid}/stat', 'rb') as handle:
                    state = handle.read().rsplit(b') ', 1)[1].split(b' ', 1)[0]
            except (FileNotFoundError, ProcessLookupError):
                return True
            except (IndexError, OSError):
                return True
            if state == b'Z':
                return True
            time.sleep(0.05)
        return False

    # ------------------------------------------------------------------
    # Legacy row compatibility
    # ------------------------------------------------------------------
    def test_legacy_rows_are_exactly_command_and_exit_code(self):
        rows = self.execute(['printf first', 'exit 3', 'printf never'])
        self.assertEqual(rows, [{'command': 'printf first', 'exit_code': 0},
                                {'command': 'exit 3', 'exit_code': 3}])
        for row in rows:
            self.assertEqual(set(row), set(check_evidence.LEGACY_ROW_KEYS))

    def test_legacy_success(self):
        rows = self.execute(['printf ok'])
        self.assertEqual(rows, [{'command': 'printf ok', 'exit_code': 0}])

    def test_legacy_deadline_exhaustion_is_exactly_a_timeout_row(self):
        marker = Path(tempfile.mkdtemp()) / 'ran'
        rows = self.execute([f'touch {shlex.quote(str(marker))}'],
                            deadline=time.monotonic() - 1)
        self.assertEqual(rows, [{'command': f'touch {shlex.quote(str(marker))}',
                                 'exit_code': 124}])
        self.assertFalse(marker.exists())

    # ------------------------------------------------------------------
    # Launch failures and timeouts
    # ------------------------------------------------------------------
    def test_launch_failure_is_environment_evidence_in_legacy_mode(self):
        rows = check_evidence.run_checks(['/nonexistent/ns-launcher-xyz'],
                                         os.environ.copy(), ['printf x'])
        self.assertEqual(rows, [{'command': 'printf x', 'exit_code': 125}])
        self.assertEqual(check_evidence.classify_checks([], rows),
                         {'kind': 'environment', 'cause': 'launch_failure'})

    def test_launch_failure_directory_row_has_no_claimed_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / 'evidence'
            rows = check_evidence.run_checks(['/nonexistent/ns-launcher-xyz'],
                                             os.environ.copy(), ['printf x'],
                                             directory=directory)
            self.assertEqual(len(rows), 1)
            row = rows[0]
            self.assertEqual(set(row), set(check_evidence.EVIDENCE_ROW_KEYS))
            self.assertEqual(row['exit_code'], 125)
            self.assertTrue(row['launch_failed'])
            self.assertFalse(row['timed_out'])
            self.assertIsNone(row['stdout_path'])
            self.assertIsNone(row['stderr_path'])
            self.assertIsNone(row['stdout_sha256'])
            self.assertIsNone(row['stderr_sha256'])
            self.assertEqual(check_evidence.classify_checks([], rows),
                             {'kind': 'environment', 'cause': 'launch_failure'})

    def test_timeout_kills_process_group_and_keeps_partial_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            pid_file = Path(tmp) / 'child.pid'
            directory = Path(tmp) / 'evidence'
            command = f'printf partial-before-timeout; sleep 30 & echo $! > {shlex.quote(str(pid_file))}; wait'
            started = time.monotonic()
            rows = self.execute([command], timeout=1, directory=directory)
            elapsed = time.monotonic() - started
            self.assertLess(elapsed, 10)
            self.assertEqual(rows[0]['exit_code'], 124)
            self.assertTrue(rows[0]['timed_out'])
            self.assertFalse(rows[0]['launch_failed'])
            stdout = (directory / rows[0]['stdout_path']).read_bytes()
            self.assertIn(b'partial-before-timeout', stdout)
            child_pid = int(pid_file.read_text())
            self.assertTrue(self.process_is_gone(child_pid),
                            f'child {child_pid} survived the process-group kill')
            self.assertEqual(check_evidence.classify_checks([], rows),
                             {'kind': 'environment', 'cause': 'timeout'})

    def test_deadline_limits_running_command(self):
        started = time.monotonic()
        rows = self.execute(['sleep 30'], timeout=120,
                            deadline=time.monotonic() + 0.6)
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 10)
        self.assertEqual(rows[0]['exit_code'], 124)

    def test_directory_deadline_exhaustion_runs_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / 'ran'
            directory = Path(tmp) / 'evidence'
            command = f'touch {shlex.quote(str(marker))}'
            rows = self.execute([command], deadline=time.monotonic() - 1,
                                directory=directory)
            self.assertEqual(rows[0]['exit_code'], 124)
            self.assertTrue(rows[0]['timed_out'])
            self.assertIsNone(rows[0]['stdout_path'])
            self.assertIsNone(rows[0]['stderr_path'])
            self.assertFalse(marker.exists())

    # ------------------------------------------------------------------
    # Evidence publication
    # ------------------------------------------------------------------
    def test_directory_rows_publish_atomic_owner_only_relative_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / 'evidence'
            rows = self.execute(['printf out; printf err >&2'],
                                directory=directory, phase='baseline')
            self.assertEqual(len(rows), 1)
            row = rows[0]
            self.assertEqual(set(row), set(check_evidence.EVIDENCE_ROW_KEYS))
            self.assertEqual(row['phase'], 'baseline')
            self.assertEqual(row['exit_code'], 0)
            self.assertFalse(row['stdout_truncated'])
            self.assertFalse(row['stderr_truncated'])
            self.assertFalse(row['timed_out'])
            self.assertFalse(row['launch_failed'])
            for key in ('stdout_path', 'stderr_path'):
                value = row[key]
                self.assertIsInstance(value, str)
                self.assertFalse(os.path.isabs(value))
                self.assertNotIn('..', Path(value).parts)
                self.assertNotIn('/', value)
                self.assertTrue(value.startswith('baseline-'))
            stdout_file = directory / row['stdout_path']
            stderr_file = directory / row['stderr_path']
            self.assertEqual(stdout_file.read_bytes(), b'out')
            self.assertEqual(stderr_file.read_bytes(), b'err')
            self.assertEqual(hashlib.sha256(b'out').hexdigest(), row['stdout_sha256'])
            self.assertEqual(hashlib.sha256(b'err').hexdigest(), row['stderr_sha256'])
            self.assertEqual(stat.S_IMODE(stdout_file.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(stderr_file.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
            self.assertEqual(sorted(path.name for path in directory.iterdir()),
                             sorted([row['stdout_path'], row['stderr_path']]))

    def test_evidence_file_names_are_unique_per_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / 'evidence'
            first = self.execute(['printf one'], directory=directory)
            second = self.execute(['printf two'], directory=directory)
            names = [first[0]['stdout_path'], first[0]['stderr_path'],
                     second[0]['stdout_path'], second[0]['stderr_path']]
            self.assertEqual(len(set(names)), 4)
            for name in names:
                self.assertTrue((directory / name).exists())

    def test_publication_failure_is_explicit_and_claims_no_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            blocker = Path(tmp) / 'not-a-directory'
            blocker.write_text('occupied')
            with self.assertRaises(check_evidence.CheckEvidenceError) as caught:
                self.execute(['printf x'], directory=blocker)
            self.assertEqual(len(caught.exception.args), 1)
            self.assertTrue(str(caught.exception.args[0]))

    def test_unsafe_phase_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(check_evidence.CheckEvidenceError):
                self.execute(['printf x'], directory=Path(tmp) / 'evidence',
                             phase='../escape')

    def test_full_environment_is_never_stored(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / 'evidence'
            env = os.environ.copy()
            env['DEEPSEEK_TEST_CANARY'] = 'canary-value-9c1f'
            rows = self.execute(['printf ok'], directory=directory, env=env)
            blob = json.dumps(rows)
            for name in (rows[0]['stdout_path'], rows[0]['stderr_path']):
                blob += (directory / name).read_text()
            self.assertNotIn('canary-value-9c1f', blob)

    # ------------------------------------------------------------------
    # Bounded, redacted capture
    # ------------------------------------------------------------------
    def test_output_is_bounded_per_stream_and_truncation_recorded(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / 'evidence'
            command = 'yes a | head -c 5000000; yes b | head -c 5000000 >&2'
            rows = self.execute([command], directory=directory, timeout=60)
            row = rows[0]
            self.assertEqual(row['exit_code'], 0)
            self.assertTrue(row['stdout_truncated'])
            self.assertTrue(row['stderr_truncated'])
            stdout = (directory / row['stdout_path']).read_bytes()
            stderr = (directory / row['stderr_path']).read_bytes()
            self.assertEqual(len(stdout), check_evidence.MAX_OUTPUT_BYTES)
            self.assertEqual(len(stderr), check_evidence.MAX_OUTPUT_BYTES)

    def test_truncation_flags_are_per_stream(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / 'evidence'
            rows = self.execute(['yes a | head -c 5000000; printf short >&2'],
                                directory=directory, timeout=60)
            self.assertTrue(rows[0]['stdout_truncated'])
            self.assertFalse(rows[0]['stderr_truncated'])
            self.assertEqual((directory / rows[0]['stderr_path']).read_bytes(),
                             b'short')

    def test_secret_crossing_read_chunks_is_redacted(self):
        secret = 'S3CR3T-abcdef-012345'
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / 'evidence'
            with mock.patch.object(check_evidence, 'READ_CHUNK_SIZE', 4):
                rows = self.execute([f"printf 'pad-{secret}-tail'"],
                                    directory=directory, redact_values=(secret,))
            content = (directory / rows[0]['stdout_path']).read_bytes()
            self.assertNotIn(secret.encode(), content)
            metadata = [{key: value for key, value in row.items() if key != 'command'}
                        for row in rows]
            self.assertNotIn(secret.encode(), json.dumps(metadata).encode())
            self.assertIn(b'[REDACTED]', content)
            self.assertEqual(content, b'pad-[REDACTED]-tail')

    def test_multiple_secrets_across_byte_chunks(self):
        secrets = ('alpha-secret-value', 'beta-secret-value')
        command = "printf 'xalpha-secret-valuey beta-secret-valuez'"
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / 'evidence'
            with mock.patch.object(check_evidence, 'READ_CHUNK_SIZE', 1):
                rows = self.execute([command], directory=directory,
                                    redact_values=secrets)
            content = (directory / rows[0]['stdout_path']).read_bytes()
            self.assertEqual(content, b'x[REDACTED]y [REDACTED]z')
            for secret in secrets:
                self.assertNotIn(secret.encode(), content)

    def test_secret_straddling_retention_boundary_is_not_published(self):
        secret = 'RETENTION-SECRET-VALUE'
        script = (f"import sys; sys.stdout.write('x'*65530 + '{secret}' + 'y'*1000)")
        command = f'{shlex.quote(sys.executable)} -c "{script}"'
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / 'evidence'
            rows = self.execute([command], directory=directory,
                                redact_values=(secret,), timeout=60)
            row = rows[0]
            self.assertTrue(row['stdout_truncated'])
            content = (directory / row['stdout_path']).read_bytes()
            self.assertLessEqual(len(content), check_evidence.MAX_OUTPUT_BYTES)
            self.assertNotIn(secret.encode(), content)
            self.assertTrue(content.startswith(b'x' * 100))
            for length in range(1, len(secret)):
                self.assertFalse(content.endswith(secret[:length].encode()),
                                 f'retained output ends with secret prefix {length}')

    def test_secret_recreated_at_redaction_seam_is_not_published(self):
        secrets = (']abc', 'X')
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / 'evidence'
            with mock.patch.object(check_evidence, 'READ_CHUNK_SIZE', 1):
                rows = self.execute(["printf 'Xabc'"], directory=directory,
                                    redact_values=secrets)
            content = (directory / rows[0]['stdout_path']).read_bytes()
            self.assertNotIn(b']abc', content)
            self.assertNotIn(b'X', content)
            self.assertIn(b'[REDACTED]', content)

    def test_partial_secret_at_end_of_stream_is_redacted(self):
        secret = 'EOF-PARTIAL-SECRET'
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / 'evidence'
            rows = self.execute([f"printf 'before-{secret[:9]}'"],
                                directory=directory, redact_values=(secret,))
            content = (directory / rows[0]['stdout_path']).read_bytes()
            self.assertEqual(content, b'before-[REDACTED]')

    # ------------------------------------------------------------------
    # Classification
    # ------------------------------------------------------------------
    def test_classify_success_has_no_cause(self):
        rows = self.execute(['printf ok'])
        result = check_evidence.classify_checks([], rows)
        self.assertEqual(result, {'kind': None, 'cause': None})
        self.assertEqual(set(result), {'kind', 'cause'})

    def test_classify_empty_inputs_is_success(self):
        self.assertEqual(check_evidence.classify_checks([], []),
                         {'kind': None, 'cause': None})

    def test_baseline_failures_are_evidence_not_rejection(self):
        with tempfile.TemporaryDirectory() as tmp:
            flag = Path(tmp) / 'flag'
            directory = Path(tmp) / 'evidence'
            command = f'test -e {shlex.quote(str(flag))}'
            baseline = self.execute([command], directory=directory, phase='baseline')
            flag.write_text('ready')
            post = self.execute([command], directory=directory, phase='post')
            self.assertNotEqual(baseline[0]['exit_code'], 0)
            self.assertEqual(post[0]['exit_code'], 0)
            self.assertEqual(check_evidence.classify_checks(baseline, post),
                             {'kind': None, 'cause': None})

    def test_matching_legacy_exit_codes_do_not_prove_same_cause(self):
        baseline = [{'command': 'pytest -q', 'exit_code': 3}]
        post = [{'command': 'pytest -q', 'exit_code': 3}]
        self.assertEqual(check_evidence.classify_checks(baseline, post),
                         {'kind': 'verification', 'cause': 'unknown'})

    def test_identical_failure_signature_is_baseline_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / 'evidence'
            command = 'printf out; printf err >&2; exit 7'
            baseline = self.execute([command], directory=directory, phase='baseline')
            post = self.execute([command], directory=directory, phase='post')
            self.assertEqual(check_evidence.classify_checks(baseline, post),
                             {'kind': 'verification', 'cause': 'baseline_failure'})

    def test_same_exit_code_different_output_is_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / 'data.txt'
            directory = Path(tmp) / 'evidence'
            command = f'cat {shlex.quote(str(data))}; exit 5'
            data.write_text('first failure text')
            baseline = self.execute([command], directory=directory, phase='baseline')
            data.write_text('second failure text')
            post = self.execute([command], directory=directory, phase='post')
            self.assertEqual(baseline[0]['exit_code'], post[0]['exit_code'])
            self.assertNotEqual(baseline[0]['stdout_sha256'], post[0]['stdout_sha256'])
            self.assertEqual(check_evidence.classify_checks(baseline, post),
                             {'kind': 'verification', 'cause': 'unknown'})

    def test_truncated_output_signature_is_not_proof(self):
        row = {'command': 'pytest -q', 'exit_code': 1,
               'stdout_truncated': True, 'stderr_truncated': False,
               'stdout_sha256': 'a' * 64, 'stderr_sha256': 'b' * 64}
        self.assertEqual(check_evidence.classify_checks([row], [dict(row)]),
                         {'kind': 'verification', 'cause': 'unknown'})

    def test_green_baseline_then_failing_post_is_regression_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            flag = Path(tmp) / 'flag'
            flag.write_text('ready')
            directory = Path(tmp) / 'evidence'
            command = f'test -e {shlex.quote(str(flag))}'
            baseline = self.execute([command], directory=directory, phase='baseline')
            flag.unlink()
            post = self.execute([command], directory=directory, phase='post')
            self.assertEqual(check_evidence.classify_checks(baseline, post),
                             {'kind': 'verification', 'cause': 'regression_candidate'})

    def test_classify_baseline_only_failure_without_post_rows(self):
        baseline = [{'command': 'pytest -q', 'exit_code': 1}]
        self.assertEqual(check_evidence.classify_checks(baseline, []),
                         {'kind': 'verification', 'cause': 'baseline_failure'})

    def test_failed_probes_are_environment_evidence(self):
        post = [{'command': 'pytest -q', 'exit_code': 0}]
        self.assertEqual(
            check_evidence.classify_checks([], post, probes=[
                {'command': 'java -version', 'exit_code': 1}]),
            {'kind': 'environment', 'cause': 'probe_failure'})
        self.assertEqual(check_evidence.classify_checks([], post, probes=('java',)),
                         {'kind': 'environment', 'cause': 'probe_failure'})
        self.assertEqual(
            check_evidence.classify_checks([], post, probes=[
                {'command': 'java -version', 'exit_code': 0}]),
            {'kind': None, 'cause': None})

    def test_environment_evidence_outranks_verification(self):
        baseline = [{'command': 'pytest -q', 'exit_code': 1}]
        post = [{'command': 'pytest -q', 'exit_code': 1}]
        timed_out = [{'command': 'pytest -q', 'exit_code': 124}]
        self.assertEqual(check_evidence.classify_checks(baseline, timed_out),
                         {'kind': 'environment', 'cause': 'timeout'})

    def test_classifier_only_returns_kind_and_cause(self):
        results = [
            check_evidence.classify_checks([], []),
            check_evidence.classify_checks([], [{'command': 'x', 'exit_code': 2}]),
            check_evidence.classify_checks([], [{'command': 'x', 'exit_code': 124}]),
        ]
        for result in results:
            self.assertEqual(set(result), {'kind', 'cause'})
            self.assertIn(result['kind'], (None, 'environment', 'verification'))
            self.assertNotIn('grade', result)
            self.assertNotIn('quality', result)

    def test_stop_on_first_failure_with_directory_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / 'evidence'
            first = Path(tmp) / 'first'
            never = Path(tmp) / 'never'
            rows = self.execute([f'touch {shlex.quote(str(first))}', 'exit 2',
                                 f'touch {shlex.quote(str(never))}'],
                                directory=directory)
            self.assertEqual([row['exit_code'] for row in rows], [0, 2])
            self.assertTrue(first.exists())
            self.assertFalse(never.exists())
            for row in rows:
                self.assertTrue((directory / row['stdout_path']).exists())


if __name__ == '__main__':
    unittest.main()
