"""Sweep the SQLite mutex spin (plan 12 step 5: the budget in build-order step 2, the adaptive spin in 3a).

Needs a release build made with RUSTFLAGS="--cfg dco_spin_sweep", which reads its overrides once at
import: DCO_SPIN_HINTS (the budget), DCO_SPIN_MODE (fixed, cap, decay, both, decay+probe),
DCO_SPIN_CAP (a number, or half), DCO_SPIN_FLOOR, DCO_SPIN_NO_CPU_RULE=1 (spin at 2 or fewer CPUs
too) and, on glibc, DCO_SPIN_ADAPTIVE=1 (FAST mutexes of the adaptive type, no custom spin). A
shipped build has none. DCO_SPIN_MODE, DCO_SPIN_CAP, DCO_SPIN_FLOOR and DCO_SPIN_NO_CPU_RULE exist only
with bench/results/adaptive-spin-2026-11-05.diff applied (5a-2 didn't ship); without it those arms run
the fixed spin, and only number, adaptive and default arms mean anything. Each arm runs in a fresh
process; arms are interleaved round by round, and the table reports each case's median.

Usage (from the repo root):
    PY bench/scaling/spin_sweep.py TMPDIR [--arms 0,fixed,cap1,decay,...] [--rounds 5]
        [--threads 1,4,8] [--fanout 8] [--phase 8,4]

An arm is a number (that budget, the shipped policy), `adaptive`, `default` (no override: what a
shipped build picks on the host), a name in ARMS, or `name:KEY=V+KEY=V` for any other mix (the keys
without their DCO_SPIN_ prefix). Cases: shared `Cache` and `FanoutCache(8)` at 32 connections at each
of --threads; `FanoutCache(8)` at its default budget (one per shard when the sweep ran, two since
2026-11-06) at each of --fanout, with the number of slot waits it reached; and --phase A,B, which
runs A threads and then scores B threads on the same handle in the same process (a load drop: the
adaptive state left by A is what B starts from).
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

# The 5a-2 arms, each at the fixed 1,024-hint budget.
ARMS: dict[str, dict[str, str]] = {
    'fixed': {'MODE': 'fixed'},
    'cap1': {'MODE': 'cap', 'CAP': '1'},
    'cap2': {'MODE': 'cap', 'CAP': '2'},
    'caphalf': {'MODE': 'cap', 'CAP': 'half'},
    'decay': {'MODE': 'decay'},
    'decay16': {'MODE': 'decay', 'FLOOR': '16'},
    'decay64': {'MODE': 'decay', 'FLOOR': '64'},
    'both': {'MODE': 'both', 'CAP': '1'},
    'both2': {'MODE': 'both', 'CAP': '2'},
    'bothhalf': {'MODE': 'both', 'CAP': 'half'},
    'probe': {'MODE': 'decay+probe'},
}


def ints(text: str) -> list[int]:
    return [int(x) for x in text.split(',') if x]


def child(tmp: str, threads: list[int], fanout: list[int], phase: list[int]) -> None:
    import diskcache_oxide as d
    from diskcache_oxide import _core

    assert not _core._DEBUG_BUILD, 'measure a release build'
    budget = getattr(_core, '_SWEEP_SPIN_BUDGET', None)
    assert budget is not None, 'not a sweep build (RUSTFLAGS="--cfg dco_spin_sweep")'
    assert _core._sqlite_mutex_status() == 'installed'
    policy = list(getattr(_core, '_SWEEP_SPIN_POLICY', ()))
    slot_waits = getattr(_core, '_sweep_slot_waits', lambda: -1)

    def fill(c):
        for i, k in enumerate(KEYS):
            c.set(k, f'{i:06x}')
        return c

    def run(cache, n: int) -> float:
        lists = [random.Random(t).choices(KEYS, k=N) for t in range(n)]
        bar = threading.Barrier(n + 1)

        def w(i: int) -> None:
            get, ks = cache.get, lists[i]
            bar.wait()
            for k in ks:
                get(k)

        ts = [threading.Thread(target=w, args=(i,)) for i in range(n)]
        for t in ts:
            t.start()
        bar.wait()
        s = time.perf_counter()
        for t in ts:
            t.join()
        return n * N / (time.perf_counter() - s) / 1000

    plain = fill(d.Cache(tempfile.mkdtemp(dir=tmp), timeout=1.0))
    fan = fill(d.FanoutCache(tempfile.mkdtemp(dir=tmp), shards=8, timeout=1.0))
    fan32 = fill(d.FanoutCache(tempfile.mkdtemp(dir=tmp), shards=8, timeout=1.0, max_connections=32))
    run(plain, 4)  # warm-up
    out: dict = {'budget': budget, 'policy': policy}
    for n in threads:
        out[f'cache {n}t'] = run(plain, n)
        out[f'c32 {n}t'] = run(fan32, n)
    for n in fanout:
        before = slot_waits()
        out[f'fanout8 {n}t'] = run(fan, n)
        out[f'fanout8 {n}t waits'] = slot_waits() - before
    if phase:
        a, b = phase
        for name, cache in (('cache', plain), ('c32', fan32)):
            run(cache, a)
            out[f'{name} {a}>{b}t'] = run(cache, b)
    print(json.dumps(out))


def arm_env(arm: str) -> dict[str, str]:
    """The DCO_SPIN_* overrides for one arm (every other DCO_SPIN_ variable is cleared)."""
    if arm == 'default':
        return {}
    if arm == 'adaptive':
        return {'DCO_SPIN_HINTS': '0', 'DCO_SPIN_ADAPTIVE': '1'}
    if arm.isdigit():
        return {'DCO_SPIN_HINTS': arm}
    name, _, extra = arm.partition(':')
    keys = dict(ARMS.get(name, {}))
    for kv in filter(None, extra.split('+')):
        k, v = kv.split('=', 1)
        keys[k] = v
    assert keys, f'unknown arm {arm}'
    keys.setdefault('HINTS', '1024')
    return {f'DCO_SPIN_{k}': v for k, v in keys.items()}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('tmp')
    ap.add_argument('--arms', default='0,fixed,cap1,decay,both,probe')
    ap.add_argument('--rounds', type=int, default=5)
    ap.add_argument('--threads', default='1,4,8')
    ap.add_argument('--fanout', default='8')
    ap.add_argument('--phase', default='8,4')
    ap.add_argument('--child', action='store_true')
    a = ap.parse_args()
    threads, fanout, phase = ints(a.threads), ints(a.fanout), ints(a.phase)
    assert len(phase) in (0, 2), '--phase takes two thread counts'
    if a.child:
        child(a.tmp, threads, fanout, phase)
        return
    arms = a.arms.split(',')
    results: dict[str, list[dict]] = {arm: [] for arm in arms}
    policies: dict[str, list] = {}
    for r in range(a.rounds):
        for arm in arms:
            env = {k: v for k, v in os.environ.items() if not k.startswith('DCO_SPIN_')}
            want = arm_env(arm)
            env.update(want)
            with tempfile.TemporaryDirectory(dir=a.tmp) as tmp:
                cmd = [sys.executable, __file__, tmp, '--child', '--threads', a.threads, '--fanout', a.fanout]
                cmd += ['--phase', a.phase]
                line = (
                    subprocess.run(cmd, env=env, capture_output=True, text=True, check=True, timeout=900)
                    .stdout.strip()
                    .splitlines()[-1]
                )
            row = json.loads(line)
            budget, policy = row.pop('budget'), row.pop('policy')
            if 'DCO_SPIN_HINTS' in want:
                assert budget == int(want['DCO_SPIN_HINTS']), f'the override did not take: {line}'
            if 'DCO_SPIN_FLOOR' in want:
                assert policy[3] == int(want['DCO_SPIN_FLOOR']), f'the floor did not take: {line}'
            policies[arm] = policy
            results[arm].append(row)
            print(f'round {r + 1} {arm:>9}: ' + ', '.join(f'{k} {v:,.0f}' for k, v in row.items()), flush=True)
    cases = list(results[arms[0]][0])
    print('\nMedian of', a.rounds, 'rounds, thousands of gets/s (waits: slot waits per run)')
    for arm, policy in policies.items():
        print(f'{arm}: policy (budget, cap, mode, floor) = {policy}')
    print('| Case | ' + ' | '.join(arms) + ' |')
    print('|---|' + '---|' * len(arms))
    for case in cases:
        meds = [statistics.median(row[case] for row in results[arm]) for arm in arms]
        print(f'| {case} | ' + ' | '.join(f'{m:,.0f}' for m in meds) + ' |')
    print('\nRanges (min-max)')
    for case in cases:
        vals = [[row[case] for row in results[arm]] for arm in arms]
        rng = [f'{min(v):,.0f}-{max(v):,.0f}' for v in vals]
        print(f'| {case} | ' + ' | '.join(rng) + ' |')
    print('\nRAW ' + json.dumps({'policies': policies, 'results': results}))


if __name__ == '__main__':
    main()
