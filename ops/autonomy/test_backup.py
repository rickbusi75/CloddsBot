"""No network, funds or production files; exercise durable SQLite snapshots."""
import contextlib
import importlib.util
import io
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('autonomy_backup', Path(__file__).with_name('backup-observer.py'))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='clodds-backup-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / 'data/cloddsbot/autonomy'
        self.target = self.root / 'dumps/cloddsbot'
        self.source.mkdir(parents=True)

    def database(self, name):
        db = sqlite3.connect(self.source / name)
        self.addCleanup(db.close)
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('PRAGMA synchronous=FULL')
        db.execute('CREATE TABLE intentions(id TEXT PRIMARY KEY,state TEXT,reservation REAL)')
        db.execute("INSERT INTO intentions VALUES('fixture','SENDING',1.23841)")
        db.commit()
        return db

    def backup(self, **kwargs):
        with contextlib.redirect_stdout(io.StringIO()):
            module.backup(self.root, **kwargs)

    def restored(self, name):
        with contextlib.closing(sqlite3.connect((self.target / name).as_uri() + '?mode=ro', uri=True)) as db:
            self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
            return db.execute('SELECT state,reservation FROM intentions').fetchall()

    def test_open_wal_restores_both_databases_without_releasing_uncertainty(self):
        for name in ('observer.sqlite', 'live.sqlite'):
            self.database(name)
            self.assertTrue((self.source / (name + '-wal')).exists())
        self.backup()
        for name in ('observer.sqlite', 'live.sqlite'):
            self.assertEqual(self.restored(name), [('SENDING', 1.23841)])
            if os.name != 'nt':
                self.assertEqual((self.target / name).stat().st_mode & 0o777, 0o600)
        self.assertEqual(list(self.target.glob('*.pending')), [])

    def test_corruption_preserves_the_last_good_snapshot(self):
        db = self.database('live.sqlite')
        self.backup()
        previous = (self.target / 'live.sqlite').read_bytes()
        db.close()
        (self.source / 'live.sqlite').write_bytes(b'not a SQLite database')
        with self.assertRaises(sqlite3.DatabaseError):
            self.backup()
        self.assertEqual((self.target / 'live.sqlite').read_bytes(), previous)
        self.assertEqual(self.restored('live.sqlite'), [('SENDING', 1.23841)])
        self.assertEqual(list(self.target.glob('*.pending')), [])

    def test_timeout_does_not_publish_partial_data_or_overwrite_the_prior_dump(self):
        self.database('live.sqlite')
        self.backup()
        previous = (self.target / 'live.sqlite').read_bytes()
        with self.assertRaises(TimeoutError):
            self.backup(timeout=0)
        self.assertEqual((self.target / 'live.sqlite').read_bytes(), previous)
        self.assertEqual(list(self.target.glob('*.pending')), [])

    def test_missing_previously_backed_up_source_is_not_treated_as_an_empty_ledger(self):
        db = self.database('live.sqlite')
        self.backup()
        db.close()
        (self.source / 'live.sqlite').unlink()
        with self.assertRaisesRegex(RuntimeError, 'source missing'):
            self.backup()
        self.assertEqual(self.restored('live.sqlite'), [('SENDING', 1.23841)])

    def test_absent_optional_ledger_is_not_created(self):
        self.database('observer.sqlite')
        self.backup()
        self.assertEqual(self.restored('observer.sqlite'), [('SENDING', 1.23841)])
        self.assertFalse((self.source / 'live.sqlite').exists())
        self.assertFalse((self.target / 'live.sqlite').exists())


if __name__ == '__main__':
    unittest.main()
