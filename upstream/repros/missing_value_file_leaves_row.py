"""When a value file is deleted out from under the cache, get() misses but the row stays counted."""

import pathlib
import tempfile

import diskcache

with tempfile.TemporaryDirectory() as tmp, diskcache.Cache(tmp) as cache:
    cache.set('big', b'x' * 100_000)  # Large enough to be stored as a separate file.
    files = [p for p in pathlib.Path(tmp).rglob('*') if p.is_file() and not p.name.startswith('cache.db')]
    files[0].unlink()
    print(
        'get ->',
        cache.get('big', 'DEFAULT'),
        "| 'big' in cache ->",
        'big' in cache,
        '| len ->',
        len(cache),
    )
    bug = cache.get('big', 'DEFAULT') == 'DEFAULT' and 'big' in cache
    print('INCONSISTENCY REPRODUCED (get misses, but in/len still see the entry)' if bug else 'not reproduced')
    raise SystemExit(1 if bug else 0)
