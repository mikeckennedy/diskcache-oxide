"""FanoutCache: the routed single-key calls and the summed `len`/`volume` that diskcache and oxide share.

Both backends route each key to one shard and behave there as a `Cache` does, so these compare
results only, never which shard a key went to: the hashes differ (oxide's is XXH3, stock's adler32),
and so does stock's habit of answering a `Timeout` as a miss, which is a divergence, never compared.
"""

from __future__ import annotations

import contextlib
import gc
import multiprocessing
from collections.abc import Callable, Generator, Iterator
from pathlib import Path
from typing import Any

import pytest

from tests.conformance import proc_workers
from tests.conformance.backends import Backend
from tests.conformance.proc_workers import SqlitePeer
from tests.conformance.test_threads import _stable_fds_inside, _threads_held

pytestmark = [pytest.mark.requires('fanout'), pytest.mark.timeout(60)]

SPAWN = multiprocessing.get_context('spawn')
JOIN_TIMEOUT = 30.0
KEYS = [f'key-{i}' for i in range(200)]


@pytest.fixture
def make_fanout(backend: Backend, request: pytest.FixtureRequest) -> Iterator[Callable[..., Any]]:
    """Factory for FanoutCaches, with the fake clock when the test asked for one; closes them all."""
    created: list[Any] = []
    clock = request.getfixturevalue('fake_clock') if 'fake_clock' in request.fixturenames else None

    def factory(directory: Path, **kwargs: Any) -> Any:
        if clock is not None:
            kwargs.setdefault('clock', clock)
        cache = backend.make_fanout_cache(directory, **kwargs)
        created.append(cache)
        return cache

    yield factory
    for cache in created:
        cache.close()


def test_every_routed_call_round_trips(make_fanout: Callable[..., Any], cache_dir: Path) -> None:
    cache = make_fanout(cache_dir, shards=8)
    for i, key in enumerate(KEYS):
        assert cache.set(key, i) is True
    for i, key in enumerate(KEYS):
        assert cache.get(key) == i
        assert cache[key] == i
        assert key in cache
    assert cache.get('missing') is None
    assert cache.get('missing', 'dflt') == 'dflt'
    assert 'missing' not in cache
    with pytest.raises(KeyError):
        cache['missing']

    cache['item'] = 'via setitem'
    assert cache.get('item') == 'via setitem'
    del cache['item']
    assert 'item' not in cache
    with pytest.raises(KeyError):
        del cache['item']

    assert cache.add(KEYS[0], 'again') is False
    assert cache.get(KEYS[0]) == 0
    assert cache.add('new', 'first') is True
    assert cache.get('new') == 'first'

    assert cache.delete(KEYS[1]) is True
    assert cache.delete(KEYS[1]) is False
    assert cache.pop(KEYS[2]) == 2
    assert cache.pop(KEYS[2], 'gone') == 'gone'

    assert cache.touch(KEYS[3], 100) is True
    assert cache.touch('missing', 100) is False

    assert cache.incr('counter') == 1
    assert cache.incr('counter', 5) == 6
    assert cache.decr('counter', 2) == 4
    assert cache.get('counter') == 4
    with pytest.raises(KeyError):
        cache.incr('no-default', default=None)


def test_expiry_applies_on_every_shard(make_fanout: Callable[..., Any], cache_dir: Path, fake_clock: Any) -> None:
    cache = make_fanout(cache_dir, shards=8)
    for key in KEYS[:40]:
        cache.set(key, 'v', expire=10)
    fake_clock.advance(11)
    for key in KEYS[:40]:
        assert cache.get(key) is None
        assert key not in cache


def test_len_and_volume_add_up_every_shard(make_fanout: Callable[..., Any], cache_dir: Path) -> None:
    cache = make_fanout(cache_dir, shards=8)
    assert len(cache) == 0
    empty = cache.volume()
    for key in KEYS:
        cache.set(key, 'x' * 1000)
    assert len(cache) == len(KEYS)
    assert cache.volume() >= empty + len(KEYS) * 1000
    for key in KEYS:
        cache.delete(key)
    assert len(cache) == 0


def test_clear_expire_and_cull_add_up_every_shard(
    make_fanout: Callable[..., Any], cache_dir: Path, fake_clock: Any
) -> None:
    cache = make_fanout(cache_dir, shards=8)
    # Distinct expire times, and far fewer than 100 per shard: stock's expire() stops after 100 rows
    # that share one (tests/conformance/test_maintenance.py).
    for i, key in enumerate(KEYS):
        cache.set(key, i, expire=10 + i if i < 60 else None)
    fake_clock.advance(100)
    assert cache.expire() == 60
    assert cache.expire() == 0
    assert len(cache) == len(KEYS) - 60
    assert cache.clear() == len(KEYS) - 60
    assert len(cache) == 0
    assert cache.clear() == 0
    assert cache.cull() == 0


def test_cull_evicts_each_shard_under_its_share(make_fanout: Callable[..., Any], cache_dir: Path) -> None:
    shards = 4
    # 1 MiB per shard, and no culling on set, so only cull() removes anything.
    cache = make_fanout(cache_dir, shards=shards, size_limit=shards << 20, cull_limit=0)
    value = b'x' * 100_000
    for i in range(120):  # About 3 MB per shard.
        cache.set(f'big-{i}', value)
    assert len(cache) == 120
    removed = cache.cull()
    assert removed > 0
    assert len(cache) == 120 - removed
    assert len(cache) <= shards * ((1 << 20) // len(value)), 'every shard is under its 1 MiB'


def test_iter_yields_every_key_once(make_fanout: Callable[..., Any], cache_dir: Path) -> None:
    # Only as a set: each backend walks its shards in turn, and their hashes put keys in different
    # shards (stock has no iterkeys).
    cache = make_fanout(cache_dir, shards=8)
    assert list(cache) == []
    for key in KEYS:
        cache.set(key, 1)
    got = list(cache)
    assert len(got) == len(KEYS)
    assert set(got) == set(KEYS)
    for key in KEYS[::2]:
        cache.delete(key)
    assert sorted(cache) == sorted(KEYS[1::2])


def test_values_survive_a_reopen(make_fanout: Callable[..., Any], cache_dir: Path) -> None:
    first = make_fanout(cache_dir, shards=8)
    for i, key in enumerate(KEYS):
        first.set(key, i)
    first.close()
    second = make_fanout(cache_dir, shards=8)
    assert [second.get(key) for key in KEYS] == list(range(len(KEYS)))
    assert len(second) == len(KEYS)


def test_two_handles_on_one_root_see_each_others_writes(make_fanout: Callable[..., Any], cache_dir: Path) -> None:
    a = make_fanout(cache_dir, shards=8)
    b = make_fanout(cache_dir, shards=8)
    for i, key in enumerate(KEYS):
        a.set(key, i)
    assert [b.get(key) for key in KEYS] == list(range(len(KEYS)))
    assert b.add(KEYS[0], 'no') is False


@pytest.mark.parametrize('shards', [1, 64])
def test_one_shard_and_many_shards_with_few_keys(make_fanout: Callable[..., Any], cache_dir: Path, shards: int) -> None:
    cache = make_fanout(cache_dir, shards=shards)
    for key in ('a', 'b', 'c'):
        cache.set(key, key.upper())
    assert len(cache) == 3
    assert [cache.get(k) for k in ('a', 'b', 'c', 'd')] == ['A', 'B', 'C', None]


def _start(target: Callable[..., None], args_list: list[tuple[Any, ...]]) -> list[Any]:
    procs = [SPAWN.Process(target=target, args=args) for args in args_list]
    for p in procs:
        p.start()
    return procs


def _join_all(procs: list[Any]) -> None:
    for p in procs:
        p.join(JOIN_TIMEOUT)
    for p in procs:
        if p.is_alive():
            p.kill()
    assert [p.exitcode for p in procs] == [0] * len(procs)


def test_spawned_processes_racing_add_have_one_winner_per_key(
    backend: Backend, make_fanout: Callable[..., Any], cache_dir: Path
) -> None:
    cache = make_fanout(cache_dir, shards=8)
    keys = KEYS[:32]
    n = 4
    barrier = SPAWN.Barrier(n)
    results = SPAWN.Queue()
    procs = _start(proc_workers.fanout_add_race, [(backend.name, str(cache_dir), keys, barrier, results)] * n)
    won = [results.get(timeout=JOIN_TIMEOUT) for _ in range(n)]
    _join_all(procs)
    winners = {key: [pid for pid, keys_won in won if key in keys_won] for key in keys}
    assert all(len(pids) == 1 for pids in winners.values()), winners
    assert all(cache.get(key) == pids[0] for key, pids in winners.items())


def test_spawned_processes_incr_totals_are_exact(
    backend: Backend, make_fanout: Callable[..., Any], cache_dir: Path
) -> None:
    cache = make_fanout(cache_dir, shards=8)
    keys = [f'counter-{i}' for i in range(8)]
    n, ops = 4, 200
    barrier = SPAWN.Barrier(n)
    _join_all(_start(proc_workers.fanout_incr_many, [(backend.name, str(cache_dir), keys, barrier, ops)] * n))
    assert sum(cache.get(key) for key in keys) == n * ops
    assert [cache.get(key) for key in keys] == [n * ops // len(keys)] * len(keys)


# -- divergences ------------------------------------------------------------------------------------

FD_THREADS = 64


def _fds_held(cache: Any, cache_dir: Path) -> int:
    """The fds inside the root while FD_THREADS threads, each having done a get and a set, stay alive."""
    cache.set('main', 0)
    with _threads_held(cache, FD_THREADS):
        return _stable_fds_inside(cache_dir)


@pytest.mark.diverges(
    backend='diskcache',
    ref='D-037',
    reason='diskcache opens a connection per thread per shard',
    raises=AssertionError,
)
def test_peak_fds_bounded_by_the_shard_budget(make_fanout: Callable[..., Any], cache_dir: Path) -> None:
    # oxide's default budget at 8 shards: one connection per shard (its database and WAL), plus each
    # shard's -shm and pin, so 2 * 8 + 2 * 8.
    shards = 8
    peak = _fds_held(make_fanout(cache_dir, shards=shards), cache_dir)
    assert peak <= 2 * shards + 2 * shards, f'{peak} fds open inside the root with {FD_THREADS} threads'


@pytest.mark.diverges(
    backend='diskcache',
    ref='D-018',
    reason="close() leaves other threads' connections open on every shard",
    raises=AssertionError,
)
def test_close_releases_every_fd_on_every_shard(make_fanout: Callable[..., Any], cache_dir: Path) -> None:
    cache = make_fanout(cache_dir, shards=8)
    # A gc pass would finalize leaked connections and hide whether close() released them.
    gc_was_enabled = gc.isenabled()
    gc.disable()
    try:
        _fds_held(cache, cache_dir)
        cache.close()
        remaining = _stable_fds_inside(cache_dir)
    finally:
        if gc_was_enabled:
            gc.enable()
    assert remaining == 0, f'{remaining} fds still open inside the root after close()'


@contextlib.contextmanager
def _every_shard_locked(backend: Backend, cache_dir: Path, shards: int) -> Generator[None]:
    """Every shard's write lock held by a SQLite connection in another process."""
    with contextlib.ExitStack() as stack:
        for shard in range(shards):
            peer = stack.enter_context(SqlitePeer(backend.db_path(cache_dir / f'{shard:03d}')))
            peer.begin('IMMEDIATE')
            stack.callback(peer.rollback)
        yield


LOCKED_WRITES: dict[str, Callable[[Any], object]] = {
    'set': lambda cache: cache.set('k', 'v'),
    'add': lambda cache: cache.add('new', 'v'),
    'incr': lambda cache: cache.incr('counter'),
    'delete': lambda cache: cache.delete('k'),
    'pop': lambda cache: cache.pop('k'),
    'touch': lambda cache: cache.touch('k', 100),
}


@pytest.mark.parametrize('op', [pytest.param(op, id=op) for op in LOCKED_WRITES])
@pytest.mark.diverges(
    backend='diskcache',
    ref='D-034',
    reason="diskcache's FanoutCache answers a Timeout as False or the default",
    raises=AssertionError,
)
def test_a_write_to_a_locked_shard_raises_timeout(
    backend: Backend, make_fanout: Callable[..., Any], cache_dir: Path, op: str
) -> None:
    shards = 2
    cache = make_fanout(cache_dir, shards=shards, timeout=0.1)
    cache.set('k', 'before')
    with _every_shard_locked(backend, cache_dir, shards):
        try:
            answer = LOCKED_WRITES[op](cache)
        except backend.timeout_error:
            answer = 'raised'
    assert answer == 'raised', f'{op} on a locked shard answered {answer!r} instead of raising Timeout'
    assert cache.get('k') == 'before'


@pytest.mark.backend_only('oxide')
def test_a_durable_fanout_cache_answers_a_locked_shard_as_a_miss(
    backend: Backend, make_fanout: Callable[..., Any], cache_dir: Path
) -> None:
    # oxide's durable=True gives stock FanoutCache's answers to a Timeout, as Cache does (D-028).
    shards = 2
    cache = make_fanout(cache_dir, shards=shards, timeout=0.1, durable=True)
    cache.set('k', 'before')
    with _every_shard_locked(backend, cache_dir, shards):
        assert cache.set('k', 'v') is False
        assert cache.add('new', 'v') is False
        assert cache.incr('counter', 5) == 5
        assert cache.delete('k') is False
        assert cache.pop('k', 'dflt') == 'dflt'
        assert cache.touch('k', 100) is False
    assert cache.get('k') == 'before'
    assert cache._durable_stats()['busy']
