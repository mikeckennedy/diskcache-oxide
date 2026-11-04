"""Latency gate: diskcache-oxide against diskcache, interleaved in the same session.

Usage (from the repo root, on AC power):
    venv/bin/python -m bench.gate            # 3.14
    .venv-ft/bin/python -m bench.gate        # 3.14t

Each round runs bench.baseline in a fresh process for diskcache, then for each oxide variant,
alternating (ABAB...), so drift in machine state hits every backend equally. The verdict uses the
median p50 across rounds. Oxide passes a latency workload if its median p50 <= diskcache's x
TOLERANCE (or its stricter TARGETS ratio), and each threaded workload (99:1 and 1,000:1, at 1/2/4/8 threads) if its
median
throughput >= diskcache's / TOLERANCE.
"""

from __future__ import annotations

import argparse
import json
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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--rounds', type=int, default=4)
    args = parser.parse_args()
    set_title('gate')

    samples: dict[str, list[dict[str, Any]]] = {v: [] for v in VARIANTS}
    with tempfile.TemporaryDirectory(prefix='dco-gate-') as tmp:
        for r in range(args.rounds):
            for variant in VARIANTS:
                samples[variant].append(run_once(variant, Path(tmp) / f'{variant}-{r}.json'))
                print(f'round {r + 1}/{args.rounds}: {variant} done', file=sys.stderr)

    def median_of(variant: str, workload: str, field: str) -> float:
        return statistics.median(s[workload][field] for s in samples[variant])

    build = 'free-threaded' if sysconfig.get_config_var('Py_GIL_DISABLED') else 'GIL'
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

    print()
    for variant, ok in passed.items():
        print(f'{variant}: {"GO" if ok else "NO-GO"}')
    raise SystemExit(0 if any(passed.values()) else 1)


if __name__ == '__main__':
    main()
