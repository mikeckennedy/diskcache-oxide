"""`DjangoCache`, the Django cache backend: settings, the shared handle table, keys, timeouts, and the
calls Django's own suite (scripts/django-suite.sh) checks only through Django's API.

Runs against the installed Django (the `django` dependency group, in all four environments), and is
skipped where Django isn't installed. Forks run in a fresh process, never in pytest itself.
"""

from __future__ import annotations

import asyncio
import enum
import os
import subprocess
import sys
import textwrap
import threading
import time
import warnings
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

django = pytest.importorskip('django')

from django.conf import settings  # noqa: E402

if not settings.configured:
    settings.configure(CACHES={'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}}, USE_TZ=True)

import diskcache_oxide  # noqa: E402
from diskcache_oxide import Cache, FanoutCache, djangocache  # noqa: E402
from diskcache_oxide.djangocache import DjangoCache  # noqa: E402
from django.core.cache import CacheKeyWarning, caches  # noqa: E402
from django.core.cache.backends.base import DEFAULT_TIMEOUT, memcache_key_warnings  # noqa: E402
from django.core.exceptions import ImproperlyConfigured  # noqa: E402
from django.test import override_settings  # noqa: E402
from django.utils.module_loading import import_string  # noqa: E402
from django.utils.safestring import SafeString, mark_safe  # noqa: E402

BACKEND = 'diskcache_oxide.DjangoCache'


@pytest.fixture(autouse=True)
def release_handles():
    yield
    djangocache.close_all()


def make(location: Any, **params: Any) -> DjangoCache:
    return DjangoCache(location, params)


def open_fds() -> int:
    return len(os.listdir('/dev/fd'))


def run_python(code: str) -> str:
    result = subprocess.run(
        [sys.executable, '-c', textwrap.dedent(code)], capture_output=True, text=True, timeout=60, check=False
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


# The lazy name.


def test_the_package_name_is_the_backend() -> None:
    assert diskcache_oxide.DjangoCache is DjangoCache
    assert import_string(BACKEND) is DjangoCache
    assert 'DjangoCache' not in diskcache_oxide.__all__


def test_mock_patch_works_through_the_lazy_name(tmp_path: Path) -> None:
    from unittest import mock

    with mock.patch('diskcache_oxide.DjangoCache.add', return_value='patched'):
        assert make(tmp_path).add('k', 'v') == 'patched'


def test_importing_the_package_does_not_import_django() -> None:
    out = run_python("""
        import sys
        import diskcache_oxide
        from diskcache_oxide import *
        print('django' in sys.modules, 'diskcache_oxide.djangocache' in sys.modules)
    """)
    assert out.split() == ['False', 'False']


def test_without_django_the_name_is_missing_with_a_hint() -> None:
    out = run_python("""
        import sys
        sys.modules['django'] = None
        import diskcache_oxide
        from diskcache_oxide import *
        print(hasattr(diskcache_oxide, 'DjangoCache'))
        try:
            diskcache_oxide.DjangoCache
        except AttributeError as error:
            print(error)
    """)
    first, hint = out.splitlines()
    assert first == 'False'
    assert "pip install 'diskcache-oxide[django]'" in hint


def test_a_broken_django_is_reported_as_itself(tmp_path: Path) -> None:
    fake = tmp_path / 'django'
    fake.mkdir()
    (fake / '__init__.py').write_text('import a_module_django_needs_but_lacks\n')
    out = run_python(f"""
        import sys
        sys.path.insert(0, {str(tmp_path)!r})
        import diskcache_oxide
        try:
            diskcache_oxide.DjangoCache
        except ModuleNotFoundError as error:
            print(error.name)
    """)
    assert out.strip() == 'a_module_django_needs_but_lacks'


def test_other_missing_names_are_attribute_errors() -> None:
    with pytest.raises(AttributeError, match='no attribute'):
        diskcache_oxide.NotAThing  # noqa: B018


# Settings.


def test_defaults_are_the_backends_own(tmp_path: Path) -> None:
    backend = make(tmp_path)
    assert backend.options['timeout'] == 1.0
    assert backend.options['pickle'] is True
    handle = backend.cache
    assert isinstance(handle, Cache)
    assert handle.timeout == 1.0
    assert backend.default_timeout == 300  # Django's own TIMEOUT default


def test_options_reach_the_cache(tmp_path: Path) -> None:
    backend = make(tmp_path, OPTIONS={'size_limit': 2**21, 'timeout': 2.5, 'cull_limit': 3})
    assert backend.cache.size_limit == 2**21
    assert backend.cache.timeout == 2.5
    assert backend.cache.cull_limit == 3


def test_django_settings_work(tmp_path: Path) -> None:
    backend = make(tmp_path, TIMEOUT=60, KEY_PREFIX='site', VERSION=3)
    backend.set('k', 'v')
    assert backend.cache.get('site:3:k') == 'v'
    _, expires = backend.cache.get('site:3:k', expire_time=True)
    assert expires is not None


@pytest.mark.parametrize('location', ['', None])
def test_location_is_required(location: Any) -> None:
    with pytest.raises(ImproperlyConfigured, match='LOCATION'):
        make(location)


def test_location_must_be_a_path() -> None:
    with pytest.raises(ImproperlyConfigured, match='LOCATION must be a str or a path'):
        make(42)


def test_location_can_be_a_path_or_relative(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert make(tmp_path / 'a').directory == str(tmp_path / 'a')
    monkeypatch.chdir(tmp_path)
    assert make('b').directory == str(tmp_path / 'b')


def test_an_unknown_option_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ImproperlyConfigured, match='unknown DjangoCache options: directory, size_limt'):
        make(tmp_path, OPTIONS={'size_limt': 1, 'directory': 'x'})


def test_a_fanout_option_needs_shards(tmp_path: Path) -> None:
    with pytest.raises(ImproperlyConfigured, match='raise_on_mismatch'):
        make(tmp_path, OPTIONS={'raise_on_mismatch': True})


@pytest.mark.parametrize(
    ('params', 'match'),
    [
        ({'OPTIONS': {'MAX_ENTRIES': 10}}, r"OPTIONS\['MAX_ENTRIES'\].*size_limit"),
        ({'OPTIONS': {'CULL_FREQUENCY': 2}}, r"OPTIONS\['CULL_FREQUENCY'\].*cull_limit"),
        ({'OPTIONS': {'clock': lambda: 0.0}}, r"OPTIONS\['clock'\].*maintenance"),
        ({'max_entries': 10}, 'max_entries.*size_limit'),
        ({'MAX_ENTRIES': 10}, 'MAX_ENTRIES.*size_limit'),
        ({'CULL_FREQUENCY': 2}, 'CULL_FREQUENCY.*cull_limit'),
        ({'cull_frequency': 2}, 'cull_frequency.*cull_limit'),
        ({'SHARDS': 8}, r"SHARDS.*OPTIONS\['shards'\]"),
        ({'DATABASE_TIMEOUT': 0.01}, r"DATABASE_TIMEOUT.*OPTIONS\['timeout'\]"),
    ],
)
def test_refused_settings(tmp_path: Path, params: dict[str, Any], match: str) -> None:
    with pytest.raises(ImproperlyConfigured, match=match):
        make(tmp_path, **params)


def test_aliases_on_one_directory_must_agree(tmp_path: Path) -> None:
    make(tmp_path)
    make(tmp_path, OPTIONS={'timeout': 1.0, 'pickle': True})  # the defaults, spelled out
    make(tmp_path, KEY_PREFIX='other', VERSION=2, TIMEOUT=5)  # Django's settings, not cache options
    with pytest.raises(ImproperlyConfigured, match='different OPTIONS'):
        make(tmp_path, OPTIONS={'size_limit': 2**21})
    with pytest.raises(ImproperlyConfigured, match='different OPTIONS'):
        make(tmp_path, OPTIONS={'timeout': 2.0})


def test_shards_makes_a_fanout_cache(tmp_path: Path) -> None:
    backend = make(tmp_path / 'f', OPTIONS={'shards': 4, 'raise_on_mismatch': True})
    assert isinstance(backend.cache, FanoutCache)
    assert backend.cache.timeout == 1.0
    backend.set('k', 'v')
    assert backend.get('k') == 'v'
    with pytest.raises(ImproperlyConfigured, match='different OPTIONS'):
        make(tmp_path / 'f')  # a plain alias on the same directory


def test_a_none_lock_wait_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ImproperlyConfigured, match="OPTIONS\\['timeout'\\]"):
        make(tmp_path, OPTIONS={'timeout': None})


def test_an_instance_from_before_close_all_cannot_reopen_with_old_options(tmp_path: Path) -> None:
    old = make(tmp_path, OPTIONS={'timeout': 3.0})
    djangocache.close_all()
    new = make(tmp_path, OPTIONS={'timeout': 7.0})
    with pytest.raises(ImproperlyConfigured, match='before close_all'):
        old.get('k')
    assert new.cache.timeout == 7.0


def test_an_instance_from_before_close_all_that_opens_first_is_refused_later(tmp_path: Path) -> None:
    old = make(tmp_path, OPTIONS={'timeout': 3.0})
    djangocache.close_all()
    old.set('k', 1)  # reopens, and its options become the directory's again
    with pytest.raises(ImproperlyConfigured, match='different OPTIONS'):
        make(tmp_path, OPTIONS={'timeout': 7.0})


def test_close_all_forgets_the_options(tmp_path: Path) -> None:
    make(tmp_path)
    djangocache.close_all()
    assert make(tmp_path, OPTIONS={'timeout': 2.0}).cache.timeout == 2.0


def test_pickle_is_on_by_default(tmp_path: Path) -> None:
    backend = make(tmp_path)
    backend.set('safe', mark_safe('<b>hi</b>'))
    assert type(backend.get('safe')) is SafeString


def test_a_session_holding_a_lone_surrogate_saves(tmp_path: Path) -> None:
    # What the cache session engine stores: a dict, here with a lone surrogate from a form field.
    # msgpack can't hold it, so under pickle (the default) it's pickled, as diskcache's backend does.
    backend = make(tmp_path)
    session = {'_auth_user_id': '1', 'query': 'caf\udce9'}
    backend.set('django.contrib.sessions.cache1', session)
    assert backend.get('django.contrib.sessions.cache1') == session
    off = make(tmp_path / 'off', OPTIONS={'pickle': False})
    with pytest.raises(UnicodeEncodeError):
        off.set('s', session)


def test_pickle_off_stores_plain_types_only(tmp_path: Path) -> None:
    backend = make(tmp_path, OPTIONS={'pickle': False})
    backend.set('plain', {'a': [1, 2.5, 'x']})
    assert backend.get('plain') == {'a': [1, 2.5, 'x']}
    with pytest.raises(TypeError):
        backend.set('safe', mark_safe('<b>hi</b>'))


# The handle table.


def test_init_opens_nothing(tmp_path: Path) -> None:
    location = tmp_path / 'new'
    make(location)
    assert not location.exists()
    assert str(location) not in djangocache.handles


def test_instances_and_aliases_share_one_handle(tmp_path: Path) -> None:
    first = make(tmp_path)
    second = make(tmp_path, KEY_PREFIX='p')
    assert first.cache is second.cache is djangocache.handles[str(tmp_path)]
    first.set('k', 1)
    assert second.get('k') is None  # another prefix
    assert make(tmp_path).get('k') == 1


@pytest.mark.skipif(sys.platform == 'win32', reason='counts descriptors in /dev/fd')
def test_racing_first_opens_keep_one_handle(tmp_path: Path) -> None:
    baseline = open_fds()
    threads = 16
    barrier = threading.Barrier(threads)
    seen: list[Any] = [None] * threads

    def worker(i: int) -> None:
        backend = make(tmp_path)
        barrier.wait()
        seen[i] = backend.cache
        backend.set(f'k{i}', i)

    pool = [threading.Thread(target=worker, args=(i,)) for i in range(threads)]
    for thread in pool:
        thread.start()
    for thread in pool:
        thread.join()
    assert all(handle is djangocache.handles[str(tmp_path)] for handle in seen)
    assert len(djangocache.handles) == 1
    assert make(tmp_path).get_many([f'k{i}' for i in range(threads)]) == {f'k{i}': i for i in range(threads)}
    djangocache.close_all()
    assert open_fds() == baseline  # no handle outlives the table, the race's losers included


@pytest.mark.skipif(sys.platform == 'win32', reason='counts descriptors in /dev/fd')
def test_close_does_nothing_and_close_all_releases_descriptors(tmp_path: Path) -> None:
    baseline = open_fds()
    backend = make(tmp_path)
    backend.set('k', 1)
    handle = backend.cache
    opened = open_fds()
    assert opened > baseline
    backend.close()
    assert djangocache.handles[str(tmp_path)] is handle
    assert open_fds() == opened
    djangocache.close_all()
    assert djangocache.handles == {}
    assert open_fds() == baseline
    assert backend.get('k') == 1  # reopened on the next call
    assert backend.cache is not handle


def test_changing_caches_closes_the_handles(tmp_path: Path) -> None:
    make(tmp_path).set('k', 1)
    assert djangocache.handles
    with override_settings(CACHES={'default': {'BACKEND': BACKEND, 'LOCATION': str(tmp_path / 'other')}}):
        assert djangocache.handles == {}
        caches['default'].set('k', 2)
        assert list(djangocache.handles) == [str(tmp_path / 'other')]
    assert djangocache.handles == {}


def test_other_setting_changes_leave_the_handles(tmp_path: Path) -> None:
    make(tmp_path).set('k', 1)
    with override_settings(USE_I18N=False):
        assert list(djangocache.handles) == [str(tmp_path)]


def test_one_instance_from_an_event_loop_and_a_thread_at_once(tmp_path: Path) -> None:
    # Under ASGI, if anything touched the cache in the loop thread before serving, every request shares
    # that thread's instance, used from the loop and from sync_to_async's thread at once.
    backend = make(tmp_path)
    backend.set('n', 0)
    rounds = 500

    def thread_side() -> None:
        for i in range(rounds):
            backend.incr('n')
            backend.set(f't{i}', i)
            assert backend.get(f't{i}') == i

    async def loop_side() -> None:
        for i in range(0, rounds, 50):
            await asyncio.gather(*(backend.aincr('n') for _ in range(50)))
            await backend.aset(f'a{i}', i)
            assert await backend.aget(f'a{i}') == i

    thread = threading.Thread(target=thread_side)
    thread.start()
    asyncio.run(loop_side())
    thread.join()
    assert backend.get('n') == 2 * rounds


def test_a_forked_child_uses_the_inherited_table(tmp_path: Path) -> None:
    # gunicorn with preload_app: the table is warm before the fork.
    out = run_python(f"""
        import os
        from django.conf import settings
        settings.configure()
        from diskcache_oxide import djangocache
        from diskcache_oxide.djangocache import DjangoCache

        backend = DjangoCache({str(tmp_path)!r}, {{}})
        backend.set('parent', 1)
        handle = backend.cache
        pid = os.fork()
        if pid == 0:
            code = 1
            try:
                child = DjangoCache({str(tmp_path)!r}, {{}})
                assert child.cache is handle
                assert child.get('parent') == 1
                child.set('child', 2)
                assert child.incr('parent') == 2
                code = 0
            finally:
                os._exit(code)
        _, status = os.waitpid(pid, 0)
        assert os.waitstatus_to_exitcode(status) == 0, status
        assert backend.get('child') == 2 and backend.get('parent') == 2
        print('ok')
    """)
    assert out.strip() == 'ok'


# Keys.


def test_a_str_subclass_key_is_stored_as_a_str(tmp_path: Path) -> None:
    backend = make(tmp_path, KEY_FUNCTION=lambda key, prefix, version: mark_safe(f'{prefix}:{version}:{key}'))
    made = backend.make_and_validate_key('k')
    assert type(made) is str
    assert made == ':1:k'
    backend.set('k', 'v')
    assert backend.get('k') == 'v'


def test_a_key_function_must_return_a_str(tmp_path: Path) -> None:
    backend = make(tmp_path, KEY_FUNCTION=lambda key, prefix, version: key.encode())
    with pytest.raises(TypeError, match='KEY_FUNCTION returned bytes'):
        backend.get('k')


BOUNDARY_KEYS = [
    '',
    'a' * 250,
    'a' * 251,
    'é' * 250,
    'é' * 251,
    *(f'ab{chr(c)}cd' for c in [*range(0x21), 0x7F]),
    'ab\x80cd',
    'ab\xa0cd',
    'ab　cd',
    'ab﻿cd',
    'ab\U0001f600cd',
]


def warns_for(backend: DjangoCache, key: str) -> list[str]:
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter('always')
        backend.validate_key(key)
    assert all(issubclass(w.category, CacheKeyWarning) for w in caught)
    return [str(w.message) for w in caught]


@pytest.mark.parametrize('key', BOUNDARY_KEYS, ids=repr)
def test_the_key_fast_path_agrees_with_django(tmp_path: Path, key: str) -> None:
    assert warns_for(make(tmp_path), key) == list(memcache_key_warnings(key))


@given(st.text(max_size=300))
def test_the_key_fast_path_agrees_with_django_on_any_text(key: str) -> None:
    backend = DjangoCache('/nonexistent-never-opened', {})
    assert warns_for(backend, key) == list(memcache_key_warnings(key))


# Timeouts.


class Seconds(enum.IntEnum):
    TEN = 10


@pytest.mark.parametrize(
    ('timeout', 'expected'),
    [(DEFAULT_TIMEOUT, 300), (None, None), (0, 0), (-1, -1), (1.5, 1.5), (60, 60), (Seconds.TEN, 10.0)],
)
def test_backend_timeouts(tmp_path: Path, timeout: Any, expected: Any) -> None:
    assert make(tmp_path).get_backend_timeout(timeout) == expected


def test_the_alias_timeout_is_the_default(tmp_path: Path) -> None:
    assert make(tmp_path, TIMEOUT=None).get_backend_timeout() is None
    assert make(tmp_path, TIMEOUT='7').get_backend_timeout() == 7  # Django coerces the alias setting


@pytest.mark.parametrize('timeout', [Decimal('1.5'), '10', True, [1]])
def test_odd_timeouts_raise_type_error(tmp_path: Path, timeout: Any) -> None:
    backend = make(tmp_path)
    with pytest.raises(TypeError, match='number of seconds'):
        backend.set('k', 'v', timeout=timeout)


def test_zero_or_negative_timeouts_store_nothing(tmp_path: Path) -> None:
    backend = make(tmp_path)
    backend.set('zero', 1, timeout=0)
    assert backend.add('negative', 1, timeout=-5) is True  # as locmem: added, already expired
    assert not backend.has_key('zero')
    assert not backend.has_key('negative')
    backend.set('k', 1)
    assert backend.touch('k', 0)
    assert backend.get('k') is None


def test_none_is_forever(tmp_path: Path) -> None:
    backend = make(tmp_path, TIMEOUT=1)
    backend.set('k', 1, timeout=None)
    assert backend.cache.get(':1:k', expire_time=True) == (1, None)


# Single-key calls.


def test_single_key_calls(tmp_path: Path) -> None:
    backend = make(tmp_path)
    assert backend.get('k', 'missing') == 'missing'
    assert backend.add('k', 1)
    assert not backend.add('k', 2)
    assert backend.get('k') == 1
    backend.set('none', None)
    assert backend.get('none', 'missing') is None
    assert backend.has_key('none')
    assert backend.touch('k', 100)
    assert not backend.touch('missing', 100)
    assert backend.delete('k')
    assert not backend.delete('k')


def test_incr_and_decr(tmp_path: Path) -> None:
    backend = make(tmp_path)
    backend.set('n', 10, timeout=1000)
    _, expires = backend.cache.get(':1:n', expire_time=True)
    assert backend.incr('n') == 11
    assert backend.incr('n', 5) == 16
    assert backend.decr('n', 6) == 10
    assert backend.cache.get(':1:n', expire_time=True) == (10, expires)  # the expiry is kept
    with pytest.raises(ValueError, match="Key ':1:missing' not found"):
        backend.incr('missing')
    with pytest.raises(ValueError):
        backend.decr('missing')
    backend.set('s', 'text')
    with pytest.raises(TypeError):
        backend.incr('s')
    backend.set('flag', True)
    with pytest.raises(TypeError):
        backend.incr('flag')
    backend.set('f', 1.5)
    assert backend.incr('f') == 2.5  # a float is a number
    assert backend.incr('n', 1.5) == 11.5


def test_clear_clears_every_alias_on_the_directory(tmp_path: Path) -> None:
    first = make(tmp_path)
    second = make(tmp_path, KEY_PREFIX='other')
    first.set('k', 1)
    second.set('k', 2)
    first.clear()
    assert first.get('k') is None
    assert second.get('k') is None


def test_async_calls(tmp_path: Path) -> None:
    backend = make(tmp_path)

    async def main() -> None:
        assert await backend.aadd('k', 1)
        assert await backend.aget('k') == 1
        await backend.aset('k', 2)
        assert await backend.ahas_key('k')
        assert await backend.atouch('k', 100)
        assert await backend.aincr('k', 3) == 5
        assert await backend.adecr('k') == 4
        await backend.aset_many({'a': 1, 'b': 2})
        assert await backend.aget_many(['a', 'b', 'c']) == {'a': 1, 'b': 2}
        await backend.adelete_many(['a'])
        assert await backend.adelete('k')
        assert await backend.aget_or_set('g', 'made') == 'made'
        await backend.aclear()
        assert await backend.aget('b') is None
        assert await backend.aclose() is None

    asyncio.run(main())


# Several keys.


def test_get_many_returns_what_is_there(tmp_path: Path) -> None:
    backend = make(tmp_path)
    backend.set('a', 1)
    backend.set('none', None)
    assert backend.get_many(['a', 'none', 'missing']) == {'a': 1, 'none': None}
    assert backend.get_many([]) == {}


def test_set_many_returns_no_failures(tmp_path: Path) -> None:
    backend = make(tmp_path)
    assert backend.set_many({'a': 1, 'b': [2]}, timeout=100) == []
    assert backend.get_many(['a', 'b']) == {'a': 1, 'b': [2]}
    _, expires = backend.cache.get(':1:a', expire_time=True)
    assert expires is not None


def test_set_many_with_timeout_zero_stores_nothing(tmp_path: Path) -> None:
    backend = make(tmp_path)
    backend.set_many({'a': 1}, timeout=0)
    assert backend.get_many(['a']) == {}


@pytest.mark.parametrize('options', [{}, {'shards': 8}, {'durable': True}], ids=['cache', 'fanout', 'durable'])
@pytest.mark.parametrize('count', [0, 1, 2, 99, 100, 101, 350])
def test_delete_many(tmp_path: Path, options: dict[str, Any], count: int) -> None:
    backend = make(tmp_path, OPTIONS=options)
    keys = [f'k{i}' for i in range(count)]
    backend.set_many({key: key for key in keys})
    backend.set('kept', 1)
    backend.delete_many([*keys, 'never-set'])
    assert backend.get_many(keys) == {}
    assert backend.get('kept') == 1


def test_delete_many_groups_keys_by_shard(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Small chunks, so a group spans several transactions and a wrong grouping would raise ValueError
    # (a key on another shard inside a shard's transaction).
    monkeypatch.setattr(djangocache, 'DELETE_CHUNK', 3)
    backend = make(tmp_path, OPTIONS={'shards': 8})
    keys = [f'k{i}' for i in range(200)]
    backend.set_many({key: 1 for key in keys})
    fanout = backend.cache
    assert isinstance(fanout, FanoutCache)
    assert len({fanout.shard_of(backend.make_and_validate_key(key)) for key in keys}) == 8
    backend.delete_many(keys)
    assert backend.get_many(keys) == {}


def test_delete_many_validates_every_key_before_deleting(tmp_path: Path) -> None:
    backend = make(tmp_path)
    backend.set_many({'a': 1, 'b': 2})
    with warnings.catch_warnings():
        warnings.simplefilter('error', CacheKeyWarning)
        with pytest.raises(CacheKeyWarning):
            backend.delete_many(['a', 'b', 'bad key'])
    assert backend.get_many(['a', 'b']) == {'a': 1, 'b': 2}


def test_delete_many_joins_the_callers_transaction(tmp_path: Path) -> None:
    backend = make(tmp_path)
    backend.set_many({'a': 1, 'b': 2})
    cache = backend.cache
    assert isinstance(cache, Cache)
    with pytest.raises(RuntimeError, match='undo'), cache.transact():
        backend.delete_many(['a', 'b'])
        raise RuntimeError('undo')
    assert backend.get_many(['a', 'b']) == {'a': 1, 'b': 2}


@pytest.mark.parametrize('options', [{}, {'shards': 8}, {'durable': True}], ids=['cache', 'fanout', 'durable'])
def test_delete_many_validates_before_any_chunk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, options: dict[str, Any]
) -> None:
    monkeypatch.setattr(djangocache, 'DELETE_CHUNK', 2)
    backend = make(tmp_path, OPTIONS=options)
    keys = [f'k{i}' for i in range(20)]
    backend.set_many({key: 1 for key in keys})
    with warnings.catch_warnings():
        warnings.simplefilter('error', CacheKeyWarning)
        with pytest.raises(CacheKeyWarning):
            backend.delete_many([*keys, 'bad key'])
    assert len(backend.get_many(keys)) == 20


def test_delete_many_passes_version(tmp_path: Path) -> None:
    backend = make(tmp_path)
    backend.set_many({'a': 1, 'b': 2})
    backend.set_many({'a': 3, 'b': 4}, version=2)
    backend.delete_many(['a', 'b'], version=2)
    assert backend.get_many(['a', 'b']) == {'a': 1, 'b': 2}
    assert backend.get_many(['a', 'b'], version=2) == {}


def held_lock(backend):
    locked, release = threading.Event(), threading.Event()

    def hold() -> None:
        with backend.cache.transact():
            locked.set()
            release.wait()

    thread = threading.Thread(target=hold)
    thread.start()
    locked.wait()
    return release, thread


def test_delete_many_on_a_durable_alias_uses_single_deletes(tmp_path: Path) -> None:
    backend = make(tmp_path, OPTIONS={'durable': True, 'timeout': 0.05})
    backend.set_many({'a': 1, 'b': 2})
    release, thread = held_lock(backend)
    try:
        assert backend.delete_many(['a', 'b']) is None  # a transaction would raise Timeout
    finally:
        release.set()
        thread.join()
    assert backend.get_many(['a', 'b']) == {'a': 1, 'b': 2}


class CountingCache:
    def __init__(self, cache: Cache) -> None:
        self.cache = cache
        self.transactions = 0
        self.deletes = 0

    def transact(self) -> Any:
        self.transactions += 1
        return self.cache.transact()

    def delete(self, key: str) -> bool:
        self.deletes += 1
        return self.cache.delete(key)


@pytest.mark.parametrize(('count', 'transactions'), [(1, 0), (2, 1), (100, 1), (101, 1), (102, 2), (250, 3)])
def test_delete_many_uses_one_transaction_per_chunk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, count: int, transactions: int
) -> None:
    backend = make(tmp_path)
    keys = [f'k{i}' for i in range(count)]
    backend.set_many({key: 1 for key in keys})
    real = backend.cache
    assert isinstance(real, Cache)
    counting = CountingCache(real)
    monkeypatch.setitem(djangocache.handles, backend.directory, counting)
    backend.delete_many(keys)
    assert (counting.transactions, counting.deletes) == (transactions, count)
    monkeypatch.undo()
    assert backend.get_many(keys) == {}


def test_a_durable_set_many_behind_a_held_lock_waits_once(tmp_path: Path) -> None:
    backend = make(tmp_path, OPTIONS={'durable': True, 'timeout': 0.2})
    release, thread = held_lock(backend)
    try:
        started = time.monotonic()
        failed = backend.set_many({f'k{i}': i for i in range(6)})
        elapsed = time.monotonic() - started
    finally:
        release.set()
        thread.join()
    assert failed == [f'k{i}' for i in range(6)]
    assert elapsed < 0.5  # one lock wait, not six


def test_a_durable_delete_many_behind_a_held_lock_waits_once(tmp_path: Path) -> None:
    backend = make(tmp_path, OPTIONS={'durable': True, 'timeout': 0.2})
    backend.set_many({f'k{i}': i for i in range(6)})
    release, thread = held_lock(backend)
    try:
        started = time.monotonic()
        backend.delete_many([f'k{i}' for i in range(6)])
        elapsed = time.monotonic() - started
    finally:
        release.set()
        thread.join()
    assert elapsed < 0.7  # one lock wait (the first False that took half of one ends the batch), not six


class SlowCache:
    """Takes `pause` seconds over each call that works, and fails the key `failing` at once."""

    def __init__(self, failing: str, pause: float) -> None:
        self.failing = failing
        self.pause = pause
        self.tried: list[str] = []

    def call(self, key: str) -> bool:
        self.tried.append(key)
        if key == self.failing:
            return False
        time.sleep(self.pause)
        return True

    def set(self, key: str, value: Any, expire: Any = None) -> bool:
        return self.call(key)

    def delete(self, key: str) -> bool:
        return self.call(key)


def test_a_durable_delete_many_goes_on_past_a_missing_key_late_in_a_long_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The batch runs for several half lock waits before the miss: only a delete that itself waited ends it.
    backend = make(tmp_path, OPTIONS={'durable': True, 'timeout': 0.02})
    slow = SlowCache(backend.make_key('missing'), pause=0.005)
    monkeypatch.setitem(djangocache.handles, backend.directory, slow)
    keys = ['a', 'b', 'c', 'd', 'missing', 'e', 'f']
    backend.delete_many(keys)
    assert slow.tried == [backend.make_key(key) for key in keys]


def test_a_durable_set_many_fails_only_the_key_that_fails_at_once_late_in_a_long_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = make(tmp_path, OPTIONS={'durable': True, 'timeout': 0.02})
    slow = SlowCache(backend.make_key('unwritable'), pause=0.005)
    monkeypatch.setitem(djangocache.handles, backend.directory, slow)
    keys = ['a', 'b', 'c', 'd', 'unwritable', 'e', 'f']
    assert backend.set_many(dict.fromkeys(keys, 1)) == ['unwritable']
    assert slow.tried == [backend.make_key(key) for key in keys]


def test_a_durable_batch_with_no_lock_wait_tries_every_key(tmp_path: Path) -> None:
    backend = make(tmp_path, OPTIONS={'durable': True, 'timeout': 0})
    backend.set_many({'a': 1, 'b': 2})
    backend.delete_many(['missing', 'a', 'b'])
    assert backend.get_many(['a', 'b']) == {}
    release, thread = held_lock(backend)
    try:
        assert backend.set_many({'a': 1, 'b': 2}) == ['a', 'b']
    finally:
        release.set()
        thread.join()


# Beyond Django's API.


def test_tags_and_evict(tmp_path: Path) -> None:
    backend = make(tmp_path)
    backend.set('a', 1, tag='user:1')
    assert backend.add('b', 2, tag='user:1')
    backend.set('c', 3, tag='user:2')
    backend.set('d', 4)
    assert backend.evict('user:1') == 2
    assert backend.get_many(['a', 'b', 'c', 'd']) == {'c': 3, 'd': 4}


def test_async_set_and_add_take_a_tag(tmp_path: Path) -> None:
    backend = make(tmp_path)

    async def main() -> None:
        await backend.aset('a', 1, tag='t')
        assert await backend.aadd('b', 2, tag='t')

    asyncio.run(main())
    assert backend.evict('t') == 2


def test_pop(tmp_path: Path) -> None:
    backend = make(tmp_path, VERSION=2)
    backend.set('k', 'v')
    assert backend.pop('k') == 'v'
    assert backend.pop('k', 'gone') == 'gone'
    backend.set('k', 'v1', version=1)
    assert backend.pop('k', version=1) == 'v1'


def test_maintenance_calls(tmp_path: Path) -> None:
    backend = make(tmp_path)
    backend.set('old', 1, timeout=0.01)
    backend.set('new', 2)
    time.sleep(0.05)
    assert backend.expire() == 1
    assert backend.cull() == 0
    assert backend.get('old') is None
    backend.stats(enable=True, reset=True)
    backend.get('new')
    backend.get('missing')
    assert backend.stats() == (1, 1)
    assert backend.maintenance_stats() is not None
    assert backend.check() == []


def test_the_extras_work_on_a_fanout_cache(tmp_path: Path) -> None:
    backend = make(tmp_path, OPTIONS={'shards': 4})
    for i in range(20):
        backend.set(f'k{i}', i, tag='all')
    assert backend.pop('k0') == 0
    assert backend.evict('all') == 19
    assert backend.expire() == 0
    assert backend.check() == []


# durable=True.


def test_a_durable_alias_behind_a_held_lock(tmp_path: Path) -> None:
    from diskcache_oxide import Timeout

    backend = make(tmp_path, OPTIONS={'durable': True, 'timeout': 0.05})
    backend.set('k', 1)
    backend.set('n', 1)
    release, thread = held_lock(backend)
    try:
        assert backend.get('k') == 1  # reads don't wait for a held write
        assert backend.set('k', 2) is None  # dropped, as Django's set returns None anyway
        assert backend.add('new', 1) is False
        assert backend.touch('k', 100) is False
        assert backend.delete('k') is False
        with pytest.raises(ValueError):  # the engine reports a held lock as a miss under durable
            backend.incr('n')
        with pytest.raises(Timeout):  # clear() isn't covered by durable (decision 6)
            backend.clear()
    finally:
        release.set()
        thread.join()
    assert backend.get('k') == 1
    assert backend.get('new') is None
    assert backend.incr('n') == 2


@pytest.mark.parametrize('engine', ['cache', 'cached_db'])
def test_a_durable_session_alias_is_refused(tmp_path: Path, engine: str) -> None:
    with override_settings(
        SESSION_ENGINE=f'django.contrib.sessions.backends.{engine}',
        CACHES={'default': {'BACKEND': BACKEND, 'LOCATION': str(tmp_path), 'OPTIONS': {'durable': True}}},
    ):
        with pytest.raises(ImproperlyConfigured, match='SESSION_CACHE_ALIAS'):
            caches['default']


def test_a_durable_alias_beside_the_session_alias_is_fine(tmp_path: Path) -> None:
    with override_settings(
        SESSION_ENGINE='django.contrib.sessions.backends.cache',
        SESSION_CACHE_ALIAS='sessions',
        CACHES={
            'default': {'BACKEND': BACKEND, 'LOCATION': str(tmp_path / 'pages'), 'OPTIONS': {'durable': True}},
            'sessions': {'BACKEND': BACKEND, 'LOCATION': str(tmp_path / 'sessions')},
        },
    ):
        caches['default'].set('page', 1)
        caches['sessions'].set('session', 2)


def test_a_durable_alias_with_database_sessions_is_fine(tmp_path: Path) -> None:
    with override_settings(
        SESSION_ENGINE='django.contrib.sessions.backends.db',
        CACHES={'default': {'BACKEND': BACKEND, 'LOCATION': str(tmp_path), 'OPTIONS': {'durable': True}}},
    ):
        caches['default'].set('k', 1)


def test_a_durable_alias_that_cannot_open_serves_a_null_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from diskcache_oxide import NullCache

    blocker = tmp_path / 'file'
    blocker.write_text('not a directory')
    backend = make(blocker / 'cache', OPTIONS={'durable': True})
    with caplog.at_level('WARNING', logger='diskcache_oxide'):
        assert backend.get('k', 'miss') == 'miss'
        backend.set('k', 1)
        assert backend.add('k', 1) is True  # a NullCache's add always wins
        assert isinstance(backend.cache, NullCache)
    logged = [r for r in caplog.records if 'could not open' in r.getMessage()]
    assert len(logged) == 1  # once per retry window, not per call
    assert logged[0].levelname == 'ERROR'  # not a Timeout: as the Caches registry logs it
    assert logged[0].exc_info is not None
    assert djangocache.unavailable[backend.directory][0] - time.monotonic() > djangocache.RETRY_AFTER_BUSY
    # After the window it tries again, and opens once the directory can exist.
    blocker.unlink()
    blocker.mkdir()
    monkeypatch.setitem(
        djangocache.unavailable,
        backend.directory,
        (time.monotonic() - 1, djangocache.unavailable[backend.directory][1]),
    )
    backend.set('k', 2)
    assert isinstance(backend.cache, Cache)
    assert backend.get('k') == 2
    assert backend.directory not in djangocache.unavailable


def test_a_plain_alias_that_cannot_open_raises(tmp_path: Path) -> None:
    blocker = tmp_path / 'file'
    blocker.write_text('not a directory')
    with pytest.raises(OSError):
        make(blocker / 'cache').get('k')


# Across processes.


def test_add_and_incr_are_atomic_across_processes(tmp_path: Path) -> None:
    # Every worker starts at once (a `go` key the parent sets once all are ready), and every round does
    # an `add`, so a check-then-set `add` or a get-then-set `incr` would lose the race.
    processes, rounds = 8, 200
    code = textwrap.dedent(f"""
        import os, time
        from django.conf import settings
        settings.configure(CACHES={{'default': {{'BACKEND': {BACKEND!r}, 'LOCATION': {str(tmp_path)!r}}}}})
        from django.core.cache import cache
        cache.set(f'ready{{os.getpid()}}', 1)
        deadline = time.monotonic() + 60
        while not cache.get('go'):
            assert time.monotonic() < deadline, 'never told to go'
            time.sleep(0.001)
        wins = 0
        for i in range({rounds}):
            cache.incr('n')
            wins += cache.add(f'a{{i}}', os.getpid())
        print(wins)
    """)
    parent = make(tmp_path)
    parent.set('n', 0)
    workers = [
        subprocess.Popen([sys.executable, '-c', code], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        for _ in range(processes)
    ]
    try:
        ready = [f'ready{worker.pid}' for worker in workers]
        deadline = time.monotonic() + 60
        while len(parent.get_many(ready)) < processes:
            assert time.monotonic() < deadline, 'workers never got ready'
            assert all(worker.poll() is None for worker in workers), [w.stderr.read() for w in workers if w.stderr]
            time.sleep(0.005)
        parent.set('go', True)
        outputs = [worker.communicate(timeout=120) for worker in workers]
    finally:
        for worker in workers:
            if worker.poll() is None:
                worker.kill()
    assert all(worker.returncode == 0 for worker in workers), [err for _, err in outputs]
    assert sum(int(out) for out, _ in outputs) == rounds  # each key added by exactly one process
    assert parent.get('n') == processes * rounds
    winners = {parent.get(f'a{i}') for i in range(rounds)}
    assert winners <= {worker.pid for worker in workers}


class Recorder:
    """Stands in for a handle, recording each call with its arguments."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def __getattr__(self, name: str) -> Any:
        def call(*args: Any, **kwargs: Any) -> Any:
            self.calls.append((name, args + tuple(sorted(kwargs.items()))))
            return name

        return call


def test_each_extra_calls_its_cache_method(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    backend = make(tmp_path, VERSION=3)
    recorder = Recorder()
    monkeypatch.setitem(djangocache.handles, backend.directory, recorder)
    assert backend.pop('k', 'dflt') == 'pop'
    assert backend.evict('tag') == 'evict'
    assert backend.expire() == 'expire'
    assert backend.cull() == 'cull'
    assert backend.stats(False, True) == 'stats'
    assert backend.maintenance_stats() == 'maintenance_stats'
    assert backend.check(True) == 'check'
    assert recorder.calls == [
        ('pop', (':3:k', 'dflt')),
        ('evict', ('tag',)),
        ('expire', ()),
        ('cull', ()),
        ('stats', (False, True)),
        ('maintenance_stats', ()),
        ('check', (True,)),
    ]


# The refusal sees through subclasses.


def test_a_backend_subclass_on_a_durable_session_alias_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / 'mysite_cache.py').write_text(
        'from diskcache_oxide import DjangoCache\n\nclass MyCache(DjangoCache):\n    pass\n'
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    with override_settings(
        SESSION_ENGINE='django.contrib.sessions.backends.cache',
        CACHES={
            'default': {
                'BACKEND': 'mysite_cache.MyCache',
                'LOCATION': str(tmp_path / 'c'),
                'OPTIONS': {'durable': True},
            }
        },
    ):
        with pytest.raises(ImproperlyConfigured, match='SESSION_CACHE_ALIAS'):
            caches['default']


def test_a_session_engine_subclass_on_a_durable_alias_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / 'mysite_sessions.py').write_text(
        'from django.contrib.sessions.backends.cached_db import SessionStore as Base\n\n'
        'class SessionStore(Base):\n    pass\n'
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    with override_settings(
        SESSION_ENGINE='mysite_sessions',
        CACHES={'default': {'BACKEND': BACKEND, 'LOCATION': str(tmp_path / 'c'), 'OPTIONS': {'durable': True}}},
    ):
        with pytest.raises(ImproperlyConfigured, match='SESSION_CACHE_ALIAS'):
            caches['default']


# The NullCache fallback's edges.


@pytest.mark.parametrize(
    ('options', 'error'), [({'size_limit': 5}, ValueError), ({'eviction_policy': 'lru'}, ValueError)]
)
def test_bad_settings_still_raise_on_a_durable_alias(
    tmp_path: Path, options: dict[str, Any], error: type[Exception]
) -> None:
    with pytest.raises(error):
        make(tmp_path, OPTIONS={'durable': True, **options}).get('k')
    assert not djangocache.unavailable


def test_the_wrong_kind_of_directory_falls_back_on_a_durable_alias(tmp_path: Path) -> None:
    from diskcache_oxide import NullCache

    make(tmp_path).set('k', 1)  # a plain Cache's directory
    djangocache.close_all()
    sharded = make(tmp_path, OPTIONS={'shards': 4, 'durable': True})
    assert isinstance(sharded.cache, NullCache)  # LayoutMismatchError, a RuntimeError
    assert sharded.get('k') is None
    sharded.delete_many(['k'])
    djangocache.close_all()
    assert not djangocache.unavailable
    assert make(tmp_path).get('k') == 1


def test_the_first_failure_is_logged_once_across_threads(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    blocker = tmp_path / 'file'
    blocker.write_text('not a directory')
    backend = make(blocker / 'cache', OPTIONS={'durable': True})
    threads = 16
    barrier = threading.Barrier(threads)

    def worker() -> None:
        barrier.wait()
        for _ in range(50):
            backend.get('k')

    with caplog.at_level('WARNING', logger='diskcache_oxide'):
        pool = [threading.Thread(target=worker) for _ in range(threads)]
        for thread in pool:
            thread.start()
        for thread in pool:
            thread.join()
    assert len([r for r in caplog.records if 'could not open' in r.getMessage()]) == 1


@pytest.mark.skipif(sys.platform == 'win32' or os.geteuid() == 0, reason='chmod-based; root ignores it')
def test_a_durable_set_many_fails_only_the_key_that_could_not_be_written(tmp_path: Path) -> None:
    from tests.unit.test_durable import unwritable_directory

    backend = make(tmp_path, OPTIONS={'durable': True})
    backend.set('warm', 1)
    with unwritable_directory(tmp_path):
        # A value of 64 KiB or more goes in a file of its own, which can't be created now.
        failed = backend.set_many({'s1': 1, 'big': 'x' * 70_000, 's2': 2})
    assert failed == ['big']
    assert backend.get_many(['s1', 'big', 's2']) == {'s1': 1, 's2': 2}


@pytest.mark.parametrize('options', [{}, {'shards': 4}], ids=['cache', 'fanout'])
def test_durable_batches_on_a_damaged_database_do_not_wait(tmp_path: Path, options: dict[str, Any]) -> None:
    from tests.unit.test_durable import _damage

    backend = make(tmp_path, OPTIONS={'durable': True, **options})
    keys = [f'k{i}' for i in range(200)]
    backend.set_many({key: 1 for key in keys})
    for directory in [tmp_path, *[p for p in tmp_path.rglob('*') if p.is_dir()]]:
        _damage(directory)
    started = time.monotonic()
    backend.set_many({key: 2 for key in keys})
    backend.delete_many(keys)
    assert time.monotonic() - started < backend.options['timeout']
