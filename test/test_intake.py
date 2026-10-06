import concurrent.futures
import hashlib
import hmac
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from intake import InvalidEvent, Queue, Site, verify

NOW = 1791267000000
SECRET = b'synthetic-ghost-signing-secret-32-bytes'
SITE = Site('site-a', 'https://publication.example.test', (SECRET,), b'ledger-key-32-bytes-synthetic-only!')


def post(**kwargs):
    current = dict(id='a'*24, status='published', title='Published title', url=SITE.origin+'/new/',
                   published_at='2026-10-06T05:00:00.000Z', updated_at='2026-10-06T05:00:00.000Z')
    current.update(kwargs)
    return {'event': 'post.published', 'post': {'current': current, 'previous': {'status': 'draft'}}}


def signed(data, timestamp=NOW, secret=SECRET):
    raw = data if isinstance(data, bytes) else json.dumps(data).encode()
    digest = hmac.new(secret, raw+str(timestamp).encode(), hashlib.sha256).hexdigest()
    return raw, [f'sha256={digest}, t={timestamp}']


class Intake(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = str(Path(self.tmp.name)/'queue.db')
        self.queue = Queue(self.path)

    def accept(self, data, timestamp=NOW, site=SITE):
        return self.queue.accept(*signed(data, timestamp), site, now_ms=NOW)

    def test_durable_duplicate_with_new_signature_timestamp(self):
        first = self.accept(post())
        self.queue = Queue(self.path)
        second = self.accept(post(), NOW+1000)
        self.assertEqual(first['event_key'], second['event_key'])
        self.assertEqual(second['outcome'], 'duplicate')

    def test_concurrent_claims(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: self.accept(post()), range(8)))
        self.assertEqual([r['outcome'] for r in results].count('queued'), 1)

    def test_republish_and_out_of_order_versions(self):
        self.accept(post(updated_at='2026-10-06T06:00:00Z'))
        self.assertEqual(self.accept(post())['outcome'], 'queued')
        self.assertEqual(self.accept(post())['outcome'], 'duplicate')

    def test_equivalent_timestamp_and_json_serialization(self):
        self.accept(post())
        data=post(updated_at='2026-10-06T05:00:00+00:00')
        raw=json.dumps(data, separators=(',', ':')).encode()
        self.assertEqual(self.accept(raw)['outcome'], 'duplicate')

    def test_conflicting_same_version(self):
        self.accept(post())
        with self.assertRaises(InvalidEvent): self.accept(post(title='Changed without version change'))

    def test_capacity_does_not_reject_duplicate(self):
        self.queue=Queue(self.path,capacity=1)
        self.accept(post())
        self.assertEqual(self.accept(post())['outcome'],'duplicate')
        with self.assertRaises(RuntimeError): self.accept(post(id='b'*24))

    def test_wrong_secret_and_cross_site(self):
        other=Site('site-b',SITE.origin,(b'other-secret-32-bytes-not-shared!!',),SITE.ledger_key)
        with self.assertRaises(InvalidEvent): self.accept(post(),site=other)

    def test_secret_rotation(self):
        rotated=Site(SITE.name,SITE.origin,(b'new-secret-32-bytes-synthetic-only',SECRET),SITE.ledger_key)
        self.assertEqual(self.accept(post(),site=rotated)['outcome'],'queued')

    def test_site_partition(self):
        other=Site('site-b',SITE.origin,SITE.secrets,SITE.ledger_key)
        self.assertNotEqual(self.accept(post())['event_key'],self.accept(post(),site=other)['event_key'])

    def test_signature_tamper_missing_duplicate_and_freshness(self):
        raw,headers=signed(post())
        for body,hs in [(raw+b' ',headers),(raw,[]),(raw,headers*2), (raw,[headers[0].replace(str(NOW),str(NOW+1))])]:
            with self.subTest(hs=hs), self.assertRaises(InvalidEvent): verify(body,hs,SITE,NOW)
        for ts in [NOW-300001,NOW+300001]:
            with self.assertRaises(InvalidEvent): self.accept(post(),timestamp=ts)

    def test_invalid_payloads(self):
        for data in [post(url='https://elsewhere.example.test/a'),post(url=SITE.origin+'/x\n'),post(id='bad'),
                     post(status='draft'),post(title='bad\nheader'),post(updated_at='2026-10-05T05:00:00Z'),
                     post(updated_at='2026-10-06T05:00:00'), {'event':'post.published.edited'},
                     b'{"event":"a","event":"b"}',b'\xff',b'[]']:
            with self.subTest(data=data), self.assertRaises(InvalidEvent): self.accept(data)

    def test_size_and_config(self):
        with self.assertRaises(InvalidEvent): verify(b'x'*1048577,[],SITE,NOW)
        with self.assertRaises(ValueError): Site('a','http://site.test',(SECRET,),SITE.ledger_key)

    def test_no_raw_post_or_secret_persisted(self):
        data=post();data['post']['current']['html']='private body must not persist'
        self.accept(data)
        db=sqlite3.connect(self.path)
        try:
            rows=str(db.execute('SELECT * FROM jobs').fetchall())
            self.assertNotIn('private body',rows);self.assertNotIn(SECRET.decode(),rows)
            self.assertIn('queued',rows)
        finally: db.close()

if __name__=='__main__': unittest.main(verbosity=2)
