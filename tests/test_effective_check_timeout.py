"""Effective managed-check timeout evidence regressions.

The configured check timeout is the nominal limit that participates in reuse
identity.  The effective timeout is the budget actually handed to the command
supervisor after the job deadline clamp; expired deadlines use zero and launch
nothing.  These tests keep the distinction visible in runtime, ledger and cache
paths.
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import shutil
import tempfile
import time
import unittest

from codex_deepseek_team import check_evidence, coordination
from tests.test_check_reuse import CheckReuseCase, LAUNCHER


class RealEffectiveTimeoutTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='dst-effective-timeout-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.env = {
            'PATH': os.environ.get('PATH', '/usr/local/bin:/usr/bin:/bin'),
            'LANG': 'C.UTF-8',
        }

    def run_checks(self, command, **kwargs):
        return check_evidence.run_checks(
            [LAUNCHER], self.env, [command],
            directory=self.root / 'evidence', **kwargs)

    def test_total_deadline_clamp_reports_the_supervisor_budget(self):
        started = time.monotonic()
        rows = self.run_checks(
            'sleep 2', timeout=1800, deadline=time.monotonic() + 0.2)
        elapsed = time.monotonic() - started
        row = rows[0]
        self.assertEqual(row['exit_code'], 124)
        self.assertTrue(row['timed_out'])
        self.assertEqual(row['check_timeout_seconds'], 1800.0)
        self.assertGreater(row['effective_timeout_seconds'], 0)
        self.assertLessEqual(row['effective_timeout_seconds'], 0.2)
        self.assertLess(elapsed, 1.5)

    def test_unclamped_effective_timeout_equals_the_configured_value(self):
        row = self.run_checks('printf ok', timeout=2.5)[0]
        self.assertEqual(row['check_timeout_seconds'], 2.5)
        self.assertEqual(row['effective_timeout_seconds'], 2.5)
        future = self.run_checks(
            'printf ok', timeout=2.5, deadline=time.monotonic() + 30)[0]
        self.assertEqual(future['check_timeout_seconds'], 2.5)
        self.assertEqual(future['effective_timeout_seconds'], 2.5)

    def test_expired_deadline_records_zero_and_does_not_launch(self):
        marker = self.root / 'ran'
        row = self.run_checks(
            f'touch {marker}', timeout=1800,
            deadline=time.monotonic() - 1)[0]
        self.assertEqual(row['exit_code'], 124)
        self.assertTrue(row['timed_out'])
        self.assertEqual(row['check_timeout_seconds'], 1800.0)
        self.assertEqual(row['effective_timeout_seconds'], 0.0)
        self.assertIsNone(row['stdout_path'])
        self.assertIsNone(row['stderr_path'])
        self.assertFalse(marker.exists())

    def test_fresh_row_roundtrips_through_public_ledger_validation(self):
        rows = self.run_checks('printf ok', timeout=3.5)
        self.assertIn('effective_timeout_seconds', check_evidence.EVIDENCE_ROW_KEYS)
        self.assertEqual(set(rows[0]), set(check_evidence.EVIDENCE_ROW_KEYS))
        roundtripped = json.loads(json.dumps(rows))
        coordination._validate_check_evidence(roundtripped)
        self.assertEqual(roundtripped[0]['check_timeout_seconds'], 3.5)
        self.assertEqual(roundtripped[0]['effective_timeout_seconds'], 3.5)


class EffectiveTimeoutValidationTests(unittest.TestCase):
    @staticmethod
    def row(effective, configured=10.0):
        return {
            'command': 'check', 'exit_code': 0,
            'check_timeout_seconds': configured,
            'effective_timeout_seconds': effective,
        }

    def test_zero_and_bounded_effective_values_are_accepted(self):
        for value in (0, 0.0, 2.5, 10, 10.0):
            with self.subTest(value=value):
                self.assertEqual(
                    check_evidence.validate_effective_timeout(value, 10.0),
                    float(value))
                coordination._validate_check_evidence([self.row(value)])

    def test_invalid_effective_values_are_rejected_everywhere(self):
        values = (True, False, '1', None, float('nan'), float('inf'),
                  float('-inf'), -1, 10.5, 86400.5, 10 ** 10000)
        for value in values:
            with self.subTest(value=value):
                with self.assertRaises(check_evidence.CheckEvidenceError):
                    check_evidence.validate_effective_timeout(value, 10.0)
                with self.assertRaises(coordination.CoordinationError):
                    coordination._validate_check_evidence([self.row(value)])

    def test_effective_without_configured_timeout_is_rejected(self):
        row = {'command': 'check', 'exit_code': 0,
               'effective_timeout_seconds': 1.0}
        with self.assertRaises(coordination.CoordinationError):
            coordination._validate_check_evidence([row])

    def test_historical_rows_without_the_new_field_remain_valid(self):
        coordination._validate_check_evidence([
            {'command': 'check', 'exit_code': 0},
            {'command': 'check', 'exit_code': 0,
             'check_timeout_seconds': 5},
        ])


class EffectiveTimeoutReuseTests(CheckReuseCase):
    def test_successful_cache_hit_survives_a_different_remaining_deadline(self):
        first = self.run_checks(timeout=5)
        second = self.run_checks(
            timeout=5, deadline=time.monotonic() + 30)
        self.assertFalse(first[0]['reused'])
        self.assertTrue(second[0]['reused'])
        self.assertEqual(second[0]['check_timeout_seconds'], 5.0)
        self.assertEqual(second[0]['effective_timeout_seconds'], 5.0)
        self.assertEqual(self.count(), 1)
        self.assertEqual(len(self.cache_entries()), 1)

    def test_old_cache_rows_without_effective_timeout_miss_conservatively(self):
        first = self.run_checks(timeout=5)
        self.assertIn('effective_timeout_seconds', first[0])
        with self.copy.lock():
            entry = self.cache_entries()[0]
            for row in entry['rows']:
                row.pop('effective_timeout_seconds')
            self.copy.save()
        second = self.run_checks(timeout=5)
        self.assertFalse(second[0]['reused'])
        self.assertIn('effective_timeout_seconds', second[0])
        self.assertEqual(self.count(), 2)

    def test_invalid_effective_cache_row_misses_conservatively(self):
        self.run_checks(timeout=5)
        with self.copy.lock():
            entry = self.cache_entries()[0]
            entry['rows'][0]['effective_timeout_seconds'] = 6.0
            self.copy.save()
        rows = self.run_checks(timeout=5)
        self.assertFalse(rows[0]['reused'])
        self.assertEqual(rows[0]['effective_timeout_seconds'], 5.0)
        self.assertEqual(self.count(), 2)

    def test_timed_out_evidence_is_never_reused(self):
        command = self.command(extra='import time; time.sleep(2)')
        first = self.run_checks(commands=[command], timeout=0.2)
        self.assertTrue(first[0]['timed_out'])
        self.assertFalse(first[0]['reused'])
        second = self.run_checks(commands=[command], timeout=0.2)
        self.assertTrue(second[0]['timed_out'])
        self.assertFalse(second[0]['reused'])
        self.assertEqual(self.count(), 2)


if __name__ == '__main__':
    unittest.main()
