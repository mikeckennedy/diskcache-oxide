"""clear / expire / volume / len / iteration / keys / large values."""

import random
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from tests.conformance.backends import Backend
from tests.conformance.conftest import FakeClock

UNUSUAL_KEYS = ['', 'é', 'ß', 'Z', 'a', 'aa', '10', '2', '中文', '\U0001f600', 'x' * 10_000, 'naïve', 'Ångström']


def payload(size: int, seed: int) -> bytes:
    return random.Random(seed).randbytes(size)


def test_clear_returns_count_including_expired(cache: Any, fake_clock: FakeClock) -> None:
    for i in range(5):
        fake_clock.advance(1)
        cache.set(f'k{i}', i, expire=10 if i < 2 else None)
    fake_clock.advance(100)

    assert cache.clear() == 5
    assert len(cache) == 0
    assert cache.clear() == 0


def test_expire_removes_only_expired(cache: Any, fake_clock: FakeClock) -> None:
    # Distinct expire times on purpose: with many identical expire times, diskcache's expire()
    # removes only 100 rows per call (observed; not pinned here).
    for i in range(6):
        fake_clock.advance(1)
        cache.set(f'k{i}', i, expire=10 if i % 2 else None)
    fake_clock.advance(100)

    assert cache.expire() == 3
    assert sorted(cache) == ['k0', 'k2', 'k4']
    assert cache.expire() == 0


def test_len_counts_entries(cache: Any, fake_clock: FakeClock) -> None:
    assert len(cache) == 0
    for i in range(4):
        fake_clock.advance(1)
        cache.set(f'k{i}', i, expire=5 if i == 0 else None)
    cache.set('k1', 'overwrite')
    cache.delete('k2')
    fake_clock.advance(10)

    # The expired k0 still counts until something removes it.
    assert len(cache) == 3
    cache.expire()
    assert len(cache) == 2


def test_volume_grows_and_shrinks_with_large_values(cache: Any, cache_dir: Path, backend: Backend) -> None:
    size = max(450_000, 2 * backend.spill_threshold)
    base = cache.volume()
    cache.set('big', payload(size, 1))
    grown = cache.volume()
    assert grown - base >= size
    assert backend.spill_files(cache_dir), 'a value over twice the spill threshold should live in its own file'

    cache.delete('big')
    assert cache.volume() <= grown - size
    assert not backend.spill_files(cache_dir)


def test_deleted_spill_file_reads_as_a_miss(cache: Any, cache_dir: Path, backend: Backend) -> None:
    cache.set('small', 'v')
    cache.set('big', payload(2 * backend.spill_threshold, 2))
    files = backend.spill_files(cache_dir)
    assert len(files) == 1
    files.pop().unlink()

    # Not an error: get returns the default and [] raises KeyError, as for any missing key.
    assert cache.get('big') is None
    assert cache.get('big', 'default') == 'default'
    with pytest.raises(KeyError):
        cache['big']
    # Surprising but observed: the row itself survives the failed read (reads never write), so
    # `in` is still True and len() still counts it.
    assert 'big' in cache
    assert len(cache) == 2
    # Other entries are untouched, and the key can be stored again.
    assert cache.get('small') == 'v'
    assert cache.set('big', b'again') is True
    assert cache.get('big') == b'again'


def test_iterkeys_is_sorted(cache: Any) -> None:
    for k in ['b', 'a', '2', '10', 'aa']:
        cache.set(k, 1)

    assert list(cache.iterkeys()) == ['10', '2', 'a', 'aa', 'b']
    assert list(cache.iterkeys(reverse=True)) == ['b', 'aa', 'a', '2', '10']


def test_iter_is_insertion_order(cache: Any) -> None:
    keys = ['b', 'a', '2', '10', 'aa']
    for k in keys:
        cache.set(k, 1)
    cache.set('b', 2)  # an overwrite keeps its original position

    assert list(cache) == keys


def test_unusual_keys_round_trip_and_sort_like_python(cache: Any) -> None:
    for i, k in enumerate(UNUSUAL_KEYS):
        cache.set(k, i)

    for i, k in enumerate(UNUSUAL_KEYS):
        assert cache.get(k) == i
        assert k in cache
    assert list(cache.iterkeys()) == sorted(UNUSUAL_KEYS)
    assert list(cache.iterkeys(reverse=True)) == sorted(UNUSUAL_KEYS, reverse=True)
    assert list(cache) == UNUSUAL_KEYS


DELETE_KEYS = 2000


@pytest.mark.parametrize('iterate', [lambda c: c.iterkeys(), lambda c: iter(c)], ids=['iterkeys', 'iter'])
def test_delete_matching_keys_while_iterating(cache: Any, iterate: Callable[[Any], Any]) -> None:
    # The sweep pattern: walk the keys and delete the ones with a prefix as they go by.
    prefixes = ['page:', 'rate:', 'user:']
    keys = [f'{prefixes[i % 3]}{i:05d}' for i in range(DELETE_KEYS)]
    for k in keys:
        cache.set(k, 1)

    seen = []
    for k in iterate(cache):
        seen.append(k)
        if k.startswith('rate:'):
            assert cache.delete(k) is True

    # Observed on diskcache: no error, and no key skipped or repeated.
    assert sorted(seen) == sorted(keys)
    assert sorted(cache) == sorted(k for k in keys if not k.startswith('rate:'))
    assert not any(cache.get(k) is not None for k in keys if k.startswith('rate:'))


def test_prefix_scan(cache: Any) -> None:
    keys = ['page:/a', 'page:/b', 'pagex', 'page', 'rate:1', 'rate:2', 'a', 'z']
    for k in keys:
        cache.set(k, 1)

    assert [k for k in cache.iterkeys() if k.startswith('page:')] == ['page:/a', 'page:/b']
    assert [k for k in cache.iterkeys() if k.startswith('rate:')] == ['rate:1', 'rate:2']
    assert [k for k in cache.iterkeys() if k.startswith('nope')] == []


@pytest.mark.parametrize('size', [450_000, 5_000_000], ids=['450KB', '5MB'])
def test_large_bytes_round_trip(make_cache: Callable[..., Any], cache_dir: Path, size: int) -> None:
    data = payload(size, size)
    cache = make_cache(cache_dir)
    assert cache.set('big', data) is True

    got = cache.get('big')
    assert type(got) is bytes
    assert got == data

    # And from a fresh Cache object on the same directory.
    assert make_cache(cache_dir).get('big') == data


@pytest.mark.diverges(backend='oxide', ref='D-010', reason='oxide expire() removes every expired entry', raises=AssertionError)
def test_expire_stops_after_100_rows_that_share_one_expire_time(cache: Any, fake_clock: FakeClock) -> None:
    # Surprising: with a frozen clock every entry gets the same expire time, and one expire() call
    # removes only the first chunk of 100 of them.
    for i in range(250):
        cache.set(f'k{i}', i, expire=10)
    fake_clock.advance(11)
    assert cache.expire() == 100
    assert len(cache) == 150


@pytest.mark.backend_only('oxide')
def test_oxide_expire_removes_all_rows_that_share_one_expire_time(cache: Any, fake_clock: FakeClock) -> None:
    for i in range(250):
        cache.set(f'k{i}', i, expire=10)
    fake_clock.advance(11)
    assert cache.expire() == 250
    assert len(cache) == 0
