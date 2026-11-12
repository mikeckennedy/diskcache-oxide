"""Under eviction_policy='none', cull() removes the expired entries but returns 0."""

import tempfile
import time

import diskcache

with tempfile.TemporaryDirectory() as tmp, diskcache.Cache(tmp, eviction_policy='none') as cache:
    cache.set('a', 1, expire=0.01)
    cache.set('b', 2, expire=0.01)
    time.sleep(0.05)
    removed = cache.cull()
    print('cull() ->', removed, '| len after ->', len(cache))
    bug = removed == 0 and len(cache) == 0
    print('BUG REPRODUCED' if bug else 'not reproduced')
    raise SystemExit(1 if bug else 0)
