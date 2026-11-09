"""The type registry (plan 16): per-type encode and decode functions, and the dataclass default.

A registered value is stored as `{"$t:<name>": body}` in both serializers. Reads resolve the name in
the reading process's registry; an unregistered name is foreign and the row stays. A registered
type is never pickled. A decode function's error is classified as a pickle hook's would be, except
that one about the row becomes `CorruptValueError` and the row is deleted under `'miss'`.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import gc
import subprocess
import sys
import threading
import time
import weakref
from collections.abc import Iterator
from pathlib import Path
from typing import Any, NamedTuple

import diskcache_oxide
import pytest
from diskcache_oxide import (
    Cache,
    Caches,
    CorruptValueError,
    FanoutCache,
    ForeignValueError,
    NullCache,
    Timeout,
    TypeRegistry,
    UnsafeValueError,
    register_type,
    registered_types,
    registry,
    unregister_type,
)
from diskcache_oxide._core import _decode, _encode

SERIALIZERS = ('msgpack', 'json')
MSGPACK, JSON, PICKLE = 3, 4, 5


# -- the types under test -------------------------------------------------------------------------


class Point:
    def __init__(self, x: int, y: int) -> None:
        self.x, self.y = x, y

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Point) and (self.x, self.y) == (other.x, other.y)

    __hash__ = None  # type: ignore[assignment]


def point_encode(p: Point) -> tuple[int, int]:
    return (p.x, p.y)


def point_decode(v: Any) -> Point:
    return Point(*v)


@dataclasses.dataclass
class Episode:
    id: int
    title: str = 'untitled'
    published: dt.datetime | None = None
    tags: list[str] = dataclasses.field(default_factory=list)
    slug: str = dataclasses.field(init=False, default='')

    def __post_init__(self) -> None:
        self.slug = self.title.lower().replace(' ', '-')


@dataclasses.dataclass(slots=True, frozen=True)
class Guest:
    name: str
    episodes: list[Episode] = dataclasses.field(default_factory=list)


class Entry(NamedTuple):
    key: str
    contents: str


class Html(str):
    """A str subclass, as markdown2 returns."""


class Boom:
    """Decoding raises whatever `error` is set to, after running `hook` if any."""

    error: BaseException = ValueError('bad body')
    hook: Any = None

    @staticmethod
    def encode(_: Boom) -> int:
        return 1

    @staticmethod
    def decode(_: Any) -> Boom:
        if Boom.hook is not None:
            Boom.hook()
        raise Boom.error


# -- fixtures ------------------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolated_global_registry() -> Iterator[None]:
    """Every test in this process shares `diskcache_oxide.registry`: put it back as it was."""
    before = registered_types()
    yield
    for cls in registered_types().values():
        unregister_type(cls)
    # Nothing in this suite registers outside a test, so "as it was" is empty; be exact anyway.
    assert not before
    Boom.error = ValueError('bad body')
    Boom.hook = None


@pytest.fixture
def directory(tmp_path: Path) -> Path:
    return tmp_path / 'c'


def _sql(directory: Path, statement: str) -> None:
    script = (
        'import sqlite3, sys\ncon = sqlite3.connect(sys.argv[1])\ncon.execute(sys.argv[2])\ncon.commit()\ncon.close()'
    )
    subprocess.run([sys.executable, '-c', script, str(directory / 'diskcache-oxide.sqlite'), statement], check=True)


def _stored(directory: Path, key: str) -> bool:
    """Whether `key` still has a row, checked by a fresh handle that never decodes."""
    with Cache(directory) as other:
        return key in other


def _mode(directory: Path, key: str) -> int:
    script = 'import sqlite3, sys\ncon = sqlite3.connect(sys.argv[1])\nprint(con.execute(sys.argv[2]).fetchone()[0])'
    out = subprocess.run(
        [
            sys.executable,
            '-c',
            script,
            str(directory / 'diskcache-oxide.sqlite'),
            f"SELECT mode FROM cache WHERE key = '{key}'",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return int(out.stdout)


def _register_all() -> None:
    register_type(Point, name='t.Point', encode=point_encode, decode=point_decode)
    register_type(Episode, name='t.Episode')
    register_type(Guest, name='t.Guest')


# -- round trips ---------------------------------------------------------------------------------


@pytest.mark.parametrize('serializer', SERIALIZERS)
def test_an_explicit_type_round_trips(directory: Path, serializer: str) -> None:
    register_type(Point, name='t.Point', encode=point_encode, decode=point_decode)
    with Cache(directory, serializer=serializer) as cache:
        cache.set('p', Point(1, 2))
        assert cache.get('p') == Point(1, 2)
        cache.set('nested', {'points': [Point(3, 4), Point(5, 6)], 'n': 1})
        assert cache.get('nested') == {'points': [Point(3, 4), Point(5, 6)], 'n': 1}
        assert cache.pop('p') == Point(1, 2)
        assert 'p' not in cache


@pytest.mark.parametrize('serializer', SERIALIZERS)
def test_the_tag_on_the_wire(serializer: str) -> None:
    register_type(Point, name='t.Point', encode=point_encode, decode=point_decode)
    mode, payload = _encode(Point(1, 2), serializer)
    assert mode == {'msgpack': MSGPACK, 'json': JSON}[serializer]
    if serializer == 'json':
        assert payload == b'{"$t:t.Point":{"$tuple":[1,2]}}'
    else:
        assert payload == b'\x81\xaa$t:t.Point\x81\xa6$tuple\x92\x01\x02'
    assert _decode(mode, payload) == Point(1, 2)


@pytest.mark.parametrize('serializer', SERIALIZERS)
def test_a_dataclass_round_trips(directory: Path, serializer: str) -> None:
    _register_all()
    when = dt.datetime(2026, 11, 6, 9, 30, tzinfo=dt.UTC)
    episode = Episode(491, 'Rust and Python', when, ['rust'])
    guest = Guest('Sam', [episode, Episode(1)])
    with Cache(directory, serializer=serializer) as cache:
        cache.set('e', episode)
        back = cache.get('e')
        assert back == episode
        assert type(back) is Episode
        assert back.published == when and back.published.tzinfo is not None
        assert back.slug == 'rust-and-python', '__post_init__ ran on the way out'
        cache.set('g', guest)
        assert cache.get('g') == guest
        assert cache.get('g').episodes[1].title == 'untitled', 'a default applied'


def test_init_false_fields_are_not_stored() -> None:
    register_type(Episode, name='t.Episode')
    _, payload = _encode(Episode(1, 'A B'), 'json')
    assert payload == b'{"$t:t.Episode":{"id":1,"title":"A B","published":null,"tags":[]}}'
    back: Any = _decode(JSON, payload)
    assert back.slug == 'a-b'


def test_a_namedtuple_and_a_str_subclass_through_explicit_functions(directory: Path) -> None:
    register_type(Entry, name='t.Entry', encode=tuple, decode=lambda t: Entry(*t))
    register_type(Html, name='t.Html', encode=str, decode=Html)
    with Cache(directory) as cache:
        cache.set('e', Entry('k', '<p>'))
        assert cache.get('e') == Entry('k', '<p>') and type(cache.get('e')) is Entry
        cache.set('h', Html('<b>'))
        assert type(cache.get('h')) is Html and cache.get('h') == '<b>'


def test_an_escaped_user_key_that_looks_like_a_tag_round_trips() -> None:
    register_type(Point, name='x', encode=point_encode, decode=point_decode)
    for serializer in SERIALIZERS:
        mode, payload = _encode({'$t:x': 1}, serializer)
        assert _decode(mode, payload) == {'$t:x': 1}


@pytest.mark.parametrize('serializer', SERIALIZERS)
def test_the_hooks_take_a_registry(serializer: str) -> None:
    reg = TypeRegistry()
    reg.register(Point, name='t.Point', encode=point_encode, decode=point_decode)
    mode, payload = _encode(Point(1, 2), serializer, types=reg)
    assert _decode(mode, payload, types=reg) == Point(1, 2)
    with pytest.raises(ForeignValueError, match='unknown tag "\\$t:t.Point"'):
        _decode(mode, payload)
    with pytest.raises(TypeError):
        _encode(Point(1, 2), serializer)


@pytest.mark.parametrize(
    ('mode', 'payload'),
    [
        (JSON, b'{"$t:t.Point":[1,2],"x":1}'),
        (MSGPACK, b'\x82\xaa$t:t.Point\x92\x01\x02\xa1x\x01'),
    ],
    ids=['json', 'msgpack'],
)
def test_a_registered_tag_map_with_a_second_entry_is_corrupt(mode: int, payload: bytes) -> None:
    reg = TypeRegistry()
    reg.register(Point, name='t.Point', encode=point_encode, decode=point_decode)
    with pytest.raises(CorruptValueError) as info:
        _decode(mode, payload, types=reg)
    assert not isinstance(info.value, ForeignValueError)


# -- the registry's rules ------------------------------------------------------------------------


def test_a_per_handle_registry_shadows_the_global_one(directory: Path) -> None:
    register_type(Point, name='t.Point', encode=point_encode, decode=point_decode)
    swapped = TypeRegistry()
    swapped.register(Point, name='t.Point', encode=lambda p: (p.y, p.x), decode=lambda v: Point(v[1], v[0]))
    other_name = TypeRegistry()
    other_name.register(Point, name='other.Point', encode=point_encode, decode=point_decode)
    with (
        Cache(directory) as plain,
        Cache(directory, types=swapped) as mine,
        Cache(directory, types=other_name) as named,
    ):
        plain.set('p', Point(1, 2))
        mine.set('q', Point(1, 2))
        named.set('r', Point(1, 2))
        # Same name, different encoding: each handle reads its own rows back right, and the other's wrong.
        assert mine.get('q') == Point(1, 2)
        assert plain.get('q') == Point(2, 1)
        assert mine.get('p') == Point(2, 1)
        # The per-handle name wins, and the global one is still reachable through it.
        assert named.get('r') == Point(1, 2)
        assert named.get('p') == Point(1, 2)
        with pytest.raises(ForeignValueError):
            plain.get('r')


def test_the_own_types_are_refused() -> None:
    for cls in (str, bytes, int, float, bool, type(None), list, tuple, dict, set, frozenset, dt.datetime, dt.date):
        with pytest.raises(ValueError, match='stores itself'):
            register_type(cls, name='x', encode=str, decode=str)
    from decimal import Decimal

    with pytest.raises(ValueError, match='stores itself'):
        register_type(Decimal, name='x', encode=str, decode=Decimal)
    # Their subclasses are exactly what the registry is for.
    register_type(Html, name='t.Html', encode=str, decode=Html)


def test_registration_is_validated() -> None:
    with pytest.raises(TypeError):
        register_type(Point(1, 2), name='x', encode=point_encode, decode=point_decode)  # ty: ignore[invalid-argument-type]
    with pytest.raises(ValueError, match='empty'):
        register_type(Point, name='', encode=point_encode, decode=point_decode)
    with pytest.raises(UnicodeEncodeError):
        register_type(Point, name='bad\udc80', encode=point_encode, decode=point_decode)
    with pytest.raises(TypeError, match='go together'):
        register_type(Point, name='x', encode=point_encode)
    with pytest.raises(TypeError, match='go together'):
        register_type(Point, name='x', decode=point_decode)
    with pytest.raises(TypeError, match='callable'):
        register_type(Point, name='x', encode=1, decode=point_decode)  # ty: ignore[invalid-argument-type]
    with pytest.raises(TypeError, match='not a dataclass'):
        register_type(Point, name='x')
    with pytest.raises(TypeError):
        register_type(1, name='x')  # ty: ignore[invalid-argument-type]
    assert not registered_types()


def test_a_dataclass_with_an_initvar_is_refused() -> None:
    @dataclasses.dataclass
    class WithInit:
        x: int
        scale: dataclasses.InitVar[int] = 1

        def __post_init__(self, scale: int) -> None:
            self.x *= scale

    with pytest.raises(TypeError, match='InitVar field scale'):
        register_type(WithInit, name='t.WithInit')
    register_type(WithInit, name='t.WithInit', encode=lambda o: o.x, decode=lambda x: WithInit(x))
    mode, payload = _encode(WithInit(2, 3))
    back: Any = _decode(mode, payload)
    assert back.x == 6


def test_last_registration_wins_by_class_and_by_name() -> None:
    register_type(Point, name='t.Point', encode=point_encode, decode=point_decode)
    register_type(Point, name='t.Point', encode=lambda p: [p.x], decode=lambda v: Point(v[0], 0))
    assert _decode(*_encode(Point(1, 2))) == Point(1, 0)
    assert registered_types() == {'t.Point': Point}
    # The name moves to another class: the old class is unregistered by the same change.
    register_type(Entry, name='t.Point', encode=tuple, decode=lambda t: Entry(*t))
    assert registered_types() == {'t.Point': Entry}
    with pytest.raises(TypeError):
        _encode(Point(1, 2))
    assert _decode(*_encode(Entry('a', 'b'))) == Entry('a', 'b')
    # A class re-registered under a new name frees its old one.
    register_type(Entry, name='t.Entry', encode=tuple, decode=lambda t: Entry(*t))
    assert registered_types() == {'t.Entry': Entry}


def test_unregister_and_introspection() -> None:
    assert len(registry) == 0
    assert repr(registry) == 'TypeRegistry([])'
    _register_all()
    assert len(registry) == 3
    assert repr(registry) == "TypeRegistry(['t.Episode', 't.Guest', 't.Point'])"
    assert unregister_type(Point) is True
    assert unregister_type(Point) is False
    assert registered_types() == {'t.Episode': Episode, 't.Guest': Guest}
    with pytest.raises(TypeError):
        _encode(Point(1, 2))
    assert isinstance(registry, TypeRegistry)


def test_a_fanout_cache_takes_a_registry(directory: Path) -> None:
    reg = TypeRegistry()
    reg.register(Point, name='t.Point', encode=point_encode, decode=point_decode)
    with FanoutCache(directory, shards=2, types=reg) as cache:
        cache.set('p', Point(1, 2))
        cache.set('q', {'points': [Point(3, 4)]})
        assert cache.get('p') == Point(1, 2)
        assert cache.get('q') == {'points': [Point(3, 4)]}
        assert cache.pop('p') == Point(1, 2)
    with FanoutCache(directory, shards=2) as plain, pytest.raises(ForeignValueError):
        plain.get('q')


def test_the_constructor_checks_the_registry_type(directory: Path) -> None:
    with pytest.raises(TypeError):
        Cache(directory, types={})  # ty: ignore[invalid-argument-type]
    with pytest.raises(TypeError):
        FanoutCache(directory, types=[])  # ty: ignore[invalid-argument-type]
    NullCache(types=TypeRegistry())


def test_caches_forwards_the_registry(tmp_path: Path) -> None:
    reg = TypeRegistry()
    reg.register(Point, name='t.Point', encode=point_encode, decode=point_decode)
    caches = Caches(tmp_path)
    caches.register('a', types=reg)
    caches.register('b', shards=2, types=reg)
    try:
        caches['a'].set('p', Point(1, 2))
        assert caches['a'].get('p') == Point(1, 2)
        caches.fanout('b').set('p', Point(3, 4))
        assert caches.fanout('b').get('p') == Point(3, 4)
    finally:
        caches.close_all()


# -- never pickled --------------------------------------------------------------------------------


class Unstorable:
    """Nothing the codec can represent; picklable."""


def test_a_registered_type_is_never_pickled(directory: Path) -> None:
    @dataclasses.dataclass
    class Holder:
        inner: Any

    register_type(Holder, name='t.Holder')
    register_type(Boom, name='t.Boom', encode=lambda _: Unstorable(), decode=lambda _: Boom())

    class Raising:
        pass

    def raise_type_error(_: Raising) -> int:
        raise TypeError('from encode')

    register_type(Raising, name='t.Raising', encode=raise_type_error, decode=lambda _: Raising())

    class Recursive:
        pass

    register_type(Recursive, name='t.Recursive', encode=lambda o: o, decode=lambda _: Recursive())

    with Cache(directory, pickle=True) as cache:
        cache.set('plain', Unstorable())  # the fallback still works for a value that reached no registration
        assert _mode(directory, 'plain') == PICKLE
        with pytest.raises(TypeError, match='from encode'):
            cache.set('raising', Raising())
        with pytest.raises(TypeError, match='nested too deeply'):
            cache.set('recursive', Recursive())
        with pytest.raises(TypeError, match="can't be stored without pickle"):
            cache.set('field', Holder(Unstorable()))
        with pytest.raises(TypeError, match="can't be stored without pickle"):
            cache.set('body', Boom())
        # A value that holds a registered type beside an unstorable one is not pickled either.
        with pytest.raises(TypeError):
            cache.set('mixed', [Holder(1), Unstorable()])
        for key in ('raising', 'recursive', 'field', 'body', 'mixed'):
            assert key not in cache


def test_an_encode_error_leaves_nothing_written(directory: Path) -> None:
    class Flaky:
        pass

    def encode(_: Flaky) -> int:
        raise RuntimeError('no')

    register_type(Flaky, name='t.Flaky', encode=encode, decode=lambda _: Flaky())
    with Cache(directory) as cache:
        cache.set('k', 'old')
        with pytest.raises(RuntimeError, match='no'):
            cache.set('k', Flaky())
        assert cache.get('k') == 'old'


# -- decode errors --------------------------------------------------------------------------------


def _write_boom(directory: Path, serializer: str = 'msgpack') -> None:
    register_type(Boom, name='t.Boom', encode=Boom.encode, decode=Boom.decode)
    with Cache(directory, serializer=serializer) as writer:
        writer.set('k', Boom())


@pytest.mark.parametrize('serializer', SERIALIZERS)
def test_an_error_about_the_row_is_corrupt_with_the_cause_kept(directory: Path, serializer: str) -> None:
    _write_boom(directory, serializer)
    with Cache(directory, serializer=serializer) as cache:
        with pytest.raises(
            CorruptValueError, match='registered type "\\$t:t.Boom" failed: ValueError: bad body'
        ) as info:
            cache.get('k')
        assert isinstance(info.value.__cause__, ValueError)
        assert _stored(directory, 'k'), "under 'raise' the row stays"
    with Cache(directory, serializer=serializer, on_decode_error='miss') as healing:
        assert healing.get('k', 'default') == 'default'
        assert not _stored(directory, 'k'), 'no build can read it, so it goes'


def test_an_old_shape_dataclass_row_is_corrupt(directory: Path) -> None:
    @dataclasses.dataclass
    class V1:
        a: int
        b: int = 0

    register_type(V1, name='t.V')
    with Cache(directory) as writer:
        writer.set('k', V1(1, 2))

    @dataclasses.dataclass
    class Removed:  # `b` removed: the stored row has an unexpected field.
        a: int

    @dataclasses.dataclass
    class Required:  # `c` added without a default: the stored row lacks it.
        a: int
        b: int
        c: int

    @dataclasses.dataclass
    class Defaulted:  # a field added with a default reads the old row fine.
        a: int
        b: int
        c: int = 9

    for cls in (Removed, Required):
        register_type(cls, name='t.V')
        with Cache(directory, on_decode_error='raise') as cache, pytest.raises(CorruptValueError) as info:
            cache.get('k')
        assert isinstance(info.value.__cause__, TypeError)
        assert _stored(directory, 'k')
    register_type(Defaulted, name='t.V')
    with Cache(directory) as cache:
        assert cache.get('k') == Defaulted(1, 2, 9)
    for cls in (Removed, Required):
        register_type(V1, name='t.V')
        with Cache(directory) as writer:
            writer.set('k', V1(1, 2))
        register_type(cls, name='t.V')
        with Cache(directory, on_decode_error='miss') as healing:
            assert healing.get('k') is None
            assert not _stored(directory, 'k')


@pytest.mark.parametrize(
    'error',
    [Timeout('nested'), OSError('disk'), RecursionError(), MemoryError(), UnsafeValueError('x'), KeyboardInterrupt()],
    ids=lambda e: type(e).__name__,
)
@pytest.mark.parametrize('serializer', SERIALIZERS)
def test_errors_that_arent_about_the_row_still_raise(directory: Path, serializer: str, error: BaseException) -> None:
    _write_boom(directory, serializer)
    Boom.error = error
    with Cache(directory, serializer=serializer, on_decode_error='miss') as cache:
        with pytest.raises(type(error)):
            cache.get('k')
        with pytest.raises(type(error)):
            cache.pop('k')
    assert _stored(directory, 'k')


@pytest.mark.parametrize(
    ('error', 'raises'),
    [
        (Timeout('nested'), False),
        (OSError('disk'), False),
        (RecursionError(), False),
        (MemoryError(), True),
        (UnsafeValueError('x'), True),
    ],
    ids=lambda v: type(v).__name__ if isinstance(v, BaseException) else str(v),
)
def test_on_a_durable_handle_an_outage_in_decode_is_a_miss(directory: Path, error: BaseException, raises: bool) -> None:
    _write_boom(directory)
    Boom.error = error
    with Cache(directory, durable=True) as cache:
        if raises:
            with pytest.raises(type(error)):
                cache.get('k')
        else:
            assert cache.get('k', 'default') == 'default'
    assert _stored(directory, 'k')


@pytest.mark.parametrize(('nested', 'kept'), [('corrupt', False), ('foreign', True)])
def test_a_decode_error_from_a_nested_read_classifies_the_outer_value(directory: Path, nested: str, kept: bool) -> None:
    _write_boom(directory)
    with Cache(directory) as writer:
        writer.set('inner', {'a': 1})
    mode = "value = X'c1'" if nested == 'corrupt' else 'mode = 99'
    _sql(directory, f"UPDATE cache SET {mode} WHERE key = 'inner'")
    strict = Cache(directory)
    Boom.hook = lambda: strict.get('inner')
    with Cache(directory, on_decode_error='miss') as cache:
        assert cache.get('k') is None
    assert _stored(directory, 'k') is kept
    strict.close()


def test_a_decode_function_may_use_the_cache(directory: Path) -> None:
    """Re-entering the cache from a decode function is as safe as from a pickle hook."""

    class Ref:
        def __init__(self, key: str) -> None:
            self.key = key

    cache = Cache(directory)
    register_type(Ref, name='t.Ref', encode=lambda r: r.key, decode=lambda k: cache.get(k))
    cache.set('target', {'x': 1})
    cache.set('ref', Ref('target'))
    assert cache.get('ref') == {'x': 1}
    cache.close()


@pytest.mark.parametrize('serializer', SERIALIZERS)
def test_an_unregistered_tag_is_foreign_and_kept(directory: Path, serializer: str) -> None:
    reg = TypeRegistry()
    reg.register(Point, name='w.Point', encode=point_encode, decode=point_decode)
    with Cache(directory, serializer=serializer, types=reg) as writer:
        writer.set('k', Point(1, 2))
    with Cache(directory, serializer=serializer) as cache:
        with pytest.raises(ForeignValueError, match='unknown tag "\\$t:w.Point"'):
            cache.get('k')
        with pytest.raises(ForeignValueError):
            cache.pop('k')
    with Cache(directory, serializer=serializer, on_decode_error='miss') as healing:
        assert healing.get('k') is None
        assert healing.pop('k', 'default') == 'default'
    assert _stored(directory, 'k'), 'another process may have it registered'
    with Cache(directory, serializer=serializer, types=reg) as reader:
        assert reader.get('k') == Point(1, 2)


# -- user code inside the encoder -----------------------------------------------------------------


def test_a_dict_grown_by_another_thread_during_encode_is_stored_consistently(directory: Path) -> None:
    """With nothing registered the GIL build iterates a dict in place; a registered encode function
    can release the GIL mid-dict, so the entries are copied first. Without the copy this panics."""

    class Slow:
        pass

    def encode(_: Slow) -> int:
        time.sleep(0.01)
        return 1

    register_type(Slow, name='t.Slow', encode=encode, decode=lambda _: Slow())
    stop = threading.Event()
    value: dict[str, Any] = {'slow': Slow()}

    def grow() -> None:
        # Resizes constantly but stays bounded: on the free-threaded build the grower runs in
        # parallel with the encoder, and an unbounded dict would outgrow the cache's size_limit.
        while not stop.is_set():
            for n in range(2000):
                value[f'k{n}'] = n
            for n in range(2000):
                del value[f'k{n}']

    grower = threading.Thread(target=grow)
    grower.start()
    try:
        with Cache(directory) as cache:
            for i in range(20):
                assert cache.set(f'v{i}', value) is True
                back = cache.get(f'v{i}')
                assert isinstance(back, dict) and isinstance(back['slow'], Slow)
    finally:
        stop.set()
        grower.join()


def test_registering_while_threads_encode() -> None:
    reg = TypeRegistry()
    stop = threading.Event()
    errors: list[BaseException] = []

    def churn() -> None:
        n = 0
        while not stop.is_set():
            reg.register(Point, name=f't.Point{n % 3}', encode=point_encode, decode=point_decode)
            if n % 7 == 0:
                reg.unregister(Point)
            n += 1

    def encode() -> None:
        try:
            while not stop.is_set():
                try:
                    mode, payload = _encode({'p': Point(1, 2), 'list': [Point(3, 4)] * 4}, types=reg)
                except TypeError:
                    continue  # unregistered at that instant
                try:
                    assert _decode(mode, payload, types=reg) == {'p': Point(1, 2), 'list': [Point(3, 4)] * 4}
                except ForeignValueError:
                    pass  # the name moved between the write and the read
        except BaseException as ex:
            errors.append(ex)

    threads = [threading.Thread(target=encode) for _ in range(16)]
    churner = threading.Thread(target=churn)
    for t in (*threads, churner):
        t.start()
    time.sleep(0.5)
    stop.set()
    for t in (*threads, churner):
        t.join()
    assert not errors


# -- garbage collection ---------------------------------------------------------------------------


class Holder:
    """An encode function bound to an object that refers back to the cache (the compiled classes
    take no weak references, so the weakref watches this)."""

    cache: Any = None

    def encode(self, p: Point) -> tuple[int, int]:
        return point_encode(p)


def test_a_per_handle_registry_whose_closure_refers_to_the_cache_is_collected(directory: Path) -> None:
    reg = TypeRegistry()
    holder = Holder()
    holder.cache = Cache(directory, types=reg)
    reg.register(Point, name='t.Point', encode=holder.encode, decode=point_decode)
    gone = weakref.ref(holder)
    del reg, holder
    gc.collect()
    assert gone() is None, 'the cache, its registry and the closure formed a cycle the collector could not see'


def test_a_global_registration_keeps_its_closure_until_unregistered(directory: Path) -> None:
    holder = Holder()
    holder.cache = Cache(directory)
    register_type(Point, name='t.Point', encode=holder.encode, decode=point_decode)
    gone = weakref.ref(holder)
    del holder
    gc.collect()
    assert gone() is not None, 'the global registry holds the function, and the function the cache'
    unregister_type(Point)
    gc.collect()
    assert gone() is None


# -- the docs' claims -----------------------------------------------------------------------------


def test_the_module_exports_the_registry_api() -> None:
    assert diskcache_oxide.registry is registry
    for name in ('register_type', 'unregister_type', 'registered_types', 'registry', 'TypeRegistry'):
        assert name in diskcache_oxide.__all__
