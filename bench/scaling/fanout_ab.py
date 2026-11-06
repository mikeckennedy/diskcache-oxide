"""Run bench/fanout.py for two or more builds, interleaved round by round (plan 12 step 5, build-order step 4).

bench.fanout interleaves its variants inside one interpreter; this interleaves interpreters. Each round
runs `python -m bench.fanout --rounds 1` once per arm (the arm order rotates each round), and the table
reports, per case, the median over rounds of each round's value, with the min-max. ops/s in thousands.

Usage (from the repo root, which holds the bench package every arm runs):
    PY bench/scaling/fanout_ab.py OUTDIR --arm before=/path/bin/python --arm after=/path/bin/python \
        [--rounds 5] [--variants oxide-cache oxide-fanout8 oxide-fanout8-c32] [fanout.py args after --]
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('out', type=Path)
    ap.add_argument('--arm', action='append', default=[], metavar='NAME=PYTHON')
    ap.add_argument('--rounds', type=int, default=5)
    ap.add_argument('--variants', nargs='+', default=['oxide-cache', 'oxide-fanout8', 'oxide-fanout8-c32'])
    ap.add_argument('rest', nargs='*', help='Extra bench.fanout arguments (after --).')
    a = ap.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    arms = [tuple(spec.split('=', 1)) for spec in a.arm]
    # samples[arm][variant][case] -> one value per round
    samples: dict[str, dict[str, dict[str, list[float]]]] = {name: {} for name, _ in arms}
    loads = []
    for r in range(a.rounds):
        order = arms[r % len(arms) :] + arms[: r % len(arms)]
        loads.append(round(os.getloadavg()[0], 2))
        for name, python in order:
            path = a.out / f'{name}-r{r + 1}.json'
            cmd = [python, '-m', 'bench.fanout', '--rounds', '1', '--out', str(path), '--variant-list', *a.variants]
            subprocess.run(cmd + a.rest, cwd=ROOT, check=True, stdout=subprocess.DEVNULL)
            for variant, cases in json.loads(path.read_text())['results'].items():
                for case, cell in cases.items():
                    samples[name].setdefault(variant, {}).setdefault(case, []).append(cell['median'])
            print(f'round {r + 1}/{a.rounds}: {name} done (load {os.getloadavg()[0]:.1f})', file=sys.stderr, flush=True)
    names = [name for name, _ in arms]
    print(f'\nMedian (min-max) of {a.rounds} rounds, thousands of ops/s; 1-min load before each round: {loads}')
    for variant in a.variants:
        print(f'\n{variant}\n| Case | ' + ' | '.join(names) + ' |')
        print('|---|' + '---|' * len(names))
        cases = sorted(
            samples[names[0]][variant],
            key=lambda c: (c.rsplit('-', 1)[0], int(c.rsplit('-', 1)[1]) if c.rsplit('-', 1)[1].isdigit() else 0),
        )
        for case in cases:
            cells = []
            for name in names:
                vals = [v / 1000 for v in samples[name][variant][case]]
                cells.append(f'{statistics.median(vals):,.0f} ({min(vals):,.0f}-{max(vals):,.0f})')
            print(f'| {case} | ' + ' | '.join(cells) + ' |')
    (a.out / 'summary.json').write_text(json.dumps({'loads': loads, 'samples': samples}, indent=1))


if __name__ == '__main__':
    main()
