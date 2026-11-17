"""Run the DSH bridge plugin's node:test suite from the Python test discovery.

The bridge is dependency-free ESM; its tests use a fake cordis context and a
fixture hook entrypoint, so no DSH installation is required. Node is discovered
via ``DEEPSEEK_TEAM_NODE`` or PATH; without Node the suite is skipped (the
Python hook protocol remains covered by test_qwen_hooks.py).
"""
import os
from pathlib import Path
import shutil
import subprocess
import unittest


REPO = Path(__file__).resolve().parents[1]
BRIDGE_TESTS = REPO / 'tests' / 'dsh_bridge' / 'bridge.test.mjs'


def find_node():
    override = os.environ.get('DEEPSEEK_TEAM_NODE')
    if override and Path(override).is_file():
        return override
    return shutil.which('node')


class QwenBridgeTests(unittest.TestCase):
    def test_bridge_event_translation_and_fail_closed(self):
        node = find_node()
        if not node:
            self.skipTest('Node is unavailable; set DEEPSEEK_TEAM_NODE or install Node '
                          'to run the DSH bridge suite.')
        result = subprocess.run([node, '--test', str(BRIDGE_TESTS)],
                                cwd=REPO, capture_output=True, text=True, timeout=180)
        self.assertEqual(result.returncode, 0,
                         result.stdout[-4000:] + '\n' + result.stderr[-4000:])


if __name__ == '__main__':
    unittest.main()