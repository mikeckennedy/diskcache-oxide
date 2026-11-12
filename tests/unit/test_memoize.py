"""`memoize`, oxide-only (plan 24, release 1; D-045, D-048).

The conformance suite checks diskcache's behavior. These check ours: type names for `typed=True`,
an argument that can't be a key refused before the function runs (naming it), `async def` refused,
a `NullCache` that runs every call but still checks the arguments, a durable handle in an outage,
and the typing (`Memoized` keeps the function's signature).
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import pytest
from diskcache_oxide import Cache, FanoutCache, Memoized, NullCache

from tests.unit.test_durable import peer_holding_the_write_lock


@pytest.fixture
def cache(tmp_path: Path) -> Any:
    with Cache(tmp_path / 'c') as c:
        yield c


def test_typed_appends_type_names(cache: Any) -> None:
    @cache.memoize(name='t', typed=True)
    def f(a: Any, b: Any = None) -> Any:
        return a

    assert f.__cache_key__(1, b=2.0) == ('t', 1, None, 'b', 2.0, 'builtins.int', 'builtins.float')


@pytest.mark.parametrize(
    ('args', 'kwargs', 'error', 'names'),
    [
        pytest.param(([1],), {}, TypeError, 'argument 0', id='list'),
        pytest.param((1, {'a': 1}), {}, TypeError, 'argument 1', id='dict'),
        pytest.param((1,), {'b': object()}, TypeError, "argument 'b'", id='keyword-object'),
        pytest.param((math.nan,), {}, ValueError, 'argument 0', id='nan'),
        pytest.param(((1, [2]),), {}, TypeError, 'argument 0', id='nested-list'),
    ],
)
def test_an_argument_that_cant_be_a_key_raises_before_the_function_runs(
    cache: Any, args: tuple[Any, ...], kwargs: dict[str, Any], error: type[Exception], names: str
) -> None:
    calls = []

    @cache.memoize()
    def f(*args: Any, **kwargs: Any) -> None:
        calls.append(1)

    with pytest.raises(error, match=f'memoize: {names} can not be part of a cache key'):
        f(*args, **kwargs)
    assert calls == []


def test_an_ignored_argument_may_be_anything(cache: Any) -> None:
    @cache.memoize(ignore=(0, 'conn'))
    def f(conn_pos: object, n: int, *, conn: object) -> int:
        return n * 2

    assert f(object(), 2, conn=object()) == 4
    assert f.__cache_key__(object(), 2, conn=object())[1:] == (2, None)


def test_an_async_function_is_refused(cache: Any) -> None:
    async def f() -> int:
        return 1

    with pytest.raises(TypeError, match='async function'):
        cache.memoize()(f)


@pytest.mark.parametrize('bad', [{'typed': 1}, {'expire': True}, {'expire': 'soon'}])
def test_bad_options_raise(cache: Any, bad: dict[str, Any]) -> None:
    with pytest.raises(TypeError):
        cache.memoize(**bad)


def test_a_null_cache_runs_every_call_but_checks_the_arguments() -> None:
    calls = []
    null = NullCache()

    @null.memoize()
    def f(x: Any) -> Any:
        calls.append(x)
        return x

    assert f(1) == f(1) == 1
    assert calls == [1, 1]
    with pytest.raises(TypeError):
        f([1])


def test_a_durable_handle_in_an_outage_runs_the_function_and_drops_the_store(tmp_path: Path) -> None:
    calls = []
    with Cache(tmp_path, durable=True, timeout=0.05) as cache:

        @cache.memoize()
        def f(x: int) -> int:
            calls.append(x)
            return x

        assert f(1) == 1
        with peer_holding_the_write_lock(tmp_path):
            assert f(1) == 1, 'a stored result still reads'
            assert f(2) == 2, "a new one runs the function at once, though it can't be stored"
            assert f(2) == 2
        assert calls == [1, 2, 2]
        assert f(2) == 2
        assert calls == [1, 2, 2, 2], 'stored now that the lock is gone'
        assert f(2) == 2
        assert calls == [1, 2, 2, 2]


def test_a_fanout_cache_memoizes_with_typed_keys_on_their_shards(tmp_path: Path) -> None:
    with FanoutCache(tmp_path, shards=4) as cache:

        @cache.memoize()
        def square(n: int) -> int:
            return n * n

        assert [square(n) for n in range(20)] == [n * n for n in range(20)]
        assert len(cache) == 20
        assert cache.get(square.__cache_key__(3)) == 9


def test_the_wrapper_is_typed_as_the_function(cache: Any) -> None:
    @cache.memoize()
    def f(a: int, b: str = 'x') -> str:
        return b * a

    memoized: Memoized[[int, str], str] = f
    assert memoized(2, 'y') == 'yy'
    assert memoized.__cache_key__(2, 'y')[1:] == (2, 'y', None)


def test_a_name_that_cant_be_a_key_raises_when_decorating(cache: Any) -> None:
    with pytest.raises(TypeError, match='memoize: name can not be part of a cache key'):
        cache.memoize(name=[1])
