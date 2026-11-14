"""`FanoutCache.transact(key=...)` (plan 13, step 13k): a transaction on one shard, refusing the others (M3, D-040).

It is `Cache.transact()` on the key's shard store, so the machinery (cross-thread ends, parking,
forks) is covered by tests/unit/test_transact.py. These pin what the shard scope adds: the
keyword-only key, the other-shard refusal for reads and writes through any handle on the root,
nesting, durable handles, and a transaction collected from a reference cycle. The semantics stock
diskcache shares are in tests/conformance/test_fanout.py.
"""

from __future__ import annotations

import re
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from diskcache_oxide import Cache, Caches, FanoutCache, Key, NullCache, ShardedCacheLike, Timeout, TransactionAborted
from diskcache_oxide._core import Transaction

from tests.conformance.proc_workers import stored_rows
from tests.keys import str_keys
from tests.unit.test_durable import SHORT, peer_holding_the_write_lock

MiB = 1 << 20
WATCHDOG = 20.0
SHARDS = 4


def _on_shard(cache: FanoutCache, shard: int, count: int = 1, prefix: str = 'k') -> list[str]:
    """`count` keys that hash to `shard`."""
    found = [k for k in (f'{prefix}{i}' for i in range(10_000)) if cache.shard_of(k) == shard]
    assert len(found) >= count
    return found[:count]


def _on_thread(call: Callable[[], Any]) -> Any:
    found: list[Any] = []
    thread = threading.Thread(target=lambda: found.append(call()), daemon=True)
    thread.start()
    thread.join(WATCHDOG)
    assert found, 'the other thread never returned'
    return found[0]


@pytest.fixture
def cache(tmp_path: Path) -> Iterator[FanoutCache]:
    with FanoutCache(tmp_path / 'f', shards=SHARDS, timeout=10, size_limit=SHARDS * MiB) as cache:
        yield cache


@pytest.fixture
def keys(cache: FanoutCache) -> tuple[str, str, str]:
    """Two keys on shard 0 and one on shard 1."""
    a, b = _on_shard(cache, 0, 2)
    (other,) = _on_shard(cache, 1)
    return a, b, other


# -- the handle and its arguments ----------------------------------------------------------------------------


def test_the_block_gets_the_fanout_cache_and_commits_on_exit(cache: FanoutCache, keys: tuple[str, str, str]) -> None:
    a, b, _ = keys
    txn = cache.transact(key=a)
    assert isinstance(txn, Transaction)
    assert repr(txn) == '<diskcache_oxide.Transaction Ready on shard 0>'
    with txn as tx:
        assert tx is cache
        tx.set(a, 1)
        cache.set(b, 2)
        assert _on_thread(lambda: cache.get(a)) is None, 'another thread saw an uncommitted write'
    assert (cache.get(a), cache.get(b)) == (1, 2)
    assert repr(txn) == '<diskcache_oxide.Transaction Done(committed) on shard 0>'


def test_the_owners_exit_after_another_thread_rolled_it_back_raises_transaction_aborted(
    cache: FanoutCache, keys: tuple[str, str, str]
) -> None:
    a, _, _ = keys
    txn = cache.transact(key=a)
    with pytest.raises(TransactionAborted, match='did not commit'), txn:
        cache.set(a, 1)
        assert _on_thread(lambda: txn.__exit__(ValueError, ValueError(), None)) is False
    assert cache.get(a) is None


def test_the_key_neednt_exist_and_a_block_that_raises_rolls_back(
    cache: FanoutCache, keys: tuple[str, str, str]
) -> None:
    a, b, _ = keys
    cache.set(b, 'before')
    with pytest.raises(KeyError), cache.transact(key=a):
        cache.set(a, 1)
        cache.delete(b)
        raise KeyError
    assert (cache.get(a), cache.get(b)) == (None, 'before')


def test_the_key_is_keyword_only_and_required(cache: FanoutCache) -> None:
    with pytest.raises(TypeError):
        cache.transact('k')  # ty: ignore[missing-argument, too-many-positional-arguments]
    with pytest.raises(TypeError):
        cache.transact()  # ty: ignore[missing-argument]


@pytest.mark.parametrize('key', [[1], frozenset(), bytearray(b'k')])
def test_a_key_that_isnt_a_key_type_raises(cache: FanoutCache, key: object) -> None:
    with pytest.raises(TypeError):
        cache.transact(key=key)  # ty: ignore[invalid-argument-type]


@pytest.mark.parametrize('key', [1, b'k', None, ('t', 2)])
def test_a_typed_key_names_its_shard_for_a_transaction(cache: FanoutCache, key: Key) -> None:
    with cache.transact(key=key) as tx:
        tx.set(key, 'in-tx')
    assert cache.get(key) == 'in-tx'


@pytest.mark.parametrize('retry', [1, 'yes', None])
def test_a_retry_that_isnt_a_bool_raises(cache: FanoutCache, retry: object) -> None:
    with pytest.raises(TypeError):
        cache.transact(key='k', retry=retry)  # ty: ignore[invalid-argument-type]


def test_it_satisfies_the_sharded_protocol(cache: FanoutCache) -> None:
    assert isinstance(cache, ShardedCacheLike)
    assert isinstance(NullCache(), ShardedCacheLike)


# -- other shards are refused (M3) ---------------------------------------------------------------------------

ROUTED: dict[str, Callable[[FanoutCache, str], Any]] = {
    'get': lambda c, k: c.get(k),
    'getitem': lambda c, k: c[k],
    'contains': lambda c, k: k in c,
    'set': lambda c, k: c.set(k, 1),
    'setitem': lambda c, k: c.__setitem__(k, 1),
    'add': lambda c, k: c.add(k, 1),
    'delete': lambda c, k: c.delete(k),
    'delitem': lambda c, k: c.__delitem__(k),
    'pop': lambda c, k: c.pop(k),
    'touch': lambda c, k: c.touch(k, 10),
    'incr': lambda c, k: c.incr(k),
    'decr': lambda c, k: c.decr(k),
}


@pytest.mark.parametrize('op', sorted(ROUTED))
def test_every_routed_call_on_another_shard_raises_reads_included(
    cache: FanoutCache, keys: tuple[str, str, str], op: str
) -> None:
    a, _, other = keys
    cache.set(other, 5)
    with cache.transact(key=a):
        with pytest.raises(ValueError, match='shard 1, but this thread.s transaction is on shard 0 of 4'):
            ROUTED[op](cache, other)
        cache.set(a, 1)
    assert cache.get(other) == 5, 'the refused call changed the other shard'
    assert cache.get(a) == 1, 'the refusal broke the transaction'


def test_the_refusal_ends_with_the_transaction(cache: FanoutCache, keys: tuple[str, str, str]) -> None:
    a, _, other = keys
    with cache.transact(key=a):
        pass
    assert cache.set(other, 1) is True
    with pytest.raises(RuntimeError), cache.transact(key=a):
        raise RuntimeError
    assert cache.get(other) == 1


def test_another_handle_on_the_root_shares_the_transaction_and_the_refusal(
    tmp_path: Path, cache: FanoutCache, keys: tuple[str, str, str]
) -> None:
    a, b, other = keys
    with FanoutCache(tmp_path / 'f', timeout=10) as second:
        started = time.monotonic()
        with cache.transact(key=a):
            second.set(b, 2)  # Joins the transaction: it would wait out the timeout if it didn't.
            with pytest.raises(ValueError, match='shard 1'):
                second.get(other)
            with second.transact(key=b) as tx:
                assert tx is second
            assert _on_thread(lambda: second.get(b)) is None, "the second handle's write committed early"
        assert time.monotonic() - started < 5
        assert second.get(b) == 2


def test_other_threads_use_every_shard_while_one_is_held(cache: FanoutCache, keys: tuple[str, str, str]) -> None:
    a, _, other = keys
    with cache.transact(key=a):
        started = time.monotonic()
        assert _on_thread(lambda: cache.set(other, 1)) is True, 'another thread was refused'
        assert time.monotonic() - started < 5, 'the other shard was locked too'
        assert _on_thread(lambda: cache.get(other)) == 1


def test_another_roots_keys_are_not_refused(tmp_path: Path, cache: FanoutCache, keys: tuple[str, str, str]) -> None:
    a, _, _ = keys
    with FanoutCache(tmp_path / 'g', shards=SHARDS, size_limit=SHARDS * MiB) as elsewhere:
        with cache.transact(key=a):
            for shard in range(SHARDS):
                (k,) = _on_shard(elsewhere, shard)
                assert elsewhere.set(k, shard) is True
            (k,) = _on_shard(elsewhere, 1)
            with elsewhere.transact(key=k):  # Another root's stores: allowed, as for two Caches.
                elsewhere.set(k, 'inner')
        assert elsewhere.get(k) == 'inner'


def test_a_plain_caches_transaction_doesnt_refuse_any_shard(tmp_path: Path, cache: FanoutCache) -> None:
    with Cache(tmp_path / 'c', timeout=10) as plain, plain.transact():
        for shard in range(SHARDS):
            (k,) = _on_shard(cache, shard)
            assert cache.set(k, shard) is True
            assert cache.get(k) == shard


# Every call over every shard, with the name its refusal gives (plan 13, decision 2).
FAN_OUT: dict[str, tuple[str, Callable[[FanoutCache], Any]]] = {
    'len': ('len()', len),
    'volume': ('volume()', lambda c: c.volume()),
    'iter': ('iteration', iter),
    'iterkeys': ('iterkeys()', lambda c: c.iterkeys()),
    'clear': ('clear()', lambda c: c.clear()),
    'expire': ('expire()', lambda c: c.expire()),
    'cull': ('cull()', lambda c: c.cull()),
    'checkpoint': ('checkpoint()', lambda c: c.checkpoint()),
}


@pytest.mark.races_maintenance
@pytest.mark.parametrize('held', [0, 3])
@pytest.mark.parametrize('op', sorted(FAN_OUT))
def test_every_call_over_every_shard_raises_inside_the_block(cache: FanoutCache, op: str, held: int) -> None:
    # Held on shard 0 and on the last shard, so a check that only looks at one shard can't pass.
    (a,) = _on_shard(cache, held)
    (other,) = _on_shard(cache, 1)
    name, call = FAN_OUT[op]
    cache.set(other, 5, expire=0.01)  # Expired by the time the block runs, so expire() or cull() would remove it.
    time.sleep(0.05)
    with cache.transact(key=a):
        cache.set(a, 1)
        refused = (
            rf'^{re.escape(name)} on a FanoutCache covers every shard, '
            rf'but this thread.s transaction is on shard {held} of 4'
        )
        with pytest.raises(ValueError, match=refused):
            call(cache)
        cache.set(a, 2)
    assert cache.get(a) == 2, 'the refusal broke the transaction'
    # Counted on disk: len() hides the expired row (D-060), removed or not.
    shards = (Path(cache.directory) / f'{shard:03d}' / 'diskcache-oxide.sqlite' for shard in range(SHARDS))
    assert stored_rows(*shards) == 2, 'the refused call removed something'
    assert cache.get(other) is None  # Expired, but still stored: the count above includes it.
    assert len(cache) == 1
    call(cache)  # The refusal ends with the block.


@pytest.mark.parametrize('op', sorted(FAN_OUT))
def test_calls_over_every_shard_are_refused_through_another_handle(
    tmp_path: Path, cache: FanoutCache, keys: tuple[str, str, str], op: str
) -> None:
    a, _, _ = keys
    name, call = FAN_OUT[op]
    with FanoutCache(tmp_path / 'f', timeout=10) as second, cache.transact(key=a):
        with pytest.raises(ValueError, match=re.escape(name)):
            call(second)


def test_an_iterator_made_before_the_block_cant_walk_inside_it(cache: FanoutCache, keys: tuple[str, str, str]) -> None:
    a, _, _ = keys
    for k in keys:
        cache.set(k, 1)
    walk, merge = iter(cache), cache.iterkeys()
    assert next(merge) == min(keys)
    with cache.transact(key=a):
        for it in (walk, merge):
            with pytest.raises(ValueError, match='^iteration on a FanoutCache covers every shard'):
                next(it)
    assert sorted(str_keys(walk)) == sorted(keys), 'the refused next() lost the walk'
    assert [min(keys), *merge] == sorted(keys), 'the refused next() lost the merge'


def test_other_threads_calls_over_every_shard_are_not_refused(cache: FanoutCache, keys: tuple[str, str, str]) -> None:
    a, _, other = keys
    cache.set(other, 1)
    with cache.transact(key=a):
        cache.set(a, 1)
        assert _on_thread(lambda: len(cache)) == 1, "another thread saw the block's uncommitted write"
        assert _on_thread(lambda: sorted(str_keys(cache))) == [other]
        assert _on_thread(lambda: cache.volume()) > 0
    assert len(cache) == 2


@pytest.mark.parametrize('op', ['clear', 'expire', 'cull', 'checkpoint'])
def test_a_bad_timeout_is_a_type_error_before_the_refusal(
    cache: FanoutCache, keys: tuple[str, str, str], op: str
) -> None:
    a, _, _ = keys
    with cache.transact(key=a), pytest.raises(TypeError):
        getattr(cache, op)(timeout='soon')


@pytest.mark.parametrize('op', sorted(FAN_OUT))
def test_another_roots_transaction_doesnt_refuse_calls_over_every_shard(
    tmp_path: Path, cache: FanoutCache, op: str
) -> None:
    _, call = FAN_OUT[op]
    with FanoutCache(tmp_path / 'g', shards=SHARDS, timeout=10) as elsewhere:
        (k,) = _on_shard(elsewhere, 0)
        with elsewhere.transact(key=k):
            call(cache)


def test_a_plain_caches_transaction_doesnt_refuse_calls_over_every_shard(
    tmp_path: Path, cache: FanoutCache, keys: tuple[str, str, str]
) -> None:
    a, _, _ = keys
    cache.set(a, 1)
    with Cache(tmp_path / 'c', timeout=10) as plain, plain.transact():
        assert len(cache) == 1
        assert list(cache.iterkeys()) == [a]
        assert cache.expire() == 0


# -- nesting -------------------------------------------------------------------------------------------------


def test_a_nested_transaction_on_the_same_shard_joins_and_its_exit_commits_nothing(
    cache: FanoutCache, keys: tuple[str, str, str]
) -> None:
    a, b, _ = keys
    with cache.transact(key=a):
        with cache.transact(key=b) as inner:
            assert inner is cache
            cache.set(b, 2)
        assert _on_thread(lambda: cache.get(b)) is None, 'the inner exit committed'
    assert cache.get(b) == 2


def test_a_nested_transaction_on_another_shard_raises_and_the_outer_one_carries_on(
    cache: FanoutCache, keys: tuple[str, str, str]
) -> None:
    a, _, other = keys
    with cache.transact(key=a):
        cache.set(a, 1)
        inner = cache.transact(key=other)  # Creating it is fine; entering it is refused.
        with pytest.raises(ValueError, match='shard 1, but this thread.s transaction is on shard 0'):
            inner.__enter__()
        assert repr(inner) == '<diskcache_oxide.Transaction Ready on shard 1>'
    assert cache.get(a) == 1
    with inner:  # Refused only while the other shard is held.
        cache.set(other, 1)
    assert cache.get(other) == 1


# -- durable=True (D-028) ------------------------------------------------------------------------------------


def test_a_durable_fanout_caches_begin_that_times_out_raises(tmp_path: Path) -> None:
    with FanoutCache(tmp_path / 'f', shards=SHARDS, timeout=SHORT, size_limit=SHARDS * MiB, durable=True) as cache:
        (a,) = _on_shard(cache, 2)
        with peer_holding_the_write_lock(tmp_path / 'f' / '002'):
            with pytest.raises(Timeout):
                cache.transact(key=a).__enter__()
            assert cache.set(a, 1) is False, 'outside a transaction the same failure is a durable miss'


def test_a_refused_call_inside_a_durable_block_raises(tmp_path: Path) -> None:
    with FanoutCache(tmp_path / 'f', shards=SHARDS, size_limit=SHARDS * MiB, durable=True) as cache:
        (a,) = _on_shard(cache, 0)
        (other,) = _on_shard(cache, 1)
        with pytest.raises(ValueError), cache.transact(key=a):
            cache.set(a, 1)
            cache.get(other)
        assert cache.get(a) is None, 'the block that raised committed'


# -- the registry ----------------------------------------------------------------------------------------------


def test_a_registered_fanout_caches_transact_takes_the_key_on_or_off(tmp_path: Path) -> None:
    caches = Caches(tmp_path)
    caches.register('sharded', shards=SHARDS, size_limit=SHARDS * MiB)
    real = caches.fanout('sharded')
    with real.transact(key='total') as tx:
        assert tx is real
        tx.incr('total')
    assert real.get('total') == 1
    caches.disable('sharded')
    off = caches.fanout('sharded')
    assert isinstance(off, NullCache)
    with off.transact(key='total') as tx:
        assert tx is off
    caches.close_all()


# -- a reference cycle ---------------------------------------------------------------------------------------

# A fresh process: a transaction that leaks keeps its gate level, which would hold up every later fork.
TRANSACTION_IN_A_CYCLE = """
import gc
import sys
import threading
import time
from diskcache_oxide import FanoutCache, Timeout

directory = sys.argv[1]
KEY = 'k'

def make():
    box = {}
    cache = FanoutCache(directory, shards=4, timeout=0.5, size_limit=4 << 20, clock=lambda: (box, time.time())[1])
    txn = cache.transact(key=KEY)
    txn.__enter__()
    cache.set(KEY, 1)
    box['txn'] = txn  # Transaction -> FanoutCache -> clock -> box -> Transaction.

make()
gc.collect()
gc.collect()
other = FanoutCache(directory, timeout=0.5)
assert other.get(KEY) is None, 'the collected transaction committed'
result = []

def write():
    try:
        result.append(other.set(KEY, 2))
    except Timeout:
        result.append('Timeout')

thread = threading.Thread(target=write)
thread.start()
thread.join()
assert result == [True], f'a write to the shard after collecting the cycle: {result}'
# The collected transaction left nothing behind: no shard is refused on this thread.
assert all(other.set(f'x{i}', i) for i in range(64)), 'a shard is still refused'
"""


def test_a_transaction_in_a_reference_cycle_is_collected_and_rolls_back(tmp_path: Path) -> None:
    proc = subprocess.run(
        [sys.executable, '-c', TRANSACTION_IN_A_CYCLE, str(tmp_path / 'f')],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr


# -- the process-wide count returns to zero ------------------------------------------------------------------

# A fresh process: the count is process-wide, so it can only be asserted where no other test runs.
HELD_STORES_RETURN_TO_ZERO = """
import gc
import os
import sys
import threading
import time
from pathlib import Path
from diskcache_oxide import Cache, FanoutCache, _core

root = Path(sys.argv[1])
held = _core._held_stores
WATCHDOG = 20.0

def zero(after):
    assert held() == 0, f'after {after}: {held()} stores still counted'

def wait_until(condition, what):
    end = time.monotonic() + WATCHDOG
    while not condition():
        assert time.monotonic() < end, f'{what}: never happened'
        time.sleep(0.001)

zero('start')
cache = Cache(root / 'c', pickle=True, timeout=10)
fan = FanoutCache(root / 'f', shards=4, timeout=10, size_limit=4 << 20)

# A commit and a rollback, on a Cache and on a shard.
with cache.transact():
    cache.set('a', 1)
    assert held() == 1
zero('a commit')
try:
    with fan.transact(key='a'):
        fan.set('a', 1)
        assert held() == 1
        raise KeyError
except KeyError:
    pass
zero('a rollback')

# A GC finalizer dropping the transaction inside its own operation: abandoned to that operation.
class Drops:
    txn = None
    def __init__(self):
        self.n = 1
    def __setstate__(self, state):
        self.__dict__.update(state)
        type(self).txn = None
        gc.collect()

cache.set('obj', Drops())
txn = cache.transact()
txn.__enter__()
Drops.txn = txn
del txn
assert cache.get('obj').n == 1
zero('an abandon inside an operation')

# Ended on another thread: a rollback, then a commit.
for exc in (ValueError, None):
    txn = fan.transact(key='b')
    txn.__enter__()
    fan.set('b', 1)
    done = []
    t = threading.Thread(target=lambda: done.append(txn.__exit__(exc, exc and exc(), None)))
    t.start()
    t.join(WATCHDOG)
    assert done == [False], done
    zero(f'a cross-thread {"rollback" if exc else "commit"}')
    try:
        txn.__exit__(None, None, None)
    except RuntimeError:
        assert exc is not None
    zero('the owner exiting after a cross-thread end')

if not hasattr(os, 'fork'):
    raise SystemExit(0)  # Windows: the last case needs fork; every case above ran.

# Parked and drained: the owner forks inside its block, the fork waits for another thread's
# transaction, and a third thread's rollback can't get in behind the owner's fork, so it parks; the
# drain at the end of the fork rolls it back.
other = Cache(root / 'other', timeout=10)
release_other = threading.Event()
other_in = threading.Event()

def hold_other():
    with other.transact():
        other.set('x', 1)
        other_in.set()
        release_other.wait(WATCHDOG)

holder = threading.Thread(target=hold_other)
holder.start()
other_in.wait(WATCHDOG)
owners = fan.transact(key='c')
entered = threading.Event()
forked = []

def owner():
    owners.__enter__()
    fan.set('c', 1)
    entered.set()
    pid = os.fork()
    if pid == 0:
        os._exit(0)
    forked.append(os.waitstatus_to_exitcode(os.waitpid(pid, 0)[1]))
    try:
        owners.__exit__(None, None, None)
        forked.append('committed')
    except RuntimeError:
        forked.append('aborted')

owner_thread = threading.Thread(target=owner)
owner_thread.start()
entered.wait(WATCHDOG)
wait_until(lambda: _core._gate_stats()['pending'], 'the owner claiming its fork')
assert owners.__exit__(ValueError, ValueError(), None) is False
assert _core._gate_stats()['deferred'], 'the rollback behind the owner\\'s fork was not parked'
assert held() >= 1, 'a parked transaction is still held until the drain'
release_other.set()
holder.join(WATCHDOG)
owner_thread.join(WATCHDOG)
assert forked == [0, 'aborted'], forked
wait_until(lambda: not _core._gate_stats()['deferred'], 'the drain')
zero('a park and its drain')
assert fan.get('c') is None, 'the parked transaction committed'
assert other.get('x') == 1
"""


def test_the_held_store_count_returns_to_zero_after_every_kind_of_end(tmp_path: Path) -> None:
    proc = subprocess.run(
        [sys.executable, '-c', HELD_STORES_RETURN_TO_ZERO, str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
