import os
from pathlib import Path
import sqlite3
import stat
import tempfile
import unittest
from unittest.mock import patch

from codex_deepseek_team.routing_models import RoutingError
from codex_deepseek_team.routing_store import RoutingStore


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'project'
        self.root.mkdir()
        self.state = Path(self.tmp.name) / 'private'
        self.env = patch.dict(os.environ, DEEPSEEK_TEAM_STATE_DIR=str(self.state))
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_private_persistent_isolated_state(self):
        store = RoutingStore(self.root)
        with store.transaction() as db:
            store.put(db, 'sources', 'one', {'id': 'one'})
        with RoutingStore(self.root).transaction() as db:
            self.assertEqual(store.get(db, 'sources', 'one'), {'id': 'one'})
        self.assertEqual(stat.S_IMODE(store.path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(store.path.parent.stat().st_mode), 0o700)
        other = self.root.parent / 'other'
        other.mkdir()
        with RoutingStore(other).transaction() as db:
            self.assertEqual(store.all(db, 'sources'), [])

    def test_idempotency_conflict_and_rollback(self):
        store = RoutingStore(self.root)
        with store.transaction() as db:
            self.assertTrue(store.put(db, 'sources', 'one', {'a': 1}))
            self.assertFalse(store.put(db, 'sources', 'one', {'a': 1}))
        with self.assertRaises(RoutingError):
            with store.transaction() as db:
                store.put(db, 'sources', 'two', {'a': 2})
                store.put(db, 'sources', 'one', {'a': 3})
        with store.transaction() as db:
            self.assertIsNone(store.get(db, 'sources', 'two'))

    def test_rejects_unsafe_database_and_sidecar(self):
        for kind in ('symlink', 'hardlink', 'journal', 'mode'):
            with self.subTest(kind=kind):
                store = RoutingStore(self.root / kind)
                with store.transaction():
                    pass
                target = self.root / ('target-' + kind)
                target.write_text('preserve')
                if kind == 'symlink':
                    store.path.unlink()
                    store.path.symlink_to(target)
                elif kind == 'hardlink':
                    store.path.unlink()
                    os.link(target, store.path)
                elif kind == 'journal':
                    Path(str(store.path) + '-journal').symlink_to(target)
                else:
                    store.path.chmod(0o644)
                with self.assertRaises(RoutingError):
                    with store.transaction():
                        pass
                self.assertEqual(target.read_text(), 'preserve')

    def test_rejects_symlink_state_root_and_relative_path(self):
        target = self.root / 'state'
        target.mkdir(mode=0o700)
        self.state.symlink_to(target)
        with self.assertRaises(RoutingError):
            with RoutingStore(self.root).transaction():
                pass
        with patch.dict(os.environ, DEEPSEEK_TEAM_STATE_DIR='relative'):
            with self.assertRaises(RoutingError):
                RoutingStore(self.root)

    def test_unknown_schema_fails_closed(self):
        store = RoutingStore(self.root)
        with store.transaction() as db:
            db.execute('PRAGMA user_version=99')
        with self.assertRaises(RoutingError):
            with store.transaction():
                pass

    def test_state_cannot_live_inside_repository(self):
        with patch.dict(os.environ, DEEPSEEK_TEAM_STATE_DIR=str(self.root / 'state')):
            with self.assertRaises(RoutingError):
                RoutingStore(self.root)

    def test_changed_schema_and_triggers_are_rejected_before_use(self):
        store = RoutingStore(self.root)
        with store.transaction() as db:
            db.execute('CREATE TRIGGER surprise AFTER INSERT ON observations BEGIN DELETE FROM sources; END')
        with self.assertRaises(RoutingError):
            with store.transaction():
                pass

    def test_oversized_record_cannot_be_committed(self):
        store = RoutingStore(self.root)
        with self.assertRaises(RoutingError):
            with store.transaction() as db:
                store.put(db, 'sources', 'too-large', {'value': 'a' * (8 * 1024 * 1024)})


if __name__ == '__main__':
    unittest.main()
