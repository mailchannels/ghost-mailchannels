import concurrent.futures
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from intake import Queue
from worker import Delivery, claim, send_one, transport
from test_intake import SITE, NOW, post, signed

DELIVERY=Delivery('sender@example.test',('team@example.test',),'synthetic-key')

class Worker(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=str(Path(self.tmp.name)/'queue.db');self.queue=Queue(self.path)
        self.queue.accept(*signed(post(title='<b>title</b>')),SITE,now_ms=NOW)

    def state(self):
        with sqlite3.connect(self.path) as db:return db.execute('SELECT state FROM jobs').fetchone()[0]

    def test_claim_commits_before_send_and_mapping(self):
        def send(payload,key):
            self.assertEqual(self.state(),'pending');self.assertEqual(key,DELIVERY.api_key)
            self.assertEqual(payload['personalizations'],[{'to':[{'email':'team@example.test'}]}])
            self.assertIn('&lt;b&gt;title&lt;/b&gt;',payload['content'][1]['value'])
            return 202
        self.assertEqual(send_one(self.queue,SITE.name,DELIVERY,send),'accepted')
        self.assertEqual(self.state(),'accepted')
        self.assertEqual(send_one(Queue(self.path),SITE.name,DELIVERY,lambda *_:self.fail('duplicate')),'idle')

    def test_concurrent_workers_one_send(self):
        calls=[]
        def send(*_):calls.append(1);return 202
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results=list(pool.map(lambda _:send_one(self.queue,SITE.name,DELIVERY,send),range(8)))
        self.assertEqual(len(calls),1);self.assertEqual(results.count('accepted'),1)

    def test_rejections_terminal(self):
        self.assertEqual(send_one(self.queue,SITE.name,DELIVERY,lambda *_:429),'rejected')
        self.assertEqual(send_one(self.queue,SITE.name,DELIVERY,lambda *_:self.fail('retry')),'idle')

    def test_ambiguous_outcomes_never_reclaimed(self):
        for status in [None,200,301,307,408,500,503]:
            with self.subTest(status=status):
                with sqlite3.connect(self.path) as db:db.execute("UPDATE jobs SET state='queued'")
                self.assertEqual(send_one(self.queue,SITE.name,DELIVERY,lambda *_:status),'pending')
                self.assertEqual(send_one(Queue(self.path),SITE.name,DELIVERY,lambda *_:self.fail('retry')),'idle')

    def test_crash_after_claim(self):
        claim(self.queue,SITE.name)
        self.assertEqual(send_one(Queue(self.path),SITE.name,DELIVERY,lambda *_:self.fail('retry')),'idle')

    def test_acceptance_before_receipt_update_failure(self):
        calls=[]
        def send(*_):calls.append(1);return 202
        with patch('worker.finish',side_effect=sqlite3.OperationalError('fixture')):
            with self.assertRaises(sqlite3.OperationalError):send_one(self.queue,SITE.name,DELIVERY,send)
        self.assertEqual(self.state(),'pending');self.assertEqual(len(calls),1)
        self.assertEqual(send_one(Queue(self.path),SITE.name,DELIVERY,send),'idle')
        self.assertEqual(len(calls),1)

    def test_site_isolation(self):
        self.assertEqual(send_one(self.queue,'other-site',DELIVERY,lambda *_:self.fail('wrong-site')),'idle')

    def test_invalid_config(self):
        with self.assertRaises(ValueError):Delivery('bad',('team@example.test',),'key')
        with self.assertRaises(ValueError):Delivery('sender@example.test',(),'key')

    def test_transport_fixed_endpoint_and_no_response_body_read(self):
        class Response:
            status=202
            def __enter__(self):return self
            def __exit__(self,*_):pass
            def read(self):raise AssertionError('Do not read response body')
        class Opener:
            def open(self,req,timeout):
                assert req.full_url=='https://api.mailchannels.net/tx/v1/send'
                assert req.get_header('X-api-key')=='synthetic-key'
                assert timeout==10
                return Response()
        with patch('worker.urllib.request.build_opener',return_value=Opener()):
            self.assertEqual(transport({},'synthetic-key'),202)

if __name__=='__main__':unittest.main()
