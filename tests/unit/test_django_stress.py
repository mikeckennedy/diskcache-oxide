"""Plan 31's step 9 gate: DjangoCache driven hard through django.core.cache across processes, forks and
a SIGKILL. The sanitizers and the soak exercise none of this module, so this is its stress test.

Slow (excluded by default): `venv/bin/pytest -m slow tests/unit/test_django_stress.py`, and the same on
`.venv-ft`. Unix only: it forks and sends SIGKILL.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip('django')

from diskcache_oxide import djangocache  # noqa: E402
from diskcache_oxide.djangocache import DjangoCache  # noqa: E402

pytestmark = [
    pytest.mark.slow,
    pytest.mark.timeout(300),  # the default 60 s is about 3x the SIGKILL test on a busy machine
    pytest.mark.skipif(sys.platform == 'win32', reason='forks and SIGKILL are Unix-only'),
]

BACKEND = 'diskcache_oxide.DjangoCache'
SHAPES = {'plain': {}, 'sharded': {'shards': 8}}


@pytest.fixture(autouse=True)
def release_handles():
    yield
    djangocache.close_all()


def settings_code(location: Path, options: dict[str, Any]) -> str:
    return (
        'from django.conf import settings\n'
        f'settings.configure(CACHES={{"default": {{"BACKEND": {BACKEND!r}, "LOCATION": {str(location)!r}, '
        f'"OPTIONS": {options!r}}}}})\n'
    )


def make(location: Path, options: dict[str, Any]) -> DjangoCache:
    return DjangoCache(str(location), {'OPTIONS': options})


def wait_for(parent: DjangoCache, keys: list[str], workers: list[subprocess.Popen], what: str) -> None:
    deadline = time.monotonic() + 120
    while len(parent.get_many(keys)) < len(keys):
        assert time.monotonic() < deadline, f'workers never {what}'
        dead = [w for w in workers if w.poll() is not None]
        assert not dead, [w.communicate()[1] for w in dead]
        time.sleep(0.005)


# 8 processes of 4 threads, the threads racing the first open.

WORKER = """
import os, threading, time
from django.core.cache import caches
from diskcache_oxide import djangocache

THREADS, ROUNDS = 4, {rounds}
pid = os.getpid()
barrier = threading.Barrier(THREADS)
wins, errors, handles = [0] * THREADS, [], [None] * THREADS

def work(t):
    try:
        barrier.wait()  # every thread's first caches['default'] at once: they race the first open
        cache = caches['default']
        cache.set(f'ready{{pid}}:{{t}}', 1)
        handles[t] = id(cache.cache)
        deadline = time.monotonic() + 120
        while not cache.get('go'):
            assert time.monotonic() < deadline, 'never told to go'
            time.sleep(0.001)
        for i in range(ROUNDS):
            cache.incr('n')
            wins[t] += cache.add(f'a{{i}}', pid)
            key = f'own:{{pid}}:{{t}}:{{i}}'
            cache.set(key, {{'pid': pid, 't': t, 'i': i}})
            assert cache.get(key) == {{'pid': pid, 't': t, 'i': i}}
            if i % 10 == 9:
                batch = [f'own:{{pid}}:{{t}}:{{j}}' for j in range(i - 9, i + 1)]
                assert len(cache.get_many(batch)) == 10
                cache.delete_many(batch[:5])
                assert sorted(cache.get_many(batch)) == sorted(batch[5:])
    except BaseException as error:
        errors.append(repr(error))

threads = [threading.Thread(target=work, args=(t,)) for t in range(THREADS)]
for thread in threads:
    thread.start()
for thread in threads:
    thread.join()
assert not errors, errors
assert len(set(handles)) == 1, handles  # the racing opens converged on one handle, used by every thread
assert handles[0] == id(next(iter(djangocache.handles.values())))
print(sum(wins))
"""


@pytest.mark.parametrize('shape', sorted(SHAPES))
def test_8_processes_of_4_threads_through_django_core_cache(tmp_path: Path, shape: str) -> None:
    processes, threads, rounds = 8, 4, 300
    location = tmp_path / 'cache'
    code = settings_code(location, SHAPES[shape]) + textwrap.dedent(WORKER.format(rounds=rounds))
    parent = make(location, SHAPES[shape])
    parent.set('n', 0, None)
    workers = [
        subprocess.Popen([sys.executable, '-c', code], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        for _ in range(processes)
    ]
    try:
        ready = [f'ready{worker.pid}:{t}' for worker in workers for t in range(threads)]
        wait_for(parent, ready, workers, 'got ready')
        parent.set('go', True, None)
        outputs = [worker.communicate(timeout=300) for worker in workers]
    finally:
        for worker in workers:
            if worker.poll() is None:
                worker.kill()
    assert all(worker.returncode == 0 for worker in workers), [err for _, err in outputs]
    assert parent.get('n') == processes * threads * rounds  # incr never lost one
    assert sum(int(out) for out, _ in outputs) == rounds  # each add won by exactly one thread anywhere
    for worker in workers:  # every thread's surviving keys are its own, intact
        for t in range(threads):
            last = f'own:{worker.pid}:{t}:{rounds - 1}'
            assert parent.get(last) == {'pid': worker.pid, 't': t, 'i': rounds - 1}
    assert parent.check() == []


# Forks from a warm table, with threads busy in the parent.

FORKER = """
import os, threading, time, warnings
warnings.simplefilter('ignore', DeprecationWarning)  # fork with threads running: deliberate here
from django.core.cache import caches
from diskcache_oxide import djangocache

CHILDREN, THREADS, ROUNDS = 4, 4, {rounds}
cache = caches['default']
cache.set('n', 0, None)
assert len(djangocache.handles) == 1  # warm: the table holds the open handle before any fork

stop, parent_incrs = threading.Event(), [0, 0]
def busy(slot):
    while not stop.is_set():
        caches['default'].incr('n')
        parent_incrs[slot] += 1
busy_threads = [threading.Thread(target=busy, args=(slot,)) for slot in range(2)]
for thread in busy_threads:
    thread.start()
time.sleep(0.05)

def child():
    errors = []
    cache.incr('n')  # the instance the parent built before the fork, as a preload_app worker uses it
    def work(t):
        try:
            c = caches['default']
            for i in range(ROUNDS):
                c.incr('n')
                c.set(f'child:{{os.getpid()}}:{{t}}:{{i}}', i)
                assert c.get(f'child:{{os.getpid()}}:{{t}}:{{i}}') == i
        except BaseException as error:
            errors.append(repr(error))
    threads = [threading.Thread(target=work, args=(t,)) for t in range(THREADS)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    if errors or len(djangocache.handles) != 1:
        print(errors, djangocache.handles, flush=True)
        os._exit(1)
    os._exit(0)

pids = []
for _ in range(CHILDREN):  # each fork lands while the parent's threads are mid-call
    pid = os.fork()
    if pid == 0:
        child()
    pids.append(pid)
    time.sleep(0.01)
codes = [os.waitstatus_to_exitcode(os.waitpid(pid, 0)[1]) for pid in pids]
stop.set()
for thread in busy_threads:
    thread.join()
assert codes == [0] * CHILDREN, codes
expected = CHILDREN * (THREADS * ROUNDS + 1) + sum(parent_incrs)
assert cache.get('n') == expected, (cache.get('n'), expected)
assert cache.check() == []
print('ok', expected)
"""


@pytest.mark.parametrize('shape', sorted(SHAPES))
def test_forks_from_a_warm_table_keep_every_count(tmp_path: Path, shape: str) -> None:
    code = settings_code(tmp_path / 'cache', SHAPES[shape]) + textwrap.dedent(FORKER.format(rounds=300))
    result = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, timeout=300, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.startswith('ok')


# A SIGKILL in the middle of a delete_many.

DELETER = """
import sys
from django.core.cache import cache
keys = [f'k{{i}}' for i in range({count})]
cache.set_many({{key: (i, 'x' * {pad}) for i, key in enumerate(keys)}}, None)
print('deleting', flush=True)
cache.delete_many(keys)
print('done', flush=True)
"""


def chunk_groups(backend: DjangoCache, keys: list[str]) -> list[list[str]]:
    """The order delete_many deletes in: one group (or one per shard, in first-seen order), each in
    chunks of DELETE_CHUNK, each chunk one transaction."""
    cache = backend.cache
    made = {backend.make_and_validate_key(key): key for key in keys}
    if not hasattr(cache, 'shard_of'):
        return [keys]
    groups: dict[int, list[str]] = {}
    for made_key, key in made.items():
        groups.setdefault(cache.shard_of(made_key), []).append(key)  # ty: ignore[call-non-callable]
    return list(groups.values())


@pytest.mark.parametrize('shape', sorted(SHAPES))
@pytest.mark.parametrize(('count', 'pad'), [(20_000, 0), (1_000, 70_000)], ids=['small', 'value-files'])
def test_a_sigkill_during_delete_many_leaves_whole_chunks(tmp_path: Path, shape: str, count: int, pad: int) -> None:
    # 'value-files': each value is over 64 KiB, so it lives in a file of its own, and a kill can land
    # between a chunk's commit and the removal of its files.
    chunk = djangocache.DELETE_CHUNK
    keys = [f'k{i}' for i in range(count)]
    landed_mid_way = 0
    for attempt, delay in enumerate((0.001, 0.003, 0.006, 0.01, 0.02, 0.04, 0.08, 0.15)):
        location = tmp_path / f'cache{attempt}'
        code = settings_code(location, SHAPES[shape]) + textwrap.dedent(DELETER.format(count=count, pad=pad))
        worker = subprocess.Popen(
            [sys.executable, '-c', code], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        assert worker.stdout is not None
        try:
            assert worker.stdout.readline().strip() == 'deleting', worker.communicate()[1]
            time.sleep(delay)
            os.kill(worker.pid, signal.SIGKILL)
            worker.wait(timeout=60)
        finally:
            if worker.poll() is None:
                worker.kill()
        backend = make(location, SHAPES[shape])
        present = backend.get_many(keys)
        assert all(present[key] == (int(key[1:]), 'x' * pad) for key in present)  # what's left is intact
        # Whole chunks only, in order: every group before the one being deleted is gone, that one has
        # lost a whole number of chunks from its front, and every group after it is untouched.
        reached_unfinished = False
        for group in chunk_groups(backend, keys):
            left = [key for key in group if key in present]
            gone = len(group) - len(left)
            assert left == group[gone:], 'a key survived inside the deleted part'
            if reached_unfinished:
                assert gone == 0, 'a group after the unfinished one was touched'
            elif gone < len(group):
                reached_unfinished = True
                assert gone % chunk == 0, f'{gone} deleted: not whole chunks of {chunk}'
        assert backend.check() == []
        backend.set('after', 'works')  # and the cache still takes writes
        assert backend.get('after') == 'works'
        if 0 < len(present) < count:
            landed_mid_way += 1
        djangocache.close_all()
    assert landed_mid_way, 'no SIGKILL landed in the middle of the delete_many'
