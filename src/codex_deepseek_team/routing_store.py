"""Private, transactional persistence for credential-free routing records."""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import os
from pathlib import Path
import re
import sqlite3
import stat
import time

from .routing_models import (MAX_JSON_BYTES, MAX_OBSERVATIONS, RoutingError, canonical,
                             fingerprint, normalized_record_features, read_json)


TABLES = frozenset(('observations', 'sources', 'decisions'))
BASE_SCHEMA = {
    'metadata': 'CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)',
    **{table: f'CREATE TABLE {table} (id TEXT PRIMARY KEY, value TEXT NOT NULL)' for table in TABLES},
    'audit': 'CREATE TABLE audit (sequence INTEGER PRIMARY KEY, at REAL NOT NULL, '
             'kind TEXT NOT NULL, entity TEXT NOT NULL, digest TEXT NOT NULL)',
}
INDEX_SCHEMA = {
    'evidence_index': 'CREATE TABLE evidence_index (id TEXT PRIMARY KEY REFERENCES observations(id), '
                      'case_key TEXT NOT NULL, decision_id TEXT, case_id TEXT NOT NULL, observed_at REAL NOT NULL)',
    'evidence_case': 'CREATE INDEX evidence_case ON evidence_index (case_key, observed_at)',
    'evidence_decision': 'CREATE INDEX evidence_decision ON evidence_index (decision_id, case_id)',
}


def evidence_key(row):
    features = row['features']
    return fingerprint([row['origin'], row['case_id'], row['action'],
                        *(features[key] for key in ('runtime', 'model', 'effort', 'context_version'))])


def evidence_keys(row):
    """Include the pre-upgrade key without rewriting historical evidence indexes."""
    canonical_row = normalized_record_features(row)
    keys = [evidence_key(canonical_row)]
    if canonical_row['features']['effort'] == 'high':
        legacy = dict(canonical_row, features=dict(canonical_row['features'], effort='medium'))
        keys.append(evidence_key(legacy))
    return keys


def _private_file(fd):
    info = os.fstat(fd)
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600):
        raise RoutingError('Routing files must be private, user-owned, ordinary files.', 78)
    return info


def _directory(path: Path) -> int:
    """Walk by directory descriptors so symlinks cannot redirect state creation."""
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            try:
                os.mkdir(part, mode=0o700, dir_fd=fd)
            except FileExistsError:
                pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        info = os.fstat(fd)
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise RoutingError('Routing directories must be private and user-owned.', 78)
        return fd
    except BaseException:
        os.close(fd)
        raise


class RoutingStore:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        state = Path(os.environ.get('DEEPSEEK_TEAM_STATE_DIR') or
                     Path.home() / '.local/state/codex-deepseek')
        if not state.is_absolute() or '..' in state.parts:
            raise RoutingError('DEEPSEEK_TEAM_STATE_DIR must be an absolute canonical path.', 78)
        if state.resolve().is_relative_to(self.root):
            raise RoutingError('Routing state must be outside the project working tree.', 78)
        self.state = state
        key = hashlib.sha256(os.fsencode(str(self.root))).hexdigest()[:24]
        self.path = state / 'routing' / key / 'routing-v3.sqlite3'

    @contextmanager
    def transaction(self):
        directory = lock = handle = None
        connection = None
        try:
            for path in (self.state, self.path.parent.parent):
                fd = _directory(path)
                os.close(fd)
            directory = _directory(self.path.parent)
            lock = os.open('routing.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,
                           0o600, dir_fd=directory)
            _private_file(lock)
            fcntl.flock(lock, fcntl.LOCK_EX)
            for suffix in ('', '-journal', '-wal', '-shm'):
                try:
                    fd = os.open(self.path.name + suffix,
                                 os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                                 dir_fd=directory)
                except FileNotFoundError:
                    continue
                try:
                    _private_file(fd)
                finally:
                    os.close(fd)
            handle = os.open(self.path.name, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,
                             0o600, dir_fd=directory)
            before = _private_file(handle)
            pinned_path = f'/proc/self/fd/{directory}/{self.path.name}'
            connection = sqlite3.connect(pinned_path, timeout=30, isolation_level=None)
            after = os.stat(self.path.name, dir_fd=directory, follow_symlinks=False)
            if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
                raise RoutingError('Routing database changed while opening.', 78)
            connection.execute('PRAGMA trusted_schema=OFF')
            connection.execute('PRAGMA journal_mode=DELETE')
            connection.execute('PRAGMA synchronous=FULL')
            connection.execute('PRAGMA foreign_keys=ON')
            connection.execute('BEGIN IMMEDIATE')
            self._initialize(connection)
            yield connection
            connection.commit()
        except (OSError, sqlite3.Error):
            raise RoutingError('Cannot safely access routing state; check ownership, permissions and database integrity.', 78) from None
        finally:
            if connection is not None:
                connection.close()  # Uncommitted transactions roll back, including failed imports.
            for fd in (handle, lock, directory):
                if fd is not None:
                    os.close(fd)

    def _initialize(self, db):
        version = db.execute('PRAGMA user_version').fetchone()[0]
        if version not in (0, 3):
            raise RoutingError('Unsupported routing state format; this package only opens format 3. Existing data was not migrated.', 78)
        if version == 0:
            from . import routing_admission
            for sql in (*BASE_SCHEMA.values(), *INDEX_SCHEMA.values()):
                db.execute(sql)
            routing_admission.initialize(db)
            db.execute('INSERT INTO metadata VALUES (?, ?)', ('project', str(self.root)))
            db.execute('PRAGMA user_version=3')
        self._verify_schema(db)
        row = db.execute("SELECT value FROM metadata WHERE key='project'").fetchone()
        if row is None or row[0] != str(self.root):
            raise RoutingError('Routing state belongs to another project.', 78)

    @staticmethod
    def _verify_schema(db):
        from . import routing_admission, routing_budget
        expected = dict(BASE_SCHEMA)
        expected.update(INDEX_SCHEMA)
        budget = dict(zip(('routing_budget', *routing_budget.INDEXES), routing_budget._SCHEMA))
        expected.update(budget)
        expected.update(routing_admission.SCHEMA)
        def normalized(sql):
            return re.sub(r'\s+', ' ', sql.replace('IF NOT EXISTS ', '')).strip()
        present = set()
        for kind, name, sql in db.execute('SELECT type, name, sql FROM sqlite_master'):
            if kind == 'index' and name.startswith('sqlite_autoindex_') and sql is None:
                continue
            if kind not in ('table', 'index') or name not in expected or normalized(sql or '') != normalized(expected[name]):
                raise RoutingError('Unexpected routing database schema; refusing changed tables, views or triggers.', 78)
            present.add(name)
        required = set(BASE_SCHEMA) | set(INDEX_SCHEMA) | set(routing_admission.SCHEMA)
        if not required <= present or (present & set(budget) and not set(budget) <= present):
            raise RoutingError('Routing database schema is incomplete.', 78)

    @staticmethod
    def _index(db, row):
        try:
            values = (row['id'], evidence_key(row), row.get('decision_id'), row['case_id'], row['observed_at'])
        except (KeyError, TypeError):
            raise RoutingError('Invalid stored observation schema.', 78) from None
        if row.get('decision_id'):
            if row['origin'] != 'local':
                raise RoutingError('External evidence cannot reference private local decisions.', 65)
            existing = db.execute('SELECT case_id FROM evidence_index WHERE decision_id=? LIMIT 1',
                                  (row['decision_id'],)).fetchone()
            if existing and existing[0] != row['case_id']:
                raise RoutingError('One recorded decision cannot represent different observed cases.', 65)
        db.execute('INSERT INTO evidence_index VALUES (?, ?, ?, ?, ?)', values)

    @staticmethod
    def get(db, table, record_id):
        if table not in TABLES:
            raise RoutingError('Unknown routing record type.')
        row = db.execute(f'SELECT value FROM {table} WHERE id=?', (record_id,)).fetchone()
        return None if row is None else normalized_record_features(read_json(row[0]))

    @staticmethod
    def all(db, table):
        if table not in TABLES:
            raise RoutingError('Unknown routing record type.')
        return [normalized_record_features(read_json(row[0]))
                for row in db.execute(f'SELECT value FROM {table} ORDER BY id')]

    @classmethod
    def put(cls, db, table, record_id, value):
        previous = cls.get(db, table, record_id)
        raw = canonical(value)
        if len(raw.encode('utf-8')) > MAX_JSON_BYTES:
            raise RoutingError('Routing record exceeds the 8 MiB storage limit.')
        if previous is not None:
            if canonical(previous) != raw:
                raise RoutingError(f'Conflicting {table} identity; existing records are immutable.', 65)
            return False
        if db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] >= MAX_OBSERVATIONS:
            raise RoutingError(f'{table} capacity reached; export and rotate project context.', 78)
        db.execute(f'INSERT INTO {table} VALUES (?, ?)', (record_id, raw))
        if table == 'observations':
            cls._index(db, value)
        cls.audit(db, table, record_id, value)
        return True

    @staticmethod
    def audit(db, kind, entity, value):
        db.execute('INSERT INTO audit (at, kind, entity, digest) VALUES (?, ?, ?, ?)',
                   (time.time(), kind, entity, hashlib.sha256(canonical(value).encode()).hexdigest()))
