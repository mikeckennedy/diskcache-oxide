"""Sweep the SQLite mutex spin budget (plan 12 step 5, build-order step 2).

Needs a release build made with RUSTFLAGS="--cfg dco_spin_sweep", which reads DCO_SPIN_HINTS (and,
on glibc, DCO_SPIN_ADAPTIVE=1: FAST mutexes of the adaptive type, no custom spin) once at import. A
shipped build has neither. Each arm runs in a fresh process; arms are interleaved round by round, and
the table reports each case's median.

Usage (from the repo root):
    PY bench/scaling/spin_sweep.py TMPDIR [--arms 0,256,1024,4096[,adaptive][,default]] [--rounds 5] [--over 36]

`default` sets no override, so it measures, and reports, the budget a shipped build picks on the host.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import statistics
import subprocess
import sys
import tempfile
import threading
import time

N = 60_000
KEYS = [f'https://talkpython.fm/static/img/{i:05d}/cover.webp?{i}' for i in range(700)]


def child(tmp: str, over: int) -> None:
    import diskcache_oxide as d
    from diskcache_oxide import _core

    assert not _core._DEBUG_BUILD, 'measure a release build'
    budget = getattr(_core, '_SWEEP_SPIN_BUDGET', None)
    assert budget is not None, 'not a sweep build (RUSTFLAGS="--cfg dco_spin_sweep")'
    assert _core._sqlite_mutex_status() == 'installed'

    def fill(c):
        for i, k in enumerate(KEYS):
            c.set(k, f'{i:06x}')
        return c

    def run(cache, threads: int) -> float:
        lists = [random.Random(t).choices(KEYS, k=N) for t in range(threads)]
        bar = threading.Barrier(threads + 1)

        def w(i: int) -> None:
            get, ks = cache.get, lists[i]
            bar.wait()
            for k in ks:
                get(k)

        ts = [threading.Thread(target=w, args=(i,)) for i in range(threads)]
        for t in ts:
            t.start()
        bar.wait()
        s = time.perf_counter()
        for t in ts:
            t.join()
        return threads * N / (time.perf_counter() - s) / 1000

    plain = fill(d.Cache(tempfile.mkdtemp(dir=tmp), timeout=1.0))
    fan = fill(d.FanoutCache(tempfile.mkdtemp(dir=tmp), shards=8, timeout=1.0))
    fan32 = fill(d.FanoutCache(tempfile.mkdtemp(dir=tmp), shards=8, timeout=1.0, max_connections=32))
    run(plain, 4)  # warm-up
    out = {
        'budget': budget,
        'cache 1t': run(plain, 1),
        'cache 4t': run(plain, 4),
        'fanout8 1t': run(fan, 1),
        'fanout8 4t': run(fan, 4),
        'fanout8 c32 4t': run(fan32, 4),
        f'cache {over}t': run(plain, over),
        f'fanout8 {over}t': run(fan, over),
    }
    print(json.dumps(out))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('tmp')
    ap.add_argument('--arms', default='0,256,1024,4096')
    ap.add_argument('--rounds', type=int, default=5)
    ap.add_argument('--over', type=int, default=36)
    ap.add_argument('--child', action='store_true')
    a = ap.parse_args()
    if a.child:
        child(a.tmp, a.over)
        return
    arms = a.arms.split(',')
    results: dict[str, list[dict]] = {arm: [] for arm in arms}
    budgets: dict[str, int] = {}
    for r in range(a.rounds):
        for arm in arms:
            env = dict(os.environ)
            # 'default': no override, so the budget is what a shipped build picks on this host.
            env.pop('DCO_SPIN_HINTS', None)
            if arm != 'default':
                env['DCO_SPIN_HINTS'] = '0' if arm == 'adaptive' else arm
            env['DCO_SPIN_ADAPTIVE'] = '1' if arm == 'adaptive' else '0'
            with tempfile.TemporaryDirectory(dir=a.tmp) as tmp:
                line = (
                    subprocess.run(
                        [sys.executable, __file__, tmp, '--child', '--over', str(a.over)],
                        env=env,
                        capture_output=True,
                        text=True,
                        check=True,
                        timeout=600,
                    )
                    .stdout.strip()
                    .splitlines()[-1]
                )
            row = json.loads(line)
            budget = row.pop('budget')
            if arm == 'default':
                budgets[arm] = budget
            else:
                expected = 0 if arm == 'adaptive' else int(arm)
                assert budget == expected, f'the override did not take: {line}'
            results[arm].append(row)
            print(f'round {r + 1} {arm:>8}: ' + ', '.join(f'{k} {v:,.0f}' for k, v in row.items()), flush=True)
    cases = list(results[arms[0]][0])
    print('\nMedian of', a.rounds, 'rounds, thousands of gets/s')
    for arm, budget in budgets.items():
        print(f'{arm}: budget {budget} hints')
    print('| Case | ' + ' | '.join(arms) + ' |')
    print('|---|' + '---|' * len(arms))
    for case in cases:
        meds = [statistics.median(row[case] for row in results[arm]) for arm in arms]
        print(f'| {case} | ' + ' | '.join(f'{m:,.0f}' for m in meds) + ' |')


if __name__ == '__main__':
    main()
