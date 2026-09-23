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
    from codex_deepseek_team import coordination, coordinator_hooks
except ImportError:
    coordination = coordinator_hooks = None


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
        # Match the fixture's declared worker authority with real project
        # configuration; constructed policy snapshots cannot grant access.
        settings.set_values(self.repo / settings.PROJECT_FILE,
                            delegation_level=75, access='full-access')

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
                 "retention": {"code": "secret_or_signing", "sensitive": True,
                               "evidence": "signing key is coordinator-only"},
                 "acceptance": ["signed"], "dependencies": [], "checks": []},
            ],
        })
        self.assertEqual(coordination.validate_task(self.repo, task["id"]), [])

    def test_user_explicit_retention_is_not_self_asserted_by_coordinator(self):
        task = self.start()
        coordination.plan_task(self.repo, task["id"], {
            "classification": "substantial",
            "deliverables": [
                {"id": "impl", "kind": "implementation", "scope": ["a.py"],
                 "executor": "coordinator",
                 "retention": {
                     "code": "user_explicit", "source": "user",
                     "prompt_sha256": task["prompt_sha256"],
                     "evidence": "coordinator claims user wanted local implementation"
                 },
                 "acceptance": ["done"], "dependencies": [], "checks": []},
                {"id": "review", "kind": "review", "scope": ["a.py"],
                 "executor": "worker", "acceptance": ["findings"], "dependencies": [], "checks": []},
            ],
        })
        issues = coordination.validate_task(self.repo, task["id"])
        self.assertTrue(any("impl" in item for item in issues), issues)

    def test_technical_retention_reason_requires_runner_record_not_free_text(self):
        task = self.start()
        coordination.plan_task(self.repo, task["id"], {
            "classification": "substantial",
            "deliverables": [
                {"id": "impl", "kind": "implementation", "scope": ["a.py"],
                 "executor": "coordinator",
                 "retention": {"code": "runner_unavailable", "evidence": "I claim the runner is unavailable"},
                 "acceptance": ["done"], "dependencies": [], "checks": []},
                {"id": "review", "kind": "review", "scope": ["a.py"],
                 "executor": "worker", "acceptance": ["findings"], "dependencies": [], "checks": []},
            ],
        })
        issues = coordination.validate_task(self.repo, task["id"])
        self.assertTrue(any("impl" in issue for issue in issues), issues)
        coordination.record_constraint(self.repo, task["id"], "impl",
                                       "runner_unavailable", "runtime capability probe failed")
        self.assertEqual(coordination.validate_task(self.repo, task["id"]), [])

    def test_50_full_access_review_only_does_not_satisfy_implementation_delegation(self):
        task = self.start(50, "full-access")
        coordination.plan_task(self.repo, task["id"], {
            "classification": "substantial",
            "deliverables": [
                {"id": "impl", "kind": "implementation", "scope": ["a.py"],
                 "executor": "coordinator", "acceptance": ["done"], "dependencies": [], "checks": []},
                {"id": "review", "kind": "review", "scope": ["a.py"],
                 "executor": "worker", "acceptance": ["review"], "dependencies": [], "checks": []},
            ],
        })
        issues = coordination.validate_task(self.repo, task["id"])
        self.assertTrue(any("implementation" in item.lower() for item in issues), issues)

    def test_native_agent_requires_reason_and_cannot_take_protected_responsibility(self):
        task = self.start()
        with self.assertRaises(coordination.CoordinationError):
            coordination.plan_task(self.repo, task["id"], {
                "classification": "substantial",
                "deliverables": [
                    {"id": "native-review", "kind": "review", "scope": ["a.py"],
                     "executor": "native-agent", "acceptance": ["findings"],
                     "dependencies": [], "checks": []},
                ],
            })
        with self.assertRaises(coordination.CoordinationError):
            coordination.plan_task(self.repo, task["id"], {
                "classification": "substantial",
                "deliverables": [
                    {"id": "native-arch", "kind": "architecture", "scope": ["design"],
                     "executor": "native-agent",
                     "delegation_reason": "independent architecture decision",
                     "acceptance": ["decision"], "dependencies": [], "checks": []},
                ],
            })

    def test_native_agent_is_additive_and_does_not_replace_required_deepseek_worker(self):
        task = self.start(50, "full-access")
        coordination.plan_task(self.repo, task["id"], {
            "classification": "substantial",
            "deliverables": [
                {"id": "native-impl", "kind": "implementation", "scope": ["a.py"],
                 "executor": "native-agent",
                 "delegation_reason": "parallel isolated implementation for comparison",
                 "acceptance": ["candidate implementation"], "dependencies": [], "checks": []},
            ],
        })
        issues = coordination.validate_task(self.repo, task["id"])
        self.assertTrue(any("worker implementation" in item.lower() for item in issues), issues)

    def test_justified_native_agent_can_complement_a_valid_deepseek_plan(self):
        task = self.start()
        planned = coordination.plan_task(self.repo, task["id"], {
            "classification": "substantial",
            "deliverables": [
                {"id": "impl", "kind": "implementation", "scope": ["a.py"],
                 "executor": "worker", "acceptance": ["implemented"],
                 "dependencies": [], "checks": []},
                {"id": "native-review", "kind": "review", "scope": ["a.py"],
                 "executor": "native-agent",
                 "delegation_reason": "independent native review in isolated context",
                 "acceptance": ["independent findings"], "dependencies": [], "checks": []},
            ],
        })
        self.assertEqual(coordination.validate_task(self.repo, task["id"]), [])
        self.assertEqual(len(planned["assignments"]), 1)
        self.assertEqual(planned["assignments"][0]["deliverable_id"], "impl")
        summary = coordination.summary(planned)
        self.assertIn("native-agent deliverable=native-review", summary)
        self.assertIn("isolated context", summary)

    def test_started_worker_deliverable_cannot_be_reassigned_to_native_agent(self):
        task = self.start()
        planned = coordination.plan_task(self.repo, task["id"], {
            "classification": "substantial",
            "deliverables": [
                {"id": "impl", "kind": "implementation", "scope": ["a.py"],
                 "executor": "worker", "acceptance": ["implemented"],
                 "dependencies": [], "checks": []},
            ],
        })
        aid = planned["assignments"][0]["id"]
        coordination.assignment_started(self.repo, task["id"], aid, "ws", "codex", [], effort="medium")
        with self.assertRaises(coordination.CoordinationError):
            coordination.plan_task(self.repo, task["id"], {
                "classification": "substantial",
                "deliverables": [
                    {"id": "impl", "kind": "implementation", "scope": ["a.py"],
                     "executor": "native-agent",
                     "delegation_reason": "switching implementation to a native agent",
                     "acceptance": ["implemented"], "dependencies": [], "checks": []},
                ],
            })

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

    def test_small_task_rejects_broad_scope_and_unplanned_second_file(self):
        task = self.start()
        with self.assertRaises(coordination.CoordinationError):
            coordination.plan_task(self.repo, task["id"], {
                "classification": "small",
                "small_evidence": "claimed small",
                "deliverables": [
                    {"id": "small", "kind": "implementation", "scope": ["src/*"],
                     "executor": "coordinator", "acceptance": ["done"],
                     "dependencies": [], "checks": []},
                ],
            })
        coordination.plan_task(self.repo, task["id"], {
            "classification": "small",
            "small_evidence": "single localized file edit",
            "deliverables": [
                {"id": "small", "kind": "implementation", "scope": ["a.py"],
                 "executor": "coordinator", "acceptance": ["done"],
                 "dependencies": [], "checks": []},
            ],
        })
        project.attach(self.repo, coordinator="codex")
        denied = coordinator_hooks.handle({
            "session_id": "sess-1", "turn_id": "turn-1", "cwd": str(self.repo),
            "hook_event_name": "PreToolUse", "tool_name": "apply_patch",
            "tool_use_id": "tool-small",
            "tool_input": {"command": "*** Add File: b.py\n+VALUE = 2\n"},
            "model": "gpt", "permission_mode": "default",
        })
        self.assertEqual(denied["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn("unplanned", denied["hookSpecificOutput"]["permissionDecisionReason"].lower())

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
                                        prepared_changes=["prepared.patch"], effort="high")
        coordination.assignment_finished(
            self.repo, task["id"], assignment["id"], status="succeeded",
            result_summary="race hypothesis", worker_changes=["a.py"],
            checks=[{"command": "python3 -m unittest", "exit_code": 0}],
        )
        reloaded = coordination.load_task(self.repo, task["id"])
        row = reloaded["assignments"][0]
        self.assertEqual(row["prepared_changes"], ["prepared.patch"])
        self.assertEqual(row["effort"], "high")
        self.assertEqual(row["worker_changes"], ["a.py"])
        self.assertEqual(row["result_summary"], "race hypothesis")
        self.assertIsNone(row["disposition"])
        coordination.use_result(self.repo, task["id"], assignment["id"],
                                disposition="reproduced",
                                evidence="reproduced and fixed as defect #2")
        reloaded = coordination.load_task(self.repo, task["id"])
        self.assertEqual(reloaded["assignments"][0]["disposition"]["kind"], "reproduced")

    def test_assignment_effort_is_canonical_and_legacy_medium_maps_to_high(self):
        for index, (requested, expected) in enumerate((("max", "max"), ("medium", "high"), ("low", "low"))):
            with self.subTest(requested=requested):
                task = coordination.open_task(
                    self.repo, session_id="sess-effort", turn_id=f"turn-{index}",
                    prompt="implement feature", policy=self.policy())
                planned = coordination.plan_task(self.repo, task["id"], {
                    "classification": "substantial",
                    "deliverables": [
                        {"id": "review", "kind": "review", "scope": ["a.py"],
                         "executor": "worker", "acceptance": ["review"],
                         "dependencies": [], "checks": []},
                    ],
                })
                row = coordination.assignment_started(
                    self.repo, task["id"], planned["assignments"][0]["id"],
                    workspace_id="ws-effort", runtime="codex",
                    prepared_changes=[], effort=requested)["assignments"][0]
                self.assertEqual(row["effort"], expected)
                self.assertEqual(row["routing_features"]["effort"], expected)
        task = coordination.open_task(
            self.repo, session_id="sess-effort", turn_id="turn-bad",
            prompt="implement feature", policy=self.policy())
        planned = coordination.plan_task(self.repo, task["id"], {
            "classification": "substantial",
            "deliverables": [
                {"id": "review", "kind": "review", "scope": ["a.py"],
                 "executor": "worker", "acceptance": ["review"],
                 "dependencies": [], "checks": []},
            ],
        })
        with self.assertRaises(coordination.CoordinationError):
            coordination.assignment_started(self.repo, task["id"], planned["assignments"][0]["id"],
                                            workspace_id="ws-effort", runtime="codex",
                                            prepared_changes=[], effort="xhigh")


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

    def test_selected_dirty_source_is_imported_explicitly_without_copying_secrets(self):
        copy = workspace.create(self.repo, self.state)
        (self.repo / "dirty.py").write_text("selected dirty change\n")
        (self.repo / ".env").write_text("SECRET=never-copy\n")
        workspace.import_paths(copy, ["dirty.py"])
        self.assertEqual((copy.path / "dirty.py").read_text(), "selected dirty change\n")
        self.assertFalse((copy.path / ".env").exists())
        self.assertIn("dirty.py", workspace.load(self.state, copy.id).metadata["prepared_paths"])
        with self.assertRaises(workspace.WorkspaceError):
            workspace.import_paths(copy, [".env"])

    def test_snapshot_attributes_only_worker_delta_not_coordinator_preparation(self):
        copy = workspace.create(self.repo, self.state)
        (copy.path / "prepared.py").write_text("prepared by coordinator\n")
        before = workspace.content_snapshot(copy)
        (copy.path / "a.py").write_text("VALUE = 2\n")
        self.assertEqual(workspace.changed_since(copy, before), ["a.py"])



class WorkspaceImportCliTests(CoordinationCase):
    def test_workspace_import_cli_copies_only_explicit_selected_file(self):
        from codex_deepseek_team import delegation_cli
        copy = workspace.create(self.repo, self.state)
        (self.repo / "selected.py").write_text("selected\n")
        (self.repo / "other.py").write_text("other\n")
        code = delegation_cli.main([
            "workspace", "import", "--state-dir", str(self.state),
            copy.id, "--include", "selected.py",
        ])
        self.assertEqual(code, 0)
        self.assertEqual((copy.path / "selected.py").read_text(), "selected\n")
        self.assertFalse((copy.path / "other.py").exists())
        loaded = workspace.load(self.state, copy.id)
        self.assertIn("selected.py", loaded.metadata["prepared_paths"])

class CoordinationCliTests(CoordinationCase):
    def test_plan_cli_persists_machine_readable_assignments_and_rejects_bad_75_plan(self):
        from codex_deepseek_team import coordination_cli
        task = self.start()
        bad = {
            "classification": "substantial",
            "deliverables": [
                {"id": "impl", "kind": "implementation", "scope": ["a.py"],
                 "executor": "worker", "acceptance": ["done"], "dependencies": [], "checks": []},
                {"id": "tests", "kind": "test", "scope": ["tests/*"],
                 "executor": "coordinator", "acceptance": ["tests"], "dependencies": [], "checks": []},
            ],
        }
        with mock.patch("sys.stdin", new=__import__("io").StringIO(json.dumps(bad))):
            self.assertEqual(coordination_cli.main([
                "coordination", "plan", "--path", str(self.repo), "--task", task["id"]
            ]), 78)
        saved = coordination.load_task(self.repo, task["id"])
        self.assertEqual(len(saved["assignments"]), 1)



class CodexHookTests(CoordinationCase):
    def setUp(self):
        super().setUp()
        # These fixtures exercise write-scope gates; authorize writes explicitly.
        settings.set_values(self.repo / settings.PROJECT_FILE, access='full-access')
        project.attach(self.repo, coordinator="codex")

    def hook(self, name, **extra):
        payload = {
            "session_id": "sess-1", "turn_id": "turn-1", "cwd": str(self.repo),
            "hook_event_name": name, "model": "gpt-5.6", "permission_mode": "default",
        }
        payload.update(extra)
        return coordinator_hooks.handle(payload)

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

    def test_stop_continuation_reuses_same_task_instead_of_losing_assignments(self):
        self.hook("UserPromptSubmit", prompt="implement feature")
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
        coordination.assignment_finished(self.repo, task["id"], aid, "succeeded", "important finding", [], [])
        blocked = self.hook("Stop", stop_hook_active=False)
        self.assertEqual(blocked["decision"], "block")
        follow = dict(session_id="sess-1", turn_id="turn-2", cwd=str(self.repo),
                      hook_event_name="UserPromptSubmit", prompt=blocked["reason"],
                      model="gpt", permission_mode="default")
        coordinator_hooks.handle(follow)
        same = coordination.latest_task(self.repo, "sess-1")
        self.assertEqual(same["id"], task["id"])
        self.assertEqual(same["assignments"][0]["result_summary"], "important finding")

    def test_pretooluse_tracks_edit_and_write_file_paths(self):
        self.hook("UserPromptSubmit", prompt="implement feature")
        task = coordination.latest_task(self.repo, "sess-1")
        coordination.plan_task(self.repo, task["id"], {
            "classification": "substantial",
            "deliverables": [
                {"id": "impl", "kind": "implementation", "scope": ["a.py"],
                 "executor": "worker", "acceptance": ["done"], "dependencies": [], "checks": []},
            ],
        })
        for tool_name, tool_input in [
            ("Edit", {"file_path": "a.py", "old_string": "1", "new_string": "2"}),
            ("Write", {"path": "a.py", "content": "VALUE = 2\n"}),
        ]:
            with self.subTest(tool=tool_name):
                denied = self.hook("PreToolUse", tool_name=tool_name,
                                   tool_use_id="tool-" + tool_name.lower(),
                                   tool_input=tool_input)
                self.assertEqual(
                    denied["hookSpecificOutput"]["permissionDecision"], "deny")
                self.assertIn(
                    "pending worker",
                    denied["hookSpecificOutput"]["permissionDecisionReason"].lower())

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

    def test_pretooluse_blocks_new_unplanned_scope_after_worker_result(self):
        self.hook("UserPromptSubmit", prompt="implement feature")
        task = coordination.latest_task(self.repo, "sess-1")
        planned = coordination.plan_task(self.repo, task["id"], {
            "classification": "substantial",
            "deliverables": [
                {"id": "impl", "kind": "implementation", "scope": ["a.py"],
                 "executor": "worker", "acceptance": ["done"], "dependencies": [], "checks": []},
            ],
        })
        aid = planned["assignments"][0]["id"]
        coordination.assignment_started(self.repo, task["id"], aid, "ws", "codex", [])
        coordination.assignment_finished(self.repo, task["id"], aid, "succeeded", "done", ["a.py"], [])
        coordination.use_result(self.repo, task["id"], aid, "incorporated", "accepted")
        denied = self.hook("PreToolUse", tool_name="apply_patch", tool_use_id="tool-new",
                           tool_input={"command": "*** Add File: docs/new.md\n+text\n"})
        self.assertEqual(denied["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn("unplanned", denied["hookSpecificOutput"]["permissionDecisionReason"].lower())

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
        stopped = self.hook("Stop", stop_hook_active=False)
        self.assertEqual(stopped["decision"], "block")
        self.assertIn("disposition", stopped["reason"].lower())
        coordination.use_result(self.repo, task["id"], aid, "incorporated", "applied finding")
        allowed = self.hook("Stop", stop_hook_active=False)
        self.assertTrue(allowed.get("continue", True))
        self.assertEqual(coordination.load_task(self.repo, task["id"])["status"], "completed")


class ProjectBindingTests(CoordinationCase):
    def test_project_attach_is_activation_marker_without_repo_local_hooks(self):
        self.assertTrue(project.attach(self.repo, coordinator="codex"))
        self.assertTrue(project.is_attached(self.repo, "codex"))
        self.assertFalse((self.repo / ".codex/hooks.json").exists())
        self.assertTrue(project.detach(self.repo, coordinator="codex"))
        self.assertFalse(project.is_attached(self.repo, "codex"))

    def test_global_hook_is_inert_for_unattached_project(self):
        result = coordinator_hooks.handle({
            "session_id": "s", "turn_id": "t", "cwd": str(self.repo),
            "hook_event_name": "UserPromptSubmit", "prompt": "implement",
            "model": "gpt", "permission_mode": "default",
        })
        self.assertEqual(result, {})
        self.assertFalse((self.state / "coordination").exists())


if __name__ == "__main__":
    unittest.main()
