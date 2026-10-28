"""Opening an existing cache with unchanged settings writes to the database.

A second connection's PRAGMA data_version changes when another connection commits. Reopening a
diskcache directory with the same settings and only reading still bumps it.
"""

import sqlite3
import tempfile
from pathlib import Path

import diskcache

with tempfile.TemporaryDirectory() as tmp:
    with diskcache.Cache(tmp) as cache:
        cache.set('k', 'v')

    peer = sqlite3.connect(Path(tmp) / 'cache.db')
    before = peer.execute('PRAGMA data_version').fetchone()[0]
    with diskcache.Cache(tmp) as cache:  # Same settings, read-only use.
        cache.get('k')
    after = peer.execute('PRAGMA data_version').fetchone()[0]
    peer.close()
    print(f'data_version before reopen: {before}, after reopen + get: {after}')
    bug = after != before
    print('BUG REPRODUCED (opening wrote to the database)' if bug else 'not reproduced')
    raise SystemExit(1 if bug else 0)
