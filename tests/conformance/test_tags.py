"""Tags, `evict(tag)` and `eviction_policy='none'` (plan 24, release 3), against diskcache 5.6.3.

Tags are passed by keyword here, as oxide requires (diskcache also takes them by position).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests.conformance.backends import Backend


@pytest.mark.requires('tags')
@pytest.mark.parametrize('tag', ['users', b'raw', 7, 2.5], ids=['str', 'bytes', 'int', 'float'])
def test_a_tag_is_stored_and_read_back(cache: Any, tag: Any) -> None:
    assert cache.set('k', 'v', tag=tag) is True
    assert cache.get('k', tag=True) == ('v', tag)
    assert cache.get('k') == 'v'
    value, expire_time, got = cache.get('k', expire_time=True, tag=True)
    assert (value, expire_time, got) == ('v', None, tag)
    assert cache.pop('k', tag=True) == ('v', tag)


@pytest.mark.requires('tags')
def test_a_miss_reads_back_no_tag(cache: Any) -> None:
    assert cache.get('missing', tag=True) == (None, None)
    assert cache.get('missing', default='d', expire_time=True, tag=True) == ('d', None, None)
    assert cache.pop('missing', default='d', tag=True) == ('d', None)


@pytest.mark.requires('tags')
def test_an_untagged_entry_has_no_tag_and_an_overwrite_replaces_it(cache: Any) -> None:
    cache.set('k', 1)
    assert cache.get('k', tag=True) == (1, None)
    cache.set('k', 2, tag='a')
    cache.set('k', 3)
    assert cache.get('k', tag=True) == (3, None), 'an overwrite without a tag clears it'
    assert cache.add('new', 1, tag='b') is True
    assert cache.get('new', tag=True) == (1, 'b')


@pytest.mark.requires('tags')
def test_touch_and_an_in_place_incr_keep_the_tag(cache: Any) -> None:
    cache.set('n', 1, tag='counters')
    assert cache.touch('n', expire=60) is True
    assert cache.incr('n') == 2
    assert cache.get('n', tag=True) == (2, 'counters')
    cache.incr('fresh')
    assert cache.get('fresh', tag=True) == (1, None), 'incr of a missing key stores no tag'


@pytest.mark.requires('tags')
def test_evict_removes_exactly_one_tag(cache: Any, backend: Backend) -> None:
    for i in range(5):
        cache.set(f'a{i}', i, tag='a')
        cache.set(f'b{i}', i, tag='b')
    cache.set('big', b'x' * (2 * backend.spill_threshold), tag='a')
    cache.set('none', 1)
    assert cache.evict('a') == 6
    assert sorted(cache) == [f'b{i}' for i in range(5)] + ['none']
    assert cache.evict('a') == 0
    assert cache.evict('nobody') == 0


@pytest.mark.requires('eviction_policy')
def test_eviction_policy_none_never_evicts_for_size(make_cache: Any, cache_dir: Path) -> None:
    cache = make_cache(cache_dir, size_limit=2**20, eviction_policy='none')
    big = b'x' * 100_000
    for i in range(30):  # About 3 MB into a 1 MiB cache.
        cache.set(f'k{i}', big)
    assert len(cache) == 30
    assert cache.cull() == 0
    assert len(cache) == 30


@pytest.mark.requires('eviction_policy')
def test_eviction_policy_none_still_removes_expired_entries(make_cache: Any, cache_dir: Path, fake_clock: Any) -> None:
    cache = make_cache(cache_dir, size_limit=2**20, eviction_policy='none')
    cache.set('gone', 1, expire=1)
    cache.set('kept', 1)
    fake_clock.advance(10)
    assert cache.expire() == 1
    assert list(cache) == ['kept']


@pytest.mark.requires('eviction_policy')
def test_least_recently_stored_evicts_the_oldest(make_cache: Any, cache_dir: Path) -> None:
    cache = make_cache(cache_dir, size_limit=2**20, eviction_policy='least-recently-stored')
    big = b'x' * 100_000
    for i in range(30):
        cache.set(f'k{i}', big)
    assert len(cache) < 30
    assert 'k29' in cache and 'k0' not in cache


@pytest.mark.requires('tags')
def test_evict_none_touches_nothing(cache: Any) -> None:
    cache.set('untagged', 1)
    cache.set('tagged', 1, tag='t')
    assert cache.evict(None) == 0
    assert len(cache) == 2


@pytest.mark.requires('tags')
def test_an_expired_incr_and_item_assignment_clear_the_tag(cache: Any, fake_clock: Any) -> None:
    cache.set('n', 1, expire=1, tag='t')
    fake_clock.advance(10)
    assert cache.incr('n') == 1
    assert cache.get('n', tag=True) == (1, None)
    cache.set('k', 1, tag='t')
    cache['k'] = 2
    assert cache.get('k', tag=True) == (2, None)


@pytest.mark.requires('tags')
@pytest.mark.diverges(backend='oxide', ref='D-052', reason='tags compare by exact type', raises=AssertionError)
def test_evict_matches_tags_numerically(cache: Any) -> None:
    cache.set('int', 1, tag=1)
    cache.set('float', 1, tag=1.0)
    assert cache.evict(1) == 2


@pytest.mark.requires('eviction_policy')
@pytest.mark.diverges(
    backend='oxide', ref='D-053', reason='cull() counts the expired entries it removed', raises=AssertionError
)
def test_cull_under_none_reports_zero_after_removing_expired_entries(
    make_cache: Any, cache_dir: Path, fake_clock: Any
) -> None:
    cache = make_cache(cache_dir, size_limit=2**20, eviction_policy='none')
    cache.set('gone', 1, expire=1)
    fake_clock.advance(10)
    assert cache.cull() == 0
    assert len(cache) == 0


@pytest.mark.requires('eviction_policy')
def test_the_eviction_policy_persists(make_cache: Any, cache_dir: Path) -> None:
    make_cache(cache_dir, eviction_policy='none').close()
    reopened = make_cache(cache_dir)
    assert reopened.eviction_policy == 'none'
