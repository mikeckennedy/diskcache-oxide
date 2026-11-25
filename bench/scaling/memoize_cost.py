"""What a memoized hit costs on top of a `get` (plan 34, 3e).

Usage (AC power, a quiet machine, not under nice):
    venv/bin/python bench/scaling/memoize_cost.py
    .venv-ft/bin/python bench/scaling/memoize_cost.py

Nanoseconds a call, the median of 7 runs of 200,000 calls, one thread, on one cached entry:

- `get`: `cache.get(key, miss, retry=True)` with the key tuple already built: the floor;
- memoized, positional: `f(1, 'a')`, which takes the wrapper's fast path for positional-only calls;
- memoized, old path: what the wrapper did before the fast path (`f.__cache_key__(...)`, then `get`);
- memoized, a keyword: `f(1, b='a')`, which still builds its key with `args_to_key`.
"""

import statistics
import sys
import tempfile
import time
from typing import Any, Callable

import diskcache_oxide

CALLS = 200_000
RUNS = 7


def per_call_ns(call: Callable[[], Any]) -> float:
    runs = []
    for _ in range(RUNS):
        start = time.perf_counter_ns()
        for _ in range(CALLS):
            call()
        runs.append((time.perf_counter_ns() - start) / CALLS)
    return statistics.median(runs)


def main() -> None:
    with tempfile.TemporaryDirectory(prefix='dco-memoize-') as directory, diskcache_oxide.Cache(directory) as cache:

        @cache.memoize(name='f')
        def f(a: int, b: str = 'a') -> str:
            return b * a

        f(1, 'a')
        f(1, b='a')
        miss = object()
        key = f.__cache_key__(1, 'a')
        get = cache.get

        def old_path() -> Any:
            found = get(f.__cache_key__(1, 'a'), miss, retry=True)
            return found if found is not miss else None

        cases = {
            'get, key already built': lambda: get(key, miss, retry=True),
            'memoized, positional (fast path)': lambda: f(1, 'a'),
            'memoized, old path': old_path,
            'memoized, a keyword (args_to_key)': lambda: f(1, b='a'),
            'empty lambda (loop overhead)': lambda: None,
        }
        print(f'Python {sys.version.split()[0]}, GIL {"on" if sys._is_gil_enabled() else "off"}; ns a call\n')
        for name, call in cases.items():
            print(f'{name:36} {per_call_ns(call):8.0f}', flush=True)


if __name__ == '__main__':
    main()
