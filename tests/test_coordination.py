"""Process enforcement: persistent task state, distribution gates and Codex hooks."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from codex_deepseek_team import project, settings, workspace

try:
    from codex_deepseek_team import coordination, codex_hooks
except ImportError:
    coordination = codex_hooks = None


class CoordinationCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="dst-coord-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        (self.repo / "a.py").write_text("VALUE = 1\n")
        subprocess.run(["git", "-C", str(self.repo), "add", "."], check=True)
        subprocess.run(
            ["git", "-C", str(self.repo), "-c", "user.name=Test", "-c",
             "user.email=test@example.test", "commit", "-qm", "base"], check=True
        )
        self.state = self.root / "state"
        self.env = mock.patch.dict(os.environ, {
            "DEEPSEEK_TEAM_STATE_DIR": str(self.state),
            "XDG_CONFIG_HOME": str(self.root / "config"),
            "HOME": str(self.root / "home"),
        })
        (self.root / "home").mkdir()
        self.env.start()
        self.addCleanup(self.env.stop)

    def policy(self, level=75, access="full-access"):
        return settings.Policy(level, access, {
            "delegation_level": "test",
            "access": "test",
        })

    def start(self, level=75, access="full-access"):
        return coordination.open_task(
            self.repo, session_id="sess-1", turn_id="turn-1",
            prompt="implement feature", policy=self.policy(level, access),
        )


class StateAndDistributionTests(CoordinationCase):
    def test_max_profile_rejects_token_worker_when_other_eligible_work_is_kept(self):
        task = self.start()
        coordination.plan_task(self.repo, task["id"], {
            "classification": "substantial",
            "deliverables": [
                {"id": "impl", "kind": "implementation", "scope": ["src/core.py"],
                 "executor": "worker", "acceptance": ["works"], "dependencies": [], "checks": []},
                {"id": "tests", "kind": "test", "scope": ["tests/test_core.py"],
                 "executor": "coordinator", "retention": {"code": "quality_owner", "evidence": "I own quality"},
                 "acceptance": ["tests"], "dependencies": [], "checks": []},
                {"id": "fixtures", "kind": "fixture", "scope": ["tests/fixtures/*"],
                 "executor": "coordinator", "retention": {"code": "release_work", "evidence": "release"},
                 "acceptance": ["fixtures"], "dependencies": [], "checks": []},
                {"id": "docs", "kind": "documentation", "scope": ["README.md"],
                 "executor": "coordinator", "retention": {"code": "quality_owner", "evidence": "I own quality"},
                 "acceptance": ["docs"], "dependencies": [], "checks": []},
            ],
        })
        issues = coordination.validate_task(self.repo, task["id"])
        self.assertTrue(any("tests" in item for item in issues), issues)
        self.assertTrue(any("fixtures" in item for item in issues), issues)
        self.assertTrue(any("docs" in item for item in issues), issues)

    def test_protected_coordinator_work_and_real_constraints_are_allowed(self):
        task = self.start()
        coordination.plan_task(self.repo, task["id"], {
            "classification": "substantial",
            "deliverables": [
                {"id": "impl", "kind": "implementation", "scope": ["src/*"],
                 "executor": "worker", "acceptance": ["implemented"],
                 "dependencies": [{"kind": "command", "value": "python3"}],
                 "checks": ["python3 -m unittest -q"]},
                {"id": "arch", "kind": "architecture", "scope": ["design"],
                 "executor": "coordinator", "acceptance": ["decision"], "dependencies": [], "checks": []},
                {"id": "release", "kind": "metadata", "scope": ["signed-release"],
                 "executor": "coordinator",
                 "retention": {"code": "secret_or_signing", "evidence": "signing key is coordinator-only"},
                 "acceptance": ["signed"], "dependencies": [], "checks": []},
            ],
        })
        self.assertEqual(coordination.validate_task(self.repo, task["id"]), [])

    def test_readonly_override_never_expands_write_access(self):
        task = self.start(75, "read-only")
        coordination.plan_task(self.repo, task["id"], {
            "classification": "substantial",
            "deliverables": [
                {"id": "impl", "kind": "implementation", "scope": ["a.py"],
                 "executor": "coordinator", "acceptance": ["done"], "dependencies": [], "checks": []},
                {"id": "review", "kind": "review", "scope": ["a.py"],
                 "executor": "worker", "acceptance": ["findings"], "dependencies": [], "checks": []},
            ],
        })
        self.assertEqual(coordination.load_task(self.repo, task["id"])["policy"]["effective_access"], "read-only")
        self.assertEqual(coordination.validate_task(self.repo, task["id"]), [])

    def test_small_single_deliverable_can_remain_local_without_worker(self):
        task = self.start()
        coordination.plan_task(self.repo, task["id"], {
            "classification": "small",
            "small_evidence": "single localized typo with one output",
            "deliverables": [
                {"id": "typo", "kind": "implementation", "scope": ["a.py"],
                 "executor": "coordinator", "acceptance": ["typo fixed"], "dependencies": [], "checks": []},
            ],
        })
        self.assertEqual(coordination.validate_task(self.repo, task["id"]), [])

    def test_worker_result_keeps_prepared_changes_separate_and_survives_reload(self):
        task = self.start()
        planned = coordination.plan_task(self.repo, task["id"], {
            "classification": "substantial",
            "deliverables": [
                {"id": "review", "kind": "review", "scope": ["a.py"],
                 "executor": "worker", "acceptance": ["review"], "dependencies": [], "checks": []},
            ],
        })
        assignment = planned["assignments"][0]
        coordination.assignment_started(self.repo, task["id"], assignment["id"],
                                        workspace_id="ws-1", runtime="codex",
                                        prepared_changes=["prepared.patch"])
        coordination.assignment_finished(
            self.repo, task["id"], assignment["id"], status="succeeded",
            result_summary="race hypothesis", worker_changes=["a.py"],
            checks=[{"command": "python3 -m unittest", "exit_code": 0}],
        )
        reloaded = coordination.load_task(self.repo, task["id"])
        row = reloaded["assignments"][0]
        self.assertEqual(row["prepared_changes"], ["prepared.patch"])
        self.assertEqual(row["worker_changes"], ["a.py"])
        self.assertEqual(row["result_summary"], "race hypothesis")
        self.assertIsNone(row["disposition"])
        coordination.use_result(self.repo, task["id"], assignment["id"],
                                disposition="reproduced",
                                evidence="reproduced and fixed as defect #2")
        reloaded = coordination.load_task(self.repo, task["id"])
        self.assertEqual(reloaded["assignments"][0]["disposition"]["kind"], "reproduced")


class ReadinessAndAttributionTests(CoordinationCase):
    def test_missing_declared_command_fails_readiness_before_worker(self):
        task = self.start()
        planned = coordination.plan_task(self.repo, task["id"], {
            "classification": "substantial",
            "deliverables": [
                {"id": "jvm", "kind": "implementation", "scope": ["a.py"],
                 "executor": "worker", "acceptance": ["jvm tests pass"],
                 "dependencies": [{"kind": "command", "value": "definitely-missing-jdk-tool"}],
                 "checks": ["definitely-missing-jdk-tool test"]},
            ],
        })
        copy = workspace.create(self.repo, self.state)
        with self.assertRaises(coordination.CoordinationError) as caught:
            coordination.ensure_assignment_ready(self.repo, task["id"],
                                                 planned["assignments"][0]["id"], copy)
        self.assertIn("missing", str(caught.exception).lower())

    def test_snapshot_attributes_only_worker_delta_not_coordinator_preparation(self):
        copy = workspace.create(self.repo, self.state)
        (copy.path / "prepared.py").write_text("prepared by coordinator\n")
        before = workspace.content_snapshot(copy)
        (copy.path / "a.py").write_text("VALUE = 2\n")
        self.assertEqual(workspace.changed_since(copy, before), ["a.py"])



class CodexHookTests(CoordinationCase):
    def hook(self, name, **extra):
        payload = {
            "session_id": "sess-1", "turn_id": "turn-1", "cwd": str(self.repo),
            "hook_event_name": name, "model": "gpt-5.6", "permission_mode": "default",
        }
        payload.update(extra)
        return codex_hooks.handle(payload)

    def test_new_prompt_creates_task_and_sessionstart_restores_it_after_compaction(self):
        out = self.hook("UserPromptSubmit", prompt="implement substantial feature")
        ctx = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("coordination task", ctx.lower())
        task = coordination.latest_task(self.repo, "sess-1")
        planned = coordination.plan_task(self.repo, task["id"], {
            "classification": "substantial",
            "deliverables": [
                {"id": "review", "kind": "review", "scope": ["a.py"],
                 "executor": "worker", "acceptance": ["review"], "dependencies": [], "checks": []},
            ],
        })
        aid = planned["assignments"][0]["id"]
        coordination.assignment_started(self.repo, task["id"], aid, "ws-1", "codex", [])
        coordination.assignment_finished(self.repo, task["id"], aid, "succeeded",
                                         "important initial review", [], [])
        restored = self.hook("SessionStart", source="compact")
        text = restored["hookSpecificOutput"]["additionalContext"]
        self.assertIn("important initial review", text)
        self.assertIn(aid, text)

    def test_pretooluse_blocks_coordinator_edit_before_valid_distribution(self):
        self.hook("UserPromptSubmit", prompt="implement feature")
        denied = self.hook("PreToolUse", tool_name="apply_patch",
                           tool_use_id="tool-1",
                           tool_input={"command": "*** Update File: a.py\n@@\n"})
        self.assertEqual(denied["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn("distribution", denied["hookSpecificOutput"]["permissionDecisionReason"].lower())

    def test_pretooluse_blocks_duplicate_edit_owned_by_pending_worker(self):
        self.hook("UserPromptSubmit", prompt="implement feature")
        task = coordination.latest_task(self.repo, "sess-1")
        coordination.plan_task(self.repo, task["id"], {
            "classification": "substantial",
            "deliverables": [
                {"id": "impl", "kind": "implementation", "scope": ["a.py"],
                 "executor": "worker", "acceptance": ["done"], "dependencies": [], "checks": []},
            ],
        })
        denied = self.hook("PreToolUse", tool_name="apply_patch",
                           tool_use_id="tool-2",
                           tool_input={"command": "*** Update File: a.py\n@@\n"})
        self.assertEqual(denied["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn("worker", denied["hookSpecificOutput"]["permissionDecisionReason"].lower())

    def test_stop_requires_completed_worker_result_to_be_dispositioned(self):
        self.hook("UserPromptSubmit", prompt="review")
        task = coordination.latest_task(self.repo, "sess-1")
        planned = coordination.plan_task(self.repo, task["id"], {
            "classification": "substantial",
            "deliverables": [
                {"id": "review", "kind": "review", "scope": ["a.py"],
                 "executor": "worker", "acceptance": ["findings"], "dependencies": [], "checks": []},
            ],
        })
        aid = planned["assignments"][0]["id"]
        coordination.assignment_started(self.repo, task["id"], aid, "ws", "codex", [])
        coordination.assignment_finished(self.repo, task["id"], aid, "succeeded",
                                         "useful review", [], [])
        stopped = self.hook("Stop")
        self.assertFalse(stopped["continue"])
        self.assertIn("disposition", stopped["stopReason"].lower())
        coordination.use_result(self.repo, task["id"], aid, "incorporated", "applied finding")
        allowed = self.hook("Stop")
        self.assertTrue(allowed.get("continue", True))


class ProjectHookInstallTests(CoordinationCase):
    def test_codex_attach_installs_managed_hooks_without_destroying_user_hooks(self):
        dot = self.repo / ".codex"
        dot.mkdir()
        hookfile = dot / "hooks.json"
        hookfile.write_text(json.dumps({
            "hooks": {"PreToolUse": [{"matcher": "^Custom$", "hooks": [
                {"type": "command", "command": "custom-hook"}]}]}
        }))
        project.attach(self.repo, coordinator="codex")
        data = json.loads(hookfile.read_text())
        self.assertIn("custom-hook", hookfile.read_text())
        self.assertIn("SessionStart", data["hooks"])
        self.assertIn("UserPromptSubmit", data["hooks"])
        self.assertIn("PreToolUse", data["hooks"])
        self.assertIn("Stop", data["hooks"])
        self.assertIn("deepseek-team coordinator-hook", hookfile.read_text())
        project.detach(self.repo, coordinator="codex")
        data = json.loads(hookfile.read_text())
        self.assertIn("custom-hook", hookfile.read_text())
        self.assertNotIn("deepseek-team coordinator-hook", hookfile.read_text())


if __name__ == "__main__":
    unittest.main()
