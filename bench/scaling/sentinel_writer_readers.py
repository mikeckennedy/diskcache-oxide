"""Plan 33's WAL check for the sentinel prototype: one writer process beside several pure readers.

Usage (a build with bench/results/read-sentinel-prototype-2026-11-24.diff applied; AC power):
    python bench/scaling/sentinel_writer_readers.py SECONDS
    GATE_EVERY=0 python bench/scaling/sentinel_writer_readers.py 15       # no forced checkpoints
    GATE_BUDGET=0.016 python bench/scaling/sentinel_writer_readers.py 8   # one forced round's budget

The readers call `get` in a loop and never write. The writer sets 1 KB values at a paced rate and,
every GATE_EVERY seconds (default 0.25; 0 for never), calls `checkpoint(timeout=GATE_BUDGET)`
(default 0.1). The parent samples the `-wal` file's size every 50 ms. One row for each mode: the
sentinel off, on, and on only after 64 or 512 operations in a row saw no change. A held read mark
caps what a checkpoint can copy and stops the log being reset, so the columns to read are the WAL's
size and how many forced checkpoints completed.
"""

import multiprocessing
import os
import random
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

KEYS = 1_000
SENTINEL = {'DCO_PROTO_SENTINEL': '1'}
MODES = (
    ('off', {}),
    ('sentinel', SENTINEL),
    ('sentinel, quiet 64', {**SENTINEL, 'DCO_PROTO_SENTINEL_QUIET': '64'}),
    ('sentinel, quiet 512', {**SENTINEL, 'DCO_PROTO_SENTINEL_QUIET': '512'}),
)
SHAPES = ((1, 1_000), (4, 1_000), (4, 100), (4, 10_000))  # (reader processes, writes a second asked)


def reader(directory: str, env: dict[str, str], go: Any, stop: Any, results: Any) -> None:
    os.environ.update(env)
    import diskcache_oxide

    cache = diskcache_oxide.Cache(directory)
    keys = [f't:{i}' for i in range(KEYS)]
    plan = [keys[random.randrange(KEYS)] for _ in range(1_000)]
    get = cache.get
    for key in keys:
        get(key)
    go.wait()
    reads = 0
    while not stop.is_set():
        for key in plan:
            get(key)
        reads += len(plan)
    results.put(('reader', reads, 0, 0))
    cache.close()


def writer(directory: str, env: dict[str, str], rate: int, go: Any, stop: Any, results: Any) -> None:
    os.environ.update(env)
    import diskcache_oxide

    every = float(os.environ.get('GATE_EVERY', '0.25'))
    budget = float(os.environ.get('GATE_BUDGET', '0.1'))
    cache = diskcache_oxide.Cache(directory)
    go.wait()
    writes = tries = wins = 0
    start = time.perf_counter()
    next_checkpoint = start + every if every else float('inf')
    while not stop.is_set():
        cache.set(f't:{writes % KEYS}', 'v' * 1_024)
        writes += 1
        now = time.perf_counter()
        if now >= next_checkpoint:
            tries += 1
            wins += bool(cache.checkpoint(timeout=budget))
            next_checkpoint = now + every
        ahead = start + writes / rate - time.perf_counter()
        if ahead > 0:
            time.sleep(ahead)
    results.put(('writer', writes, tries, wins))
    cache.close()


def run(mode: str, env: dict[str, str], readers: int, rate: int, seconds: float) -> None:
    import diskcache_oxide

    context = multiprocessing.get_context('spawn')
    directory = tempfile.mkdtemp(prefix='dco-sentinel-')
    cache = diskcache_oxide.Cache(directory)
    for i in range(KEYS):
        cache.set(f't:{i}', 'v' * 1_024)
    cache.close()
    go, stop, results = context.Event(), context.Event(), context.Queue()
    pool = [context.Process(target=reader, args=(directory, env, go, stop, results)) for _ in range(readers)]
    pool.append(context.Process(target=writer, args=(directory, env, rate, go, stop, results)))
    for process in pool:
        process.start()
    time.sleep(2.0)  # every worker open and warm
    go.set()
    start = time.perf_counter()
    sizes = []
    while time.perf_counter() - start < seconds:
        time.sleep(0.05)
        sizes.append(sum(path.stat().st_size for path in Path(directory).rglob('*-wal')))
    stop.set()
    got = [results.get(timeout=120) for _ in pool]
    elapsed = time.perf_counter() - start
    for process in pool:
        process.join()
    shutil.rmtree(directory, ignore_errors=True)
    reads = sum(g[1] for g in got if g[0] == 'reader')
    _, writes, tries, wins = next(g for g in got if g[0] == 'writer')
    cells = (
        mode,
        str(readers),
        f'{rate:,}',
        f'{reads / elapsed / 1_000:,.0f}k',
        f'{writes / elapsed:,.0f}',
        f'{max(sizes) // 1_024:,}',
        f'{sizes[-1] // 1_024:,}',
        f'{wins}/{tries}',
    )
    print('| ' + ' | '.join(cells) + ' |', flush=True)


def main() -> None:
    seconds = float(sys.argv[1]) if len(sys.argv) > 1 else 8.0
    head = ('Mode', 'Readers', 'Writes/s asked', 'Reads/s', 'Writes/s done', 'WAL peak KiB', 'WAL at end KiB', 'Won')
    print('| ' + ' | '.join(head) + ' |')
    print('|---' * len(head) + '|')
    for readers, rate in SHAPES:
        for mode, env in MODES:
            run(mode, env, readers, rate, seconds)


if __name__ == '__main__':
    main()
