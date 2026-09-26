"""Contract tests for the private delegation lessons journal and rule service.

These tests exercise the shared Task 1 interface from
``docs/superpowers/specs/2026-09-26-delegation-lessons.md`` only: append-only
outcome events, versioned reviews, advisory rule snapshots and guidance.
"""
import fcntl
import hashlib
import os
from pathlib import Path
import re
import sqlite3
import stat
import tempfile
import threading
import unittest
from unittest import mock

from codex_deepseek_team import lessons
from codex_deepseek_team.lessons import LessonError

_DEFAULT = object()


class LessonsTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="dst-lessons-")
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.repo = self.base / "project"
        self.repo.mkdir()
        self.home = self.base / "home"
        self.home.mkdir()
        self.state = self.base / "state"
        self.env = mock.patch.dict(os.environ, {
            "DEEPSEEK_TEAM_STATE_DIR": str(self.state),
            "HOME": str(self.home),
        })
        self.env.start()
        self.addCleanup(self.env.stop)

    # -- helpers ---------------------------------------------------------
    def project(self, name="other"):
        path = self.base / name
        path.mkdir(exist_ok=True)
        return path

    def key(self, project=None):
        resolved = str((project or self.repo).resolve())
        return hashlib.sha256(os.fsencode(resolved)).hexdigest()[:24]

    def lesson_dir(self, project=None):
        return self.state / "lessons" / self.key(project)

    def ctx(self, kind=None, effort=None, version=None, **extra):
        context = dict(extra)
        features = {}
        if kind is not None:
            features["kind"] = kind
        if effort is not None:
            features["effort"] = effort
        if features:
            context["features"] = features
        if version is not None:
            context["lessons"] = {"version": version}
        return context

    def record(self, task, assignment, disposition="incorporated", *, evidence="worker output accepted",
               context=None, rework=None, root=None):
        return lessons.record_outcome(
            root or self.repo, task_id=task, assignment_id=assignment,
            disposition=disposition, evidence=evidence,
            context=self.ctx() if context is None else context, rework=rework,
        )

    def clean_case(self, index, *, kind="implementation", effort="high", root=None):
        return self.record(f"task-{index:04d}", f"assignment-{index:04d}",
                           context=self.ctx(kind=kind, effort=effort, version=0), root=root)

    def rework_case(self, index, cause, *, kind="implementation", severity="major", root=None):
        return self.record(
            f"task-{index:04d}", f"assignment-{index:04d}", "needs-rework",
            evidence=f"correction required for case {index}",
            context=self.ctx(kind=kind, effort="high", version=0),
            rework={"cause": cause, "severity": severity, "summary": f"failure {index}",
                    "prevention": "tighten the brief"},
            root=root,
        )

    def case_id(self, index):
        return f"task-{index:04d}/assignment-{index:04d}"

    def rule(self, rid="rule-1", *, when=None, condition="Use a bounded brief",
             action="Split the work before delegating", evidence):
        return {"id": rid, "when": when or {}, "condition": condition,
                "action": action, "evidence": list(evidence)}

    def apply(self, rules, *, expected=_DEFAULT, through=_DEFAULT, summary="coordinator review",
              root=None):
        root = root or self.repo
        status = lessons.status(root)
        payload = {
            "expected_version": status["version"] if expected is _DEFAULT else expected,
            "through_event": status["through_event"] if through is _DEFAULT else through,
            "summary": summary,
            "rules": rules,
        }
        return lessons.apply_review(root, payload)

    def canonical_version(self):
        database = self.lesson_dir() / "journal.sqlite3"
        if not database.exists():
            return 0
        db = sqlite3.connect(database)
        try:
            row = db.execute("SELECT MAX(version) FROM reviews").fetchone()
            return row[0] or 0
        finally:
            db.close()

    def digests(self, project=None):
        directory = self.lesson_dir(project)
        if not directory.exists():
            return {}
        return {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                for path in sorted(directory.iterdir()) if path.is_file() and not path.is_symlink()}


class CoordinatorReviewRegressionTests(LessonsTestCase):
    def test_structured_correction_on_incorporation_is_not_clean(self):
        for index in range(3):
            self.record(f"task-{index}", f"assignment-{index}",
                        context=self.ctx(kind="implementation", effort="high", version=0),
                        rework={"cause": "context_gap", "severity": "minor",
                                "summary": "Coordinator supplied a missing input before acceptance.",
                                "prevention": "Declare and import the required inputs."})
        result = lessons.status(self.repo)
        self.assertEqual(result["counts"]["clean"], 0)
        self.assertEqual(result["counts"]["rework"], 3)
        self.assertTrue(result["review_due"])

    def test_unknown_feature_does_not_match_constrained_rule(self):
        self.clean_case(1)
        self.apply([self.rule(when={"domain": "unknown"}, evidence=[self.case_id(1)])])
        self.assertEqual(lessons.snapshot(self.repo, {"domain": "unknown"})["rule_ids"], [])

    # -- corrective regressions from the independent review -----------------
    def test_read_snapshot_is_transactional_across_a_concurrent_review(self):
        for index in range(3):
            self.clean_case(index)
        self.apply([])
        before = lessons.review_bundle(self.repo)
        self.assertEqual((before["expected_version"], before["reviewed_through"],
                          before["through_event"]), (1, 3, 3))

        events_read = threading.Event()
        writer_started = threading.Event()
        writer_finished = threading.Event()
        reader_result, writer_result = {}, {}
        blocked = []
        original_reviews = lessons._reviews
        reader_thread = None

        def gated_reviews(db):
            if threading.current_thread() is reader_thread:
                # The journal is read but the review tables are not: the exact
                # window in which an interleaved commit used to leak into a read.
                events_read.set()
                writer_started.wait(timeout=10)
                writer_finished.wait(timeout=3)
                blocked.append(not writer_finished.is_set())
            return original_reviews(db)

        def read():
            try:
                reader_result["bundle"] = lessons.review_bundle(self.repo)
            except BaseException as error:  # surfaced by the assertions below
                reader_result["error"] = error

        def write():
            writer_started.set()
            try:
                self.clean_case(3)
                writer_result["status"] = self.apply([], expected=1)
            except BaseException as error:  # surfaced by the assertions below
                writer_result["error"] = error
            finally:
                writer_finished.set()

        with mock.patch.object(lessons, "_reviews", gated_reviews):
            reader_thread = threading.Thread(target=read)
            reader_thread.start()
            self.assertTrue(events_read.wait(timeout=10))
            writer_thread = threading.Thread(target=write)
            writer_thread.start()
            reader_thread.join(timeout=30)
            writer_thread.join(timeout=30)
        self.assertFalse(reader_thread.is_alive())
        self.assertFalse(writer_thread.is_alive())
        self.assertIsNone(reader_result.get("error"))
        self.assertIsNone(writer_result.get("error"))
        bundle = reader_result["bundle"]
        # The bundle is one canonical snapshot, never a mixture of two states.
        self.assertEqual(bundle["through_event"], 3)
        self.assertEqual(bundle["reviewed_through"], 3)
        self.assertLessEqual(bundle["reviewed_through"], bundle["through_event"])
        case_ids = {case["case_id"] for case in bundle["cases"]}
        for rule in bundle["rules"]:
            for case_id in rule["evidence"]:
                self.assertIn(case_id, case_ids)
        self.assertEqual(bundle["cases_since_review"], 0)
        # The real interleaved commit was attempted inside the read window and
        # had to wait for the snapshot to close before it could land.
        self.assertEqual(blocked, [True])
        self.assertEqual(writer_result["status"]["version"], 2)
        self.assertEqual(lessons.status(self.repo)["version"], 2)
        self.assertEqual(lessons.status(self.repo)["through_event"], 4)

    def test_derived_markdown_never_leads_the_canonical_review(self):
        self.clean_case(1)
        rule = self.rule("rule-a", evidence=[self.case_id(1)])
        real_replace = os.replace

        def crash_after_rename(src, dst, *args, **kwargs):
            real_replace(src, dst, *args, **kwargs)
            raise SystemExit("simulated crash after the derived Markdown rename")

        with mock.patch.object(lessons.os, "replace", side_effect=crash_after_rename):
            with self.assertRaises(SystemExit):
                self.apply([rule])
        # Canonical SQLite is the authority: the crash point may not leave the
        # derived Markdown ahead of a review that was rolled back.
        canonical = lessons.snapshot(self.repo)
        self.assertEqual(canonical["version"], 1)
        self.assertEqual(canonical["rule_ids"], ["rule-a"])
        markdown = Path(lessons.status(self.repo)["rules_file"])
        self.assertTrue(markdown.exists())
        version = re.search(r"^Version: (\d+)$", markdown.read_text(), re.M)
        self.assertIsNotNone(version)
        self.assertEqual(int(version.group(1)), canonical["version"])

    def test_publish_failure_after_commit_reports_the_committed_review(self):
        self.clean_case(1)
        rule = self.rule("rule-a", evidence=[self.case_id(1)])

        def fail_publish(src, dst, *args, **kwargs):
            raise OSError("simulated mirror write failure")

        with mock.patch.object(lessons.os, "replace", side_effect=fail_publish):
            with self.assertRaises(LessonError) as caught:
                self.apply([rule])
        self.assertIn("committed", str(caught.exception))
        # Canonical state kept the review instead of pretending it rolled back.
        self.assertEqual(lessons.status(self.repo)["version"], 1)
        self.assertEqual(lessons.snapshot(self.repo)["rule_ids"], ["rule-a"])
        self.assertEqual([path.name for path in self.lesson_dir().iterdir()
                          if path.name.endswith(".tmp")], [])
        # The derived mirror is regenerated from canonical state by the next review.
        self.apply([rule])
        markdown = Path(lessons.status(self.repo)["rules_file"])
        self.assertIn("Version: 2", markdown.read_text())

    def test_markdown_publish_holds_the_writer_lock_and_committed_version(self):
        self.clean_case(1)
        case = self.case_id(1)
        first, second = self.rule("rule-a", evidence=[case]), self.rule("rule-b", evidence=[case])
        publishing = threading.Event()
        release_publish = threading.Event()
        second_started = threading.Event()
        real_replace = os.replace
        results = {}
        calls = []

        def gated_replace(src, dst, *args, **kwargs):
            calls.append(dst)
            if len(calls) == 1:
                publishing.set()
                release_publish.wait(timeout=10)
            return real_replace(src, dst, *args, **kwargs)

        def first_apply():
            try:
                results["first"] = self.apply([first])
            except BaseException as error:  # surfaced by the assertions below
                results["first_error"] = error

        def second_apply():
            second_started.set()
            try:
                results["second"] = self.apply([second])
            except BaseException as error:  # surfaced by the assertions below
                results["second_error"] = error

        try:
            with mock.patch.object(lessons.os, "replace", side_effect=gated_replace):
                first_thread = threading.Thread(target=first_apply)
                first_thread.start()
                self.assertTrue(publishing.wait(timeout=10))
                second_thread = threading.Thread(target=second_apply)
                second_thread.start()
                self.assertTrue(second_started.wait(timeout=10))
                # The mirror is published only after its review committed, and the
                # publisher still owns the writer lock, so a later review cannot
                # commit in between or be overwritten by a stale mirror.
                self.assertEqual(self.canonical_version(), 1)
                lock = os.open(self.lesson_dir() / "lessons.lock", os.O_RDWR)
                try:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                finally:
                    os.close(lock)
                release_publish.set()
                first_thread.join(timeout=30)
                second_thread.join(timeout=30)
        finally:
            release_publish.set()
        self.assertEqual(results.get("first_error"), None)
        self.assertEqual(results.get("second_error"), None)
        self.assertFalse(first_thread.is_alive())
        self.assertFalse(second_thread.is_alive())
        self.assertEqual(lessons.status(self.repo)["version"], 2)
        self.assertEqual(self.canonical_version(), 2)
        markdown = Path(lessons.status(self.repo)["rules_file"])
        self.assertIn("Version: 2", markdown.read_text())
        self.assertEqual(lessons.snapshot(self.repo)["rule_ids"], ["rule-b"])

    def test_malformed_feature_metadata_reads_back_as_unknown(self):
        self.clean_case(1, kind="banana")
        self.clean_case(2, kind="documentation")
        self.clean_case(3, kind="implementation", effort="turbo")
        status = lessons.status(self.repo)
        self.assertEqual(status["cohorts"],
                         [{"kind": "documentation", "effort": "high", "version": 0,
                           "cases": 1, "clean": 1, "rework": 0, "rejected": 0},
                          {"kind": "implementation", "effort": "unknown", "version": 0,
                           "cases": 1, "clean": 1, "rework": 0, "rejected": 0},
                          {"kind": "unknown", "effort": "high", "version": 0,
                           "cases": 1, "clean": 1, "rework": 0, "rejected": 0}])
        bundle = lessons.review_bundle(self.repo)
        self.assertEqual({case["case_id"]: case["kind"] for case in bundle["cases"]},
                         {self.case_id(1): "unknown", self.case_id(2): "documentation",
                          self.case_id(3): "implementation"})

    def test_unknown_kind_cannot_manufacture_the_rework_threshold(self):
        for index in range(1, 4):
            self.rework_case(index, "brief_gap", kind="banana")
        status = lessons.status(self.repo)
        self.assertFalse(status["review_due"])
        self.assertEqual(status["due_reasons"], [])
        self.assertEqual(status["cohorts"][0]["kind"], "unknown")
        other = self.project("literal-unknown")
        for index in range(1, 4):
            self.rework_case(index, "brief_gap", kind="unknown", root=other)
        self.assertFalse(lessons.status(other)["review_due"])

    def test_unknown_structured_correction_counts_regardless_of_disposition(self):
        self.record("task-0001", "assignment-0001", "incorporated",
                    evidence="accepted with an unclassified correction",
                    context=self.ctx(kind="implementation", effort="high", version=0),
                    rework={"cause": "unknown", "severity": "minor",
                            "summary": "cause not classified", "prevention": "keep evidence"})
        self.record("task-0002", "assignment-0002", "reproduced",
                    evidence="reproduced with an unclassified correction",
                    context=self.ctx(kind="implementation", effort="high", version=0),
                    rework={"cause": "unknown", "severity": "unknown",
                            "summary": "cause not classified", "prevention": "keep evidence"})
        self.record("task-0003", "assignment-0003", "incorporated",
                    evidence="accepted with a classified correction",
                    context=self.ctx(kind="implementation", effort="high", version=0),
                    rework={"cause": "brief_gap", "severity": "minor",
                            "summary": "brief was ambiguous", "prevention": "tighten the brief"})
        status = lessons.status(self.repo)
        self.assertEqual(status["counts"]["unknown_attribution"], 2)
        self.assertEqual(status["counts"], {"cases": 3, "clean": 0, "rework": 3,
                                            "rejected": 0, "unknown_attribution": 2})
        self.assertEqual(status["causes"], {"brief_gap": 1, "unknown": 2})


class JournalTests(LessonsTestCase):
    def test_append_replay_and_stable_sequence(self):
        first = self.record("task-0001", "assignment-0001")
        second = self.record(
            "task-0001", "assignment-0001", "needs-rework", evidence="review found a gap",
            context=self.ctx(kind="implementation", effort="high", version=0),
            rework={"cause": "brief_gap", "severity": "minor", "summary": "brief was ambiguous",
                    "prevention": "state acceptance checks up front"},
        )
        # The payload is replayed literally (same keys, different insertion order).
        replay = lessons.record_outcome(
            self.repo, task_id="task-0001", assignment_id="assignment-0001",
            disposition="incorporated", evidence="worker output accepted", context={},
        )
        self.assertEqual(replay["seq"], first["seq"])
        self.assertNotEqual(second["seq"], first["seq"])
        events = lessons.journal(self.repo)
        self.assertEqual([event["seq"] for event in events], [1, 2])
        self.assertEqual(sorted(events[0]), ["at", "case_id", "context", "disposition",
                                             "evidence", "rework", "seq"])
        self.assertEqual(events[0]["case_id"], "task-0001/assignment-0001")
        self.assertEqual(events[1]["disposition"], "needs-rework")
        self.assertEqual(events[1]["rework"]["cause"], "brief_gap")
        self.assertEqual([event["seq"] for event in lessons.journal(self.repo)], [1, 2])

    def test_two_dispositions_one_case_and_clean_denominator(self):
        self.clean_case(1)
        self.clean_case(2)
        self.rework_case(3, "worker_error")
        self.record("task-0003", "assignment-0003", "incorporated",
                    evidence="reworked result incorporated",
                    context=self.ctx(kind="implementation", effort="high", version=0))
        self.record("task-0004", "assignment-0004", "rejected", evidence="out of scope",
                    context=self.ctx(), rework={"cause": "requirements_changed", "severity": "redo",
                                                "summary": "scope changed", "prevention": "confirm scope"})
        status = lessons.status(self.repo)
        self.assertEqual(status["counts"], {"cases": 4, "clean": 2, "rework": 1,
                                            "rejected": 1, "unknown_attribution": 0})
        # Historical rework survives a later incorporation and stays one case.
        self.assertEqual(len(lessons.journal(self.repo)), 5)

    def test_reproduced_and_rejected_buckets(self):
        self.record("task-0001", "assignment-0001", "reproduced", evidence="risk reproduced as expected")
        self.record("task-0002", "assignment-0002", "rejected", evidence="not applicable")
        counts = lessons.status(self.repo)["counts"]
        self.assertEqual(counts, {"cases": 2, "clean": 1, "rework": 0,
                                  "rejected": 1, "unknown_attribution": 1})

    def test_rework_and_rejection_remain_separately_visible(self):
        self.rework_case(1, "integration")
        self.record("task-0001", "assignment-0001", "rejected", evidence="rejected after rework",
                    context=self.ctx(),
                    rework={"cause": "integration", "severity": "redo",
                            "summary": "downstream contract changed", "prevention": "freeze the contract"})
        counts = lessons.status(self.repo)["counts"]
        # One case is visible in both buckets; the clean denominator stays honest.
        self.assertEqual(counts, {"cases": 1, "clean": 0, "rework": 1,
                                  "rejected": 1, "unknown_attribution": 0})

    def test_case_identity_is_task_and_assignment(self):
        self.record("task-0001", "assignment-0001")
        self.record("task-0001", "assignment-0002")
        self.record("task-0002", "assignment-0001")
        self.assertEqual(lessons.status(self.repo)["counts"]["cases"], 3)

    def test_repeated_dispositions_do_not_manufacture_cases(self):
        self.record("task-0001", "assignment-0001")
        self.record("task-0001", "assignment-0001", "incorporated", evidence="second acceptance note")
        status = lessons.status(self.repo)
        self.assertEqual(status["counts"]["cases"], 1)
        self.assertEqual(len(lessons.journal(self.repo)), 2)

    def test_validation_rejects_bad_input_and_keeps_state(self):
        with self.assertRaises(LessonError):
            self.record("task-0001", "assignment-0001", "accepted")
        with self.assertRaises(LessonError):
            self.record("task-0001", "assignment-0001", evidence="   ")
        with self.assertRaises(LessonError):
            self.record("task-0001", "assignment-0001", context=["not", "an", "object"])
        with self.assertRaises(LessonError):
            self.record("task-0001", "assignment-0001", context={"bad": {1, 2}})
        with self.assertRaises(LessonError):
            self.record("task/with/slash", "assignment-0001")
        with self.assertRaises(LessonError):
            self.record("", "assignment-0001")
        self.assertEqual(lessons.journal(self.repo), [])

    def test_rework_validation_is_closed_and_bounded(self):
        valid = {"cause": "unknown", "severity": "unknown", "summary": "not classified",
                 "prevention": "review the brief"}
        self.assertEqual(lessons.validate_rework(valid), valid)
        self.assertIsNone(lessons.validate_rework(None))
        bad_values = [
            "cause",
            {"cause": "worker_error", "severity": "major", "summary": "s", "prevention": "p", "extra": "x"},
            {"cause": "worker_error", "severity": "major", "summary": "s"},
            {"cause": "nonsense", "severity": "major", "summary": "s", "prevention": "p"},
            {"cause": "worker_error", "severity": "nonsense", "summary": "s", "prevention": "p"},
            {"cause": "worker_error", "severity": "major", "summary": "  ", "prevention": "p"},
            {"cause": "worker_error", "severity": "major", "summary": 3, "prevention": "p"},
            {"cause": "worker_error", "severity": "major", "summary": "s", "prevention": "p" * 2001},
        ]
        for value in bad_values:
            with self.subTest(value=value):
                with self.assertRaises(LessonError):
                    lessons.validate_rework(value)
        with self.assertRaises(LessonError):
            self.record("task-0001", "assignment-0001", "needs-rework",
                        rework={"cause": "worker_error", "severity": "major",
                                "summary": "s", "prevention": "p", "evidence": "elsewhere"})
        self.assertEqual(lessons.journal(self.repo), [])

    def test_context_and_evidence_are_bounded(self):
        with self.assertRaises(LessonError):
            self.record("task-0001", "assignment-0001", context={"blob": "x" * (1024 * 1024 + 1)})
        with self.assertRaises(LessonError):
            self.record("task-0001", "assignment-0001", evidence="x" * 4001)
        self.assertEqual(lessons.journal(self.repo), [])

    def test_concurrent_appends_preserve_all_events(self):
        results = []
        errors = []

        def append(index):
            try:
                results.append(self.clean_case(index))
            except Exception as error:  # pragma: no cover - failure surfaced below
                errors.append(error)

        threads = [threading.Thread(target=append, args=(index,)) for index in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(sorted(event["seq"] for event in results), list(range(1, 9)))
        self.assertEqual(len(lessons.journal(self.repo)), 8)

    def test_read_only_empty_state_creates_nothing(self):
        self.assertEqual(lessons.journal(self.repo), [])
        self.assertEqual(lessons.snapshot(self.repo),
                         {"version": 0, "rule_ids": [], "rules": []})
        status = lessons.status(self.repo)
        self.assertEqual(status["version"], 0)
        self.assertEqual(status["through_event"], 0)
        self.assertEqual(status["counts"], {"cases": 0, "clean": 0, "rework": 0,
                                            "rejected": 0, "unknown_attribution": 0})
        self.assertEqual(status["cohorts"], [])
        self.assertEqual((status["causes"], status["severities"]), ({}, {}))
        self.assertEqual(status["cases_since_review"], 0)
        self.assertFalse(status["review_due"])
        bundle = lessons.review_bundle(self.repo)
        self.assertEqual((bundle["expected_version"], bundle["reviewed_through"],
                          bundle["cases"], bundle["rules"]), (0, 0, [], []))
        self.assertEqual(lessons.render_guidance(self.repo), "")
        self.assertFalse(self.state.exists())


class StatusAndReviewTests(LessonsTestCase):
    def test_threshold_ten_distinct_cases(self):
        for index in range(9):
            self.clean_case(index)
        status = lessons.status(self.repo)
        self.assertEqual(status["cases_since_review"], 9)
        self.assertFalse(status["review_due"])
        # Repeated events for one assignment never meet the case threshold.
        self.record("task-0000", "assignment-0000", "incorporated", evidence="second acceptance note")
        status = lessons.status(self.repo)
        self.assertEqual(status["cases_since_review"], 9)
        self.assertFalse(status["review_due"])
        self.clean_case(9)
        status = lessons.status(self.repo)
        self.assertEqual(status["cases_since_review"], 10)
        self.assertTrue(status["review_due"])
        self.assertTrue(status["due_reasons"][0].startswith("cases_since_review="))

    def test_threshold_three_rework_same_kind_and_cause(self):
        self.rework_case(1, "context_gap")
        self.rework_case(2, "context_gap")
        status = lessons.status(self.repo)
        self.assertFalse(status["review_due"])
        self.assertEqual(status["causes"], {"context_gap": 2})
        self.rework_case(3, "context_gap")
        status = lessons.status(self.repo)
        self.assertTrue(status["review_due"])
        self.assertTrue(any("cause=context_gap" in reason and "kind=implementation" in reason
                            for reason in status["due_reasons"]), status["due_reasons"])

    def test_rework_groups_require_same_kind_and_cause(self):
        other = self.project("mixed")
        self.rework_case(1, "context_gap", kind="implementation", root=other)
        self.rework_case(2, "worker_error", kind="implementation", root=other)
        self.rework_case(3, "context_gap", kind="test", root=other)
        status = lessons.status(other)
        self.assertEqual(status["counts"]["rework"], 3)
        self.assertFalse(status["review_due"])

    def test_unknown_cause_and_missing_attribution_never_group(self):
        for index in range(1, 4):
            self.rework_case(index, "unknown")
        for index in range(4, 7):
            self.record(self.case_id(index).split("/")[0], f"assignment-{index:04d}",
                        "needs-rework", evidence="missing structure",
                        context=self.ctx(kind="implementation", effort="high", version=0))
        status = lessons.status(self.repo)
        self.assertFalse(status["review_due"])
        self.assertEqual(status["counts"]["unknown_attribution"], 6)
        self.assertEqual(status["causes"], {"unknown": 6})
        # Explicit unknown causes still carry the coordinator-supplied severity.
        self.assertEqual(status["severities"], {"major": 3, "unknown": 3})

    def test_cohorts_group_by_kind_effort_and_version(self):
        self.clean_case(1, kind="implementation", effort="high")
        self.clean_case(2, kind="implementation", effort="high")
        self.clean_case(3, kind="implementation", effort="low")
        self.clean_case(4, kind="test", effort="medium")
        cohorts = lessons.status(self.repo)["cohorts"]
        self.assertEqual(
            cohorts,
            [{"kind": "implementation", "effort": "high", "version": 0,
              "cases": 2, "clean": 2, "rework": 0, "rejected": 0},
             {"kind": "implementation", "effort": "low", "version": 0,
              "cases": 1, "clean": 1, "rework": 0, "rejected": 0},
             {"kind": "test", "effort": "high", "version": 0,
              "cases": 1, "clean": 1, "rework": 0, "rejected": 0}],
        )

    def test_no_change_review_and_rule_removal_bump_version(self):
        self.clean_case(1)
        case = self.case_id(1)
        first = self.rule("rule-a", when={"kind": "implementation"}, evidence=[case])
        self.apply([first])
        self.assertEqual(lessons.snapshot(self.repo)["version"], 1)
        self.assertEqual(lessons.snapshot(self.repo)["rules"], [first])

        self.apply(lessons.snapshot(self.repo)["rules"])
        self.assertEqual(lessons.snapshot(self.repo)["version"], 2)
        self.assertEqual(lessons.snapshot(self.repo)["rules"], [first])
        self.assertEqual(lessons.review_bundle(self.repo)["reviewed_through"],
                         lessons.status(self.repo)["through_event"])

        self.apply([])
        self.assertEqual(lessons.snapshot(self.repo), {"version": 3, "rule_ids": [], "rules": []})
        markdown = Path(lessons.status(self.repo)["rules_file"]).read_text()
        self.assertIn("No active rules", markdown)

    def test_stale_expected_version_is_rejected(self):
        self.clean_case(1)
        case = self.case_id(1)
        self.apply([self.rule("rule-a", evidence=[case])])
        before = self.digests()
        with self.assertRaises(LessonError):
            self.apply([self.rule("rule-b", evidence=[case])], expected=0)
        self.assertEqual(lessons.snapshot(self.repo)["version"], 1)
        self.assertEqual(lessons.snapshot(self.repo)["rule_ids"], ["rule-a"])
        self.assertEqual(self.digests(), before)

    def test_through_event_must_be_a_forward_snapshot_sequence(self):
        for index in range(5):
            self.clean_case(index)
        self.apply([])
        self.assertEqual(lessons.status(self.repo)["through_event"], 5)
        self.clean_case(5)
        self.assertEqual(lessons.status(self.repo)["through_event"], 6)
        self.apply([])  # a no-change review may acknowledge the newer head
        self.assertEqual(lessons.review_bundle(self.repo)["reviewed_through"], 6)
        for through in (3, 99, "6", None, True):
            with self.subTest(through=through):
                with self.assertRaises(LessonError):
                    self.apply([], expected=2, through=through)
        self.assertEqual(lessons.snapshot(self.repo)["version"], 2)

    def test_concurrent_outcome_during_review_is_not_acknowledged(self):
        for index in range(10):
            self.clean_case(index)
        bundle = lessons.review_bundle(self.repo)
        self.assertEqual((bundle["expected_version"], bundle["reviewed_through"],
                          bundle["through_event"]), (0, 0, 10))
        self.assertEqual(len(bundle["cases"]), 10)
        # A newer outcome lands while the coordinator is reviewing the bundle.
        self.clean_case(10)
        rules = [self.rule("rule-a", evidence=[self.case_id(0), self.case_id(1)])]
        result = lessons.apply_review(self.repo, {
            "expected_version": bundle["expected_version"],
            "through_event": bundle["through_event"],
            "summary": "reviewed the first ten cases",
            "rules": rules,
        })
        self.assertEqual(result["version"], 1)
        self.assertEqual(result["through_event"], 11)
        self.assertEqual(result["cases_since_review"], 1)
        self.assertFalse(result["review_due"])
        # The concurrent case is not part of the acknowledged snapshot.
        with self.assertRaises(LessonError):
            lessons.apply_review(self.repo, {
                "expected_version": 1, "through_event": 10, "summary": "sneak in",
                "rules": [self.rule("rule-b", evidence=[self.case_id(10)])],
            })
        self.assertEqual(lessons.snapshot(self.repo)["rule_ids"], ["rule-a"])
        # An explicit later review can acknowledge it.
        result = lessons.apply_review(self.repo, {
            "expected_version": 1, "through_event": 11, "summary": "reviewed the concurrent case",
            "rules": [self.rule("rule-a", evidence=[self.case_id(0), self.case_id(10)])],
        })
        self.assertEqual((result["version"], result["cases_since_review"]), (2, 0))

    def test_apply_payload_is_exact_and_malformed_rules_do_not_write(self):
        self.clean_case(1)
        case = self.case_id(1)
        head = lessons.status(self.repo)["through_event"]
        self.apply([], expected=0, through=head)
        version = lessons.snapshot(self.repo)["version"]
        bad_payloads = [
            "not a payload",
            {"expected_version": version, "through_event": head, "summary": "s"},
            {"expected_version": version, "through_event": head, "summary": "s", "rules": [], "extra": 1},
            {"expected_version": True, "through_event": head, "summary": "s", "rules": []},
            {"expected_version": version, "through_event": head, "summary": "  ", "rules": []},
            {"expected_version": version, "through_event": head, "summary": "s", "rules": "rules"},
            {"expected_version": version, "through_event": head, "summary": "s",
             "rules": [{"id": "r", "when": {}, "condition": "c", "action": "a"}]},
            {"expected_version": version, "through_event": head, "summary": "s",
             "rules": [{"id": "r", "when": {}, "condition": "c", "action": "a",
                        "evidence": [case], "extra": 1}]},
            {"expected_version": version, "through_event": head, "summary": "s",
             "rules": [{"id": "r", "when": {"unknown": "x"}, "condition": "c", "action": "a",
                        "evidence": [case]}]},
            {"expected_version": version, "through_event": head, "summary": "s",
             "rules": [{"id": "r", "when": {"kind": "sparkling"}, "condition": "c", "action": "a",
                        "evidence": [case]}]},
            {"expected_version": version, "through_event": head, "summary": "s",
             "rules": [{"id": "r", "when": {"effort": "auto"}, "condition": "c", "action": "a",
                        "evidence": [case]}]},
            {"expected_version": version, "through_event": head, "summary": "s",
             "rules": [{"id": "r", "when": {}, "condition": " ", "action": "a",
                        "evidence": [case]}]},
            {"expected_version": version, "through_event": head, "summary": "s",
             "rules": [{"id": "r", "when": {}, "condition": "c", "action": "a",
                        "evidence": []}]},
            {"expected_version": version, "through_event": head, "summary": "s",
             "rules": [{"id": "r", "when": {}, "condition": "c", "action": "a",
                        "evidence": ["task-9999/assignment-9999"]}]},
            {"expected_version": version, "through_event": head, "summary": "s",
             "rules": [{"id": "r", "when": {}, "condition": "c", "action": "a",
                        "evidence": [7]}]},
            {"expected_version": version, "through_event": head, "summary": "s",
             "rules": [self.rule("same", evidence=[case]), self.rule("same", evidence=[case])]},
            {"expected_version": version, "through_event": head, "summary": "s",
             "rules": [self.rule(f"rule-{index}", evidence=[case]) for index in range(21)]},
        ]
        before = self.digests()
        for payload in bad_payloads:
            with self.subTest(payload=payload):
                with self.assertRaises(LessonError):
                    lessons.apply_review(self.repo, payload)
        self.assertEqual(lessons.snapshot(self.repo)["version"], version)
        self.assertEqual(self.digests(), before)

    def test_review_bundle_consolidates_events_with_context(self):
        self.rework_case(1, "brief_gap")
        self.record("task-0001", "assignment-0001", "incorporated",
                    evidence="rework incorporated",
                    context=self.ctx(kind="implementation", effort="high", version=0))
        bundle = lessons.review_bundle(self.repo)
        self.assertEqual(len(bundle["cases"]), 1)
        case = bundle["cases"][0]
        self.assertEqual(case["case_id"], self.case_id(1))
        self.assertEqual(case["kind"], "implementation")
        self.assertEqual(case["effort"], "high")
        self.assertEqual(case["version"], 0)
        self.assertEqual([event["disposition"] for event in case["events"]],
                         ["needs-rework", "incorporated"])
        self.assertEqual(case["events"][0]["rework"]["cause"], "brief_gap")
        self.assertEqual(case["events"][0]["context"]["features"]["kind"], "implementation")

    def test_markdown_is_generated_from_canonical_rules_and_recoverable(self):
        self.clean_case(1)
        case = self.case_id(1)
        self.assertFalse(Path(lessons.status(self.repo)["rules_file"]).exists())
        rule = self.rule("rule-a", when={"kind": "implementation", "effort": "high"},
                         condition="Keep checks executable", action="Declare a reproducer",
                         evidence=[case])
        self.apply([rule])
        markdown = Path(lessons.status(self.repo)["rules_file"])
        self.assertEqual(stat.S_IMODE(markdown.stat().st_mode), 0o600)
        text = markdown.read_text()
        for expected in ("Version: 1", "rule-a", "Keep checks executable",
                         "Declare a reproducer", case):
            self.assertIn(expected, text)
        markdown.unlink()
        self.assertEqual(lessons.snapshot(self.repo)["rule_ids"], ["rule-a"])
        self.apply(lessons.snapshot(self.repo)["rules"])
        self.assertIn("Version: 2", markdown.read_text())

    def test_review_history_is_immutable(self):
        self.clean_case(1)
        case = self.case_id(1)
        self.apply([self.rule("rule-a", condition="first", evidence=[case])])
        self.apply([self.rule("rule-a", condition="second", evidence=[case])])
        database = self.lesson_dir() / "journal.sqlite3"
        db = sqlite3.connect(database)
        try:
            reviews = db.execute("SELECT version, through_event FROM reviews ORDER BY version").fetchall()
            self.assertEqual(reviews, [(1, 1), (2, 1)])
            stored = db.execute("SELECT value FROM rules WHERE version=1 ORDER BY ordinal").fetchall()
            self.assertEqual(len(stored), 1)
            self.assertIn("first", stored[0][0])
            self.assertIn("second", db.execute(
                "SELECT value FROM rules WHERE version=2 ORDER BY ordinal").fetchone()[0])
        finally:
            db.close()


class SnapshotTests(LessonsTestCase):
    def test_feature_matching_is_exact_and_normalizes_effort(self):
        self.clean_case(1, kind="implementation", effort="high")
        case = self.case_id(1)
        rules = [
            self.rule("any", evidence=[case]),
            self.rule("impl", when={"kind": "implementation"}, evidence=[case]),
            self.rule("docs", when={"kind": "documentation"}, evidence=[case]),
            self.rule("high", when={"effort": "high"}, evidence=[case]),
        ]
        self.apply(rules)
        self.assertEqual(lessons.snapshot(self.repo)["rule_ids"], ["any", "impl", "docs", "high"])
        self.assertEqual(lessons.snapshot(self.repo, {"kind": "implementation", "effort": "high"})["rule_ids"],
                         ["any", "impl", "high"])
        # The legacy medium spelling compares equal to high.
        self.assertEqual(lessons.snapshot(self.repo, {"kind": "implementation", "effort": "medium"})["rule_ids"],
                         ["any", "impl", "high"])
        # A missing or unknown feature cannot satisfy a constrained rule.
        self.assertEqual(lessons.snapshot(self.repo, {"kind": "documentation"})["rule_ids"],
                         ["any", "docs"])
        self.assertEqual(lessons.snapshot(self.repo, {})["rule_ids"], ["any"])
        self.assertEqual(lessons.snapshot(self.repo, {"kind": "unknown", "effort": "unknown"})["rule_ids"], ["any"])
        with self.assertRaises(LessonError):
            lessons.snapshot(self.repo, ["kind"])

    def test_rule_effort_medium_is_stored_as_high(self):
        self.clean_case(1)
        case = self.case_id(1)
        self.apply([self.rule("legacy", when={"effort": "medium"}, evidence=[case])])
        stored = lessons.snapshot(self.repo)
        self.assertEqual(stored["rules"][0]["when"], {"effort": "high"})
        self.assertEqual(lessons.snapshot(self.repo, {"effort": "medium"})["rule_ids"], ["legacy"])
        self.assertEqual(lessons.snapshot(self.repo, {"effort": "high"})["rule_ids"], ["legacy"])
        self.assertEqual(lessons.snapshot(self.repo, {"effort": "low"})["rule_ids"], [])

    def test_snapshot_captures_or_survives_later_reviews(self):
        self.clean_case(1)
        case = self.case_id(1)
        self.apply([self.rule("rule-a", evidence=[case])])
        captured = lessons.snapshot(self.repo)
        self.apply([self.rule("rule-b", evidence=[case])])
        self.assertEqual(captured["rule_ids"], ["rule-a"])
        self.assertEqual(lessons.snapshot(self.repo)["rule_ids"], ["rule-b"])

    def test_legacy_context_stays_unknown(self):
        self.record("task-0001", "assignment-0001", context={})
        self.record("task-0002", "assignment-0002", "needs-rework", context={},
                    evidence="legacy correction")
        status = lessons.status(self.repo)
        self.assertEqual(
            status["cohorts"],
            [{"kind": "unknown", "effort": "unknown", "version": "unknown",
              "cases": 2, "clean": 1, "rework": 1, "rejected": 0}])
        self.assertEqual(status["counts"]["unknown_attribution"], 1)
        self.assertEqual(status["causes"], {"unknown": 1})
        self.assertEqual(status["severities"], {"unknown": 1})


class GuidanceTests(LessonsTestCase):
    def test_guidance_is_read_only_and_lists_due_review_commands(self):
        self.clean_case(1)
        case = self.case_id(1)
        rule = self.rule("rule-a", when={"kind": "implementation"}, condition="Keep it bounded",
                         action="Declare checks", evidence=[case])
        self.apply([rule])
        for index in range(2, 12):
            self.clean_case(index)
        status = lessons.status(self.repo)
        self.assertTrue(status["review_due"])
        before = self.digests()
        guidance = lessons.render_guidance(self.repo)
        self.assertIn("rule-a", guidance)
        self.assertIn("Keep it bounded", guidance)
        self.assertIn("Declare checks", guidance)
        self.assertIn(case, guidance)
        self.assertIn("deepseek-team lessons review", guidance)
        self.assertIn("cases_since_review=", guidance)
        self.assertEqual(self.digests(), before)
        # A feature filter excludes non-matching rules but keeps the due reminder.
        guidance = lessons.render_guidance(self.repo, {"kind": "documentation"})
        self.assertNotIn("rule-a", guidance)
        self.assertIn("deepseek-team lessons review", guidance)

    def test_guidance_empty_when_nothing_applies(self):
        self.clean_case(1)
        self.assertEqual(lessons.render_guidance(self.repo), "")
        self.assertEqual(lessons.render_guidance(self.repo, {"kind": "implementation"}), "")


class PrivateStateTests(LessonsTestCase):
    def test_private_modes_and_project_isolation(self):
        self.clean_case(1)
        directory = self.lesson_dir()
        self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
        self.assertEqual(directory.stat().st_uid, os.geteuid())
        self.assertEqual(stat.S_IMODE((directory / "journal.sqlite3").stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((directory / "lessons.lock").stat().st_mode), 0o600)
        other = self.project("other-project")
        self.assertEqual(lessons.journal(other), [])
        self.clean_case(2, root=other)
        self.assertEqual(lessons.status(self.repo)["counts"]["cases"], 1)
        self.assertEqual(lessons.status(other)["counts"]["cases"], 1)
        self.assertNotEqual(self.key(), self.key(other))

    def test_default_state_lives_under_home(self):
        home = self.base / "home-default"
        home.mkdir()
        with mock.patch.dict(os.environ, {"HOME": str(home)}, clear=True):
            self.record("task-0001", "assignment-0001")
            directory = home / ".local/state/codex-deepseek/lessons" / self.key()
            database = directory / "journal.sqlite3"
            self.assertTrue(database.is_file())
            self.assertEqual(stat.S_IMODE(database.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
        with mock.patch.dict(os.environ, {"DEEPSEEK_TEAM_STATE_DIR": "relative/path"}):
            with self.assertRaises(LessonError):
                self.record("task-0002", "assignment-0002")

    def test_symlinked_state_files_are_rejected(self):
        target = self.base / "target.txt"
        target.write_text("{}")
        directory = self.lesson_dir()
        directory.mkdir(parents=True, mode=0o700)
        os.symlink(target, directory / "journal.sqlite3")
        with self.assertRaises(LessonError):
            lessons.journal(self.repo)
        with self.assertRaises(LessonError):
            self.record("task-0001", "assignment-0001")
        (directory / "journal.sqlite3").unlink()

        self.record("task-0001", "assignment-0001")
        lock = directory / "lessons.lock"
        lock.unlink()
        os.symlink(target, lock)
        with self.assertRaises(LessonError):
            self.record("task-0002", "assignment-0002")

    def test_symlinked_state_directory_is_rejected(self):
        outside = self.base / "outside"
        outside.mkdir()
        self.state.mkdir()
        (self.state / "lessons").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(LessonError):
            self.record("task-0001", "assignment-0001")
        with self.assertRaises(LessonError):
            lessons.journal(self.repo)

    def test_foreign_project_database_is_rejected(self):
        self.clean_case(1)
        database = self.lesson_dir() / "journal.sqlite3"
        other = self.project("foreign")
        self.clean_case(2, root=other)
        database.write_bytes((self.lesson_dir(other) / "journal.sqlite3").read_bytes())
        with self.assertRaises(LessonError):
            lessons.journal(self.repo)

    def test_markdown_is_rejected_when_not_private(self):
        self.clean_case(1)
        case = self.case_id(1)
        self.apply([self.rule("rule-a", evidence=[case])])
        markdown = self.lesson_dir() / "DELEGATION_LESSONS.md"
        os.chmod(markdown, 0o644)
        with self.assertRaises(LessonError):
            self.apply([self.rule("rule-b", evidence=[case])])
        self.assertEqual(lessons.snapshot(self.repo)["version"], 1)

    def test_symlinked_markdown_is_rejected_atomically(self):
        self.clean_case(1)
        case = self.case_id(1)
        self.apply([self.rule("rule-a", evidence=[case])])
        markdown = self.lesson_dir() / "DELEGATION_LESSONS.md"
        markdown.unlink()
        target = self.base / "target.md"
        target.write_text("do not replace me")
        os.symlink(target, markdown)
        with self.assertRaises(LessonError):
            self.apply([self.rule("rule-b", evidence=[case])])
        self.assertEqual(lessons.snapshot(self.repo)["version"], 1)
        self.assertEqual(lessons.snapshot(self.repo)["rule_ids"], ["rule-a"])
        db = sqlite3.connect(self.lesson_dir() / "journal.sqlite3")
        try:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM reviews").fetchone()[0], 1)
        finally:
            db.close()

    def test_read_queries_leave_existing_state_untouched(self):
        self.clean_case(1)
        case = self.case_id(1)
        self.apply([self.rule("rule-a", evidence=[case])])
        before = self.digests()
        lessons.journal(self.repo)
        lessons.snapshot(self.repo)
        lessons.status(self.repo)
        lessons.review_bundle(self.repo)
        lessons.render_guidance(self.repo)
        self.assertEqual(self.digests(), before)


if __name__ == "__main__":
    unittest.main()
