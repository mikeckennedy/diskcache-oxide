"""Type aliases shared by the stubs, the protocols and `NullCache`."""

from collections.abc import Callable
from typing import Protocol

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
