"""Disposable native Ghost fixture; no internet egress, provider calls or real credentials.
Run with the same Python environment as the receiver. Docker image must already be pulled.
"""
from pathlib import Path
import json
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from intake import Queue, Site

IMAGE = 'ghost:6.67.0@sha256:9482099b1c8700764dc3d38c293e09c16d70f8bacc14af589c47d90b4bebd017'
SECRET = b'native-fixture-signing-secret-32-bytes'

def docker(*args):
    return subprocess.run(['docker', *args], capture_output=True, text=True, check=True, timeout=90).stdout.strip()

def main():
    scenario = sys.argv[1] if len(sys.argv) > 1 else 'events'
    assert scenario in ('events', 'routes', 'failures', 'failures-default')
    name = 'visibility-ghost-' + uuid.uuid4().hex[:10]
    network = name + '-net'
    docker('network', 'create', '--internal', network)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            queue = Queue(str(Path(tmp) / 'queue.db'))
            site = Site('native', 'https://publication.example.test', (SECRET,), b'native-ledger-fixture-key-32-bytes')
            docker('run', '-d', '--name', name + '-receiver', '--network', network,
                   '--network-alias', 'receiver.example.test', '-v', tmp + ':/data', 'visibility-ghost-receiver:test')
            route_args = []
            if scenario == 'routes':
                routes = Path(tmp) / 'routes.yaml'
                routes.write_text("""routes:
collections:
  /news/:
    permalink: /news/{slug}/
    template: index
    filter: tag:news
  /archive/:
    permalink: /archive/{slug}/
    template: index
    filter: tag:-news
taxonomies:
  tag: /tag/{slug}/
  author: /author/{slug}/
""")
                route_args = ['-v', str(routes) + ':/var/lib/ghost/content/settings/routes.yaml']
            docker('run', '-d', '--name', name, '--network', network, *route_args,
                   '-e', 'NODE_ENV=development', '-e', 'url=https://publication.example.test',
                   '-e', 'security__allowWebhookInternalIPs=' + ('false' if scenario == 'failures-default' else 'true'), '-e', 'database__client=sqlite3',
                   '-e', 'database__connection__filename=/var/lib/ghost/content/data/test.db', IMAGE)
            for _ in range(60):
                if 'Ghost booted' in docker('logs', name): break
                time.sleep(0.5)
            else: raise AssertionError('Ghost failed to boot')
            bootstrap = """const m=require('./core/server/models');m.Integration.add({name:'Native fixture',api_keys:[{type:'admin'}]},{context:{internal:true}}).then(x=>{require('fs').writeFileSync('/tmp/fixture-key.json',JSON.stringify(x.toJSON()),{mode:384});process.exit(0)}).catch(e=>{console.error(e.message);process.exit(1)})"""
            docker('exec', '-w', '/var/lib/ghost/current', name, 'node', '-e', bootstrap)
            docker('cp', str(Path(__file__).with_name('events.js')), name + ':/tmp/events.js')
            result = docker('exec', name, 'node', '/tmp/events.js', 'http://receiver.example.test:8186/hooks/native', 'failures' if scenario.startswith('failures') else scenario)
            if not (Path(tmp) / 'captures.jsonl').exists():
                print(docker('logs', name))
                print(docker('logs', name + '-receiver'))
            captures = [json.loads(line) for line in (Path(tmp) / 'captures.jsonl').read_text().splitlines()]
            if scenario.startswith('failures'):
                from collections import Counter
                counts = Counter(c['path'] for c in captures)
                expected_gone = 2 if scenario == 'failures-default' else 1
                assert counts == {'/fixture/unavailable': 2, '/fixture/timeout': 2, '/fixture/gone': expected_gone}, counts
                receipts = json.loads(result)
                for phase in ['first', 'second']:
                    hooks = {h['name']: h for h in receipts[phase]}
                    expected_names = {'unavailable', 'timeout', 'gone'} if scenario == 'failures-default' else {'unavailable', 'timeout'}
                    assert set(hooks) == expected_names, hooks
                    if scenario == 'failures-default':
                        assert hooks['unavailable']['status'] is None, hooks
                        assert hooks['gone']['status'] is None, hooks
                    else:
                        assert str(hooks['unavailable']['status']) == '503', hooks
                    assert 'ETIMEDOUT' in hooks['timeout']['error'], hooks
                with sqlite3.connect(queue.path) as db:
                    assert db.execute('SELECT count(*) FROM jobs').fetchone()[0] == 0
                print(json.dumps({'image': IMAGE, 'transport': 'request-external' if scenario == 'failures-default' else '@tryghost/request', 'scenario': receipts, 'request_counts': dict(counts),
                    'observation': 'Two publications; terminal receipts checked after each. One request per retained hook per publication;410 removal differs by transport (see counts and receipts).',
                    'queued_jobs': 0, 'network': 'isolated Docker; fixture responses only'}, indent=2))
                return
            assert len(captures) == 2, f'Expected publish and republish only, got {len(captures)}'
            assert all(c['status'] == 202 for c in captures), [c['status'] for c in captures]
            assert all(c['elapsed'] < 2 for c in captures)
            posts = [json.loads(c['body'])['post']['current'] for c in captures]
            if scenario == 'routes':
                expected_urls = ['https://publication.example.test/news/native-route/',
                                 'https://publication.example.test/archive/moved-route/']
                assert [p['url'] for p in posts] == expected_urls, [p['url'] for p in posts]
                assert all(p['canonical_url'] == 'https://external.example.test/original/' for p in posts)
                assert [p['url'] for p in json.loads(result)['versions']] == expected_urls
            assert posts[0]['id'] == posts[1]['id']
            assert posts[0]['updated_at'] != posts[1]['updated_at']
            for capture in captures:
                signatures = [v for k,v in capture['headers'] if k == 'x-ghost-signature']
                assert queue.accept(capture['body'].encode(), signatures, site)['outcome'] == 'duplicate'
            with sqlite3.connect(queue.path) as db:
                assert db.execute('SELECT count(*) FROM jobs').fetchone()[0] == 2
            print(json.dumps({'image': IMAGE, 'scenario': json.loads(result), 'events': [
                {'event': json.loads(c['body'])['event'], 'status': c['status'],
                 'receiver_seconds': round(c['elapsed'], 4), 'title': p['title'],
                 'url': p['url'], 'published_at': p['published_at'], 'updated_at': p['updated_at']}
                for c,p in zip(captures,posts)], 'queued_jobs': 2, 'replay': 'both duplicate',
                'network': 'internal Docker network; no provider transport invoked'}, indent=2))
    finally:
        subprocess.run(['docker', 'rm', '-fv', name + '-receiver'], capture_output=True)
        subprocess.run(['docker', 'rm', '-fv', name], capture_output=True)
        subprocess.run(['docker', 'network', 'rm', network], capture_output=True)

if __name__ == '__main__': main()
