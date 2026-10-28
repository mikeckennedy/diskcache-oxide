"""The value codec (design section 5), through the `_core._encode` / `_core._decode` test hooks.

Round trips must give equal values of identical types, recursively, for both serializers. The tag
format these tests pin is specified in plans/spike-3-codecs.md.
"""

from __future__ import annotations

import collections
import datetime as dt
import enum
import json
import math
import pickle
import random
import sys
import threading
import time
import zoneinfo
from decimal import Decimal
from pathlib import Path
from typing import Any, NamedTuple

import pytest
from hypothesis import given
from hypothesis import strategies as st

import diskcache_oxide
from diskcache_oxide import CorruptValueError, UnsafeValueError
from diskcache_oxide._core import _decode, _encode
from tests.conformance.value_cases import CASES, exact_equal

SERIALIZERS = ('msgpack', 'json')
STRUCTURED_MODE = {'msgpack': 3, 'json': 4}
RAW_STR, RAW_BYTES, MSGPACK, JSON, PICKLE, NATIVE_INT, NATIVE_FLOAT = range(1, 8)


def roundtrip(value: Any, serializer: str = 'msgpack', pickle: bool = False) -> Any:
    mode, payload = _encode(value, serializer, pickle)
    return _decode(mode, payload, allow_pickle=pickle)


# ---- the conformance value table ----


@pytest.mark.parametrize('serializer', SERIALIZERS)
@pytest.mark.parametrize(('case_id', 'value', 'outcome', 'pickle_outcome'), CASES, ids=[c[0] for c in CASES])
def test_value_cases(serializer: str, case_id: str, value: Any, outcome: str, pickle_outcome: str) -> None:
    if outcome == 'TypeError':
        with pytest.raises(TypeError):
            _encode(value, serializer)
    else:
        assert exact_equal(roundtrip(value, serializer), value)
    assert pickle_outcome == 'roundtrip'
    assert exact_equal(roundtrip(value, serializer, pickle=True), value)


# ---- modes, and the native-mode boundaries ----


@pytest.mark.parametrize('serializer', SERIALIZERS)
@pytest.mark.parametrize(
    ('value', 'mode', 'payload'),
    [
        ('héllo', RAW_STR, 'héllo'.encode()),
        ('', RAW_STR, b''),
        (b'\xff\x00', RAW_BYTES, b'\xff\x00'),
        (0, NATIVE_INT, 0),
        (2**63 - 1, NATIVE_INT, 2**63 - 1),
        (-(2**63), NATIVE_INT, -(2**63)),
        (1.5, NATIVE_FLOAT, 1.5),
        (sys.float_info.max, NATIVE_FLOAT, sys.float_info.max),
        (5e-324, NATIVE_FLOAT, 5e-324),
    ],
)
def test_raw_and_native_modes(serializer: str, value: Any, mode: int, payload: Any) -> None:
    got_mode, got_payload = _encode(value, serializer)
    assert (got_mode, got_payload) == (mode, payload)
    assert type(got_payload) is type(payload)
    assert exact_equal(_decode(got_mode, got_payload), value)


@pytest.mark.parametrize('serializer', SERIALIZERS)
def test_negative_zero_is_native_and_keeps_its_sign(serializer: str) -> None:
    # The Rust test codec::tests::negative_zero_survives_real checks SQLite keeps the sign too.
    mode, payload = _encode(-0.0, serializer)
    assert mode == NATIVE_FLOAT and math.copysign(1, payload) == -1
    assert math.copysign(1, _decode(mode, payload)) == -1


@pytest.mark.parametrize('serializer', SERIALIZERS)
@pytest.mark.parametrize('value', [2**63, -(2**63) - 1, 2**64, 2**64 + 1, -(2**200), True, False, math.nan, math.inf, -math.inf, None])
def test_values_outside_native_range_use_the_serializer(serializer: str, value: Any) -> None:
    mode, payload = _encode(value, serializer)
    assert mode == STRUCTURED_MODE[serializer] and type(payload) is bytes
    assert exact_equal(_decode(mode, payload), value)


@pytest.mark.parametrize('serializer', SERIALIZERS)
def test_bool_is_never_native(serializer: str) -> None:
    for value in (True, False):
        mode, _ = _encode(value, serializer)
        assert mode not in (NATIVE_INT, NATIVE_FLOAT)
        assert roundtrip(value, serializer) is value


def test_serializer_name_is_checked() -> None:
    with pytest.raises(ValueError, match='msgpack'):
        _encode({}, 'pickle')
    assert _encode({}) == _encode({}, 'msgpack')


def test_lone_surrogate_str_is_refused() -> None:
    with pytest.raises(UnicodeEncodeError):
        _encode('\ud800')


def test_plain_json_is_standard_json() -> None:
    value = {'a': [1, 2.5, None, True, 'x'], 'b': {'c': 'd'}, 'n': -0.0}
    mode, payload = _encode(value, 'json')
    assert mode == JSON
    assert json.loads(payload) == value


@pytest.mark.parametrize('serializer', SERIALIZERS)
def test_large_values_take_the_direct_write_path(serializer: str) -> None:
    body = random.Random(42).randbytes(450 * 1024)
    for value in (
        {'content_gz': body, 'created': '2026-10-27T12:00:00+00:00', 'etag': 'abc123', 'version': 2},
        [body, 'x' * 100_000, 3],
        (body,),
        {'nested': {'big': body}},
    ):
        assert exact_equal(roundtrip(value, serializer), value)


# ---- rejected types ----


class MyStr(str):
    pass


class MyInt(int):
    pass


class MyFloat(float):
    pass


class MyBytes(bytes):
    pass


class MyDict(dict):  # type: ignore[type-arg]
    pass


class MyList(list):  # type: ignore[type-arg]
    pass


class Flag(enum.IntFlag):
    A = 1


class Color(enum.StrEnum):
    RED = 'red'


class P(NamedTuple):
    a: int


class CustomTz(dt.tzinfo):
    def utcoffset(self, d: Any) -> dt.timedelta:
        return dt.timedelta(hours=1)

    def dst(self, d: Any) -> dt.timedelta:
        return dt.timedelta(0)

    def tzname(self, d: Any) -> str:
        return 'X'


def _keyless_zoneinfo() -> dt.tzinfo:
    """A ZoneInfo with no key (from_file), which can't round-trip without pickle."""
    for root in zoneinfo.TZPATH:
        path = Path(root) / 'UTC'
        if path.is_file():
            with path.open('rb') as f:
                return zoneinfo.ZoneInfo.from_file(f)
    return CustomTz()  # no system tzdata: any foreign tzinfo is rejected the same way


def _recursive_list() -> list[Any]:
    a: list[Any] = []
    a.append(a)
    return a


REJECTED: dict[str, Any] = {
    'str-subclass': MyStr('x'),
    'int-subclass': MyInt(3),
    'float-subclass': MyFloat(1.5),
    'bytes-subclass': MyBytes(b'x'),
    'dict-subclass': MyDict(a=1),
    'list-subclass': MyList([1]),
    'int-flag': Flag.A,
    'str-enum': Color.RED,
    'namedtuple': P(1),
    'ordered-dict': collections.OrderedDict(a=1),
    'defaultdict': collections.defaultdict(int),
    'counter': collections.Counter('ab'),
    'deque': collections.deque([1]),
    'bytearray': bytearray(b'x'),
    'memoryview': memoryview(b'x'),
    'object': object(),
    'complex': 1j,
    'range': range(3),
    'type': int,
    'function': len,
    'time': dt.time(12, 0),
    'timedelta': dt.timedelta(1),
    'custom-tzinfo': dt.datetime(2026, 1, 1, tzinfo=CustomTz()),
    'named-timezone': dt.datetime(2026, 1, 1, tzinfo=dt.timezone(dt.timedelta(hours=-7), 'PDT')),
    'zoneinfo-from-file': dt.datetime(2026, 1, 1, tzinfo=_keyless_zoneinfo()),
    'nested-subclass': {'a': [1, MyStr('x')]},
    'subclass-key': {MyStr('k'): 1},
    'nested-in-tuple': (1, Flag.A),
    'nested-in-set': {1, Color.RED},
    'recursive': _recursive_list(),
    'too-deep': json.loads('[' * 200 + ']' * 200),
}


@pytest.mark.parametrize('serializer', SERIALIZERS)
@pytest.mark.parametrize('name', REJECTED)
def test_rejected_without_pickle(serializer: str, name: str) -> None:
    with pytest.raises(TypeError):
        _encode(REJECTED[name], serializer)


PICKLABLE = [n for n in REJECTED if n not in ('memoryview', 'zoneinfo-from-file')]


@pytest.mark.parametrize('serializer', SERIALIZERS)
@pytest.mark.parametrize('name', PICKLABLE)
def test_rejected_values_pickle_when_allowed(serializer: str, name: str) -> None:
    value = REJECTED[name]
    mode, payload = _encode(value, serializer, pickle=True)
    assert mode == PICKLE
    out = _decode(mode, payload, allow_pickle=True)
    assert type(out) is type(value)


@pytest.mark.parametrize('serializer', SERIALIZERS)
def test_pickle_only_when_the_serializer_cannot(serializer: str) -> None:
    for value in ({'a': (1, 2)}, [dt.date(2026, 1, 1)], {1, 2}, None):
        assert _encode(value, serializer, pickle=True)[0] == STRUCTURED_MODE[serializer]
    assert _encode('x', serializer, pickle=True)[0] == RAW_STR


def test_mutation_errors_never_fall_back_to_pickle() -> None:
    # Only TypeError (can't represent) falls back; other errors propagate.
    with pytest.raises(UnicodeEncodeError):
        _encode(['\ud800'], pickle=True)


class Spy(dt.tzinfo):
    """A tzinfo that records every call, to prove the encoder never runs it."""

    calls: list[str] = []

    def utcoffset(self, d: Any) -> dt.timedelta:
        Spy.calls.append('utcoffset')
        return dt.timedelta(0)

    def dst(self, d: Any) -> dt.timedelta:
        Spy.calls.append('dst')
        return dt.timedelta(0)

    def tzname(self, d: Any) -> str:
        Spy.calls.append('tzname')
        return 'S'


class SpyStr(str):
    def __str__(self) -> str:
        Spy.calls.append('__str__')
        return 'spy'

    def __hash__(self) -> int:
        Spy.calls.append('__hash__')
        return 0


class SpyDict(dict):  # type: ignore[type-arg]
    def items(self) -> Any:
        Spy.calls.append('items')
        return super().items()

    def __iter__(self) -> Any:
        Spy.calls.append('__iter__')
        return super().__iter__()


@pytest.mark.parametrize('serializer', SERIALIZERS)
def test_encoder_runs_no_user_code(serializer: str) -> None:
    Spy.calls.clear()
    for value in (dt.datetime(2026, 1, 1, tzinfo=Spy()), SpyStr('k'), {'a': SpyStr('v')}, SpyDict(a=1), [SpyDict()]):
        with pytest.raises(TypeError):
            _encode(value, serializer)
    assert Spy.calls == []


# ---- the tag format and the escape rule ----


def test_json_tag_spellings() -> None:
    def enc(v: Any) -> bytes:
        mode, payload = _encode(v, 'json')
        assert mode == JSON
        return payload

    assert enc({'$t': 1, 'x': 2}) == b'{"$esc":{"$t":1,"x":2}}'
    assert enc({'x': 1, '$t': 2}) == b'{"x":1,"$t":2}'  # only the first key is checked
    assert enc((1, b'\x00')) == b'{"$tuple":[1,{"$bytes":"AA=="}]}'
    assert enc({1: 'a'}) == b'{"$dict":[[1,"a"]]}'
    assert enc([math.nan, -math.inf, 2**64]) == b'[{"$float":"nan"},{"$float":"-inf"},{"$int":"18446744073709551616"}]'
    assert enc(dt.date(2026, 1, 2)) == b'{"$date":"2026-01-02"}'
    assert enc(Decimal('1.10')) == b'{"$decimal":"1.10"}'
    assert enc(frozenset()) == b'{"$frozenset":[]}'
    la = zoneinfo.ZoneInfo('America/Los_Angeles')
    assert enc(dt.datetime(2026, 11, 1, 1, 30, fold=1, tzinfo=la)) == b'{"$datetime":"2026-11-01T01:30:00-08:00[America/Los_Angeles][_fold=1]"}'
    assert enc(dt.datetime(2026, 1, 1, fold=1)) == b'{"$datetime":"2026-01-01T00:00:00[_fold=1]"}'


def test_msgpack_tag_spellings() -> None:
    assert _encode({'$t': 1}) == (MSGPACK, b'\x81\xa4$esc\x81\xa2$t\x01')
    assert _encode((1,)) == (MSGPACK, b'\x81\xa6$tuple\x91\x01')
    assert _encode([b'\x00']) == (MSGPACK, b'\x91\xc4\x01\x00')  # bytes are native bin
    assert _encode({1: 2}) == (MSGPACK, b'\x81\x01\x02')  # non-str keys are native
    # A UTC datetime is the standard timestamp extension (timestamp32 here).
    assert _encode(dt.datetime(1970, 1, 1, 0, 0, 1, tzinfo=dt.UTC)) == (MSGPACK, b'\xd6\xff\x00\x00\x00\x01')


TAG_SHAPED = [
    {'$tuple': [1, 2]},
    {'$esc': {'$set': [1]}},
    {'$esc': 1},
    {'$': None},
    {'$t': 'datetime', '$v': '2026-10-27T00:00:00'},
    {'$bytes': 'AA=='},
    {'$float': 'nan'},
    {'$dict': [[1, 2]]},
    {'$nope': 1},
    {'a': {'$int': '5'}},
    [{'$set': []}],
    {'$t': {'$t': {'$t': 1}}},
    {1: {'$tuple': []}},
    ({'$tuple': ()},),
]


@pytest.mark.parametrize('serializer', SERIALIZERS)
@pytest.mark.parametrize('value', TAG_SHAPED, ids=repr)
def test_tag_shaped_user_data_round_trips(serializer: str, value: Any) -> None:
    assert exact_equal(roundtrip(value, serializer), value)


@pytest.mark.parametrize('serializer', SERIALIZERS)
def test_zoneinfo_fold_survives(serializer: str) -> None:
    la = zoneinfo.ZoneInfo('America/Los_Angeles')
    for fold in (0, 1):
        v = dt.datetime(2026, 11, 1, 1, 30, fold=fold, tzinfo=la)
        out = roundtrip(v, serializer)
        assert out.fold == fold and out.utcoffset() == v.utcoffset() and out.tzinfo is la
        for other in (dt.datetime(2026, 1, 1, fold=fold), dt.datetime(2026, 1, 1, fold=fold, tzinfo=dt.UTC), dt.datetime(2026, 7, 1, fold=fold, tzinfo=la)):
            assert roundtrip(other, serializer).fold == fold


def test_utc_singleton_survives() -> None:
    for serializer in SERIALIZERS:
        assert roundtrip(dt.datetime(2026, 1, 1, tzinfo=dt.UTC), serializer).tzinfo is dt.UTC


# ---- read-side errors ----


def test_pickle_rows_need_allow_pickle() -> None:
    payload = pickle.dumps({'a': 1}, protocol=5)
    with pytest.raises(UnsafeValueError):
        _decode(PICKLE, payload)
    assert _decode(PICKLE, payload, allow_pickle=True) == {'a': 1}
    assert issubclass(UnsafeValueError, TypeError)


def test_pickle_errors_pass_through() -> None:
    with pytest.raises(pickle.UnpicklingError):
        _decode(PICKLE, b'not a pickle', allow_pickle=True)


GARBAGE = [
    b'',
    b'\xc1',
    b'\xff\xff\xff',
    b'{"a"',
    b'not json at all',
    b'\x92\x01',  # msgpack array missing an item
    b'\x81\xa4$nop\x01',  # unknown msgpack tag
    b'\x82\xa6$tuple\x90\x01\x02',  # tag map with two entries
    b'\xa3\xff\xfe\xfd',  # msgpack str with invalid UTF-8
    b'\xd4\x05\x00',  # unknown extension
    b'\xd7\xff\x00\x00\x01\x00\x00\x00\x00\x00',  # timestamp64: 0 s and 64 ns (sub-microsecond)
    b'\x01\x02',  # trailing bytes after a msgpack value
    b'[1] x',
    b'{"$nope":1}',
    b'{"$tuple":[1],"x":2}',
    b'{"$int":"12_3"}',
    b'{"$int":" 1"}',
    b'{"$bytes":"A"}',
    b'{"$float":"zero"}',
    b'{"$decimal":"1.x"}',
    b'{"$date":"yesterday"}',
    b'{"$datetime":"2026-01-01T00:00:00[No/Such_Zone]"}',
    b'{"$dict":[[1]]}',
    b'{"$esc":[1]}',
    b'\x81\xa6$bytes\xa0',  # JSON-only tag in msgpack
    b'\x81\xa5$date\xa9yesterday',
    b'\x81\xa8$decimal\xa31.x',
    b'\x91' * 100_000 + b'\xc0',
    b'[' * 100_000 + b']' * 100_000,
]


@pytest.mark.parametrize('mode', [MSGPACK, JSON])
@pytest.mark.parametrize('payload', GARBAGE, ids=lambda p: repr(p[:24]))
def test_garbage_is_corrupt(mode: int, payload: bytes) -> None:
    with pytest.raises(CorruptValueError):
        _decode(mode, payload)


@pytest.mark.parametrize('mode', [MSGPACK, JSON])
def test_random_garbage_is_corrupt_or_decodes(mode: int) -> None:
    rng = random.Random(7)
    for _ in range(3000):
        payload = rng.randbytes(rng.randrange(1, 40))
        try:
            _decode(mode, payload)
        except CorruptValueError:
            pass


def test_specific_corrupt_rows() -> None:
    for mode, payload in [
        (0, b'x'),
        (8, b'x'),
        (-1, 1),
        (RAW_STR, b'\xff'),
        (NATIVE_INT, b'1'),
        (NATIVE_INT, 1.0),
        (NATIVE_FLOAT, b'1.0'),
        (NATIVE_FLOAT, 1),
        (RAW_BYTES, 1),
        (MSGPACK, 1),
        (JSON, 2.0),
        (MSGPACK, b'\xc1'),
        (JSON, b'{'),
    ]:
        with pytest.raises(CorruptValueError):
            _decode(mode, payload)
    assert issubclass(CorruptValueError, ValueError)
    assert diskcache_oxide.CorruptValueError is CorruptValueError
    assert CorruptValueError.__module__ == 'diskcache_oxide'


def test_corrupt_keeps_the_python_cause() -> None:
    with pytest.raises(CorruptValueError) as info:
        _decode(JSON, b'{"$decimal":"1.x"}')
    assert info.value.__cause__ is not None
    with pytest.raises(CorruptValueError) as info:
        _decode(MSGPACK, b'\x81\xa8$decimal\xa31.x')
    assert info.value.__cause__ is not None


def test_decode_hook_checks_the_payload_type() -> None:
    with pytest.raises(TypeError):
        _decode(RAW_STR, 'x')  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        _decode(NATIVE_INT, True)


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
    st.decimals(allow_nan=False),  # Decimal('NaN') != itself, so exact_equal can't check it.
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


@pytest.mark.parametrize('serializer', SERIALIZERS)
@given(value=values)
def test_hypothesis_roundtrip(serializer: str, value: Any) -> None:
    mode, payload = _encode(value, serializer)
    out = _decode(mode, payload)
    assert exact_equal(out, value), (value, out)


# ---- free-threaded stress ----


def _consistent(out: Any) -> bool:
    """The shape the mutator maintains: k<i> -> [i, str(i)], 'lst' -> [0, 1, ..., n-1], 's' a set of ints."""
    if type(out) is not dict:
        return False
    for k, v in out.items():
        if k == 'lst':
            if type(v) is not list or v != list(range(len(v))):
                return False
        elif k == 's':
            if type(v) is not set or not all(type(x) is int for x in v):
                return False
        elif k == 'nested':
            if type(v) is not dict or not all(type(x) is int and x == int(y) for y, x in v.items()):
                return False
        else:
            i = int(k[1:])
            if v != [i, str(i)]:
                return False
    return True


@pytest.mark.parametrize('serializer', SERIALIZERS)
def test_concurrent_mutation_never_corrupts(serializer: str) -> None:
    """One thread mutates a dict (and a list, set and dict inside it) while others encode it.

    Every encode either produces bytes that decode to a consistent-looking value, or raises
    RuntimeError/TypeError. It never crashes, panics, or produces bytes that fail to decode. Most
    interesting on the free-threaded build, where the threads really run at once.
    """
    shared: dict[str, Any] = {'lst': [], 's': set(), 'nested': {}}
    stop = threading.Event()
    counts = collections.Counter[str]()
    failures: list[BaseException] = []

    def mutate() -> None:
        rng = random.Random(1)
        while not stop.is_set():
            i = rng.randrange(200)
            op = rng.randrange(6)
            if op == 0:
                shared[f'k{i}'] = [i, str(i)]
            elif op == 1:
                shared.pop(f'k{i}', None)
            elif op == 2:
                lst = shared['lst']
                if len(lst) < 50:
                    lst.append(len(lst))
                else:
                    del lst[:]
            elif op == 3:
                if shared['lst']:
                    shared['lst'].pop()
            elif op == 4:
                shared['s'].add(i) if rng.random() < 0.6 else shared['s'].discard(i)
            elif rng.random() < 0.6:
                shared['nested'][str(i)] = i
            else:
                shared['nested'].pop(str(i), None)

    def encode_loop() -> None:
        deadline = time.monotonic() + 1.0
        try:
            while time.monotonic() < deadline:
                try:
                    mode, payload = _encode(shared, serializer)
                except (RuntimeError, TypeError):
                    counts['refused'] += 1
                    continue
                out = _decode(mode, payload)  # must never raise
                assert _consistent(out), out
                counts['ok'] += 1
        except BaseException as ex:  # noqa: BLE001 - reported to the main thread
            failures.append(ex)

    mutator = threading.Thread(target=mutate)
    encoders = [threading.Thread(target=encode_loop) for _ in range(4)]
    mutator.start()
    for t in encoders:
        t.start()
    for t in encoders:
        t.join()
    stop.set()
    mutator.join()
    assert not failures, failures[0]
    assert counts['ok'] > 100
