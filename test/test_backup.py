from contextlib import closing
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backup import snapshot
from intake import Queue
from receipts import inspect
from worker import claim


class Backups(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / 'queue?#.db'
        self.output = self.root / 'snapshot.db'
        self.queue = Queue(self.source)
        with closing(self.queue.connect()) as db, db:
            for i, state in enumerate(['queued', 'pending', 'accepted', 'rejected']):
                db.execute('INSERT INTO jobs(site,event_key,content_hash,title,url,state,created_at) VALUES(?,?,?,?,?,?,?)',
                           ('site', f'{i:064x}', 'hash', 'Private title', 'https://private.test/', state, i))

    def test_live_wal_backup_and_isolated_restore_preserve_states(self):
        with closing(self.queue.connect()) as writer:
            writer.execute("UPDATE jobs SET title='Committed WAL' WHERE state='accepted'")
            writer.commit()
            self.assertTrue(Path(str(self.source) + '-wal').exists())
            writer.execute("UPDATE jobs SET state='accepted' WHERE state='pending'")
            report = snapshot(self.source, self.output)
            self.assertEqual(report['counts'], dict.fromkeys(['queued', 'pending', 'accepted', 'rejected'], 1))
            writer.rollback()
        restore = self.root / 'isolated-restore'
        restore.mkdir()
        restored = restore / 'queue.db'
        shutil.copyfile(self.output, restored)  # Only this file, no WAL/SHM.
        self.assertEqual(inspect(restored, 'site')['counts'], report['counts'])
        with closing(sqlite3.connect(restored)) as db:
            self.assertEqual(db.execute('PRAGMA integrity_check').fetchall(), [('ok',)])
            self.assertEqual(db.execute("SELECT title FROM jobs WHERE state='accepted'").fetchone()[0], 'Committed WAL')
        queue = Queue(restored)
        self.assertIsNotNone(claim(queue, 'site'))
        self.assertIsNone(claim(queue, 'site'))  # Pending/accepted/rejected never reclaimed.
        self.assertEqual(inspect(self.source, 'site')['counts']['queued'], 1)
        self.assertEqual(self.output.stat().st_mode & 0o777, 0o600)
        self.assertEqual(hashlib.sha256(self.output.read_bytes()).hexdigest(), report['sha256'])
        self.assertEqual(list(self.root.glob('.ghost-backup-*')), [])
        self.assertNotIn('Private', json.dumps(report))

    def test_old_snapshot_can_reintroduce_eligible_work_requires_reconciliation(self):
        snapshot(self.source, self.output)
        claimed = claim(self.queue, 'site')
        self.assertIsNotNone(claimed)
        self.assertEqual(inspect(self.source, 'site')['counts']['queued'], 0)
        self.assertEqual(inspect(self.output, 'site')['counts']['queued'], 1)
        self.assertIn('snapshot gap', snapshot(self.source, self.root / 'new.db')['restore_policy'])

    def test_refuses_existing_source_target_symlink_and_missing_source(self):
        for output in (self.source, self.root / 'alias.db', self.root / 'existing.db'):
            if output.name == 'alias.db': output.symlink_to(self.root / 'missing-target')
            if output.name == 'existing.db': output.write_bytes(b'preserve')
            with self.assertRaises(FileExistsError): snapshot(self.source, output)
        self.assertEqual((self.root / 'existing.db').read_bytes(), b'preserve')
        missing = self.root / 'missing.db'
        with self.assertRaises(sqlite3.Error): snapshot(missing, self.output)
        self.assertFalse(missing.exists())
        self.assertFalse(self.output.exists())

    def test_bad_schema_and_state_never_publish_and_clean_temporary(self):
        with closing(self.queue.connect()) as db, db:
            db.execute("UPDATE jobs SET state='unknown'")
        with self.assertRaises(ValueError): snapshot(self.source, self.output)
        self.assertFalse(self.output.exists())
        with closing(self.queue.connect()) as db, db: db.execute('DROP TABLE jobs')
        with self.assertRaises(sqlite3.Error): snapshot(self.source, self.output)
        self.assertEqual(list(self.root.glob('.ghost-backup-*')), [])

    def test_timeout_never_publishes(self):
        with patch('backup.time.monotonic', side_effect=[0, 31]):
            with self.assertRaises(TimeoutError): snapshot(self.source, self.output)
        self.assertFalse(self.output.exists())
        self.assertEqual(list(self.root.glob('.ghost-backup-*')), [])

    def test_racing_destination_is_not_overwritten(self):
        import os
        link = os.link
        def racing_link(source, target):
            Path(target).write_bytes(b'other backup')
            return link(source, target)
        with patch('backup.os.link', side_effect=racing_link):
            with self.assertRaises(FileExistsError): snapshot(self.source, self.output)
        self.assertEqual(self.output.read_bytes(), b'other backup')
        self.assertEqual(list(self.root.glob('.ghost-backup-*')), [])

    def test_cli_sanitizes_error_and_reports_success_without_content(self):
        command = [sys.executable, str(Path(__file__).resolve().parents[1] / 'backup.py'),
                   '--database', str(self.source), '--output', str(self.output)]
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['counts']['pending'], 1)
        self.assertNotIn('Private', result.stdout)
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, '')
        self.assertNotIn(str(self.root), result.stderr)
