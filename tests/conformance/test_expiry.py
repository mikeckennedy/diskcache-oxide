"""Expiry semantics, driven entirely by the fake clock."""

from typing import Any

import pytest

from tests.conformance.conftest import FakeClock


def test_hit_just_before_expiry_miss_at_exactly_expiry(cache: Any, fake_clock: FakeClock) -> None:
    cache.set('k', 'v', expire=10)
    fake_clock.advance(9.999)
    assert cache.get('k') == 'v'
    fake_clock.advance(0.001)
    assert cache.get('k') is None


@pytest.mark.parametrize('expire', [0, -1, -0.5])
def test_zero_or_negative_expire_is_an_immediate_miss(cache: Any, fake_clock: FakeClock, expire: float) -> None:
    assert cache.set('k', 'v', expire=expire) is True
    assert cache.get('k') is None
    assert 'k' not in cache


def test_expire_passed_positionally(cache: Any, fake_clock: FakeClock) -> None:
    cache.set('k', 'v', 60)
    fake_clock.advance(59)
    assert cache.get('k') == 'v'
    fake_clock.advance(1)
    assert cache.get('k') is None


def test_expire_time_is_store_time_plus_expire(cache: Any, fake_clock: FakeClock) -> None:
    cache.set('k', 'v', expire=5)
    value, expire_time = cache.get('k', expire_time=True)
    assert value == 'v'
    assert expire_time == pytest.approx(fake_clock.now + 5)


def test_expire_none_never_expires(cache: Any, fake_clock: FakeClock) -> None:
    cache.set('k', 'v', expire=None)
    assert cache.get('k', expire_time=True) == ('v', None)
    fake_clock.advance(10**9)
    assert cache.get('k') == 'v'


def test_expired_entry_misses_on_every_read(cache: Any, fake_clock: FakeClock) -> None:
    cache.set('k', 'v', expire=1)
    fake_clock.advance(2)
    assert cache.get('k') is None
    assert cache.get('k', 'd') == 'd'
    assert 'k' not in cache
    with pytest.raises(KeyError):
        cache['k']


def test_expired_entry_still_counted_until_expire(cache: Any, fake_clock: FakeClock) -> None:
    cache.set('k', 'v', expire=1)
    volume_before = cache.volume()
    fake_clock.advance(2)
    assert cache.get('k') is None
    # Reads don't remove expired rows: they stay in len, iteration and volume until expire().
    assert len(cache) == 1
    assert list(cache.iterkeys()) == ['k']
    assert list(cache) == ['k']
    assert cache.volume() == volume_before
    assert cache.expire() == 1
    assert len(cache) == 0
    assert list(cache.iterkeys()) == []


def test_clear_counts_expired_entries(cache: Any, fake_clock: FakeClock) -> None:
    cache.set('a', 1, expire=1)
    cache.set('b', 1, expire=1)
    cache.set('c', 1)
    fake_clock.advance(2)
    assert cache.clear() == 3
    assert len(cache) == 0


def test_pop_and_delete_of_expired_entry_act_missing(cache: Any, fake_clock: FakeClock) -> None:
    cache.set('k', 'v', expire=1)
    fake_clock.advance(2)
    assert cache.pop('k', 'd') == 'd'
    cache.set('j', 'v', expire=1)
    fake_clock.advance(2)
    assert cache.delete('j') is False


def test_touch_extends_expiry(cache: Any, fake_clock: FakeClock) -> None:
    cache.set('k', 'v', expire=10)
    assert cache.touch('k', 100) is True
    fake_clock.advance(50)
    assert cache.get('k') == 'v'
    fake_clock.advance(51)
    assert cache.get('k') is None


def test_touch_can_shorten_expiry(cache: Any, fake_clock: FakeClock) -> None:
    cache.set('k', 'v', expire=100)
    assert cache.touch('k', 5) is True
    fake_clock.advance(6)
    assert cache.get('k') is None


def test_touch_none_removes_expiry(cache: Any, fake_clock: FakeClock) -> None:
    cache.set('k', 'v', expire=10)
    assert cache.touch('k', None) is True
    assert cache.get('k', expire_time=True) == ('v', None)
    fake_clock.advance(10**6)
    assert cache.get('k') == 'v'


def test_touch_expired_entry_returns_false_and_does_not_revive(cache: Any, fake_clock: FakeClock) -> None:
    cache.set('k', 'v', expire=10)
    fake_clock.advance(11)
    assert cache.touch('k', 100) is False
    assert cache.get('k') is None


def test_set_over_expired_entry_revives_it(cache: Any, fake_clock: FakeClock) -> None:
    cache.set('k', 'old', expire=1)
    fake_clock.advance(2)
    assert cache.set('k', 'new') is True
    assert cache.get('k', expire_time=True) == ('new', None)
    assert len(cache) == 1
