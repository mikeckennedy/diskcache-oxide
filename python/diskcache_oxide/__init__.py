"""diskcache-oxide: a shared, durable disk cache with a Rust engine, SQLite storage and diskcache's API.

A cache is a directory: every thread and process that opens the same path shares one cache, and it
survives restarts. The entry points:

- `Cache(directory)`: the cache. `get`, `set`, `add`, `delete`, `pop`, `touch`, `incr`, `decr`,
  dict syntax, `iterkeys(prefix=)`, `expire()`, `clear()`, `cull()`, `checkpoint()`, `transact()`,
  `memoize()`, tags with `evict(tag)`, and the queue calls `push`, `pull`, `peek`, `peekitem`.
  Keys are `str`, `bytes`, `int`, `float`, `bool`, `None` or tuples of those.
- `FanoutCache(directory, shards=8)`: the same API over several databases, for many processes that
  write often.
- `NullCache()`: caching switched off, with the same API, so call sites don't change.
- `Caches(base_dir)`: a registry of named caches, each opened on first use.
- `CacheLike`, `TransactionalCacheLike`, `ShardedCacheLike`: protocols for code that accepts any of
  them.
- `Lock`, `RLock`, `BoundedSemaphore`, `throttle`, `barrier`, `Averager`, `memoize_stampede`: the
  coordination recipes, holding across every process.
- `register_type(cls, name=..., encode=..., decode=...)`: store your own types without pickle; a
  dataclass needs only a name. `registry` is the process-wide `TypeRegistry` behind it.
- Exceptions: `Timeout` (a `TimeoutError`), `TransactionBusy` (a `Timeout`), `TransactionAborted`,
  `FormatMismatchError` and `LayoutMismatchError` (`RuntimeError`s), `UnsafeValueError` (a
  `TypeError`), `CorruptValueError`, `CorruptKeyError` and `ForeignValueError` (`ValueError`s).

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

from collections.abc import Callable
from typing import Any

from diskcache_oxide._constants import ENOVAL, UNKNOWN
from diskcache_oxide._core import (
    BUNDLED_SQLITE_VERSION,
    Cache,
    CorruptKeyError,
    CorruptValueError,
    FanoutCache,
    ForeignValueError,
    FormatMismatchError,
    LayoutMismatchError,
    Timeout,
    TransactionAborted,
    TransactionBusy,
    TypeRegistry,
    UnsafeValueError,
    __version__,
    registry,
    sqlite_compile_options,
    sqlite_version,
)
from diskcache_oxide._types import Key, Memoized, Memoizer
from diskcache_oxide._warnings import EmptyDirWarning, UnknownFileWarning
from diskcache_oxide.caches import Caches, CacheStats
from diskcache_oxide.null import NullCache
from diskcache_oxide.protocol import CacheLike, ShardedCacheLike, TransactionalCacheLike
from diskcache_oxide.recipes import Averager, BoundedSemaphore, Lock, RLock, barrier, memoize_stampede, throttle


def register_type[T](
    cls: type[T],
    *,
    name: str,
    encode: Callable[[T], object] | None = None,
    decode: Callable[[Any], T] | None = None,
) -> None:
    """Register `cls` in the process-wide `registry`, so every cache can store it without pickle.

    `encode(instance)` must return a value the codec can already store and `decode(value)` must give the
    instance back. A dataclass needs neither: its `init=True` fields are stored and it is rebuilt with
    `cls(**fields)`. See `TypeRegistry.register` for the rules. Call it at import time, in every process
    that opens the cache.

    Examples:
        ```python
        from bson import ObjectId
        import diskcache_oxide

        diskcache_oxide.register_type(ObjectId, name='bson.ObjectId', encode=str, decode=ObjectId)
        diskcache_oxide.register_type(Episode, name='tp.Episode')  # a dataclass
        ```
    """
    registry.register(cls, name=name, encode=encode, decode=decode)


def unregister_type(cls: type) -> bool:
    """Remove `cls` from the process-wide `registry`. Returns whether it was registered."""
    return registry.unregister(cls)


def registered_types() -> dict[str, type]:
    """The process-wide registrations, as `{name: cls}`."""
    return registry.registered()


__all__ = [
    'Averager',
    'BUNDLED_SQLITE_VERSION',
    'BoundedSemaphore',
    'Cache',
    'CacheLike',
    'CacheStats',
    'Caches',
    'CorruptKeyError',
    'CorruptValueError',
    'ENOVAL',
    'EmptyDirWarning',
    'FanoutCache',
    'ForeignValueError',
    'FormatMismatchError',
    'Key',
    'LayoutMismatchError',
    'Lock',
    'Memoized',
    'Memoizer',
    'NullCache',
    'RLock',
    'ShardedCacheLike',
    'Timeout',
    'TransactionAborted',
    'TransactionBusy',
    'TransactionalCacheLike',
    'TypeRegistry',
    'UNKNOWN',
    'UnknownFileWarning',
    'UnsafeValueError',
    '__version__',
    'barrier',
    'memoize_stampede',
    'register_type',
    'registered_types',
    'registry',
    'sqlite_compile_options',
    'sqlite_version',
    'throttle',
    'unregister_type',
]
