"""Create a private, consistent queue snapshot; never restore or send automatically."""
import argparse
from contextlib import closing, contextmanager
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time


def snapshot(database, output, *, timeout=30):
    """Back up committed SQLite state to a new file in an operator-owned directory."""
    if not 0 < timeout <= 300:
        raise ValueError('Invalid backup timeout')
    source = Path(database).resolve()
    target = Path(output).absolute()
    # lexists also refuses dangling symlinks. link below handles concurrent creation.
    if os.path.lexists(target) or source == target.resolve():
        raise FileExistsError('Backup destination must be new')
    started = time.monotonic()
    temporary = None

    def progress(status, remaining, total):
        if time.monotonic() - started > timeout:
            raise TimeoutError('Backup time budget exceeded')

    try:
        with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True, timeout=1)) as src:
            src.execute('PRAGMA query_only=ON')
            fd, name = tempfile.mkstemp(prefix='.ghost-backup-', suffix='.db', dir=target.parent)
            temporary = Path(name)
            os.close(fd)  # mkstemp creates the file with mode0600.
            with closing(sqlite3.connect(temporary)) as dst:
                src.backup(dst, pages=128, progress=progress, sleep=0.05)
                # Emit a standalone database, not a main file dependent on a WAL sibling.
                if dst.execute('PRAGMA journal_mode=DELETE').fetchone()[0] != 'delete':
                    raise ValueError('Unable to finalize standalone backup')
                if dst.execute('PRAGMA quick_check').fetchall() != [('ok',)]:
                    raise ValueError('Backup integrity check failed')
                dst.execute('SELECT site,event_key,content_hash,title,url,state,created_at FROM jobs LIMIT 0')
                counts = {state: count for state, count in dst.execute('SELECT state,count(*) FROM jobs GROUP BY state')}
                if set(counts) - {'queued', 'pending', 'accepted', 'rejected'}:
                    raise ValueError('Unsupported receipt state')
            with temporary.open('rb') as stream:
                digest = hashlib.file_digest(stream, 'sha256').hexdigest()
                os.fsync(stream.fileno())
            # Same-filesystem atomic publication; unlike replace, never clobbers a destination.
            os.link(temporary, target)
            temporary.unlink()
            with closing_fd(os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)) as directory:
                os.fsync(directory)
            return {'sha256': digest, 'bytes': target.stat().st_size, 'counts': counts,
                    'completed_at': int(time.time()),
                    'restore_policy': 'Keep workers stopped until snapshot gap is reconciled'}
    finally:
        if temporary is not None:
            for path in (temporary, Path(str(temporary) + '-wal'), Path(str(temporary) + '-shm'),
                         Path(str(temporary) + '-journal')):
                path.unlink(missing_ok=True)


@contextmanager
def closing_fd(fd):
    """Close a raw directory descriptor after syncing it."""
    try:
        yield fd
    finally:
        os.close(fd)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--timeout', type=float, default=30)
    args = parser.parse_args()
    try:
        result = snapshot(args.database, args.output, timeout=args.timeout)
    except (OSError, ValueError, sqlite3.Error):
        print('Backup failed: check source, new destination, storage and time budget. Do not start restored workers.', file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
