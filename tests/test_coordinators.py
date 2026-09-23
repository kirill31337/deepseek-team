"""Coordinator-native managed instructions for Codex and Claude Code."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from codex_deepseek_team import project


class CoordinatorProjectTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='deepseek-team-project-'))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.repo = self.tmp / 'repo'
        self.repo.mkdir()
        subprocess.run(['git', 'init', '-q', os.fspath(self.repo)], check=True,
                       stderr=subprocess.DEVNULL)

    def test_claude_attach_targets_only_claude_md_and_detaches_cleanly(self):
        claude = self.repo / 'CLAUDE.md'
        self.assertTrue(project.attach(self.repo, coordinator='claude'))
        self.assertTrue(claude.exists())
        self.assertFalse((self.repo / 'AGENTS.md').exists())
        content = claude.read_text()
        self.assertIn('worker --runtime claude', content)
        self.assertTrue(project.detach(self.repo, coordinator='claude'))
        self.assertFalse(claude.exists())

    def test_both_coordinators_get_runtime_specific_instructions(self):
        self.assertTrue(project.attach(self.repo, coordinator='both'))
        agents = (self.repo / 'AGENTS.md').read_text()
        claude = (self.repo / 'CLAUDE.md').read_text()
        self.assertIn('worker --runtime codex', agents)
        self.assertIn('worker --runtime claude', claude)
        for content in (agents, claude):
            self.assertIn('OS sandbox', content)
            self.assertIn('Every worker requires the Linux OS sandbox.', content)
            self.assertNotIn('worker --os-sandbox off', content)
            self.assertNotIn('--runtime codex --os-sandbox off', content)
            self.assertNotIn('--runtime claude --os-sandbox off', content)
        self.assertFalse(project.attach(self.repo, coordinator='both'))
        self.assertTrue(project.detach(self.repo, coordinator='both'))
        self.assertFalse((self.repo / 'AGENTS.md').exists())
        self.assertFalse((self.repo / 'CLAUDE.md').exists())

    def test_default_attach_remains_codex_compatible(self):
        self.assertTrue(project.attach(self.repo))
        self.assertTrue((self.repo / 'AGENTS.md').exists())
        self.assertFalse((self.repo / 'CLAUDE.md').exists())
        self.assertIn('worker --runtime codex', (self.repo / 'AGENTS.md').read_text())

    def test_claude_existing_bytes_and_mode_are_preserved(self):
        claude = self.repo / 'CLAUDE.md'
        original = b'# Existing Claude rules\r\nkeep this byte-for-byte'
        claude.write_bytes(original)
        os.chmod(claude, 0o640)
        self.assertTrue(project.attach(self.repo, coordinator='claude'))
        self.assertTrue(project.detach(self.repo, coordinator='claude'))
        self.assertEqual(claude.read_bytes(), original)
        self.assertEqual(claude.stat().st_mode & 0o777, 0o640)

    def test_unknown_coordinator_is_rejected_without_files(self):
        with self.assertRaises(project.ProjectError):
            project.attach(self.repo, coordinator='unknown')
        self.assertEqual({path.name for path in self.repo.iterdir()}, {'.git'})


if __name__ == '__main__':
    unittest.main()
