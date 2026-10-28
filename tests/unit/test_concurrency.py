"""Concurrency rules from 04-design.md section 10 that the conformance suite can't reach.

Fork scenarios run in a fresh process (tests/unit/fork_scenarios.py), never in pytest itself.
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from diskcache_oxide import Cache, Timeout
from tests.conformance.proc_workers import SqlitePeer

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize('scenario', ['d020', 'fork-while-a-write-waits', 'two-forkers'])
def test_fork_scenario(scenario: str, tmp_path: Path) -> None:
    proc = subprocess.run(
        [sys.executable, '-m', 'tests.unit.fork_scenarios', scenario, str(tmp_path / 'c')],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=50,
    )
    assert proc.returncode == 0, proc.stderr


# ---------------------------------------------------------------------------
# clear(): same-process fairness, and writes made while it runs survive (D-011)
# ---------------------------------------------------------------------------

CLEAR_KEYS = 50_000
CLEAR_ATTEMPTS = 3


def _writes_during_clear(cache: Cache, count: int) -> tuple[int, int, list[str]] | None:
    """Clear `count` keys on another thread while this one writes with the cache's 1 s timeout.

    Returns (removed, writes landed while clear() ran, the keys written), or None if clear()
    finished before a write could land. Every write must succeed: a same-process writer queues for
    the in-process write lock, and clear() yields to it between chunks.
    """
    removed: list[int] = []
    clearing = threading.Thread(target=lambda: removed.append(cache.clear()))
    clearing.start()
    deadline = time.monotonic() + 20
    while len(cache) == count and clearing.is_alive():
        assert time.monotonic() < deadline, 'clear() never started'
    written: list[str] = []
    landed = 0
    while clearing.is_alive():
        # Alternate new keys and overwrites of keys clear() may not have reached yet.
        key = f'new{len(written)}' if len(written) % 2 else f'k{count - 1 - len(written)}'
        assert cache.set(key, 'during') is True
        written.append(key)
        landed += clearing.is_alive()
    clearing.join()
    return (removed[0], landed, written) if landed else None


def test_clear_lets_same_process_writers_in_and_spares_their_writes(tmp_path: Path) -> None:
    _clear_fairness(tmp_path)


@pytest.mark.slow
@pytest.mark.timeout(600)
@pytest.mark.parametrize('run', range(20))
def test_clear_lets_same_process_writers_in_and_spares_their_writes_20x(tmp_path: Path, run: int) -> None:
    _clear_fairness(tmp_path)


def _clear_fairness(tmp_path: Path) -> None:
    cache = Cache(tmp_path / 'c', timeout=1.0)
    count = CLEAR_KEYS
    for _ in range(CLEAR_ATTEMPTS):
        cache.clear()
        for i in range(count):
            cache.set(f'k{i}', i)
        try:
            outcome = _writes_during_clear(cache, count)
        except Timeout:
            pytest.fail('a same-process write timed out during clear()')
        if outcome is not None:
            break
        count *= 2
    else:
        pytest.fail(f'clear() finished before any write could land, even with {count // 2} keys')

    removed, landed, written = outcome
    assert landed >= 1
    # Only rows stored before clear() started are removed; an overwrite during it is a new store.
    overwrites = sum(1 for key in written if key.startswith('k'))
    assert count - overwrites <= removed <= count
    survivors = set(cache.iterkeys())
    assert survivors == set(written), f'{len(set(written) - survivors)} writes made during clear() were lost'
    assert all(cache.get(key) == 'during' for key in written)
    cache.close()


# ---------------------------------------------------------------------------
# Detached busy retry: waiting for another process's lock doesn't hold the interpreter
# ---------------------------------------------------------------------------


def test_waiting_for_another_processs_lock_lets_other_threads_run(tmp_path: Path) -> None:
    cache = Cache(tmp_path / 'c', timeout=5.0)
    cache.set('k', 'v')
    gaps: list[float] = []
    stop = threading.Event()

    def ticker() -> None:
        last = time.perf_counter()
        while not stop.is_set():
            time.sleep(0.002)
            now = time.perf_counter()
            gaps.append(now - last)
            last = now

    with SqlitePeer(tmp_path / 'c' / 'diskcache-oxide.sqlite') as peer:
        peer.begin('IMMEDIATE')
        writer = threading.Thread(target=lambda: cache.set('k', 'w'))
        writer.start()
        time.sleep(0.1)  # The write is now waiting for the peer.
        tick = threading.Thread(target=ticker)
        tick.start()
        time.sleep(0.6)
        assert writer.is_alive(), 'the write should still be waiting for the peer'
        peer.rollback()
        writer.join(5)
        stop.set()
        tick.join()

    assert cache.get('k') == 'w'
    # With the interpreter held for the whole wait, the ticker would stall for about 0.6 s.
    assert max(gaps) < 0.1, f'another thread stalled for {max(gaps):.3f}s while a write waited'
    cache.close()
