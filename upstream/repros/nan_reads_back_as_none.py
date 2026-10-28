"""A top-level float NaN reads back as None, ignoring the default."""

import math
import tempfile

import diskcache

with tempfile.TemporaryDirectory() as tmp, diskcache.Cache(tmp) as cache:
    print('set ->', cache.set('k', math.nan))
    print("'k' in cache ->", 'k' in cache, '| len ->', len(cache))
    value = cache.get('k', default='DEFAULT')
    print('get(default=DEFAULT) ->', repr(value))
    print('nested [nan] ->', cache.set('n', [math.nan]) and cache.get('n'))
    bug = value is None
    print('BUG REPRODUCED' if bug else 'not reproduced')
    raise SystemExit(1 if bug else 0)
