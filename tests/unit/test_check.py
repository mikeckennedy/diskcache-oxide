"""`check()`, oxide-only (plan 24, release 2; D-050): under concurrent writes, and on a FanoutCache.

The findings themselves are tested in the core (`check::tests`): missing and short files, damaged
names, young and old orphans, drifted totals.
"""

from __future__ import annotations

import multiprocessing
import threading
from pathlib import Path
from typing import Any

from diskcache_oxide import Cache, FanoutCache, UnknownFileWarning

BIG = 100_000


def _writer(directory: str, rounds: int, done: Any) -> None:
    with Cache(directory) as cache:
        for i in range(rounds):
            # Overwrites of spilled values: the replaced file is unlinked after each commit.
            cache.set(f'k{i % 8}', bytes([i % 256]) * BIG)
    done.set()


def test_check_with_fix_alongside_a_writer_loses_nothing(tmp_path: Path) -> None:
    spawn = multiprocessing.get_context('spawn')
    done = spawn.Event()
    writer = spawn.Process(target=_writer, args=(str(tmp_path), 400, done))
    with Cache(tmp_path) as cache:
        cache.set('steady', b's' * BIG)
        writer.start()
        rounds = 0
        while not done.is_set():
            found = cache.check(fix=True)
            assert found == [], [str(w.message) for w in found]
            rounds += 1
        writer.join(60)
        assert writer.exitcode == 0, 'a write failed while check(fix=True) ran'
        assert rounds > 0
        assert cache.get('steady') == b's' * BIG
        for i in range(8):
            value = cache.get(f'k{i}')
            assert value is not None and len(value) == BIG
        assert cache.check() == []


def test_a_fanout_cache_checks_every_shard(tmp_path: Path) -> None:
    with FanoutCache(tmp_path, shards=3) as cache:
        for i in range(12):
            cache.set(f'k{i}', b'v' * BIG)
        assert cache.check() == []
        shard_dir = next(p for p in sorted(tmp_path.iterdir()) if p.is_dir())
        stray = next(p for p in shard_dir.iterdir() if p.is_dir()) / 'stray.val'
        stray.write_bytes(b'x')
        import os
        import time

        old = time.time() - 7200
        os.utime(stray, (old, old))
        [found] = cache.check()
        assert found.category is UnknownFileWarning and str(stray) in str(found.message)
        cache.check(fix=True)
        assert not stray.exists()


def test_check_is_refused_inside_a_transaction(tmp_path: Path) -> None:
    import pytest

    with Cache(tmp_path) as cache, cache.transact(), pytest.raises(RuntimeError):
        cache.check()


def test_check_can_run_from_many_threads(tmp_path: Path) -> None:
    with Cache(tmp_path) as cache:
        for i in range(20):
            cache.set(f'k{i}', b'v' * BIG)
        errors: list[BaseException] = []

        def run() -> None:
            try:
                for _ in range(5):
                    assert cache.check(fix=True) == []
            except BaseException as ex:
                errors.append(ex)

        threads = [threading.Thread(target=run) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)
        assert not errors, errors
        assert len(cache) == 20


def test_check_with_fix_is_refused_inside_another_caches_transaction(tmp_path: Path) -> None:
    import pytest

    with Cache(tmp_path / 'a') as a, Cache(tmp_path / 'b') as b:
        b.set('big', b'x' * BIG)
        next(p for p in (tmp_path / 'b').rglob('*.val')).unlink()
        with a.transact():
            assert b.check(), 'a plain check only reads, so it may run'
            with pytest.raises(RuntimeError, match='another cache'):
                b.check(fix=True)
        assert len(b) == 1, 'the refused fix changed nothing'


def _hold_a_spilled_write(directory: str, written: Any, release: Any) -> None:
    with Cache(directory) as cache, cache.transact():
        cache.set('pending', b'p' * BIG)
        written.set()
        release.wait(60)


def test_a_transactions_uncommitted_large_value_survives_check_with_fix(tmp_path: Path) -> None:
    # The value's file is in place before its row is committed: young, so check() leaves it alone.
    spawn = multiprocessing.get_context('spawn')
    written, release = spawn.Event(), spawn.Event()
    holder = spawn.Process(target=_hold_a_spilled_write, args=(str(tmp_path), written, release))
    with Cache(tmp_path, timeout=1) as cache:
        cache.set('k', 1)
        holder.start()
        assert written.wait(60)
        for _ in range(20):
            assert cache.check() == []
        release.set()
        holder.join(60)
        assert holder.exitcode == 0
        assert cache.get('pending') == b'p' * BIG
