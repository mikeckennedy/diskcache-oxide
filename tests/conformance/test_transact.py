"""`Cache.transact()`: the semantics diskcache and oxide share.

Calls inside the block on the thread that entered it run in one write transaction: it commits on
exit and rolls back on an exception. A nested block joins the outer one, so its exit commits nothing,
and an exception caught inside the outer block keeps the inner block's writes (no savepoint). The
block's `as` target differs (D-029), so these use the cache itself.
"""

import threading
from typing import Any

import pytest

pytestmark = pytest.mark.requires('transact')


def _read_on_another_thread(cache: Any, key: str) -> Any:
    found: list[Any] = []
    thread = threading.Thread(target=lambda: found.append(cache.get(key)))
    thread.start()
    thread.join(30)
    assert found, 'the reading thread never returned'
    return found[0]


def test_a_block_commits_on_exit(cache: Any) -> None:
    with cache.transact():
        cache.set('a', 1)
        cache.set('b', 2)
        assert cache.get('a') == 1, 'the block reads its own write'
        assert _read_on_another_thread(cache, 'a') is None, 'another thread saw an uncommitted write'
    assert cache.get('a') == 1 and cache.get('b') == 2
    assert _read_on_another_thread(cache, 'b') == 2


def test_a_block_that_raises_rolls_back(cache: Any) -> None:
    cache.set('kept', 'before')
    with pytest.raises(ValueError, match='boom'), cache.transact():
        cache.set('new', 1)
        cache.set('kept', 'inside')
        cache.delete('kept')
        raise ValueError('boom')
    assert cache.get('new') is None
    assert cache.get('kept') == 'before'
    assert cache.set('after', 1) is True, 'the write lock was not released'


def test_a_nested_block_joins_the_outer_one(cache: Any) -> None:
    with cache.transact():
        cache.set('outer', 1)
        with cache.transact():
            cache.set('inner', 2)
            assert cache.get('outer') == 1
        assert cache.get('inner') == 2
    assert cache.get('outer') == 1 and cache.get('inner') == 2


def test_an_inner_exit_doesnt_commit(cache: Any) -> None:
    with cache.transact():
        with cache.transact():
            cache.set('inner', 1)
        assert _read_on_another_thread(cache, 'inner') is None, 'the inner exit committed'
    assert _read_on_another_thread(cache, 'inner') == 1


def test_a_caught_inner_exception_keeps_the_inner_writes(cache: Any) -> None:
    with cache.transact():
        cache.set('outer', 1)
        try:
            with cache.transact():
                cache.set('inner', 2)
                raise KeyError('inner')
        except KeyError:
            pass
        assert cache.get('inner') == 2, 'the inner exception undid its writes'
    assert cache.get('outer') == 1 and cache.get('inner') == 2


def test_an_inner_exception_that_escapes_rolls_back_everything(cache: Any) -> None:
    with pytest.raises(KeyError), cache.transact():
        cache.set('outer', 1)
        with cache.transact():
            cache.set('inner', 2)
            raise KeyError('inner')
    assert cache.get('outer') is None and cache.get('inner') is None
