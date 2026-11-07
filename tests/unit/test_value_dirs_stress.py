"""Writers in several processes against a process that removes every empty value directory (plan 17).

`rmdir` removes only an empty directory, so the adversary can never lose a value; what it does is
win the race between a writer's `mkdir` and its temp file's create, over and over, which the
write's retry must absorb. Every value is read back, and the tree ends with no empty directory.
"""

from __future__ import annotations

import os
import threading
import time
import traceback
from pathlib import Path
from typing import Any

from diskcache_oxide import Cache

from tests.unit.spawn_race import SPAWN

PROCESSES = 4
THREADS = 2
ROUNDS = 150


def _value(worker: int, thread: int, i: int) -> bytes:
    return bytes([worker * THREADS + thread]) * (70_000 + i)


def _writer(directory: str, worker: int, barrier: Any, results: Any) -> None:
    try:
        failures: list[str] = []
        with Cache(directory) as cache:
            barrier.wait()

            def run(thread: int) -> None:
                for i in range(ROUNDS):
                    key = f'{worker}:{thread}:{i % 10}'
                    value = _value(worker, thread, i)
                    cache.set(key, value)
                    if cache.get(key) != value:
                        failures.append(f'{key} read back wrong in round {i}')
                    if i % 3 == 0:
                        cache.delete(key)

            threads = [threading.Thread(target=run, args=(t,)) for t in range(THREADS)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        results.put(('ok', failures))
    except Exception:
        results.put(('crash', traceback.format_exc()))


def _adversary(directory: str, stop: Any, barrier: Any, results: Any) -> None:
    try:
        barrier.wait()
        removed = 0
        root = Path(directory)
        while not stop.is_set():
            for path in root.glob('??'):
                try:
                    os.rmdir(path)
                    removed += 1
                except OSError:
                    pass
        results.put(('ok', removed))
    except Exception:
        results.put(('crash', traceback.format_exc()))


def test_writers_survive_a_process_that_removes_every_empty_value_directory(tmp_path: Path) -> None:
    directory = str(tmp_path / 'c')
    Cache(directory).close()
    barrier = SPAWN.Barrier(PROCESSES + 1)
    results = SPAWN.Queue()
    stop = SPAWN.Event()
    writers = [SPAWN.Process(target=_writer, args=(directory, w, barrier, results)) for w in range(PROCESSES)]
    adversary = SPAWN.Process(target=_adversary, args=(directory, stop, barrier, results))
    for p in (*writers, adversary):
        p.start()
    outcomes = [results.get(timeout=120) for _ in writers]
    stop.set()
    outcomes.append(results.get(timeout=30))
    for p in (*writers, adversary):
        p.join(30)
    assert [p.exitcode for p in (*writers, adversary)] == [0] * (PROCESSES + 1)
    assert not [o for o in outcomes if o[0] == 'crash'], outcomes
    assert [o[1] for o in outcomes[:-1]] == [[]] * PROCESSES, 'every value read back as written'
    assert outcomes[-1][1] > 0, 'the adversary removed directories, so the race really ran'

    # The surviving values are all readable, and once their directories are a minute old a close
    # leaves none empty.
    with Cache(directory) as cache:
        for key in cache:
            worker, thread, _ = (int(part) for part in str(key).split(':'))
            assert cache.get(key)[:1] == bytes([worker * THREADS + thread])
    then = time.time() - 60
    for path in Path(directory).glob('??'):
        os.utime(path, (then, then))
    Cache(directory).close()
    empty = [p for p in Path(directory).glob('??') if not any(p.iterdir())]
    assert empty == []
