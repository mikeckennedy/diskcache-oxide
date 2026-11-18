"""`DjangoCache`: diskcache-oxide as a Django cache backend.

```python
CACHES = {
    'default': {
        'BACKEND': 'diskcache_oxide.DjangoCache',
        'LOCATION': '/var/cache/my-site/default',
        'TIMEOUT': 300,                      # Django's own: an entry's default lifetime
        'OPTIONS': {'size_limit': 2 * 1024**3},
    },
}
```

`OPTIONS` takes `Cache`'s keywords by their own names (`timeout`, `size_limit`, `cull_limit`,
`eviction_policy`, `serializer`, `pickle`, `types`, `max_connections`, `sqlite_cache_size`,
`wal_size_limit`, `wal_checkpoint`, `on_decode_error`, `durable`, `maintenance_interval`), and
`shards` makes it a `FanoutCache` (which also takes `raise_on_mismatch`). Two defaults differ from
`Cache`'s: `timeout`, the lock wait, is 1.0 s, not 60, so a blocked write raises `Timeout` long before a
worker timeout kills the worker; and `pickle` is True, because Django caches any picklable object
(responses, `SafeString`s, model instances, sessions). Values msgpack can store are still stored
without pickle. `TIMEOUT`, `KEY_PREFIX`, `VERSION` and `KEY_FUNCTION` are Django's and work as in
every backend.

Refused with `ImproperlyConfigured`: a missing `LOCATION`, an unknown option, `clock`, and Django's
`MAX_ENTRIES` and `CULL_FREQUENCY` (a cache is bounded by `size_limit` in bytes, not by an entry
count). A `durable=True` alias that the `cache` or `cached_db` session engine uses (or a subclass of
either) is refused too, on the first use of the alias: a durable write that can't get the lock is
dropped instead of raising, and the session engines read that as "try again" (the cache engine's
`create()` up to 10,000 times) or only log it.

**Errors raise.** A write that can't get the lock within `timeout` raises `Timeout`, as a Redis backend
raises on a lost connection. `OPTIONS = {'durable': True}` is for a page cache that must never fail a
request: single-key writes are dropped instead (`add`, `touch` and `delete` return False, `set_many`
returns the keys it didn't store), and reads miss. It doesn't cover everything: `incr` raises
Django's `ValueError`, as for a missing key, and `clear()` raises `Timeout`. If a durable alias's
directory can't be opened at all, the backend serves a `NullCache` (where `add` always succeeds and
nothing is stored) and tries again later, as the `Caches` registry does: after 1 s for a `Timeout`,
logged at WARNING, and after 10 s for anything else, logged at ERROR with the traceback, about once
per retry.

**Beyond Django's API**: `tag=` on `set` and `add`, `evict(tag)`, `pop`, `expire()`, `cull()`,
`stats()`, `maintenance_stats()`, `check()`, and `cache`, the `Cache` underneath.

**One cache per directory per process.** Django builds a backend instance per thread, and under ASGI
up to one per request. Instances hold no cache of their own: every instance for a directory, in every
thread, shares one `Cache`, opened on first use and kept for the life of the process. Aliases on one
`LOCATION` must have the same `OPTIONS`. `close()`, which Django calls after every request, does
nothing; `close_all()` closes every handle (the next call reopens), and runs by itself when a test
changes `CACHES` with `override_settings`.
"""

from __future__ import annotations

import inspect
import logging
import os
import time
from importlib import import_module
from typing import Any, Optional

from asgiref.sync import sync_to_async
from django.core.cache.backends.base import (
    DEFAULT_TIMEOUT,
    MEMCACHE_MAX_KEY_LENGTH,
    BaseCache,
    memcached_error_chars_re,
)
from django.core.exceptions import ImproperlyConfigured
from django.core.signals import setting_changed
from django.utils.module_loading import import_string

from diskcache_oxide._core import Cache, FanoutCache, Timeout
from diskcache_oxide._types import Key, MaintenanceStats
from diskcache_oxide.null import NullCache

__all__ = ['DjangoCache', 'close_all']

log = logging.getLogger('diskcache_oxide')


def keyword_defaults(kind: type) -> dict[str, Any]:
    return {
        name: parameter.default
        for name, parameter in inspect.signature(kind).parameters.items()
        if name != 'directory' and not name.startswith('_')
    }


# The backend's own defaults, over the engine's.
BACKEND_DEFAULTS: dict[str, Any] = {'timeout': 1.0, 'pickle': True}

# Every option a directory can be opened with, filled in, so two aliases compare equal when they'd
# open the same cache (`{}` and `{'timeout': 1.0}`). Computed once: `__init__` runs per thread and
# per ASGI request. `clock` is left out: a fake clock turns background maintenance off for the
# whole directory, which is never what a Django setting means.
CACHE_DEFAULTS = {**keyword_defaults(Cache), **BACKEND_DEFAULTS}
FANOUT_DEFAULTS = {**keyword_defaults(FanoutCache), **BACKEND_DEFAULTS}
del CACHE_DEFAULTS['clock'], FANOUT_DEFAULTS['clock']

# Settings that mean something else here, each with what to use instead.
REFUSED_OPTIONS = {
    'MAX_ENTRIES': "diskcache-oxide bounds a cache by bytes, not entries: use OPTIONS['size_limit']",
    'CULL_FREQUENCY': "diskcache-oxide evicts by bytes, not a fraction of entries: use OPTIONS['cull_limit']",
    'clock': 'a fake clock would switch background maintenance off for the whole directory',
}
REFUSED_PARAMS = {
    'max_entries': REFUSED_OPTIONS['MAX_ENTRIES'],
    'cull_frequency': REFUSED_OPTIONS['CULL_FREQUENCY'],
    # diskcache.DjangoCache's top-level settings.
    'SHARDS': "use OPTIONS['shards']",
    'DATABASE_TIMEOUT': "use OPTIONS['timeout'], the lock wait in seconds",
}

# The open handle for each directory, and the options every alias on it was configured with. Plain
# dicts, no lock: a lookup is one read, a racing first open keeps whichever handle `setdefault` stored
# and closes the other. A lock held by another thread at `fork()` would hang the child; without one, a
# forked child inherits the table and its handles keep working (the engine's fork guarantee).
handles: dict[str, Cache | FanoutCache] = {}
configured: dict[str, dict[str, Any]] = {}

# A durable alias whose directory couldn't be opened: when to try again, and the NullCache served
# until then. `Timeout` is an `OSError`; `RuntimeError` covers a directory holding the other kind of
# cache (`LayoutMismatchError`) and a child forked mid-transaction. Bad settings still raise.
unavailable: dict[str, tuple[float, NullCache]] = {}
RETRY_OPEN_AFTER = 10.0
RETRY_AFTER_BUSY = 1.0  # a Timeout at open means busy (a fork, a peer holding the lock), not broken
OPEN_ERRORS = (OSError, RuntimeError)

# The session engines that store sessions in a cache, and this backend's dotted names.
CACHE_SESSION_ENGINES = frozenset(
    {'django.contrib.sessions.backends.cache', 'django.contrib.sessions.backends.cached_db'}
)
BACKEND_NAMES = frozenset({'diskcache_oxide.DjangoCache', 'diskcache_oxide.djangocache.DjangoCache'})

# A miss in `get_many`, so a stored None is returned.
MISSING = object()

# Keys per `delete_many` transaction: long enough to amortize the commit (100 deletes in one cost a fifth
# or less of 100 single ones), short enough that the write lock is never held for long.
DELETE_CHUNK = 100

# Bound once: `memcached_error_chars_re` is a lazy proxy, and calling through it costs most of what
# the fast path saves.
search_bad_chars = memcached_error_chars_re.search


def close_all() -> None:
    """Close every handle this module opened, and forget each directory's options.

    The next call on any `DjangoCache` reopens its directory. For tests that create and delete cache
    directories; a running site never needs it. Runs by itself when `CACHES` changes under
    `override_settings`.
    """
    try:
        for directory in list(handles):
            handle = handles.pop(directory, None)
            if handle is not None:
                handle.close(checkpoint=False)
    finally:
        configured.clear()
        unavailable.clear()


def close_all_when_caches_change(*, setting: str, **kwargs: Any) -> None:
    if setting == 'CACHES':
        close_all()


setting_changed.connect(close_all_when_caches_change, dispatch_uid='diskcache_oxide.djangocache.close_all')


def options_mismatch(directory: str) -> ImproperlyConfigured:
    return ImproperlyConfigured(
        f'two DjangoCache aliases on {directory} have different OPTIONS (or an instance created before '
        'close_all() is still in use); aliases that share a LOCATION share one cache, so their OPTIONS must match'
    )


def is_cache_session_engine(engine: object) -> bool:
    if engine in CACHE_SESSION_ENGINES:
        return True
    if not isinstance(engine, str):
        return False
    try:  # a subclass of either engine's SessionStore
        from django.contrib.sessions.backends import cache, cached_db

        store = import_module(engine).SessionStore
        return issubclass(store, cache.SessionStore | cached_db.SessionStore)
    except Exception:
        return False


def is_this_backend(dotted: object) -> bool:
    if dotted in BACKEND_NAMES:
        return True
    try:
        return isinstance(dotted, str) and issubclass(import_string(dotted), DjangoCache)
    except Exception:
        return False


def refuse_durable_sessions(directory: str) -> None:
    """Raise if the cache or cached_db session engine stores its sessions in this durable directory."""
    from django.conf import settings

    params = settings.CACHES.get(settings.SESSION_CACHE_ALIAS) or {}
    location = params.get('LOCATION')
    if not location or os.path.abspath(os.fspath(location)) != directory:
        return
    if is_this_backend(params.get('BACKEND')) and is_cache_session_engine(getattr(settings, 'SESSION_ENGINE', None)):
        raise ImproperlyConfigured(
            f'SESSION_CACHE_ALIAS {settings.SESSION_CACHE_ALIAS!r} is a durable=True DjangoCache: a durable '
            "write that can't get the lock returns False instead of raising, which the session engine reads "
            'as "try again" (up to 10,000 times) or only logs. Give sessions an alias without durable=True.'
        )


def exact_str(made: object) -> str:
    if isinstance(made, str):
        return str.__str__(made)  # a str subclass (a SafeString) as an exact str; str() would keep the type
    raise TypeError(f'a cache key must be a str, but KEY_FUNCTION returned {type(made).__name__}')


class DjangoCache(BaseCache):
    """A Django cache backend on a diskcache-oxide `Cache` (or a `FanoutCache` with `OPTIONS['shards']`).

    Args:
        location: The cache directory, Django's `LOCATION`. Required.
        params: The rest of the alias's settings, as Django passes them.

    Raises:
        ImproperlyConfigured: No `LOCATION`, an unknown or refused option, another alias on the
            same directory with different `OPTIONS`, or a `durable=True` alias that the cache or
            cached_db session engine stores sessions in.
    """

    def __init__(self, location: str | os.PathLike[str], params: dict[str, Any]) -> None:
        for name, instead in REFUSED_PARAMS.items():
            if name in params:
                raise ImproperlyConfigured(f'{name} is not a DjangoCache setting: {instead}')
        options = params.get('OPTIONS') or {}
        for name, instead in REFUSED_OPTIONS.items():
            if name in options:
                raise ImproperlyConfigured(f"OPTIONS['{name}'] is not supported by DjangoCache: {instead}")
        super().__init__(params)
        if not location:
            raise ImproperlyConfigured('DjangoCache needs a LOCATION: the cache directory')
        if not isinstance(location, str | os.PathLike):
            raise ImproperlyConfigured(f'LOCATION must be a str or a path, not {type(location).__name__}')
        directory = os.path.abspath(os.fspath(location))
        sharded = 'shards' in options
        defaults = FANOUT_DEFAULTS if sharded else CACHE_DEFAULTS
        unknown = sorted(options.keys() - defaults.keys())
        if unknown:
            raise ImproperlyConfigured(
                f'unknown DjangoCache option{"s" if len(unknown) > 1 else ""}: {", ".join(unknown)}'
            )
        if 'timeout' in options and options['timeout'] is None:
            # None would be the engine's 60 s lock wait, longer than a worker timeout.
            raise ImproperlyConfigured("OPTIONS['timeout'] is the lock wait in seconds; leave it out for 1.0")
        filled = {**defaults, **options}
        if configured.setdefault(directory, filled) != filled:
            raise options_mismatch(directory)
        if filled['durable']:
            refuse_durable_sessions(directory)
        self.directory = directory
        self.sharded = sharded
        self.options = filled

    @property
    def cache(self) -> Cache | FanoutCache | NullCache:
        """The `Cache` (or `FanoutCache`) under this backend, shared by every alias on its directory.

        Its keys are the made keys (`prefix:version:key`), not the ones passed to the backend. On a
        `durable=True` alias whose directory couldn't be opened, a `NullCache` until the next try.
        """
        handle = handles.get(self.directory)
        if handle is None:
            handle = self.open_handle()
        return handle

    def open_handle(self) -> Cache | FanoutCache | NullCache:
        # Again, since close_all() may have run since __init__: then an instance built after it decides.
        if configured.setdefault(self.directory, self.options) != self.options:
            raise options_mismatch(self.directory)
        durable = self.options['durable']
        down = unavailable.get(self.directory) if durable else None
        if down is not None:
            now = time.monotonic()
            if now < down[0]:
                return down[1]
            # This thread retries; the others keep serving the NullCache meanwhile, rather than all
            # retrying at once (and all logging). Without a lock, a few may still race here.
            unavailable[self.directory] = (now + RETRY_AFTER_BUSY, down[1])
        try:
            new = (FanoutCache if self.sharded else Cache)(self.directory, **self.options)
        except OPEN_ERRORS as error:
            if not durable:
                raise
            return self.serve_null(error, down)
        unavailable.pop(self.directory, None)
        handle = handles.setdefault(self.directory, new)
        if handle is not new:
            new.close(checkpoint=False)
        return handle

    def serve_null(self, error: BaseException, down: Optional[tuple[float, NullCache]]) -> NullCache:
        busy = isinstance(error, Timeout)
        retry_after = RETRY_AFTER_BUSY if busy else RETRY_OPEN_AFTER
        entry = (time.monotonic() + retry_after, down[1] if down is not None else NullCache())
        if down is None:
            # The first failure: only the thread whose entry went in logs it.
            if unavailable.setdefault(self.directory, entry) is not entry:
                return unavailable[self.directory][1]
        else:
            unavailable[self.directory] = entry
        message = 'DjangoCache could not open %s; serving a NullCache, trying again in %.0f s'
        if busy:
            log.warning(message + ' (%s)', self.directory, retry_after, error)
        else:
            log.error(message, self.directory, retry_after, exc_info=error)
        return entry[1]

    # Keys and timeouts.

    def make_and_validate_key(self, key: Any, version: Optional[int] = None) -> str:
        made = self.make_key(key, version=version)
        if type(made) is not str:
            made = exact_str(made)
        self.validate_key(made)
        return made

    def validate_key(self, key: str) -> None:
        """Warn (`CacheKeyWarning`) about a key memcached couldn't store, as every Django backend does."""
        if len(key) <= MEMCACHE_MAX_KEY_LENGTH and not search_bad_chars(key):
            return
        super().validate_key(key)

    def get_backend_timeout(self, timeout: Any = DEFAULT_TIMEOUT) -> Optional[float]:
        """Seconds from now, or None for forever. Zero or less means "don't store it", as in Django."""
        kind = type(timeout)
        if kind is int or kind is float:
            return timeout
        if timeout is DEFAULT_TIMEOUT:
            return self.default_timeout
        if timeout is None:
            return None
        if isinstance(timeout, int | float) and kind is not bool:
            return float(timeout)
        raise TypeError(f'a cache timeout is a number of seconds or None, not {kind.__name__}')

    # Single keys.

    def add(
        self, key: Any, value: Any, timeout: Any = DEFAULT_TIMEOUT, version: Optional[int] = None, tag: Key = None
    ) -> bool:
        """Django's `add`, plus `tag=`: an entry tagged here can be removed with `evict(tag)`."""
        key = self.make_and_validate_key(key, version=version)
        return self.cache.add(key, value, self.get_backend_timeout(timeout), tag=tag)

    def get(self, key: Any, default: Any = None, version: Optional[int] = None) -> Any:
        key = self.make_and_validate_key(key, version=version)
        return self.cache.get(key, default)

    def set(
        self, key: Any, value: Any, timeout: Any = DEFAULT_TIMEOUT, version: Optional[int] = None, tag: Key = None
    ) -> None:
        """Django's `set`, plus `tag=`: an entry tagged here can be removed with `evict(tag)`."""
        key = self.make_and_validate_key(key, version=version)
        self.cache.set(key, value, self.get_backend_timeout(timeout), tag=tag)

    def touch(self, key: Any, timeout: Any = DEFAULT_TIMEOUT, version: Optional[int] = None) -> bool:
        key = self.make_and_validate_key(key, version=version)
        return self.cache.touch(key, self.get_backend_timeout(timeout))

    def delete(self, key: Any, version: Optional[int] = None) -> bool:
        key = self.make_and_validate_key(key, version=version)
        return self.cache.delete(key)

    def has_key(self, key: Any, version: Optional[int] = None) -> bool:
        key = self.make_and_validate_key(key, version=version)
        return key in self.cache

    def incr(self, key: Any, delta: int = 1, version: Optional[int] = None) -> int | float:
        """Add `delta` atomically, across threads and processes, keeping the entry's expiry.

        Raises:
            ValueError: The key is missing or expired.
            TypeError: The stored value isn't an integer.
        """
        key = self.make_and_validate_key(key, version=version)
        try:
            return self.cache.incr(key, delta, None)
        except KeyError:
            raise ValueError(f"Key '{key}' not found") from None

    def decr(self, key: Any, delta: int = 1, version: Optional[int] = None) -> int | float:
        return self.incr(key, -delta, version=version)

    # Several keys.

    def get_many(self, keys: Any, version: Optional[int] = None) -> dict[Any, Any]:
        """The keys that are there, with their values; a stored None is returned."""
        cache = self.cache
        found = {}
        for key in keys:
            value = cache.get(self.make_and_validate_key(key, version=version), MISSING)
            if value is not MISSING:
                found[key] = value
        return found

    def set_many(self, data: Any, timeout: Any = DEFAULT_TIMEOUT, version: Optional[int] = None) -> list[Any]:
        """A `set` per key, not one transaction: a value's pickling can run arbitrary code (load a
        session, run a query), which mustn't run holding the cache's write lock. Returns the keys
        that weren't stored, which only a `durable=True` cache can have. A durable `set` behind a held
        lock waits out the lock wait and returns False, so once one returns False after half a lock wait
        has passed, it and every key after it are returned untried: one wait, not one per key.
        A failure that comes at once (one value that can't be written) only fails that key.
        """
        expire = self.get_backend_timeout(timeout)
        cache = self.cache
        deadline = time.monotonic() + self.options['timeout'] / 2
        failed = []
        items = iter(data.items())
        for key, value in items:
            if cache.set(self.make_and_validate_key(key, version=version), value, expire) is False:
                failed.append(key)
                if time.monotonic() >= deadline:
                    failed.extend(rest for rest, _ in items)
                    break
        return failed

    def delete_many(self, keys: Any, version: Optional[int] = None) -> None:
        """Delete the keys, up to `DELETE_CHUNK` at a time in one transaction (one per shard on a
        `FanoutCache`; a one-key chunk is a plain delete). Every key is made and validated first, so
        a `CacheKeyWarning` raised as an error deletes nothing. A `Timeout` part-way leaves the
        chunks before it deleted, as a loop of deletes would. Inside the caller's own transaction on
        one shard of a `FanoutCache`, keys on another shard raise the engine's `ValueError`.

        Under `durable=True`, single deletes, which durable covers and a transaction doesn't. A durable
        delete that can't get the lock returns False after the lock wait, as a miss does at once, so
        the batch stops at the first False past half a lock wait: one wait, not one per key.
        """
        made = [self.make_and_validate_key(key, version=version) for key in keys]
        cache = self.cache
        if self.options['durable']:
            deadline = time.monotonic() + self.options['timeout'] / 2
            for key in made:
                if not cache.delete(key) and time.monotonic() >= deadline:
                    return
            return
        fanout = cache if isinstance(cache, FanoutCache) else None
        if fanout is not None:
            by_shard: dict[int, list[str]] = {}
            for key in made:
                by_shard.setdefault(fanout.shard_of(key), []).append(key)
            groups = list(by_shard.values())
        else:
            groups = [made]
        for group in groups:
            for start in range(0, len(group), DELETE_CHUNK):
                chunk = group[start : start + DELETE_CHUNK]
                if len(chunk) == 1:
                    cache.delete(chunk[0])
                    continue
                with cache.transact(key=chunk[0]) if isinstance(cache, FanoutCache) else cache.transact():
                    for key in chunk:
                        cache.delete(key)

    def clear(self) -> None:
        """Remove every entry in the directory: every alias, prefix and version on this `LOCATION`."""
        self.cache.clear()

    def close(self, **kwargs: Any) -> None:
        """Nothing: Django calls it after every request, and the handle lives for the process."""

    # Beyond Django's API. Each acts on the whole directory, every alias on it included, except `pop`.

    def pop(self, key: Any, default: Any = None, version: Optional[int] = None) -> Any:
        """Remove the key and return its value, or `default` if it's missing, atomically."""
        return self.cache.pop(self.make_and_validate_key(key, version=version), default)

    def evict(self, tag: Key) -> int:
        """Remove every entry tagged `tag` (with `set(..., tag=)` or `add(..., tag=)`). Returns how many."""
        return self.cache.evict(tag)

    def expire(self) -> int:
        """Remove expired entries now, rather than waiting for background maintenance. Returns how many."""
        return self.cache.expire()

    def cull(self) -> int:
        """Remove expired entries, then evict until the cache is under `size_limit`. Returns how many."""
        return self.cache.cull()

    def stats(self, enable: bool = True, reset: bool = False) -> tuple[int, int]:
        """`(hits, misses)` since counting was enabled; see `Cache.stats`."""
        return self.cache.stats(enable, reset)

    def maintenance_stats(self) -> MaintenanceStats:
        """What background maintenance has done; see `Cache.maintenance_stats`."""
        return self.cache.maintenance_stats()

    def check(self, fix: bool = False) -> list[Any]:
        """Check the cache's consistency, returning the problems as warnings; see `Cache.check`."""
        return self.cache.check(fix)

    # Async: each runs its sync form in one `sync_to_async` call, as Django 6.1 does for an overridden
    # method. On 5.2 and 6.0, `BaseCache`'s own `aincr` is an `aget` and then an `aset`: not atomic,
    # and it resets the expiry. Overriding every one keeps the three lines the same.

    async def aadd(
        self, key: Any, value: Any, timeout: Any = DEFAULT_TIMEOUT, version: Optional[int] = None, tag: Key = None
    ) -> bool:
        return await sync_to_async(self.add, thread_sensitive=True)(key, value, timeout, version, tag)

    async def aget(self, key: Any, default: Any = None, version: Optional[int] = None) -> Any:
        return await sync_to_async(self.get, thread_sensitive=True)(key, default, version)

    async def aset(
        self, key: Any, value: Any, timeout: Any = DEFAULT_TIMEOUT, version: Optional[int] = None, tag: Key = None
    ) -> None:
        return await sync_to_async(self.set, thread_sensitive=True)(key, value, timeout, version, tag)

    async def atouch(self, key: Any, timeout: Any = DEFAULT_TIMEOUT, version: Optional[int] = None) -> bool:
        return await sync_to_async(self.touch, thread_sensitive=True)(key, timeout, version)

    async def adelete(self, key: Any, version: Optional[int] = None) -> bool:
        return await sync_to_async(self.delete, thread_sensitive=True)(key, version)

    async def ahas_key(self, key: Any, version: Optional[int] = None) -> bool:
        return await sync_to_async(self.has_key, thread_sensitive=True)(key, version)

    async def aincr(self, key: Any, delta: int = 1, version: Optional[int] = None) -> int | float:
        return await sync_to_async(self.incr, thread_sensitive=True)(key, delta, version)

    async def adecr(self, key: Any, delta: int = 1, version: Optional[int] = None) -> int | float:
        return await sync_to_async(self.decr, thread_sensitive=True)(key, delta, version)

    async def aget_many(self, keys: Any, version: Optional[int] = None) -> dict[Any, Any]:
        return await sync_to_async(self.get_many, thread_sensitive=True)(keys, version)

    async def aset_many(self, data: Any, timeout: Any = DEFAULT_TIMEOUT, version: Optional[int] = None) -> list[Any]:
        return await sync_to_async(self.set_many, thread_sensitive=True)(data, timeout, version)

    async def adelete_many(self, keys: Any, version: Optional[int] = None) -> None:
        return await sync_to_async(self.delete_many, thread_sensitive=True)(keys, version)

    async def aclear(self) -> None:
        return await sync_to_async(self.clear, thread_sensitive=True)()

    async def aclose(self, **kwargs: Any) -> None:
        """Nothing, without a thread hop: `close()` does nothing."""
