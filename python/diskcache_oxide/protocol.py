"""`CacheLike`: the interface `Cache`, `FanoutCache` and `NullCache` share, for typing code that accepts either.

`TransactionalCacheLike` adds `transact()`. It's a separate protocol so a cache whose transactions
need more than `retry` (a sharded cache's `transact(key=...)`) can still be a `CacheLike`.
`ShardedCacheLike` is that sharded cache's: `CacheLike` plus `transact(*, key, retry=False)`, which
`FanoutCache` and `NullCache` both satisfy.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable, Iterator
from contextlib import AbstractContextManager
from types import TracebackType
from typing import Any, Literal, Protocol, Self, overload, runtime_checkable

from diskcache_oxide._types import Key, Memoizer


@runtime_checkable
class CacheLike(Protocol):
    """What application code can rely on from a `Cache`, a `FanoutCache` or a `NullCache`.

    All three satisfy it, so a factory can return either and callers never need
    to check which one they got. A test keeps the three signatures identical.

    Examples:
        ```python
        from diskcache_oxide import Cache, CacheLike, NullCache

        def make_cache(enabled: bool) -> CacheLike:
            return Cache('/var/cache/my-app') if enabled else NullCache()

        def load_user(cache: CacheLike, user_id: str) -> dict:
            if (user := cache.get(f'user:{user_id}')) is not None:
                return user
            user = db.load_user(user_id)
            cache.set(f'user:{user_id}', user, expire=300)
            return user
        ```
    """

    @property
    def directory(self) -> str | None:
        """The cache directory (None for a `NullCache` created without one)."""

    @property
    def timeout(self) -> float:
        """Seconds a write waits for another process's lock before raising `Timeout`."""

    @property
    def size_limit(self) -> int:
        """The disk budget in bytes."""

    @property
    def cull_limit(self) -> int:
        """The most entries one `set()` evicts when over `size_limit`."""

    @property
    def enabled(self) -> bool:
        """True if caching is on (`Cache`), False if it's off (`NullCache`)."""

    @overload
    def get(
        self, key: Key, default: Any = None, *, expire_time: Literal[False] = False, retry: bool = False
    ) -> Any: ...
    @overload
    def get(
        self, key: Key, default: Any = None, *, expire_time: Literal[True], retry: bool = False
    ) -> tuple[Any, float | None]: ...
    def get(self, key: Key, default: Any = None, *, expire_time: bool = False, retry: bool = False) -> Any:
        """Return the value for `key`, or `default` if it's missing or expired."""

    def set(self, key: Key, value: Any, expire: float | None = None, *, retry: bool = False) -> bool:
        """Store `value` under `key`, with an optional expiry in seconds."""

    def add(self, key: Key, value: Any, expire: float | None = None, *, retry: bool = False) -> bool:
        """Store `value` only if `key` is missing or expired. True if it was stored."""

    def delete(self, key: Key, *, retry: bool = False) -> bool:
        """Remove `key`. True if a live entry was removed."""

    def pop(self, key: Key, default: Any = None, *, retry: bool = False) -> Any:
        """Remove `key` and return its value, or `default`."""

    def touch(self, key: Key, expire: float | None = None, *, retry: bool = False) -> bool:
        """Change an existing entry's expiry. True if the entry existed."""

    def incr(self, key: Key, delta: float = 1, default: float | None = 0, *, retry: bool = False) -> int | float:
        """Add `delta` to the number under `key`, atomically, and return the result."""

    def decr(self, key: Key, delta: float = 1, default: float | None = 0, *, retry: bool = False) -> int | float:
        """Subtract `delta` from the number under `key`, atomically, and return the result."""

    def memoize(
        self,
        name: str | None = None,
        typed: bool = False,
        expire: float | None = None,
        ignore: Iterable[int | str] = (),
    ) -> Memoizer:
        """A decorator caching a function's results in this cache (diskcache's `memoize`)."""

    @overload
    def iterkeys(self, *, prefix: str, reverse: bool = False) -> Iterator[str]: ...
    @overload
    def iterkeys(self, *, prefix: None = None, reverse: bool = False) -> Iterator[Key]: ...
    def iterkeys(self, *, prefix: str | None = None, reverse: bool = False) -> Iterator[Key]:
        """Iterate over keys in sorted order, optionally only those starting with `prefix`."""

    def expire(self, *, timeout: float | None = None, retry: bool = False) -> int:
        """Remove every expired entry. Returns how many were removed."""

    def clear(self, *, timeout: float | None = None, retry: bool = False) -> int:
        """Remove every entry that existed when the call started. Returns how many."""

    def cull(self, *, timeout: float | None = None, retry: bool = False) -> int:
        """Remove expired entries, then evict until under `size_limit`. Returns how many."""

    def volume(self) -> int:
        """The size on disk in bytes."""

    def checkpoint(self, *, timeout: float | None = None) -> bool:
        """Empty the write-ahead log. True if it was reset, False if other processes kept it busy."""

    def close(self, *, checkpoint: bool = True) -> None:
        """Close this handle and return its connections."""

    def __getitem__(self, key: Key, /) -> Any:
        """`cache[key]`: the value, or `KeyError` if it's missing or expired."""

    def __setitem__(self, key: Key, value: Any, /) -> None:
        """`cache[key] = value`: store a value that never expires."""

    def __delitem__(self, key: Key, /) -> None:
        """`del cache[key]`: remove the entry, or raise `KeyError`."""

    def __contains__(self, key: object, /) -> bool:
        """`key in cache`: True if the key holds a live value."""

    def __len__(self) -> int:
        """The number of entries."""

    def __bool__(self) -> bool:
        """True if caching is on, so `if cache:` reads naturally."""

    def __iter__(self) -> Iterator[Key]:
        """Iterate over every key once. A `Cache` yields them in insertion order."""

    def __enter__(self) -> Self:
        """Return the cache, for use in a `with` block."""

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
        /,
    ) -> None:
        """Close the cache."""


@runtime_checkable
class TransactionalCacheLike(CacheLike, Protocol):
    """A `CacheLike` with `transact()`, which `Cache` and `NullCache` both have.

    On a `Cache`, `with cache.transact() as tx:` runs the block's calls on this thread in one write
    transaction; `tx` is the cache. On a `NullCache` it's a no-op that yields the `NullCache`.

    Examples:
        ```python
        from diskcache_oxide import TransactionalCacheLike

        def count(cache: TransactionalCacheLike, path: str) -> None:
            with cache.transact() as tx:
                tx.incr('requests')
                tx.incr(f'requests:{path}')
        ```
    """

    def transact(self, retry: bool = False) -> AbstractContextManager[Self]:
        """A context manager holding a write transaction for the block, yielding the cache."""


@runtime_checkable
class ShardedCacheLike(CacheLike, Protocol):
    """A `CacheLike` with a sharded cache's `transact(*, key, retry=False)`, which `FanoutCache` and
    `NullCache` both have.

    On a `FanoutCache`, `with cache.transact(key='total') as tx:` runs the block's calls on this
    thread for keys on `key`'s shard in one write transaction, and refuses keys on other shards
    (`ValueError`); `tx` is the cache. On a `NullCache` it's a no-op that yields the `NullCache`.
    `key` is keyword-only, so `transact('total')` is a type error rather than a `retry` of 'total'.

    Examples:
        ```python
        from diskcache_oxide import ShardedCacheLike

        def bump(cache: ShardedCacheLike, key: Key) -> None:
            with cache.transact(key=key) as tx:
                tx.set(key, tx.get(key, 0) + 1)
        ```
    """

    def transact(self, *, key: Key, retry: bool = False) -> AbstractContextManager[Self]:
        """A context manager holding a write transaction on `key`'s shard for the block, yielding the cache."""


# The constructor arguments both classes accept (NullCache accepts and ignores them).
DirectoryArg = str | os.PathLike[str] | None
ClockArg = Callable[[], float] | None
