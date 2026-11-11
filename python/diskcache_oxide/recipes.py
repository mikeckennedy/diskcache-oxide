"""Coordination recipes (plan 24, release 2): diskcache 5.6.3's `recipes.py`, atomic across every
thread and process sharing a cache directory.

`Averager`, `Lock`, `RLock`, `BoundedSemaphore`, `throttle`, `barrier` and `memoize_stampede`, with
diskcache's signatures and behavior, and these differences:

- **The coordination recipes refuse a `NullCache` or a `durable=True` handle** (`TypeError`): on a
  disabled cache every caller would hold the lock, and on a durable one a dropped `delete` would
  leave it held forever. `memoize_stampede` is a memoize, so it follows `memoize`'s rules instead.
- **A recipe that would wait inside this thread's own transaction on its cache raises
  `RuntimeError`:** polling there holds the write lock, so no other process could ever release.
- **On a `FanoutCache`** each recipe's transaction covers its key's shard (`transact(key=...)`).
- **`RLock`'s owner** is `'{pid}-{tid}-{nonce}'`, the nonce made per process (and again in a forked
  child), so containers sharing a volume, or a reused pid, never share a lock.
- **`throttle`** treats a missing bucket (after an idle gap longer than `expire`, a `clear()` or a
  cull) as full instead of raising `TypeError`, and adds its bucket at decoration rather than
  resetting it, so every worker that imports the module doesn't refill it.
- **`tag=`** is any key type, as for `set()`, compared by exact type (D-052). `barrier` passes it on
  to the lock factory only when it isn't None, so a factory written without a `tag` parameter works.

Coordination keys can be culled like any other (least-recently-stored culls the oldest first, and a
long-held lock is old), and `clear()` releases them: keep them in a cache of their own.
"""

from __future__ import annotations

import functools
import inspect
import math
import os
import random
import threading
import time
from collections.abc import Callable
from typing import Any

from diskcache_oxide._memoize import _MISS, _culprit, _full_name, args_to_key
from diskcache_oxide._types import Key

__all__ = ['Averager', 'BoundedSemaphore', 'Lock', 'RLock', 'barrier', 'memoize_stampede', 'throttle']

# How long a waiting recipe sleeps between tries, as diskcache's.
_POLL = 0.001


def _new_nonce() -> str:
    return os.urandom(8).hex()


_NONCE = _new_nonce()


def _renew_nonce() -> None:
    global _NONCE
    _NONCE = _new_nonce()


# A forked child is another owner: it can neither re-enter nor release its parent's RLock.
os.register_at_fork(after_in_child=_renew_nonce)


def _coordinating(cache: Any, what: str) -> None:
    """Refuse a cache a lock can't work on (decision 10)."""
    if not getattr(cache, 'enabled', True):
        raise TypeError(f'{what} needs a real cache: on a NullCache every caller would hold it at once')
    if getattr(cache, 'durable', False):
        raise TypeError(
            f'{what} needs a cache without durable=True: an outage would look like a held lock, '
            'and a dropped delete would keep it held'
        )


def _check_tag(tag: object) -> None:
    from diskcache_oxide.null import _check_tag as check

    check(tag)


def _transact(cache: Any, key: Key) -> Any:
    """A transaction covering `key`: one shard on a FanoutCache."""
    if hasattr(cache, 'shard_of'):
        return cache.transact(key=key, retry=True)
    return cache.transact(retry=True)


def _before_waiting(cache: Any, what: str) -> None:
    """Refuse to wait inside this thread's own transaction on `cache`. A transaction on another cache
    isn't refused: the wait holds that cache's write lock, and a fork waits for it, but no one is
    stopped from releasing."""
    held = getattr(cache, '_in_transaction', None)
    if held is not None and held():
        raise RuntimeError(
            f"{what} would wait inside this thread's transaction on {cache!r}, holding its write lock, "
            'so no other process could ever release it'
        )


class Averager:
    """A running average stored under `key`: `add(value)`, `get()`, `pop()`.

    The total and count are updated together in one transaction, so every thread and process adding
    to it agrees. `None` while nothing has been added.
    """

    def __init__(self, cache: Any, key: Key, expire: float | None = None, tag: Key = None) -> None:
        _coordinating(cache, 'Averager')
        _check_tag(tag)
        self._tag = tag
        self._cache = cache
        self._key = key
        self._expire = expire

    def add(self, value: float) -> None:
        """Add `value` to the average."""
        with _transact(self._cache, self._key):
            total, count = self._cache.get(self._key, default=(0.0, 0))
            self._cache.set(self._key, (total + value, count + 1), expire=self._expire, tag=self._tag)

    def get(self) -> float | None:
        """The current average, or None if nothing has been added."""
        total, count = self._cache.get(self._key, default=(0.0, 0), retry=True)
        return None if count == 0 else total / count

    def pop(self) -> float | None:
        """The current average, removing it."""
        total, count = self._cache.pop(self._key, default=(0.0, 0), retry=True)
        return None if count == 0 else total / count


class Lock:
    """A lock shared by every thread and process on the cache: `acquire()`, `release()`, `locked()`,
    or `with lock:`. Not reentrant (see `RLock`); any holder of the key can release it.

    `acquire()` polls every millisecond until it gets the lock. Give it an `expire` so a holder that
    dies without releasing doesn't keep it forever.
    """

    def __init__(self, cache: Any, key: Key, expire: float | None = None, tag: Key = None) -> None:
        _coordinating(cache, 'Lock')
        _check_tag(tag)
        self._tag = tag
        self._cache = cache
        self._key = key
        self._expire = expire

    def acquire(self) -> None:
        """Wait for the lock and take it."""
        while not self._cache.add(self._key, None, expire=self._expire, tag=self._tag, retry=True):
            _before_waiting(self._cache, 'Lock.acquire()')
            time.sleep(_POLL)

    def release(self) -> None:
        """Release the lock."""
        self._cache.delete(self._key, retry=True)

    def locked(self) -> bool:
        """Whether someone holds the lock."""
        return self._key in self._cache

    def __enter__(self) -> None:
        self.acquire()

    def __exit__(self, *exc_info: object) -> None:
        self.release()


class RLock:
    """A reentrant lock shared by every thread and process on the cache: the thread holding it may
    take it again, and must release it as many times. Another thread or process waits."""

    def __init__(self, cache: Any, key: Key, expire: float | None = None, tag: Key = None) -> None:
        _coordinating(cache, 'RLock')
        _check_tag(tag)
        self._tag = tag
        self._cache = cache
        self._key = key
        self._expire = expire

    @staticmethod
    def _owner() -> str:
        return f'{os.getpid()}-{threading.get_ident()}-{_NONCE}'

    def acquire(self) -> None:
        """Wait for the lock, or take it again if this thread holds it."""
        owner = self._owner()
        while True:
            with _transact(self._cache, self._key):
                holder, count = self._cache.get(self._key, default=(None, 0))
                if holder == owner or count == 0:
                    self._cache.set(self._key, (owner, count + 1), expire=self._expire, tag=self._tag)
                    return
            _before_waiting(self._cache, 'RLock.acquire()')
            time.sleep(_POLL)

    def release(self) -> None:
        """Release once. Raises `AssertionError` if this thread doesn't hold the lock."""
        owner = self._owner()
        with _transact(self._cache, self._key):
            holder, count = self._cache.get(self._key, default=(None, 0))
            if holder != owner or count <= 0:
                raise AssertionError('cannot release un-acquired lock')
            self._cache.set(self._key, (holder, count - 1), expire=self._expire, tag=self._tag)

    def __enter__(self) -> None:
        self.acquire()

    def __exit__(self, *exc_info: object) -> None:
        self.release()


class BoundedSemaphore:
    """A semaphore shared by every thread and process on the cache: at most `value` holders at once.
    Releasing more than were acquired raises `AssertionError`."""

    def __init__(self, cache: Any, key: Key, value: int = 1, expire: float | None = None, tag: Key = None) -> None:
        _coordinating(cache, 'BoundedSemaphore')
        _check_tag(tag)
        self._tag = tag
        self._cache = cache
        self._key = key
        self._value = value
        self._expire = expire

    def acquire(self) -> None:
        """Wait for a free slot and take it."""
        while True:
            with _transact(self._cache, self._key):
                free = self._cache.get(self._key, default=self._value)
                if free > 0:
                    self._cache.set(self._key, free - 1, expire=self._expire, tag=self._tag)
                    return
            _before_waiting(self._cache, 'BoundedSemaphore.acquire()')
            time.sleep(_POLL)

    def release(self) -> None:
        """Give a slot back."""
        with _transact(self._cache, self._key):
            free = self._cache.get(self._key, default=self._value)
            if not self._value > free:
                raise AssertionError('cannot release un-acquired semaphore')
            self._cache.set(self._key, free + 1, expire=self._expire, tag=self._tag)

    def __enter__(self) -> None:
        self.acquire()

    def __exit__(self, *exc_info: object) -> None:
        self.release()


def throttle(
    cache: Any,
    count: int,
    seconds: float,
    name: str | None = None,
    expire: float | None = None,
    tag: Key = None,
    time_func: Callable[[], float] = time.time,
    sleep_func: Callable[[float], object] = time.sleep,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """A decorator allowing at most `count` calls every `seconds`, across every thread and process on
    the cache: a token bucket stored under `name` (the function's qualified name by default). A call
    with no token waits (`sleep_func`) until one is due."""
    _coordinating(cache, 'throttle')
    _check_tag(tag)

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        rate = count / float(seconds)
        key = _full_name(func) if name is None else name
        # Added, not set: every worker importing this module mustn't refill the shared bucket.
        cache.add(key, (time_func(), count), expire=expire, tag=tag, retry=True)

        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            while True:
                with _transact(cache, key):
                    now = time_func()
                    # A bucket that expired or was cleared is a full one.
                    last, tally = cache.get(key, default=(now, count))
                    tally += (now - last) * rate
                    delay = 0.0
                    if tally > count:
                        cache.set(key, (now, count - 1), expire, tag=tag)
                    elif tally >= 1:
                        cache.set(key, (now, tally - 1), expire, tag=tag)
                    else:
                        delay = (1 - tally) / rate
                if not delay:
                    break
                _before_waiting(cache, 'throttle')
                sleep_func(delay)
            return func(*args, **kwargs)

        return wrapper

    return decorator


def barrier(
    cache: Any,
    lock_factory: Callable[..., Any],
    name: str | None = None,
    expire: float | None = None,
    tag: Key = None,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """A decorator running the function under a lock made by `lock_factory(cache, key, expire=...)`
    (`Lock`, `RLock`, `BoundedSemaphore`, or one of your own): one caller at a time, everywhere."""
    _check_tag(tag)

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        key = _full_name(func) if name is None else name
        lock = (
            lock_factory(cache, key, expire=expire) if tag is None else lock_factory(cache, key, expire=expire, tag=tag)
        )

        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            with lock:
                return func(*args, **kwargs)

        return wrapper

    return decorator


def memoize_stampede(
    cache: Any,
    expire: float,
    name: str | None = None,
    typed: bool = False,
    tag: Key = None,
    beta: float = 1,
    ignore: Any = (),
) -> Callable[[Callable[..., Any]], Any]:
    """`memoize` with cache stampede protection, as diskcache's: a result close to expiry is
    recomputed early, in a background thread, by one caller (Vattani et al., VLDB 2015), while every
    caller keeps getting the cached result. Results are stored as `(result, seconds it took)`.

    Keys, `typed`, `ignore` and the argument rules are `memoize`'s (D-048). It works on a `NullCache`
    (every call runs) and a durable handle (an outage runs the function), as `memoize` does. The
    time left is measured with `time.time()`, as diskcache's, so a cache opened with a `clock=` that
    isn't wall time recomputes on that clock's expiry but judges "close to expiring" by wall time.
    """
    if callable(name):
        raise TypeError('name cannot be callable')
    _check_tag(tag)
    skip = frozenset(ignore)
    retry = not getattr(cache, 'durable', False)

    def decorator(func: Callable[..., Any]) -> Any:
        if inspect.iscoroutinefunction(func):
            raise TypeError(f'memoize_stampede can not cache an async function ({_full_name(func)})')
        base: tuple[Key, ...] = (_full_name(func) if name is None else name,)

        def timer(*args: Any, **kwargs: Any) -> tuple[Any, float]:
            start = time.time()
            result = func(*args, **kwargs)
            return result, time.time() - start

        def __cache_key__(*args: Any, **kwargs: Any) -> tuple[Key, ...]:
            """The cache key for these arguments."""
            return args_to_key(base, args, kwargs, typed, skip)

        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            key = __cache_key__(*args, **kwargs)
            try:
                pair, expire_time = cache.get(key, default=_MISS, expire_time=True, retry=retry)
            except TypeError, ValueError:
                _culprit(args, kwargs, skip)
                raise
            if pair is not _MISS:
                result, delta = pair
                ttl = (expire_time or math.inf) - time.time()
                # -log(1 - random()) rather than -log(random()), which raises if random() gives 0.0.
                if -delta * beta * math.log(1.0 - random.random()) < ttl:
                    return result
                # One caller recomputes early; the others keep the cached result meanwhile.
                claim = ('diskcache_oxide.recompute', *key)
                if cache.add(claim, None, expire=delta, retry=retry):

                    def recompute() -> None:
                        # No `with cache:`: that closes the handle here (D-018).
                        cache.set(key, timer(*args, **kwargs), expire=expire, tag=tag, retry=retry)

                    threading.Thread(target=recompute, daemon=True).start()
                return result
            pair = timer(*args, **kwargs)
            cache.set(key, pair, expire=expire, tag=tag, retry=retry)
            return pair[0]

        wrapper.__cache_key__ = __cache_key__  # ty: ignore[unresolved-attribute]
        return wrapper

    return decorator
