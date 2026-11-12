"""`memoize` (plan 24, release 1), against diskcache 5.6.3's `Cache.memoize`.

The key shape is diskcache's exactly: `(name,) + args + (None,) + sorted keyword names and values`,
where `name` defaults to the function's module and qualified name. So `__cache_key__` returns the
same tuple on both backends, apart from `typed=True` (D-045).
"""

from __future__ import annotations

from typing import Any

import pytest

pytestmark = pytest.mark.requires('memoize')


def counted(cache: Any, **options: Any) -> tuple[Any, list[tuple[Any, ...]]]:
    calls: list[tuple[Any, ...]] = []

    @cache.memoize(**options)
    def add(a: Any, b: Any = 0, *, c: Any = 0) -> Any:
        calls.append((a, b, c))
        return a + b + c

    return add, calls


def test_a_second_call_with_the_same_arguments_is_served_from_the_cache(cache: Any) -> None:
    add, calls = counted(cache)
    assert add(1, 2) == 3
    assert add(1, 2) == 3
    assert add(2, 2) == 4
    assert calls == [(1, 2, 0), (2, 2, 0)]


def test_the_key_is_diskcaches_shape(cache: Any) -> None:
    add, _ = counted(cache)
    name = f'{__name__}.counted.<locals>.add'
    assert add.__cache_key__(1, 2) == (name, 1, 2, None)
    assert add.__cache_key__(1, c=3, b=2) == (name, 1, None, 'b', 2, 'c', 3)
    assert add.__cache_key__() == (name, None)


def test_arguments_are_not_bound_to_the_signature(cache: Any) -> None:
    # As in diskcache: f(1, 2) and f(1, b=2) are two entries, and defaults aren't filled in.
    add, calls = counted(cache)
    add(1, 2)
    add(1, b=2)
    add(1)
    add(1, 0)
    assert len(calls) == 4


def test_name_replaces_the_qualified_name(cache: Any) -> None:
    add, _ = counted(cache, name='totals')
    assert add.__cache_key__(1) == ('totals', 1, None)
    assert add(1, 2) == 3
    assert cache.get(('totals', 1, 2, None)) == 3


def test_deleting_the_key_forces_a_recompute(cache: Any) -> None:
    add, calls = counted(cache)
    add(1, 2)
    assert cache.delete(add.__cache_key__(1, 2)) is True
    add(1, 2)
    assert len(calls) == 2


def test_ignore_drops_positions_and_names_from_the_key(cache: Any) -> None:
    add, calls = counted(cache, ignore=(1, 'c'))
    name = f'{__name__}.counted.<locals>.add'
    assert add.__cache_key__(1, 2, c=3) == (name, 1, None)
    assert add(1, 2, c=3) == 6
    assert add(1, 5, c=7) == 6, 'ignored arguments share the first result'
    assert len(calls) == 1


@pytest.mark.parametrize('expire', [0, -1])
def test_a_zero_or_negative_expire_never_stores(cache: Any, expire: int) -> None:
    add, calls = counted(cache, expire=expire)
    add(1)
    add(1)
    assert len(calls) == 2
    assert len(cache) == 0


def test_expire_sets_the_entrys_expiry(cache: Any) -> None:
    add, _ = counted(cache, expire=60)
    add(1)
    _, expire_time = cache.get(add.__cache_key__(1), expire_time=True)
    assert expire_time is not None


def test_a_none_result_is_cached(cache: Any) -> None:
    calls = []

    @cache.memoize()
    def nothing(x: int) -> None:
        calls.append(x)

    assert nothing(1) is None
    assert nothing(1) is None
    assert calls == [1]


def test_an_exception_stores_nothing(cache: Any) -> None:
    calls = []

    @cache.memoize()
    def fails(x: int) -> int:
        calls.append(x)
        raise ValueError(x)

    for _ in range(2):
        with pytest.raises(ValueError):
            fails(1)
    assert calls == [1, 1]
    assert len(cache) == 0


def test_memoize_without_parentheses_raises(cache: Any) -> None:
    with pytest.raises(TypeError, match='name cannot be callable'):

        @cache.memoize
        def f() -> None:
            pass


def test_the_wrapper_keeps_the_functions_identity(cache: Any) -> None:
    def original(x: int) -> int:
        """Doubles."""
        return 2 * x

    wrapped = cache.memoize()(original)
    assert wrapped.__wrapped__ is original
    assert (wrapped.__name__, wrapped.__doc__) == ('original', 'Doubles.')


@pytest.mark.diverges(backend='oxide', ref='D-045', reason='typed=True stores type names', raises=AssertionError)
def test_typed_appends_the_argument_types(cache: Any) -> None:
    add, _ = counted(cache, typed=True, name='t')
    assert add.__cache_key__(1, 2.0, c='x') == ('t', 1, 2.0, None, 'c', 'x', int, float, str)


def test_typed_tells_an_int_from_a_float(cache: Any) -> None:
    add, calls = counted(cache, typed=True)
    add(1)
    add(1.0)
    assert len(calls) == 2


@pytest.mark.requires('fanout')
def test_a_fanout_cache_memoizes_too(backend: Any, cache_dir: Any) -> None:
    cache = backend.make_fanout_cache(cache_dir, shards=4)
    try:
        add, calls = counted(cache)
        add(1, 2)
        add(1, 2)
        assert calls == [(1, 2, 0)]
    finally:
        cache.close()


@pytest.mark.diverges(backend='oxide', ref='D-048', reason='arguments must be key types', raises=TypeError)
def test_any_picklable_argument_is_part_of_the_key(cache: Any) -> None:
    @cache.memoize()
    def total(values: list[int]) -> int:
        return sum(values)

    assert total([1, 2]) == 3
    assert total([1, 2]) == 3
