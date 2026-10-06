"""Audit a conclusive pending-receipt resolution. Never sends, retries or requeues."""
import argparse
from contextlib import closing
import json
import re
import sqlite3
import sys
import time

from coordination import queue_lock


AUDIT_SCHEMA = '''CREATE TABLE IF NOT EXISTS resolutions (
    id INTEGER PRIMARY KEY, site TEXT NOT NULL, event_key TEXT NOT NULL,
    resolved_at INTEGER NOT NULL, outcome TEXT NOT NULL, actor TEXT NOT NULL,
    evidence_reference TEXT NOT NULL, UNIQUE(site,event_key))'''


def resolve(database, site, event_key, outcome, actor, evidence_reference, *, confirmed=False):
    """Atomically store operator attribution and resolve pending after external investigation."""
    if (confirmed is not True or outcome not in ('accepted', 'rejected')
            or not isinstance(site, str) or not site or len(site) > 200
            or not isinstance(event_key, str) or not re.fullmatch(r'[0-9a-f]{64}', event_key)
            or any(not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,199}', value)
                   for value in (actor, evidence_reference))):
        raise ValueError('Invalid resolution or missing evidence confirmation')
    # No Queue constructor: refuse missing DBs and do not migrate legacy job metadata.
    with queue_lock(database, exclusive=True) as path:
        with closing(sqlite3.connect(path.as_uri() + '?mode=rw', uri=True, timeout=1)) as db, db:
            db.execute('PRAGMA synchronous=FULL')
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT state FROM jobs WHERE site=? AND event_key=?', (site,event_key)).fetchone()
            if row != ('pending',):
                raise ValueError('Only an existing pending receipt can be resolved')
            db.execute(AUDIT_SCHEMA)
            now = int(time.time())
            cursor = db.execute('''INSERT INTO resolutions
                (site,event_key,resolved_at,outcome,actor,evidence_reference) VALUES(?,?,?,?,?,?)''',
                (site,event_key,now,outcome,actor,evidence_reference))
            changed = db.execute("UPDATE jobs SET state=? WHERE site=? AND event_key=? AND state='pending'",
                                 (outcome,site,event_key)).rowcount
            if changed != 1:
                raise RuntimeError('Receipt changed during resolution')
            result = {'resolution_id': cursor.lastrowid, 'site': site, 'event_key': event_key,
                      'outcome': outcome, 'resolved_at': now,
                      'meaning': 'Operator-confirmed provider outcome; no delivery or resend claim'}
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('database', 'site', 'receipt', 'actor', 'evidence-reference'):
        parser.add_argument('--' + name, required=True)
    parser.add_argument('--outcome', choices=('accepted', 'rejected'), required=True)
    parser.add_argument('--confirm-provider-evidence', action='store_true')
    args = parser.parse_args()
    try:
        result = resolve(args.database, args.site, args.receipt, args.outcome, args.actor,
                         args.evidence_reference, confirmed=args.confirm_provider_evidence)
    except (OSError, ValueError, RuntimeError, sqlite3.Error):
        print('Resolution refused: verify evidence, pending receipt, database access and stopped workers.', file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
