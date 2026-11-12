"""An ordinary key that shares a queue's prefix sets the queue's next number, then push fails."""

import sqlite3
import tempfile

import diskcache

with tempfile.TemporaryDirectory() as tmp, diskcache.Cache(tmp) as cache:
    cache['user-9'] = 'a profile, not a queue item'
    first = cache.push('job', prefix='user')
    print("push(prefix='user') ->", first)
    try:
        second = cache.push('job', prefix='user')
        print('second push ->', second)
    except sqlite3.IntegrityError as ex:
        print('second push raised IntegrityError:', ex)
        second = None
    bug = first == 'user-000000000000010' or second is None
    print('BUG REPRODUCED' if bug else 'not reproduced')
    raise SystemExit(1 if bug else 0)
