"""What a maintenance call pays per chunk while other threads keep the interpreter busy (Phase 2.5).

    python -m bench.reattach [--rows 20000] [--writers 2] [--readers 2] [--rounds 5]

`clear()` runs detached, in chunks of 100 rows, and between chunks it waits (pauses, yields to a
waiting writer, re-takes the write lock) through the gate's blocker. On a GIL build, a blocker that
re-attaches the interpreter on an already-detached thread first has to win the GIL back from the
busy threads, up to the 5 ms switch interval per wait. This times `clear()` of `--rows` rows with
writer threads (tight `set()` loops, so the clear yields every chunk) and reader threads (tight
`get()` loops) running, and reports the time per chunk.
"""

from __future__ import annotations

import argparse
import statistics
import sys
import tempfile
import threading
import time
from pathlib import Path

CHUNK = 100


def one_round(directory: Path, rows: int, writers: int, readers: int) -> tuple[float, float]:
    from diskcache_oxide import Cache

    cache = Cache(directory, size_limit=2**34)
    for i in range(rows):
        cache.set(f'k{i}', i)
    stop = threading.Event()
    writes = [0] * writers

    def write(n: int) -> None:
        i = 0
        while not stop.is_set():
            cache.set(f'w{n}-{i % 50}', i)
            i += 1
        writes[n] = i

    def read() -> None:
        i = 0
        while not stop.is_set():
            cache.get(f'k{i % 1000}')
            i += 1

    threads = [threading.Thread(target=write, args=(n,)) for n in range(writers)]
    threads += [threading.Thread(target=read) for _ in range(readers)]
    for t in threads:
        t.start()
    time.sleep(0.2)
    started = time.perf_counter()
    cache.clear()
    took = time.perf_counter() - started
    stop.set()
    for t in threads:
        t.join()
    cache.close()
    return took, sum(writes) / took


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--rows', type=int, default=20_000)
    parser.add_argument('--writers', type=int, default=2)
    parser.add_argument('--readers', type=int, default=2)
    parser.add_argument('--rounds', type=int, default=5)
    args = parser.parse_args()

    build = 'GIL' if sys._is_gil_enabled() else 'free-threaded'
    print(f'Python {sys.version.split()[0]} ({build}), switch interval {sys.getswitchinterval() * 1000:.1f} ms')
    print(f'{args.rows:,} rows, {args.writers} writer + {args.readers} reader threads, {args.rounds} rounds')
    takes = []
    for n in range(args.rounds):
        with tempfile.TemporaryDirectory() as tmp:
            took, write_rate = one_round(Path(tmp) / 'c', args.rows, args.writers, args.readers)
        takes.append(took)
        per_chunk = took / (args.rows / CHUNK) * 1000
        print(f'  round {n + 1}: clear {took:.3f} s, {per_chunk:.2f} ms/chunk, writers {write_rate:,.0f} sets/s')
    median = statistics.median(takes)
    print(f'median clear {median:.3f} s, {median / (args.rows / CHUNK) * 1000:.2f} ms/chunk')


if __name__ == '__main__':
    main()
