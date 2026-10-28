"""expire() removes only 100 entries when the expired entries share one expire time."""

import tempfile
import time
from unittest import mock

import diskcache

with tempfile.TemporaryDirectory() as tmp, diskcache.Cache(tmp) as cache:
    now = time.time()
    with mock.patch('time.time', return_value=now):  # Same timestamp for every set.
        for i in range(250):
            cache.set(f'k{i}', i, expire=10)
    removed = cache.expire(now=now + 11)
    print('expired entries: 250 | expire() removed:', removed, '| len after:', len(cache))
    bug = removed != 250
    print('BUG REPRODUCED' if bug else 'not reproduced')
    raise SystemExit(1 if bug else 0)
