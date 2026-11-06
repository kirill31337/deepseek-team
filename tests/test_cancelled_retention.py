"""Regression coverage for a cancelled historical coordinator deliverable.

A current cancellation is not retained active work, but it must not waive
profile distribution, outcome freshness, scope immutability or hook authority.
These tests use the real CLI and lifecycle handler against isolated state.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

from codex_deepseek_team import coordination, coordinator_hooks, project, settings

from test_coordination import CoordinationCase


class CancelledRetentionCliTests(CoordinationCase):
    FEATURES = {
        "domain": "python", "operation": "fix", "localization": "known",
        "coupling": "local", "verification": "none", "clarity": "clear",
        "risk": "low", "scope_size": "small",
    }

    def setUp(self):
        super().setUp()
        project.attach(self.repo, coordinator="codex")
        # Never inherit a coordinator selection or global disable switch from
        # the outer development session.
        os.environ.pop("DEEPSEEK_TEAM_PROJECT_ROOT", None)
        os.environ.pop("DEEPSEEK_TEAM_DISABLED", None)
        self.package_root = Path(coordination.__file__).resolve().parents[1]

    def hook(self, name, **extra):
        payload = {
            "session_id": "sess-cancelled", "turn_id": "turn-1",
            "cwd": str(self.repo), "hook_event_name": name, "model": "gpt-5.6",
            "permission_mode": "default",
        }
        payload.update(extra)
        return coordinator_hooks.handle(payload)

    def cli(self, *args, stdin=None):
        environment = dict(os.environ)
        existing = environment.get("PYTHONPATH")
        environment["PYTHONPATH"] = str(self.package_root) + (
            os.pathsep + existing if existing else "")
        return subprocess.run(
            [sys.executable, "-m", "codex_deepseek_team", *args],
            input=stdin, text=True, capture_output=True, env=environment,
            cwd=self.repo, check=False,
        )

    def plan_payload(self):
        return {
            "classification": "substantial",
            "deliverables": [
                {
                    "id": "worker-review", "kind": "review",
                    "scope": ["tests/test_worker.py"], "executor": "worker",
                    "acceptance": ["findings reviewed"], "dependencies": [], "checks": [],
                },
                {
                    "id": "cancelled-impl", "kind": "implementation",
                    "scope": ["src/cancelled.py"], "executor": "auto",
                    "features": dict(self.FEATURES), "acceptance": ["behavior verified"],
                    "dependencies": [], "checks": [],
                },
            ],
        }

    def finish_worker_review(self, task, planned):
        assignment = next(row for row in planned["assignments"]
                          if row["deliverable_id"] == "worker-review")
        coordination.assignment_started(
            self.repo, task["id"], assignment["id"], "ws-cancelled-review",
            "codex", [], effort="high",
        )
        coordination.assignment_finished(
            self.repo, task["id"], assignment["id"], "succeeded",
            "reviewed the bounded worker slice", ["tests/test_worker.py"], [],
        )
        coordination.use_result(
            self.repo, task["id"], assignment["id"], "incorporated",
            "reviewed and accepted the recorded worker result",
        )

    def test_cancelled_verification_retention_completes_but_hook_stays_scoped(self):
        submitted = self.hook("UserPromptSubmit", prompt="implement substantial feature")
        self.assertIn("coordination task", submitted["hookSpecificOutput"]["additionalContext"].lower())
        task = coordination.latest_task(self.repo, "sess-cancelled")

        planned = self.cli(
            "coordination", "plan", "--path", str(self.repo), "--task", task["id"],
            stdin=json.dumps(self.plan_payload()),
        )
        self.assertEqual(planned.returncode, 78, planned.stderr)
        payload = json.loads(planned.stdout)
        stored = coordination.load_task(self.repo, task["id"])
        cancelled = next(row for row in stored["deliverables"]
                         if row["id"] == "cancelled-impl")
        diagnostic = next(row for row in payload["diagnostics"]
                          if row["id"] == "cancelled-impl")
        self.assertEqual(cancelled["executor"], "coordinator")
        self.assertEqual(diagnostic["requested_executor"], "auto")
        self.assertIn("ineligible_verification", diagnostic["reason_codes"])
        self.assertTrue(any("cancelled-impl" in issue and "retained by coordinator" in issue
                            for issue in payload["issues"]), payload["issues"])

        self.finish_worker_review(task, stored)

        recorded = self.cli(
            "coordination", "result", "--path", str(self.repo), "--task", task["id"],
            "--deliverable", "cancelled-impl", "--outcome", "cancelled",
            "--evidence", "superseded before retaining worker-eligible implementation",
        )
        self.assertEqual(recorded.returncode, 0, recorded.stderr)
        self.assertEqual(coordination.validate_task(self.repo, task["id"]), [])

        denied = self.hook(
            "PreToolUse", tool_name="apply_patch", tool_use_id="tool-unplanned",
            tool_input={"command": "*** Add File: src/unplanned.py\n+VALUE = 2\n"},
        )
        self.assertEqual(denied["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn(
            "unplanned",
            denied["hookSpecificOutput"]["permissionDecisionReason"].lower(),
        )

        completed = self.cli(
            "coordination", "complete", "--path", str(self.repo), "--task", task["id"],
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("status=completed", completed.stdout)
        self.assertEqual(
            coordination.load_task(self.repo, task["id"])["status"], "completed")

    def test_profiles_keep_their_distribution_rules_after_cancellation(self):
        cases = (
            (25, "full-access", [], 1),
            (50, "full-access", ["50/full-access requires at least one worker"], 2),
            (75, "full-access", ["75/full-access requires worker assignment"], 3),
            ("auto", "full-access", [], 4),
            (75, "read-only", [], 5),
        )
        for level, access, expected, sequence in cases:
            with self.subTest(level=level, access=access):
                settings.set_values(
                    self.repo / settings.PROJECT_FILE,
                    delegation_level=level, access=access,
                )
                task = coordination.open_task(
                    self.repo, session_id=f"sess-profile-{sequence}",
                    turn_id=f"turn-{sequence}", prompt="implement substantial feature",
                    policy=self.policy(level, access),
                )
                planned = coordination.plan_task(self.repo, task["id"], {
                    "classification": "substantial",
                    "deliverables": [{
                        "id": "cancelled-impl", "kind": "implementation",
                        "scope": ["src/cancelled.py"], "executor": "auto",
                        "features": dict(self.FEATURES),
                        "acceptance": ["behavior verified"],
                        "dependencies": [], "checks": [],
                    }],
                })
                item = planned["deliverables"][0]
                self.assertEqual(item["executor"], "coordinator")
                coordination.observe_coordinator_result(
                    self.repo, task["id"], "cancelled-impl", "cancelled",
                    "superseded before retaining worker-eligible implementation",
                )
                issues = coordination.validate_task(self.repo, task["id"])
                self.assertFalse(
                    any("retained by coordinator" in issue for issue in issues), issues)
                for fragment in expected:
                    self.assertTrue(any(fragment in issue for issue in issues), issues)
                if not expected:
                    self.assertEqual(issues, [])


if __name__ == "__main__":
    unittest.main()
