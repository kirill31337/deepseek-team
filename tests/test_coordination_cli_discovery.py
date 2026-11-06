"""CLI discovery and task-id guidance for persistent coordination state."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from codex_deepseek_team import coordination, settings


class CoordinationCliDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="dst-coord-cli-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        (self.repo / "a.py").write_text("VALUE = 1\n")
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        subprocess.run(["git", "-C", str(self.repo), "add", "."], check=True)
        subprocess.run(
            ["git", "-C", str(self.repo), "-c", "user.name=Test", "-c",
             "user.email=test@example.test", "commit", "-qm", "base"],
            check=True,
        )
        self.state = self.root / "state"
        self.home = self.root / "home"
        self.home.mkdir()
        self.env = dict(os.environ)
        for name in ("DEEPSEEK_API_KEY", "DEEPSEEK_TEAM_DISABLED",
                     "DEEPSEEK_TEAM_PROJECT_ROOT"):
            self.env.pop(name, None)
        self.env.update({
            "HOME": str(self.home),
            "XDG_CONFIG_HOME": str(self.root / "config"),
            "DEEPSEEK_TEAM_STATE_DIR": str(self.state),
            "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
        })

    def cli(self, *args, input_text=""):
        return subprocess.run(
            [sys.executable, "-m", "codex_deepseek_team", *args],
            input=input_text, text=True, capture_output=True, env=self.env,
            cwd=self.repo, timeout=30,
        )

    def create_task(self):
        with mock.patch.dict(os.environ, self.env, clear=True):
            return coordination.open_task(
                self.repo, session_id="session-1", turn_id="turn-1",
                prompt="implement feature",
                policy=settings.Policy(75, "full-access", {
                    "delegation_level": "test",
                    "access": "test",
                }),
            )

    def test_root_help_lists_coordination_with_description(self):
        result = self.cli("--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("coordination task state", result.stdout)
        self.assertIn("coordination", result.stdout.split("positional arguments:", 1)[1])

    def test_coordination_help_still_lists_existing_subcommands(self):
        result = self.cli("coordination", "--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        for command in ("plan", "status", "use", "result", "abandon", "finalize", "complete"):
            self.assertIn(command, result.stdout)

    def test_malformed_task_ids_explain_format_provenance_and_discovery(self):
        malformed = (
            "not-a-task",
            "task-123",
            "task-" + "a" * 21,
            "task-" + "A" * 20,
            "task-" + "g" * 20,
        )
        for task_id in malformed:
            with self.subTest(task_id=task_id):
                result = self.cli("coordination", "status", "--task", task_id)
                self.assertEqual(result.returncode, 64, result.stderr)
                self.assertEqual(result.stdout, "")
                self.assertIn("Invalid coordination task id", result.stderr)
                self.assertIn("task-", result.stderr)
                self.assertIn("exactly 20", result.stderr)
                self.assertIn("hexadecimal", result.stderr)
                self.assertIn("session/turn hooks", result.stderr)
                self.assertIn("deepseek-team coordination status", result.stderr)
                self.assertIn("--session", result.stderr)

    def test_valid_task_ids_keep_stable_status_dispatch(self):
        task = self.create_task()
        result = self.cli("coordination", "status", "--path", str(self.repo),
                          "--task", task["id"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(task["id"], result.stdout)
        self.assertIn("status=planning", result.stdout)
        by_session = self.cli("coordination", "status", "--path", str(self.repo),
                              "--session", "session-1")
        self.assertEqual(by_session.returncode, 0, by_session.stderr)
        self.assertIn(task["id"], by_session.stdout)


if __name__ == "__main__":
    unittest.main()
