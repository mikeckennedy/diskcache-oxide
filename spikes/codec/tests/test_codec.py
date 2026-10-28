"""Spike 3 correctness: exact round-trips (equal values and identical types) and rejections."""

from __future__ import annotations

import collections
import datetime as dt
import enum
import json
import math
import os
import sys
import zoneinfo
from decimal import Decimal
from pathlib import Path
from typing import Any, NamedTuple

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

import dco_codec_spike as m

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / 'tests' / 'conformance'))
from value_cases import CASES, exact_equal  # noqa: E402

CODECS = {
    'serde_json': (m.json_encode, m.json_decode),
    'sonic': (m.sonic_encode, m.sonic_decode),
    'msgpack': (m.msgpack_encode, m.msgpack_decode),
}

# The outcomes this spike proposes for the value_cases TBDs.
PROPOSED = {'date': 'roundtrip', 'decimal': 'roundtrip', 'set': 'roundtrip', 'datetime-zoneinfo': 'roundtrip'}


def has_negative_zero(v: Any) -> bool:
    if isinstance(v, float):
        return v == 0 and math.copysign(1, v) < 0
    if isinstance(v, dict):
        return any(has_negative_zero(k) or has_negative_zero(x) for k, x in v.items())
    if isinstance(v, (list, tuple, set, frozenset)):
        return any(has_negative_zero(x) for x in v)
    return False


def skip_sonic_negative_zero(codec: str, value: Any) -> None:
    # sonic-rs 0.5.10 decodes "-0.0" as +0.0 (serde_json is correct). Recorded in the spike report.
    if codec == 'sonic' and has_negative_zero(value):
        pytest.xfail('sonic-rs decodes -0.0 as 0.0')


def roundtrip(codec: str, value: Any) -> Any:
    enc, dec = CODECS[codec]
    return dec(enc(value))


@pytest.mark.parametrize('codec', CODECS)
@pytest.mark.parametrize(('case_id', 'value', 'outcome'), [(c[0], c[1], PROPOSED.get(c[0], c[2])) for c in CASES], ids=[c[0] for c in CASES])
def test_value_cases(codec: str, case_id: str, value: Any, outcome: str) -> None:
    if outcome == 'TypeError':
        with pytest.raises(TypeError):
            CODECS[codec][0](value)
    else:
        skip_sonic_negative_zero(codec, value)
        assert exact_equal(roundtrip(codec, value), value)


class MyStr(str):
    pass


class MyInt(int):
    pass


class Flag(enum.IntFlag):
    A = 1


class P(NamedTuple):
    a: int


class CustomTz(dt.tzinfo):
    def utcoffset(self, d: Any) -> dt.timedelta:
        return dt.timedelta(hours=1)

    def dst(self, d: Any) -> dt.timedelta:
        return dt.timedelta(0)

    def tzname(self, d: Any) -> str:
        return 'X'


def _recursive_list() -> list[Any]:
    a: list[Any] = []
    a.append(a)
    return a


REJECTED = {
    'str-subclass': MyStr('x'),
    'int-subclass': MyInt(3),
    'int-flag': Flag.A,
    'namedtuple': P(1),
    'ordered-dict': collections.OrderedDict(a=1),
    'defaultdict': collections.defaultdict(int),
    'counter': collections.Counter('ab'),
    'bytearray': bytearray(b'x'),
    'memoryview': memoryview(b'x'),
    'object': object(),
    'complex': 1j,
    'time': dt.time(12, 0),
    'timedelta': dt.timedelta(1),
    'custom-tzinfo': dt.datetime(2026, 1, 1, tzinfo=CustomTz()),
    'named-timezone': dt.datetime(2026, 1, 1, tzinfo=dt.timezone(dt.timedelta(hours=-7), 'PDT')),
    'nested-subclass': {'a': [1, MyStr('x')]},
    'subclass-key': {MyStr('k'): 1},
    'recursive': _recursive_list(),
    'too-deep': json.loads('[' * 200 + ']' * 200),
}


@pytest.mark.parametrize('codec', CODECS)
@pytest.mark.parametrize('name', REJECTED)
def test_rejected(codec: str, name: str) -> None:
    with pytest.raises(TypeError):
        CODECS[codec][0](REJECTED[name])


@pytest.mark.parametrize('codec', CODECS)
def test_bool_and_negative_zero(codec: str) -> None:
    skip_sonic_negative_zero(codec, -0.0)
    out = roundtrip(codec, [True, False, 1, 0, -0.0, 0.0, 1.0])
    assert [type(x) for x in out] == [bool, bool, int, int, float, float, float]
    assert math.copysign(1, out[4]) == -1 and math.copysign(1, out[5]) == 1


def test_plain_json_is_untagged_and_standard() -> None:
    value = {'a': [1, 2.5, None, True, 'x'], 'b': {'c': 'd'}}
    data, tagged = m.json_encode_info(value)
    assert not tagged
    assert json.loads(data) == value
    assert m.json_encode_info({'a': (1,)})[1]
    assert m.json_encode_info({'$schema': 1})[1]


def test_tag_spellings() -> None:
    assert m.json_encode({'$t': 1, 'x': 2}) == b'{"$esc":{"$t":1,"x":2}}'
    assert m.json_encode((1, b'\x00')) == b'{"$tuple":[1,{"$bytes":"AA=="}]}'
    assert m.json_encode({1: 'a'}) == b'{"$dict":[[1,"a"]]}'
    assert m.json_encode([math.nan, -math.inf, 2**64]) == b'[{"$float":"nan"},{"$float":"-inf"},{"$int":"18446744073709551616"}]'
    la = zoneinfo.ZoneInfo('America/Los_Angeles')
    assert m.json_encode(dt.datetime(2026, 11, 1, 1, 30, fold=1, tzinfo=la)) == b'{"$datetime":"2026-11-01T01:30:00-08:00[America/Los_Angeles][_fold=1]"}'
    assert m.json_encode(dt.datetime(2026, 1, 1, fold=1)) == b'{"$datetime":"2026-01-01T00:00:00[_fold=1]"}'


@pytest.mark.parametrize('codec', CODECS)
def test_zoneinfo_fold_survives(codec: str) -> None:
    la = zoneinfo.ZoneInfo('America/Los_Angeles')
    for fold in (0, 1):
        v = dt.datetime(2026, 11, 1, 1, 30, fold=fold, tzinfo=la)
        out = roundtrip(codec, v)
        assert out.fold == fold and out.utcoffset() == v.utcoffset() and out.tzinfo is la
        for other in (dt.datetime(2026, 1, 1, fold=fold), dt.datetime(2026, 1, 1, fold=fold, tzinfo=dt.UTC), dt.datetime(2026, 7, 1, fold=fold, tzinfo=la)):
            assert roundtrip(codec, other).fold == fold


@pytest.mark.parametrize('codec', ['serde_json', 'sonic'])
def test_unknown_tag_and_bad_tag_shape_fail_loudly(codec: str) -> None:
    dec = CODECS[codec][1]
    with pytest.raises(ValueError):
        dec(b'{"$nope":1}')
    with pytest.raises(ValueError):
        dec(b'{"$tuple":[1],"x":2}')


@pytest.mark.parametrize('codec', CODECS)
def test_crafted_deep_input_fails_cleanly(codec: str) -> None:
    data = b'\x91' * 100_000 + b'\xc0' if codec == 'msgpack' else b'[' * 100_000 + b']' * 100_000
    with pytest.raises(ValueError):
        CODECS[codec][1](data)


# ---- Hypothesis ----

fixed_tz = st.builds(dt.timezone, st.timedeltas(min_value=dt.timedelta(hours=-23, minutes=-59), max_value=dt.timedelta(hours=23, minutes=59)))
datetimes = st.one_of(
    st.datetimes(),
    st.datetimes(timezones=st.just(dt.UTC)),
    st.datetimes(timezones=fixed_tz),
    st.datetimes(timezones=st.timezones()),
)
hashable_scalars = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(),
    st.integers(min_value=-(2**200), max_value=2**200),
    st.floats(allow_nan=False),
    st.text(),
    st.binary(max_size=40),
    datetimes,
    st.dates(),
    st.decimals(allow_nan=False),
)
hashables = st.recursive(hashable_scalars, lambda c: st.one_of(st.tuples(c), st.tuples(c, c), st.frozensets(c, max_size=3)), max_leaves=6)
scalars = st.one_of(hashable_scalars, st.floats(allow_nan=True, allow_infinity=True))
keys = st.one_of(st.text(max_size=8), st.text(max_size=8).map(lambda s: '$' + s))


def containers(children: st.SearchStrategy[Any]) -> st.SearchStrategy[Any]:
    return st.one_of(
        st.lists(children, max_size=5),
        st.lists(children, max_size=5).map(tuple),
        st.dictionaries(keys, children, max_size=5),
        st.dictionaries(hashables, children, max_size=4),
        st.sets(hashables, max_size=4),
        st.frozensets(hashables, max_size=4),
    )


values = st.recursive(scalars, containers, max_leaves=30)


@pytest.mark.parametrize('codec', CODECS)
@settings(max_examples=int(os.environ.get('HYPOTHESIS_EXAMPLES', '1500')), deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(value=values)
def test_hypothesis_roundtrip(codec: str, value: Any) -> None:
    if codec == 'sonic' and has_negative_zero(value):
        return
    out = roundtrip(codec, value)
    assert exact_equal(out, value), (value, out)
