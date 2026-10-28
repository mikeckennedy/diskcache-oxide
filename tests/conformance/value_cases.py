"""The value-fidelity table for test_values.py.

Each case is `(id, value, oxide_default, oxide_pickle)`. The outcomes say what diskcache-oxide
should do with the value under the default `pickle=False` and with `pickle=True`:

    'roundtrip'  `get` returns a value of exactly the same type, recursively, equal to the original.
    'TypeError'  `set` raises TypeError (D-001).
    'TBD'        not decided yet; Phase 1 settles the tag format. Skipped on oxide.

diskcache pickles everything, so every case round-trips there.
"""

from __future__ import annotations

import collections
import dataclasses
import datetime as dt
import enum
import gzip
import math
import zoneinfo
from decimal import Decimal
from typing import Any, Literal, NamedTuple

Outcome = Literal['roundtrip', 'TypeError', 'TBD']


@dataclasses.dataclass(slots=True)
class HomepageSnapshot:
    """The shape of Talk Python's homepage snapshot. Module level so it pickles."""

    title: str
    episode_ids: list[int]
    built_at: dt.datetime


class Color(enum.StrEnum):
    """Module level so it pickles."""

    RED = 'red'


class Priority(enum.IntEnum):
    HIGH = 1


class Point(NamedTuple):
    x: int
    y: int


_PAGE = ('<html><body>' + 'Talk Python episode listing. ' * 20_000 + '</body></html>').encode()

CASES: list[tuple[str, Any, Outcome, Outcome]] = [
    # str and bytes are stored raw by oxide.
    ('str-empty', '', 'roundtrip', 'roundtrip'),
    ('str-unicode', 'héllo wörld ☃ \U0001f40d', 'roundtrip', 'roundtrip'),
    ('str-large', 'x' * 1_000_000, 'roundtrip', 'roundtrip'),
    ('bytes-empty', b'', 'roundtrip', 'roundtrip'),
    ('bytes-large', bytes(range(256)) * 4_000, 'roundtrip', 'roundtrip'),
    ('bytes-non-utf8', b'\xff\xfe\x00\x80invalid', 'roundtrip', 'roundtrip'),
    # int: big ints are tagged in JSON.
    ('int-zero', 0, 'roundtrip', 'roundtrip'),
    ('int-negative', -12345, 'roundtrip', 'roundtrip'),
    ('int-2**63', 2**63, 'roundtrip', 'roundtrip'),
    ('int-2**64+1', 2**64 + 1, 'roundtrip', 'roundtrip'),
    ('int-negative-huge', -(2**200), 'roundtrip', 'roundtrip'),
    # float: NaN and inf are tagged in JSON; -0.0 must keep its sign.
    ('float-plain', 3.25, 'roundtrip', 'roundtrip'),
    ('float-negative-zero', -0.0, 'roundtrip', 'roundtrip'),
    ('float-nan', math.nan, 'roundtrip', 'roundtrip'),
    ('float-inf', math.inf, 'roundtrip', 'roundtrip'),
    ('float-negative-inf', -math.inf, 'roundtrip', 'roundtrip'),
    # bool must not come back as int.
    ('bool-true', True, 'roundtrip', 'roundtrip'),
    ('bool-false', False, 'roundtrip', 'roundtrip'),
    ('none', None, 'roundtrip', 'roundtrip'),
    # Containers.
    ('list-empty', [], 'roundtrip', 'roundtrip'),
    ('dict-empty', {}, 'roundtrip', 'roundtrip'),
    ('nested-dict-list', {'a': [1, 2.5, {'b': [None, True, 'c']}], 'd': {'e': []}}, 'roundtrip', 'roundtrip'),
    ('tuple', (1, 'two', 3.0), 'roundtrip', 'roundtrip'),
    ('tuple-empty', (), 'roundtrip', 'roundtrip'),
    ('tuple-nested-in-list', [(1, 2), [3, (4,)]], 'roundtrip', 'roundtrip'),
    ('dict-int-keys', {1: 'one', 2: 'two', -3: 'minus three'}, 'roundtrip', 'roundtrip'),
    ('dict-mixed-keys', {'a': 1, 2: 'b'}, 'roundtrip', 'roundtrip'),
    # Talk Python's gzip page entry: bytes inside a dict.
    ('dict-gzip-entry', {'status': 200, 'headers': {'Content-Encoding': 'gzip'}, 'body': gzip.compress(_PAGE)}, 'roundtrip', 'roundtrip'),
    ('datetime-naive', dt.datetime(2026, 10, 27, 13, 45, 30, 123456), 'roundtrip', 'roundtrip'),
    ('datetime-aware-utc', dt.datetime(2026, 10, 27, 13, 45, 30, tzinfo=dt.UTC), 'roundtrip', 'roundtrip'),
    ('datetime-aware-offset', dt.datetime(2026, 10, 27, 13, 45, tzinfo=dt.timezone(dt.timedelta(hours=-7))), 'roundtrip', 'roundtrip'),
    ('date', dt.date(2026, 10, 27), 'roundtrip', 'roundtrip'),  # Settled in Phase 1 Spike 3.
    ('decimal', Decimal('3.14159265358979323846'), 'roundtrip', 'roundtrip'),  # Settled in Phase 1 Spike 3.
    ('set', {1, 2, 3}, 'roundtrip', 'roundtrip'),  # Settled in Phase 1 Spike 3.
    # User data that looks like a tag must not be decoded as one (the escape mechanism).
    ('dict-tag-shaped', {'__dco__': 'tuple', 'v': [1, 2]}, 'roundtrip', 'roundtrip'),
    # Shaped like the real tag format settled in Phase 1 (`$`-prefixed single-key dicts).
    ('dict-dollar-tag-shaped', {'$tuple': [1, 2]}, 'roundtrip', 'roundtrip'),
    ('dict-dollar-esc-shaped', {'$esc': {'$set': [1]}}, 'roundtrip', 'roundtrip'),
    ('dict-tag-shaped-dollar', {'$t': 'datetime', '$v': '2026-10-27T00:00:00'}, 'roundtrip', 'roundtrip'),
    # A slots dataclass needs pickle.
    ('slots-dataclass', HomepageSnapshot('Talk Python', [500, 501], dt.datetime(2026, 10, 27)), 'TypeError', 'roundtrip'),
    # Subclasses of types oxide can store. Without pickle they would silently come back as the
    # base type (str, int, tuple, dict), so they are refused instead.
    ('str-enum', Color.RED, 'TypeError', 'roundtrip'),
    ('int-enum', Priority.HIGH, 'TypeError', 'roundtrip'),
    ('namedtuple', Point(1, 2), 'TypeError', 'roundtrip'),
    ('ordered-dict', collections.OrderedDict([('b', 1), ('a', 2)]), 'TypeError', 'roundtrip'),
    ('defaultdict', collections.defaultdict(list, {'a': [1]}), 'TypeError', 'roundtrip'),
    ('datetime-zoneinfo', dt.datetime(2026, 10, 27, 13, 45, tzinfo=zoneinfo.ZoneInfo('America/Los_Angeles')), 'roundtrip', 'roundtrip'),  # Settled in Phase 1 Spike 3.
]

# Cases where diskcache 5.6.3 itself does not round-trip, keyed by case id, mapped to (divergence ref,
# description). The round-trip tests are strict xfails on diskcache for these, and test_values.py
# pins diskcache's actual behavior separately.
DISKCACHE_QUIRKS: dict[str, tuple[str, str]] = {
    # Verified on 5.6.3: set returns True, the key is present and counted by len(), but get returns
    # None (even with a different default). NaN nested in a container round-trips fine. The likely
    # cause is that top-level floats are stored natively in SQLite, where NaN becomes NULL.
    'float-nan': ('D-008', 'a top-level float NaN comes back as None'),
}


def exact_equal(a: Any, b: Any) -> bool:
    """Recursive equality that also requires identical types at every level.

    NaN equals NaN, and -0.0 does not equal 0.0. Dicts must also keep their insertion order.
    """
    if type(a) is not type(b):
        return False
    if isinstance(a, float):
        if math.isnan(a) or math.isnan(b):
            return math.isnan(a) and math.isnan(b)
        return a == b and math.copysign(1.0, a) == math.copysign(1.0, b)
    if isinstance(a, collections.defaultdict) and a.default_factory is not b.default_factory:
        return False
    if isinstance(a, dict):
        return len(a) == len(b) and all(exact_equal(ka, kb) and exact_equal(va, vb) for (ka, va), (kb, vb) in zip(a.items(), b.items()))
    if isinstance(a, (list, tuple)):
        return len(a) == len(b) and all(exact_equal(x, y) for x, y in zip(a, b))
    if isinstance(a, (set, frozenset)):
        return len(a) == len(b) and all(any(exact_equal(x, y) for y in b) for x in a)
    if isinstance(a, dt.datetime):
        # Aware and naive datetimes never compare equal; also require the same offset, not just the
        # same instant, and the same kind of tzinfo (a ZoneInfo must not come back as a fixed offset).
        return a == b and a.utcoffset() == b.utcoffset() and type(a.tzinfo) is type(b.tzinfo) and a.tzinfo == b.tzinfo
    if dataclasses.is_dataclass(a):
        return all(exact_equal(getattr(a, f.name), getattr(b, f.name)) for f in dataclasses.fields(a))
    return bool(a == b)
