"""Type stubs for the compiled `diskcache_oxide._core` extension (hand-written, checked by stubtest)."""

import os
from types import TracebackType
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager
from typing import Any, Literal, Self, final, overload

# PyO3 generates __all__ from everything the module adds, in the order it adds them.
__all__ = [
    '__version__',
    'BUNDLED_SQLITE_VERSION',
    '_DEBUG_BUILD',
    'Timeout',
    'UnsafeValueError',
    'CorruptValueError',
    'sqlite_version',
    'sqlite_compile_options',
    '_encode',
    '_decode',
    '_sqlite_memory',
    'FormatMismatchError',
    'Cache',
]

__version__: str
BUNDLED_SQLITE_VERSION: str
"""The SQLite version the bundled library was compiled from (the `SQLITE_VERSION` macro)."""
_DEBUG_BUILD: bool
"""True for an unoptimized (debug) build of the extension."""

class Timeout(TimeoutError):
    """The database stayed locked for longer than the cache's timeout."""

class UnsafeValueError(TypeError):
    """A pickle=False cache read a value that was stored with pickle."""

class CorruptValueError(ValueError):
    """A stored value couldn't be decoded: an unknown mode, or bytes that don't parse."""

class FormatMismatchError(RuntimeError):
    """The cache directory holds a database in a format this version doesn't support."""

def sqlite_version() -> str:
    """The SQLite version reported at runtime by `sqlite3_libversion()`."""

def sqlite_compile_options() -> list[str]:
    """The compile-time options of the linked SQLite (`PRAGMA compile_options`)."""

def _encode(value: object, serializer: str = 'msgpack', pickle: bool = False) -> tuple[int, bytes | int | float]:
    """Test hook: `_core._encode(value, serializer='msgpack', pickle=False) -> (mode, payload)`."""

def _sqlite_memory(reset: bool = False) -> tuple[int, int]:
    """Test hook: the bundled SQLite's (bytes in use, high-water mark), optionally resetting the mark."""

def _decode(mode: int, payload: bytes | int | float, allow_pickle: bool = False) -> object:
    """Test hook: `_core._decode(mode, payload, allow_pickle=False)`, with the payload a `bytes`,
    `int` or `float` standing for a BLOB, INTEGER or REAL column."""

@final
class Cache:
    """A disk cache backed by SQLite, shared safely across threads and processes.

    diskcache-compatible API; see `tests/DIVERGENCES.md` for the deliberate differences.
    """

    def __new__(
        cls,
        directory: str | os.PathLike[str],
        timeout: float | None = None,
        *,
        size_limit: int | None = None,
        cull_limit: int | None = None,
        serializer: str = 'msgpack',
        pickle: bool = False,
        clock: Callable[[], float] | None = None,
        max_connections: int | None = None,
        sqlite_cache_size: int | None = None,
        wal_size_limit: int | None = None,
    ) -> Cache: ...
    @property
    def directory(self) -> str: ...
    @property
    def timeout(self) -> float: ...
    @property
    def size_limit(self) -> int: ...
    @property
    def cull_limit(self) -> int: ...
    @property
    def enabled(self) -> bool: ...
    @overload
    def get(self, key: str, default: Any = None, *, expire_time: Literal[False] = False) -> Any: ...
    @overload
    def get(self, key: str, default: Any = None, *, expire_time: Literal[True]) -> tuple[Any, float | None]: ...
    def set(self, key: str, value: Any, expire: float | None = None) -> bool: ...
    def add(self, key: str, value: Any, expire: float | None = None) -> bool: ...
    def delete(self, key: str) -> bool: ...
    def pop(self, key: str, default: Any = None) -> Any: ...
    def touch(self, key: str, expire: float | None = None) -> bool: ...
    # Defaults are delta=1, default=0; PyO3 reports them as `...` at runtime.
    def incr(self, key: str, delta: int | float = ..., default: int | float | None = ...) -> int | float: ...
    def decr(self, key: str, delta: int | float = ..., default: int | float | None = ...) -> int | float: ...
    def iterkeys(self, *, prefix: str | None = None, reverse: bool = False) -> Iterator[str]: ...
    def expire(self) -> int: ...
    def clear(self) -> int: ...
    def cull(self) -> int: ...
    def volume(self) -> int: ...
    def close(self) -> None: ...
    def _wal_stats(self) -> dict[str, int]:
        """Test hook: forced WAL checkpoint counters (checks, attempts, successes, busy, errors, no_slot, max_pause_us, max_passive_us, max_truncate_us, slow, last_wal_bytes)."""
    def _hold_write_transaction(self) -> AbstractContextManager[None]:
        """Test hook until transact() lands: holds a write transaction until the block exits."""
    def __getitem__(self, key: str, /) -> Any: ...
    def __setitem__(self, key: str, value: Any, /) -> None: ...
    def __delitem__(self, key: str, /) -> None: ...
    def __contains__(self, key: object, /) -> bool: ...
    def __len__(self) -> int: ...
    def __bool__(self) -> bool: ...
    def __iter__(self) -> Iterator[str]: ...
    def __enter__(self) -> Self: ...
    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None, /) -> None: ...
