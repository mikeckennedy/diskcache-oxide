"""The recommended setup: a durable registry for rebuildable data, plain caches for state.

Layout, all under one directory the app owns:

    <root>/cache/pages          rendered HTML fragments, tagged per episode     (durable registry)
    <root>/cache/data           results of the slow loader, via memoize          (durable registry)
    <root>/cache/rate_limits    per-client counters                              (durable registry)
    <root>/state/sessions       logins                                           (plain Cache)
    <root>/state/coordination   job claims and locks                             (plain Cache)
    <root>/state/jobs           the work queue                                   (plain Cache)

A directory is a cache or a parent of caches, never both.
"""

from __future__ import annotations

import functools
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from diskcache_oxide import Cache, Caches

import app.cache_types  # noqa: F401  (registers the stored types before any cache is read)
from app import reads

MiB = 2**20
CacheName = Literal['pages', 'data', 'rate_limits']


@dataclass
class AppCaches:
    """Everything the app keeps on disk, opened once per process."""

    registry: Caches[CacheName]
    sessions: Cache
    coordination: Cache
    jobs: Cache

    def state(self) -> dict[str, Cache]:
        return {'sessions': self.sessions, 'coordination': self.coordination, 'jobs': self.jobs}

    def close(self) -> None:
        self.registry.close_all()
        for cache in self.state().values():
            cache.close()


def open_caches(root: Path) -> AppCaches:
    # Rebuildable data: durable, so an outage is a miss and a slower page, never an error page.
    # timeout=1.0 keeps a busy lock from holding a request for the default 60 seconds.
    registry: Caches[CacheName] = Caches(root / 'cache', durable=True, timeout=1.0)
    registry.register('pages', size_limit=256 * MiB)
    registry.register('data', size_limit=64 * MiB)
    registry.register('rate_limits', size_limit=16 * MiB)  # least-recently-stored: never 'none'

    # State: opened directly (a registry would hand out a NullCache when one can't open, and then
    # add() always wins and push() stores nothing), not durable, never evicted for size.
    def state(name: str) -> Cache:
        return Cache(root / 'state' / name, timeout=3.0, eviction_policy='none')

    return AppCaches(registry, state('sessions'), state('coordination'), state('jobs'))


def memoized(registry: Caches[CacheName], name: CacheName, **options: object) -> Callable:
    """`memoize` that looks the cache up on every call, so it follows the registry's switches and
    recovers when a cache that couldn't open comes back (caches.qmd, "memoize on a registered
    cache"). Each call also records whether it was a hit, for the page's request strip."""

    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args: object, **kwargs: object) -> object:
            cache = registry[name]
            memo = cache.memoize(**options)(func)  # ty: ignore[invalid-argument-type]
            key = memo.__cache_key__(*args, **kwargs)
            started = time.perf_counter()
            hit = key in cache
            value = memo(*args, **kwargs)
            label = f'{getattr(func, "__name__", "call")}{args!r}'.replace(',)', ')')
            reads.record(name, label, hit, time.perf_counter() - started)
            return value

        return wrapper

    return decorator
