"""Test-only capture wrapper around the actual receiver, inside an isolated network."""
import asyncio
import json
from pathlib import Path
import time
import uvicorn
from intake import Queue, Site
from server import create_app

queue = Queue('/data/queue.db')
site = Site('native', 'https://publication.example.test',
            (b'native-fixture-signing-secret-32-bytes',), b'native-ledger-fixture-key-32-bytes')
app = create_app(queue, {'native': site})

async def capture(scope, receive, send):
    raw = bytearray()
    started = time.monotonic()
    status = None
    async def read():
        message = await receive()
        raw.extend(message.get('body', b''))
        return message
    async def write(message):
        nonlocal status
        if message['type'] == 'http.response.start': status = message['status']
        await send(message)
    if scope['path'].startswith('/fixture/'):
        while True:
            message = await read()
            if not message.get('more_body', False): break
        mode = scope['path'].split('/')[-1]
        if mode == 'timeout': await asyncio.sleep(2.5)
        code = {'unavailable': 503, 'gone': 410, 'timeout': 202}[mode]
        await write({'type': 'http.response.start', 'status': code, 'headers': [(b'content-length', b'0')]})
        await write({'type': 'http.response.body', 'body': b''})
    else:
        await app(scope, read, write)
    with Path('/data/captures.jsonl').open('a') as output:
        output.write(json.dumps({'path': scope['path'], 'body': raw.decode(), 'headers': [(k.decode(),v.decode()) for k,v in scope['headers']],
                                 'status': status, 'elapsed': time.monotonic() - started}) + '\n')

uvicorn.run(capture, host='0.0.0.0', port=8186, http='h11', lifespan='off', log_level='critical', access_log=False)
