"""Linked opt-in bootstrap inside an already-managed instruction block.

A standalone marker inside the owned span opts that runtime into a compact
generated bootstrap that references the fixed human-maintained guide
``docs/agents/delegation.md`` instead of embedding a full profile snapshot.
"""
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import unittest
from unittest import mock

from codex_deepseek_team import project, settings


LINKED = b"<!-- codex-deepseek-team:guidance:linked -->"
PROFILE_HEADER = b"### Effective delegation profile"
GUIDE_RELATIVE = Path("docs") / "agents" / "delegation.md"
MAX_BLOCK = 4096


class LinkedGuidanceCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dsteam-linked-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.root = self.tmp
        self.repo = self.root / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", os.fspath(self.repo)],
                       check=True, stderr=subprocess.DEVNULL)
        # Isolate every config/state lookup from the host so attach/resolve never
        # reads or writes user configuration.
        self.home = self.root / "home"
        self.home.mkdir()
        environment = mock.patch.dict(os.environ, {
            "HOME": str(self.home),
            "CODEX_HOME": str(self.root / "codex"),
            "CLAUDE_CONFIG_DIR": str(self.root / "claude"),
            "XDG_CONFIG_HOME": str(self.root / "config"),
            "DEEPSEEK_TEAM_STATE_DIR": str(self.root / "state"),
        }, clear=False)
        environment.start()
        self.addCleanup(environment.stop)
        self.addCleanup(os.environ.pop, "DEEPSEEK_TEAM_DISABLED", None)
        os.environ.pop("DEEPSEEK_TEAM_DISABLED", None)

    # -- helpers ---------------------------------------------------------
    def target(self, runtime="codex"):
        return self.repo / project.TARGETS[runtime]

    def write_guide(self, data=b"# Human guide\n\nDetailed delegation rules.\n"):
        guide = self.repo / GUIDE_RELATIVE
        guide.parent.mkdir(parents=True, exist_ok=True)
        guide.write_bytes(data)
        return guide

    def attach_full(self, runtime="codex"):
        return project.attach(self.repo, coordinator=runtime)

    def inject_marker(self, runtime="codex"):
        """Place the standalone marker inside the already-managed span."""
        path = self.target(runtime)
        content = path.read_bytes()
        after_start = content.index(project.START_MARKER) + len(project.START_MARKER)
        meta_end = content.index(b"\n", after_start + 1) + 1
        path.write_bytes(content[:meta_end] + LINKED + b"\n" + content[meta_end:])

    def block_bytes(self, runtime="codex"):
        data = self.target(runtime).read_bytes()
        begin = data.index(project.START_MARKER)
        end = data.index(project.END_MARKER) + len(project.END_MARKER)
        return data[begin:end]

    # -- unmarked / new behavior stays exactly the same ------------------
    def test_new_attachment_has_no_marker_and_full_guidance(self):
        self.assertTrue(self.attach_full())
        block = self.block_bytes()
        self.assertNotIn(LINKED, block)
        self.assertIn(PROFILE_HEADER, block)
        self.assertGreater(len(block), MAX_BLOCK)

    def test_unmarked_refresh_keeps_full_guidance(self):
        self.attach_full()
        policy = settings.resolve(self.repo, effort="max")
        self.assertTrue(project.attach(self.repo, policy=policy))
        block = self.block_bytes()
        self.assertNotIn(LINKED, block)
        self.assertIn(b"effort=max", block)

    # -- marker inside the managed span activates the compact mode ------
    def test_marker_inside_managed_block_activates_short_bootstrap(self):
        self.write_guide()
        self.attach_full()
        self.inject_marker()
        self.assertTrue(project.attach(self.repo))
        block = self.block_bytes()
        self.assertIn(LINKED, block)
        self.assertIn(b"docs/agents/delegation.md", block)
        self.assertIn(b"deepseek-team status", block)
        self.assertIn(b"--runtime codex", block)
        self.assertIn(b"off", block)
        self.assertIn(b"sandbox", block)
        self.assertLessEqual(len(block), MAX_BLOCK)

    def test_linked_mode_persists_across_refresh_and_is_idempotent(self):
        self.write_guide()
        self.attach_full()
        self.inject_marker()
        project.attach(self.repo)
        first = self.target().read_bytes()
        # A second refresh must re-emit the marker and change nothing.
        self.assertFalse(project.attach(self.repo))
        self.assertEqual(self.target().read_bytes(), first)
        self.assertEqual(project.attach(self.repo), False)

    def test_linked_refresh_with_changed_policy_stays_short_and_preserves_bytes(self):
        self.write_guide()
        prefix = b"# Project rules\r\nkeep me\r\n"
        suffix = b"\ntrailing user note\n"
        self.target().write_bytes(prefix)
        self.attach_full()
        self.inject_marker()
        project.attach(self.repo)
        self.target().write_bytes(self.target().read_bytes() + suffix)
        before = self.target().read_bytes()

        policy = settings.resolve(self.repo, effort="max")
        prepared = project.prepare_refresh(self.repo, policy, coordinator="codex")
        self.assertEqual(len(prepared), 1)
        _, _, _, updated = prepared[0]
        # A changed policy must not expand the compact block or inject a snapshot.
        self.assertEqual(updated, before)

        self.assertFalse(project.attach(self.repo, policy=policy))
        data = self.target().read_bytes()
        self.assertTrue(data.startswith(prefix))
        self.assertTrue(data.endswith(suffix))
        block = self.block_bytes()
        self.assertLessEqual(len(block), MAX_BLOCK)
        self.assertIn(LINKED, block)
        self.assertNotIn(PROFILE_HEADER, block)
        self.assertNotIn(b"effort=max", block)

    def test_linked_mode_persists_through_refresh_attached(self):
        self.write_guide()
        self.attach_full()
        self.inject_marker()
        project.attach(self.repo)
        short = self.block_bytes()

        policy = settings.resolve(self.repo)
        changed, problem = project.refresh_attached(self.repo, policy, coordinator="codex")
        self.assertEqual(problem, "")
        self.assertFalse(changed)
        self.assertEqual(self.block_bytes(), short)

    def test_runtime_specific_commands_per_runtime(self):
        self.write_guide()
        for runtime in ("codex", "claude"):
            with self.subTest(runtime=runtime):
                self.attach_full(runtime)
                self.inject_marker(runtime)
                project.attach(self.repo, coordinator=runtime)
                block = self.block_bytes(runtime)
                other = "claude" if runtime == "codex" else "codex"
                self.assertIn(b"--runtime " + runtime.encode(), block)
                self.assertNotIn(b"--runtime " + other.encode(), block)

    # -- marker scope ----------------------------------------------------
    def test_marker_outside_managed_block_is_ignored(self):
        self.target().write_bytes(LINKED + b"\n\n# user text\n")
        self.attach_full()
        block = self.block_bytes()
        self.assertNotIn(LINKED, block)
        self.assertIn(PROFILE_HEADER, block)
        # A refresh keeps the full block and leaves the outside text alone.
        project.attach(self.repo)
        self.assertNotIn(LINKED, self.block_bytes())
        self.assertIn(PROFILE_HEADER, self.block_bytes())

    def test_marker_inside_a_code_example_outside_the_block_is_ignored(self):
        example = b"Example:\n\n```\n" + LINKED + b"\n```\n"
        self.target().write_bytes(example)
        self.attach_full()
        self.assertNotIn(LINKED, self.block_bytes())
        self.assertIn(PROFILE_HEADER, self.block_bytes())
        project.attach(self.repo)
        self.assertIn(example, self.target().read_bytes())

    # -- detach ----------------------------------------------------------
    def test_detach_removes_linked_block_and_restores_original(self):
        self.write_guide()
        original = b"# Rules\nno trailing newline"
        self.target().write_bytes(original)
        self.attach_full()
        self.inject_marker()
        project.attach(self.repo)
        self.assertIn(LINKED, self.block_bytes())
        self.assertTrue(project.detach(self.repo))
        self.assertEqual(self.target().read_bytes(), original)

    # -- validation ------------------------------------------------------
    def _linked_repo(self):
        self.attach_full()
        self.inject_marker()
        # First activation requires a valid guide.
        self.write_guide()
        project.attach(self.repo)
        return self.target().read_bytes()

    def test_missing_guide_raises_and_leaves_files_unchanged(self):
        before = self._linked_repo()
        (self.repo / GUIDE_RELATIVE).unlink()
        with self.assertRaises(project.ProjectError):
            project.attach(self.repo)
        self.assertEqual(self.target().read_bytes(), before)

    def test_empty_guide_is_refused(self):
        before = self._linked_repo()
        (self.repo / GUIDE_RELATIVE).write_bytes(b"")
        with self.assertRaises(project.ProjectError):
            project.attach(self.repo)
        self.assertEqual(self.target().read_bytes(), before)

    def test_non_utf8_guide_is_refused(self):
        before = self._linked_repo()
        (self.repo / GUIDE_RELATIVE).write_bytes(b"\xff\xfe\x00invalid")
        with self.assertRaises(project.ProjectError):
            project.attach(self.repo)
        self.assertEqual(self.target().read_bytes(), before)

    def test_symlinked_guide_file_is_refused(self):
        before = self._linked_repo()
        outside = self.root / "outside.md"
        outside.write_bytes(b"# outside\n")
        guide = self.repo / GUIDE_RELATIVE
        guide.unlink()
        os.symlink(outside, guide)
        with self.assertRaises(project.ProjectError):
            project.attach(self.repo)
        self.assertEqual(self.target().read_bytes(), before)

    def test_symlinked_directory_component_is_refused(self):
        before = self._linked_repo()
        outside = self.root / "outside-docs"
        (outside / "agents").mkdir(parents=True)
        (outside / "agents" / "delegation.md").write_bytes(b"# outside guide\n")
        guide_dir = self.repo / "docs"
        shutil.rmtree(guide_dir)
        os.symlink(outside, guide_dir)
        with self.assertRaises(project.ProjectError):
            project.attach(self.repo)
        self.assertEqual(self.target().read_bytes(), before)

    def test_both_runtime_refresh_is_all_or_nothing(self):
        self.write_guide()
        project.attach(self.repo, coordinator="both")
        # Codex stays in full mode but its block is stale-mutated so a refresh
        # would rewrite it. Claude is opted into linked mode.
        codex = self.target("codex")
        codex.write_bytes(codex.read_bytes().replace(b"DeepSeek delegation",
                                                     b"STALE codex text"))
        self.inject_marker("claude")
        project.attach(self.repo, coordinator="claude")
        self.assertIn(LINKED, self.block_bytes("claude"))

        codex_mutated = codex.read_bytes()
        claude_before = self.target("claude").read_bytes()
        (self.repo / GUIDE_RELATIVE).unlink()

        policy = settings.resolve(self.repo)
        with self.assertRaises(project.ProjectError):
            project.prepare_refresh(self.repo, policy, coordinator="both")
        self.assertEqual(codex.read_bytes(), codex_mutated)
        self.assertEqual(self.target("claude").read_bytes(), claude_before)

        changed, problem = project.refresh_attached(self.repo, policy, coordinator="both")
        self.assertEqual(changed, 0)
        self.assertIn("delegation.md", problem)
        self.assertEqual(codex.read_bytes(), codex_mutated)
        self.assertEqual(self.target("claude").read_bytes(), claude_before)

    def test_pure_render_does_not_mutate_or_touch_guide(self):
        self.write_guide()
        before = self._linked_repo()
        guide = self.repo / GUIDE_RELATIVE
        guide_bytes = guide.read_bytes()
        guide_mode = stat.S_IMODE(guide.stat().st_mode)
        policy = settings.resolve(self.repo)
        prepared = project.prepare_refresh(self.repo, policy, coordinator="codex")
        self.assertEqual(self.target().read_bytes(), before)
        self.assertEqual(guide.read_bytes(), guide_bytes)
        self.assertEqual(stat.S_IMODE(guide.stat().st_mode), guide_mode)
        self.assertTrue(prepared)

    def test_short_bootstrap_keeps_required_guardrails(self):
        self.write_guide()
        self.attach_full()
        self.inject_marker()
        project.attach(self.repo)
        body = self.block_bytes()
        for phrase in (b"docs/agents/delegation.md", b"deepseek-team status",
                       b"config show --effective --instructions", b"off",
                       b"never widened", b"integration", b"final",
                       b"sandbox", b"native", b"plan"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, body)


if __name__ == "__main__":
    unittest.main()
