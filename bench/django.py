"""Django cache backends compared through django.core.cache (plan 31, step 6).

Usage (from the repo root, on AC power, nothing else heavy running, not under nice):
    venv/bin/python -m bench.django            # 3.14
    .venv-ft/bin/python -m bench.django        # 3.14t
    venv/bin/python -m bench.django --quick    # a smoke run of the script; numbers mean nothing

Writes bench/results/django-<python>-<machine>.json and prints Markdown tables. Only compare results
from the same machine.

Every backend is reached through `django.core.cache.caches[alias]`, so each pays the same framework
cost (key making, validation, the per-thread instance). The backends:

- `oxide`: diskcache_oxide.DjangoCache on a plain Cache, and `oxide-8` on a FanoutCache of 8 shards.
- `diskcache`: diskcache 5.6.3's DjangoCache (its default, a FanoutCache of 8 shards).
- `file`: Django's FileBasedCache. `locmem`: Django's LocMemCache, the ceiling, since it's an
  in-process dict that shares nothing between processes. `db`: Django's DatabaseCache on SQLite.
- `redis`: Django's RedisCache, only with `--redis redis://host:port/db` (and the `redis` package
  installed). Run in a container on a Mac, it answers through the VM's port forwarding, which adds
  latency a Redis on the host wouldn't have; label such numbers. Its database is flushed.

Django's own backends get MAX_ENTRIES of 100,000 so none of them culls during a run (the default
of 300 would cull on most writes, which measures culling). `db` runs its SQLite in WAL mode at
SQLite's default `synchronous=FULL`. The throughput value is a 1 KB `str`, which oxide and
diskcache store as text while Django's own backends pickle it (and FileBasedCache compresses it).

Sections:
- `latency`: one thread, `get` (a hit) and `set`, per value: a small dict, a 4 KiB str, and the
  pickled HttpResponse the per-view cache stores, at 50 KiB (under oxide's 64 KiB value-file line)
  and 100 KiB (over it). p50 of a get is timed in batches; p99 and set are per-call. Every get
  trial runs before any set, on a cache that has had one write, so the gets measure the read path,
  not the state a burst of writes leaves behind.
- `overhead`: oxide's backend `get` against a raw `Cache.get` of the same made key, small dict.
- `threads` and `processes`: ops/s in a share-nothing loop over 1,000 keys of ~1 KB, at 1, 2, 4 and 8
  threads in one process or single-threaded processes, at 99:1 and 1,000:1 reads to writes.
  Processes are spawned and configure Django themselves. `locmem` across processes is each
  process's own dict, so its number is a ceiling, not a shared cache.
- `async`: `await cache.aget()` (a `sync_to_async` hop, decision 8) against the same `get` called
  inline, per-call p50/p99/p99.9/max, quiet, for a 100 KiB value, and beside a writer process
  setting other keys in the same cache as fast as it can. Inline is never the default (Michael,
  2026-11-18: an `await` that never suspends doesn't yield the loop); this shows what the hop costs.
"""

from __future__ import annotations

import argparse
import asyncio
import gc
import json
import multiprocessing
import os
import platform
import queue
import random
import shutil
import statistics
import sys
import sysconfig
import tempfile
import threading
import time
import urllib.parse
from collections.abc import Callable
from pathlib import Path
from typing import Any

RESULTS_DIR = Path(__file__).parent / 'results'
THREAD_COUNTS: tuple[int, ...] = (1, 2, 4, 8)
MIXES = {'99to1': 100, '1000to1': 1_000}
KEYS = 1_000
BIG = 100_000

BACKENDS = ['oxide', 'oxide-8', 'diskcache', 'file', 'locmem', 'db']  # main() adds 'redis' with --redis


def cache_settings(root: Path) -> dict[str, dict[str, Any]]:
    big = {'MAX_ENTRIES': BIG}
    return {
        'oxide': {'BACKEND': 'diskcache_oxide.DjangoCache', 'LOCATION': str(root / 'oxide')},
        'oxide-8': {
            'BACKEND': 'diskcache_oxide.DjangoCache',
            'LOCATION': str(root / 'oxide-8'),
            'OPTIONS': {'shards': 8},
        },
        'diskcache': {'BACKEND': 'diskcache.DjangoCache', 'LOCATION': str(root / 'diskcache')},
        'file': {
            'BACKEND': 'django.core.cache.backends.filebased.FileBasedCache',
            'LOCATION': str(root / 'file'),
            'OPTIONS': big,
        },
        'locmem': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache', 'LOCATION': 'bench', 'OPTIONS': big},
        'db': {'BACKEND': 'django.core.cache.backends.db.DatabaseCache', 'LOCATION': 'bench_cache', 'OPTIONS': big},
    }


def setup_django(root: Path) -> None:
    """Configure Django in this process (the parent and every spawned worker)."""
    import django
    from django.conf import settings

    if settings.configured:
        return
    caches = cache_settings(root)
    caches['default'] = caches['locmem']
    redis = root / 'redis-url'  # written by main() for --redis, so spawned workers find it too
    if redis.exists():
        caches['redis'] = {'BACKEND': 'django.core.cache.backends.redis.RedisCache', 'LOCATION': redis.read_text()}
    settings.configure(
        CACHES=caches,
        DATABASES={
            'default': {
                'ENGINE': 'django.db.backends.sqlite3',
                'NAME': str(root / 'db.sqlite3'),
                'OPTIONS': {'timeout': 30, 'init_command': 'PRAGMA journal_mode=WAL;'},
            }
        },
        USE_TZ=True,
    )
    django.setup()


def response_of(size: int) -> Any:
    """What `cache_page` stores: an HttpResponse, pickled by the backend. Incompressible body."""
    from django.http import HttpResponse

    response = HttpResponse(random.Random(size).randbytes(size), content_type='text/html')
    response['Cache-Control'] = 'max-age=60'
    return response


def values() -> dict[str, Any]:
    return {
        'small dict': {'title': 'Episode 42', 'guests': ['Ada', 'Grace'], 'number': 42, 'live': True},
        '4 KiB str': 'x' * 4_096,
        '50 KiB response': response_of(50 * 1024),
        '100 KiB response': response_of(100 * 1024),
    }


def percentile(samples: list[float], pct: float) -> float:
    ordered = sorted(samples)
    return ordered[min(len(ordered) - 1, int(len(ordered) * pct / 100))]


def per_call_us(op: Callable[[int], Any], n: int) -> list[float]:
    out = []
    for i in range(n):
        start = time.perf_counter_ns()
        op(i)
        out.append((time.perf_counter_ns() - start) / 1_000)
    return out


def batched_p50_us(op: Callable[[int], Any], batches: int, batch: int) -> float:
    per_op = []
    i = 0
    for _ in range(batches):
        start = time.perf_counter_ns()
        for _ in range(batch):
            op(i)
            i += 1
        per_op.append((time.perf_counter_ns() - start) / batch / 1_000)
    return statistics.median(per_op)


def latency(quick: bool) -> dict[str, Any]:
    from django.core.cache import caches

    trials = 1 if quick else 3
    batches, batch, samples = (3, 50, 200) if quick else (10, 200, 2_000)
    results: dict[str, Any] = {}
    for name, value in values().items():
        results[name] = {}
        for alias in BACKENDS:
            cache = caches[alias]
            cache.set('hit', value, None)
            get, set_ = cache.get, cache.set
            for _ in range(100):
                get('hit')
            p50s, p99s, sets = [], [], []
            gc.disable()
            try:
                # Every get trial before any set: a read right after a burst of writes measures the
                # cache's state after them (a longer write-ahead log), not the backend's read path.
                for _ in range(trials):
                    p50s.append(batched_p50_us(lambda i: get('hit'), batches, batch))
                    p99s.append(percentile(per_call_us(lambda i: get('hit'), samples), 99))
                for _ in range(trials):
                    sets.append(
                        statistics.median(per_call_us(lambda i: set_(f'w:{i % 100}', value, None), samples // 2))
                    )
            finally:
                gc.enable()
            results[name][alias] = {
                'get_p50_us': statistics.median(p50s),
                'get_p99_us': statistics.median(p99s),
                'set_p50_us': statistics.median(sets),
            }
            cache.clear()
    return results


def overhead(quick: bool) -> dict[str, float]:
    from django.core.cache import caches

    cache = caches['oxide']
    value = values()['small dict']
    cache.set('hit', value, None)
    made = cache.make_and_validate_key('hit')
    raw = cache.cache.get
    batches, batch = (3, 100) if quick else (20, 1_000)
    gc.disable()
    try:
        backend = statistics.median(batched_p50_us(lambda i: cache.get('hit'), batches, batch) for _ in range(5))
        direct = statistics.median(batched_p50_us(lambda i: raw(made), batches, batch) for _ in range(5))
    finally:
        gc.enable()
    cache.clear()
    return {'backend_get_p50_us': backend, 'raw_get_p50_us': direct, 'overhead_us': backend - direct}


def fill(alias: str) -> None:
    from django.core.cache import caches

    cache = caches[alias]
    value = 'v' * 1_024
    for i in range(KEYS):
        cache.set(f't:{i}', value, None)


def loop(alias: str, seed: int, every: int, stop: Callable[[], bool]) -> int:
    """The share-nothing loop: this thread's own instance (caches is per-thread), keys and plan."""
    from django.core.cache import caches

    cache = caches[alias]
    get, set_ = cache.get, cache.set
    value = 'v' * 1_024
    keys = [''.join(('t:', str(i))) for i in range(KEYS)]
    rng = random.Random(seed)
    plan = [(keys[rng.randrange(KEYS)], i % every == 0) for i in range(every * max(1, 1_000 // every))]
    ops = 0
    while not stop():
        for key, write in plan:
            if write:
                set_(key, value, None)
            else:
                get(key)
        ops += len(plan)
    return ops


def threads(alias: str, every: int, seconds: float) -> dict[str, float]:
    out = {}
    for count in THREAD_COUNTS:
        counts = [0] * count
        errors: list[BaseException] = []
        done = threading.Event()
        barrier = threading.Barrier(count + 1)

        def worker(n: int) -> None:
            from django.core.cache import caches

            try:
                caches[alias].get('t:0')  # this thread's instance, opened before the clock starts
            finally:
                barrier.wait()
            try:
                counts[n] = loop(alias, n, every, done.is_set)
            except BaseException as error:  # reported after the join, not counted as 0 ops
                errors.append(error)

        pool = [threading.Thread(target=worker, args=(n,)) for n in range(count)]
        for thread in pool:
            thread.start()
        barrier.wait()
        start = time.perf_counter()
        time.sleep(seconds)
        done.set()
        for thread in pool:
            thread.join()
        elapsed = time.perf_counter() - start
        if errors:
            raise RuntimeError(f'{alias}: a thread worker failed') from errors[0]
        out[str(count)] = sum(counts) / elapsed
    return out


def process_worker(root: str, alias: str, seed: int, every: int, ready: Any, go: Any, stop: Any, results: Any) -> None:
    setup_django(Path(root))
    if alias == 'locmem':
        fill(alias)  # each process's own dict
    from django.core.cache import caches

    caches[alias].get('t:0')  # open before the clock starts
    ready.release()
    go.wait()
    results.put(loop(alias, seed, every, stop.is_set))


def processes(root: Path, alias: str, every: int, seconds: float) -> dict[str, float]:
    context = multiprocessing.get_context('spawn')
    out = {}
    for count in THREAD_COUNTS:
        ready, go, stop, results = context.Semaphore(0), context.Event(), context.Event(), context.Queue()
        pool = [
            context.Process(target=process_worker, args=(str(root), alias, n, every, ready, go, stop, results))
            for n in range(count)
        ]
        for process in pool:
            process.start()
        for _ in pool:
            if not ready.acquire(timeout=120):  # a worker that died during startup never releases
                stop.set()
                go.set()
                raise RuntimeError(
                    f'{alias}: a worker process never got ready (exit codes {[p.exitcode for p in pool]})'
                )
        go.set()
        start = time.perf_counter()
        time.sleep(seconds)
        stop.set()
        try:
            total = sum(results.get(timeout=seconds + 120) for _ in pool)
        except queue.Empty:
            raise RuntimeError(f'{alias}: a worker process never reported (exit codes {[p.exitcode for p in pool]})')
        elapsed = time.perf_counter() - start
        for process in pool:
            process.join()
            if process.exitcode:
                raise RuntimeError(f'{alias} worker exited with {process.exitcode}')
        out[str(count)] = total / elapsed
    return out


def writer(root: str, alias: str, stop: Any, ready: Any) -> None:
    setup_django(Path(root))
    from django.core.cache import caches

    cache = caches[alias]
    value = 'w' * 1_024
    cache.set('writer:0', value)
    ready.set()
    i = 0
    while not stop.is_set():
        cache.set(f'writer:{i % 1_000}', value, None)
        i += 1


def async_tails(root: Path, quick: bool) -> dict[str, Any]:
    from django.core.cache import caches

    n = 2_000 if quick else 50_000
    cache = caches['oxide']
    cache.set('small', values()['small dict'], None)
    cache.set('big', values()['100 KiB response'], None)

    def summary(samples: list[float]) -> dict[str, float]:
        return {
            'p50_us': statistics.median(samples),
            'p99_us': percentile(samples, 99),
            'p99_9_us': percentile(samples, 99.9),
            'max_us': max(samples),
        }

    async def measure(key: str) -> dict[str, Any]:
        hop, inline = [], []
        for _ in range(200):
            await cache.aget(key)
        gc.disable()
        try:
            for _ in range(n):
                start = time.perf_counter_ns()
                await cache.aget(key)
                hop.append((time.perf_counter_ns() - start) / 1_000)
                start = time.perf_counter_ns()
                cache.get(key)
                inline.append((time.perf_counter_ns() - start) / 1_000)
        finally:
            gc.enable()
        return {'aget (thread hop)': summary(hop), 'get inline': summary(inline)}

    results = {
        'quiet, small dict': asyncio.run(measure('small')),
        'quiet, 100 KiB response': asyncio.run(measure('big')),
    }
    context = multiprocessing.get_context('spawn')
    stop, ready = context.Event(), context.Event()
    process = context.Process(target=writer, args=(str(root), 'oxide', stop, ready))
    process.start()
    try:
        if not ready.wait(60):
            raise RuntimeError('the writer process never started')
        results['beside a writer process, small dict'] = asyncio.run(measure('small'))
    finally:
        stop.set()
        process.join()
    cache.clear()
    return results


def power_source() -> str:
    if sys.platform != 'darwin':
        return 'unknown'
    import subprocess

    try:
        out = subprocess.run(['pmset', '-g', 'batt'], capture_output=True, text=True, timeout=5).stdout
    except OSError, subprocess.SubprocessError:
        return 'unknown'
    return 'ac' if 'AC Power' in out else 'battery' if 'Battery Power' in out else 'unknown'


def metadata(machine: str, quick: bool, seconds: float) -> dict[str, Any]:
    import diskcache
    import diskcache_oxide
    import django

    gil_disabled = bool(sysconfig.get_config_var('Py_GIL_DISABLED'))
    return {
        'python': platform.python_version() + ('t' if gil_disabled else ''),
        'gil_enabled_at_runtime': sys._is_gil_enabled(),
        'django': django.__version__,
        'diskcache': diskcache.__version__,
        'diskcache_oxide': diskcache_oxide.__version__,
        'machine': machine,
        'platform': platform.platform(),
        'cpu_count': os.cpu_count(),
        'power': power_source(),
        'nice': os.getpriority(os.PRIO_PROCESS, 0),  # os.nice(0) raises PermissionError at nice 20 on macOS
        'quick': quick,
        'seconds_per_point': seconds,
        'timestamp': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
    }


def without_credentials(url: str) -> str:
    """The URL with any user and password dropped, for the results file."""
    parts = urllib.parse.urlsplit(url)
    netloc = parts.hostname or ''
    if parts.port:
        netloc += f':{parts.port}'
    return parts._replace(netloc=netloc).geturl()


def tables(results: dict[str, Any], meta: dict[str, Any]) -> str:
    lines = [f'Django {meta["django"]}, Python {meta["python"]}, {meta["machine"]}, {meta["power"]} power.\n']
    if meta.get('redis'):
        lines.append(
            "`redis` is Django's RedisCache at " + meta['redis'] + '. Run in a container on a Mac, it answers '
            "through the VM's port forwarding: its numbers measure that as much as Redis.\n"
        )
    lines.append('## get and set latency, one thread (µs: get p50 / get p99 / set p50)\n')
    lines.append('| Value | ' + ' | '.join(BACKENDS) + ' |')
    lines.append('|---' * (len(BACKENDS) + 1) + '|')
    for name, row in results['latency'].items():
        cells = [
            f'{row[b]["get_p50_us"]:.1f} / {row[b]["get_p99_us"]:.1f} / {row[b]["set_p50_us"]:.1f}' for b in BACKENDS
        ]
        lines.append(f'| {name} | ' + ' | '.join(cells) + ' |')
    o = results['overhead']
    lines.append(
        f'\nBackend overhead: DjangoCache.get {o["backend_get_p50_us"]:.2f} µs against Cache.get '
        f'{o["raw_get_p50_us"]:.2f} µs, {o["overhead_us"]:.2f} µs for the framework.\n'
    )
    for section in ('threads', 'processes'):
        for mix in MIXES:
            lines.append(f'## {section}, {mix} (thousand ops/s at 1 / 2 / 4 / 8)\n')
            lines.append('| Backend | 1 | 2 | 4 | 8 |')
            lines.append('|---|---|---|---|---|')
            for backend in BACKENDS:
                row = results[section][mix][backend]
                lines.append(f'| {backend} | ' + ' | '.join(f'{row[str(c)] / 1_000:.0f}' for c in THREAD_COUNTS) + ' |')
            lines.append('')
    lines.append('## async reads on oxide (µs: p50 / p99 / p99.9 / max)\n')
    lines.append('| Case | aget (thread hop) | get inline |')
    lines.append('|---|---|---|')
    for case, row in results['async'].items():
        cells = [
            '{p50_us:.1f} / {p99_us:.1f} / {p99_9_us:.1f} / {max_us:.0f}'.format(**row[k])
            for k in ('aget (thread hop)', 'get inline')
        ]
        lines.append(f'| {case} | ' + ' | '.join(cells) + ' |')
    return '\n'.join(lines) + '\n'


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--machine', default=platform.node().split('.')[0] or 'unknown')
    parser.add_argument('--quick', action='store_true', help='Smoke-test the script; the numbers mean nothing.')
    parser.add_argument('--seconds', type=float, help='Seconds per throughput point (default 2, quick 0.2).')
    parser.add_argument('--out', type=Path)
    parser.add_argument('--redis', metavar='URL', help="Also measure Django's RedisCache at this URL (flushed).")
    args = parser.parse_args()
    seconds = args.seconds or (0.2 if args.quick else 2.0)

    import diskcache_oxide

    if getattr(diskcache_oxide._core, '_DEBUG_BUILD', False):
        raise SystemExit('refusing to benchmark a debug build of diskcache_oxide; rebuild in release mode')
    root = Path(tempfile.mkdtemp(prefix='dco-bench-django-'))
    try:
        if args.redis:
            (root / 'redis-url').write_text(args.redis)
            BACKENDS.append('redis')
        setup_django(root)
        from django.core.management import call_command

        call_command('createcachetable', verbosity=0)
        meta = metadata(args.machine, args.quick, seconds)
        meta['redis'] = without_credentials(args.redis) if args.redis else None
        if not args.quick and (meta['power'] == 'battery' or meta['nice'] > 0):
            # A laptop on battery throttles, and nice skews timings: such numbers aren't publishable.
            raise SystemExit(f'refusing a full run on {meta["power"]} power at nice {meta["nice"]}; use --quick')
        results: dict[str, Any] = {}
        print('latency', file=sys.stderr)
        results['latency'] = latency(args.quick)
        results['overhead'] = overhead(args.quick)
        for section in ('threads', 'processes'):
            results[section] = {mix: {} for mix in MIXES}
            for backend in BACKENDS:
                fill(backend)
                for mix, every in MIXES.items():
                    print(f'{section} {backend} {mix}', file=sys.stderr)
                    if section == 'threads':
                        results[section][mix][backend] = threads(backend, every, seconds)
                    else:
                        results[section][mix][backend] = processes(root, backend, every, seconds)
        print('async', file=sys.stderr)
        results['async'] = async_tails(root, args.quick)
        from diskcache_oxide.djangocache import close_all
        from django.core.cache import caches

        caches.close_all()
        close_all()
    finally:
        shutil.rmtree(root, ignore_errors=True)

    out: Path = (
        args.out or RESULTS_DIR / f'django-{meta["python"]}-{meta["machine"]}{"-quick" if args.quick else ""}.json'
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({'meta': meta, 'results': results}, indent=2) + '\n')
    text = tables(results, meta)
    out.with_suffix('.md').write_text(text)
    print(text)
    print(f'Wrote {out}')


if __name__ == '__main__':
    main()
