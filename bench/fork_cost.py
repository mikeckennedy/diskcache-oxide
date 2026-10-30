"""What a fork costs the parent, with caches open (Phase 2.5, review 07).

    python -m bench.fork_cost [--caches 1] [--idle 8] [--forks 200]

Since `e817ba2` the before-fork hook closes every pool's idle connections, so the child inherits a
clean SQLite lock table. The parent pays twice: the fork itself (the hook's quiesce and closes), and
its next operations, which reopen connections. This opens `--caches` caches, warms each pool to
`--idle` idle connections with that many threads, then times `--forks` forks (`os.fork()` until it
returns in the parent; the child exits at once) and, after each, the parent's first `get` and first
`set` per cache. With `--caches 0` it times a fork with nothing open, for reference.
"""

from __future__ import annotations

import argparse
import os
import statistics
import sys
import tempfile
import threading
import time
from pathlib import Path


def warm(cache: object, idle: int) -> None:
    """Leaves `idle` connections in the pool: `idle` threads each read at the same time."""
    barrier = threading.Barrier(idle)

    def read() -> None:
        barrier.wait()
        for _ in range(50):
            cache.get('k')  # type: ignore[attr-defined]

    threads = [threading.Thread(target=read) for _ in range(idle)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()


def one_fork() -> float:
    started = time.perf_counter_ns()
    pid = os.fork()
    if pid == 0:
        os._exit(0)
    elapsed = time.perf_counter_ns() - started
    os.waitpid(pid, 0)
    return elapsed / 1_000


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--caches', type=int, default=1)
    parser.add_argument('--idle', type=int, default=8)
    parser.add_argument('--forks', type=int, default=200)
    args = parser.parse_args()

    from diskcache_oxide import Cache

    with tempfile.TemporaryDirectory() as tmp:
        caches = [Cache(Path(tmp) / f'c{i}', max_connections=max(args.idle, 1)) for i in range(args.caches)]
        for cache in caches:
            cache.set('k', 'v')
        fork_us: list[float] = []
        get_us: list[float] = []
        set_us: list[float] = []
        for _ in range(args.forks):
            for cache in caches:
                warm(cache, args.idle)
            fork_us.append(one_fork())
            for cache in caches:
                started = time.perf_counter_ns()
                cache.get('k')
                get_us.append((time.perf_counter_ns() - started) / 1_000)
                started = time.perf_counter_ns()
                cache.set('k', 'v')
                set_us.append((time.perf_counter_ns() - started) / 1_000)
        steady_get = []
        for _ in range(1_000):
            started = time.perf_counter_ns()
            caches[0].get('k') if caches else None
            steady_get.append((time.perf_counter_ns() - started) / 1_000)
        for cache in caches:
            cache.close()

    def row(name: str, values: list[float]) -> str:
        if not values:
            return f'{name:<28} -'
        q = statistics.quantiles(values, n=100)
        return f'{name:<28} p50 {statistics.median(values):9.1f} µs   p99 {q[98]:9.1f} µs'

    build = 'free-threaded' if not sys._is_gil_enabled() else 'GIL'
    print(f'Python {sys.version.split()[0]} ({build}), {args.caches} cache(s), {args.idle} idle connection(s) each, {args.forks} forks')
    print(row('fork (parent side)', fork_us))
    print(row('first get after the fork', get_us))
    print(row('first set after the fork', set_us))
    print(row('steady get, for reference', steady_get if caches else []))


if __name__ == '__main__':
    main()
