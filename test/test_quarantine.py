from contextlib import closing
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backup import snapshot
from intake import Queue
from quarantine import quarantine, AUDIT_SCHEMA
from receipts import inspect
from recover import resolve
from worker import Delivery, send_one
from test_intake import SITE, NOW, post, signed
ROOT=Path(__file__).resolve().parents[1]
DELIVERY=Delivery('sender@example.test',('team@example.test',),'fixture-key')

class Quarantine(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name); self.path=self.root/'queue.db'; self.queue=Queue(self.path)
        self.key=self.queue.accept(*signed(post()),SITE,now_ms=NOW)['event_key']

    def quarantine(self, **changes):
        args=dict(database=self.path,site=SITE.name,actor='operator-1',evidence_reference='RESTORE-123',confirmed=True)
        args.update(changes); return quarantine(**args)

    def test_actual_restore_prevents_resend_and_preserves_two_audits(self):
        saved=self.root/'backup.db'; snapshot(self.path,saved)
        self.assertEqual(send_one(self.queue,SITE.name,DELIVERY,lambda *_:202),'accepted')
        restored=self.root/'restored.db'; shutil.copyfile(saved,restored)
        self.assertEqual(inspect(restored,SITE.name)['counts']['queued'],1)
        self.assertEqual(self.quarantine(database=restored)['quarantined'],1)
        queue=Queue(restored)
        self.assertEqual(send_one(queue,SITE.name,DELIVERY,lambda *_:self.fail('resend')),'idle')
        self.assertEqual(queue.accept(*signed(post()),SITE,now_ms=NOW)['outcome'],'duplicate')
        row=inspect(restored,SITE.name,include_content=True)['receipts'][0]
        self.assertEqual(row['state'],'pending')
        for name in ('attempt_started_at','response_observed_at','http_status','attempt_sender','attempt_recipients'):
            self.assertIsNone(row[name])
        self.assertEqual(row['restore_quarantine']['actor'],'operator-1')
        self.assertNotIn('actor',inspect(restored,SITE.name)['receipts'][0]['restore_quarantine'])
        resolve(restored,SITE.name,self.key,'accepted','operator-1','PROVIDER-123',confirmed=True)
        second=self.root/'after.db'; snapshot(restored,second)
        row=inspect(second,SITE.name,include_content=True)['receipts'][0]
        self.assertEqual(row['state'],'accepted')
        self.assertEqual(row['restore_quarantine']['evidence_reference'],'RESTORE-123')
        self.assertEqual(row['operator_resolution']['evidence_reference'],'PROVIDER-123')

    def test_missing_post_snapshot_event_still_needs_reconciliation(self):
        saved=self.root/'backup.db'; snapshot(self.path,saved)
        later=post(id='b'*24); self.queue.accept(*signed(later),SITE,now_ms=NOW)
        self.quarantine(database=saved)
        # Quarantine cannot invent missing history: intake must remain stopped.
        self.assertEqual(Queue(saved).accept(*signed(later),SITE,now_ms=NOW)['outcome'],'queued')

    def test_bounded_batches_scope_states_and_repeated_execution(self):
        with closing(self.queue.connect()) as db, db:
            for i,(site,state) in enumerate([(SITE.name,'queued'),(SITE.name,'pending'),(SITE.name,'accepted'),(SITE.name,'rejected'),('other','queued')]):
                db.execute('INSERT INTO jobs(site,event_key,content_hash,title,url,state,created_at) VALUES(?,?,?,?,?,?,?)',
                           (site,f'{i:064x}','hash','Private','https://private.test',state,1))
        self.assertEqual(self.quarantine(limit=1)['remaining_queued'],1)
        self.assertEqual(self.quarantine(limit=1)['remaining_queued'],0)
        repeated=self.quarantine()
        self.assertEqual((repeated['quarantined'],repeated['queued_by_site']),(0,{'other':1}))
        self.assertEqual(inspect(self.path,'other')['counts']['queued'],1)
        self.assertEqual(inspect(self.path,SITE.name)['counts'],dict(queued=0,pending=3,accepted=1,rejected=1))
        with closing(self.queue.connect()) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM restore_quarantines').fetchone()[0],2)

    def test_audit_and_state_failure_roll_back(self):
        for table,operation in [('jobs','UPDATE'),('restore_quarantines','INSERT')]:
            with self.subTest(table=table):
                with closing(self.queue.connect()) as db, db:
                    db.execute(AUDIT_SCHEMA)
                    db.execute(f"CREATE TRIGGER fail BEFORE {operation} ON {table} BEGIN SELECT RAISE(ABORT,'fixture'); END")
                with self.assertRaises(sqlite3.IntegrityError): self.quarantine()
                self.assertEqual(inspect(self.path,SITE.name)['counts']['queued'],1)
                with closing(self.queue.connect()) as db, db:
                    self.assertEqual(db.execute('SELECT count(*) FROM restore_quarantines').fetchone()[0],0)
                    db.execute('DROP TRIGGER fail')

    def test_invalid_scope_confirmation_and_missing_database(self):
        for changes in [{'confirmed':False},{'site':'other'},{'actor':''},{'evidence_reference':'secret\n'}, {'limit':0},{'limit':501},{'limit':True}]:
            with self.subTest(changes=changes),self.assertRaises(ValueError): self.quarantine(**changes)
        missing=self.root/'missing.db'
        with self.assertRaises(FileNotFoundError): self.quarantine(database=missing)
        self.assertFalse(missing.exists())
        self.assertEqual(inspect(self.path,SITE.name)['counts']['queued'],1)

    def test_active_worker_in_another_process_excludes_quarantine(self):
        script="from coordination import queue_lock; import sys;\nwith queue_lock(sys.argv[1]):\n print('locked',flush=True)\n sys.stdin.read()"
        child=subprocess.Popen([sys.executable,'-c',script,str(self.path)],cwd=ROOT,stdin=subprocess.PIPE,stdout=subprocess.PIPE,text=True)
        try:
            self.assertEqual(child.stdout.readline().strip(),'locked')
            with self.assertRaises(BlockingIOError): self.quarantine()
        finally: child.communicate('',timeout=5)
        self.assertEqual(child.returncode,0)
        self.assertEqual(self.quarantine()['quarantined'],1)

    def test_cli_requires_confirmation_and_omits_private_values(self):
        command=[sys.executable,str(ROOT/'quarantine.py'),'--database',str(self.path),'--site',SITE.name,'--actor','operator-1','--evidence-reference','RESTORE-123']
        refused=subprocess.run(command,capture_output=True,text=True)
        self.assertEqual(refused.returncode,1); self.assertNotIn(str(self.root),refused.stderr)
        result=subprocess.run(command+['--confirm-services-stopped'],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(json.loads(result.stdout)['quarantined'],1)
        for private in ('operator-1','RESTORE-123','Published title'): self.assertNotIn(private,result.stdout)
