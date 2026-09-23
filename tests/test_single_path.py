"""Removed policies cannot silently reactivate older execution paths."""
import contextlib
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from codex_deepseek_team import coordination, routing_cli
from codex_deepseek_team.routing_models import RoutingError, validate_config
from codex_deepseek_team.routing_store import RoutingStore


class SinglePathTests(unittest.TestCase):
    def test_removed_configuration_is_rejected(self):
        for config in ({'admission_policy': 'evidence'}, {'recovery_rate': .1},
                       {'recovery_cooldown_seconds': 3600},
                       {'min_success_probability': .8}, {'require_cost_evidence': True}):
            with self.subTest(config=config), self.assertRaises(RoutingError):
                validate_config(config)
        self.assertEqual(validate_config({})['failure_cooldown_seconds'], 300)

    def test_removed_trial_commands_are_not_available(self):
        with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()), \
                patch.object(routing_cli, 'RoutingService', side_effect=AssertionError('removed command reached state')):
            self.assertEqual(routing_cli.main(['routing', 'recovery', 'status']), 64)
            self.assertEqual(routing_cli.main(['routing', 'configure', '--admission-policy', 'evidence']), 64)
            self.assertEqual(routing_cli.main(['routing', 'configure', '--min-success-probability', '.8']), 64)
            self.assertEqual(routing_cli.main(['routing', 'configure', '--no-require-cost-evidence']), 64)

    def test_old_database_is_rejected_without_migration_or_data_loss(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / 'project'
            root.mkdir()
            with patch.dict(os.environ, DEEPSEEK_TEAM_STATE_DIR=str(base / 'state')):
                for version in (1, 2):
                    store = RoutingStore(root / str(version))
                    with store.transaction() as db:
                        store.put(db, 'sources', 'keep', {'id': 'keep'})
                        db.execute(f'PRAGMA user_version={version}')
                    before = store.path.read_bytes()
                    with self.subTest(version=version), self.assertRaises(RoutingError):
                        with store.transaction():
                            self.fail('Unsupported format was opened')
                    self.assertEqual(store.path.read_bytes(), before)

    def test_feedback_without_current_explicit_result_cannot_complete_work(self):
        item = dict(id='impl', executor='coordinator', routing_feedback=[{
            'observation': {'action': 'coordinator', 'outcome': 'accepted', 'observed_at': 1}}])
        self.assertTrue(coordination.completion_issues({'deliverables': [item]}))


if __name__ == '__main__':
    unittest.main()
