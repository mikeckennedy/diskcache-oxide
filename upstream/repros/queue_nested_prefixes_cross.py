"""pull(prefix='q') takes an item of the queue 'q-1'."""

import tempfile

import diskcache

with tempfile.TemporaryDirectory() as tmp, diskcache.Cache(tmp) as cache:
    cache.push('an item of queue q-1', prefix='q-1')
    got = cache.pull(prefix='q')
    print("pull(prefix='q') ->", got)
    bug = got[0] == 'q-1-500000000000000'
    print('BUG REPRODUCED' if bug else 'not reproduced')
    raise SystemExit(1 if bug else 0)
