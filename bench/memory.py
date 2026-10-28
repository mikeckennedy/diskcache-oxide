"""Memory under load: how much SQLite memory diskcache-oxide uses as threads pile on.

Usage (from the repo root, with the memory-instrumented build; see scripts/check.sh):
    .venv-mem/bin/python -m bench.memory [--threads 1,4,8,16,32] [--seconds 2] [--opt sqlite_cache_size=512]

Needs a build with SQLite's memory statistics on (SQLITE_DEFAULT_MEMSTATUS=1), which costs a
process-wide mutex on every SQLite allocation. So the memory numbers here are definitive, but the
throughput printed next to them is NOT: take performance numbers from bench.gate on the normal build.

Each thread count runs the 99:1 read/write mix over uniformly random keys of a database much larger
than the page-cache budget (the worst case for page-cache growth), and reports SQLite's high-water
mark, what it still holds afterwards, and the budget `max_connections` x `sqlite_cache_size` implies.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from diskcache_oxide import Cache, _core
from tests.proctitle import set_title

KEYS = 50_000
BODY = 'x' * 1_000
PAGE_SIZE = 4096
DEFAULT_PAGES = 256
DEFAULT_CONNECTIONS = 8


def instrumented() -> bool:
    """True if this build keeps SQLite's memory statistics (they read 0 with MEMSTATUS off)."""
    with tempfile.TemporaryDirectory() as tmp, Cache(tmp) as probe:
        probe.set('probe', 1)
        return _core._sqlite_memory()[0] > 0


def storm(cache: Cache, threads: int, seconds: float) -> float:
    stop = time.perf_counter() + seconds
    counts = [0] * threads

    def worker(n: int) -> None:
        rng = random.Random(n)
        ops = 0
        while time.perf_counter() < stop:
            key = f'k:{rng.randrange(KEYS)}'
            if ops % 100 == 0:
                cache.set(key, {'id': ops, 'body': BODY})
            else:
                cache.get(key)
            ops += 1
        counts[n] = ops

    pool = [threading.Thread(target=worker, args=(n,)) for n in range(threads)]
    start = time.perf_counter()
    for t in pool:
        t.start()
    for t in pool:
        t.join()
    return sum(counts) / (time.perf_counter() - start)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--threads', default='1,4,8,16,32')
    parser.add_argument('--seconds', type=float, default=2.0)
    parser.add_argument('--opt', action='append', default=[], metavar='NAME=VALUE', help='Cache option, e.g. sqlite_cache_size=512.')
    parser.add_argument('--out', type=Path, help='Also write the results as JSON.')
    args = parser.parse_args()
    set_title('bench-memory')
    if not instrumented():
        print('this build has SQLite memory statistics off; build .venv-mem (scripts/check.sh) to measure memory', file=sys.stderr)
        return 2
    opts: dict[str, Any] = {name: json.loads(value) for name, _, value in (o.partition('=') for o in args.opt)}
    pages = opts.get('sqlite_cache_size', DEFAULT_PAGES)
    connections = opts.get('max_connections', DEFAULT_CONNECTIONS)
    budget = connections * pages * PAGE_SIZE

    rows = []
    with tempfile.TemporaryDirectory(prefix='dco-memory-') as tmp:
        cache = Cache(Path(tmp) / 'c', size_limit=2**34, **opts)
        for i in range(KEYS):
            cache.set(f'k:{i}', {'id': i, 'body': BODY})
        baseline, _ = _core._sqlite_memory()
        print(f'{KEYS} keys, {connections} connections x {pages} pages: page-cache budget {budget / 2**20:.1f} MiB')
        print(f'{"threads":>8s} {"high-water MiB":>15s} {"held after MiB":>15s} {"ops/s (informational)":>22s}')
        for threads in (int(t) for t in args.threads.split(',')):
            _core._sqlite_memory(reset=True)
            ops = storm(cache, threads, args.seconds)
            used, high = _core._sqlite_memory()
            rows.append({'threads': threads, 'high_water': high, 'held_after': used, 'ops_per_sec_informational': ops})
            print(f'{threads:8d} {high / 2**20:15.2f} {used / 2**20:15.2f} {ops:22,.0f}')
        cache.close()
    if args.out:
        result = {'python': sys.version, 'opts': opts, 'budget': budget, 'baseline': baseline, 'rows': rows}
        args.out.write_text(json.dumps(result, indent=2) + '\n')
    return 0


if __name__ == '__main__':
    sys.exit(main())
