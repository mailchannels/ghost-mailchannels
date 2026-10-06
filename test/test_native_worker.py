"""Real TLS and SIGKILL tests. Only the fixed provider socket is redirected to loopback."""
import http.server
import json
import os
from pathlib import Path
import select
import socket
import sqlite3
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from intake import Queue
from worker import transport
from test_intake import SITE, NOW, post, signed

ROOT=Path(__file__).resolve().parents[1]

class Provider(http.server.BaseHTTPRequestHandler):
    def log_message(self,*args):pass
    def do_POST(self):
        payload=self.rfile.read(int(self.headers['Content-Length']))
        self.server.requests.append((self.path,self.headers.get('X-Api-Key'),json.loads(payload)))
        mode=self.server.mode
        if mode=='disconnect':
            self.connection.shutdown(socket.SHUT_RDWR);self.connection.close();return
        if mode=='timeout':
            time.sleep(10.4);return
        self.send_response(mode)
        if 300<=mode<400:self.send_header('Location','https://untrusted.example.test/collect')
        self.send_header('Content-Length','0');self.end_headers()


CHILD=r'''
import os,socket,sys,time
from unittest.mock import patch
import worker
from intake import Queue
original_socket=socket.create_connection
port=int(sys.argv[2]);mode=sys.argv[3]
def redirect(address,*args,**kwargs):
    assert address == ('api.mailchannels.net',443), 'unexpected outbound destination'
    return original_socket(('127.0.0.1',port),*args,**kwargs)
def barrier():
    print('BARRIER',flush=True)
    while True:time.sleep(1)
original_finish=worker.finish
def finish(*args):
    if mode=='before_finish':barrier()
    original_finish(*args)
    if mode=='after_finish':barrier()
def send(payload,key):
    if mode=='after_claim':barrier()
    return worker.transport(payload,key)
worker.finish=finish
with patch('socket.create_connection',redirect):
    print(worker.send_one(Queue(sys.argv[1]),'site-a',worker.Delivery('sender@example.test',('team@example.test',),'synthetic-key'),send),flush=True)
'''

class NativeWorker(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp=tempfile.TemporaryDirectory();cls.root=Path(cls.tmp.name)
        cls.cert=cls.root/'server.pem';cls.key=cls.root/'server.key'
        subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','1',
                        '-keyout',str(cls.key),'-out',str(cls.cert),'-subj','/CN=api.mailchannels.net',
                        '-addext','subjectAltName=DNS:api.mailchannels.net'],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        cls.server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Provider)
        context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);context.load_cert_chain(cls.cert,cls.key)
        cls.server.socket=context.wrap_socket(cls.server.socket,server_side=True)
        cls.port=cls.server.server_port
        cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True);cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown();cls.server.server_close();cls.thread.join();cls.tmp.cleanup()

    def setUp(self):
        self.server.mode=202;self.server.requests=[]
        self.socket=socket.create_connection

    def route(self,address,*args,**kwargs):
        self.assertEqual(address,('api.mailchannels.net',443))
        return self.socket(('127.0.0.1',self.port),*args,**kwargs)

    def request(self,trusted=True):
        env={'SSL_CERT_FILE':str(self.cert)} if trusted else {'SSL_CERT_FILE':'/missing-test-ca','SSL_CERT_DIR':'/missing-test-ca-dir'}
        with patch.dict(os.environ,env),patch('socket.create_connection',self.route):
            return transport({'synthetic':'payload'},'synthetic-key')

    def test_verified_tls_request_and_empty202(self):
        self.assertEqual(self.request(),202)
        self.assertEqual(self.server.requests,[('/tx/v1/send','synthetic-key',{'synthetic':'payload'})])

    def test_untrusted_certificate_fails_before_request(self):
        self.assertIsNone(self.request(False));self.assertEqual(self.server.requests,[])

    def test_redirects_not_followed(self):
        for status in [301,302,303,307,308]:
            with self.subTest(status=status):
                self.server.mode=status;self.server.requests=[]
                self.assertEqual(self.request(),status);self.assertEqual(len(self.server.requests),1)

    def test_rejection_and_server_error_are_single_requests(self):
        for status in [400,401,429,500,503]:
            self.server.mode=status;self.server.requests=[]
            self.assertEqual(self.request(),status);self.assertEqual(len(self.server.requests),1)

    def test_disconnect_after_provider_receives_request(self):
        self.server.mode='disconnect'
        self.assertIsNone(self.request());self.assertEqual(len(self.server.requests),1)

    def test_actual_socket_timeout(self):
        self.server.mode='timeout';started=time.monotonic()
        self.assertIsNone(self.request());self.assertGreaterEqual(time.monotonic()-started,9.5)
        self.assertEqual(len(self.server.requests),1)

    def test_sigkill_boundaries_and_fresh_process_restart(self):
        for mode,expected,calls in [('after_claim','pending',0),('before_finish','pending',1),('after_finish','accepted',1)]:
            with self.subTest(mode=mode),tempfile.TemporaryDirectory() as directory:
                dbpath=str(Path(directory)/'queue.db');queue=Queue(dbpath)
                queue.accept(*signed(post()),SITE,now_ms=NOW)
                self.server.requests=[]
                env={**os.environ,'SSL_CERT_FILE':str(self.cert),'PYTHONPATH':str(ROOT)}
                command=[sys.executable,'-c',CHILD,dbpath,str(self.port),mode]
                child=subprocess.Popen(command,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,env=env)
                try:
                    ready,_,_=select.select([child.stdout],[],[],8)
                    self.assertTrue(ready,'worker failed to reach barrier')
                    self.assertEqual(child.stdout.readline().strip(),'BARRIER')
                    with sqlite3.connect(dbpath) as db:self.assertEqual(db.execute('SELECT state FROM jobs').fetchone()[0],expected)
                    self.assertEqual(len(self.server.requests),calls)
                    child.kill();child.wait(timeout=3);self.assertEqual(child.returncode,-9)
                finally:
                    if child.poll() is None:child.kill();child.wait()
                    child.stdout.close();child.stderr.close()
                command[-1]='normal'
                restarted=subprocess.run(command,env=env,text=True,capture_output=True,timeout=5,check=True)
                self.assertEqual(restarted.stdout.strip(),'idle');self.assertEqual(len(self.server.requests),calls)
                with sqlite3.connect(dbpath) as db:self.assertEqual(db.execute('SELECT state FROM jobs').fetchone()[0],expected)

if __name__=='__main__':unittest.main(verbosity=2)
