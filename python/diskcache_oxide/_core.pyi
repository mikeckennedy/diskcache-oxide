"""Type stubs for the compiled `diskcache_oxide._core` extension (hand-written, checked by tests/unit/test_stubs.py)."""

import os
from collections.abc import Callable, Iterator
from types import TracebackType
from typing import Any, Literal, Self, final, overload

# PyO3 generates __all__ from everything the module adds, in the order it adds them.
__all__ = [  # noqa: RUF022 (PyO3's order, checked by test_stubs.py)
    '__version__',
    'BUNDLED_SQLITE_VERSION',
    '_DEBUG_BUILD',
    'Timeout',
    'TransactionBusy',
    'UnsafeValueError',
    'CorruptValueError',
    'ForeignValueError',
    'sqlite_version',
    'sqlite_compile_options',
    '_encode',
    '_decode',
    '_sqlite_memory',
    '_gate_stats',
    '_sqlite_error',
    'FormatMismatchError',
    'LayoutMismatchError',
    'Cache',
    'FanoutCache',
    'Transaction',
]

__version__: str
BUNDLED_SQLITE_VERSION: str
"""The SQLite version the bundled library was compiled from (the `SQLITE_VERSION` macro)."""
_DEBUG_BUILD: bool
"""True for an unoptimized (debug) build of the extension."""

class Timeout(TimeoutError):
    """A lock stayed held, or a fork stayed pending, for longer than the cache's timeout."""

class TransactionBusy(Timeout):
    """A thread holding one cache's transaction couldn't begin another's, because another thread holds its write
    lock. It fails at once rather than waiting, since the holder could be waiting on a lock this thread holds."""

class UnsafeValueError(TypeError):
    """A pickle=False cache read a value that was stored with pickle."""

class CorruptValueError(ValueError):
    """A stored value couldn't be decoded: an unknown mode, or bytes that don't parse."""

class ForeignValueError(CorruptValueError):
    """A stored value this build can't read but another might: an unknown mode, tag or extension (a newer version wrote
    it), or a time zone missing from this host."""

class FormatMismatchError(RuntimeError):
    """The cache directory holds a database in a format this version doesn't support."""

class LayoutMismatchError(FormatMismatchError, ValueError):
    """The directory's layout doesn't match how it was opened: a plain `Cache` on a `FanoutCache` root or shard, a
    `FanoutCache` on a plain `Cache`'s directory, a shard opened as another index or count than it records, or a
    `shards=` that differs from the stored count. Also a `ValueError`, which is what these raised before."""

def sqlite_version() -> str:
    """The SQLite version reported at runtime by `sqlite3_libversion()`."""

def sqlite_compile_options() -> list[str]:
    """The compile-time options of the linked SQLite (`PRAGMA compile_options`)."""

def _encode(value: object, serializer: str = 'msgpack', pickle: bool = False) -> tuple[int, bytes | int | float]:
    """Test hook: `_core._encode(value, serializer='msgpack', pickle=False) -> (mode, payload)`."""

def _gate_stats() -> dict[str, Any]:
    """Test hook: the fork gate's state and counters (pending, holders, parked, fork_timeouts, deferred)."""

def _sqlite_error(code: int) -> BaseException:
    """Test hook: the exception a SQLite result code becomes, as a store call would raise it."""

def _sqlite_memory(reset: bool = False) -> tuple[int, int]:
    """Test hook: the bundled SQLite's (bytes in use, high-water mark), optionally resetting the mark."""

def _decode(mode: int, payload: bytes | float, allow_pickle: bool = False) -> object:
    """Test hook: `_core._decode(mode, payload, allow_pickle=False)`, with the payload a `bytes`,
    `int` or `float` standing for a BLOB, INTEGER or REAL column."""

@final
class Cache:
    """A disk cache backed by SQLite, shared safely across threads and processes.

    Every thread and process that opens the same directory shares one cache. The API follows
    `diskcache.Cache`; the deliberate differences are listed in the Divergences guide.

    Args:
        directory: The cache directory. It's created (owner-only, `0700`) if it doesn't exist.
        timeout: Seconds a call waits for a lock or a pending fork before raising `Timeout`
            (default 60). It has to exceed work in flight plus a fork: see the Concurrency and
            Forks guide.
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
            default, unless `durable=True`, where it's `'miss'`) raises. `'miss'` treats it as a miss, logs a warning
            on the `diskcache_oxide`
            logger, and deletes the row if no build could ever read it (a class removed since it
            was pickled, malformed bytes). A row another version or host might read
            (`ForeignValueError`) is left in place. A callable does the same as `'miss'`, but is
            called with `(key, exc)` instead of the warning, after the row is dealt with; whatever
            it raises is logged. `UnsafeValueError`, `Timeout`, `MemoryError` and other errors that
            aren't about the stored bytes raise, though under `durable=True` a `Timeout`, `OSError`
            or `RecursionError` from a pickle hook is a miss, with the row kept, and so is a
            `RecursionError` from a value too deep for this thread's stack. Covers `get`, `cache[key]` and `pop`, which
            decodes before it deletes, so it keeps a row it can't read. Under `'raise'`, `pop` still
            deletes a value no build could read, then raises.
        durable: Keep the cache from taking a request down, for an app whose every caller can fall
            back to its source of truth (default False). An outage on a single-key call (a `Timeout`,
            a full disk, an I/O error, a damaged database) doesn't raise: `get`, `pop` and `in` give
            the default or False, `set`, `add`, `touch` and `delete` give False, and `incr` and `decr`
            give `default + delta`. It's reported on the `diskcache_oxide` logger, at most once a
            minute per kind of failure. Bugs still raise (`TypeError`, `ValueError`,
            `UnsafeValueError`), as do `MemoryError` and `KeyboardInterrupt`, a `RecursionError` from
            encoding a value too deep for this thread's stack, the constructor, admin
            calls (`len`, `volume`, `clear`, `expire`, `cull`, `checkpoint`, iteration) and anything
            inside a transaction held on this cache. This isn't durability in the database sense:
            this option can drop writes, and a dropped `set`, `delete`, `pop` or `incr` means stale
            data until the entry expires, or forever if it has no expiry. See "A cache that can't
            take a request down" in the Getting Started guide before you use it.

    Raises:
        ValueError: A setting is out of range, such as a `size_limit` under 1 MiB.
        TypeError: An argument has the wrong type, including a `bool` for `timeout`.
        FormatMismatchError: The directory holds a database in a format this version can't read.
        LayoutMismatchError: The directory is a `FanoutCache` root or one of its shards. Also a
            `ValueError`, and a `FormatMismatchError`.
        Timeout: Another process held the database locked, or a fork stayed pending, for longer
            than `timeout`.
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
        on_decode_error: Literal['raise', 'miss'] | Callable[[str, Exception], object] | None = None,
        durable: bool = False,
    ) -> Cache: ...
    @property
    def directory(self) -> str:
        """The cache directory, as given to the constructor."""
    @property
    def timeout(self) -> float:
        """Seconds a call waits for a lock or a pending fork before raising `Timeout`."""
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
            Timeout: Reading a large value kept losing to concurrent overwrites until `timeout`, or
                a pending fork outlasted it. `durable=True` returns `default` instead.
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
            True. With `durable=True`, False if the write failed and was dropped: whatever was
            stored under `key` stays, and can be served stale.

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
            True if the value was stored, False if the key already held a live value. With
            `durable=True`, also False if the write failed, so a claim fails closed. A `NullCache`
            returns True, so use a non-durable handle for claims and locks.

        Raises:
            TypeError: The value can't be stored exactly without pickle, or `expire` is a `bool`.
            Timeout: Another process held the write lock for longer than `timeout`.
        """
    def delete(self, key: str) -> bool:
        """Remove `key`.

        Returns:
            True if a live entry was removed, False if the key was missing or expired. With
            `durable=True`, also False if the delete failed: that invalidation is lost, and the
            entry can be served stale until it expires, or forever if it has no expiry. The log
            line is the only signal.

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
            The removed value, or `default`. With `durable=True`, a `pop` whose delete fails returns
            `default`, never the value, which could otherwise be delivered twice. The row stays.

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
            True if the entry existed and was updated, False if it was missing or expired, or with
            `durable=True`, if the call failed.

        Raises:
            Timeout: Another process held the write lock for longer than `timeout`.
        """
    # Defaults are delta=1, default=0; PyO3 reports them as `...` at runtime.
    def incr(self, key: str, delta: float = ..., default: float | None = ...) -> int | float:
        """Add `delta` to the number stored under `key`, atomically, and return the result.

        Atomic across threads and processes, so concurrent increments are never lost. The entry
        keeps its existing expiry.

        Args:
            key: The key.
            delta: The amount to add (default 1).
            default: The starting value for a missing or expired key (default 0). Pass None to
                raise `KeyError` instead.

        Returns:
            The new value. With `durable=True`, `default + delta` if the call failed, whatever was
            stored, so a rate limiter fails open. Don't use that number as an id: two callers can
            get the same one.

        Raises:
            KeyError: The key is missing and `default` is None, or with `durable=True`, the call
                failed and `default` is None.
            TypeError: The stored value isn't a number.
            Timeout: Another process held the write lock for longer than `timeout`.
        """
    def decr(self, key: str, delta: float = ..., default: float | None = ...) -> int | float:
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
            timeout: Seconds to keep retrying while other processes or a pending fork are busy
                (default: the cache's `timeout`).

        Returns:
            True if the log was reset, False if readers or writers in other processes, or a
            pending fork, kept it busy. It never raises for a busy database.
        """
    def close(self, *, checkpoint: bool = True) -> None:
        """Close this handle and return its connections.

        When the last handle on a directory in the process closes, every connection to it closes,
        whichever threads used them. Using the cache again afterwards reopens it.

        It never raises. Behind a fork that stays stuck it returns after about 2 seconds at most,
        whatever `timeout` is, and those connections close when the fork ends.

        Args:
            checkpoint: If this is the process's last handle and the write-ahead log is over half
                of `wal_size_limit`, checkpoint it first, waiting at most 1 second. A small log
                costs nothing extra.
        """
    def _wal_stats(self) -> dict[str, int]:
        """Test hook: forced WAL checkpoint counters (checks, attempts, successes, busy, errors, no_slot, fork_pending,
        max_pause_us, max_passive_us, max_truncate_us, slow, last_wal_bytes)."""
    def _durable_stats(self) -> dict[str, dict[str, int]]:
        """Test hook: every failure `durable=True` has swallowed on this handle, by bucket (busy, broken, lost_deletes)
        and then by operation. Empty on a handle that isn't durable."""
    def _handle_count(self) -> int:
        """Test hook: how many open handles this cache's store counts, deferred releases included."""
    def transact(self, retry: bool = False) -> Transaction:
        """A write transaction, for `with cache.transact() as tx:`.

        `tx` is the cache itself. On the thread that entered the block, every call on this cache
        directory inside it, through any handle, runs in the transaction: it commits when the block
        exits and rolls back if the block raises. Other threads' and processes' writes wait for it.
        Entered on a thread that already holds a transaction on this directory, it joins that one:
        the inner block's exit doesn't commit, and an exception the caller catches inside doesn't
        undo the inner block's writes.

        A transaction belongs to a thread, not an asyncio task: another coroutine's writes on the
        same thread during an `await` inside the block join it and roll back with it.

        Args:
            retry: Keep trying to begin while it times out, instead of raising `Timeout`. Never
                retries `TransactionBusy`. Each attempt waits up to `timeout`.

        Raises:
            TypeError: `retry` isn't a `bool`.
        """
    def __getitem__(self, key: str, /) -> Any:
        """`cache[key]`: the value, or `KeyError` if it's missing or expired, or the read failed under
        `durable=True`."""
    def __setitem__(self, key: str, value: Any, /) -> None:
        """`cache[key] = value`: store a value that never expires, like `set()`. Under `durable=True` a failed write is
        dropped."""
    def __delitem__(self, key: str, /) -> None:
        """`del cache[key]`: remove the entry, or raise `KeyError` if it's missing or expired, or the delete failed
        under `durable=True`."""
    def __contains__(self, key: object, /) -> bool:
        """`key in cache`: True if the key holds a live (unexpired) value. False if the check failed under
        `durable=True`."""
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
    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
        /,
    ) -> None:
        """Close the cache, as `close()` does."""

class FanoutCache:
    """A disk cache split over several SQLite databases (shards), so reads and writes on different keys
    don't queue on one file's locks.

    Each key lives in exactly one shard, chosen by a hash that is part of the on-disk format: every
    process, and every release, sends a key to the same shard. The root records its shard count, so
    every handle agrees on it. Single-key calls behave exactly as on `Cache`, on the key's shard.
    `len()` and `volume()` add up every shard under one `timeout`.

    `clear()`, `expire()`, `cull()` and `checkpoint()` run on every shard in turn, under one
    `timeout` for the whole call. If a shard fails, the others are still done, and then the first
    failure raises, as its own type (so `except Timeout` still works), with two extra attributes and
    a note naming them:

    - `failed_shards`: a tuple of the indexes of the shards that failed, in order.
    - `removed` (not on `checkpoint()`): how many entries the call removed in all, from every
      shard, including what a failed shard removed before it failed.

    Args:
        directory: The root directory. It holds the manifest (`diskcache-oxide-fanout`) and one
            directory per shard, `000` to `NNN`. It must not be a plain `Cache`'s directory.
        timeout: As for `Cache` (default 60). Opening, `len()`, `volume()`, `clear()`, `expire()`,
            `cull()`, `checkpoint()` and `close()` share one `timeout` across every shard, not one
            each.
        shards: How many shards, 1 to 256. `None` (the default) uses the count the root records, or
            8 for a new root. A count that differs from the recorded one raises `LayoutMismatchError` (a `ValueError`):
            changing it needs a new directory. Each shard takes about 4 file descriptors to open,
            and 2 more per connection it opens in use, so 256 shards need over 1,024: more than
            Linux's default `ulimit -n`. Too few raises `OSError` (`EMFILE`) with the numbers.
        size_limit: The disk budget in bytes for the whole cache, split evenly (floored) across the
            shards, each of which culls on its own. Each shard needs at least 1 MiB, so the total must
            be at least `shards` MiB. With none given, a new root gets 1 GiB in all.
        cull_limit: As for `Cache`, per shard.
        serializer: As for `Cache`.
        pickle: As for `Cache`.
        clock: As for `Cache`.
        max_connections: The most SQLite connections this process opens for the whole cache, split
            across the shards (the first `max_connections % shards` get one more). At least `shards`.
            The default is one per shard, and never fewer than 8 in all.
        sqlite_cache_size: As for `Cache`, per connection.
        wal_size_limit: As for `Cache`, per shard (each shard has its own write-ahead log).
        wal_checkpoint: As for `Cache`.
        on_decode_error: As for `Cache`.
        durable: As for `Cache`: single-key calls on a shard that fails give their miss values. One
            report covers every shard, named by the root directory.

    Raises:
        ValueError: A setting is out of range, or `max_connections` is below `shards`.
        LayoutMismatchError: `shards` differs from the root's, or the directory belongs to a plain
            `Cache` (or a shard of it does). Also a `ValueError`, and a `FormatMismatchError`.
        TypeError: An argument has the wrong type, including a `bool` for `timeout` or `shards`.
        FormatMismatchError: The root's manifest, or a shard's database, is in a format this version
            can't read.
        OSError: The filesystem doesn't support hard links, which creating the manifest needs, or
            the process can't open enough files for every shard (`ulimit -n`).
        Timeout: A shard stayed locked, or a fork stayed pending, for longer than `timeout`.
    """

    def __new__(
        cls,
        directory: str | os.PathLike[str],
        timeout: float | None = None,
        *,
        shards: int | None = None,
        size_limit: int | None = None,
        cull_limit: int | None = None,
        serializer: str = 'msgpack',
        pickle: bool = False,
        clock: Callable[[], float] | None = None,
        max_connections: int | None = None,
        sqlite_cache_size: int | None = None,
        wal_size_limit: int | None = None,
        wal_checkpoint: Literal['manual', 'auto'] = 'manual',
        on_decode_error: Literal['raise', 'miss'] | Callable[[str, Exception], object] | None = None,
        durable: bool = False,
    ) -> FanoutCache: ...
    @property
    def directory(self) -> str:
        """The root directory, as given to the constructor."""
    @property
    def timeout(self) -> float:
        """Seconds a call waits for a lock or a pending fork before raising `Timeout`."""
    @property
    def shards(self) -> int:
        """How many shards the root has."""
    @property
    def size_limit(self) -> int:
        """The disk budget in bytes: the sum of every shard's."""
    @property
    def cull_limit(self) -> int:
        """The most entries a single `set()` evicts from its shard when that shard is over its share.

        Shard 0's value: every shard's only while they were all created, and last set, under the same settings.
        """
    @property
    def enabled(self) -> bool:
        """Always True for a real cache; False for a `NullCache`."""
    def shard_of(self, key: str) -> int:
        """The shard `key` lives in, from 0 to `shards - 1`: the same in every process and release."""
    @overload
    def get(self, key: str, default: Any = None, *, expire_time: Literal[False] = False) -> Any:
        """As `Cache.get`, on the key's shard."""
    @overload
    def get(self, key: str, default: Any = None, *, expire_time: Literal[True]) -> tuple[Any, float | None]: ...
    def set(self, key: str, value: Any, expire: float | None = None) -> bool:
        """As `Cache.set`, on the key's shard."""
    def add(self, key: str, value: Any, expire: float | None = None) -> bool:
        """As `Cache.add`, on the key's shard: atomic across threads and processes."""
    def delete(self, key: str) -> bool:
        """As `Cache.delete`, on the key's shard."""
    def pop(self, key: str, default: Any = None) -> Any:
        """As `Cache.pop`, on the key's shard."""
    def touch(self, key: str, expire: float | None = None) -> bool:
        """As `Cache.touch`, on the key's shard."""
    def incr(self, key: str, delta: float = ..., default: float | None = ...) -> int | float:
        """As `Cache.incr`, on the key's shard: atomic across threads and processes."""
    def decr(self, key: str, delta: float = ..., default: float | None = ...) -> int | float:
        """As `Cache.decr`, on the key's shard."""
    def volume(self) -> int:
        """The total disk use in bytes of every shard (databases plus value files).

        Raises on any shard's failure, under `durable=True` too.
        """
    def expire(self, *, timeout: float | None = None) -> int:
        """Remove every expired entry from every shard, and its value file, as `Cache.expire` does.

        Args:
            timeout: Seconds the whole call may spend waiting for write locks (another thread's or
                another process's) or a pending fork, across every shard, for this call only (default:
                the cache's `timeout`). It bounds waiting, not work: a chunk already running finishes,
                and once it's spent, each remaining chunk still gets one try with no wait, so shards no
                one else is writing are all done even at a short timeout.

        Returns:
            How many entries were removed.

        Raises:
            Timeout: Some shard stayed locked past `timeout`; the others were still done. It carries
                `removed` and `failed_shards` (see the class docs), as any failure here does, under
                `durable=True` too.
        """
    def clear(self, *, timeout: float | None = None) -> int:
        """Remove every entry that existed when the call started, from every shard.

        Every shard's starting point is taken before anything is removed, so writes that land while
        it runs, on any shard, are kept, as on `Cache.clear`.

        Args:
            timeout: As for `expire`: one budget for the whole call.

        Returns:
            How many entries were removed.

        Raises:
            Timeout: As for `expire`, with `removed` and `failed_shards`.
        """
    def cull(self, *, timeout: float | None = None) -> int:
        """Remove expired entries, then evict the least recently stored from each shard until it's
        under its share of `size_limit`.

        Args:
            timeout: As for `expire`: one budget for the whole call.

        Returns:
            How many entries were removed.

        Raises:
            Timeout: As for `expire`, with `removed` and `failed_shards`.
        """
    def checkpoint(self, *, timeout: float | None = None) -> bool:
        """Checkpoint every shard's write-ahead log and truncate it to zero, as `Cache.checkpoint` does.

        Args:
            timeout: Seconds to keep retrying busy shards, for the whole call (default: the cache's
                `timeout`). A shard reached after it's spent still gets one try.

        Returns:
            True if every shard's log was reset, False if readers or writers in other processes, or
            a pending fork, kept any of them busy.

        Raises:
            OSError: A real error, such as a full disk, on some shard, raised after every shard was
                tried. It carries `failed_shards` (see the class docs).
        """
    def iterkeys(self, *, prefix: str | None = None, reverse: bool = False) -> Iterator[str]:
        """Iterate over keys in sorted order, optionally only those starting with `prefix`.

        The same order `Cache.iterkeys` gives for the same keys (code-point order, like `sorted()`):
        a merge of every shard's sorted keys. Each shard keeps a small page of keys buffered, so the
        first `next()` reads every shard once, and later ones read a shard only when its page runs
        out. No connection or lock is held between `next()` calls. As on `Cache`, it isn't a
        snapshot: a key stored or deleted while it runs may or may not be yielded, but none is
        yielded twice and the order holds. Expired entries not yet removed are included. `next()`
        raises `ValueError` while another thread is inside it.

        Args:
            prefix: Only yield keys that start with this string.
            reverse: Yield keys in descending order.

        Raises:
            Timeout: A shard stayed locked, or a fork stayed pending, for longer than `timeout`
                during one `next()` (which shares that one budget across the shards it reads).
        """
    def __iter__(self) -> Iterator[str]:
        """Iterate over every key once: shard 0's in insertion order, then shard 1's, and so on.

        That isn't the order keys were stored in across the cache. Otherwise as `iterkeys()`.
        """
    def close(self, *, checkpoint: bool = True) -> None:
        """Close this handle on every shard, as `Cache.close()` does for one.

        The whole close shares one budget: the checkpoints share at most 1 second, and behind a fork
        that stays stuck every release together waits at most 1 second more. It never raises. Using
        the cache again reopens the shards it touches.

        Args:
            checkpoint: Checkpoint each shard's write-ahead log first, as `Cache.close` does.
        """
    def _durable_stats(self) -> dict[str, dict[str, int]]:
        """Test hook: every failure `durable=True` has swallowed on this handle, as `Cache._durable_stats`."""
    def _handle_counts(self) -> list[int]:
        """Test hook: how many open handles each shard's store counts, in shard order."""
    def __getitem__(self, key: str, /) -> Any:
        """`cache[key]`, as on `Cache`."""
    def __setitem__(self, key: str, value: Any, /) -> None:
        """`cache[key] = value`, as on `Cache`."""
    def __delitem__(self, key: str, /) -> None:
        """`del cache[key]`, as on `Cache`."""
    def __contains__(self, key: object, /) -> bool:
        """`key in cache`, as on `Cache`."""
    def __len__(self) -> int:
        """The number of entries in every shard, including expired entries not yet removed.

        Raises on any shard's failure, under `durable=True` too.
        """
    def __bool__(self) -> bool:
        """Always True, even when empty, so `if cache:` means "caching is on"."""
    def __enter__(self) -> Self:
        """Return the cache, for `with FanoutCache(...) as cache:`."""
    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
        /,
    ) -> None:
        """Close the cache, as `close()` does."""

@final
class Transaction:
    """The context manager `Cache.transact()` returns. It's entered once and exited once.

    It may be exited on another thread: that thread commits or rolls back the owner's transaction
    within its `timeout`, and a commit that can't run in time raises `Timeout` and rolls back.
    """

    def __enter__(self) -> Cache:
        """Begin the transaction, or join the one this thread already holds on the directory, and
        return the cache.

        Raises:
            Timeout: The write lock stayed taken, or a fork stayed pending, for longer than
                `timeout` (with `retry=False`).
            TransactionBusy: This thread holds another cache's transaction, and another thread holds
                this one's write lock.
            RuntimeError: It was already entered, or is being entered on another thread, or this
                process is a child forked inside a transaction on this directory.
        """
    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
        /,
    ) -> bool:
        """Commit (no exception) or roll back, and return False. A joined transaction's exit does neither.

        With no exception, while another thread is still ending the transaction, it waits up to
        the cache's `timeout` for that end and reports its outcome. With an exception passing it
        returns at once.

        Raises:
            RuntimeError: With no exception, the transaction didn't commit: it was rolled back by
                another thread, an earlier exit, or an error inside it (`TransactionAborted`). Also
                raised if it was never entered or is still being entered.
            Timeout: The commit couldn't run within `timeout`; it rolled back. Or another thread's
                end was still running after `timeout`, and whether it committed is unknown.
        """
