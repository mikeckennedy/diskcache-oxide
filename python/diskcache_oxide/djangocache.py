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
count).

**One cache per directory per process.** Django builds a backend instance per thread, and under ASGI
up to one per request. Instances hold no cache of their own: every instance for a directory, in every
thread, shares one `Cache`, opened on first use and kept for the life of the process. Aliases on one
`LOCATION` must have the same `OPTIONS`. `close()`, which Django calls after every request, does
nothing; `close_all()` closes every handle (the next call reopens), and runs by itself when a test
changes `CACHES` with `override_settings`.
"""

from __future__ import annotations

import inspect
import os
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

from diskcache_oxide._core import Cache, FanoutCache

__all__ = ['DjangoCache', 'close_all']


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


def close_all_when_caches_change(*, setting: str, **kwargs: Any) -> None:
    if setting == 'CACHES':
        close_all()


setting_changed.connect(close_all_when_caches_change, dispatch_uid='diskcache_oxide.djangocache.close_all')


def options_mismatch(directory: str) -> ImproperlyConfigured:
    return ImproperlyConfigured(
        f'two DjangoCache aliases on {directory} have different OPTIONS (or an instance created before '
        'close_all() is still in use); aliases that share a LOCATION share one cache, so their OPTIONS must match'
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
        ImproperlyConfigured: No `LOCATION`, an unknown or refused option, or another alias on the
            same directory with different `OPTIONS`.
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
        self.directory = directory
        self.sharded = sharded
        self.options = filled

    @property
    def cache(self) -> Cache | FanoutCache:
        """The `Cache` (or `FanoutCache`) under this backend, shared by every alias on its directory.

        Its keys are the made keys (`prefix:version:key`), not the ones passed to the backend.
        """
        handle = handles.get(self.directory)
        if handle is None:
            handle = self.open_handle()
        return handle

    def open_handle(self) -> Cache | FanoutCache:
        # Again, since close_all() may have run since __init__: then an instance built after it decides.
        if configured.setdefault(self.directory, self.options) != self.options:
            raise options_mismatch(self.directory)
        new = (FanoutCache if self.sharded else Cache)(self.directory, **self.options)
        handle = handles.setdefault(self.directory, new)
        if handle is not new:
            new.close(checkpoint=False)
        return handle

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

    def add(self, key: Any, value: Any, timeout: Any = DEFAULT_TIMEOUT, version: Optional[int] = None) -> bool:
        key = self.make_and_validate_key(key, version=version)
        return self.cache.add(key, value, self.get_backend_timeout(timeout))

    def get(self, key: Any, default: Any = None, version: Optional[int] = None) -> Any:
        key = self.make_and_validate_key(key, version=version)
        return self.cache.get(key, default)

    def set(self, key: Any, value: Any, timeout: Any = DEFAULT_TIMEOUT, version: Optional[int] = None) -> None:
        key = self.make_and_validate_key(key, version=version)
        self.cache.set(key, value, self.get_backend_timeout(timeout))

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

    def clear(self) -> None:
        """Remove every entry in the directory: every alias, prefix and version on this `LOCATION`."""
        self.cache.clear()

    def close(self, **kwargs: Any) -> None:
        """Nothing: Django calls it after every request, and the handle lives for the process."""

    # Async: each runs its sync form in one `sync_to_async` call, as Django 6.1 does for an overridden
    # method. On 5.2 and 6.0, `BaseCache`'s own `aincr` is an `aget` and then an `aset`: not atomic,
    # and it resets the expiry. Overriding every one keeps the three lines the same.

    async def aadd(self, key: Any, value: Any, timeout: Any = DEFAULT_TIMEOUT, version: Optional[int] = None) -> bool:
        return await sync_to_async(self.add, thread_sensitive=True)(key, value, timeout, version)

    async def aget(self, key: Any, default: Any = None, version: Optional[int] = None) -> Any:
        return await sync_to_async(self.get, thread_sensitive=True)(key, default, version)

    async def aset(self, key: Any, value: Any, timeout: Any = DEFAULT_TIMEOUT, version: Optional[int] = None) -> None:
        return await sync_to_async(self.set, thread_sensitive=True)(key, value, timeout, version)

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
