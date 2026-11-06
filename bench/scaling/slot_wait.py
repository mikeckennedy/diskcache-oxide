"""Before/after probe for the slot wait (plan 12 step 5, build-order step 3, 5b).

Each arm is an interpreter with a diskcache-oxide release build installed, plus optional environment
overrides (a `--cfg dco_spin_sweep` build reads DCO_SLOT_SPIN and DCO_SLOT_SPINNERS;
a shipped build has no knob). Each arm runs in a fresh process, arms are interleaved round by round,
and the table reports each case's median, thousands of gets/s, one shared handle per case.

Usage (from the repo root):
    PY bench/scaling/slot_wait.py TMPDIR --arm before=/path/bin/python --arm after=/path/bin/python \
        [--arm 'spin0=/path/bin/python:DCO_SLOT_SPIN=0'] [--rounds 5] [--over 36]
"""

from __future__ import annotations

import argparse
import json
import os
import random
import statistics
import subprocess
import tempfile
import threading
import time

N = 60_000
KEYS = [f'https://talkpython.fm/static/img/{i:05d}/cover.webp?{i}' for i in range(700)]


def child(tmp: str, over: int) -> None:
    import diskcache_oxide as d
    from diskcache_oxide import _core

    assert not _core._DEBUG_BUILD, 'measure a release build'
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
        'slot_wait': list(getattr(_core, '_SWEEP_SLOT_WAIT', ())) or None,
        'cache 1t': run(plain, 1),
        'cache 4t': run(plain, 4),
        'fanout8 1t': run(fan, 1),
        'fanout8 4t': run(fan, 4),
        'fanout8 c32 4t': run(fan32, 4),
        f'cache {over}t': run(plain, over),
        f'fanout8 {over}t': run(fan, over),
        f'fanout8 c32 {over}t': run(fan32, over),
    }
    print(json.dumps(out))


def parse_arm(spec: str) -> tuple[str, str, dict[str, str]]:
    name, rest = spec.split('=', 1)
    python, _, envs = rest.partition(':')
    env = dict(kv.split('=', 1) for kv in envs.split(',') if kv)
    return name, python, env


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('tmp')
    ap.add_argument('--arm', action='append', default=[])
    ap.add_argument('--rounds', type=int, default=5)
    ap.add_argument('--over', type=int, default=36)
    ap.add_argument('--child', action='store_true')
    a = ap.parse_args()
    if a.child:
        child(a.tmp, a.over)
        return
    arms = [parse_arm(s) for s in a.arm]
    results: dict[str, list[dict]] = {name: [] for name, _, _ in arms}
    for r in range(a.rounds):
        for name, python, overrides in arms:
            env = {k: v for k, v in os.environ.items() if not k.startswith('DCO_')}
            env.update(overrides)
            with tempfile.TemporaryDirectory(dir=a.tmp) as tmp:
                line = (
                    subprocess.run(
                        [python, __file__, tmp, '--child', '--over', str(a.over)],
                        env=env,
                        capture_output=True,
                        text=True,
                        check=True,
                        timeout=900,
                    )
                    .stdout.strip()
                    .splitlines()[-1]
                )
            row = json.loads(line)
            slot_wait = row.pop('slot_wait')
            knobs = ('DCO_SLOT_SPIN', 'DCO_SLOT_SPINNERS')
            if any(k in overrides for k in knobs):
                assert slot_wait is not None, f'{name}: not a sweep build'
                want = [int(overrides.get(k, have)) for k, have in zip(knobs, slot_wait, strict=True)]
                assert slot_wait == want, f'{name}: the override did not take: {slot_wait} != {want}'
            results[name].append(row)
            print(f'round {r + 1} {name:>10}: ' + ', '.join(f'{k} {v:,.0f}' for k, v in row.items()), flush=True)
    names = [name for name, _, _ in arms]
    cases = list(results[names[0]][0])
    print('\nMedian of', a.rounds, 'rounds, thousands of gets/s')
    print('| Case | ' + ' | '.join(names) + ' |')
    print('|---|' + '---|' * len(names))
    for case in cases:
        meds = [statistics.median(row[case] for row in results[name]) for name in names]
        print(f'| {case} | ' + ' | '.join(f'{m:,.0f}' for m in meds) + ' |')


if __name__ == '__main__':
    main()
