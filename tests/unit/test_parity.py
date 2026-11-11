"""Release 2's small parity calls, oxide-only (plan 24): what conformance can't check.

`reversed()` across a FanoutCache's shards, pickling (options carried, the registries, a durable
handle, an unpicklable `clock`, a spawned process), `reset()`'s refusals and a FanoutCache's split,
`durable`, and the sentinels.
"""

from __future__ import annotations

import multiprocessing
import pickle
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from diskcache_oxide import ENOVAL, UNKNOWN, Cache, FanoutCache, NullCache, TypeRegistry, registry

from tests.keys import str_keys


def test_reversed_walks_shards_last_to_first(tmp_path: Path) -> None:
    with FanoutCache(tmp_path, shards=4) as cache:
        for i in range(40):
            cache.set(f'k{i}', i)
        forward = str_keys(cache)
        assert str_keys(reversed(cache)) == forward[::-1]


def test_reversed_includes_every_key_type(tmp_path: Path) -> None:
    with Cache(tmp_path) as cache:
        keys = ['a', 1, (2, None), b'x']
        for key in keys:
            cache.set(key, 1)
        assert list(reversed(cache)) == keys[::-1]


def test_durable_reads_back(tmp_path: Path) -> None:
    with Cache(tmp_path / 'a') as plain, Cache(tmp_path / 'b', durable=True) as durable:
        assert (plain.durable, durable.durable, NullCache().durable) == (False, True, False)
    with FanoutCache(tmp_path / 'f', shards=2, durable=True) as fan:
        assert fan.durable


def test_a_pickled_cache_keeps_its_options(tmp_path: Path) -> None:
    with Cache(tmp_path, timeout=2.5, serializer='json', durable=True, on_decode_error='miss') as cache:
        again = pickle.loads(pickle.dumps(cache))
        try:
            assert (again.timeout, again.durable) == (2.5, True)
            again.set('k', [1, 2])
            assert cache.get('k') == [1, 2]
        finally:
            again.close()


def test_a_pickled_fanout_cache_keeps_its_shard_count(tmp_path: Path) -> None:
    with FanoutCache(tmp_path, shards=3) as cache:
        cache.set('k', 1)
        # Reopening needs no shards= (the root records it), but the pickle carries the real count.
        with FanoutCache(tmp_path) as reopened:
            again = pickle.loads(pickle.dumps(reopened))
            try:
                assert again.shards == 3 and again.get('k') == 1
            finally:
                again.close()


def test_an_unpicklable_clock_makes_the_cache_unpicklable(tmp_path: Path) -> None:
    with Cache(tmp_path, clock=lambda: 0.0) as cache, pytest.raises((pickle.PicklingError, AttributeError)):
        pickle.dumps(cache)


@dataclass
class Point:
    x: int


def test_the_global_registry_pickles_by_reference_and_a_local_one_by_its_registrations() -> None:
    assert pickle.loads(pickle.dumps(registry)) is registry
    local = TypeRegistry()
    local.register(Point, name='tests.parity.Point')
    again = pickle.loads(pickle.dumps(local))
    assert again is not local
    assert again.registered() == {'tests.parity.Point': Point}


def test_a_cache_with_its_own_types_pickles(tmp_path: Path) -> None:
    local = TypeRegistry()
    local.register(Point, name='tests.parity.Point')
    with Cache(tmp_path, types=local) as cache:
        cache.set('p', Point(3))
        again = pickle.loads(pickle.dumps(cache))
        try:
            assert again.get('p') == Point(3)
        finally:
            again.close()


def _read_in_child(cache: Any, queue: Any) -> None:
    queue.put(cache.get('k'))
    cache.close()


def test_a_cache_passed_to_a_spawned_process_opens_there(tmp_path: Path) -> None:
    context = multiprocessing.get_context('spawn')
    with Cache(tmp_path) as cache:
        cache.set('k', 'from the parent')
        queue = context.Queue()
        child = context.Process(target=_read_in_child, args=(cache, queue))
        child.start()
        assert queue.get(timeout=30) == 'from the parent'
        child.join(30)
        assert child.exitcode == 0


@pytest.mark.parametrize(
    ('call', 'error', 'match'),
    [
        (lambda c: c.reset('sqlite_mmap_size', 1), ValueError, 'D-023'),
        (lambda c: c.reset('eviction_policy', 'least-recently-used'), ValueError, 'D-022'),
        (lambda c: c.reset('eviction_policy', 'least-frequently-used'), ValueError, 'every read a write'),
        (lambda c: c.reset('eviction_policy', 'bogus'), ValueError, "'least-recently-stored' or 'none'"),
        (lambda c: c.reset('eviction_policy', 1), TypeError, '(?i)str'),
        (lambda c: c.reset('statistics', True), ValueError, 'stats'),
        (lambda c: c.reset('nonsense'), ValueError, 'size_limit'),
        (lambda c: c.reset('cull_limit', 5, update=False), ValueError, 'update=False'),
        (lambda c: c.reset('size_limit', 1000), ValueError, 'at least'),
        (lambda c: c.reset('cull_limit', -1), ValueError, 'negative'),
        (lambda c: c.reset('cull_limit', True), TypeError, 'int'),
    ],
)
@pytest.mark.parametrize('make', [lambda p: Cache(p), lambda p: FanoutCache(p, shards=2), lambda p: NullCache()])
def test_reset_refuses_what_it_cannot_do(
    tmp_path: Path, make: Any, call: Any, error: type[Exception], match: str
) -> None:
    cache = make(tmp_path)
    with pytest.raises(error, match=match):
        call(cache)
    cache.close()


def test_reset_reads_what_another_handle_stored(tmp_path: Path) -> None:
    with Cache(tmp_path) as one, Cache(tmp_path / '..' / tmp_path.name) as two:
        two.reset('cull_limit', 7)
        assert one.reset('cull_limit') == 7
        assert one.reset('cull_limit', ENOVAL) == 7


def test_a_fanout_size_limit_is_split_and_read_back_as_the_total(tmp_path: Path) -> None:
    with FanoutCache(tmp_path, shards=4) as cache:
        assert cache.reset('size_limit', 8 << 20) == 8 << 20
        assert cache.reset('size_limit') == 8 << 20
        assert cache.size_limit == 8 << 20
        with pytest.raises(ValueError, match='for 4 shards'):
            cache.reset('size_limit', 3 << 20)


def test_the_sentinels_are_singletons_across_pickle() -> None:
    assert pickle.loads(pickle.dumps(ENOVAL)) is ENOVAL
    assert pickle.loads(pickle.dumps(UNKNOWN)) is UNKNOWN
    assert not isinstance(ENOVAL, tuple)
    assert repr(ENOVAL) == 'ENOVAL'


def test_reset_is_refused_inside_a_transaction(tmp_path: Path) -> None:
    with Cache(tmp_path, size_limit=2 << 20) as cache:
        with cache.transact(), pytest.raises(RuntimeError, match='rollback'):
            cache.reset('size_limit', 1 << 30)
        assert cache.size_limit == 2 << 20
        with cache.transact():
            assert cache.reset('size_limit') == 2 << 20, 'reading is fine'


def _read_fanout(cache: Any, queue: Any) -> None:
    queue.put((cache.get('k'), cache.shards, cache.durable, cache.get('p')))
    cache.close()


def test_a_fanout_cache_with_options_and_types_crosses_a_spawn(tmp_path: Path) -> None:
    local = TypeRegistry()
    local.register(Point, name='tests.parity.Point')
    context = multiprocessing.get_context('spawn')
    with FanoutCache(tmp_path, shards=3, durable=True, types=local) as cache:
        cache.set('k', 'v')
        cache.set('p', Point(4))
        queue = context.Queue()
        child = context.Process(target=_read_fanout, args=(cache, queue))
        child.start()
        assert queue.get(timeout=30) == ('v', 3, True, Point(4))
        child.join(30)
        assert child.exitcode == 0
