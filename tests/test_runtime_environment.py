"""Installed Python launchers must work inside the sparse worker namespace."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from codex_deepseek_team import development, sandbox


class BridgeInterpreterTests(unittest.TestCase):
    def setUp(self):
        try:
            self.backend = sandbox.probe_backend()
        except sandbox.SandboxError as error:
            if os.environ.get('DEEPSEEK_TEAM_REQUIRE_LIVE') == '1':
                self.fail(str(error))
            self.skipTest(str(error))
        temporary = tempfile.TemporaryDirectory(prefix='dst-bridge-python-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.work, self.home, self.control = [self.root / name for name in ('work', 'home', 'control')]
        for path in (self.work, self.home, self.control, self.work / '.git'):
            path.mkdir()
        self.env = {'PATH': os.defpath, 'HOME': str(self.home)}

    def test_venv_symlink_chain_launches_bridge_without_exposing_prefix(self):
        prefix = self.root / 'private-prefix'
        (prefix / 'bin').mkdir(parents=True)
        launcher = prefix / 'bin/python'
        launcher.symlink_to('python3')
        (prefix / 'bin/python3').symlink_to(Path(sys.executable).resolve())
        secret = prefix / 'unrelated-private-file'
        secret.write_text('must stay outside namespace')
        (self.control / 'bridge.py').write_text(
            'from pathlib import Path\n'
            f'assert not Path({str(secret)!r}).exists()\n'
            'print("BRIDGE_PYTHON_OK")\n')
        with patch.object(development.sys, 'executable', str(launcher)):
            layout = development.layout(self.backend, self.work, self.home, self.control, [])
            result = subprocess.run(development.bridge_command(layout), env=self.env,
                                    text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('BRIDGE_PYTHON_OK', result.stdout)

    def test_preflight_rejects_missing_bridge_interpreter(self):
        with patch.object(development.sys, 'executable', str(self.root / 'missing-python')):
            layout = development.layout(self.backend, self.work, self.home, self.control, [])
            with self.assertRaises(development.DevelopmentError):
                development.probe(layout, self.env)
