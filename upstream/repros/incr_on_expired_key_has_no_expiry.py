"""incr() on an expired key creates a fresh counter with no expiry (the old TTL is dropped)."""

import tempfile
import time
from unittest import mock

import diskcache

with tempfile.TemporaryDirectory() as tmp, diskcache.Cache(tmp) as cache:
    now = time.time()
    with mock.patch('time.time', return_value=now):
        cache.add('counter', 0, expire=60)
    with mock.patch('time.time', return_value=now + 61):  # The window has expired.
        print('incr ->', cache.incr('counter'))
        value, expire_time = cache.get('counter', expire_time=True)
    print('value:', value, '| expire_time:', expire_time)
    bug = expire_time is None
    print('BUG REPRODUCED (the counter now never expires)' if bug else 'not reproduced')
    raise SystemExit(1 if bug else 0)
