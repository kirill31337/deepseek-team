"""Cleanup never overwrites a destination created after validation."""
import importlib
import os
from pathlib import Path
import tempfile
import unittest


class AtomicRenameTests(unittest.TestCase):
    def test_existing_files_and_directories_are_preserved(self):
        rename = importlib.import_module('codex_deepseek_team.atomic_rename').move
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                for kind in ('file', 'directory'):
                    source, target = root / ('source-' + kind), root / ('target-' + kind)
                    if kind == 'directory':
                        source.mkdir(); target.mkdir()
                        (target / 'canary').write_bytes(b'preserve')
                    else:
                        source.write_bytes(b'source'); target.write_bytes(b'preserve')
                    before = target.stat().st_ino
                    with self.assertRaises(FileExistsError):
                        rename(fd, source.name, fd, target.name)
                    self.assertEqual(target.stat().st_ino, before)
                    self.assertTrue(source.exists())
                    self.assertEqual((target / 'canary' if kind == 'directory' else target).read_bytes(), b'preserve')
            finally:
                os.close(fd)

    def test_success_keeps_same_directory_inode(self):
        rename = importlib.import_module('codex_deepseek_team.atomic_rename').move
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'work').mkdir()
            inode = (root / 'work').stat().st_ino
            fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                rename(fd, 'work', fd, 'quarantine')
                self.assertEqual((root / 'quarantine').stat().st_ino, inode)
                self.assertFalse((root / 'work').exists())
            finally:
                os.close(fd)
