"""diskcache-oxide: a shared, durable disk cache with a Rust engine, SQLite storage and diskcache's API.

A cache is a directory: every thread and process that opens the same path shares one cache, and it
survives restarts. The entry points:

- `Cache(directory)`: the cache. `get`, `set`, `add`, `delete`, `pop`, `touch`, `incr`, `decr`,
  dict syntax, `iterkeys(prefix=)`, `expire()`, `clear()`, `cull()`, `checkpoint()`, `transact()`.
- `FanoutCache(directory, shards=8)`: the same API over several databases, for many processes that
  write often.
- `NullCache()`: caching switched off, with the same API, so call sites don't change.
- `Caches(base_dir)`: a registry of named caches, each opened on first use.
- `CacheLike`, `TransactionalCacheLike`, `ShardedCacheLike`: protocols for code that accepts any of
  them.
- Exceptions: `Timeout` (a `TimeoutError`), `TransactionBusy`, `UnsafeValueError` (a `TypeError`),
  `CorruptValueError` and `ForeignValueError` (`ValueError`s), `FormatMismatchError` and
  `LayoutMismatchError` (`RuntimeError`s).

There is no command-line interface and no environment variable to set. Warnings and swallowed
failures go to the `diskcache_oxide` logger.

Examples:
    ```python
    from diskcache_oxide import Cache

    with Cache('/var/cache/my-app', timeout=1.0) as cache:
        cache.set('user:42', {'name': 'Sam'}, expire=300)
        user = cache.get('user:42')  # None on a miss
    ```

`Cache` and `FanoutCache` live in the compiled `diskcache_oxide._core` extension. This package
re-exports them, so `diskcache_oxide.Timeout` is the public name and the one exceptions pickle
under. `NullCache`, the protocols and `Caches` are pure Python.
"""

from diskcache_oxide._core import (
    BUNDLED_SQLITE_VERSION,
    Cache,
    CorruptValueError,
    FanoutCache,
    ForeignValueError,
    FormatMismatchError,
    LayoutMismatchError,
    Timeout,
    TransactionBusy,
    UnsafeValueError,
    __version__,
    sqlite_compile_options,
    sqlite_version,
)
from diskcache_oxide.caches import Caches, CacheStats
from diskcache_oxide.null import NullCache
from diskcache_oxide.protocol import CacheLike, ShardedCacheLike, TransactionalCacheLike

__all__ = [
    'BUNDLED_SQLITE_VERSION',
    'Cache',
    'CacheLike',
    'CacheStats',
    'Caches',
    'CorruptValueError',
    'FanoutCache',
    'ForeignValueError',
    'FormatMismatchError',
    'LayoutMismatchError',
    'NullCache',
    'ShardedCacheLike',
    'Timeout',
    'TransactionBusy',
    'TransactionalCacheLike',
    'UnsafeValueError',
    '__version__',
    'sqlite_compile_options',
    'sqlite_version',
]
