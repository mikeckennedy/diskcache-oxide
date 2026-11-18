"""File descriptors a FanoutCache holds with many threads: stock diskcache against diskcache-oxide (D-037).

Usage (from the repo root; the script raises its own RLIMIT_NOFILE, but `ulimit -n 10240` first does no harm):
    venv/bin/python -m bench.fanout_fds
    venv/bin/python -m bench.fanout_fds --threads 1,8,64 --out /tmp/fds.json
    venv/bin/python -m bench.fanout_fds --no-probe

For each backend (diskcache's FanoutCache(shards=8), diskcache-oxide's FanoutCache(shards=8)) and each
thread count, a fresh process opens the cache on an empty directory and starts the threads. Each
thread sets and gets KEYS_PER_THREAD keys of its own, enough that it touches all eight shards (the
chance one is missed is under 1 in a million), then waits at a barrier, so every connection is live
when the main thread counts. It reports:
- process: descriptors open in the whole process, over the count from before the cache was opened
  (os.listdir of /proc/self/fd on Linux, /dev/fd elsewhere).
- in root: descriptors whose path is inside the cache's root directory, the figure D-037 quotes
  (264 at 64 threads, there for one get and one set each, so one or two shards per thread). Paths
  come from /proc/self/fd links on Linux, and on macOS from readlink, falling back to fcntl
  F_GETPATH (/dev/fd entries aren't links there).
- after close: descriptors still open inside the root once the threads are released and close() has
  returned (D-018: diskcache's close() closes only the calling thread's connections).
A thread that fails (too many open files) still reaches the barrier; the row then reports the error.

The probe (skip it with --no-probe) runs the same thing in a subprocess whose soft RLIMIT_NOFILE is
lowered to PROBE_LIMIT (256, what a shell started over non-interactive ssh gets on macOS), at 1, 2,
3, ... threads, and reports the first thread count at which each backend fails, and the error. It
stops at PROBE_MAX threads (64).
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
import sysconfig
import tempfile
import threading
from pathlib import Path
from typing import Any, Optional

from bench.fanout import raise_fd_limit, warn_low_fd_limit

SHARDS = 8
KEYS_PER_THREAD = 128  # 8 * (7/8) ** 128 is about 3e-7: the chance a thread misses one of the 8 shards.
THREAD_COUNTS = (1, 8, 16, 32, 64)
PROBE_LIMIT = 256
PROBE_MAX = 64
BACKENDS: dict[str, str] = {'diskcache': 'diskcache', 'oxide': 'diskcache_oxide'}
REPO = Path(__file__).resolve().parent.parent


def fd_dir() -> str:
    return '/proc/self/fd' if os.path.isdir('/proc/self/fd') else '/dev/fd'


def open_fds() -> list[int]:
    """The descriptors open in this process (including, harmlessly, the one listing them, on both counts)."""
    return [int(name) for name in os.listdir(fd_dir()) if name.isdigit()]


def fd_path(fd: int) -> Optional[str]:
    """The path a descriptor refers to, or None (a pipe, a socket, or a descriptor closed meanwhile)."""
    try:
        return os.readlink(f'{fd_dir()}/{fd}')
    except OSError:
        pass
    if sys.platform != 'darwin':
        return None
    import fcntl

    try:
        raw = fcntl.fcntl(fd, getattr(fcntl, 'F_GETPATH', 50), bytes(1024))  # MAXPATHLEN
    except OSError:
        return None
    return os.fsdecode(raw.split(b'\0', 1)[0]) or None


def inside(root: str) -> int:
    prefix = root.rstrip(os.sep) + os.sep
    count = 0
    for fd in open_fds():
        path = fd_path(fd)
        if path and os.path.realpath(path).startswith(prefix):
            count += 1
    return count


def measure(backend: str, threads: int) -> dict[str, Any]:
    """Open a FanoutCache(shards=8), run `threads` threads that touch every shard, and count while they wait."""
    import importlib

    module = importlib.import_module(BACKENDS[backend])
    with tempfile.TemporaryDirectory(prefix='dco-fds-') as tmp:
        root = os.path.realpath(tmp)
        before = len(open_fds())
        cache = module.FanoutCache(root, shards=SHARDS, timeout=10)
        ready = threading.Barrier(threads + 1)
        release = threading.Event()
        errors: list[str] = []
        count_error: Optional[str] = None

        def worker(n: int) -> None:
            try:
                for i in range(KEYS_PER_THREAD):
                    key = f'{n}:{i}'
                    cache.set(key, i)
                    cache.get(key)
            except Exception as exc:
                errors.append(f'{type(exc).__module__}.{type(exc).__qualname__}: {exc}')
            finally:
                ready.wait()
                release.wait()

        pool = [threading.Thread(target=worker, args=(n,)) for n in range(threads)]
        started = 0
        try:
            for t in pool:
                t.start()
                started += 1
            ready.wait(timeout=300)
            try:
                process: Optional[int] = len(open_fds()) - before
                in_root: Optional[int] = inside(root)
            except OSError as exc:  # Listing needs a descriptor of its own, so at the limit it fails too.
                process = in_root = None
                count_error = f'counting: {type(exc).__qualname__}: {exc}'
        finally:
            if started < threads:
                ready.abort()
            release.set()
            for t in pool[:started]:
                t.join()
        cache.close()
        try:
            after_close: Optional[int] = inside(root)
        except OSError:
            after_close = None
    return {
        'backend': backend,
        'threads': threads,
        'baseline': before,
        'process': process,
        'in_root': in_root,
        'after_close': after_close,
        'error': errors[0] if errors else count_error,
        'failed_threads': len(errors),
    }


def child_main(args: argparse.Namespace) -> None:
    if args.limit:
        import resource

        _, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        resource.setrlimit(resource.RLIMIT_NOFILE, (args.limit, hard))
    try:
        result = measure(args.child, args.child_threads)
    except Exception as exc:  # The open itself, or starting a thread, can fail under a low limit.
        result = {'backend': args.child, 'threads': args.child_threads, 'error': f'{type(exc).__qualname__}: {exc}'}
    print(json.dumps(result))


def run_child(backend: str, threads: int, limit: Optional[int] = None) -> dict[str, Any]:
    cmd = [sys.executable, '-m', 'bench.fanout_fds', '--child', backend, '--child-threads', str(threads)]
    cmd += ['--limit', str(limit)] if limit else []
    out = subprocess.run(cmd, capture_output=True, text=True, cwd=REPO, timeout=600)
    lines = [line for line in out.stdout.splitlines() if line.startswith('{')]
    if out.returncode != 0 or not lines:
        tail = (out.stderr.strip().splitlines() or ['no output'])[-1]
        return {'backend': backend, 'threads': threads, 'error': f'child exited with {out.returncode}: {tail}'}
    return json.loads(lines[-1])


def dash(value: Optional[int]) -> str:
    return '-' if value is None else str(value)


def probe(backend: str) -> dict[str, Any]:
    """The first thread count at which `backend` fails with the soft limit at PROBE_LIMIT."""
    last_ok: Optional[dict[str, Any]] = None
    for n in range(1, PROBE_MAX + 1):
        result = run_child(backend, n, PROBE_LIMIT)
        if result.get('error'):
            return {'limit': PROBE_LIMIT, 'first_failure_threads': n, 'error': result['error'], 'last_ok': last_ok}
        last_ok = result
    return {'limit': PROBE_LIMIT, 'first_failure_threads': None, 'error': None, 'last_ok': last_ok}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--threads', default=','.join(map(str, THREAD_COUNTS)), help='Thread counts, comma-separated.')
    parser.add_argument('--no-probe', action='store_true', help=f'Skip the probe under a soft limit of {PROBE_LIMIT}.')
    parser.add_argument('--out', type=Path, help='Also write the results as JSON here.')
    parser.add_argument('--child', choices=list(BACKENDS), help=argparse.SUPPRESS)
    parser.add_argument('--child-threads', type=int, help=argparse.SUPPRESS)
    parser.add_argument('--limit', type=int, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.child:
        child_main(args)
        return

    nofile = raise_fd_limit()
    warn_low_fd_limit(nofile)
    counts = [int(n) for n in args.threads.split(',')]
    rows = [run_child(backend, n) for backend in BACKENDS for n in counts]
    gil_disabled = bool(sysconfig.get_config_var('Py_GIL_DISABLED'))
    python = platform.python_version() + ('t' if gil_disabled else '')
    print(f'Python {python}, {platform.platform()}, FanoutCache(shards={SHARDS}), RLIMIT_NOFILE soft {nofile["soft"]}')
    print(f'each thread sets and gets {KEYS_PER_THREAD} keys (every shard), then waits; counts while all wait')
    print(f'{"backend":11}{"threads":>8}{"process":>9}{"in root":>9}{"after close":>13}  error')
    for r in rows:
        cells = ''.join(f'{dash(r.get(k)):>{w}}' for k, w in (('process', 9), ('in_root', 9), ('after_close', 13)))
        error = f'{r["failed_threads"]} thread(s): {r["error"]}' if r.get('failed_threads') else r.get('error') or ''
        print(f'{r["backend"]:11}{r["threads"]:>8}{cells}  {error}')
    print('process = over the count before the open; in root = inside the cache root; after close = in root, after')

    probes: dict[str, Any] = {}
    if not args.no_probe:
        print(f'\nProbe: soft RLIMIT_NOFILE {PROBE_LIMIT}, 1 to {PROBE_MAX} threads, one fresh process per count')
        for backend in BACKENDS:
            probes[backend] = p = probe(backend)
            ok = p['last_ok']
            held = f'; at {ok["threads"]} threads it held {dash(ok.get("process"))} fds' if ok else ''
            if p['first_failure_threads'] is None:
                print(f'  {backend}: no failure up to {PROBE_MAX} threads{held}')
            else:
                print(f'  {backend}: first fails at {p["first_failure_threads"]} threads{held}: {p["error"]}')

    if args.out:
        meta = {'python': python, 'platform': platform.platform(), 'shards': SHARDS, 'nofile': nofile}
        meta['keys_per_thread'] = KEYS_PER_THREAD
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps({'meta': meta, 'rows': rows, 'probe': probes}, indent=2) + '\n')
        print(f'\nWrote {args.out}')


if __name__ == '__main__':
    main()
