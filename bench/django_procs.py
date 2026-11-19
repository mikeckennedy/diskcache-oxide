"""Django cache backends at one count of worker processes, a few rounds each (plan 31, step 6).

Usage (from the repo root, on AC power, nothing else heavy running, not under nice):
    venv/bin/python -m bench.django_procs              # 4 processes, 3 rounds of 3 s
    .venv-ft/bin/python -m bench.django_procs --processes 8

The same loop, settings and backends as bench.django's `processes` section (spawned processes, one
thread each, 1,000 keys of a 1 KB str, 99:1 and 1,000:1), at one process count, with the median of
several rounds. Prints Markdown; bench/results/django-4procs-*.md holds the run the Django page uses.
"""

from __future__ import annotations

import argparse
import shutil
import statistics
import sys
import tempfile
from pathlib import Path

from bench import django as bench

BACKENDS = ['oxide', 'oxide-8', 'diskcache', 'db', 'file', 'locmem']


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--processes', type=int, default=4)
    parser.add_argument('--rounds', type=int, default=3)
    parser.add_argument('--seconds', type=float, default=3.0)
    args = parser.parse_args()
    bench.THREAD_COUNTS = (args.processes,)
    root = Path(tempfile.mkdtemp(prefix='dco-django-procs-'))
    try:
        bench.setup_django(root)
        from django.core.management import call_command

        call_command('createcachetable', verbosity=0)
        for backend in BACKENDS:
            bench.fill(backend)
        build = 'GIL' if sys._is_gil_enabled() else 'free-threaded'
        print(f'{sys.version.split()[0]} ({build}), {args.processes} processes, median of {args.rounds} rounds\n')
        print('| Mix | Backend | ops/s | Rounds |')
        print('|---|---|---|---|')
        for mix, every in bench.MIXES.items():
            for backend in BACKENDS:
                runs = [
                    bench.processes(root, backend, every, args.seconds)[str(args.processes)] for _ in range(args.rounds)
                ]
                rounds = ', '.join(f'{r / 1000:.0f}k' for r in runs)
                print(f'| {mix} | {backend} | {statistics.median(runs) / 1000:,.0f}k | {rounds} |', flush=True)
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == '__main__':
    main()
