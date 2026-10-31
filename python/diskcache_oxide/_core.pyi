"""Type stubs for the compiled `diskcache_oxide._core` extension (hand-written, checked by tests/unit/test_stubs.py)."""

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
    'ForeignValueError',
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

class ForeignValueError(CorruptValueError):
    """A stored value this build can't read but another might: an unknown mode, tag or extension (a newer version wrote it), or a time zone missing from this host."""

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

    Every thread and process that opens the same directory shares one cache. The API follows
    `diskcache.Cache`; the deliberate differences are listed in the Divergences guide.

    Args:
        directory: The cache directory. It's created (owner-only, `0700`) if it doesn't exist.
        timeout: Seconds a write waits for another process's lock before raising `Timeout`
            (default 60).
        size_limit: The disk budget in bytes, database plus value files (default 1 GiB, minimum
            1 MiB). Stored in the cache, so it applies to every process.
        cull_limit: How many least-recently-stored entries a `set()` evicts when the cache is over
            `size_limit` (default 10).
        serializer: `'msgpack'` (the default) or `'json'`, for values that aren't plain `str` or
            `bytes`.
        pickle: Allow pickle for values the serializer can't represent exactly. Without it, such
            values raise `TypeError` and pickled values are never read.
        clock: A function returning the current time in seconds, used instead of the native clock.
            Tests pass `clock=lambda: time.time()` to follow a patched `time.time`.
        max_connections: The most SQLite connections this process opens for the directory, shared
            by every handle on it (default 8).
        sqlite_cache_size: SQLite's page cache per connection, in 4 KiB pages (default 256, 1 MiB).
        wal_size_limit: The write-ahead log is trimmed back to half this many bytes whenever it
            resets (default 64 MiB, minimum 1 MiB).
        wal_checkpoint: `'manual'` (the default) leaves checkpoints to `checkpoint()` and
            `close()`. `'auto'` also checkpoints after writes once the log is over
            `wal_size_limit`.
        on_decode_error: What a read does with a stored value it can't decode. `'raise'` (the
            default) raises. `'miss'` treats it as a miss, logs a warning on the `diskcache_oxide`
            logger, and deletes the row if no build could ever read it (a class removed since it
            was pickled, malformed bytes). A row another version or host might read
            (`ForeignValueError`) is left in place. A callable does the same as `'miss'`, but is
            called with `(key, exc)` instead of the warning, after the row is dealt with; whatever
            it raises is logged. `UnsafeValueError`, `Timeout`, `MemoryError` and other errors that
            aren't about the stored bytes always raise. Covers `get`, `cache[key]` and `pop`, which
            decodes before it deletes, so it keeps a row it can't read. Under `'raise'`, `pop` still
            deletes a value no build could read, then raises.

    Raises:
        ValueError: A setting is out of range, such as a `size_limit` under 1 MiB.
        TypeError: An argument has the wrong type, including a `bool` for `timeout`.
        FormatMismatchError: The directory holds a database in a format this version can't read.
        Timeout: Another process held the database locked for longer than `timeout`.
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
        wal_checkpoint: Literal['manual', 'auto'] = 'manual',
        on_decode_error: Literal['raise', 'miss'] | Callable[[str, Exception], object] = ...,
    ) -> Cache: ...
    @property
    def directory(self) -> str:
        """The cache directory, as given to the constructor."""
    @property
    def timeout(self) -> float:
        """Seconds a write waits for another process's lock before raising `Timeout`."""
    @property
    def size_limit(self) -> int:
        """The disk budget in bytes. A `set()` that finds the cache over it evicts entries."""
    @property
    def cull_limit(self) -> int:
        """The most entries a single `set()` evicts when the cache is over `size_limit`."""
    @property
    def enabled(self) -> bool:
        """Always True for a real cache; False for a `NullCache`."""
    @overload
    def get(self, key: str, default: Any = None, *, expire_time: Literal[False] = False) -> Any:
        """Return the value stored for `key`, or `default` if it's missing or expired.

        Reads never write, so any number of threads and processes can read at once.

        Args:
            key: The key to look up.
            default: Returned when the key is missing or expired.
            expire_time: Also return the entry's expiry time.

        Returns:
            The value, or `default`. With `expire_time=True`, a `(value, expire_time)` tuple, where
            `expire_time` is a Unix timestamp, or None for an entry that never expires (or a miss).

        Raises:
            UnsafeValueError: The value was stored with pickle and this cache has `pickle=False`.
            CorruptValueError: The stored bytes can't be decoded.
            Timeout: Reading a large value kept losing to concurrent overwrites until `timeout`.
        """
    @overload
    def get(self, key: str, default: Any = None, *, expire_time: Literal[True]) -> tuple[Any, float | None]: ...
    def set(self, key: str, value: Any, expire: float | None = None) -> bool:
        """Store `value` under `key`, replacing any existing value.

        Values of 64 KiB or more (after encoding) are stored in their own file.

        Args:
            key: The key.
            value: The value: `str`, `bytes`, numbers, `bool`, `None`, and lists, tuples, dicts,
                sets and datetimes of those, or anything picklable with `pickle=True`.
            expire: Seconds until the entry expires, or None for never.

        Returns:
            True.

        Raises:
            TypeError: The value can't be stored exactly without pickle, or `expire` is a `bool`.
            Timeout: Another process held the write lock for longer than `timeout`.
        """
    def add(self, key: str, value: Any, expire: float | None = None) -> bool:
        """Store `value` only if `key` is missing or expired.

        Atomic across threads and processes: when several callers race to add one key, exactly one
        succeeds. That makes it a simple way to claim a job or take a lock.

        Args:
            key: The key.
            value: The value, as for `set()`.
            expire: Seconds until the entry expires, or None for never.

        Returns:
            True if the value was stored, False if the key already held a live value.

        Raises:
            TypeError: The value can't be stored exactly without pickle, or `expire` is a `bool`.
            Timeout: Another process held the write lock for longer than `timeout`.
        """
    def delete(self, key: str) -> bool:
        """Remove `key`.

        Returns:
            True if a live entry was removed, False if the key was missing or expired.

        Raises:
            Timeout: Another process held the write lock for longer than `timeout`.
        """
    def pop(self, key: str, default: Any = None) -> Any:
        """Remove `key` and return its value, delivering it to exactly one caller.

        Reads and decodes the value first, as `get` does, then deletes exactly that stored version,
        so a value it can't read stays for whoever can. A `pop` that loses that delete to other
        writers 8 times falls back to removing the row first and decoding afterwards. Only then can
        a failed decode cost the value: one from a newer version, or any error from a pickle hook.
        `timeout` covers the whole call.

        Args:
            key: The key.
            default: Returned when the key is missing or expired, or its value file is missing.

        Returns:
            The removed value, or `default`.

        Raises:
            UnsafeValueError: The value was stored with pickle and this cache has `pickle=False`.
                The row stays.
            ForeignValueError: `on_decode_error='raise'`, and another version or host might read
                the value. The row stays.
            CorruptValueError: `on_decode_error='raise'`, and no build could read the value. The row
                is deleted first.
            Timeout: Another process held the write lock for longer than `timeout`, or `timeout` ran
                out after losing a race to another writer. This `pop` deletes nothing, and whatever
                the other writer left stays.
        """
    def touch(self, key: str, expire: float | None = None) -> bool:
        """Change the expiry of an existing entry without rewriting its value.

        Args:
            key: The key.
            expire: Seconds from now until the entry expires, or None to make it never expire.

        Returns:
            True if the entry existed and was updated, False if it was missing or expired.

        Raises:
            Timeout: Another process held the write lock for longer than `timeout`.
        """
    # Defaults are delta=1, default=0; PyO3 reports them as `...` at runtime.
    def incr(self, key: str, delta: int | float = ..., default: int | float | None = ...) -> int | float:
        """Add `delta` to the number stored under `key`, atomically, and return the result.

        Atomic across threads and processes, so concurrent increments are never lost. The entry
        keeps its existing expiry.

        Args:
            key: The key.
            delta: The amount to add (default 1).
            default: The starting value for a missing or expired key (default 0). Pass None to
                raise `KeyError` instead.

        Returns:
            The new value.

        Raises:
            KeyError: The key is missing and `default` is None.
            TypeError: The stored value isn't a number.
            Timeout: Another process held the write lock for longer than `timeout`.
        """
    def decr(self, key: str, delta: int | float = ..., default: int | float | None = ...) -> int | float:
        """Subtract `delta` from the number stored under `key`, atomically. See `incr()`.

        Returns:
            The new value.
        """
    def iterkeys(self, *, prefix: str | None = None, reverse: bool = False) -> Iterator[str]:
        """Iterate over keys in sorted order, optionally only those starting with `prefix`.

        A prefix is an indexed range scan, so it costs time in proportion to the matches, not the
        size of the cache. Keys are fetched a page at a time with no lock held in between, so
        deleting keys while iterating is safe. Expired entries not yet removed are included. Like a
        generator, an iterator serves one thread at a time: `next()` raises `ValueError` while
        another thread is inside it.

        Args:
            prefix: Only yield keys that start with this string.
            reverse: Yield keys in descending order.

        Returns:
            An iterator of keys.
        """
    def expire(self, *, timeout: float | None = None) -> int:
        """Remove every expired entry, and its value file.

        Works in chunks and lets other writers in between them.

        Args:
            timeout: Seconds each chunk may wait for the write lock (another thread's or another
                process's), for this call only (default: the cache's `timeout`). It bounds each
                wait, not the whole call.

        Returns:
            How many entries were removed.
        """
    def clear(self, *, timeout: float | None = None) -> int:
        """Remove every entry that existed when the call started.

        Works in chunks and lets other writers in between them. Writes that land while it runs,
        including overwrites of existing keys, are kept.

        Args:
            timeout: Seconds each chunk may wait for the write lock (another thread's or another
                process's), for this call only (default: the cache's `timeout`). It bounds each
                wait, not the whole call.

        Returns:
            How many entries were removed.
        """
    def cull(self, *, timeout: float | None = None) -> int:
        """Remove expired entries, then evict the least recently stored until under `size_limit`.

        Args:
            timeout: Seconds each chunk may wait for the write lock (another thread's or another
                process's), for this call only (default: the cache's `timeout`). It bounds each
                wait, not the whole call.

        Returns:
            How many entries were removed.
        """
    def volume(self) -> int:
        """The cache's size on disk in bytes: the database plus value files."""
    def checkpoint(self, *, timeout: float | None = None) -> bool:
        """Copy the write-ahead log into the database and truncate it to zero.

        With readers in other processes, a burst of writes can leave the log much larger than the
        database. Call this when a short pause suits the app: at shutdown, in a maintenance job,
        or after a large `clear()`. `close()` also does it for a log left large.

        Args:
            timeout: Seconds to keep retrying while other processes are busy (default: the cache's
                `timeout`).

        Returns:
            True if the log was reset, False if readers or writers in other processes kept it
            busy. It never raises for a busy database.
        """
    def close(self, *, checkpoint: bool = True) -> None:
        """Close this handle and return its connections.

        When the last handle on a directory in the process closes, every connection to it closes,
        whichever threads used them. Using the cache again afterwards reopens it.

        Args:
            checkpoint: If this is the process's last handle and the write-ahead log is over half
                of `wal_size_limit`, checkpoint it first, waiting at most 1 second. A small log
                costs nothing extra.
        """
    def _wal_stats(self) -> dict[str, int]:
        """Test hook: forced WAL checkpoint counters (checks, attempts, successes, busy, errors, no_slot, max_pause_us, max_passive_us, max_truncate_us, slow, last_wal_bytes)."""
    def _hold_write_transaction(self) -> AbstractContextManager[None]:
        """Test hook until transact() lands: holds a write transaction until the block exits."""
    def __getitem__(self, key: str, /) -> Any:
        """`cache[key]`: the value, or `KeyError` if it's missing or expired."""
    def __setitem__(self, key: str, value: Any, /) -> None:
        """`cache[key] = value`: store a value that never expires, like `set()`."""
    def __delitem__(self, key: str, /) -> None:
        """`del cache[key]`: remove the entry, or raise `KeyError` if it's missing or expired."""
    def __contains__(self, key: object, /) -> bool:
        """`key in cache`: True if the key holds a live (unexpired) value."""
    def __len__(self) -> int:
        """The number of entries, including expired entries not yet removed."""
    def __bool__(self) -> bool:
        """Always True, even when empty, so `if cache:` means "caching is on"."""
    def __iter__(self) -> Iterator[str]:
        """Iterate over keys in insertion order. An overwrite keeps its key's place.

        As with `iterkeys()`, `next()` raises `ValueError` while another thread is inside it.
        """
    def __enter__(self) -> Self:
        """Return the cache, for `with Cache(...) as cache:`."""
    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None, /) -> None:
        """Close the cache, as `close()` does."""
