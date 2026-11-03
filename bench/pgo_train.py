"""The calls `bench.baseline` doesn't make, for scripts/build_pgo.sh's profile training.

Everything a web app does with a cache, in rough proportion: mostly small reads and writes of
strings, bytes, dicts and datetimes through both serializers, some pickled values, spilled pages,
misses, deletes, atomics, key listings and maintenance, from several threads.

Usage: python -m bench.pgo_train
"""

from __future__ import annotations

import datetime as dt
import gzip
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from diskcache_oxide import Cache, Caches, NullCache


@dataclass(slots=True)
class Snapshot:
    title: str
    episodes: list[int]


ROUNDS = 20_000
NOW = dt.datetime(2026, 10, 30, 12, tzinfo=dt.UTC)
VALUES: list[Any] = [
    'a short string',
    'x' * 2_000,
    b'some bytes',
    12345,
    3.5,
    True,
    None,
    {
        'title': 'An episode',
        'number': 500,
        'guests': ['a', 'b'],
        'published': NOW,
        'score': 4.5,
    },
    [{'url': f'https://example.com/{i}', 'ok': True} for i in range(20)],
    (1, 'two', 3.0),
    {'body': gzip.compress(b'<html>' * 500), 'etag': 'abc', 'at': NOW},
]


def exercise(cache: Cache, seed: int) -> None:
    for i in range(ROUNDS):
        n = (i * 2_654_435_761 + seed) % 500
        key = f'k:{n}'
        cache.get(key)
        cache.get(key)
        cache.get(f'missing:{n}', 'default')
        if i % 10 == 0:
            cache.set(key, VALUES[n % len(VALUES)], expire=60 if n % 2 else None)
        if i % 50 == 0:
            key in cache
            cache.add(f'claim:{n}', 1, expire=30)
            cache.incr(f'ctr:{n % 20}')
            cache.touch(key, 60)
            cache.get(key, expire_time=True)
        if i % 200 == 0:
            cache.delete(key)
            cache.pop(f'claim:{n}')
        if i % 2_000 == 0:
            sum(1 for _ in cache.iterkeys(prefix='ctr:'))
            cache.expire()
            len(cache)
            cache.volume()


def main() -> None:
    with tempfile.TemporaryDirectory(prefix='dco-pgo-') as tmp:
        base = Path(tmp)
        with Caches(base, timeout=5.0, on_decode_error='miss') as caches:
            caches.register('msgpack')
            caches.register('json', serializer='json')
            caches.register('pickled', pickle=True)
            for name in ('msgpack', 'json'):
                cache = caches[name]
                assert isinstance(cache, Cache)
                threads = [threading.Thread(target=exercise, args=(cache, seed)) for seed in range(4)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join()

            pickled = caches['pickled']
            for i in range(ROUNDS // 2):
                key = f'snap:{i % 200}'
                if i % 10 == 0:
                    pickled.set(key, Snapshot('home', list(range(30))))
                pickled.get(key)

            pages = caches['msgpack']
            page = gzip.compress(bytes(range(256)) * 2_000)
            for i in range(2_000):
                key = f'page:{i % 40}'
                if i % 5 == 0:
                    pages.set(key, {'html': page, 'at': NOW}, expire=300)
                pages.get(key)

            off = NullCache(invalidate=pages)
            for i in range(ROUNDS // 10):
                off.get(f'k:{i}')
                off.delete(f'absent:{i}')
            caches.stats()
            caches.expire_all()
            caches['json'].clear()


if __name__ == '__main__':
    main()
