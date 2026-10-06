"""One-shot durable sender for Ghost editorial notifications; no automatic retries."""
from contextlib import closing
from dataclasses import dataclass
import html
import http.client
import json
import os
from pathlib import Path
import re
import ssl
import time
import urllib.error
import urllib.request

from intake import Queue
from coordination import queue_lock


@dataclass(frozen=True)
class Delivery:
    """Trusted sender, internal recipients and API key; never sourced from an event."""
    sender: str
    recipients: tuple[str, ...]
    api_key: str

    def __post_init__(self):
        addresses = (self.sender,) + self.recipients
        if (not self.recipients or len(self.recipients) > 50 or len(set(self.recipients)) != len(self.recipients)
                or any(not isinstance(v, str) or not re.fullmatch(r'[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+', v) for v in addresses)
                or not self.api_key.strip() or any(ord(c) < 33 for c in self.api_key)):
            raise ValueError('Invalid server-side delivery configuration')


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect rather than forwarding credentials or changing method."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def transport(payload, api_key):
    """Single fixed-host TLS request. Return only status; discard response content."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect(),
                                        urllib.request.HTTPSHandler(context=ssl.create_default_context()))
    request = urllib.request.Request('https://api.mailchannels.net/tx/v1/send',
        data=json.dumps(payload).encode(), method='POST',
        headers={'Content-Type': 'application/json', 'X-Api-Key': api_key})
    try:
        with opener.open(request, timeout=10) as response:
            return response.status
    except urllib.error.HTTPError as error:
        status = error.code
        error.close()
        return status
    except (urllib.error.URLError, OSError, http.client.HTTPException):
        return None


def claim(queue, site, delivery=None):
    """Commit pending before any provider call, excluding all other workers."""
    with closing(queue.connect()) as db, db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute("SELECT event_key,title,url FROM jobs WHERE site=? AND state='queued' ORDER BY created_at,event_key LIMIT 1", (site,)).fetchone()
        if row is None:
            return None
        db.execute("""UPDATE jobs SET state='pending', attempt_started_at=?,
                      response_observed_at=NULL,http_status=NULL,
                      attempt_sender=?,attempt_recipients=?
                      WHERE site=? AND event_key=? AND state='queued'""",
                   (int(time.time()), delivery.sender if delivery else None,
                    json.dumps(delivery.recipients) if delivery else None, site, row[0]))
    return row


def finish(queue, site, event_key, state, status=None):
    """Record a conclusive outcome only from pending; no path resets work to queued."""
    if state not in ('accepted','rejected'):
        raise ValueError('Invalid final receipt state')
    with closing(queue.connect()) as db, db:
        changed=db.execute("""UPDATE jobs SET state=?,response_observed_at=?,http_status=?
                              WHERE site=? AND event_key=? AND state='pending'""",
                           (state,int(time.time()),status,site,event_key)).rowcount
        if changed != 1:
            raise RuntimeError('Receipt transition failed')


def send_one(queue, site, delivery, send=transport):
    """Hold a shared process lock throughout a send; recovery needs exclusive access."""
    with queue_lock(queue.path):
        return _send_one(queue, site, delivery, send)


def _send_one(queue, site, delivery, send):
    """Process one queued notification; ambiguous outcomes remain pending indefinitely."""
    if not isinstance(delivery, Delivery):
        raise ValueError('Validated server delivery configuration required')
    job=claim(queue,site,delivery)
    if job is None:
        return 'idle'
    event_key,title,url=job
    payload={'from':{'email':delivery.sender},'personalizations':[{'to':[{'email':r} for r in delivery.recipients]}],
             'subject':'New post published', 'content':[
                 {'type':'text/plain','value':f'A post was published:\n\n{title}\n{url}'},
                 {'type':'text/html','value':f'<p>A post was published:</p><p><a href="{html.escape(url,quote=True)}">{html.escape(title)}</a></p>'}]}
    status=send(payload,delivery.api_key)
    if status == 202:
        finish(queue,site,event_key,'accepted',status)
        return 'accepted'
    if isinstance(status,int) and 400 <= status < 500 and status != 408:
        finish(queue,site,event_key,'rejected',status)
        return 'rejected'
    # None means no conclusive response was observed. Never invent a completion time.
    if type(status) is int and 100 <= status <= 599:
        with closing(queue.connect()) as db, db:
            db.execute("""UPDATE jobs SET response_observed_at=?,http_status=?
                          WHERE site=? AND event_key=? AND state='pending'""",
                       (int(time.time()),status,site,event_key))
    return 'pending'


if __name__ == '__main__':
    os.umask(0o077)
    config=json.loads(Path(os.environ['GHOST_WORKER_CONFIG']).read_text())
    delivery=Delivery(config['sender'],tuple(config['recipients']),config['api_key'])
    # One invocation sends at most one job. A supervisor may repeat; pending is never reclaimed.
    print(send_one(Queue(config['database']),config['site'],delivery))
