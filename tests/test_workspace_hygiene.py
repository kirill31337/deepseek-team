"""Repository hygiene: worker copies are independent repos and creation is read-only on source.

These tests use temporary real Git repositories. They assert observable behaviour (refs, HEAD,
index, worktree registrations and dirty tracked/untracked files) rather than matching prose, so a
change to workspace.create that leaked branches, worktrees or index state would fail them.
"""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from codex_deepseek_team import project, settings, workspace


def _git(root, *args):
    result = subprocess.run(["git", "-C", str(root), *args],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
    return result.stdout.decode("utf-8")


def _commit(root, *args):
    subprocess.run(["git", "-C", str(root), "-c", "user.name=Test",
                    "-c", "user.email=test@example.test", *args],
                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)


def _worktree_registry(root):
    """Sorted relative names under .git/worktrees, or an empty list when absent."""
    directory = root / ".git" / "worktrees"
    if not directory.is_dir():
        return []
    return sorted(str(path.relative_to(directory)) for path in directory.rglob("*"))


class HygieneFixture(unittest.TestCase):
    """A committed source repository with dirty tracked/untracked and staged files.

    Holds only setup and helpers, so subclasses add behaviour without rerunning each other.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="dst-hygiene-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.source)], check=True)
        (self.source / "calc.py").write_text("def add(a, b):\n    return a - b\n")
        (self.source / "notes.txt").write_text("committed notes\n")
        subprocess.run(["git", "-C", str(self.source), "add", "."],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
        _commit(self.source, "commit", "-qm", "base")
        # Dirty tracked file, untracked file and a staged (uncommitted) addition: the exact index
        # and worktree state workspace.create must never disturb.
        (self.source / "calc.py").write_text("def add(a, b):\n    return a + b\n")
        (self.source / "untracked.txt").write_text("local scratch\n")
        (self.source / "staged.txt").write_text("staged change\n")
        subprocess.run(["git", "-C", str(self.source), "add", "staged.txt"],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)

        self.state = self.root / "state"
        self.env = mock.patch.dict(os.environ, {
            "DEEPSEEK_TEAM_STATE_DIR": str(self.state),
            "XDG_CONFIG_HOME": str(self.root / "config"),
            "HOME": str(self.root / "home"),
        })
        (self.root / "home").mkdir()
        self.env.start()
        self.addCleanup(self.env.stop)

    def source_state(self):
        return {
            "head": _git(self.source, "rev-parse", "HEAD").strip(),
            "branch": _git(self.source, "symbolic-ref", "HEAD").strip(),
            "refs": _git(self.source, "for-each-ref",
                         "--format=%(refname) %(objectname) %(objecttype)"),
            "index": _git(self.source, "ls-files", "-s"),
            "worktrees": _git(self.source, "worktree", "list", "--porcelain"),
            "registry": _worktree_registry(self.source),
            "status": _git(self.source, "status", "--porcelain=v1", "--untracked-files=all"),
        }


class WorkspaceHygieneTests(HygieneFixture):
    def test_repeated_creation_and_finish_leave_source_untouched(self):
        before = self.source_state()
        self.assertNotIn("deepseek/", before["refs"])
        copies = []
        for _ in range(3):
            copies.append(workspace.create(self.source, self.state))
            self.assertEqual(self.source_state(), before)
        for copy in copies:
            with copy.lock():
                copy.begin("verification")
                copy.finish("succeeded", exit_code=0)
            self.assertEqual(self.source_state(), before)
        # The dirty tracked/untracked and staged files are still present and untouched.
        self.assertEqual((self.source / "untracked.txt").read_text(), "local scratch\n")
        self.assertEqual((self.source / "calc.py").read_text(),
                         "def add(a, b):\n    return a + b\n")
        self.assertIn("staged.txt", before["index"])

    def test_each_copy_has_independent_git_metadata_and_private_ref(self):
        copies = [workspace.create(self.source, self.state) for _ in range(2)]
        source_head = _git(self.source, "rev-parse", "HEAD").strip()
        git_dirs, branches = set(), set()
        for copy in copies:
            self.assertFalse((copy.path / ".git").is_symlink())
            self.assertTrue((copy.path / ".git").is_dir())
            git_dir = Path(_git(copy.path, "rev-parse", "--absolute-git-dir").strip())
            self.assertTrue(git_dir.is_dir())
            self.assertTrue(git_dir.is_relative_to(self.state.resolve()))
            self.assertFalse(git_dir.is_relative_to(self.source.resolve()))
            git_dirs.add(git_dir)
            branch = _git(copy.path, "symbolic-ref", "HEAD").strip()
            self.assertEqual(branch, "refs/heads/deepseek/" + copy.id)
            branches.add(branch)
            # The copy is a working tree of its own repository at the source HEAD.
            self.assertEqual(_git(copy.path, "rev-parse", "HEAD").strip(), source_head)
            self.assertEqual(_git(copy.path, "rev-parse", "--is-inside-work-tree").strip(), "true")
            # The internal branch lives only inside this private copy.
            self.assertIn(branch, _git(copy.path, "for-each-ref", "--format=%(refname)"))
        self.assertEqual(len(git_dirs), 2)
        self.assertEqual(len(branches), 2)
        self.assertNotIn("deepseek/", _git(self.source, "for-each-ref", "--format=%(refname)"))
        self.assertEqual(self.source_state()["head"], source_head)

    def test_copy_commits_never_leak_into_source_refs_or_history(self):
        before = self.source_state()
        copy = workspace.create(self.source, self.state)
        (copy.path / "calc.py").write_text("def add(a, b):\n    return a + b\n")
        _commit(copy.path, "commit", "-qam", "worker fix")
        self.assertNotEqual(_git(copy.path, "rev-parse", "HEAD").strip(), before["head"])
        self.assertEqual(self.source_state(), before)

    def test_creation_preserves_a_foreign_source_worktree(self):
        foreign = self.root / "user-topic"
        subprocess.run(["git", "-C", str(self.source), "worktree", "add", "-q",
                        "-b", "user-topic", str(foreign), "HEAD"],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
        (foreign / "user-note.txt").write_text("user work in progress\n")
        before = self.source_state()
        self.assertIn("user-topic", before["worktrees"])
        for _ in range(2):
            workspace.create(self.source, self.state)
            self.assertEqual(self.source_state(), before)
        self.assertEqual((foreign / "user-note.txt").read_text(), "user work in progress\n")
        self.assertFalse((self.source / ".git" / "worktrees").is_symlink())
        # The coordinator must not have created a task branch in the source.
        self.assertNotIn("deepseek/", _git(self.source, "for-each-ref", "--format=%(refname)"))


class WorkspaceHygieneGuidanceTests(HygieneFixture):
    """The generated and packaged guidance must actually carry the hygiene rules."""

    REQUIRED = ("Workspace and branch hygiene", "workspace.create", "detached worktree",
                "synthetic branch", "bulk-prune", "automatic cleanup engine",
                "outside the project")

    def test_generated_and_packaged_guidance_describe_the_rules(self):
        policy = settings.resolve(delegation_level="auto", access="full-access")
        generated = settings.instructions(policy, "codex")
        packaged = project.DATA_FILE.read_text(encoding="utf-8")
        for text, label in ((generated, "generated"), (packaged, "packaged")):
            for phrase in self.REQUIRED:
                self.assertIn(phrase, text, f"{label} guidance is missing {phrase!r}")
        composed = project._guidance("codex", self.source)
        self.assertIsInstance(composed, bytes)
        self.assertIn(b"Workspace and branch hygiene", composed)
        self.assertIn(b"workspace.create", composed)


if __name__ == "__main__":
    unittest.main()
