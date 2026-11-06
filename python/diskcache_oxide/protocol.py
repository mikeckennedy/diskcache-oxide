"""`CacheLike`: the interface `Cache`, `FanoutCache` and `NullCache` share, for typing code that accepts either.

`TransactionalCacheLike` adds `transact()`. It's a separate protocol so a cache whose transactions
need more than `retry` (a sharded cache's `transact(key=...)`) can still be a `CacheLike`.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager
from types import TracebackType
from typing import Any, Literal, Protocol, Self, overload, runtime_checkable


@runtime_checkable
class CacheLike(Protocol):
    """What application code can rely on from a `Cache`, a `FanoutCache` or a `NullCache`.

    All three satisfy it, so a factory can return either and callers never need
    to check which one they got. A test keeps the three signatures identical.
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
    def get(self, key: str, default: Any = None, *, expire_time: Literal[False] = False) -> Any: ...
    @overload
    def get(self, key: str, default: Any = None, *, expire_time: Literal[True]) -> tuple[Any, float | None]: ...
    def get(self, key: str, default: Any = None, *, expire_time: bool = False) -> Any:
        """Return the value for `key`, or `default` if it's missing or expired."""

    def set(self, key: str, value: Any, expire: float | None = None) -> bool:
        """Store `value` under `key`, with an optional expiry in seconds."""

    def add(self, key: str, value: Any, expire: float | None = None) -> bool:
        """Store `value` only if `key` is missing or expired. True if it was stored."""

    def delete(self, key: str) -> bool:
        """Remove `key`. True if a live entry was removed."""

    def pop(self, key: str, default: Any = None) -> Any:
        """Remove `key` and return its value, or `default`."""

    def touch(self, key: str, expire: float | None = None) -> bool:
        """Change an existing entry's expiry. True if the entry existed."""

    def incr(self, key: str, delta: float = 1, default: float | None = 0) -> int | float:
        """Add `delta` to the number under `key`, atomically, and return the result."""

    def decr(self, key: str, delta: float = 1, default: float | None = 0) -> int | float:
        """Subtract `delta` from the number under `key`, atomically, and return the result."""

    def iterkeys(self, *, prefix: str | None = None, reverse: bool = False) -> Iterator[str]:
        """Iterate over keys in sorted order, optionally only those starting with `prefix`."""

    def expire(self, *, timeout: float | None = None) -> int:
        """Remove every expired entry. Returns how many were removed."""

    def clear(self, *, timeout: float | None = None) -> int:
        """Remove every entry that existed when the call started. Returns how many."""

    def cull(self, *, timeout: float | None = None) -> int:
        """Remove expired entries, then evict until under `size_limit`. Returns how many."""

    def volume(self) -> int:
        """The size on disk in bytes."""

    def checkpoint(self, *, timeout: float | None = None) -> bool:
        """Empty the write-ahead log. True if it was reset, False if other processes kept it busy."""

    def close(self, *, checkpoint: bool = True) -> None:
        """Close this handle and return its connections."""

    def __getitem__(self, key: str, /) -> Any:
        """`cache[key]`: the value, or `KeyError` if it's missing or expired."""

    def __setitem__(self, key: str, value: Any, /) -> None:
        """`cache[key] = value`: store a value that never expires."""

    def __delitem__(self, key: str, /) -> None:
        """`del cache[key]`: remove the entry, or raise `KeyError`."""

    def __contains__(self, key: object, /) -> bool:
        """`key in cache`: True if the key holds a live value."""

    def __len__(self) -> int:
        """The number of entries."""

    def __bool__(self) -> bool:
        """True if caching is on, so `if cache:` reads naturally."""

    def __iter__(self) -> Iterator[str]:
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
    """

    def transact(self, retry: bool = False) -> AbstractContextManager[Self]:
        """A context manager holding a write transaction for the block, yielding the cache."""


# The constructor arguments both classes accept (NullCache accepts and ignores them).
DirectoryArg = str | os.PathLike[str] | None
ClockArg = Callable[[], float] | None
