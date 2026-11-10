"""`Cache.transact()`: oxide's own behavior beyond what diskcache shares (D-020, D-029 to D-031).

The shared semantics (commit, rollback, nesting) are in tests/conformance/test_transact.py. Fork
scenarios and the cross-thread ends that hold the interpreter if they go wrong run in a fresh
process, never in pytest itself.
"""

from __future__ import annotations

import _thread
import asyncio
import gc
import pickle
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from diskcache_oxide import Cache, Timeout, TransactionAborted, TransactionBusy
from diskcache_oxide._core import Transaction

from tests.unit.test_durable import BIG, NOT_AS_ROOT, SHORT, _value_file, peer_holding_the_write_lock, unreadable

REPO_ROOT = Path(__file__).resolve().parents[2]
WATCHDOG = 20.0


def _read_on_another_thread(cache: Cache, key: str) -> Any:
    found: list[Any] = []
    thread = threading.Thread(target=lambda: found.append(cache.get(key)), daemon=True)
    thread.start()
    thread.join(WATCHDOG)
    assert found, 'the reading thread never returned'
    return found[0]


@pytest.fixture
def cache(tmp_path: Path) -> Iterator[Cache]:
    with Cache(tmp_path / 'c', timeout=10) as cache:
        yield cache


# -- the handle --------------------------------------------------------------------------------------------


def test_the_block_gets_the_cache_itself(cache: Cache) -> None:
    txn = cache.transact()
    assert isinstance(txn, Transaction)
    with txn as tx:
        assert tx is cache
        tx.set('k', 1)
    assert cache.get('k') == 1


def test_a_nested_block_gets_the_cache_too(cache: Cache) -> None:
    with cache.transact(), cache.transact() as inner:
        assert inner is cache


def test_a_second_handle_on_the_directory_passes_through(tmp_path: Path) -> None:
    first = Cache(tmp_path / 'c', timeout=10)
    second = Cache(tmp_path / 'c', timeout=10)
    started = time.monotonic()
    with first.transact():
        first.set('outer', 1)
        with second.transact() as tx:  # Would wait out the timeout behind the outer one if it began its own.
            assert tx is second
            second.set('inner', 2)
        assert _read_on_another_thread(first, 'inner') is None, "the second handle's exit committed"
    assert time.monotonic() - started < 5
    assert (first.get('outer'), first.get('inner')) == (1, 2)
    first.close()
    second.close()


def test_a_second_store_doesnt_pass_through(tmp_path: Path) -> None:
    a = Cache(tmp_path / 'a', timeout=10)
    b = Cache(tmp_path / 'b', timeout=10)
    with a.transact():
        with b.transact():
            b.set('k', 1)
        assert _read_on_another_thread(b, 'k') == 1, "b's own transaction committed at its exit"
    a.close()
    b.close()


# -- misuse ------------------------------------------------------------------------------------------------


def test_reentering_a_transaction_raises(cache: Cache) -> None:
    txn = cache.transact()
    with txn:
        with pytest.raises(RuntimeError, match='only once'):
            txn.__enter__()
    with pytest.raises(RuntimeError, match='only once'):
        txn.__enter__()


def test_exiting_a_transaction_that_was_never_entered_raises(cache: Cache) -> None:
    with pytest.raises(RuntimeError, match='never entered'):
        cache.transact().__exit__(None, None, None)


def test_misuse_is_a_plain_runtime_error_not_transaction_aborted(cache: Cache) -> None:
    # TransactionAborted means "your writes are gone"; a programming error is a plain RuntimeError.
    txn = cache.transact()
    with pytest.raises(RuntimeError, match='never entered') as never_entered:
        txn.__exit__(None, None, None)
    with txn, pytest.raises(RuntimeError, match='only once') as reentered:
        txn.__enter__()
    assert not isinstance(never_entered.value, TransactionAborted)
    assert not isinstance(reentered.value, TransactionAborted)


def test_transaction_aborted_is_a_runtime_error_that_pickles_under_the_package_name() -> None:
    error = TransactionAborted('the transaction did not commit')
    assert isinstance(error, RuntimeError)
    assert TransactionAborted.__module__ == 'diskcache_oxide'
    copy = pickle.loads(pickle.dumps(error))
    assert type(copy) is TransactionAborted
    assert copy.args == error.args


@pytest.mark.parametrize('retry', [1, 0, None, 'yes'])
def test_a_retry_that_isnt_a_bool_raises(cache: Cache, retry: object) -> None:
    with pytest.raises(TypeError):
        cache.transact(retry=retry)  # ty: ignore[invalid-argument-type]


def test_a_concurrent_enter_raises(cache: Cache) -> None:
    # One thread's __enter__ waits for the write lock, held by another thread's transaction. The
    # same Transaction entered (or exited) meanwhile must not begin a second one.
    txn = cache.transact()
    holding, release = threading.Event(), threading.Event()
    entered: list[Any] = []

    def holder() -> None:
        with cache.transact():
            holding.set()
            release.wait(WATCHDOG)

    def enterer() -> None:
        entered.append(txn.__enter__())

    hold = threading.Thread(target=holder, daemon=True)
    hold.start()
    assert holding.wait(WATCHDOG)
    enter = threading.Thread(target=enterer, daemon=True)
    enter.start()
    time.sleep(0.2)  # The enterer is now waiting for the write lock.
    try:
        with pytest.raises(RuntimeError, match='being entered'):
            txn.__enter__()
        with pytest.raises(RuntimeError, match='being entered'):
            txn.__exit__(None, None, None)
    finally:
        release.set()
        hold.join(WATCHDOG)
        enter.join(WATCHDOG)
    assert entered == [cache]
    assert txn.__exit__(None, None, None) is False  # Ends the enterer's transaction, from this thread.
    assert cache.set('k', 1) is True, 'the write lock was not released'


def test_an_exit_after_a_commit_succeeds_again(cache: Cache) -> None:
    txn = cache.transact()
    with txn:
        cache.set('k', 1)
    assert txn.__exit__(None, None, None) is False
    assert txn.__exit__(KeyError, KeyError('k'), None) is False


def test_an_exit_after_a_rollback_raises_unless_an_exception_is_passing(cache: Cache) -> None:
    txn = cache.transact()
    with pytest.raises(KeyError), txn:
        cache.set('k', 1)
        raise KeyError('k')
    with pytest.raises(TransactionAborted, match='did not commit'):
        txn.__exit__(None, None, None)
    assert txn.__exit__(KeyError, KeyError('k'), None) is False
    assert cache.get('k') is None


def test_a_failed_enter_can_be_retried(tmp_path: Path) -> None:
    cache = Cache(tmp_path / 'c', timeout=SHORT)
    cache.set('k', 1)
    txn = cache.transact()
    with peer_holding_the_write_lock(tmp_path / 'c'), pytest.raises(Timeout):
        txn.__enter__()
    with txn:
        cache.set('k', 2)
    assert cache.get('k') == 2
    cache.close()


# -- retry --------------------------------------------------------------------------------------------------


def test_retry_waits_out_timeouts_until_it_begins(tmp_path: Path) -> None:
    cache = Cache(tmp_path / 'c', timeout=SHORT)
    holding = threading.Event()

    def holder() -> None:
        with cache.transact():
            holding.set()
            time.sleep(0.5)  # Ten of this cache's timeouts.

    thread = threading.Thread(target=holder, daemon=True)
    thread.start()
    assert holding.wait(WATCHDOG)
    with pytest.raises(Timeout):
        cache.transact().__enter__()
    with cache.transact(retry=True):
        cache.set('k', 1)
    thread.join(WATCHDOG)
    assert cache.get('k') == 1
    cache.close()


def test_retry_is_interruptible_between_attempts(tmp_path: Path) -> None:
    cache = Cache(tmp_path / 'c', timeout=SHORT)
    cache.set('k', 1)
    txn = cache.transact(retry=True)
    # A pytest started as a background job of a non-interactive shell (`nohup scripts/check.sh &`)
    # inherits SIGINT ignored, Python keeps an inherited SIG_IGN, and `interrupt_main` does nothing for
    # an ignored signal, so the test waited out pytest's timeout (seen twice on the mini, 2026-11-07).
    # Python's own handler for the test's duration, as an interactive run has it.
    previous = signal.signal(signal.SIGINT, signal.default_int_handler)
    try:
        with peer_holding_the_write_lock(tmp_path / 'c'):
            timer = threading.Timer(0.3, _thread.interrupt_main)
            timer.start()
            started = time.monotonic()
            with pytest.raises(KeyboardInterrupt):
                txn.__enter__()
            assert time.monotonic() - started < 5
            timer.join()
    finally:
        signal.signal(signal.SIGINT, previous)
    assert 'Ready' in repr(txn)
    with txn:
        cache.set('k', 2)
    assert cache.get('k') == 2
    cache.close()


def test_retry_never_retries_the_cross_cache_refusal(tmp_path: Path) -> None:
    a = Cache(tmp_path / 'a', timeout=10)
    b = Cache(tmp_path / 'b', timeout=10)
    holding, release = threading.Event(), threading.Event()

    def hold_b() -> None:
        with b.transact():
            holding.set()
            release.wait(WATCHDOG)

    holder = threading.Thread(target=hold_b, daemon=True)
    holder.start()
    try:
        assert holding.wait(WATCHDOG)
        with a.transact():
            started = time.monotonic()
            with pytest.raises(TransactionBusy), b.transact(retry=True):
                pass
            assert time.monotonic() - started < 1, 'the refusal was retried'
            a.set('k', 1)
    finally:
        release.set()
        holder.join(WATCHDOG)
    assert a.get('k') == 1
    a.close()
    b.close()


# -- forks (D-020) -------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    'scenario',
    ['transact-fork-from-the-owner', 'transact-fork-from-another-thread', 'transact-inherited-mid-operation'],
)
def test_fork_scenario(scenario: str, tmp_path: Path) -> None:
    proc = subprocess.run(
        [sys.executable, '-m', 'tests.unit.fork_scenarios', scenario, str(tmp_path / 'c')],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=50,
    )
    assert proc.returncode == 0, proc.stderr


# -- ending it from another thread (D-031) -------------------------------------------------------------------

# The owner is inside a get() whose pickle hook sleeps with the interpreter released, while another
# thread ends the owner's transaction. Run in a fresh process: a wait that held the interpreter
# would hang it.
ENDED_ELSEWHERE_MID_OPERATION = """
import sys
import threading
import time
from diskcache_oxide import Cache, Timeout, TransactionAborted

mode, directory = sys.argv[1], sys.argv[2]
HOOK_SECONDS = 1.0 if mode == 'timeout' else 0.3
inside = threading.Event()

class Slow:
    def __init__(self):
        self.n = 1

    def __setstate__(self, state):
        self.__dict__.update(state)
        inside.set()
        time.sleep(HOOK_SECONDS)

CACHE = Cache(directory, pickle=True, timeout=0.2 if mode == 'timeout' else 10)
CACHE.set('slow', Slow())
TXN = CACHE.transact()
ended = []

def end():
    inside.wait()
    started = time.monotonic()
    try:
        if mode == 'rollback':
            ended.append(TXN.__exit__(ValueError, ValueError('x'), None))
        else:
            ended.append(TXN.__exit__(None, None, None))
    except Timeout:
        ended.append('timeout')
    ended.append(time.monotonic() - started)

ender = threading.Thread(target=end)
ender.start()
TXN.__enter__()
CACHE.set('in-tx', 1)
assert CACHE.get('slow').n == 1
ender.join()  # The owner's own exit comes after the other thread's end has returned.
outcome = 'exited'
try:
    TXN.__exit__(None, None, None)
except TransactionAborted as e:
    assert 'did not commit' in str(e), e
    outcome = 'aborted'
result, took = ended
if mode == 'commit':
    assert (result, outcome) == (False, 'exited'), (result, outcome)
    assert took >= 0.1, f'the commit returned after {took:.2f}s, before the operation handed the connection back'
    assert CACHE.get('in-tx') == 1, 'the commit from the other thread did not commit'
elif mode == 'rollback':
    assert (result, outcome) == (False, 'aborted'), (result, outcome)
    assert took < 0.2, f'the rollback waited {took:.2f}s for the operation'
    assert CACHE.get('in-tx') is None, 'the rolled-back write survived'
else:
    assert (result, outcome) == ('timeout', 'aborted'), (result, outcome)
    assert took < 0.8, f'the commit took {took:.2f}s, past its 0.2 s timeout'
    assert CACHE.get('in-tx') is None, 'the timed-out commit committed'
assert CACHE.set('after', 1) is True, 'the write lock was not released'
"""


@pytest.mark.parametrize('mode', ['commit', 'rollback', 'timeout'])
def test_an_end_from_another_thread_while_the_owner_is_inside_an_operation(mode: str, tmp_path: Path) -> None:
    proc = subprocess.run(
        [sys.executable, '-c', ENDED_ELSEWHERE_MID_OPERATION, mode, str(tmp_path / 'c')],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr


# The main D-031 sequence: the owner leaves its block as soon as its operation returns, while the
# other thread's commit, which waited for that operation, is still finishing. The owner's exit must
# wait for that commit's outcome, not report a rollback.
OWNER_EXITS_WHILE_ANOTHER_THREAD_COMMITS = """
import sys
import tempfile
import threading
import time
from diskcache_oxide import Cache

inside = threading.Event()

class Slow:
    def __init__(self):
        self.n = 1

    def __setstate__(self, state):
        self.__dict__.update(state)
        inside.set()
        time.sleep(0.2)

for run in range(10):
    inside.clear()
    cache = Cache(tempfile.mkdtemp(dir=sys.argv[1]), pickle=True, timeout=10)
    cache.set('slow', Slow())
    txn = cache.transact()
    ended = []

    def end():
        inside.wait()
        ended.append(txn.__exit__(None, None, None))

    ender = threading.Thread(target=end)
    ender.start()
    with txn:
        cache.set('in-tx', 1)
        assert cache.get('slow').n == 1
    ender.join()
    assert ended == [False], ended
    assert cache.get('in-tx') == 1, 'the commit from the other thread did not commit'
    cache.close()
"""


def test_the_owners_exit_waits_for_a_commit_another_thread_is_finishing(tmp_path: Path) -> None:
    proc = subprocess.run(
        [sys.executable, '-c', OWNER_EXITS_WHILE_ANOTHER_THREAD_COMMITS, str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr


def test_an_exit_with_an_exception_doesnt_wait_for_an_end_elsewhere(tmp_path: Path) -> None:
    # The other thread's commit waits for the owner's operation; the owner's hook leaves with an
    # exception meanwhile, which must not wait for that commit (it waits for the hook).
    cache = Cache(tmp_path / 'c', pickle=True, timeout=10)
    cache.set('obj', _ExitsWithAnException())
    txn = cache.transact()
    _ExitsWithAnException.txn = txn
    _ExitsWithAnException.ending = threading.Event()
    ended: list[object] = []
    with txn:
        cache.set('in-tx', 1)

        def end() -> None:
            ended.append(txn.__exit__(None, None, None))

        _ExitsWithAnException.ender = threading.Thread(target=end, daemon=True)
        assert cache.get('obj').n == 1
    _ExitsWithAnException.ender.join(WATCHDOG)
    assert _ExitsWithAnException.results == [False], _ExitsWithAnException.results
    assert ended == [False]
    assert cache.get('in-tx') == 1
    cache.close()


class _ExitsWithAnException:
    """Unpickling it starts a commit on another thread, then exits the transaction with an exception."""

    txn: Transaction | None = None
    ender: threading.Thread
    ending: threading.Event
    results: list[object] = []

    def __init__(self) -> None:
        self.n = 1

    def __setstate__(self, state: dict[str, object]) -> None:
        self.__dict__.update(state)
        cls = type(self)
        cls.ender.start()
        time.sleep(0.1)  # The commit is now waiting for this operation.
        assert cls.txn is not None
        started = time.monotonic()
        cls.results.append(cls.txn.__exit__(ValueError, ValueError(), None))
        assert time.monotonic() - started < 1, 'the exit with an exception waited for the commit'


def test_the_owners_exit_after_a_rollback_elsewhere_raises(cache: Cache) -> None:
    txn = cache.transact()
    rolled_back: list[bool] = []
    with pytest.raises(TransactionAborted, match='did not commit'), txn:
        cache.set('k', 1)
        thread = threading.Thread(target=lambda: rolled_back.append(txn.__exit__(ValueError, ValueError(), None)))
        thread.start()
        thread.join(WATCHDOG)
        assert rolled_back == [False]
    assert cache.get('k') is None


def test_the_owners_exit_after_a_commit_elsewhere_succeeds(cache: Cache) -> None:
    txn = cache.transact()
    with txn:
        cache.set('k', 1)
        thread = threading.Thread(target=lambda: txn.__exit__(None, None, None))
        thread.start()
        thread.join(WATCHDOG)
        assert _read_on_another_thread(cache, 'k') == 1, 'the other thread did not commit'
    assert cache.get('k') == 1


# -- dropped inside one of its own operations ----------------------------------------------------------------


class _DropsTheTransaction:
    """Unpickling it drops the last reference to a manually entered Transaction, inside the get()."""

    txn: Transaction | None = None

    def __init__(self) -> None:
        self.n = 1

    def __setstate__(self, state: dict[str, object]) -> None:
        self.__dict__.update(state)
        type(self).txn = None
        gc.collect()


def test_a_transaction_dropped_inside_its_own_operation_rolls_back_when_it_returns(tmp_path: Path) -> None:
    cache = Cache(tmp_path / 'c', pickle=True, timeout=10)
    cache.set('obj', _DropsTheTransaction())
    txn = cache.transact()
    txn.__enter__()
    cache.set('inside', 1)
    _DropsTheTransaction.txn = txn
    del txn
    assert cache.get('obj').n == 1
    assert cache.get('inside') is None, 'the dropped transaction committed'
    assert _read_on_another_thread(cache, 'inside') is None
    writer = threading.Thread(target=lambda: cache.set('after', 1), daemon=True)
    writer.start()
    writer.join(WATCHDOG)
    assert cache.get('after') == 1, 'the write lock was not released'
    cache.close()


class _ExitsItsOwnTransaction:
    """Unpickling it exits the transaction from inside the get() that decodes it."""

    txn: Transaction | None = None
    refused: list[str] = []

    def __init__(self) -> None:
        self.n = 1

    def __setstate__(self, state: dict[str, object]) -> None:
        self.__dict__.update(state)
        txn = type(self).txn
        assert txn is not None
        try:
            txn.__exit__(None, None, None)
        except RuntimeError as e:
            type(self).refused.append(str(e))


def test_an_exit_from_inside_its_own_operation_raises_and_the_block_then_aborts(tmp_path: Path) -> None:
    cache = Cache(tmp_path / 'c', pickle=True, timeout=10)
    cache.set('obj', _ExitsItsOwnTransaction())
    txn = cache.transact()
    _ExitsItsOwnTransaction.txn = txn
    with pytest.raises(TransactionAborted, match='did not commit'), txn:
        cache.set('inside', 1)
        assert cache.get('obj').n == 1
    assert len(_ExitsItsOwnTransaction.refused) == 1
    assert "can't end inside one of its own operations" in _ExitsItsOwnTransaction.refused[0]
    _ExitsItsOwnTransaction.txn = None
    assert cache.get('inside') is None
    writer = threading.Thread(target=lambda: cache.set('after', 1), daemon=True)
    writer.start()
    writer.join(WATCHDOG)
    assert cache.get('after') == 1, 'the write lock was not released'
    cache.close()


# A fresh process: a transaction that leaks keeps its gate level, which would hold up every later fork.
TRANSACTION_IN_A_CYCLE = """
import gc
import sys
import threading
import time
from diskcache_oxide import Cache, Timeout

directory = sys.argv[1]

def make():
    box = {}
    cache = Cache(directory, timeout=0.5, clock=lambda: (box, time.time())[1])
    txn = cache.transact()
    txn.__enter__()
    cache.set('k', 1)
    box['txn'] = txn  # Transaction -> Cache -> clock -> box -> Transaction.

make()
gc.collect()
gc.collect()
other = Cache(directory, timeout=0.5)
result = []

def write():
    try:
        result.append(other.set('x', 1))
    except Timeout:
        result.append('Timeout')

thread = threading.Thread(target=write)
thread.start()
thread.join()
assert result == [True], f'a write after collecting the cycle: {result}'
assert other.get('k') is None, 'the collected transaction committed'
"""


def test_a_transaction_in_a_reference_cycle_is_collected_and_rolls_back(tmp_path: Path) -> None:
    proc = subprocess.run(
        [sys.executable, '-c', TRANSACTION_IN_A_CYCLE, str(tmp_path / 'c')],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr


# -- durable=True (D-028) ------------------------------------------------------------------------------------


def test_a_durable_handles_begin_that_times_out_raises(tmp_path: Path) -> None:
    cache = Cache(tmp_path / 'c', timeout=SHORT, durable=True)
    cache.set('k', 1)
    with peer_holding_the_write_lock(tmp_path / 'c'):
        with pytest.raises(Timeout):
            cache.transact().__enter__()
        with pytest.raises(Timeout), cache.transact():
            pass
    cache.close()


@NOT_AS_ROOT
def test_a_failure_inside_a_durable_block_raises(tmp_path: Path) -> None:
    cache = Cache(tmp_path / 'c', durable=True)
    cache.set('k', b'x' * BIG)
    with pytest.raises(PermissionError), cache.transact():
        cache.set('inside', 1)
        with unreadable(_value_file(tmp_path / 'c')):
            cache.get('k')
    assert cache.get('inside') is None
    cache.close()


# -- a thread, not a task -------------------------------------------------------------------------------------


def test_another_coroutines_write_during_an_await_rolls_back_with_the_block(cache: Cache) -> None:
    async def other() -> None:
        cache.set('other', 1)

    async def main() -> None:
        with pytest.raises(ValueError), cache.transact():
            cache.set('mine', 1)
            await asyncio.create_task(other())
            raise ValueError

    asyncio.run(main())
    assert (cache.get('mine'), cache.get('other')) == (None, None)
