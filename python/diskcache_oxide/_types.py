"""Type aliases shared by the stubs, the protocols and `NullCache`."""

from collections.abc import Callable
from typing import Protocol, TypedDict

type Key = str | bytes | int | float | bool | None | tuple[Key, ...]
"""A cache key: `str`, `bytes`, `int` (64-bit), `float` (not NaN), `bool`, `None`, or a tuple of those,
nested up to 32 deep. Only the exact types; keys compare by exact type, so `1`, `1.0` and `True` are
three keys."""


class Memoized[**P, R](Protocol):
    """A function wrapped by `memoize`: called as the function was, plus `__cache_key__`."""

    __wrapped__: Callable[P, R]

    def __call__(self, *args: P.args, **kwargs: P.kwargs) -> R: ...

    def __cache_key__(self, *args: P.args, **kwargs: P.kwargs) -> tuple[Key, ...]:
        """The cache key a call with these arguments uses, for `cache.delete(f.__cache_key__(...))`."""
        ...


class Memoizer(Protocol):
    """What `cache.memoize(...)` returns: a decorator."""

    def __call__[**P, R](self, func: Callable[P, R], /) -> Memoized[P, R]: ...


class MaintenanceStats(TypedDict):
    """How background maintenance has gone on a cache in this process: `maintenance_stats()`.

    `interval` is in seconds, 0 when maintenance is off for this cache here (`maintenance_interval=0`,
    or a handle with its own `clock`). `turns` and `removed` count the turns that ran and the entries
    they removed; `failures` counts turns that failed, plus one for each stretch of 3 intervals in
    which turns found work and couldn't get to it (another connection kept the write lock).
    `last_error` is the last failure's message. On a `FanoutCache`, the counts are every shard's summed,
    `interval` is the largest, and `last_error` the first shard's that has one.
    """

    interval: float
    turns: int
    removed: int
    failures: int
    last_error: str | None
