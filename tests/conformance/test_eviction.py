"""Size-limit culling and the least-recently-stored eviction policy.

Sizes are relative to `empty_volume`, never absolute (D-006). Every test runs at two value sizes,
one on each side of oxide's 64 KiB spill threshold: an inline value is counted by the database's
pages and a spilled one by its file's length, and eviction has to hold for both. (Both sizes spill
on diskcache, whose threshold is 32 KiB.) Each value adds roughly its own length to `volume()`, but
the assertions are properties (survivors, order, bounds), never exact byte counts.

The cache room is ROOM bytes above the empty volume. That is just over 1 MiB on purpose: oxide's
MIN_SIZE_LIMIT is 1 MiB (D-005), so a smaller limit would be refused there instead of exercised.

The clock is advanced before every `set` so store times are distinct and eviction order is defined.
"""

import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from tests.conformance.backends import Backend
from tests.conformance.conftest import FakeClock

MIN_SIZE_LIMIT = 1_048_576
VALUE_SIZE = 60_000
SLOTS = 20
ROOM = SLOTS * VALUE_SIZE  # 1.2 or 1.4 MB, above oxide's 1 MiB minimum
DEFAULT_CULL_LIMIT = 10


def key(i: int) -> str:
    return f'k{i:03d}'


@pytest.fixture(autouse=True, params=[60_000, 70_000], ids=['60k', '70k'])
def _value_size(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Runs each test under and over oxide's 65,536-byte spill threshold."""
    module = sys.modules[__name__]
    monkeypatch.setattr(module, 'VALUE_SIZE', request.param)
    monkeypatch.setattr(module, 'ROOM', SLOTS * request.param)


def value(i: int, size: int | None = None) -> bytes:
    return bytes([i % 256]) * (VALUE_SIZE if size is None else size)


def fill(cache: Any, clock: FakeClock, start: int, stop: int, **kwargs: Any) -> None:
    for i in range(start, stop):
        clock.advance(1)
        cache.set(key(i), value(i), **kwargs)


def fill_until_gone(cache: Any, clock: FakeClock, start: int, victim: str, *, max_sets: int = 3 * SLOTS) -> int:
    """Store new keys from `start` until `victim` is evicted. Returns the next unused index."""
    i = start
    while victim in cache:
        assert i - start < max_sets, f'{victim} was never evicted'
        clock.advance(1)
        cache.set(key(i), value(i))
        i += 1
    return i


@pytest.fixture
def limited(
    make_cache: Callable[..., Any],
    cache_dir: Path,
    empty_volume: int,
    fake_clock: FakeClock,
) -> Any:
    return make_cache(cache_dir, size_limit=empty_volume + ROOM)


def test_oldest_stored_go_first(limited: Any, fake_clock: FakeClock) -> None:
    fill(limited, fake_clock, 0, 1)
    stop = fill_until_gone(limited, fake_clock, 1, key(0))
    stored = [key(i) for i in range(stop)]
    present = [k for k in stored if k in limited]

    assert present, 'culling removed everything'
    # The survivors are exactly the most recently stored keys: a contiguous suffix of store order.
    assert present == stored[len(stored) - len(present) :]
    assert key(stop - 1) in limited


def test_get_does_not_refresh(limited: Any, fake_clock: FakeClock) -> None:
    fake_clock.advance(1)
    limited.set(key(0), value(0))
    i = 1
    while len(limited) == i:
        assert i < 3 * SLOTS, 'nothing was ever evicted'
        # Reading the oldest key after every set must not save it (least-recently-STORED, not -used).
        assert limited.get(key(0)) == value(0)
        fake_clock.advance(1)
        limited.set(key(i), value(i))
        i += 1

    assert key(0) not in limited
    assert key(i - 1) in limited


def test_overwrite_moves_key_to_back_of_eviction_order(limited: Any, fake_clock: FakeClock) -> None:
    fill(limited, fake_clock, 0, 15)
    assert len(limited) == 15
    fake_clock.advance(1)
    limited.set(key(0), b'new')

    fill_until_gone(limited, fake_clock, 15, key(1))

    # k000 was re-stored after k001..k014, so it outlives them.
    assert limited.get(key(0)) == b'new'
    # ...but __iter__ is insertion order, and an overwrite keeps the original position.
    assert next(iter(limited)) == key(0)


def test_volume_stays_within_one_cull_of_the_limit(limited: Any, fake_clock: FakeClock) -> None:
    # Culling happens per set and removes at most cull_limit entries, so the bound is loose:
    # the limit plus cull_limit times the largest value.
    slack = DEFAULT_CULL_LIMIT * VALUE_SIZE
    for i in range(3 * SLOTS):
        fake_clock.advance(1)
        assert limited.set(key(i), value(i)) is True
        assert limited.volume() <= limited.size_limit + slack
    assert len(limited) < 3 * SLOTS


@pytest.mark.requires('cull_limit')
def test_one_set_culls_at_most_cull_limit_entries(limited: Any, fake_clock: FakeClock) -> None:
    fill(limited, fake_clock, 0, SLOTS - 2)
    before = len(limited)
    assert before == SLOTS - 2

    # One value worth 15 slots overshoots by far more than cull_limit small entries can free.
    fake_clock.advance(1)
    assert limited.set('big', value(255, 15 * VALUE_SIZE)) is True

    assert len(limited) >= before + 1 - DEFAULT_CULL_LIMIT
    assert limited.volume() > limited.size_limit, 'one set freed more than cull_limit entries'
    assert 'big' in limited


def test_repeated_sets_converge_below_limit(limited: Any, fake_clock: FakeClock) -> None:
    # Whether the big set alone gets back under the limit depends on cull_limit (pinned by the
    # test above); either way, more sets must converge.
    fill(limited, fake_clock, 0, SLOTS - 2)
    fake_clock.advance(1)
    limited.set('big', value(255, 15 * VALUE_SIZE))

    for i in range(100, 100 + SLOTS):
        fake_clock.advance(1)
        limited.set(key(i), value(i))
        if limited.volume() <= limited.size_limit:
            break
    assert limited.volume() <= limited.size_limit


def _fill_mixed(cache: Any, clock: FakeClock, *, expiring: bool) -> None:
    """18 of the 20 slots: k000-k004 live (the oldest), k005-k009 expiring at +50 s, k010-k017 live."""
    fill(cache, clock, 0, 5)
    fill(cache, clock, 5, 10, expire=50 if expiring else None)
    fill(cache, clock, 10, SLOTS - 2)
    assert len(cache) == SLOTS - 2, 'the fill itself should not evict anything'


def test_expired_entries_are_culled_before_older_live_ones(
    make_cache: Callable[..., Any],
    tmp_path: Path,
    empty_volume: int,
    fake_clock: FakeClock,
) -> None:
    size_limit = empty_volume + ROOM
    big = value(255, 3 * VALUE_SIZE)  # 18 + 3 slots: over the limit by at least a slot

    # Control: with nothing expired, the same set really does push the cache over its limit and
    # evicts the oldest live entry.
    control = make_cache(tmp_path / 'control', size_limit=size_limit)
    _fill_mixed(control, fake_clock, expiring=False)
    fake_clock.advance(100)
    control.set('big', big)
    assert key(0) not in control, 'the control set never put the cache over its limit'

    cache = make_cache(tmp_path / 'expiring', size_limit=size_limit)
    _fill_mixed(cache, fake_clock, expiring=True)
    fake_clock.advance(100)  # k005-k009 are now expired
    assert cache.volume() + len(big) > cache.size_limit
    cache.set('big', big)

    # The expired entries went first, and freed enough that every live entry survives, even the
    # five stored before them.
    assert not any(key(i) in cache for i in range(5, 10))
    assert sorted(cache) == sorted([key(i) for i in range(5)] + [key(i) for i in range(10, SLOTS - 2)] + ['big'])
    assert cache.volume() <= cache.size_limit


@pytest.mark.requires('cull_limit')
def test_set_sweeps_expired_entries_without_size_pressure(
    make_cache: Callable[..., Any], cache_dir: Path, fake_clock: FakeClock
) -> None:
    # Surprising but observed: with no size_limit pressure at all, a set still removes expired
    # entries, at most cull_limit of them per set.
    cache = make_cache(cache_dir)
    for i in range(15):
        fake_clock.advance(1)
        cache.set(key(i), i, expire=1_000)
    fake_clock.advance(2_000)
    assert len(cache) == 15

    fake_clock.advance(1)
    cache.set('new', 1)
    assert len(cache) == 15 + 1 - DEFAULT_CULL_LIMIT

    fake_clock.advance(1)
    cache.set('newer', 1)
    assert sorted(cache) == ['new', 'newer']


def _clear(cache: Any, clock: FakeClock, count: int) -> None:
    assert cache.clear() == count


def _expire(cache: Any, clock: FakeClock, count: int) -> None:
    clock.advance(2_000)
    assert cache.expire() == count


def _sweep(cache: Any, clock: FakeClock, count: int) -> None:
    # No size pressure: each set still removes up to cull_limit expired entries.
    clock.advance(2_000)
    for i in range(count):
        clock.advance(1)
        cache.set(f'small{i}', i)
    assert sorted(cache) == sorted(f'small{i}' for i in range(count))
    for i in range(count):
        assert cache.delete(f'small{i}')


def _delete(cache: Any, clock: FakeClock, count: int) -> None:
    for i in range(count):
        assert cache.delete(key(i))


def _pop(cache: Any, clock: FakeClock, count: int) -> None:
    for i in range(count):
        assert cache.pop(key(i)) == value(i)


def _overwrite_small(cache: Any, clock: FakeClock, count: int) -> None:
    for i in range(count):
        clock.advance(1)
        cache.set(key(i), i)
    assert cache.clear() == count


BULK_REMOVALS = {
    'clear': _clear,
    'expire': _expire,
    'sweep': _sweep,
    'delete': _delete,
    'pop': _pop,
    'overwrite-small': _overwrite_small,
}


@pytest.mark.parametrize('removal', BULK_REMOVALS)
def test_volume_comes_back_when_values_are_removed(
    make_cache: Callable[..., Any], cache_dir: Path, empty_volume: int, fake_clock: FakeClock, removal: str
) -> None:
    # Whatever removes a value has to give its bytes back, or volume() only ever grows and a
    # size-limited cache ends up evicting on every set.
    count = 12
    cache = make_cache(cache_dir)
    fill(cache, fake_clock, 0, count, expire=1_000)
    assert cache.volume() >= empty_volume + count * VALUE_SIZE // 2, 'the values are counted'
    BULK_REMOVALS[removal](cache, fake_clock, count)
    assert len(cache) == 0
    # Emptied database pages stay allocated until they are reused, which is all that may be left.
    assert cache.volume() < empty_volume + count * VALUE_SIZE // 2, f'{removal} gave nothing back'


@pytest.mark.diverges(
    backend='oxide',
    ref='D-005',
    reason='oxide refuses a size_limit under 1 MiB with ValueError',
    raises=ValueError,
)
def test_size_limit_below_empty_volume_wipes_everything(
    make_cache: Callable[..., Any],
    cache_dir: Path,
    empty_volume: int,
    fake_clock: FakeClock,
) -> None:
    cache = make_cache(cache_dir, size_limit=empty_volume // 2)
    fake_clock.advance(1)
    # set reports success, yet the value it just stored is culled along with everything else.
    assert cache.set('a', 'b') is True
    assert len(cache) == 0
    assert cache.get('a') is None


@pytest.mark.backend_only('oxide')
def test_oxide_refuses_size_limit_below_one_mib(backend: Backend, cache_dir: Path, tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        backend.make_cache(cache_dir, size_limit=MIN_SIZE_LIMIT - 1)

    with backend.make_cache(tmp_path / 'ok', size_limit=MIN_SIZE_LIMIT) as cache:
        assert cache.size_limit == MIN_SIZE_LIMIT
        assert cache.set('a', 'b') is True
        assert cache.get('a') == 'b'
