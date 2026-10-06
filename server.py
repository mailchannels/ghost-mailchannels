"""ASGI Ghost webhook boundary. Run behind a maintained HTTPS reverse proxy."""
import asyncio
import json
import os
import re
import sqlite3
from pathlib import Path

from intake import InvalidEvent, Queue, Site

MAX_BODY = 1_048_576


def create_app(queue, sites, read_timeout=0.75):
    """Bind trusted connection configuration; the request never supplies secrets or site origins."""
    sites = dict(sites)
    if any(not re.fullmatch(r'[a-z0-9-]{1,64}', key) or key != site.name for key, site in sites.items()):
        raise ValueError('Invalid connection ID mapping')
    all_secrets = [secret for site in sites.values() for secret in set(site.secrets)]
    if len(all_secrets) != len(set(all_secrets)):
        raise ValueError('Signing secrets must not be shared across sites')

    async def respond(send, status):
        # No provider responses, diagnostics, payload fields, URLs or secrets reflected.
        await send({'type': 'http.response.start', 'status': status, 'headers': [
            (b'content-length', b'0'), (b'cache-control', b'no-store')]})
        await send({'type': 'http.response.body', 'body': b''})

    async def app(scope, receive, send):
        if scope['type'] != 'http':
            return
        if scope['path'] == '/health' and scope['method'] == 'GET':
            return await respond(send, 204)
        route = re.fullmatch(r'/hooks/([a-z0-9-]{1,64})', scope['path'])
        site = sites.get(route[1]) if route else None
        if site is None:
            return await respond(send, 404)
        if scope['method'] != 'POST':
            return await respond(send, 405)
        if scope.get('query_string'):
            return await respond(send, 400)
        headers = {}
        for key, value in scope['headers']:
            headers.setdefault(key.lower(), []).append(value)
        if sum(len(k)+len(v) for k, v in scope['headers']) > 16384:
            return await respond(send, 431)
        lengths = headers.get(b'content-length', [])
        types = headers.get(b'content-type', [])
        signatures = headers.get(b'x-ghost-signature', [])
        if len(signatures) != 1:
            return await respond(send, 401)
        if (len(types) != 1 or types[0].lower() not in (b'application/json', b'application/json; charset=utf-8')):
            return await respond(send, 415)
        if b'content-encoding' in headers or b'transfer-encoding' in headers:
            return await respond(send, 400)
        if len(lengths) != 1 or not re.fullmatch(rb'[0-9]{1,8}', lengths[0]):
            return await respond(send, 411)
        length = int(lengths[0])
        if not 0 < length <= MAX_BODY:
            return await respond(send, 413)

        async def read_body():
            body = bytearray()
            while True:
                event = await receive()
                if event['type'] == 'http.disconnect':
                    return None
                if event['type'] != 'http.request':
                    raise InvalidEvent('Unexpected ASGI event')
                chunk = event.get('body', b'')
                if len(body)+len(chunk) > length:
                    raise InvalidEvent('Body exceeds declared length')
                body.extend(chunk)
                if not event.get('more_body', False):
                    if len(body) != length:
                        raise InvalidEvent('Truncated body')
                    return bytes(body)
        try:
            raw = await asyncio.wait_for(read_body(), timeout=read_timeout)
            if raw is None:
                return
            signature = signatures[0].decode('ascii')
            # Thread work may finish after client disconnect; its committed receipt handles replay.
            await asyncio.to_thread(queue.accept, raw, [signature], site)
        except asyncio.TimeoutError:
            return await respond(send, 408)
        except (InvalidEvent, UnicodeError, ValueError):
            return await respond(send, 400)
        except (sqlite3.Error, OSError, RuntimeError):
            return await respond(send, 503)
        return await respond(send, 202)
    return app


def configured_app():
    """Load server-owned config from a mounted file, never command-line secret arguments."""
    os.umask(0o077)
    config = json.loads(Path(os.environ['GHOST_CONFIG']).read_text())
    sites = {item['name']: Site(item['name'], item['origin'], tuple(bytes.fromhex(s) for s in item['secrets_hex']),
                               bytes.fromhex(item['ledger_key_hex'])) for item in config['sites']}
    if len(sites) != len(config['sites']):
        raise ValueError('Duplicate site IDs')
    return create_app(Queue(config['database']), sites)


if __name__ == '__main__':
    import uvicorn
    uvicorn.run(configured_app(), host='127.0.0.1', port=int(os.environ.get('PORT', '8186')),
                http='h11', lifespan='off', access_log=False, log_level='warning',
                limit_concurrency=32, timeout_keep_alive=2, h11_max_incomplete_event_size=16384)
