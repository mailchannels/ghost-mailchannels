from contextlib import closing
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from intake import Queue
from receipts import inspect


class Receipts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'queue?#.db'
        self.queue = Queue(self.path)
        with closing(self.queue.connect()) as db, db:
            for i, state in enumerate(['queued', 'pending', 'accepted', 'rejected']):
                db.execute('INSERT INTO jobs(site,event_key,content_hash,title,url,state,created_at) VALUES(?,?,?,?,?,?,?)',
                           ('site-a', f'{i:064x}', 'hash', 'Private title', 'https://private.example.test/', state, 100+i))
            db.execute('INSERT INTO jobs(site,event_key,content_hash,title,url,state,created_at) VALUES(?,?,?,?,?,?,?)',
                       ('site-b', 'f'*64, 'hash', 'Other site title', 'https://other.example.test/', 'pending', 90))

    def test_site_scoped_redacted_snapshot_and_counts(self):
        result = inspect(self.path, 'site-a')
        self.assertEqual(result['counts'], dict.fromkeys(['queued','pending','accepted','rejected'], 1))
        self.assertEqual(result['total_receipts'], 4)
        self.assertNotIn('title', json.dumps(result))
        self.assertNotIn('https://', json.dumps(result))
        self.assertEqual(result['oldest_intake_at_by_state']['pending'], 101)
        self.assertEqual(inspect(self.path, 'unknown')['total_receipts'], 0)

    def test_bounded_cursor_pages_do_not_skip_or_repeat(self):
        first = inspect(self.path, 'site-a', limit=2)
        second = inspect(self.path, 'site-a', limit=2, after=first['next_cursor'])
        self.assertIsNone(second['next_cursor'])
        self.assertEqual([r['event_key'] for r in first['receipts'] + second['receipts']], [f'{i:064x}' for i in range(4)])
        self.assertEqual(second['total_receipts'], 4)

    def test_explicit_content_and_no_state_changes(self):
        before = inspect(self.path, 'site-a')
        detailed = inspect(self.path, 'site-a', include_content=True)
        self.assertEqual(detailed['receipts'][0]['title'], 'Private title')
        self.assertEqual(inspect(self.path, 'site-a')['receipts'], before['receipts'])

    def test_reads_committed_wal_but_not_uncommitted_changes(self):
        with closing(self.queue.connect()) as writer:
            writer.execute("UPDATE jobs SET state='accepted' WHERE site='site-a' AND state='pending'")
            self.assertEqual(inspect(self.path, 'site-a')['counts']['pending'], 1)
            writer.commit()
            self.assertEqual(inspect(self.path, 'site-a')['counts']['accepted'], 2)

    def test_missing_database_not_created(self):
        missing = Path(self.tmp.name) / 'missing.db'
        with self.assertRaises(sqlite3.OperationalError): inspect(missing, 'site-a')
        self.assertFalse(missing.exists())

    def test_invalid_arguments_and_unknown_state_fail(self):
        for args in [{'limit':0}, {'limit':501}, {'after':"' OR 1=1 --"}]:
            with self.assertRaises(ValueError): inspect(self.path, 'site-a', **args)
        with closing(self.queue.connect()) as db, db:
            db.execute("UPDATE jobs SET state='invalid' WHERE site='site-a'")
        with self.assertRaises(ValueError): inspect(self.path, 'site-a')

    def test_cli_redaction_and_sanitized_failure(self):
        cmd = [sys.executable, str(Path(__file__).resolve().parents[1] / 'receipts.py'), '--site','site-a','--database',str(self.path)]
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout)['counts']['pending'],1)
        self.assertNotIn('Private title',result.stdout)
        cmd[-1] = str(Path(self.tmp.name) / 'private-missing.db')
        result = subprocess.run(cmd, capture_output=True, text=True)
        self.assertEqual(result.returncode,1)
        self.assertEqual(result.stdout,'')
        self.assertNotIn('private-missing',result.stderr)
