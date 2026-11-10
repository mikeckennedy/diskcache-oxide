"""`memoize`, for `Cache`, `FanoutCache` and `NullCache` (plan 24, release 1).

diskcache 5.6.3's `Cache.memoize`, with its key shape exactly: `(name,) + args + (None,)`, then each
keyword name and value, sorted by name, where `name` defaults to the function's module and qualified
name. So `__cache_key__` returns the tuple diskcache would, and `cache.delete(f.__cache_key__(...))`
forgets one call. Arguments aren't bound to the signature: `f(1, 2)` and `f(1, b=2)` are two
entries, as in diskcache. Differences: `typed=True` adds each argument type's `'module.qualname'`
rather than the class (D-045); on a durable handle reads and writes don't pass `retry=True`, so an
outage runs the function and drops the store (D-028); an argument that can't be part of a key raises `TypeError` (or
`ValueError` for a NaN) naming it, before the function runs (D-048); `async def` functions are
refused (they would cache a coroutine object).
"""

from __future__ import annotations

import functools
import inspect
from collections.abc import Callable, Iterable
from typing import Any

from diskcache_oxide._types import Key

# A miss, told apart from a cached None.
_MISS = object()


def _full_name(func: Any) -> str:
    return f'{func.__module__}.{func.__qualname__}'


def _type_name(value: object) -> str:
    kind = type(value)
    return f'{kind.__module__}.{kind.__qualname__}'


def _checked(what: str, value: object) -> None:
    """`value` can be part of a key, or an error naming the argument (`what`)."""
    from diskcache_oxide.null import _check_key

    try:
        _check_key(value, 1)
    except (TypeError, ValueError) as error:
        raise type(error)(f'memoize: {what} can not be part of a cache key: {error}') from None


def args_to_key(
    base: tuple[Key, ...], args: tuple[Any, ...], kwargs: dict[str, Any], typed: bool, ignore: frozenset[int | str]
) -> tuple[Key, ...]:
    """diskcache's `args_to_key`. The arguments are checked when the key is used (see `_culprit`)."""
    kept = [arg for i, arg in enumerate(args) if i not in ignore]
    named = sorted((k, v) for k, v in kwargs.items() if k not in ignore)
    key: list[Key] = [*base, *kept, None]
    for name, value in named:
        key.extend((name, value))
    if typed:
        key.extend(_type_name(arg) for arg in kept)
        key.extend(_type_name(value) for _, value in named)
    return tuple(key)


def _culprit(args: tuple[Any, ...], kwargs: dict[str, Any], ignore: frozenset[int | str]) -> None:
    """After the cache refused a key: raise naming the argument that can't be part of one, if any."""
    for i, arg in enumerate(args):
        if i not in ignore:
            _checked(f'argument {i}', arg)
    for name, value in sorted(kwargs.items()):
        if name not in ignore:
            _checked(f'argument {name!r}', value)


def memoize(
    cache: Any,
    name: str | None = None,
    typed: bool = False,
    expire: float | None = None,
    ignore: Iterable[int | str] = (),
    retry: bool = True,
) -> Callable[[Callable[..., Any]], Any]:
    """The decorator `cache.memoize(...)` returns. Reads and writes pass `retry`: True, as in diskcache,
    except on a durable handle, where an outage should run the function rather than wait."""
    if callable(name):
        raise TypeError('name cannot be callable')
    from diskcache_oxide.null import _check_seconds

    _check_seconds('expire', expire)
    if type(typed) is not bool:
        raise TypeError(f'typed must be a bool, not {type(typed).__name__}')
    if name is not None:
        _checked('name', name)
    skip = frozenset(ignore)
    stores = expire is None or expire > 0

    def decorator(func: Callable[..., Any]) -> Any:
        if inspect.iscoroutinefunction(func):
            raise TypeError(
                f'memoize can not cache an async function ({_full_name(func)}): it would store the coroutine'
            )
        base: tuple[Key, ...] = (_full_name(func) if name is None else name,)

        def __cache_key__(*args: Any, **kwargs: Any) -> tuple[Key, ...]:
            """The cache key for these arguments."""
            return args_to_key(base, args, kwargs, typed, skip)

        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            key = __cache_key__(*args, **kwargs)
            try:
                result = cache.get(key, _MISS, retry=retry)
            except TypeError, ValueError:
                # Before the function runs: name the argument if one is to blame, else re-raise.
                _culprit(args, kwargs, skip)
                raise
            if result is _MISS:
                result = func(*args, **kwargs)
                if stores:
                    cache.set(key, result, expire, retry=retry)
            return result

        wrapper.__cache_key__ = __cache_key__  # ty: ignore[unresolved-attribute]
        return wrapper

    return decorator
