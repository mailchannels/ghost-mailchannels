"""Attempt evidence is atomic with claims and never makes unknown sends retryable."""
from contextlib import closing
from dataclasses import replace
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backup import snapshot
from intake import Queue
from receipts import inspect
from worker import Delivery, send_one
from test_intake import SITE, NOW, post, signed

DELIVERY = Delivery('sender@example.test', ('team@example.test',), 'private-fixture-key')


class AttemptMetadata(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'queue.db'
        self.queue = Queue(self.path)
        self.queue.accept(*signed(post()), SITE, now_ms=NOW)

    def receipt(self, content=False):
        return inspect(self.path, SITE.name, include_content=content)['receipts'][0]

    def test_routing_snapshot_commits_before_transport_and_response_is_recorded(self):
        def send(payload, key):
            row = self.receipt(True)
            self.assertEqual(row['state'], 'pending')
            self.assertEqual(row['attempt_started_at'], 100)
            self.assertIsNone(row['response_observed_at'])
            self.assertEqual(row['attempt_sender'], DELIVERY.sender)
            self.assertEqual(row['attempt_recipients'], list(DELIVERY.recipients))
            self.assertNotIn(key, self.path.read_bytes().decode(errors='ignore'))
            return 202
        with patch('worker.time.time', return_value=100):
            self.assertEqual(send_one(self.queue, SITE.name, DELIVERY, send), 'accepted')
        row = self.receipt()
        self.assertEqual(row['http_status'], 202)
        self.assertEqual(row['response_observed_at'], 100)
        self.assertNotIn('attempt_sender', row)
        self.assertNotIn('attempt_recipients', row)
        changed = replace(DELIVERY, recipients=('other@example.test',))
        self.assertEqual(send_one(self.queue, SITE.name, changed, lambda *_: self.fail('retry')), 'idle')
        self.assertEqual(self.receipt(True)['attempt_recipients'], list(DELIVERY.recipients))

    def test_crash_keeps_claim_evidence_without_invented_response(self):
        def crash(*_):
            raise RuntimeError('fixture crash')
        with self.assertRaises(RuntimeError):
            send_one(self.queue, SITE.name, DELIVERY, crash)
        row = self.receipt(True)
        self.assertEqual(row['state'], 'pending')
        self.assertIsInstance(row['attempt_started_at'], int)
        self.assertIsNone(row['response_observed_at'])
        self.assertIsNone(row['http_status'])
        self.assertEqual(row['attempt_sender'], DELIVERY.sender)
        self.assertEqual(send_one(self.queue, SITE.name, DELIVERY, lambda *_: self.fail('retry')), 'idle')

    def test_ambiguous_response_records_status_without_requeue(self):
        self.assertEqual(send_one(self.queue, SITE.name, DELIVERY, lambda *_: 503), 'pending')
        row = self.receipt()
        self.assertEqual(row['http_status'], 503)
        self.assertIsInstance(row['response_observed_at'], int)
        self.assertEqual(send_one(self.queue, SITE.name, DELIVERY, lambda *_: self.fail('retry')), 'idle')

    def test_finish_failure_does_not_record_false_acceptance(self):
        with patch('worker.finish', side_effect=sqlite3.OperationalError('fixture')):
            with self.assertRaises(sqlite3.OperationalError):
                send_one(self.queue, SITE.name, DELIVERY, lambda *_: 202)
        row = self.receipt()
        self.assertEqual(row['state'], 'pending')
        self.assertIsNone(row['http_status'])
        self.assertIsNone(row['response_observed_at'])

    def test_legacy_database_migrates_without_fabricating_attempts(self):
        old = Path(self.tmp.name) / 'legacy.db'
        with closing(sqlite3.connect(old)) as db, db:
            db.execute('CREATE TABLE jobs(site TEXT,event_key TEXT,content_hash TEXT,title TEXT,url TEXT,state TEXT,created_at INTEGER,PRIMARY KEY(site,event_key))')
            db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?)', (SITE.name, 'a'*64, 'h', 'private', 'https://example.test', 'pending', 10))
        self.assertNotIn('attempt_started_at', inspect(old, SITE.name)['receipts'][0])
        Queue(old)
        Queue(old)
        row = inspect(old, SITE.name, include_content=True)['receipts'][0]
        self.assertEqual(row['state'], 'pending')
        self.assertIsNone(row['attempt_started_at'])
        self.assertIsNone(row['attempt_sender'])
        self.assertEqual(row['event_key'], 'a'*64)

    def test_backup_retains_attempt_evidence_without_outputting_recipients(self):
        send_one(self.queue, SITE.name, DELIVERY, lambda *_: None)
        output = Path(self.tmp.name) / 'backup.db'
        report = snapshot(self.path, output)
        self.assertNotIn(DELIVERY.recipients[0], json.dumps(report))
        row = inspect(output, SITE.name, include_content=True)['receipts'][0]
        self.assertEqual(row, self.receipt(True))
        self.assertEqual(row['state'], 'pending')
        self.assertEqual(inspect(output, 'another-site')['receipts'], [])

    def test_failed_attempt_write_rolls_back_claim_before_any_send(self):
        with closing(self.queue.connect()) as db, db:
            db.execute("""CREATE TRIGGER fail_attempt BEFORE UPDATE OF attempt_started_at ON jobs
                          BEGIN SELECT RAISE(ABORT, 'fixture'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            send_one(self.queue, SITE.name, DELIVERY, lambda *_: self.fail('network before commit'))
        self.assertEqual(self.receipt()['state'], 'queued')
        self.assertIsNone(self.receipt()['attempt_started_at'])

    def test_no_response_leaves_observation_unknown(self):
        self.assertEqual(send_one(self.queue, SITE.name, DELIVERY, lambda *_: None), 'pending')
        row = self.receipt()
        self.assertIsNone(row['http_status'])
        self.assertIsNone(row['response_observed_at'])
        self.assertIsInstance(row['attempt_started_at'], int)
