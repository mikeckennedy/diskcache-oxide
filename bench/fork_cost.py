"""What a fork costs the parent, with caches open (Phase 2.5, review 07).

    python -m bench.fork_cost [--caches 1] [--idle 8] [--forks 200] [--rss-mib 0] [--load-threads 0]

Since `e817ba2` the before-fork hook closes every pool's idle connections, so the child inherits a
clean SQLite lock table. The parent pays twice: the fork itself (the hook's quiesce and closes), and
its next operations, which reopen connections. This opens `--caches` caches, warms each pool to
`--idle` idle connections with that many threads, then times `--forks` forks (`os.fork()` until it
returns in the parent; the child exits at once) and, after each, the parent's first `get` and first
`set` per cache. With `--caches 0` it times a fork with nothing open, for reference.

Warming holds `--idle` leases at once: each thread reads a pickled value whose unpickling sleeps
5 ms, which keeps its connection while the interpreter is free. Plain reads overlap only on a
free-threaded build; on a GIL build the pool would stay at one connection. The idle count is checked
from the process's open files (2 per connection). The WAL's size at the fork is reported too: with
no other process on the file, the cost of a fork used to depend on it (953b7f5).

`--rss-mib` grows the process until its anonymous resident memory reaches that target, to time a fork
at a production worker's size: a fork copies the parent's page tables, so its cost grows with
resident memory. Pass a real worker's `RssAnon` (from `/proc/<pid>/status`): the ballast is all
anonymous memory, and file-backed pages don't cost a fork the same way. On macOS, which has no
`RssAnon`, it uses the process's whole resident size. The ballast is half 1 MiB buffers and half
short strs, since an app's heap is mostly small objects.

`--load-threads` runs that many threads doing get and set on the first cache throughout the forks,
and reports their calls that overlapped a fork against those that didn't. Every cache call in the
process waits for a pending fork, so the overlapping calls' worst case is what requests feel. The
load threads share the pool with the warm-up, so "warming added" can read low with them.

Not modeled: a child that lives on, so the parent pays real copy-on-write copies (the child here
exits at once), and the write faults the parent takes afterwards as refcounts touch pages the fork
write-protected.
"""

from __future__ import annotations

import argparse
import bisect
import os
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any


class Slow:
    """Unpickling it holds the reader's connection for 5 ms, with the interpreter free."""

    def __init__(self) -> None:
        self.n = 1  # Some state, or pickle never calls __setstate__.

    def __setstate__(self, state: dict[str, int]) -> None:
        self.__dict__.update(state)
        time.sleep(0.005)


def open_fds() -> int:
    return len(os.listdir('/dev/fd'))


def warm(cache: Any, idle: int) -> None:
    """Leaves `idle` connections in the pool: `idle` threads hold a lease at the same time."""
    barrier = threading.Barrier(idle)

    def read() -> None:
        barrier.wait()
        cache.get('slow')

    threads = [threading.Thread(target=read) for _ in range(idle)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()


def rss_mib() -> float:
    """The process's anonymous resident memory on Linux (`RssAnon`), its whole resident size elsewhere."""
    try:
        with open('/proc/self/status') as status:
            for line in status:
                if line.startswith('RssAnon:'):
                    return int(line.split()[1]) / 2**10
    except OSError:
        pass
    out = subprocess.run(['ps', '-o', 'rss=', '-p', str(os.getpid())], capture_output=True, text=True, check=True)
    return int(out.stdout) / 2**10


def grow(target_mib: int) -> list[object]:
    """Ballast until `rss_mib()` reaches `target_mib`, alternating 1 MiB buffers and 1 MiB of short strs."""
    ballast: list[object] = []
    # A str's real size on this build (it differs between GIL and free-threaded), plus its list slot.
    per_str = sys.getsizeof(str(10**6)) + 8
    step = 0
    while rss_mib() < target_mib:
        for _ in range(16):  # 16 MiB between checks: reading the RSS isn't free.
            if step % 2 == 0:
                ballast.append(bytearray(2**20))  # Zero-filled as it's made, so already resident.
            else:
                ballast.append([str(10**6 + i) for i in range(2**20 // per_str)])
            step += 1
    return ballast


class Load:
    """Threads doing get and set on one cache, each call timed with when it started."""

    def __init__(self, cache: Any, threads: int) -> None:
        self.cache = cache
        self.stop = threading.Event()
        self.calls: list[list[tuple[int, int]]] = [[] for _ in range(threads)]
        self.threads = [threading.Thread(target=self.run, args=(i,)) for i in range(threads)]

    def run(self, index: int) -> None:
        calls = self.calls[index]
        n = 0
        while not self.stop.is_set():
            started = time.perf_counter_ns()
            if n % 10:
                self.cache.get(f'load{n % 1000}')
            else:
                self.cache.set(f'load{n % 1000}', 'v' * 100)
            calls.append((started, time.perf_counter_ns()))
            n += 1

    def __enter__(self) -> Load:
        for t in self.threads:
            t.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop.set()
        for t in self.threads:
            t.join()

    def split(self, forks: list[tuple[int, int]]) -> tuple[list[float], list[float]]:
        """The calls' latencies in µs: those that overlapped a fork, and the rest."""
        starts = [f[0] for f in forks]
        during: list[float] = []
        outside: list[float] = []
        for started, ended in (c for calls in self.calls for c in calls):
            # The last fork that started before this call ended is the only one it can overlap.
            i = bisect.bisect_left(starts, ended) - 1
            overlaps = i >= 0 and forks[i][1] > started
            (during if overlaps else outside).append((ended - started) / 1_000)
        return during, outside


def one_fork() -> tuple[int, int]:
    """When `os.fork()` started and returned in the parent."""
    started = time.perf_counter_ns()
    pid = os.fork()
    if pid == 0:
        os._exit(0)
    ended = time.perf_counter_ns()
    os.waitpid(pid, 0)
    return started, ended


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--caches', type=int, default=1)
    parser.add_argument('--idle', type=int, default=8)
    parser.add_argument('--forks', type=int, default=200)
    parser.add_argument('--rss-mib', type=int, default=0, help='grow until anonymous RSS reaches this many MiB')
    parser.add_argument('--load-threads', type=int, default=0, help='threads doing get/set during the forks')
    args = parser.parse_args()
    ballast = grow(args.rss_mib)

    from diskcache_oxide import Cache

    with tempfile.TemporaryDirectory() as tmp:
        caches = [
            Cache(Path(tmp) / f'c{i}', max_connections=max(args.idle, 1), pickle=True) for i in range(args.caches)
        ]
        for cache in caches:
            cache.set('k', 'v')
            cache.set('slow', Slow())
            for i in range(2_000):  # A WAL like a working cache's.
                cache.set(f'fill{i}', 'x' * 500)
        base = open_fds()
        for cache in caches:
            warm(cache, args.idle)
        extra = (open_fds() - base) // 2 if caches else 0
        wal = sum((Path(tmp) / f'c{i}' / 'diskcache-oxide.sqlite-wal').stat().st_size for i in range(args.caches))
        rss_at_fork = rss_mib()
        forks: list[tuple[int, int]] = []
        get_us: list[float] = []
        set_us: list[float] = []
        with Load(caches[0] if caches else None, args.load_threads if caches else 0) as load:
            for _ in range(args.forks):
                for cache in caches:
                    warm(cache, args.idle)
                forks.append(one_fork())
                for cache in caches:
                    started = time.perf_counter_ns()
                    cache.get('k')
                    get_us.append((time.perf_counter_ns() - started) / 1_000)
                    started = time.perf_counter_ns()
                    cache.set('k', 'v')
                    set_us.append((time.perf_counter_ns() - started) / 1_000)
        fork_us = [(ended - started) / 1_000 for started, ended in forks]
        load_during, load_outside = load.split(forks)
        steady_get = []
        for _ in range(1_000):
            started = time.perf_counter_ns()
            caches[0].get('k') if caches else None
            steady_get.append((time.perf_counter_ns() - started) / 1_000)
        for cache in caches:
            cache.close()

    def row(name: str, values: list[float]) -> str:
        if not values:
            return f'{name:<32} -'
        p99 = statistics.quantiles(values, n=100, method='inclusive')[98] if len(values) > 1 else values[0]
        return f'{name:<32} p50 {statistics.median(values):9.1f} µs   p99 {p99:9.1f} µs   max {max(values):9.1f} µs'

    build = 'free-threaded' if not sys._is_gil_enabled() else 'GIL'
    print(
        f'Python {sys.version.split()[0]} ({build}), {args.caches} cache(s), {args.idle} idle connection(s) each, '
        f'{args.forks} forks, {args.load_threads} load thread(s), RSS at the first fork {rss_at_fork:.0f} MiB'
    )
    print(
        f'warming added {extra} connection(s) in all (expected {args.caches * max(args.idle - 1, 0)}); WAL before the '
        f'first fork: {wal / 2**20:.1f} MiB'
    )
    print(row('fork (parent side)', fork_us))
    print(row('first get after the fork', get_us))
    print(row('first set after the fork', set_us))
    print(row('steady get, for reference', steady_get if caches else []))
    if args.load_threads:
        print(f'load calls: {len(load_during)} overlapped a fork, {len(load_outside)} did not')
        print(row('load call overlapping a fork', load_during))
        print(row('load call, no fork', load_outside))
    del ballast


if __name__ == '__main__':
    main()
