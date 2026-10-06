import hashlib
import hmac
import http.client
import json
from pathlib import Path
import socket
import sqlite3
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import uvicorn
from intake import Queue, Site
from server import create_app
from test_intake import SECRET, SITE, post


class Receiver(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.path=str(Path(self.tmp.name)/'queue.db')
        self.queue=Queue(self.path)
        self.app=create_app(self.queue,{SITE.name:SITE},read_timeout=0.2)
        sock=socket.socket();sock.bind(('127.0.0.1',0));self.port=sock.getsockname()[1]
        config=uvicorn.Config(self.app,http='h11',lifespan='off',log_level='critical',access_log=False)
        self.server=uvicorn.Server(config)
        self.thread=threading.Thread(target=self.server.run,kwargs={'sockets':[sock]},daemon=True)
        self.thread.start()
        for _ in range(100):
            if self.server.started:break
            time.sleep(0.01)
        self.assertTrue(self.server.started)
        self.addCleanup(self.stop)

    def stop(self):
        self.server.should_exit=True;self.thread.join(3)
        self.tmp.cleanup()

    def body_headers(self):
        raw=json.dumps(post()).encode();stamp=str(int(time.time()*1000))
        signature='sha256='+hmac.new(SECRET,raw+stamp.encode(),hashlib.sha256).hexdigest()+', t='+stamp
        return raw,{'Content-Type':'application/json','X-Ghost-Signature':signature,'Content-Length':str(len(raw))}

    def request(self,method='POST',path='/hooks/site-a',body=None,headers=None):
        raw,default=self.body_headers()
        client=http.client.HTTPConnection('127.0.0.1',self.port,timeout=3)
        try:
            client.request(method,path,raw if body is None else body,default if headers is None else headers)
            result=client.getresponse();data=result.read()
            self.assertNotIn(SECRET,data)
            self.assertNotEqual(result.status,410)
            return result.status,data
        finally: client.close()

    def count(self):
        with sqlite3.connect(self.path) as db:return db.execute('SELECT count(*) FROM jobs').fetchone()[0]

    def test_ack_observes_commit_and_duplicate(self):
        self.assertEqual(self.request()[0],202);self.assertEqual(self.count(),1)
        self.assertEqual(self.request()[0],202);self.assertEqual(self.count(),1)

    def test_unknown_site_method_and_query(self):
        self.assertEqual(self.request(path='/hooks/other')[0],404)
        self.assertEqual(self.request(method='GET')[0],405)
        self.assertEqual(self.request(path='/hooks/site-a?token=x')[0],400)
        self.assertEqual(self.count(),0)

    def test_content_type_encoding_missing_signature_and_tamper(self):
        raw,headers=self.body_headers()
        for changes,status in [({'Content-Type':'text/plain'},415),({'Content-Encoding':'gzip'},400),({'X-Ghost-Signature':''},400)]:
            self.assertEqual(self.request(body=raw,headers={**headers,**changes})[0],status)
        del headers['X-Ghost-Signature'];self.assertEqual(self.request(body=raw,headers=headers)[0],401)
        raw,headers=self.body_headers();self.assertEqual(self.request(body=raw.replace(b'Published',b'published'),headers=headers)[0],400)
        self.assertEqual(self.count(),0)

    def wire(self,request,shutdown=False):
        with socket.create_connection(('127.0.0.1',self.port),timeout=3) as client:
            client.sendall(request)
            if shutdown:client.shutdown(socket.SHUT_WR)
            reply=client.recv(4096)
        return reply

    def test_duplicate_signature(self):
        raw,headers=self.body_headers()
        wire=b'POST /hooks/site-a HTTP/1.1\r\nHost: localhost\r\n'
        wire+=b''.join(f'{k}: {v}\r\n'.encode() for k,v in headers.items())
        wire+=f'X-Ghost-Signature: {headers["X-Ghost-Signature"]}\r\n\r\n'.encode()+raw
        self.assertIn(b' 401 ',self.wire(wire));self.assertEqual(self.count(),0)

    def test_oversized_declared_body_rejected_before_upload(self):
        wire=b'POST /hooks/site-a HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\nX-Ghost-Signature: x\r\nContent-Length: 1048577\r\n\r\n'
        self.assertIn(b' 413 ',self.wire(wire));self.assertEqual(self.count(),0)

    def test_slow_partial_body_times_out(self):
        wire=b'POST /hooks/site-a HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\nX-Ghost-Signature: x\r\nContent-Length: 100\r\n\r\n{'
        self.assertIn(b' 408 ',self.wire(wire));self.assertEqual(self.count(),0)

    def test_disconnected_partial_body_not_queued(self):
        wire=b'POST /hooks/site-a HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\nX-Ghost-Signature: x\r\nContent-Length: 100\r\n\r\n{'
        self.wire(wire,True);self.assertEqual(self.count(),0)

    def test_capacity_and_lock_failure_are_503_not_ack(self):
        self.queue.capacity=0
        self.assertEqual(self.request()[0],503);self.assertEqual(self.count(),0)
        self.queue.capacity=100
        db=sqlite3.connect(self.path);db.execute('BEGIN IMMEDIATE')
        try:self.assertEqual(self.request()[0],503)
        finally:db.rollback();db.close()
        self.assertEqual(self.count(),0)
        self.assertEqual(self.request()[0],202)

    def test_secret_reuse_configuration_rejected(self):
        other=Site('site-b',SITE.origin,(SECRET,),SITE.ledger_key)
        with self.assertRaises(ValueError):create_app(self.queue,{SITE.name:SITE,'site-b':other})

if __name__=='__main__':unittest.main()
