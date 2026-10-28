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
revocation still works). Invalidations then still land while caching is off, and nothing stale is
served once it's switched back on. Everything else stays null.

The constructor accepts the same arguments as `Cache` and ignores them, so switching between the
two is a one-line change. Bad argument *types* still raise as they would on `Cache` (`bool`
expire/timeout, non-`str` keys), so code that works with a `NullCache` also works with a real one.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
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


class NullCache:
    """A switched-off cache. See the module docstring for the rule every method follows."""

    __slots__ = ('_cull_limit', '_directory', '_invalidate', '_size_limit', '_timeout')

    def __init__(
        self,
        directory: str | os.PathLike[str] | None = None,
        timeout: float = 60.0,
        *,
        size_limit: int | None = None,
        cull_limit: int | None = None,
        serializer: Literal['msgpack', 'json'] = 'msgpack',
        pickle: bool = False,
        clock: Callable[[], float] | None = None,
        max_connections: int = 8,
        sqlite_cache_size: int | None = None,
        invalidate: CacheLike | None = None,
    ) -> None:
        _check_seconds('timeout', timeout)
        self._directory = None if directory is None else os.fspath(directory)
        self._timeout = float(timeout)
        self._size_limit = DEFAULT_SIZE_LIMIT if size_limit is None else size_limit
        self._cull_limit = DEFAULT_CULL_LIMIT if cull_limit is None else cull_limit
        self._invalidate = invalidate

    def __repr__(self) -> str:
        forwarding = f', invalidate={self._invalidate!r}' if self._invalidate is not None else ''
        return f'NullCache({self._directory!r}{forwarding})'

    # -- attributes ---------------------------------------------------------------------------

    @property
    def directory(self) -> str | None:
        return self._directory

    @property
    def timeout(self) -> float:
        return self._timeout

    @property
    def size_limit(self) -> int:
        return self._size_limit

    @property
    def cull_limit(self) -> int:
        return self._cull_limit

    @property
    def enabled(self) -> bool:
        return False

    # -- reads: nothing is ever present ---------------------------------------------------------

    @overload
    def get(self, key: str, default: Any = None, *, expire_time: Literal[False] = False) -> Any: ...
    @overload
    def get(self, key: str, default: Any = None, *, expire_time: Literal[True]) -> tuple[Any, float | None]: ...
    def get(self, key: str, default: Any = None, *, expire_time: bool = False) -> Any:
        _check_key(key)
        return (default, None) if expire_time else default

    def __getitem__(self, key: str) -> Any:
        _check_key(key)
        raise KeyError(key)

    def __contains__(self, key: object) -> bool:
        return False

    def __len__(self) -> int:
        return 0

    def __bool__(self) -> bool:
        return False

    def __iter__(self) -> Iterator[str]:
        return iter(())

    def volume(self) -> int:
        return 0

    # -- writes: succeed, then expire instantly ---------------------------------------------------

    def set(self, key: str, value: Any, expire: float | None = None) -> bool:
        _check_key(key)
        _check_seconds('expire', expire)
        return True

    def __setitem__(self, key: str, value: Any) -> None:
        self.set(key, value)

    def add(self, key: str, value: Any, expire: float | None = None) -> bool:
        _check_key(key)
        _check_seconds('expire', expire)
        return True

    def touch(self, key: str, expire: float | None = None) -> bool:
        _check_key(key)
        _check_seconds('expire', expire)
        return False

    def incr(self, key: str, delta: int | float = 1, default: int | float | None = 0) -> int | float:
        _check_key(key)
        if default is None:
            raise KeyError(key)
        return default + delta

    def decr(self, key: str, delta: int | float = 1, default: int | float | None = 0) -> int | float:
        return self.incr(key, -delta, default)

    # -- removals: forwarded with invalidate= -----------------------------------------------------

    def delete(self, key: str) -> bool:
        _check_key(key)
        if self._invalidate is not None:
            return self._invalidate.delete(key)
        return False

    def __delitem__(self, key: str) -> None:
        if not self.delete(key) and self._invalidate is None:
            raise KeyError(key)

    def pop(self, key: str, default: Any = None) -> Any:
        _check_key(key)
        if self._invalidate is not None:
            self._invalidate.delete(key)
        return default

    def clear(self) -> int:
        return self._invalidate.clear() if self._invalidate is not None else 0

    def iterkeys(self, *, prefix: str | None = None, reverse: bool = False) -> Iterator[str]:
        if self._invalidate is not None:
            return self._invalidate.iterkeys(prefix=prefix, reverse=reverse)
        return iter(())

    # -- maintenance and lifecycle -------------------------------------------------------------

    def expire(self) -> int:
        return 0

    def cull(self) -> int:
        return 0

    def close(self) -> None:
        # Never closes the `invalidate=` cache: it belongs to whoever created it.
        pass

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None, /
    ) -> None:
        self.close()
