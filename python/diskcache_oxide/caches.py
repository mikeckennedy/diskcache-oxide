"""`Caches`: a small registry of named caches under one base directory.

An app registers its caches once, by name, then looks them up wherever it needs one:

    caches = Caches('/var/cache/app', timeout=1.0, on_decode_error='miss')
    caches.register('general')
    caches.register('show_pages', size_limit=2 * 2**30)

    caches['general'].set('key', 'value')

**Lookups.** A cache opens on its first lookup and the same object comes back every time after
that. A lookup is one dict read, with no lock, so it's cheap enough to do on every request.

**When a cache can't open** (a permissions problem, a database in a format this version can't read,
another process holding the lock past `timeout`), the lookup returns a `NullCache` instead of
raising, reports the error through `on_open_error`, and tries again after `retry_open_after` seconds.
A bad setting (`size_limit=True`) is a bug, not an outage, so that still raises. With `durable=True`
among the settings, the handle a lookup returns doesn't raise for an outage either, so the two together
keep a cache failure out of a request: the registry covers opening, and `durable` everything after.

**Switches.** `disable(name)` makes later lookups return `NullCache(invalidate=real)`: reads miss and
writes are dropped, but deletes and clears still reach the real cache, so entries invalidated by
removal aren't served stale once it's turned back on. Invalidation by writing is dropped like any
other write: an overwriting `set` or a version-bump `incr` while it's off leaves the old value. Switches change later
lookups only. Code that already holds the real
`Cache` keeps it, so set them at startup, before handing caches out.

**What stays in the app:** reading settings files and environment variables, mapping the app's own
names (purposes) onto these caches, descriptions for an admin page, and when to run `expire_all()`.
"""

from __future__ import annotations

import inspect
import logging
import os
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from types import TracebackType
from typing import Any, Self

from diskcache_oxide._core import Cache, Timeout
from diskcache_oxide.null import NullCache
from diskcache_oxide.protocol import CacheLike

__all__ = ['CacheStats', 'Caches']


log = logging.getLogger('diskcache_oxide')

# The keywords `Cache` accepts, so a misspelled setting fails at registration, not on first use.
_CACHE_SETTINGS = frozenset(inspect.signature(Cache).parameters) - {'directory'}

# Errors that mean "this cache can't open right now", as opposed to a bug in its settings.
# `Timeout` is an `OSError`. `RuntimeError` covers `FormatMismatchError`, the not-WAL error, and a
# child forked mid-transaction that can't write the file (D-020): a NullCache is right for all.
_OPEN_ERRORS = (OSError, RuntimeError)

# The longest a `Timeout` at open is remembered: it means busy (a fork, a peer holding the lock), not
# broken. Still remembered, or each lookup would spend the whole `timeout` behind a stuck fork.
_RETRY_AFTER_BUSY = 1.0


@dataclass(frozen=True, slots=True)
class CacheStats:
    """One cache's numbers, for an admin page. `error` says why it couldn't be read, if it couldn't."""

    name: str
    directory: str
    enabled: bool
    count: int
    volume: int
    error: str | None = None


def _check_settings(settings: dict[str, Any]) -> None:
    unknown = sorted(settings.keys() - _CACHE_SETTINGS)
    if unknown:
        raise TypeError(f'unknown Cache setting{"s" if len(unknown) > 1 else ""}: {", ".join(unknown)}')
    report = settings.get('on_decode_error')
    if callable(report):
        # A registry calls it with the cache's name too; catch a `(key, exc)` reporter carried over
        # from `Cache` here, not as a TypeError logged on every decode failure.
        try:
            signature = inspect.signature(report)
        except TypeError, ValueError:
            return  # No signature to check (some builtins).
        try:
            signature.bind('name', 'key', Exception())
        except TypeError:
            raise TypeError(
                f'a Caches on_decode_error is called as (name, key, exc); {report!r} does not take those'
            ) from None


class Caches[N: str = str]:
    """A registry of named caches under one base directory.

    `Caches[Literal['general', 'show_pages']]` makes a misspelled name a type error.

    Args:
        base_dir: Where the caches live: each one in `base_dir/<name>` unless `register` says
            otherwise. Created when the first cache opens.
        on_open_error: Called as `on_open_error(name, exc)` when a cache can't open (default: log at
            ERROR on the `diskcache_oxide` logger, or at WARNING with no traceback for a `Timeout`).
            Whatever it raises is logged.
        retry_open_after: Seconds before a cache that failed to open is tried again (default 10).
            Lookups in between get a `NullCache` without trying. A `Timeout` means busy rather than
            broken, so that's tried again after at most 1 s.
        **defaults: `Cache` settings for every cache, such as `timeout=1.0`, `durable=True` or
            `on_decode_error='miss'`. `register` can override them. A callable `on_decode_error`
            here is called as `(name, key, exc)`, so one reporter can tell the caches apart.
    """

    def __init__(
        self,
        base_dir: str | os.PathLike[str],
        *,
        on_open_error: Callable[[str, Exception], object] | None = None,
        retry_open_after: float = 10.0,
        _monotonic: Callable[[], float] = time.monotonic,
        **defaults: Any,
    ) -> None:
        _check_settings(defaults)
        if isinstance(retry_open_after, bool) or not isinstance(retry_open_after, int | float):
            raise TypeError(f'retry_open_after must be a number of seconds, not {type(retry_open_after).__name__}')
        if not 0 <= retry_open_after < float('inf'):
            raise ValueError(f'retry_open_after must be a non-negative number of seconds, got {retry_open_after!r}')
        self._base_dir = os.path.abspath(os.fspath(base_dir))
        self._defaults = defaults
        self._on_open_error = on_open_error
        self._retry_open_after = float(retry_open_after)
        self._monotonic = _monotonic
        # name -> (directory, settings), in registration order.
        self._specs: dict[N, tuple[str, dict[str, Any]]] = {}
        self._disabled: set[N] = set()
        # What `caches[name]` returns, filled on first lookup. Read without a lock: a lookup that
        # races another opens a second handle, and the loser is closed (handles on one directory
        # share one store, so that's cheap and safe).
        self._resolved: dict[N, CacheLike] = {}
        # The real handles, opened on demand.
        self._opened: dict[N, Cache] = {}
        # name -> (retry after this monotonic time, the NullCache handed out until then).
        self._failed: dict[N, tuple[float, NullCache]] = {}

    def __repr__(self) -> str:
        return f'Caches({self._base_dir!r}, {list(self._specs)!r})'

    # -- registration ----------------------------------------------------------------------------

    def register(
        self,
        name: N,
        *,
        directory: str | os.PathLike[str] | None = None,
        **settings: Any,
    ) -> None:
        """Register a cache. Nothing opens until its first lookup.

        Args:
            name: The cache's name. Without `directory`, it's also the directory's name under
                `base_dir`, so it must be a plain name: no path separators, not `.` or `..`.
            directory: Put this cache somewhere else.
            **settings: `Cache` settings for this cache, over the registry's defaults. A callable
                `on_decode_error` is called as `(name, key, exc)`, as at the registry level.

        Raises:
            ValueError: The name is taken or isn't a plain directory name, or another registered
                cache already uses that directory (two names would share one store, and the last
                one opened would quietly set its `size_limit`).
            TypeError: An unknown `Cache` setting.
        """
        if not isinstance(name, str) or not name:
            raise ValueError(f'a cache name must be a non-empty str, got {name!r}')
        if name in self._specs:
            raise ValueError(f'a cache named {name!r} is already registered')
        _check_settings(settings)
        if directory is None:
            if name in ('.', '..') or os.sep in name or (os.altsep and os.altsep in name) or os.path.isabs(name):
                raise ValueError(f'{name!r} is not a plain directory name; pass directory= to place it elsewhere')
            path = os.path.join(self._base_dir, name)
        else:
            path = os.path.abspath(os.fspath(directory))

        def same(a: str, b: str) -> bool:
            # normcase too: on a case-insensitive filesystem 'General' and 'general' are one store.
            return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))

        for other, (other_path, _) in self._specs.items():
            if same(other_path, path):
                raise ValueError(f'caches {other!r} and {name!r} would share the directory {path}')
        self._specs[name] = (path, self._defaults | settings)

    def __contains__(self, name: object) -> bool:
        return name in self._specs

    def __iter__(self) -> Iterator[N]:
        """The registered names, in registration order."""
        return iter(self._specs)

    def __len__(self) -> int:
        return len(self._specs)

    def directory(self, name: N) -> str:
        """Where the named cache lives."""
        return self._spec(name)[0]

    # -- lookup ----------------------------------------------------------------------------------

    def __getitem__(self, name: N) -> CacheLike:
        """The named cache: opened on first use, then the same object every time.

        A `NullCache` while it can't be opened (see the class docs), or `NullCache(invalidate=real)`
        while it's disabled.

        Raises:
            KeyError: No cache by that name is registered.
            TypeError, ValueError: Its settings are invalid.
        """
        try:
            return self._resolved[name]
        except KeyError:
            return self._resolve(name)

    def _resolve(self, name: N) -> CacheLike:
        self._spec(name)
        failed = self._failed.get(name)
        if failed is not None and self._monotonic() < failed[0]:
            return failed[1]
        try:
            real = self._open(name)
        except _OPEN_ERRORS as exc:
            null = NullCache()
            retry = (
                min(self._retry_open_after, _RETRY_AFTER_BUSY) if isinstance(exc, Timeout) else self._retry_open_after
            )
            self._failed[name] = (self._monotonic() + retry, null)
            self._report_open_error(name, exc, retry)
            return null
        self._failed.pop(name, None)
        disabled = name in self._disabled
        result: CacheLike = NullCache(invalidate=real) if disabled else real
        stored = self._resolved.setdefault(name, result)
        # A switch or close_all that ran between the checks above and the store would otherwise
        # leave a stale answer in place for good: drop it, so the next lookup resolves afresh.
        if (name in self._disabled) != disabled or self._opened.get(name) is not real:
            self._resolved.pop(name, None)
        return stored

    def _open(self, name: N) -> Cache:
        """The real handle for `name`, opening it if needed. Raises if it can't open."""
        opened = self._opened.get(name)
        if opened is not None:
            return opened
        path, settings = self._spec(name)
        cache = Cache(path, **self._bind_reporter(name, settings))
        winner = self._opened.setdefault(name, cache)
        if winner is not cache:
            cache.close(checkpoint=False)
        return winner

    @staticmethod
    def _bind_reporter(name: str, settings: dict[str, Any]) -> dict[str, Any]:
        report = settings.get('on_decode_error')
        if callable(report):
            return settings | {'on_decode_error': lambda key, exc: report(name, key, exc)}
        return settings

    def _spec(self, name: N) -> tuple[str, dict[str, Any]]:
        try:
            return self._specs[name]
        except KeyError:
            raise KeyError(name) from None

    def _report_open_error(self, name: N, exc: Exception, retry: float) -> None:
        if self._on_open_error is None:
            path = self._specs[name][0]
            if isinstance(exc, Timeout):
                log.warning(
                    'cache %r at %s is busy; using a NullCache, retrying in %g s: %s',
                    name,
                    path,
                    retry,
                    exc,
                )
            else:
                log.error(
                    'could not open cache %r at %s; using a NullCache, retrying in %g s',
                    name,
                    path,
                    retry,
                    exc_info=exc,
                )
            return
        try:
            self._on_open_error(name, exc)
        except Exception:
            log.exception('on_open_error failed for cache %r', name)

    # -- switches --------------------------------------------------------------------------------

    def disable(self, *names: N) -> None:
        """Later lookups of these caches return `NullCache(invalidate=real)`."""
        for name in names:
            self._spec(name)
        for name in names:
            self._disabled.add(name)
            self._resolved.pop(name, None)

    def enable(self, *names: N) -> None:
        """Later lookups of these caches return the real cache again."""
        for name in names:
            self._spec(name)
        for name in names:
            self._disabled.discard(name)
            self._resolved.pop(name, None)

    def enabled(self, name: N) -> bool:
        """Whether lookups of this cache return the real one (it may still fail to open)."""
        self._spec(name)
        return name not in self._disabled

    # -- admin -----------------------------------------------------------------------------------

    def stats(self) -> dict[N, CacheStats]:
        """Count and volume for every registered cache, opening each one. Never raises for a cache
        that can't be read: its `error` says why."""
        result: dict[N, CacheStats] = {}
        for name, (path, _) in list(self._specs.items()):
            enabled = name not in self._disabled
            try:
                cache = self._open(name)
                result[name] = CacheStats(name, path, enabled, len(cache), cache.volume())
            except Exception as exc:
                result[name] = CacheStats(name, path, enabled, 0, 0, f'{type(exc).__name__}: {exc}')
        return result

    def clear(self, name: N, *, timeout: float | None = None) -> int:
        """Clear the real cache, even while it's disabled. Returns how many entries were removed.

        Raises:
            KeyError: No cache by that name is registered.
            OSError, RuntimeError: It can't be opened (an admin action shouldn't report 0 for that),
                or `Timeout` from the clear itself.
        """
        return self._open(name).clear(timeout=timeout)

    def expire_all(self, *, timeout: float | None = None) -> dict[N, int | None]:
        """Remove expired entries from every cache. Returns how many each removed, or None for a
        cache that couldn't be opened or expired (logged; the rest still run)."""
        result: dict[N, int | None] = {}
        for name in list(self._specs):
            try:
                result[name] = self._open(name).expire(timeout=timeout)
            except Exception:
                log.exception('could not expire cache %r', name)
                result[name] = None
        return result

    def close_all(self) -> None:
        """Close every opened cache and forget it; a later lookup opens it again.

        A lookup that races this can return a handle that's no longer tracked. It still works: a
        closed handle reopens when it's used.
        """
        opened = list(self._opened.values())
        self._resolved.clear()
        self._opened.clear()
        self._failed.clear()
        for cache in opened:
            try:
                cache.close()
            except Exception:
                log.exception('could not close cache at %s', cache.directory)

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
        /,
    ) -> None:
        self.close_all()
