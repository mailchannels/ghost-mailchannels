"""Read-only, site-scoped queue diagnostics. Never sends or changes receipt state."""
import argparse
from contextlib import closing
import json
from pathlib import Path
import re
import sqlite3
import sys
import time

STATES = ('queued', 'pending', 'accepted', 'rejected')


def inspect(database, site, *, limit=50, after=None, include_content=False):
    if not isinstance(site, str) or not site or not 1 <= limit <= 500:
        raise ValueError('Invalid site or page size')
    if after is not None and not re.fullmatch(r'[0-9a-f]{64}', after):
        raise ValueError('Invalid receipt cursor')
    # No Queue constructor: it creates files/schema. URI escaping preserves literal ?/# paths.
    uri = Path(database).resolve().as_uri() + '?mode=ro'
    with closing(sqlite3.connect(uri, uri=True, timeout=1)) as db:
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        counts = {state: 0 for state in STATES}
        oldest = {}
        for state, count, created in db.execute(
                'SELECT state,count(*),min(created_at) FROM jobs WHERE site=? GROUP BY state', (site,)):
            if state not in counts:
                raise ValueError('Unsupported receipt state')
            counts[state] = count
            oldest[state] = created
        available = {row[1] for row in db.execute('PRAGMA table_info(jobs)')}
        metadata = ['attempt_started_at', 'response_observed_at', 'http_status']
        if include_content:
            metadata += ['attempt_sender', 'attempt_recipients']
        metadata = [name for name in metadata if name in available]
        fields = 'event_key,state,created_at' + (',title,url' if include_content else '')
        fields += ''.join(',' + name for name in metadata)
        rows = db.execute(f'SELECT {fields} FROM jobs WHERE site=? AND event_key>? ORDER BY event_key LIMIT ?',
                          (site, after or '', limit + 1)).fetchall()
        columns = ['event_key', 'state', 'created_at'] + (['title', 'url'] if include_content else []) + metadata
        receipts = [dict(zip(columns, row)) for row in rows[:limit]]
        for receipt in receipts:
            if receipt.get('attempt_recipients') is not None:
                receipt['attempt_recipients'] = json.loads(receipt['attempt_recipients'])
        for table, key, audit_columns in (('resolutions', 'operator_resolution', ['id', 'resolved_at', 'outcome']),
                                          ('restore_quarantines', 'restore_quarantine', ['id', 'quarantined_at'])):
            if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
                continue
            if include_content:
                audit_columns += ['actor', 'evidence_reference']
            for receipt in receipts:
                audit = db.execute('SELECT ' + ','.join(audit_columns) + f' FROM {table} WHERE site=? AND event_key=?',
                                   (site, receipt['event_key'])).fetchone()
                receipt[key] = dict(zip(audit_columns, audit)) if audit else None

    return {'site': site, 'observed_at': int(time.time()), 'counts': counts,
            'total_receipts': sum(counts.values()), 'oldest_intake_at_by_state': oldest,
            'receipts': receipts, 'next_cursor': rows[limit-1][0] if len(rows) > limit else None,
            'pending_meaning': 'In flight, acceptance unknown or restore quarantine; never automatically resend',
            'accepted_meaning': 'Provider accepted; delivery not established'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', required=True)
    parser.add_argument('--site', required=True)
    parser.add_argument('--limit', type=int, default=50)
    parser.add_argument('--after')
    parser.add_argument('--include-content', action='store_true', help='Include stored editorial titles and URLs')
    args = parser.parse_args()
    try:
        result = inspect(args.database, args.site, limit=args.limit, after=args.after,
                         include_content=args.include_content)
    except (OSError, ValueError, sqlite3.Error):
        print('Receipt inspection failed: check database access/schema and arguments.', file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
