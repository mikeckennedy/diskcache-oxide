"""The queue calls `push`, `pull`, `peek` and `peekitem` (plan 24, release 4), against diskcache 5.6.3.

Arguments after `prefix`/`side`/`last` are passed by keyword, as oxide requires (D-047, D-052).
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

import pytest

from tests.conformance.value_cases import HomepageSnapshot

pytestmark = pytest.mark.requires('peek_pull_push')

START = 500_000_000_000_000


def test_push_numbers_from_the_middle_and_pull_takes_the_front(cache: Any) -> None:
    assert [cache.push(v) for v in 'abc'] == [START, START + 1, START + 2]
    assert cache.push('z', side='front') == START - 1
    assert cache.pull() == (START - 1, 'z')
    assert cache.pull() == (START, 'a')
    assert cache.pull(side='back') == (START + 2, 'c')
    assert cache.pull() == (START + 1, 'b')
    assert cache.pull() == (None, None)
    assert len(cache) == 0


def test_a_prefix_makes_string_keys(cache: Any) -> None:
    assert cache.push(1, prefix='jobs') == 'jobs-500000000000000'
    assert cache.push(2, prefix='jobs') == 'jobs-500000000000001'
    assert cache.push(3, prefix='other') == 'other-500000000000000'
    assert cache['jobs-500000000000001'] == 2
    assert cache.pull(prefix='jobs', side='back') == ('jobs-500000000000001', 2)
    assert cache.pull() == (None, None), 'the int queue is another queue'


def test_peek_reads_without_removing(cache: Any) -> None:
    assert cache.peek() == (None, None)
    cache.push('a')
    cache.push('b')
    assert cache.peek() == (START, 'a')
    assert cache.peek(side='back') == (START + 1, 'b')
    assert len(cache) == 2


def test_peekitem_is_the_newest_or_oldest_entry_of_any_kind(cache: Any) -> None:
    with pytest.raises(KeyError):
        cache.peekitem()
    cache.set('first', 1)
    cache.push('q')
    cache.set('last', 3)
    assert cache.peekitem() == ('last', 3)
    assert cache.peekitem(last=False) == ('first', 1)
    assert len(cache) == 3


def test_expire_time_and_tag_shapes(cache: Any, fake_clock: Any) -> None:
    key = cache.push('v', expire=60, tag='t')
    expires = fake_clock.now + 60
    assert cache.peek(expire_time=True) == ((key, 'v'), pytest.approx(expires))
    assert cache.peek(tag=True) == ((key, 'v'), 't')
    assert cache.peekitem(expire_time=True, tag=True) == ((key, 'v'), pytest.approx(expires), 't')
    assert cache.pull(expire_time=True, tag=True) == ((key, 'v'), pytest.approx(expires), 't')
    assert cache.pull(default='d', expire_time=True, tag=True) == ('d', None, None)
    assert cache.peek(default='d', tag=True) == ('d', None)


def test_pull_skips_expired_items(cache: Any, fake_clock: Any) -> None:
    cache.push('old', expire=1)
    cache.push('new')
    fake_clock.advance(10)
    assert cache.pull() == (START + 1, 'new')
    assert cache.pull() == (None, None)


def test_a_large_value_goes_through_a_queue(cache: Any) -> None:
    big = b'x' * 200_000
    cache.push(big)
    assert cache.peek() == (START, big)
    assert cache.pull() == (START, big)
    assert cache.pull() == (None, None)


def test_push_and_pull_inside_a_transaction(cache: Any) -> None:
    with cache.transact():
        cache.push('a')
        assert cache.pull() == (START, 'a')
    assert len(cache) == 0


# -- what oxide does differently ------------------------------------------------------------------


@pytest.mark.diverges(backend='oxide', ref='D-054', reason='the int range holds ints only', raises=AssertionError)
def test_a_float_key_in_the_int_range_is_taken_as_its_end(cache: Any) -> None:
    cache[5.5] = 'float'
    assert cache.push('v') == 6.5


@pytest.mark.diverges(
    backend='oxide', ref='D-054', reason='only keys of the exact shape belong to a queue', raises=AssertionError
)
def test_an_ordinary_key_sharing_the_prefix_sets_the_next_number(cache: Any) -> None:
    cache['user-9'] = 'profile'
    assert cache.push('v', prefix='user') == 'user-000000000000010'


@pytest.mark.diverges(backend='oxide', ref='D-054', reason='nested prefixes never cross', raises=AssertionError)
def test_a_pull_takes_an_item_of_a_nested_prefix(cache: Any) -> None:
    cache.push('inner', prefix='q-1')
    assert cache.pull(prefix='q') == ('q-1-500000000000000', 'inner')


@pytest.mark.diverges(
    backend='oxide', ref='D-055', reason='peek never writes: an expired head is left', raises=AssertionError
)
def test_peek_deletes_the_expired_heads_it_passes(cache: Any, cache_dir: Path, backend: Any, fake_clock: Any) -> None:
    cache.push('old', expire=1)
    cache.push('new')
    fake_clock.advance(10)
    assert cache.peek() == (START + 1, 'new')
    # Counted on disk: oxide's len() would hide the expired row either way (D-060).
    assert backend.stored_rows(cache_dir) == 1


@pytest.mark.diverges(backend='oxide', ref='D-057', reason='push raises past its range', raises=OverflowError)
def test_push_past_the_front_of_the_range_stores_key_zero(cache: Any) -> None:
    cache[1] = 'user'
    assert cache.push('v', side='front') == 0
    assert cache[0] == 'v'


@pytest.mark.diverges(backend='oxide', ref='D-057', reason='a bad side is a ValueError', raises=ValueError)
def test_a_bad_side_raises_key_error(cache: Any) -> None:
    with pytest.raises(KeyError):
        cache.push('v', side='middle')


def test_pull_and_peek_on_another_cache_dir_see_nothing(make_cache: Any, tmp_path: Path) -> None:
    one = make_cache(tmp_path / 'one')
    two = make_cache(tmp_path / 'two')
    one.push('a')
    assert two.peek() == (None, None)
    assert two.pull() == (None, None)


@pytest.mark.covers('D-056')
@pytest.mark.backend_only('oxide')
def test_a_pickled_head_stays_for_a_handle_that_can_read_it(make_cache: Any, cache_dir: Path, backend: Any) -> None:
    writer = make_cache(cache_dir, pickle=True)
    snapshot = HomepageSnapshot('Talk Python', [500, 501], dt.datetime(2026, 10, 27))
    writer.push(snapshot)
    writer.push('after')
    reader = make_cache(cache_dir, pickle=False)
    with pytest.raises(backend.unsafe_value_error):
        reader.pull()
    assert len(reader) == 2, 'the refused head was kept, and the queue behind it waits'
    key, value = writer.pull()
    assert key == START and value == snapshot
    assert reader.pull() == (START + 1, 'after')
