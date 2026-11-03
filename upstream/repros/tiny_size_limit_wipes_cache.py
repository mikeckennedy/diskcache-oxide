"""A size_limit below the empty database's volume silently evicts every value, including the one just set."""

import tempfile

import diskcache

with (
    tempfile.TemporaryDirectory() as tmp,
    diskcache.Cache(tmp, size_limit=1_000) as cache,
):
    print('empty volume:', cache.volume(), 'bytes; size_limit: 1000')
    print('set ->', cache.set('k', 'v'))
    print('get ->', cache.get('k'), '| len ->', len(cache))
    bug = cache.get('k') is None
    print('BUG REPRODUCED (no error or warning; set returned True)' if bug else 'not reproduced')
    raise SystemExit(1 if bug else 0)
