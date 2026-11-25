"""The Python boundary on free-threaded builds: what in a call into `_core` stops scaling (plan 34, 3c).

Usage (AC power, nothing else heavy running):
    .venv-ft/bin/python bench/scaling/boundary.py
    venv/bin/python bench/scaling/boundary.py

Millions of calls a second at 1, 2, 4 and 8 threads, each thread calling in a share-nothing loop. Every case is
paired with a control that does the same thing to a plain Python object, so a fall that the control shares belongs to
CPython (a shared object's reference count, a shared function), not to `_core`.

- "closure": each call reaches the object through a closure variable, which takes a reference to the one shared
  object on every call, from every thread.
- "bound": each thread binds the method once, so a call touches no shared reference in the caller.
"""

import sys
import tempfile
import threading
import time
import types
from typing import Any, Callable

import diskcache_oxide


class Plain:
    """A pure-Python stand-in with a method, for the bound-method control."""

    timeout = 60.0

    def get(self, key: str) -> str:
        return key


def rate(make: Callable[[], Callable[[], Any]], threads: int, seconds: float = 1.0) -> float:
    counts = [0] * threads
    stop = threading.Event()
    barrier = threading.Barrier(threads + 1)

    def worker(n: int) -> None:
        call = make()
        barrier.wait()
        done = 0
        while not stop.is_set():
            for _ in range(1_000):
                call()
            done += 1_000
        counts[n] = done

    pool = [threading.Thread(target=worker, args=(n,)) for n in range(threads)]
    for thread in pool:
        thread.start()
    barrier.wait()
    start = time.perf_counter()
    time.sleep(seconds)
    stop.set()
    for thread in pool:
        thread.join()
    return sum(counts) / (time.perf_counter() - start) / 1e6


def closure_attribute(obj: Any) -> Callable[[], Callable[[], Any]]:
    def make() -> Callable[[], Any]:
        def call() -> Any:
            return obj.timeout

        return call

    return make


def bound(method_of: Callable[[], Callable[[str], Any]], key: str) -> Callable[[], Callable[[], Any]]:
    def make() -> Callable[[], Any]:
        method = method_of()

        def call() -> Any:
            return method(key)

        return call

    return make


def main() -> None:
    cache = diskcache_oxide.Cache(tempfile.mkdtemp(prefix='dco-boundary-'))
    cache.set('k', 'v')
    plain = Plain()
    namespace = types.SimpleNamespace(timeout=60.0)
    cases = {
        'closure: Cache.timeout (oxide getter)': closure_attribute(cache),
        'closure: SimpleNamespace.timeout (control)': closure_attribute(namespace),
        'closure: Plain.timeout (control)': closure_attribute(plain),
        'bound: Cache.get, a hit': bound(lambda: cache.get, 'k'),
        'bound: Cache.get, a miss': bound(lambda: cache.get, 'missing'),
        'bound: Plain.get (control)': bound(lambda: plain.get, 'k'),
        'bound: dict.get (control)': bound(lambda: {'k': 'v'}.get, 'k'),
    }
    counts = (1, 2, 4, 8)
    print(
        f'Python {sys.version.split()[0]}, GIL {"on" if sys._is_gil_enabled() else "off"}; millions of calls a second'
    )
    print()
    print('| Case | ' + ' | '.join(str(c) for c in counts) + ' |')
    print('|---' * (len(counts) + 1) + '|')
    for name, make in cases.items():
        print(f'| {name} | ' + ' | '.join(f'{rate(make, c):.2f}' for c in counts) + ' |', flush=True)
    cache.close()


if __name__ == '__main__':
    main()
