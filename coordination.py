"""Cooperative Unix locks for one canonical, operator-owned local queue path."""
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import stat


@contextmanager
def queue_lock(database, *, exclusive=False):
    """Exclude recovery from active updated workers; never unlink the lock inode."""
    path = Path(database).resolve(strict=True)
    if not path.is_file():
        raise ValueError('Queue must be an existing regular file')
    fd = os.open(str(path) + '.workers.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise ValueError('Lock file must be private and owned by this operator')
        fcntl.flock(fd, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
        yield path
    finally:
        os.close(fd)
