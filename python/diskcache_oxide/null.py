"""`NullCache`: a cache that's switched off, so app code needs no `if cache is None` checks.

**The rule:** a `NullCache` behaves like a real cache in which every entry expires the instant it
is written and is swept straight away. Every result follows from that one sentence:

- `get` and `pop` return the default; `__getitem__` raises `KeyError`;
- `set` and `add` return True (the key was absent, so an add "succeeds");
- `incr`/`decr` return `default ± delta`, and raise `KeyError` when `default=None`;
- `delete`, `touch` and `in` are False;
- `len`, `volume`, `clear`, `expire` and `cull` are 0;
- iteration yields nothing.

It's falsy and its `enabled` is False, so `if cache:` reads as "caching is on".

**`invalidate=`.** Passing `invalidate=real_cache` forwards the operations that *remove* data to
that cache: `delete`, `pop` (as a delete), `clear`, and `iterkeys` (so enumerate-then-delete
revocation still works). Invalidation by removal then still lands while caching is off. Everything
else stays null, so invalidation that writes doesn't: a `set` that overwrites the stale value, or an
`incr` that bumps a version key, is dropped, and the old value is served again once caching is back
on.

**`transact()`.** It's a no-op context manager that yields the `NullCache`: there's nothing to
commit or roll back. It never opens a transaction on an `invalidate=` cache, since a switched-off
cache must never take a write lock or raise `Timeout`. So removals forwarded inside the block land
at once, and an exception in the block doesn't undo them.

The constructor accepts the same arguments as `Cache` and ignores them, so switching between the
two is a one-line change. Bad argument *types* still raise as they would on `Cache` (`bool`
expire/timeout, non-`str` keys), so code that works with a `NullCache` also works with a real one.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, nullcontext
from types import TracebackType
from typing import Any, Literal, Self, overload

from diskcache_oxide.protocol import CacheLike

DEFAULT_SIZE_LIMIT = 2**30
DEFAULT_CULL_LIMIT = 10


def _check_key(key: object) -> None:
    if type(key) is not str:
        raise TypeError(f'cache keys must be str, not {type(key).__name__}')


def _check_seconds(name: str, value: object) -> None:
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError(f'{name} must be a number of seconds or None, not {type(value).__name__}')


def _check_timeout(value: object) -> None:
    """As `Cache` validates a timeout: a number of seconds (not `bool`), finite and not negative."""
    _check_seconds('timeout', value)
    # float() first, as `Cache` converts: an int too large for a float raises OverflowError.
    if isinstance(value, int | float) and not 0 <= float(value) < float('inf'):
        raise ValueError(f'timeout must be a non-negative number of seconds, got {value}')


def _check_decode_policy(value: Any) -> None:
    """As `Cache` validates `on_decode_error`."""
    if value is None:
        return
    if isinstance(value, str):
        if value not in ('raise', 'miss'):
            raise ValueError(f"on_decode_error must be 'raise', 'miss' or a callable, got {value!r}")
    elif not callable(value):
        raise TypeError(f"on_decode_error must be 'raise', 'miss' or a callable, not {type(value).__name__}")


def _check_durable(value: object) -> None:
    """As `Cache` validates `durable`: a `bool`, not something truthy."""
    if not isinstance(value, bool):
        raise TypeError(f'durable must be a bool, not {type(value).__name__}')


class NullCache:
    """A switched-off cache, so app code needs no `if cache is None` checks.

    It behaves like a real cache in which every entry expires the instant it's written and is
    swept straight away: reads miss, writes succeed, and it's always empty. It's falsy, and its
    `enabled` is False.

    Args:
        directory: Accepted and ignored, like every other `Cache` argument, so switching between
            the two is a one-line change.
        invalidate: A real cache to forward removals to (`delete`, `pop`, `clear` and `iterkeys`),
            so invalidation by removal still lands while caching is off. Writes aren't forwarded:
            an overwriting `set` or a version-bump `incr` is dropped, and the old value is served
            once caching is back on.

    Examples:
        ```python
        from diskcache_oxide import Cache, NullCache

        cache = NullCache()
        cache.set('k', 1)        # True, and nothing is stored
        cache.get('k', 'miss')   # 'miss'
        cache.add('job', 1)      # True: don't claim jobs on a cache that might be off

        real = Cache('/var/cache/my-app')
        off = NullCache(invalidate=real)
        off.delete('user:42')    # removed from `real`; off.get('user:42') is still None
        ```
    """

    __slots__ = ('_cull_limit', '_directory', '_invalidate', '_size_limit', '_timeout')

    def __init__(
        self,
        directory: str | os.PathLike[str] | None = None,
        timeout: float | None = None,
        *,
        size_limit: int | None = None,
        cull_limit: int | None = None,
        serializer: Literal['msgpack', 'json'] = 'msgpack',
        pickle: bool = False,
        clock: Callable[[], float] | None = None,
        max_connections: int = 8,
        sqlite_cache_size: int | None = None,
        wal_size_limit: int | None = None,
        wal_checkpoint: Literal['manual', 'auto'] = 'manual',
        on_decode_error: Literal['raise', 'miss'] | Callable[[str, Exception], object] | None = None,
        durable: bool = False,
        invalidate: CacheLike | None = None,
    ) -> None:
        _check_timeout(timeout)
        _check_decode_policy(on_decode_error)
        _check_durable(durable)
        self._directory = None if directory is None else os.fspath(directory)
        self._timeout = 60.0 if timeout is None else float(timeout)
        self._size_limit = DEFAULT_SIZE_LIMIT if size_limit is None else size_limit
        self._cull_limit = DEFAULT_CULL_LIMIT if cull_limit is None else cull_limit
        self._invalidate = invalidate

    def __repr__(self) -> str:
        """Shows the directory and any `invalidate=` cache."""
        forwarding = f', invalidate={self._invalidate!r}' if self._invalidate is not None else ''
        return f'NullCache({self._directory!r}{forwarding})'

    # -- attributes ---------------------------------------------------------------------------

    @property
    def directory(self) -> str | None:
        """The directory passed to the constructor, or None. Nothing is created there."""
        return self._directory

    @property
    def timeout(self) -> float:
        """The timeout passed to the constructor (default 60). Unused."""
        return self._timeout

    @property
    def size_limit(self) -> int:
        """The size limit passed to the constructor (default 1 GiB). Unused."""
        return self._size_limit

    @property
    def cull_limit(self) -> int:
        """The cull limit passed to the constructor (default 10). Unused."""
        return self._cull_limit

    @property
    def enabled(self) -> bool:
        """Always False: caching is off."""
        return False

    # -- reads: nothing is ever present ---------------------------------------------------------

    @overload
    def get(self, key: str, default: Any = None, *, expire_time: Literal[False] = False) -> Any: ...
    @overload
    def get(self, key: str, default: Any = None, *, expire_time: Literal[True]) -> tuple[Any, float | None]: ...
    def get(self, key: str, default: Any = None, *, expire_time: bool = False) -> Any:
        """Return `default`, as every entry has already expired. With `expire_time=True`, `(default, None)`."""
        _check_key(key)
        return (default, None) if expire_time else default

    def __getitem__(self, key: str, /) -> Any:
        """Always raises `KeyError`, as every entry has already expired."""
        _check_key(key)
        raise KeyError(key)

    def __contains__(self, key: object, /) -> bool:
        """Always False."""
        return False

    def __len__(self) -> int:
        """Always 0."""
        return 0

    def __bool__(self) -> bool:
        """Always False, so `if cache:` means "caching is on"."""
        return False

    def __iter__(self) -> Iterator[str]:
        """Yields nothing."""
        return iter(())

    def volume(self) -> int:
        """Always 0: a `NullCache` uses no disk."""
        return 0

    def checkpoint(self, *, timeout: float | None = None) -> bool:
        """Always True: there is no write-ahead log to reset."""
        _check_timeout(timeout)
        return True

    # -- writes: succeed, then expire instantly ---------------------------------------------------

    def set(self, key: str, value: Any, expire: float | None = None) -> bool:
        """Return True without storing anything: the write succeeds, then expires instantly."""
        _check_key(key)
        _check_seconds('expire', expire)
        return True

    def __setitem__(self, key: str, value: Any, /) -> None:
        """Store nothing, as `set()` does."""
        self.set(key, value)

    def add(self, key: str, value: Any, expire: float | None = None) -> bool:
        """Return True without storing anything: the key is always absent, so the add succeeds.

        With caching off, every caller's `add()` succeeds, so don't rely on it for job claims or locks.
        A `Cache(durable=True)` does the opposite when a write fails: its `add()` returns False.
        """
        _check_key(key)
        _check_seconds('expire', expire)
        return True

    def touch(self, key: str, expire: float | None = None) -> bool:
        """Always False: the key never exists."""
        _check_key(key)
        _check_seconds('expire', expire)
        return False

    def incr(self, key: str, delta: float = 1, default: float | None = 0) -> int | float:
        """Return `default + delta`, since every count starts again from `default`.

        Raises:
            KeyError: `default` is None.
        """
        _check_key(key)
        if default is None:
            raise KeyError(key)
        return default + delta

    def decr(self, key: str, delta: float = 1, default: float | None = 0) -> int | float:
        """Return `default - delta`. See `incr()`."""
        return self.incr(key, -delta, default)

    # -- removals: forwarded with invalidate= -----------------------------------------------------

    def delete(self, key: str) -> bool:
        """False, or with `invalidate=`, delete the key from that cache and return its result."""
        _check_key(key)
        if self._invalidate is not None:
            return self._invalidate.delete(key)
        return False

    def __delitem__(self, key: str, /) -> None:
        """Raise `KeyError`, or with `invalidate=`, delete the key from that cache."""
        if not self.delete(key) and self._invalidate is None:
            raise KeyError(key)

    def pop(self, key: str, default: Any = None) -> Any:
        """Return `default`. With `invalidate=`, also delete the key from that cache."""
        _check_key(key)
        if self._invalidate is not None:
            self._invalidate.delete(key)
        return default

    def clear(self, *, timeout: float | None = None) -> int:
        """0, or with `invalidate=`, clear that cache (passing `timeout` on) and return its count."""
        _check_timeout(timeout)
        if self._invalidate is None:
            return 0
        # Passed on only when given, so an `invalidate=` cache without the keyword still works.
        return self._invalidate.clear() if timeout is None else self._invalidate.clear(timeout=timeout)

    def iterkeys(self, *, prefix: str | None = None, reverse: bool = False) -> Iterator[str]:
        """Yield nothing, or with `invalidate=`, that cache's keys, so list-then-delete still works."""
        if self._invalidate is not None:
            return self._invalidate.iterkeys(prefix=prefix, reverse=reverse)
        return iter(())

    # -- transactions: nothing to hold ------------------------------------------------------------

    def transact(self, retry: bool = False, *, key: str | None = None) -> AbstractContextManager[Self]:
        """A no-op context manager that yields this `NullCache`.

        It never opens a transaction on an `invalidate=` cache: removals forwarded inside the block
        land at once, and an exception in the block doesn't undo them.

        Args:
            retry: Accepted for `Cache.transact()`'s signature; it must be a `bool`.
            key: Accepted so a switched-off sharded cache's `transact(key=...)` works too; it must be
                a `str` or None.

        Raises:
            TypeError: `retry` isn't a `bool`, or `key` isn't a `str` or None.
        """
        if not isinstance(retry, bool):
            raise TypeError(f'retry must be a bool, not {type(retry).__name__}')
        if key is not None:
            _check_key(key)
        return nullcontext(self)

    # -- maintenance and lifecycle -------------------------------------------------------------

    def expire(self, *, timeout: float | None = None) -> int:
        """Always 0."""
        _check_timeout(timeout)
        return 0

    def cull(self, *, timeout: float | None = None) -> int:
        """Always 0."""
        _check_timeout(timeout)
        return 0

    def close(self, *, checkpoint: bool = True) -> None:
        """Do nothing. An `invalidate=` cache is never closed: it belongs to whoever created it."""
        # Never closes the `invalidate=` cache: it belongs to whoever created it.

    def __enter__(self) -> Self:
        """Return the cache, for `with NullCache() as cache:`."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
        /,
    ) -> None:
        """Do nothing, as `close()` does."""
        self.close()
