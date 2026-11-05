"""Cache, NullCache and CacheLike stay interchangeable: same public methods, same parameters."""

from __future__ import annotations

import gc
import inspect
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from diskcache_oxide import Cache, CacheLike, NullCache, TransactionalCacheLike

REPO = Path(__file__).resolve().parents[2]

# Protocol members that are properties, not methods.
PROPERTIES = {'directory', 'timeout', 'size_limit', 'cull_limit', 'enabled'}


def protocol_methods() -> list[str]:
    names = {
        n
        for n, v in vars(CacheLike).items()
        if callable(v) and (not n.startswith('_') or (n.startswith('__') and n.endswith('__')))
    }
    return sorted(names - {'__init__', '__class_getitem__', '__subclasshook__', '__init_subclass__'} - PROPERTIES)


def params(func: Callable[..., Any]) -> list[tuple[str, inspect._ParameterKind]]:
    signature = inspect.signature(func)
    return [(p.name, p.kind) for p in signature.parameters.values() if p.name != 'self']


@pytest.mark.parametrize('name', protocol_methods())
def test_every_protocol_method_has_the_same_parameters_on_both_classes(
    name: str,
) -> None:
    expected = params(getattr(CacheLike, name))
    assert params(getattr(NullCache, name)) == expected, f'NullCache.{name}'
    if name.startswith('__') and name not in {'__enter__', '__exit__'}:
        return  # PyO3 dunder slots don't carry text signatures; ty checks them below.
    assert params(getattr(Cache, name)) == expected, f'Cache.{name}'


@pytest.mark.parametrize('name', sorted(PROPERTIES))
def test_every_protocol_property_exists_on_both_classes(name: str) -> None:
    assert hasattr(Cache, name) and hasattr(NullCache, name)


def test_constructors_take_the_same_arguments() -> None:
    cache_params = [name for name, _ in params(Cache)]
    null_params = [name for name, _ in params(NullCache.__init__)]
    assert null_params == [*cache_params, 'invalidate']


def test_ty_sees_both_classes_as_cache_like(tmp_path: Path) -> None:
    probe = tmp_path / 'probe.py'
    probe.write_text(
        'from diskcache_oxide import Cache, CacheLike, NullCache\n'
        "real: CacheLike = Cache('/tmp/x')\n"
        'null: CacheLike = NullCache()\n'
        'forwarding: CacheLike = NullCache(invalidate=real)\n'
    )
    result = subprocess.run(
        [sys.executable, '-m', 'ty', 'check', '--python', sys.prefix, str(probe)],
        capture_output=True,
        text=True,
        cwd=REPO,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_cache_like_has_no_transact() -> None:
    # A sharded cache's transact() takes a key, so it can be a CacheLike but not a TransactionalCacheLike.
    assert 'transact' not in vars(CacheLike) and not hasattr(CacheLike, 'transact')


def test_transact_has_the_protocols_parameters_on_both_classes() -> None:
    expected = params(TransactionalCacheLike.transact)
    assert expected == [('retry', inspect.Parameter.POSITIONAL_OR_KEYWORD)]
    assert params(Cache.transact) == expected
    # NullCache also takes the keyword-only key of a sharded cache's transact(key=...).
    assert params(NullCache.transact) == [*expected, ('key', inspect.Parameter.KEYWORD_ONLY)]
    assert inspect.signature(Cache.transact).parameters['retry'].default is False
    assert inspect.signature(NullCache.transact).parameters['retry'].default is False


def test_both_classes_are_transactional_at_runtime(tmp_path: Path) -> None:
    with Cache(tmp_path / 'c') as real:
        assert isinstance(real, TransactionalCacheLike)
        assert isinstance(NullCache(invalidate=real), TransactionalCacheLike)


def test_the_test_hook_is_gone() -> None:
    assert not hasattr(Cache, '_hold_write_transaction')


def _ty(tmp_path: Path, source: str) -> subprocess.CompletedProcess[str]:
    probe = tmp_path / 'probe.py'
    probe.write_text(source)
    return subprocess.run(
        [sys.executable, '-m', 'ty', 'check', '--python', sys.prefix, str(probe)],
        capture_output=True,
        text=True,
        cwd=REPO,
    )


def test_ty_sees_both_classes_as_transactional(tmp_path: Path) -> None:
    result = _ty(
        tmp_path,
        'from diskcache_oxide import Cache, Caches, NullCache, TransactionalCacheLike\n'
        "real: TransactionalCacheLike = Cache('/tmp/x')\n"
        'null: TransactionalCacheLike = NullCache()\n'
        'forwarding: TransactionalCacheLike = NullCache(invalidate=real)\n'
        "looked_up: TransactionalCacheLike = Caches('/tmp/y')['a']\n"
        'with real.transact(retry=True) as tx:\n'
        "    tx.set('k', 1)\n",
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_ty_finds_no_transact_on_cache_like(tmp_path: Path) -> None:
    result = _ty(
        tmp_path,
        'from diskcache_oxide import CacheLike\n'
        'def use(cache: CacheLike) -> None:\n'
        '    with cache.transact():\n'
        '        pass\n',
    )
    assert result.returncode != 0 and 'transact' in result.stdout, result.stdout + result.stderr


@pytest.mark.parametrize('timeout', [1e10, 1e19, 1e20, 1e300])
def test_a_huge_timeout_works_instead_of_panicking(tmp_path: Path, timeout: float) -> None:
    # Duration::from_secs_f64 panics above about 1.8e19 s, and Instant + Duration on overflow; a
    # panic surfaces as PanicException, a BaseException that slips past `except Exception`.
    with Cache(tmp_path / 'c', timeout=timeout) as cache:
        assert cache.set('k', 1) is True and cache.get('k') == 1
        assert cache.timeout > 1e9  # Clamped to about 100 years.


_REENTRANT: dict[str, Cache] = {}


class _WritesOnUnpickle:
    """Unpickling it writes to the cache it was read from (roadmap Phase 2.5, item 7)."""

    payload: str

    def __setstate__(self, state: dict[str, object]) -> None:
        self.__dict__.update(state)
        _REENTRANT['cache'].set('written-while-unpickling', 1)


@pytest.mark.parametrize('popped', ['inline', 'spilled'])
def test_pop_decodes_outside_any_write_transaction(tmp_path: Path, popped: str) -> None:
    # Decoding inside pop's write transaction made a __setstate__ that writes the same cache wait
    # out the whole timeout behind its own transaction, then raise Timeout. Now it decodes first,
    # as a get does, and deletes afterwards.
    cache = Cache(tmp_path / 'c', timeout=2, pickle=True)
    _REENTRANT['cache'] = cache
    try:
        value = _WritesOnUnpickle()
        value.payload = 'x' * (100 if popped == 'inline' else 100_000)
        cache.set('k', value)
        started = time.monotonic()
        result = cache.pop('k')
        assert time.monotonic() - started < 1, 'pop waited on its own write transaction'
        assert isinstance(result, _WritesOnUnpickle)
        assert cache.get('written-while-unpickling') == 1
        assert 'k' not in cache
    finally:
        _REENTRANT.clear()
        cache.close()


def test_pop_inside_a_held_transaction_decodes_with_the_same_transaction_still_usable(
    tmp_path: Path,
) -> None:
    # Inside your own held transaction the decode still runs while it's open; the write from
    # __setstate__ must join it, not wait for it.
    cache = Cache(tmp_path / 'c', timeout=2, pickle=True)
    _REENTRANT['cache'] = cache
    try:
        value = _WritesOnUnpickle()
        value.payload = 'x'
        cache.set('k', value)
        started = time.monotonic()
        with cache.transact():
            assert isinstance(cache.pop('k'), _WritesOnUnpickle)
        assert time.monotonic() - started < 1, 'the write from __setstate__ waited on its own transaction'
        assert cache.get('written-while-unpickling') == 1
        assert 'k' not in cache
    finally:
        _REENTRANT.clear()
        cache.close()


_BIG = 70_000  # over the 64 KiB spill threshold


def _value_files(directory: Path) -> set[Path]:
    return set(directory.rglob('*.val'))


def _change_spilled_key(cache: Cache, op: str) -> None:
    if op == 'set':
        cache.set('k', b'y' * _BIG)
    elif op == 'set twice':
        # The second replaces a file this transaction created.
        cache.set('k', b'z' * _BIG)
        cache.set('k', b'y' * _BIG)
    elif op == 'add':
        cache.delete('k')
        assert cache.add('k', b'y' * _BIG)
    elif op == 'delete':
        assert cache.delete('k')
    elif op == 'pop':
        assert cache.pop('k') == b'x' * _BIG
    elif op == 'clear':
        cache.clear()


@pytest.mark.parametrize('op', ['set', 'set twice', 'add', 'delete', 'pop', 'clear'])
def test_a_rolled_back_held_transaction_keeps_spilled_values_and_leaves_no_new_files(tmp_path: Path, op: str) -> None:
    # Value files the transaction replaced or deleted are unlinked only once it commits, and the
    # ones it created are removed when it rolls back (review 07).
    cache = Cache(tmp_path / 'c')
    try:
        cache.set('k', b'x' * _BIG)
        before = _value_files(tmp_path / 'c')
        with pytest.raises(RuntimeError, match='abort'):
            with cache.transact():
                _change_spilled_key(cache, op)
                raise RuntimeError('abort')
        assert cache.get('k') == b'x' * _BIG
        assert _value_files(tmp_path / 'c') == before
    finally:
        cache.close()


@pytest.mark.parametrize('op', ['set', 'set twice', 'add', 'delete', 'pop', 'clear'])
def test_a_committed_held_transaction_unlinks_the_spilled_values_it_replaced(tmp_path: Path, op: str) -> None:
    cache = Cache(tmp_path / 'c')
    try:
        cache.set('k', b'x' * _BIG)
        before = _value_files(tmp_path / 'c')
        with cache.transact():
            _change_spilled_key(cache, op)
            assert before <= _value_files(tmp_path / 'c'), 'a value file went before the commit'
        after = _value_files(tmp_path / 'c')
        assert not (before & after), 'the replaced value file outlived the commit'
        expected = b'y' * _BIG if op in ('set', 'set twice', 'add') else None
        assert cache.get('k') == expected
        assert len(after) == (1 if expected else 0)
    finally:
        cache.close()


@pytest.mark.parametrize('commit', [False, True])
@pytest.mark.parametrize('op', ['expire', 'cull'])
def test_held_maintenance_unlinks_value_files_only_once_it_commits(tmp_path: Path, op: str, commit: bool) -> None:
    # cull_limit=0: no inline cull on set. For cull, 20 spilled values (1.4 MB) are over the 1 MiB
    # size_limit; for expire, one value has expired.
    cache = Cache(tmp_path / 'c', size_limit=1 << 20, cull_limit=0)
    try:
        if op == 'expire':
            cache.set('k', b'x' * _BIG, expire=0.05)
            time.sleep(0.1)
        else:
            for i in range(20):
                cache.set(f'k{i}', b'x' * _BIG)
        before = _value_files(tmp_path / 'c')
        try:
            with cache.transact():
                removed = cache.expire() if op == 'expire' else cache.cull()
                assert removed >= 1
                assert _value_files(tmp_path / 'c') == before, 'a value file went before the commit'
                if not commit:
                    raise RuntimeError('abort')
        except RuntimeError:
            assert not commit
        after = _value_files(tmp_path / 'c')
        if commit:
            assert after < before and len(before) - len(after) == removed
        else:
            assert after == before
            assert len(cache) == len(before)
    finally:
        cache.close()


# -- per-call timeout on clear/expire/cull (plan 08, step 1) -------------------------------------


@pytest.fixture
def held_write_lock(tmp_path: Path):
    """A cache with expired rows, and a peer process that can hold its write lock.

    The peer has to be another process: POSIX locks are per process, so a second SQLite library in
    this one (the stdlib's) wouldn't block the cache at all.
    """
    from tests.conformance.proc_workers import SqlitePeer

    cache = Cache(tmp_path / 'c', timeout=60)
    for i in range(10):
        cache.set(f'k{i}', 'v', expire=0.001)
    time.sleep(0.01)
    with SqlitePeer(tmp_path / 'c' / 'diskcache-oxide.sqlite') as peer:
        yield cache, peer
    cache.close()


@pytest.mark.parametrize('method', ['clear', 'expire', 'cull'])
def test_maintenance_takes_its_own_timeout(held_write_lock, method: str) -> None:
    cache, peer = held_write_lock
    peer.begin('IMMEDIATE')
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        getattr(cache, method)(timeout=0.2)
    assert time.monotonic() - started < 5, 'the per-call timeout was ignored for the handle’s 60 s'
    assert cache.timeout == 60, 'the override is for that call only'

    # A longer per-call timeout outlasts the peer.
    import threading

    # Each set may sweep rows that expired before it, so count what's left rather than assuming 10.
    left = len(cache)
    assert left > 0
    release = threading.Timer(0.3, peer.rollback)
    release.start()
    try:
        assert getattr(cache, method)(timeout=10) == left
    finally:
        release.cancel()
        release.join()


@pytest.mark.parametrize('method', ['clear', 'expire', 'cull'])
def test_maintenance_timeout_also_bounds_another_threads_write(tmp_path: Path, method: str) -> None:
    import threading

    cache = Cache(tmp_path / 'c', timeout=60)
    cache.set('k', 'v')
    other = Cache(tmp_path / 'c', timeout=60)
    holding, done = threading.Event(), threading.Event()

    def hold() -> None:
        with other.transact():
            holding.set()
            done.wait(10)

    holder = threading.Thread(target=hold)
    holder.start()
    try:
        assert holding.wait(10)
        started = time.monotonic()
        with pytest.raises(TimeoutError):
            getattr(cache, method)(timeout=0.2)
        assert time.monotonic() - started < 5
    finally:
        done.set()
        holder.join()
    other.close()
    cache.close()


@pytest.mark.parametrize('method', ['clear', 'expire', 'cull', 'checkpoint'])
@pytest.mark.parametrize('make', [lambda p: Cache(p), lambda p: NullCache(p)], ids=['Cache', 'NullCache'])
def test_maintenance_timeout_is_validated(tmp_path: Path, make, method: str) -> None:
    cache = make(tmp_path / 'c')
    bad_values = [
        (True, TypeError),
        ('1', TypeError),
        (-1, ValueError),
        (float('inf'), ValueError),
        (float('nan'), ValueError),
        (10**400, OverflowError),
    ]
    for bad, error in bad_values:
        with pytest.raises(error):
            getattr(cache, method)(timeout=bad)
    getattr(cache, method)(timeout=None)
    getattr(cache, method)(timeout=0)
    cache.close()


def test_null_cache_validates_its_timeout_like_cache(tmp_path: Path) -> None:
    for make in (Cache, NullCache):
        with pytest.raises(ValueError):
            make(tmp_path / 'c', timeout=-1)
        cache = make(tmp_path / 'c', timeout=None)
        assert cache.timeout == 60
        cache.close()


def test_checkpoint_timeout_is_keyword_only(tmp_path: Path) -> None:
    for make in (Cache, NullCache):
        cache = make(tmp_path / 'c')
        with pytest.raises(TypeError):
            cache.checkpoint(0.5)  # ty: ignore[too-many-positional-arguments]
        cache.close()


def test_null_cache_clear_passes_the_timeout_to_its_invalidate_cache(
    held_write_lock,
) -> None:
    cache, peer = held_write_lock
    peer.begin('IMMEDIATE')
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        NullCache(invalidate=cache).clear(timeout=0.2)
    assert time.monotonic() - started < 5
    peer.rollback()


@pytest.mark.parametrize('walk', ['iterkeys', 'iter'])
def test_a_key_iterator_in_a_reference_cycle_is_collected_and_releases_its_handle(tmp_path: Path, walk: str) -> None:
    keeper = Cache(tmp_path / 'c')

    def make() -> None:
        box: dict[str, Any] = {}
        cache = Cache(tmp_path / 'c', clock=lambda: (box, time.time())[1])
        box['iterator'] = cache.iterkeys() if walk == 'iterkeys' else iter(cache)  # Iterator -> cache -> clock -> box.

    make()
    assert keeper._handle_count() == 2, 'the cycle keeps the second handle open'
    gc.collect()
    assert keeper._handle_count() == 1, 'collecting the cycle released it'
    keeper.close()
