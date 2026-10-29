"""WAL growth under write bursts with readers in other processes (D-024, Phase 2.5 step 1).

    python -m bench.wal_burst [--readers 4] [--rounds 3] [--wal-size-limit BYTES]

Starts reader processes (4 threads each) that read nonstop, then runs bursts of 20k sets plus a
clear() and a short trickle of writes. Prints how long the bursts took, the WAL's peak and final
size, and the forced-checkpoint counters. A huge --wal-size-limit (e.g. 2**40) behaves like having
no limit: it reproduces the old, unbounded growth.
"""

from __future__ import annotations

import argparse
import multiprocessing
import tempfile
import threading
import time
from pathlib import Path


def _reader(directory: str, stop_at: float) -> None:
    from diskcache_oxide import Cache

    cache = Cache(directory, size_limit=2**34)

    def loop() -> None:
        i = 0
        while time.time() < stop_at:
            cache.get(f'k{i % 1000}')
            i += 1

    threads = [threading.Thread(target=loop) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()


def main() -> None:
    from diskcache_oxide import Cache

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--readers', type=int, default=4)
    parser.add_argument('--rounds', type=int, default=3)
    parser.add_argument('--wal-size-limit', type=int)
    args = parser.parse_args()
    opts = {} if args.wal_size_limit is None else {'wal_size_limit': args.wal_size_limit}
    with tempfile.TemporaryDirectory(prefix='dco-walburst-') as tmp:
        cache = Cache(tmp, size_limit=2**34, **opts)
        cache.set('k0', 'v')
        wal = Path(tmp) / 'diskcache-oxide.sqlite-wal'
        stop_at = time.time() + 30 + 15 * args.rounds
        ctx = multiprocessing.get_context('spawn')
        readers = [ctx.Process(target=_reader, args=(tmp, stop_at)) for _ in range(args.readers)]
        for p in readers:
            p.start()
        time.sleep(2)
        body = 'x' * 2_000
        start = time.time()
        peak = 0
        for _ in range(args.rounds):
            for i in range(20_000):
                cache.set(f'k{i}', body)
                if i % 1_000 == 0:
                    peak = max(peak, wal.stat().st_size)
            cache.clear()
            peak = max(peak, wal.stat().st_size)
        burst = time.time() - start
        for n in range(500):
            cache.set(f't{n % 100}', 'v')
            time.sleep(0.002)
        final = wal.stat().st_size
        for p in readers:
            p.terminate()
            p.join(10)
        stats = cache._wal_stats() if hasattr(cache, '_wal_stats') else {}
        cache.close()
    print(
        f'{args.readers} reader processes x 4 threads, {args.rounds} bursts: {burst:.1f}s, '
        f'WAL peak {peak / 2**20:.1f} MiB, final {final / 2**20:.1f} MiB, checkpoints {stats}'
    )


if __name__ == '__main__':
    main()
