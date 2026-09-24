"""The original tracked-backup failure through the CLI and real OS admission."""
import json
import os
from pathlib import Path
import subprocess
import sys

from codex_deepseek_team import coordination, settings
from tests import test_live_delegation as fixtures


class PreparationCliTests(fixtures.LiveBase):
    def cli(self, *arguments):
        env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / 'src'),
                   HOME=str(self.root / 'home'), CODEX_HOME=str(self.root / 'codex'))
        return subprocess.run([sys.executable, '-m', 'codex_deepseek_team', *arguments],
                              cwd=self.source, env=env, input='Review calc.py only.',
                              text=True, capture_output=True, timeout=30)

    def test_tracked_backup_failure_can_be_closed_and_clean_head_prepared(self):
        (self.source / '.env.backup').write_text('NONSECRET_FIXTURE_MUST_NOT_BE_PRINTED\n')
        for arguments in [('add', '.env.backup'), ('-c', 'user.name=Test', '-c',
                'user.email=test@example.test', 'commit', '-qm', 'tracked backup')]:
            subprocess.run(['git', '-C', str(self.source), *arguments], check=True, capture_output=True)
        settings.set_values(self.source / settings.PROJECT_FILE,
                            delegation_level=50, access='full-access')
        policy = settings.resolve(self.source)
        task = coordination.open_task(self.source, session_id='preflight-cli', turn_id='1',
                                      prompt='review calc.py', policy=policy)
        task = coordination.plan_task(self.source, task['id'], {
            'classification': 'substantial', 'deliverables': [{
                'id': 'review', 'kind': 'review', 'executor': 'worker',
                'scope': ['calc.py'], 'acceptance': ['review completed'],
                'dependencies': [], 'checks': [],
            }],
        })
        assignment = task['assignments'][0]['id']
        command = ('worker', '--runtime', 'claude', '--claude', str(self.driver),
                   '--effort', 'high', '--coord-task', task['id'],
                   '--coord-assignment', assignment, '--state-dir', str(self.state))
        result = self.cli(*command)
        self.assertEqual(result.returncode, 78, result.stderr)
        self.assertIn('.env.backup', result.stderr)
        self.assertNotIn('NONSECRET_FIXTURE_MUST_NOT_BE_PRINTED', result.stdout + result.stderr)
        row = coordination.load_task(self.source, task['id'])['assignments'][0]
        self.assertEqual((row['status'], row['queue_state'], row['error_kind']),
                         ('failed', 'finished', 'environment'))
        self.assertEqual(row['failure_stage'], 'preparation')
        self.assertFalse(row.get('workspace_id'))
        self.assertFalse(row.get('started_at'))
        self.assertFalse((self.state / 'workspaces').exists())

        repeated = self.cli(*command)
        self.assertEqual(repeated.returncode, 78, repeated.stderr)
        self.assertEqual(coordination.load_task(self.source, task['id'])['assignments'][0], row)
        coordination.use_result(self.source, task['id'], assignment, 'rejected',
                                'Inspected committed-name blocker; model did not start.')
        coordination.complete_task(self.source, task['id'])

        # Untracking without a commit does not fix HEAD. The required server
        # file remains on disk both before and after the actual source change.
        subprocess.run(['git', '-C', str(self.source), 'rm', '--cached', '.env.backup'],
                       check=True, capture_output=True)
        check = self.cli('workspace', 'check', '--json')
        self.assertEqual(check.returncode, 78, check.stderr)
        self.assertFalse(json.loads(check.stdout)['eligible'])
        subprocess.run(['git', '-C', str(self.source), '-c', 'user.name=Test', '-c',
                        'user.email=test@example.test', 'commit', '-qm', 'untrack backup'],
                       check=True, capture_output=True)
        check = self.cli('workspace', 'check', '--json')
        self.assertEqual(check.returncode, 0, check.stderr)
        self.assertTrue(json.loads(check.stdout)['eligible'])
        created = self.cli('workspace', 'create', '--state-dir', str(self.state), '--json')
        self.assertEqual(created.returncode, 0, created.stderr)
        copy = json.loads(created.stdout)
        self.assertEqual(copy['status'], 'ready')
        self.assertFalse((Path(copy['path']) / '.env.backup').exists())
        self.assertEqual((self.source / '.env.backup').read_text(), 'NONSECRET_FIXTURE_MUST_NOT_BE_PRINTED\n')
