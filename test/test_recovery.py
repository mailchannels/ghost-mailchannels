"""Recovery never contacts a provider and cannot race a cooperating worker."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backup import snapshot
from coordination import queue_lock
from intake import Queue
from receipts import inspect
from recover import resolve, AUDIT_SCHEMA
from worker import Delivery, send_one
from test_intake import SITE, NOW, post, signed

ROOT = Path(__file__).resolve().parents[1]
DELIVERY = Delivery('sender@example.test', ('team@example.test',), 'fixture-key')


class Recovery(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'queue.db'
        self.queue = Queue(self.path)
        self.key = self.queue.accept(*signed(post()), SITE, now_ms=NOW)['event_key']
        send_one(self.queue, SITE.name, DELIVERY, lambda *_: None)

    def resolve(self, **changes):
        values = dict(database=self.path, site=SITE.name, event_key=self.key,
                      outcome='accepted', actor='operator-1', evidence_reference='CASE-123', confirmed=True)
        values.update(changes)
        return resolve(**values)

    def test_atomic_resolution_audit_and_no_resend(self):
        before = inspect(self.path, SITE.name, include_content=True)['receipts'][0]
        result = self.resolve()
        after = inspect(self.path, SITE.name, include_content=True)['receipts'][0]
        self.assertEqual(after['state'], 'accepted')
        self.assertIsNone(after['http_status'])
        self.assertIsNone(after['response_observed_at'])
        self.assertEqual(after['attempt_started_at'], before['attempt_started_at'])
        self.assertEqual(after['operator_resolution']['actor'], 'operator-1')
        self.assertEqual(after['operator_resolution']['evidence_reference'], 'CASE-123')
        self.assertEqual(after['operator_resolution']['id'], result['resolution_id'])
        plain = inspect(self.path, SITE.name)['receipts'][0]['operator_resolution']
        self.assertNotIn('actor', plain)
        self.assertNotIn('evidence_reference', plain)
        self.assertEqual(send_one(self.queue, SITE.name, DELIVERY, lambda *_: self.fail('resend')), 'idle')
        with self.assertRaises(ValueError): self.resolve()

    def test_rejected_resolution_and_backup_preserve_audit(self):
        self.resolve(outcome='rejected')
        output = Path(self.tmp.name) / 'snapshot.db'
        snapshot(self.path, output)
        self.assertEqual(inspect(output, SITE.name, include_content=True)['receipts'],
                         inspect(self.path, SITE.name, include_content=True)['receipts'])
        self.assertEqual(send_one(self.queue, SITE.name, DELIVERY, lambda *_: self.fail('resend')), 'idle')

    def test_requires_evidence_valid_identity_scope_and_pending(self):
        for changes in ({'confirmed':False}, {'outcome':'queued'}, {'site':'other'},
                        {'event_key':'f'*64}, {'actor':''}, {'evidence_reference':'private text\nsecret'}):
            with self.subTest(changes=changes), self.assertRaises(ValueError): self.resolve(**changes)
        for state in ('queued', 'accepted', 'rejected'):
            with closing(self.queue.connect()) as db, db:
                db.execute('UPDATE jobs SET state=?', (state,))
            with self.assertRaises(ValueError): self.resolve()

    def test_audit_write_failure_rolls_back_without_state_change(self):
        with closing(self.queue.connect()) as db, db:
            db.execute(AUDIT_SCHEMA)
            db.execute("CREATE TRIGGER fail_audit BEFORE INSERT ON resolutions BEGIN SELECT RAISE(ABORT,'fixture'); END")
        with self.assertRaises(sqlite3.IntegrityError): self.resolve()
        self.assertEqual(inspect(self.path, SITE.name)['receipts'][0]['state'], 'pending')

    def test_state_write_failure_rolls_back_audit(self):
        with closing(self.queue.connect()) as db, db:
            db.execute(AUDIT_SCHEMA)
            db.execute("CREATE TRIGGER fail_state BEFORE UPDATE OF state ON jobs BEGIN SELECT RAISE(ABORT,'fixture'); END")
        with self.assertRaises(sqlite3.IntegrityError): self.resolve()
        with closing(self.queue.connect()) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM resolutions').fetchone()[0], 0)

    def test_live_worker_blocks_resolution_and_recovery_blocks_worker(self):
        # A separate process demonstrates OS-lock coordination rather than a Python mutex.
        script = "from coordination import queue_lock; import sys;\nwith queue_lock(sys.argv[1]):\n print('locked',flush=True)\n sys.stdin.read()"
        child = subprocess.Popen([sys.executable, '-c', script, str(self.path)], cwd=ROOT,
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(child.stdout.readline().strip(), 'locked')
            with self.assertRaises(BlockingIOError): self.resolve()
        finally:
            child.communicate('', timeout=5)
        self.assertEqual(child.returncode, 0)
        with queue_lock(self.path, exclusive=True):
            with self.assertRaises(BlockingIOError):
                send_one(self.queue, SITE.name, DELIVERY, lambda *_: self.fail('send under recovery'))
        self.resolve()

    def test_worker_holds_lock_through_transport_and_failure_releases_it(self):
        with closing(self.queue.connect()) as db, db: db.execute("UPDATE jobs SET state='queued'")
        def sending(*_):
            with self.assertRaises(BlockingIOError): self.resolve()
            raise RuntimeError('transport fixture')
        with self.assertRaises(RuntimeError): send_one(self.queue, SITE.name, DELIVERY, sending)
        self.resolve()

    def test_missing_database_and_symlink_lock_refused(self):
        missing = Path(self.tmp.name) / 'missing.db'
        with self.assertRaises(FileNotFoundError): self.resolve(database=missing)
        self.assertFalse(missing.exists())
        lock = Path(str(self.path) + '.workers.lock')
        lock.unlink()
        target = Path(self.tmp.name) / 'target'
        target.write_text('preserve')
        lock.symlink_to(target)
        with self.assertRaises(OSError): self.resolve()
        self.assertEqual(target.read_text(), 'preserve')

    def test_cli_refuses_without_confirmation_and_sanitizes_errors(self):
        args = [sys.executable, str(ROOT/'recover.py'), '--database', str(self.path), '--site', SITE.name,
                '--receipt', self.key, '--actor', 'operator-1', '--evidence-reference', 'CASE-123', '--outcome', 'accepted']
        refusal = subprocess.run(args, capture_output=True, text=True)
        self.assertEqual(refusal.returncode, 1)
        self.assertNotIn(str(self.path), refusal.stderr)
        success = subprocess.run(args + ['--confirm-provider-evidence'], capture_output=True, text=True)
        self.assertEqual(success.returncode, 0, success.stderr)
        self.assertEqual(json.loads(success.stdout)['outcome'], 'accepted')
        self.assertNotIn('CASE-123', success.stdout)
