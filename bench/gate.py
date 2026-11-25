"""Latency gate: diskcache-oxide against diskcache, interleaved in the same session.

Usage (from the repo root, on AC power):
    venv/bin/python -m bench.gate            # 3.14
    .venv-ft/bin/python -m bench.gate        # 3.14t

On Windows the gate of record runs on the PGO wheel from scripts/build_pgo.ps1, installed in a venv
of its own with diskcache 5.6.3 and pytest, from the repo root (plan 25, decision 4).

Each round runs bench.baseline in a fresh process for diskcache, then for each oxide variant,
alternating (ABAB...), so drift in machine state hits every backend equally. The verdict uses the
median p50 across rounds. Oxide passes a latency workload if its median p50 <= diskcache's x
TOLERANCE (or its stricter TARGETS ratio), and each threaded workload (99:1 and 1,000:1, at 1/2/4/8 threads) if its
median throughput >= diskcache's / TOLERANCE.

It also judges oxide's own scaling (plan 34, step 4), in the same rounds: a shared plain Cache and a
shared FanoutCache(8) at 1,000:1 over 1, 2 and 4 threads (free-threaded builds only, bench.threads),
and the same two read by 1, 2 and 4 processes (bench.read_procs). Each is the rate at 2 and 4 as a
ratio to its own rate at 1, against a floor for this machine (SCALING_FLOORS, keyed by --machine,
which defaults to the host name). A machine with no floors gets its ratios printed and no verdict.
--no-scaling skips the section.
"""

from __future__ import annotations

import argparse
import json
import platform
import statistics
import subprocess
import sys
import sysconfig
import tempfile
from pathlib import Path
from typing import Any

from tests.proctitle import set_title

TOLERANCE = 1.05
LATENCY = (
    'get_hit_small',
    'get_miss',
    'get_hit_small_dict',
    'set_small',
    'get_random_wide',
)
THROUGHPUT = ('threads_99to1', 'threads_1000to1')
# Stricter ceilings for some latency workloads, as a ratio to diskcache (no extra tolerance). They
# replace Phase 2's absolute targets, which don't carry over between machines (architect's Phase 2
# review). `set_small` writes as many WAL pages as diskcache, so a SQLite upsert sets its floor.
TARGETS = {'get_hit_small': 0.55, 'set_small': 0.85}
THREAD_COUNTS = ('1', '2', '4', '8')

# Scaling rows: (kind, variant). 'threads' rows run only on free-threaded builds.
SCALING_ROWS = (
    ('threads', 'oxide Cache, shared'),
    ('threads', 'oxide FanoutCache(8), shared'),
    ('processes', 'oxide Cache'),
    ('processes', 'oxide FanoutCache(8)'),
)
SCALING_COUNTS = (1, 2, 4)
# How long each process-scaling point runs (thread points run SCALING_SECONDS).
SCALING_PROCESS_SECONDS = 1.5
# Floors for rate(n) / rate(1), per machine, row and count (one set for both builds): about
# 80% of what plan 34's step 1 measured, for the noise between runs (thread runs spread up to 13%).
# They are regression guards, not scaling targets: a floor below 1.0 (the Mac's FanoutCache threads at 2,
# its plain Cache processes at 4) passes a rate that falls, because 0.5.8 already falls there.
# Evidence, 2026-11-24, ratios at 2 and 4 as measured:
# - mac-studio-m5-max (M5 Max, 6 performance cores of 18), 0.5.8 wheels. Threads, 1,000:1, 5 rounds
#   (bench/results/threads-mac-studio-m5-max.md): Cache 1.24 and 1.30 on 3.14t, 1.34 and 1.54 on
#   3.15t; FanoutCache(8) 1.14 and 1.58, 1.15 and 1.75. Processes, reads only, 5 rounds
#   (read-procs-3.14.10*-mac-studio-m5-max.md): Cache 1.40 and 0.96 on 3.14, 1.33 and 0.90 on
#   3.14t (macOS's per-file lock: it falls past 2); FanoutCache(8) 1.64 and 2.66, 1.63 and 2.65.
# - htz (x86-64, 8 vCPUs, in Docker), 0.5.8 wheels (bench/results/linux/htz-2026-11-24/). Threads
#   on 3.14t, 7 rounds: Cache 1.67 and 2.68, FanoutCache(8) 1.37 and 3.04. Processes: Cache 1.71
#   and 3.07 on 3.14, 1.62 and 2.81 on 3.14t; FanoutCache(8) 1.76 and 3.18, 1.84 and 3.37.
SCALING_FLOORS: dict[str, dict[tuple[str, str], dict[int, float]]] = {
    'mac-studio-m5-max': {
        ('threads', 'oxide Cache, shared'): {2: 1.0, 4: 1.05},
        ('threads', 'oxide FanoutCache(8), shared'): {2: 0.9, 4: 1.25},
        ('processes', 'oxide Cache'): {2: 1.05, 4: 0.7},
        ('processes', 'oxide FanoutCache(8)'): {2: 1.3, 4: 2.1},
    },
    'htz': {
        ('threads', 'oxide Cache, shared'): {2: 1.35, 4: 2.15},
        ('threads', 'oxide FanoutCache(8), shared'): {2: 1.1, 4: 2.4},
        ('processes', 'oxide Cache'): {2: 1.3, 4: 2.25},
        ('processes', 'oxide FanoutCache(8)'): {2: 1.4, 4: 2.5},
    },
}
SCALING_SECONDS = 1.0

# Name -> extra bench.baseline arguments. diskcache is the reference.
VARIANTS: dict[str, list[str]] = {
    'diskcache': ['--backend', 'diskcache'],
    'oxide': ['--backend', 'oxide'],
}


def run_once(variant: str, out: Path) -> dict[str, Any]:
    only = [arg for name in (*LATENCY, *THROUGHPUT) for arg in ('--only', name)]
    cmd = [
        sys.executable,
        '-m',
        'bench.baseline',
        *VARIANTS[variant],
        *only,
        '--out',
        str(out),
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL)
    return json.loads(out.read_text())['results']


def scaling_once(rows: list[tuple[str, str]], tmp: Path) -> dict[tuple[str, str], dict[int, float]]:
    """One round of the scaling rows: calls a second at each count."""
    from bench import read_procs

    counts = ','.join(str(c) for c in SCALING_COUNTS)
    got: dict[tuple[str, str], dict[int, float]] = {}
    for kind, variant in rows:
        if kind == 'threads':
            cmd = [
                sys.executable,
                '-m',
                'bench.threads',
                '--worker',
                variant,
                '--mixes',
                '1000:1',
                '--counts',
                counts,
                '--seconds',
                str(SCALING_SECONDS),
            ]
            out = subprocess.run(cmd, check=True, capture_output=True, text=True)
            rates = json.loads(out.stdout.strip().splitlines()[-1])['results']['1000:1']
            got[(kind, variant)] = {int(c): rate for c, rate in rates.items()}
        else:
            spec = next(v for v in read_procs.VARIANTS if v.name == variant)
            directory = tempfile.mkdtemp(dir=tmp)
            read_procs.populate(spec, directory)
            got[(kind, variant)] = {
                c: read_procs.measure(spec, [directory], c, SCALING_PROCESS_SECONDS)[0] for c in SCALING_COUNTS
            }
    return got


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--rounds', type=int, default=4)
    parser.add_argument(
        '--machine',
        default=platform.node().split('.')[0] or 'unknown',
        help='Which SCALING_FLOORS to judge against (default: the host name).',
    )
    parser.add_argument('--no-scaling', action='store_true', help="Skip oxide's own scaling rows.")
    args = parser.parse_args()
    free_threaded = bool(sysconfig.get_config_var('Py_GIL_DISABLED'))
    scaling_rows = [] if args.no_scaling else [r for r in SCALING_ROWS if r[0] == 'processes' or free_threaded]
    scaling: dict[tuple[str, str], dict[int, list[float]]] = {r: {c: [] for c in SCALING_COUNTS} for r in scaling_rows}
    set_title('gate')

    samples: dict[str, list[dict[str, Any]]] = {v: [] for v in VARIANTS}
    with tempfile.TemporaryDirectory(prefix='dco-gate-') as tmp:
        for r in range(args.rounds):
            for variant in VARIANTS:
                samples[variant].append(run_once(variant, Path(tmp) / f'{variant}-{r}.json'))
                print(f'round {r + 1}/{args.rounds}: {variant} done', file=sys.stderr)
            if scaling_rows:
                for row, rates in scaling_once(scaling_rows, Path(tmp)).items():
                    for c, rate in rates.items():
                        scaling[row][c].append(rate)
                print(f'round {r + 1}/{args.rounds}: scaling done', file=sys.stderr)

    def median_of(variant: str, workload: str, field: str) -> float:
        return statistics.median(s[workload][field] for s in samples[variant])

    build = 'free-threaded' if free_threaded else 'GIL'
    print(
        f'\nPython {sys.version.split()[0]} ({build}), {args.rounds} interleaved rounds, tolerance {TOLERANCE:.2f}x\n'
    )
    print(f'{"workload":28} {"variant":14} {"p50 us / ops":>14} {"vs diskcache":>13}  verdict')

    passed: dict[str, bool] = {v: True for v in VARIANTS if v != 'diskcache'}
    rows = [(w, 'p50_us', w) for w in LATENCY] + [(w, n, f'{w} x{n}') for w in THROUGHPUT for n in THREAD_COUNTS]
    for workload, field, label in rows:
        higher_is_better = workload in THROUGHPUT
        reference = median_of('diskcache', workload, field)
        for variant in VARIANTS:
            value = median_of(variant, workload, field)
            if variant == 'diskcache':
                print(f'{label:28} {variant:14} {value:14,.3f}')
                continue
            ratio = value / reference
            limit = TARGETS.get(label, TOLERANCE)
            ok = ratio >= 1 / TOLERANCE if higher_is_better else ratio <= limit
            passed[variant] &= ok
            target = f' (target <= {limit:.2f}x)' if label in TARGETS else ''
            print(f'{label:28} {variant:14} {value:14,.3f} {ratio:12.2f}x  {"pass" if ok else "FAIL"}{target}')

    floors = SCALING_FLOORS.get(args.machine)
    if scaling_rows:
        judged = 'no floors for this machine, so no verdict' if floors is None else 'against its floors'
        print(f'\noxide scaling on {args.machine} ({judged}): median calls a second, and the ratio to 1\n')
        for row in scaling_rows:
            kind, variant = row
            one = statistics.median(scaling[row][1])
            print(f'{kind:9} {variant:30} x1 {one:12,.0f}')
            for c in SCALING_COUNTS[1:]:
                ratio = statistics.median(scaling[row][c]) / one
                floor = None if floors is None else floors[row][c]
                ok = floor is None or ratio >= floor
                passed['oxide'] &= ok
                verdict = '' if floor is None else f'  {"pass" if ok else "FAIL"} (floor {floor:.2f})'
                print(f'{kind:9} {variant:30} x{c} {statistics.median(scaling[row][c]):12,.0f} {ratio:6.2f}x{verdict}')

    unjudged = f' (scaling not judged: no floors for {args.machine!r})' if scaling_rows and floors is None else ''
    print()
    for variant, ok in passed.items():
        note = unjudged if variant == 'oxide' and ok else ''
        print(f'{variant}: {"GO" if ok else "NO-GO"}{note}')
    raise SystemExit(0 if any(passed.values()) else 1)


if __name__ == '__main__':
    main()
