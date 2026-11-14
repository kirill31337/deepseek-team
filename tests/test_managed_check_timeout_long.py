"""Real command timeout regressions, including the 121-second acceptance."""
from pathlib import Path
import os
import shlex
import sys
import tempfile
import time
import unittest
from codex_deepseek_team import check_evidence
from tests.test_check_reuse import LAUNCHER


class RealCheckTimeoutTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='dst-managed-timeout-raw-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.env = {
            'PATH': os.environ.get('PATH', '/usr/local/bin:/usr/bin:/bin'),
            'LANG': 'C.UTF-8',
        }

    def run_checks(self, command, *, timeout, deadline=None):
        directory = self.root / 'evidence'
        return check_evidence.run_checks(
            [LAUNCHER], self.env, [command], timeout=timeout,
            deadline=deadline, directory=directory)

    def test_short_per_command_timeout_records_the_used_limit(self):
        started = time.monotonic()
        rows = self.run_checks('sleep 2', timeout=0.2)
        elapsed = time.monotonic() - started
        self.assertEqual(rows[0]['exit_code'], 124)
        self.assertTrue(rows[0]['timed_out'])
        self.assertEqual(rows[0]['check_timeout_seconds'], 0.2)
        self.assertLess(elapsed, 1.5)

    def test_total_deadline_clamps_the_configured_check_timeout(self):
        started = time.monotonic()
        rows = self.run_checks(
            'sleep 2', timeout=5, deadline=time.monotonic() + 0.3)
        elapsed = time.monotonic() - started
        self.assertEqual(rows[0]['exit_code'], 124)
        self.assertTrue(rows[0]['timed_out'])
        self.assertEqual(rows[0]['check_timeout_seconds'], 5.0)
        self.assertLess(elapsed, 1.5)

    def test_real_command_longer_than_120_seconds_completes_with_180_limit(self):
        command = f'{shlex.quote(sys.executable)} -c ' + shlex.quote(
            'import time; time.sleep(121); print("long-complete")')
        started = time.monotonic()
        rows = self.run_checks(command, timeout=180)
        elapsed = time.monotonic() - started
        self.assertEqual(rows[0]['exit_code'], 0)
        self.assertFalse(rows[0]['timed_out'])
        self.assertEqual(rows[0]['check_timeout_seconds'], 180.0)
        self.assertGreater(elapsed, 120)
