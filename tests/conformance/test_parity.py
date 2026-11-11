"""Small parity calls (plan 24, release 2): `reversed()`, `pop(expire_time=)`, pickling, `reset()`,
`stats()` and `check()`, against diskcache 5.6.3.
"""

from __future__ import annotations

import os
import pickle
import time
from pathlib import Path
from typing import Any

import pytest

from tests.conformance.backends import Backend

HOUR = 3600


@pytest.mark.requires('reversed')
def test_reversed_is_insertion_order_backwards(cache: Any) -> None:
    for key in ('b', 'c', 'a'):
        cache.set(key, 1)
    cache.set('b', 2)  # An overwrite keeps its place.
    assert list(reversed(cache)) == ['a', 'c', 'b']


@pytest.mark.requires('reversed')
def test_reversed_of_an_empty_cache_is_empty(cache: Any) -> None:
    assert list(reversed(cache)) == []


@pytest.mark.requires('pop_expire_time')
def test_pop_can_return_the_expire_time(cache: Any) -> None:
    cache.set('k', 'v', expire=60)
    value, expire_time = cache.pop('k', expire_time=True)
    assert value == 'v'
    assert expire_time is not None and expire_time > time.time()
    assert cache.pop('k', default='d', expire_time=True) == ('d', None)
    cache.set('forever', 1)
    assert cache.pop('forever', expire_time=True) == (1, None)


@pytest.mark.requires('pickling')
def test_a_pickled_cache_opens_the_same_directory(cache: Any, cache_dir: Path) -> None:
    cache.set('k', 'v')
    again = pickle.loads(pickle.dumps(cache))
    try:
        assert Path(again.directory) == Path(cache.directory)
        assert again.get('k') == 'v'
        assert again.timeout == cache.timeout
    finally:
        again.close()


@pytest.mark.requires('reset')
def test_reset_reads_and_persists_the_limits(make_cache: Any, cache_dir: Path) -> None:
    cache = make_cache(cache_dir)
    assert cache.reset('cull_limit', 5) == 5
    assert cache.reset('cull_limit') == 5
    assert cache.cull_limit == 5
    assert cache.reset('size_limit', 2**21) == 2**21
    assert cache.size_limit == 2**21
    cache.close()
    reopened = make_cache(cache_dir)
    assert (reopened.cull_limit, reopened.size_limit) == (5, 2**21), 'persisted'


@pytest.mark.requires('statistics')
def test_stats_count_hits_and_misses_of_get(cache: Any) -> None:
    assert cache.stats(enable=True) == (0, 0)
    cache.set('k', 1)
    cache.get('k')
    cache.get('k')
    cache.get('missing')
    assert cache.stats() == (2, 1)
    assert cache.stats(reset=True) == (2, 1), 'the counts as they were'
    assert cache.stats() == (0, 0)
    cache.stats(enable=False)
    cache.get('k')
    assert cache.stats(enable=False) == (0, 0), 'not counted while off'


@pytest.mark.requires('check')
def test_check_finds_nothing_in_a_healthy_cache(cache: Any, backend: Backend) -> None:
    cache.set('small', 1)
    cache.set('big', b'x' * (2 * backend.spill_threshold))
    assert cache.check() == []


@pytest.mark.requires('check')
def test_check_reports_a_missing_value_file_and_fix_removes_its_row(
    cache: Any, cache_dir: Path, backend: Backend
) -> None:
    cache.set('big', b'x' * (2 * backend.spill_threshold))
    [path] = backend.spill_files(cache_dir)
    path.unlink()
    found = cache.check()
    assert any('file not found' in str(w.message) for w in found), [str(w.message) for w in found]
    cache.check(fix=True)
    assert len(cache) == 0
    # The emptied value folder may still be reported by diskcache (its fix removes one level per call).
    assert not any('file not found' in str(w.message) for w in cache.check())


@pytest.mark.requires('check')
def test_check_reports_an_old_unknown_file_and_fix_removes_it(cache: Any, cache_dir: Path, backend: Backend) -> None:
    cache.set('big', b'x' * (2 * backend.spill_threshold))
    [real] = backend.spill_files(cache_dir)
    stray = real.with_name('stray-' + real.name)
    stray.write_bytes(b'not a value')
    old = time.time() - 2 * HOUR  # oxide leaves files younger than an hour alone: a writer may own them
    os.utime(stray, (old, old))
    found = cache.check()
    assert any('unknown file' in str(w.message) for w in found), [str(w.message) for w in found]
    cache.check(fix=True)
    assert not stray.exists()
    assert cache.get('big') == b'x' * (2 * backend.spill_threshold)


@pytest.mark.requires('statistics')
@pytest.mark.diverges(backend='oxide', ref='D-049', reason='counted per process', raises=AssertionError)
def test_stats_are_shared_with_other_processes(cache: Any, cache_dir: Path, backend: Backend) -> None:
    import multiprocessing

    from tests.conformance import proc_workers

    cache.stats(enable=True, reset=True)
    cache.set('k', 1)
    cache.get('k')
    spawn = multiprocessing.get_context('spawn')
    results = spawn.Queue()
    child = spawn.Process(target=proc_workers.stats_reader, args=(backend.name, str(cache_dir), results))
    child.start()
    seen = results.get(timeout=60)
    child.join(60)
    assert seen == (1, 0), 'another process sees the counts'
