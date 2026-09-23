"""Shared scope matching and stable task selection regressions.

These exercise the real Codex/Claude lifecycle hooks and the persistent ledger
rather than private helpers, so a regression in either layer fails here.
"""
from unittest import mock

from codex_deepseek_team import coordination, coordinator_hooks, project, scope_matching, settings
from test_coordination import CoordinationCase


def _deliverable(identity, kind, scope, executor="coordinator", **extra):
    item = {
        "id": identity, "kind": kind, "scope": list(scope), "executor": executor,
        "acceptance": ["checked"], "dependencies": [], "checks": [],
    }
    item.update(extra)
    return item


class _HookCase(CoordinationCase):
    def setUp(self):
        super().setUp()
        # Scope gates are independent of the target profile; keep the fixture at
        # a level that does not force a worker for every coordinator deliverable.
        settings.set_values(self.repo / settings.PROJECT_FILE,
                            delegation_level=25, access="full-access")
        project.attach(self.repo, coordinator="codex")

    def hook(self, event, **extra):
        payload = {
            "session_id": "scope", "turn_id": "turn-1", "cwd": str(self.repo),
            "hook_event_name": event,
        }
        payload.update(extra)
        return coordinator_hooks.handle(payload)

    def submit(self, deliverables, *, classification="substantial", session="scope"):
        self.hook("UserPromptSubmit", prompt="Implement the feature",
                  session_id=session, turn_id="turn-1")
        task = coordination.latest_task(self.repo, session)
        return coordination.plan_task(self.repo, task["id"], {
            "classification": classification,
            "small_evidence": "One concrete file changes.",
            "deliverables": deliverables,
        })

    def write(self, path, **extra):
        return self.hook("PreToolUse", tool_name="Write",
                         tool_input={"file_path": path, "content": "VALUE = 2\n"}, **extra)

    def decision(self, result):
        return result.get("hookSpecificOutput", {}).get("permissionDecision")

    def reason(self, result):
        return result.get("hookSpecificOutput", {}).get("permissionDecisionReason", "").lower()


class ScopeContainmentTests(_HookCase):
    def setUp(self):
        super().setUp()
        (self.repo / "src").mkdir()
        (self.repo / "src" / "core.py").write_text("VALUE = 1\n")
        (self.repo / "src" / "core1.py").write_text("VALUE = 1\n")

    def test_glob_scope_allows_matching_write_but_not_directory_delete(self):
        self.submit([_deliverable("impl", "implementation", ["src/*.py"])])
        self.assertEqual(self.write("src/core.py"), {})
        denied = self.hook("PreToolUse", tool_name="Bash", tool_input={"command": "rm -rf src"})
        self.assertEqual(self.decision(denied), "deny")
        self.assertIn("unplanned", self.reason(denied))

    def test_plain_directory_scope_covers_descendants(self):
        self.submit([_deliverable("impl", "implementation", ["src"])])
        self.assertEqual(self.write("src/core.py"), {})
        self.assertEqual(self.write("src/deep/nested.py"), {})

    def test_neighboring_prefix_is_not_covered(self):
        self.submit([_deliverable("impl", "implementation", ["src"])])
        denied = self.write("src_backup/core.py")
        self.assertEqual(self.decision(denied), "deny")
        self.assertIn("unplanned", self.reason(denied))

    def test_question_mark_and_character_class_globs(self):
        cases = [
            (["src/core?.py"], "src/core1.py", True),
            (["src/core?.py"], "src/core12.py", False),
            (["src/[c]ore.py"], "src/core.py", True),
            (["src/[c]ore.py"], "src/xore.py", False),
        ]
        for index, (scope, path, allowed) in enumerate(cases):
            with self.subTest(scope=scope, path=path):
                session = "class-" + str(index)
                self.submit([_deliverable("impl", "implementation", scope)], session=session)
                result = self.write(path, session_id=session)
                if allowed:
                    self.assertEqual(result, {}, result)
                else:
                    self.assertEqual(self.decision(result), "deny", result)

    def test_symlinked_mutation_resolves_inside_and_outside(self):
        (self.repo / "alias.py").symlink_to("src/core.py")
        (self.root / "outside.txt").write_text("outside\n")
        (self.repo / "escape.txt").symlink_to(self.root / "outside.txt")
        self.submit([_deliverable("impl", "implementation", ["src/core.py"])])
        self.assertEqual(self.write("alias.py"), {})
        denied = self.write("escape.txt")
        self.assertEqual(self.decision(denied), "deny")
        self.assertIn("outside the attached project", self.reason(denied))

    def test_whole_project_scope_authorizes_root_level_paths(self):
        self.submit([_deliverable("impl", "implementation", ["*"])])
        self.assertEqual(self.write("src/core.py"), {})
        self.assertEqual(self.write("README.md"), {})

    def test_external_scope_cannot_grant_authority(self):
        self.submit([_deliverable("impl", "implementation", ["../outside"])])
        denied = self.write("src/core.py")
        self.assertEqual(self.decision(denied), "deny")
        self.assertIn("unplanned", self.reason(denied))

    def test_unresolvable_paths_do_not_grant_authority(self):
        for error in (OSError('unreadable path'), RuntimeError('symlink loop')):
            with self.subTest(error=type(error).__name__), \
                    mock.patch('pathlib.Path.resolve', side_effect=error):
                self.assertIsNone(scope_matching.canonical(self.repo, 'src/core.py'))


class ScopeConflictTests(_HookCase):
    def setUp(self):
        super().setUp()
        (self.repo / "src").mkdir()
        (self.repo / "src" / "core.py").write_text("VALUE = 1\n")

    def test_pending_worker_glob_conflicts_even_with_integration_scope(self):
        self.submit([
            _deliverable("integration", "integration", ["src"]),
            _deliverable("worker-impl", "implementation", ["src/*.py"], executor="worker"),
        ])
        denied = self.write("src/core.py")
        self.assertEqual(self.decision(denied), "deny")
        self.assertIn("pending worker", self.reason(denied))

    def test_directory_delete_conflicts_with_contained_worker_scope(self):
        self.submit([
            _deliverable("integration", "integration", ["src"]),
            _deliverable("worker-impl", "implementation", ["src/core.py"], executor="worker"),
        ])
        denied = self.hook("PreToolUse", tool_name="Bash", tool_input={"command": "rm -r src"})
        self.assertEqual(self.decision(denied), "deny")
        self.assertIn("pending worker", self.reason(denied))

    def test_directory_mutation_invalidates_contained_accepted_result(self):
        task = self.submit([_deliverable("impl", "implementation", ["src/core.py"])])
        coordination.observe_coordinator_result(
            self.repo, task["id"], "impl", "accepted", "verified before edit")
        coordination.record_coordinator_event(
            self.repo, task["id"], "mutation_requested", ["src"])
        self.assertTrue(coordination.completion_issues(
            coordination.load_task(self.repo, task["id"])))

    def test_nested_directory_mutation_conflicts_with_worker_glob(self):
        for index, scope in enumerate(('src/*/core.py', 'src/*.py', 'src/pkg?/core.py',
                                       'src/[p]kg/core.py')):
            with self.subTest(scope=scope):
                session = 'nested-glob-' + str(index)
                self.submit([
                    _deliverable('integration', 'integration', ['src']),
                    _deliverable('worker-impl', 'implementation', [scope], executor='worker'),
                ], session=session)
                denied = self.hook('PreToolUse', tool_name='Bash', session_id=session,
                                   tool_input={'command': 'rm -rf src/pkg src/pkg1'})
                self.assertEqual(self.decision(denied), 'deny')
                self.assertIn('pending worker', self.reason(denied))

    def test_nested_directory_mutation_invalidates_glob_result(self):
        task = self.submit([_deliverable('impl', 'implementation', ['src/*/core.py'])])
        coordination.observe_coordinator_result(
            self.repo, task['id'], 'impl', 'accepted', 'verified before edit')
        coordination.record_coordinator_event(
            self.repo, task['id'], 'mutation_requested', ['src/pkg'])
        self.assertTrue(coordination.completion_issues(
            coordination.load_task(self.repo, task['id'])))

    def test_glob_intersection_is_conservative_without_neighbor_conflicts(self):
        self.assertTrue(scope_matching.overlaps('src/[ab].py', 'src/[bc].py'))
        self.assertFalse(scope_matching.overlaps('src/a*.py', 'src/b*.py'))
        self.assertFalse(scope_matching.overlaps('src/pkg', 'src/other*.py'))
        self.assertFalse(scope_matching.overlaps('src_backup', 'src/*.py'))

    def test_known_nonmatching_file_does_not_conflict_with_worker_glob(self):
        (self.repo / 'src/notes.md').write_text('notes\n')
        self.submit([
            _deliverable('integration', 'integration', ['src']),
            _deliverable('worker-impl', 'implementation', ['src/*.py'], executor='worker'),
        ])
        self.assertEqual(self.write('src/notes.md'), {})

    def test_unknown_path_type_detects_intersection_without_expanding_authority(self):
        task = self.submit([_deliverable("impl", "implementation", ["src/*.py"])])
        coordination.observe_coordinator_result(
            self.repo, task["id"], "impl", "accepted", "verified before edit")
        # A directory path is not authorized by the glob scope (authoritative)
        # but must still intersect it for invalidation (conservative).
        denied = self.hook("PreToolUse", tool_name="Bash", tool_input={"command": "rm -rf src"})
        self.assertEqual(self.decision(denied), "deny")
        coordination.record_coordinator_event(
            self.repo, task["id"], "mutation_requested", ["src"])
        self.assertTrue(coordination.completion_issues(
            coordination.load_task(self.repo, task["id"])))


class TaskSelectionTests(_HookCase):
    def set_created(self, task_id, created):
        record = coordination.load_task(self.repo, task_id)
        record["created_at"] = created
        coordination._atomic(coordination._task_path(self.repo, task_id), record)

    def outstanding(self, task_id, scope=("a.py",), identity="review"):
        return coordination.plan_task(self.repo, task_id, {
            "classification": "small",
            "small_evidence": "One review with a single outstanding result.",
            "deliverables": [_deliverable(identity, "review", list(scope))],
        })

    def open(self, turn, prompt="work"):
        return coordination.open_task(
            self.repo, session_id="scope", turn_id=turn, prompt=prompt,
            policy=settings.resolve(self.repo))

    def test_begin_turn_retains_older_active_after_newer_completed_update(self):
        older = self.open("t-1")
        self.outstanding(older["id"])
        newer = self.open("t-2")
        self.outstanding(newer["id"], scope=("docs/notes.md",))
        coordination.observe_coordinator_result(
            self.repo, newer["id"], "review", "accepted", "reviewed")
        coordination.complete_task(self.repo, newer["id"])
        # A later cost/evidence update on the completed task must not hide the
        # still-unfinished older task from any lifecycle hook.
        coordination.observe_coordinator_result(
            self.repo, newer["id"], "review", "accepted", "reviewed again")
        self.set_created(older["id"], 100.0)
        self.set_created(newer["id"], 200.0)
        self.assertEqual(coordination.latest_task(self.repo, "scope")["id"], newer["id"])
        self.assertEqual(coordination.active_task(self.repo, "scope")["id"], older["id"])

        context = self.hook("SessionStart", source="compact")
        text = context["hookSpecificOutput"]["additionalContext"]
        self.assertIn(older["id"], text)

        allowed = self.write("a.py")
        self.assertEqual(allowed, {}, allowed)
        events = coordination.load_task(self.repo, older["id"])["coordinator_events"]
        self.assertTrue(any(row["kind"] == "mutation_requested" for row in events))
        self.assertEqual(coordination.load_task(self.repo, newer["id"])["status"], "completed")

        stopped = self.hook("Stop", stop_hook_active=False)
        self.assertEqual(stopped.get("decision"), "block", stopped)
        self.assertIn("outstanding", stopped.get("reason", ""))

    def test_user_prompt_retains_newest_unfinished_and_lists_the_rest(self):
        first = self.open("t-1")
        self.outstanding(first["id"])
        second = self.open("t-2")
        self.outstanding(second["id"])
        self.set_created(first["id"], 100.0)
        self.set_created(second["id"], 200.0)
        self.assertEqual(coordination.active_task(self.repo, "scope")["id"], second["id"])

        context = self.hook("SessionStart", source="compact")[
            "hookSpecificOutput"]["additionalContext"]
        self.assertIn(first["id"], context)
        self.assertIn(second["id"], context)

        self.hook("UserPromptSubmit", prompt="What is the status?", turn_id="t-3")
        self.assertEqual(coordination.latest_task(self.repo, "scope")["id"], second["id"])
        self.assertEqual(coordination.load_task(self.repo, first["id"])["status"], "planned")
        self.assertEqual(coordination.load_task(self.repo, second["id"])["status"], "planned")

        continued = coordination.begin_turn(
            self.repo, session_id="scope", turn_id="t-4", prompt="more",
            policy=settings.resolve(self.repo))
        self.assertEqual(continued["id"], second["id"])
        self.assertEqual(coordination.load_task(self.repo, first["id"])["status"], "planned")

    def test_missing_or_invalid_created_at_sorts_first_without_migration(self):
        legacy = self.open("t-legacy")
        self.outstanding(legacy["id"])
        valid = self.open("t-valid")
        self.outstanding(valid["id"])
        for task_id, created in ((legacy["id"], None), (valid["id"], 0.25)):
            record = coordination.load_task(self.repo, task_id)
            record.pop("created_at", None)
            if created is not None:
                record["created_at"] = created
            coordination._atomic(coordination._task_path(self.repo, task_id), record)
        # The record with a finite numeric created_at is newer than the default 0.
        self.assertEqual(coordination.active_task(self.repo, "scope")["id"], valid["id"])
        # Repeated reads stay stable and never rewrite legacy records.
        self.assertEqual(coordination.active_task(self.repo, "scope")["id"], valid["id"])
        self.assertNotIn("created_at", coordination.load_task(self.repo, legacy["id"]))

    def test_equal_created_at_falls_back_to_id_tie_break(self):
        one = self.open("t-one")
        self.outstanding(one["id"])
        two = self.open("t-two")
        self.outstanding(two["id"])
        self.set_created(one["id"], 5.0)
        self.set_created(two["id"], 5.0)
        expected = max(one["id"], two["id"])
        self.assertEqual(coordination.active_task(self.repo, "scope")["id"], expected)
        self.assertEqual(coordination.latest_task(self.repo, "scope")["id"], expected)

    def test_completed_only_session_falls_back_to_latest_task(self):
        task = self.open("t-1")
        self.outstanding(task["id"])
        coordination.observe_coordinator_result(
            self.repo, task["id"], "review", "accepted", "reviewed")
        coordination.complete_task(self.repo, task["id"])
        self.assertIsNone(coordination.active_task(self.repo, "scope"))
        self.assertEqual(coordination.latest_task(self.repo, "scope")["id"], task["id"])
        context = self.hook("SessionStart")["hookSpecificOutput"]["additionalContext"]
        self.assertIn(task["id"], context)
        stopped = self.hook("Stop")
        self.assertNotEqual(stopped.get("decision"), "block", stopped)
        self.assertEqual(coordination.load_task(self.repo, task["id"])["status"], "completed")

    def test_stop_checks_older_work_after_finishing_newer_task(self):
        for index, draft in enumerate((False, True)):
            with self.subTest(draft=draft):
                older = self.open('older-' + str(index))
                self.outstanding(older['id'])
                newer = self.open('newer-' + str(index))
                self.set_created(older['id'], 100.0 + index * 2)
                self.set_created(newer['id'], 101.0 + index * 2)
                if not draft:
                    self.outstanding(newer['id'])
                    coordination.observe_coordinator_result(
                        self.repo, newer['id'], 'review', 'accepted', 'checked')
                stopped = self.hook('Stop', stop_hook_active=False)
                self.assertEqual(stopped.get('decision'), 'block', stopped)
                self.assertIn(older['id'], stopped.get('reason', ''))
                self.assertIn(coordination.load_task(self.repo, newer['id'])['status'],
                              ('closed', 'completed'))
                repeated = self.hook('Stop', stop_hook_active=True)
                self.assertNotEqual(repeated.get('decision'), 'block')
                self.assertIn(older['id'], repeated.get('systemMessage', ''))
                self.assertEqual(coordination.load_task(self.repo, older['id'])['status'], 'planned')

    def test_created_at_handles_nonfinite_and_large_numbers(self):
        one = self.open('t-one')
        two = self.open('t-two')
        self.set_created(two['id'], 1)
        for value in (None, 'invalid', True, float('nan'), float('inf')):
            with self.subTest(value=value):
                self.set_created(one['id'], value)
                self.assertEqual(coordination.latest_task(self.repo, 'scope')['id'], two['id'])
        self.set_created(one['id'], 10 ** 400)
        self.assertEqual(coordination.latest_task(self.repo, 'scope')['id'], one['id'])

    def test_newer_task_cannot_bypass_older_pending_worker(self):
        older = self.submit([
            _deliverable('worker-impl', 'implementation', ['a.py'], executor='worker')])
        newer = self.open('newer')
        self.outstanding(newer['id'])
        self.set_created(older['id'], 1)
        self.set_created(newer['id'], 2)
        denied = self.write('a.py')
        self.assertEqual(self.decision(denied), 'deny')
        self.assertIn('pending worker', self.reason(denied))

    def test_mutation_invalidates_overlapping_older_unfinished_result(self):
        older = self.open('older')
        self.outstanding(older['id'])
        coordination.observe_coordinator_result(
            self.repo, older['id'], 'review', 'accepted', 'checked')
        newer = self.open('newer')
        self.outstanding(newer['id'])
        self.set_created(older['id'], 1)
        self.set_created(newer['id'], 2)
        self.assertEqual(self.write('a.py'), {})
        self.assertTrue(coordination.completion_issues(
            coordination.load_task(self.repo, older['id'])))

    def test_native_dispatch_cannot_bypass_older_pending_worker(self):
        older = self.submit([
            _deliverable('worker-impl', 'implementation', ['a.py'], executor='worker')])
        newer = self.open('native-newer')
        coordination.plan_task(self.repo, newer['id'], {
            'classification': 'small', 'small_evidence': 'One explicitly requested native edit',
            'deliverables': [_deliverable(
                'native-edit', 'implementation', ['a.py'], executor='native-agent',
                delegation_reason='The user explicitly requested this native agent',
                native_exception={'code': 'explicit_user_request',
                                  'evidence': 'The user specifically requested a native agent for this edit'})]})
        self.set_created(older['id'], 1)
        self.set_created(newer['id'], 2)
        denied = self.hook('PreToolUse', tool_name='spawn_agent', tool_use_id='new-native',
            tool_input={'prompt': '[deepseek-team:' + newer['id'] + ':native-edit] Edit a.py'})
        self.assertEqual(self.decision(denied), 'deny', denied)
        self.assertIn('pending worker', self.reason(denied))
