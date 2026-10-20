"""Quarantine restored queued receipts pending investigation. Never sends or requeues."""
import argparse
from contextlib import closing
import json
import re
import sqlite3
import sys
import time

from coordination import queue_lock

AUDIT_SCHEMA = '''CREATE TABLE IF NOT EXISTS restore_quarantines (
    id INTEGER PRIMARY KEY, site TEXT NOT NULL, event_key TEXT NOT NULL,
    quarantined_at INTEGER NOT NULL, actor TEXT NOT NULL,
    evidence_reference TEXT NOT NULL, UNIQUE(site,event_key))'''


def quarantine(database, site, actor, evidence_reference, *, limit=100, confirmed=False):
    """Atomically remove a bounded batch of restored queued rows from send eligibility."""
    if (confirmed is not True or not isinstance(site, str) or not site or len(site) > 200
            or type(limit) is not int or not 1 <= limit <= 500
            or any(not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,199}', value)
                   for value in (actor, evidence_reference))):
        raise ValueError('Invalid quarantine or missing stopped-service confirmation')
    with queue_lock(database, exclusive=True) as path:
        with closing(sqlite3.connect(path.as_uri() + '?mode=rw', uri=True, timeout=1)) as db, db:
            db.execute('PRAGMA synchronous=FULL')
            db.execute('BEGIN IMMEDIATE')
            if not db.execute('SELECT 1 FROM jobs WHERE site=? LIMIT 1', (site,)).fetchone():
                raise ValueError('Site has no receipts; verify recovery scope')
            db.execute(AUDIT_SCHEMA)
            db.execute('''INSERT INTO restore_quarantines
                (site,event_key,quarantined_at,actor,evidence_reference)
                SELECT site,event_key,?,?,? FROM jobs WHERE site=? AND state='queued' ORDER BY event_key LIMIT ?''',
                (int(time.time()), actor, evidence_reference, site, limit))
            quarantined = db.execute('''UPDATE jobs SET state='pending' WHERE site=? AND state='queued'
                AND event_key IN (SELECT event_key FROM restore_quarantines WHERE site=?)''', (site, site)).rowcount
            remaining = db.execute("SELECT count(*) FROM jobs WHERE site=? AND state='queued'", (site,)).fetchone()[0]
            queued_by_site = dict(db.execute("SELECT site,count(*) FROM jobs WHERE state='queued' GROUP BY site"))
            result = {'site': site, 'quarantined': quarantined, 'remaining_queued': remaining,
                      'queued_by_site': queued_by_site,
                      'meaning': 'Snapshot outcomes unknown; keep services stopped until gap reconciliation'}
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('database', 'site', 'actor', 'evidence-reference'):
        parser.add_argument('--' + name, required=True)
    parser.add_argument('--limit', type=int, default=100)
    parser.add_argument('--confirm-services-stopped', action='store_true')
    args = parser.parse_args()
    try:
        result = quarantine(args.database, args.site, args.actor, args.evidence_reference,
                            limit=args.limit, confirmed=args.confirm_services_stopped)
    except (OSError, ValueError, RuntimeError, sqlite3.Error):
        print('Quarantine refused: verify stopped services, recovery scope, arguments and database access.', file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
