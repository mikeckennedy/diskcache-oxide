"""Value fidelity: what goes in comes back with exactly the same types. Driven by value_cases.py."""

from __future__ import annotations

import collections
import datetime as dt
import math
import pickle
import zoneinfo
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from tests.conformance.value_cases import CASES, DISKCACHE_QUIRKS, Color, HomepageSnapshot, Point, Priority, exact_equal

_LA = dt.datetime(2026, 10, 27, 13, 45, tzinfo=zoneinfo.ZoneInfo('America/Los_Angeles'))


def _roundtrip_params() -> list[Any]:
    params = []
    for case_id, value, oxide_default, _ in CASES:
        marks = []
        if oxide_default != 'roundtrip':
            # TBD cases are marked too: if Phase 1 makes one round-trip, strict xfail flags the table for an update.
            # A TypeError case must fail with exactly that; a TBD case may fail any way until Phase 1 decides.
            raises = TypeError if oxide_default == 'TypeError' else None
            marks.append(pytest.mark.diverges(backend='oxide', ref='D-001', reason=f'{case_id}: {oxide_default} without pickle', raises=raises))
        if case_id in DISKCACHE_QUIRKS:
            ref, description = DISKCACHE_QUIRKS[case_id]
            marks.append(pytest.mark.diverges(backend='diskcache', ref=ref, reason=description, raises=AssertionError))
        params.append(pytest.param(value, id=case_id, marks=marks))
    return params


def _oxide_params(column: int, outcome: str) -> list[Any]:
    params = []
    for case in CASES:
        if case[column] == outcome:
            params.append(pytest.param(case[1], id=case[0]))
        elif case[column] == 'TBD':
            params.append(pytest.param(case[1], id=case[0], marks=pytest.mark.skip(reason='outcome is TBD until Phase 1')))
    return params


# ---------------------------------------------------------------------------
# The exact-equality helper itself
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ('a', 'b'),
    [
        (True, 1),
        (1, 1.0),
        (0.0, -0.0),
        ((1, 2), [1, 2]),
        ({1: 'a'}, {'1': 'a'}),
        ({'a': 1, 'b': 2}, {'b': 2, 'a': 1}),
        ([{'x': (1,)}], [{'x': [1]}]),
        (b'a', 'a'),
        # What a silent coercion of the subclass cases would come back as.
        (Color.RED, 'red'),
        (Priority.HIGH, 1),
        (Point(1, 2), (1, 2)),
        (collections.OrderedDict(a=1), {'a': 1}),
        (collections.defaultdict(list, a=1), {'a': 1}),
        (collections.defaultdict(list, a=1), collections.defaultdict(set, a=1)),
        (_LA, _LA.astimezone(dt.timezone(_LA.utcoffset()))),
    ],
)
def test_exact_equal_rejects_lookalikes(a: Any, b: Any) -> None:
    assert not exact_equal(a, b)


def test_exact_equal_accepts_nan_and_matching_negative_zero() -> None:
    assert exact_equal(math.nan, float('nan'))
    assert exact_equal({'v': [-0.0]}, {'v': [-0.0]})


def test_every_case_pickles() -> None:
    # The cases must be picklable for diskcache and for oxide's pickle=True path.
    for case_id, value, _, _ in CASES:
        assert exact_equal(pickle.loads(pickle.dumps(value)), value), case_id


# ---------------------------------------------------------------------------
# Round-trips (diskcache pickles everything, with the exceptions in DISKCACHE_QUIRKS)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize('value', _roundtrip_params())
def test_value_roundtrips(cache: Any, value: Any) -> None:
    assert cache.set('k', value) is True
    assert exact_equal(cache.get('k'), value)


@pytest.mark.parametrize('value', _roundtrip_params())
def test_value_roundtrips_across_reopen(make_cache: Callable[..., Any], cache_dir: Path, value: Any) -> None:
    with make_cache(cache_dir) as writer:
        writer.set('k', value)
    reader = make_cache(cache_dir)
    assert exact_equal(reader.get('k'), value)


@pytest.mark.diverges(backend='oxide', ref='D-008', reason='oxide round-trips a top-level NaN', raises=AssertionError)
def test_diskcache_top_level_nan_reads_back_as_none(cache: Any) -> None:
    # Surprising: the NaN is stored (set is True, the key exists) but reads back as None, ignoring the default.
    assert cache.set('k', math.nan) is True
    assert 'k' in cache
    assert len(cache) == 1
    assert cache.get('k') is None
    assert cache.get('k', 'default') is None
    # Nested NaN is pickled and survives.
    cache.set('nested', [math.nan])
    assert exact_equal(cache.get('nested'), [math.nan])


# ---------------------------------------------------------------------------
# Oxide's side of D-001
# ---------------------------------------------------------------------------


@pytest.mark.backend_only('oxide')
@pytest.mark.parametrize('value', _oxide_params(2, 'TypeError'))
def test_oxide_default_rejects_unrepresentable_value(cache: Any, value: Any) -> None:
    with pytest.raises(TypeError):
        cache.set('k', value)
    # A rejected set stores nothing.
    assert cache.get('k') is None
    assert 'k' not in cache


@pytest.mark.backend_only('oxide')
@pytest.mark.parametrize('value', _oxide_params(2, 'TypeError'))
def test_oxide_rejected_set_keeps_the_previous_value(cache: Any, value: Any) -> None:
    cache.set('k', 'previous')
    with pytest.raises(TypeError):
        cache.set('k', value)
    assert cache.get('k') == 'previous'


@pytest.mark.backend_only('oxide')
@pytest.mark.parametrize('value', _oxide_params(3, 'roundtrip'))
def test_oxide_pickle_true_roundtrips(make_cache: Callable[..., Any], cache_dir: Path, value: Any) -> None:
    cache = make_cache(cache_dir, pickle=True)
    assert cache.set('k', value) is True
    assert exact_equal(cache.get('k'), value)


@pytest.mark.backend_only('oxide')
def test_oxide_pickle_false_handle_refuses_to_unpickle(make_cache: Callable[..., Any], cache_dir: Path, backend: Any) -> None:
    # A pickle=True peer may store pickled rows; a pickle=False handle must never run pickle on them,
    # and must leave the row alone rather than destroying the peer's data.
    writer = make_cache(cache_dir, pickle=True)
    snapshot = HomepageSnapshot('Talk Python', [500, 501], dt.datetime(2026, 10, 27))
    writer.set('snapshot', snapshot)

    reader = make_cache(cache_dir, pickle=False)
    with pytest.raises(backend.unsafe_value_error):
        reader.get('snapshot')
    assert issubclass(backend.unsafe_value_error, TypeError)

    assert 'snapshot' in reader
    assert exact_equal(writer.get('snapshot'), snapshot)


@pytest.mark.backend_only('oxide')
def test_oxide_pickle_false_pop_refuses_and_keeps_the_row(make_cache: Callable[..., Any], cache_dir: Path, backend: Any) -> None:
    # `pop` deletes inside its transaction and decodes after the commit, so the refusal has to come
    # before the delete: a pickle=False handle's pop raised UnsafeValueError but had already removed
    # the peer's row (found by the architect's plan-08 review).
    writer = make_cache(cache_dir, pickle=True)
    snapshot = HomepageSnapshot('Talk Python', [500, 501], dt.datetime(2026, 10, 27))
    writer.set('snapshot', snapshot)

    reader = make_cache(cache_dir, pickle=False)
    with pytest.raises(backend.unsafe_value_error):
        reader.pop('snapshot')
    with pytest.raises(backend.unsafe_value_error):
        reader.pop('snapshot', 'default')

    assert 'snapshot' in reader
    assert exact_equal(writer.pop('snapshot'), snapshot)
    assert 'snapshot' not in writer
    assert reader.pop('snapshot', 'default') == 'default'
