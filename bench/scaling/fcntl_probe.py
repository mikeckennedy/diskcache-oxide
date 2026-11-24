"""A shared POSIX advisory lock taken and released on one file by N processes: no SQLite, no oxide.

Usage (standard library only, any Python 3.8 or later, AC power, nothing else heavy running):
    python3 bench/scaling/fcntl_probe.py
    python3 bench/scaling/fcntl_probe.py --counts 1,2,3,4 --rounds 5 --seconds 2 --json out.json

Plan 33's first control. A read in SQLite's WAL mode takes a shared `fcntl` lock on one byte of the
`-shm` file and releases it, and a process with one reader makes both system calls on every read.
This script does only that, on the byte range SQLite uses for a read mark, from N spawned
processes. "one file" is every process on the same file, as readers of one database are;
"file per process" is the control, where nothing is shared. If "one file" falls as processes are
added while the control climbs, the kernel's per-file lock path is the limit and a plain Cache's
multi-process read fall needs no further cause.

Each row is the median of the rounds, in thousands of lock and unlock pairs a second, with the
share of the workers' CPU time spent in the kernel.
"""

import argparse
import fcntl
import json
import multiprocessing
import os
import platform
import resource
import shutil
import statistics
import sys
import tempfile
import time

# SQLite's WAL_READ_LOCK(1): byte 124 of the -shm file (WALINDEX_LOCK_OFFSET 120, plus 3 + the mark).
LOCK_BYTE = 124
ARMS = ('one file', 'file per process')


def worker(path, ready, go, stop, results):
    fd = os.open(path, os.O_RDWR)
    lockf, shared, unlock = fcntl.lockf, fcntl.LOCK_SH | fcntl.LOCK_NB, fcntl.LOCK_UN
    ready.release()
    go.wait()
    before = resource.getrusage(resource.RUSAGE_SELF)
    pairs = 0
    while not stop.is_set():
        for _ in range(1_000):
            lockf(fd, shared, 1, LOCK_BYTE)
            lockf(fd, unlock, 1, LOCK_BYTE)
        pairs += 1_000
    after = resource.getrusage(resource.RUSAGE_SELF)
    results.put((pairs, after.ru_utime - before.ru_utime, after.ru_stime - before.ru_stime))
    os.close(fd)


def measure(paths, seconds):
    context = multiprocessing.get_context('spawn')
    ready, go, stop, results = context.Semaphore(0), context.Event(), context.Event(), context.Queue()
    pool = [context.Process(target=worker, args=(path, ready, go, stop, results)) for path in paths]
    for process in pool:
        process.start()
    for _ in pool:
        if not ready.acquire(timeout=120):
            stop.set()
            go.set()
            raise RuntimeError('a worker never got ready')
    go.set()
    start = time.perf_counter()
    time.sleep(seconds)
    stop.set()
    got = [results.get(timeout=seconds + 120) for _ in pool]
    elapsed = time.perf_counter() - start
    for process in pool:
        process.join()
    user, system = sum(g[1] for g in got), sum(g[2] for g in got)
    return sum(g[0] for g in got) / elapsed, system / (user + system) if user + system else 0.0


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        '--counts', default='', help='Process counts, comma separated (default: 1,2,3,4,8,16 up to the CPU count).'
    )
    parser.add_argument('--rounds', type=int, default=5)
    parser.add_argument('--seconds', type=float, default=1.5)
    parser.add_argument('--json', help='Also write the samples to this file.')
    args = parser.parse_args()
    cpus = os.cpu_count() or 1
    counts = [int(c) for c in args.counts.split(',')] if args.counts else [c for c in (1, 2, 3, 4, 8, 16) if c <= cpus]

    root = tempfile.mkdtemp(prefix='dco-fcntl-')
    samples = {arm: {str(c): [] for c in counts} for arm in ARMS}
    system_share = {arm: {str(c): [] for c in counts} for arm in ARMS}
    try:
        files = []
        for n in range(max(counts)):
            path = os.path.join(root, 'f%d' % n)
            with open(path, 'wb') as handle:
                handle.write(b'\0' * 32_768)
            files.append(path)
        for r in range(args.rounds):
            for count in counts:
                for arm in ARMS:
                    paths = [files[0]] * count if arm == 'one file' else files[:count]
                    rate, share = measure(paths, args.seconds)
                    samples[arm][str(count)].append(rate)
                    system_share[arm][str(count)].append(share)
            print('round %d/%d done' % (r + 1, args.rounds), file=sys.stderr)
    finally:
        shutil.rmtree(root, ignore_errors=True)

    print(
        '%s, %s, %d CPUs, Python %s. Median of %d rounds of %.1f s.'
        % (platform.platform(), platform.machine(), cpus, platform.python_version(), args.rounds, args.seconds)
    )
    print()
    print('| Processes | one file (pairs/s) | system time | file per process (pairs/s) | system time |')
    print('|---|---|---|---|---|')
    for count in counts:
        cells = []
        for arm in ARMS:
            cells.append('%s' % format(round(statistics.median(samples[arm][str(count)]) / 1_000), ',') + 'k')
            cells.append('%.0f%%' % (100 * statistics.median(system_share[arm][str(count)])))
        print('| %d | %s |' % (count, ' | '.join(cells)))
    if args.json:
        meta = {
            'platform': platform.platform(),
            'machine': platform.machine(),
            'cpus': cpus,
            'python': platform.python_version(),
            'rounds': args.rounds,
            'seconds': args.seconds,
            'timestamp': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
        }
        with open(args.json, 'w') as handle:
            json.dump({'meta': meta, 'samples': samples, 'system_share': system_share}, handle, indent=2)
            handle.write('\n')


if __name__ == '__main__':
    main()
