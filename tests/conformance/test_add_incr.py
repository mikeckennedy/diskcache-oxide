"""add, incr and decr, including the fixed-window counter pattern and its expiry edge cases."""

from typing import Any

import pytest

from tests.conformance.conftest import FakeClock

# ---------------------------------------------------------------------------
# add
# ---------------------------------------------------------------------------


def test_add_missing_returns_true_and_stores(cache: Any) -> None:
    assert cache.add('k', 1) is True
    assert cache.get('k') == 1


def test_add_present_returns_false_and_keeps_value(cache: Any) -> None:
    cache.set('k', 1)
    assert cache.add('k', 2) is False
    assert cache.get('k') == 1


def test_add_with_expire(cache: Any, fake_clock: FakeClock) -> None:
    assert cache.add('k', 1, expire=5) is True
    _, expire_time = cache.get('k', expire_time=True)
    assert expire_time == pytest.approx(fake_clock.now + 5)
    fake_clock.advance(5)
    assert cache.get('k') is None


def test_add_expire_positional(cache: Any, fake_clock: FakeClock) -> None:
    assert cache.add('k', 1, 5) is True
    fake_clock.advance(5)
    assert cache.get('k') is None


def test_add_over_expired_key_returns_true_and_stores(cache: Any, fake_clock: FakeClock) -> None:
    cache.set('k', 1, expire=1)
    fake_clock.advance(2)
    assert cache.add('k', 2) is True
    assert cache.get('k', expire_time=True) == (2, None)


# ---------------------------------------------------------------------------
# incr / decr basics
# ---------------------------------------------------------------------------


def test_incr_missing_starts_from_zero(cache: Any) -> None:
    assert cache.incr('k') == 1
    assert cache.get('k') == 1


def test_incr_missing_starts_from_given_default(cache: Any) -> None:
    assert cache.incr('a', default=10) == 11
    assert cache.incr('b', 2, default=10) == 12


def test_incr_present(cache: Any) -> None:
    cache.set('k', 5)
    assert cache.incr('k') == 6
    assert cache.get('k') == 6


def test_incr_with_delta_and_negative_delta(cache: Any) -> None:
    assert cache.incr('k', 5) == 5
    assert cache.incr('k', -10) == -5
    assert cache.get('k') == -5


def test_decr(cache: Any) -> None:
    cache.set('k', 5)
    assert cache.decr('k') == 4
    assert cache.decr('k', 3) == 1
    assert cache.decr('k', -3) == 4


def test_decr_missing_starts_from_zero(cache: Any) -> None:
    assert cache.decr('k') == -1


def test_incr_default_none_on_present_key_works(cache: Any) -> None:
    cache.set('k', 5)
    assert cache.incr('k', default=None) == 6
    assert cache.decr('k', default=None) == 5


def test_incr_default_none_on_missing_key_raises_and_creates_nothing(cache: Any) -> None:
    cache.set('other', 1)
    with pytest.raises(KeyError):
        cache.incr('k', default=None)
    assert 'k' not in cache
    assert len(cache) == 1


def test_decr_default_none_on_missing_key_raises(cache: Any) -> None:
    with pytest.raises(KeyError):
        cache.decr('k', default=None)
    assert len(cache) == 0


def test_incr_default_none_on_expired_key_raises(cache: Any, fake_clock: FakeClock) -> None:
    cache.set('k', 5, expire=1)
    fake_clock.advance(2)
    with pytest.raises(KeyError):
        cache.incr('k', default=None)
    assert cache.get('k') is None


# ---------------------------------------------------------------------------
# incr and expiry
# ---------------------------------------------------------------------------


def test_incr_keeps_existing_expire_time(cache: Any, fake_clock: FakeClock) -> None:
    # The fixed-window pattern: add(k, 0, expire=w) then incr(k).
    assert cache.add('k', 0, expire=10) is True
    _, expire_time = cache.get('k', expire_time=True)
    fake_clock.advance(3)
    assert cache.incr('k') == 1
    assert cache.get('k', expire_time=True) == (1, expire_time)
    fake_clock.advance(7)
    assert cache.get('k') is None


def test_incr_on_expired_key_creates_counter_with_no_expiry(cache: Any, fake_clock: FakeClock) -> None:
    cache.add('k', 41, expire=10)
    fake_clock.advance(11)
    # The old value is ignored and, surprisingly, the old expiry too: the new counter never expires.
    assert cache.incr('k') == 1
    assert cache.get('k', expire_time=True) == (1, None)
    fake_clock.advance(10**6)
    assert cache.get('k') == 1


def test_incr_on_missing_key_creates_counter_with_no_expiry(cache: Any, fake_clock: FakeClock) -> None:
    assert cache.incr('k') == 1
    assert cache.get('k', expire_time=True) == (1, None)
    fake_clock.advance(10**6)
    assert cache.get('k') == 1


# ---------------------------------------------------------------------------
# incr value types
# ---------------------------------------------------------------------------


def test_incr_on_float(cache: Any) -> None:
    cache.set('k', 2.5)
    assert cache.incr('k') == 3.5
    assert type(cache.get('k')) is float


def test_incr_float_delta_on_missing_key(cache: Any) -> None:
    assert cache.incr('k', 0.5) == 0.5


def test_incr_on_true_raises_type_error(cache: Any) -> None:
    cache.set('k', True)
    # diskcache doesn't treat True as 1; incr fails (with "can't concat int to bytes").
    with pytest.raises(TypeError):
        cache.incr('k')
    assert cache.get('k') is True


def test_incr_on_str_raises_type_error(cache: Any) -> None:
    cache.set('k', 'x')
    with pytest.raises(TypeError):
        cache.incr('k')
    assert cache.get('k') == 'x'


def test_incr_past_int64_raises_overflow_error(cache: Any) -> None:
    # The counter is a SQLite INTEGER, so it can't pass 2**63 - 1. The failed incr changes nothing.
    cache.set('k', 2**63 - 1)
    with pytest.raises(OverflowError):
        cache.incr('k')
    assert cache.get('k') == 2**63 - 1
