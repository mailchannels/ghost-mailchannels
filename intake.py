"""Signed Ghost post-published intake and durable notification queue (no sender)."""
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import hmac
import json
import re
import sqlite3
import time
from urllib.parse import urlsplit


class InvalidEvent(ValueError):
    """A request cannot be accepted under the configured site contract."""


@dataclass(frozen=True)
class Site:
    """Server-owned connection identity; never derived from webhook fields."""
    name: str
    origin: str
    secrets: tuple[bytes, ...]
    ledger_key: bytes

    def __post_init__(self):
        url = urlsplit(self.origin)
        if (not self.name or url.scheme != 'https' or not url.hostname or url.username
                or url.password or url.path not in ('', '/') or url.query or url.fragment
                or not self.secrets or any(len(secret) < 32 for secret in self.secrets)
                or len(self.ledger_key) < 32):
            raise ValueError('Invalid server-side site configuration')


def unique_object(pairs):
    """Reject duplicate JSON keys instead of depending on parser overwrite rules."""
    result = {}
    for key, value in pairs:
        if key in result:
            raise InvalidEvent('Duplicate JSON key')
        result[key] = value
    return result


def date(value):
    """Normalize timezone-aware event versions without losing fractional seconds."""
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            raise ValueError()
        return parsed.astimezone(timezone.utc).isoformat(timespec='microseconds')
    except (AttributeError, TypeError, ValueError):
        raise InvalidEvent('Invalid post timestamp') from None


def verify(raw, signature_headers, site, now_ms=None):
    """Verify original bytes plus signed millisecond timestamp, then parse minimal fields."""
    if not isinstance(raw, bytes) or not 0 < len(raw) <= 1_048_576:
        raise InvalidEvent('Invalid request size')
    if len(signature_headers) != 1:
        raise InvalidEvent('Exactly one signature header is required')
    match = re.fullmatch(r'sha256=([0-9a-f]{64}), t=([0-9]{13})', signature_headers[0])
    if not match:
        raise InvalidEvent('Invalid signature format')
    digest, timestamp = match.groups()
    now = int(time.time() * 1000) if now_ms is None else now_ms
    if abs(now - int(timestamp)) > 300_000:
        raise InvalidEvent('Expired or future signature')
    valid = False
    for secret in site.secrets:
        expected = hmac.new(secret, raw + timestamp.encode('ascii'), hashlib.sha256).hexdigest()
        valid |= hmac.compare_digest(digest, expected)
    if not valid:
        raise InvalidEvent('Invalid signature')
    try:
        event = json.loads(raw.decode('utf-8'), object_pairs_hook=unique_object)
        if event['event'] != 'post.published':
            raise InvalidEvent('Unsupported event')
        post = event['post']['current']
        if post['status'] != 'published':
            raise InvalidEvent('Expected a published post')
        post_id, title, url = post['id'], post['title'], post['url']
        if not isinstance(post_id, str) or not re.fullmatch(r'[a-f0-9]{24}', post_id):
            raise InvalidEvent('Invalid post ID')
        if not isinstance(title, str) or not 0 < len(title) <= 2000 or any(ord(c) < 32 for c in title):
            raise InvalidEvent('Invalid title')
        if not isinstance(url, str) or len(url) > 4096 or any(ord(c) < 33 for c in url):
            raise InvalidEvent('Invalid post URL')
        target, origin = urlsplit(url), urlsplit(site.origin)
        if (target.scheme != 'https' or target.netloc != origin.netloc or target.username
                or target.password or target.fragment):
            raise InvalidEvent('Post URL does not belong to configured origin')
        published, updated = date(post['published_at']), date(post['updated_at'])
        if updated < published:
            raise InvalidEvent('Post version predates publication')
        return {'id': post_id, 'published_at': published, 'updated_at': updated, 'title': title, 'url': url}
    except (KeyError, TypeError, UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        raise InvalidEvent('Invalid published-post payload') from None


class Queue:
    """SQLite intake receipts; only queued jobs are eligible for a future sender."""
    def __init__(self, path, capacity=10000):
        self.path, self.capacity = path, capacity
        with closing(self.connect()) as db, db:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('''CREATE TABLE IF NOT EXISTS jobs (
                site TEXT NOT NULL, event_key TEXT NOT NULL, content_hash TEXT NOT NULL,
                title TEXT NOT NULL, url TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'queued',
                created_at INTEGER NOT NULL, PRIMARY KEY(site,event_key))''')
            # Additive migration preserves legacy receipt states and deduplication keys.
            db.execute('BEGIN IMMEDIATE')
            columns = {row[1] for row in db.execute('PRAGMA table_info(jobs)')}
            for name, kind in (
                    ('attempt_started_at', 'INTEGER'), ('response_observed_at', 'INTEGER'),
                    ('http_status', 'INTEGER'), ('attempt_sender', 'TEXT'),
                    ('attempt_recipients', 'TEXT')):
                if name not in columns:
                    db.execute(f'ALTER TABLE jobs ADD COLUMN {name} {kind}')


    def connect(self):
        """Open a short transaction; lock contention must fail before Ghost's request budget."""
        db = sqlite3.connect(self.path, timeout=1)
        db.execute('PRAGMA synchronous=FULL')
        return db

    def accept(self, raw, signature_headers, site, now_ms=None):
        """Durably enqueue once; a success means stored, never sent or delivered."""
        event = verify(raw, signature_headers, site, now_ms)
        identity = json.dumps([site.name, 'post.published', event['id'], event['published_at'], event['updated_at']], separators=(',', ':')).encode()
        event_key = hmac.new(site.ledger_key, identity, hashlib.sha256).hexdigest()
        content = json.dumps([event['title'], event['url']], ensure_ascii=False, separators=(',', ':')).encode()
        content_hash = hmac.new(site.ledger_key, content, hashlib.sha256).hexdigest()
        db = self.connect()
        try:
            with db:
                db.execute('BEGIN IMMEDIATE')
                previous = db.execute('SELECT content_hash FROM jobs WHERE site=? AND event_key=?', (site.name, event_key)).fetchone()
                if previous:
                    if not hmac.compare_digest(previous[0], content_hash):
                        raise InvalidEvent('Conflicting content for existing post version')
                    return {'outcome': 'duplicate', 'event_key': event_key}
                count = db.execute('SELECT count(*) FROM jobs WHERE site=?', (site.name,)).fetchone()[0]
                if count >= self.capacity:
                    raise RuntimeError('Queue capacity reached; operator action required')
                db.execute('INSERT INTO jobs(site,event_key,content_hash,title,url,created_at) VALUES(?,?,?,?,?,?)',
                           (site.name, event_key, content_hash, event['title'], event['url'], int(time.time())))
            return {'outcome': 'queued', 'event_key': event_key}
        finally:
            db.close()
