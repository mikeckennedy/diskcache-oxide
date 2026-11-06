"""Builds report.html (charts) and summary.md (verdict) from a soak run directory's saved data.

    python -m soak.report soak-results/latest

The orchestrator calls build() every interval, so the report can be opened mid-run. Standard library
only: the charts are inline SVG, and the page has no external resources.

Leak analysis: for each long-lived worker (never killed, so it lives the whole run), fit a line
through RSS and through open fds against time, skipping the warm-up, and report the slope.
"""

from __future__ import annotations

import csv
import html
import json
import sys
from pathlib import Path
from typing import Any

# Growth past these, sustained over the run, is flagged as a suspected leak.
RSS_LEAK_MIB_PER_H = 2.0
FDS_LEAK_PER_H = 1.0
WARMUP_FRACTION = 0.1
MIN_FIT_HOURS = 0.5
WAL_WARN_BYTES = 512 * 2**20
# p99 in the last hour above this multiple of the first hour's is flagged as latency drift.
DRIFT_RATIO = 2.0

COLORS = [
    '#2563eb',
    '#dc2626',
    '#16a34a',
    '#9333ea',
    '#ea580c',
    '#0891b2',
    '#ca8a04',
    '#db2777',
]


def read_csv(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows = []
    with path.open(newline='') as handle:
        for row in csv.DictReader(handle):
            rows.append({k: number(v) for k, v in row.items()})
    return rows


def number(value: str | None) -> Any:
    if value in (None, ''):
        return None
    try:
        return int(value)
    except ValueError:
        try:
            return float(value)
        except ValueError:
            return value


def fit(points: list[tuple[float, float]]) -> float | None:
    """Least-squares slope of y over x, or None with too few points."""
    if len(points) < 5:
        return None
    n = len(points)
    mx = sum(x for x, _ in points) / n
    my = sum(y for _, y in points) / n
    var = sum((x - mx) ** 2 for x, _ in points)
    return None if var == 0 else sum((x - mx) * (y - my) for x, y in points) / var


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------


def analyze(run: Path) -> dict[str, Any]:
    timeline = read_csv(run / 'timeline.csv')
    workers = read_csv(run / 'workers.csv')
    maint = read_csv(run / 'maint.csv')
    events = []
    if (run / 'events.jsonl').exists():
        for line in (run / 'events.jsonl').read_text().splitlines():
            try:
                events.append(json.loads(line))
            except ValueError:
                pass
    hours = timeline[-1]['elapsed_h'] if timeline else 0.0
    warmup = max(0.25, hours * WARMUP_FRACTION) if hours > 1 else hours * WARMUP_FRACTION

    leaks: list[dict[str, Any]] = []
    for wid in sorted({w['id'] for w in workers if w.get('long_lived')}):
        rows = [w for w in workers if w['id'] == wid and w['elapsed_h'] >= warmup]
        if not rows or rows[-1]['elapsed_h'] - rows[0]['elapsed_h'] < MIN_FIT_HOURS:
            continue
        rss = fit([(r['elapsed_h'], r['rss_mib']) for r in rows if r.get('rss_mib') is not None])
        fds = fit([(r['elapsed_h'], r['fds']) for r in rows if r.get('fds') is not None])
        leaks.append(
            {
                'id': wid,
                'python': rows[0].get('python'),
                'rss_start': rows[0]['rss_mib'],
                'rss_end': rows[-1]['rss_mib'],
                'rss_slope': rss,
                'fds_start': rows[0]['fds'],
                'fds_end': rows[-1]['fds'],
                'fds_slope': fds,
                'rss_flag': rss is not None and rss > RSS_LEAK_MIB_PER_H,
                'fds_flag': fds is not None and fds > FDS_LEAK_PER_H,
            }
        )

    drift: dict[str, dict[str, Any]] = {}
    if hours >= 2:
        for col in ('get_p99_us', 'set_p99_us', 'get_p50_us', 'set_p50_us'):
            first = [t[col] for t in timeline if t['elapsed_h'] <= 1 and t.get(col)]
            last = [t[col] for t in timeline if t['elapsed_h'] >= hours - 1 and t.get(col)]
            if first and last:
                a, b = sorted(first)[len(first) // 2], sorted(last)[len(last) // 2]
                drift[col] = {
                    'first_hour': a,
                    'last_hour': b,
                    'ratio': b / a if a else None,
                    'flag': bool(a) and b / a > DRIFT_RATIO,
                }

    wal_max = max(
        (max(t.get('chaos_wal_bytes') or 0, t.get('ledger_wal_bytes') or 0) for t in timeline),
        default=0,
    )
    problems = [e for e in events if e.get('problem')]
    kinds: dict[str, int] = {}
    for e in problems:
        kinds[e['kind']] = kinds.get(e['kind'], 0) + 1
    finished = any(e['kind'] == 'finish' for e in events)
    # The auto group's WAL counts too, so a store that outruns its limit badly shows as a flag.
    wal_max = max(wal_max, max((t.get('auto_peak_wal') or 0 for t in timeline), default=0))
    flags = (
        sum(lk['rss_flag'] + lk['fds_flag'] for lk in leaks)
        + sum(d['flag'] for d in drift.values())
        + (wal_max > WAL_WARN_BYTES)
    )
    start = next((e for e in events if e['kind'] == 'start'), {})
    lanes = next((e for e in reversed(events) if e['kind'] == 'lanes'), None)
    return {
        'lanes': lanes,
        'timeline': timeline,
        'workers': workers,
        'maint': maint,
        'events': events,
        'problems': problems,
        'problem_kinds': kinds,
        'leaks': leaks,
        'drift': drift,
        'wal_max': wal_max,
        'hours': hours,
        'finished': finished,
        'flags': flags,
        'start': start,
        'verdict': 'PASS' if not problems and not flags else ('FAIL' if problems else 'WARN'),
    }


# ---------------------------------------------------------------------------
# Charts (inline SVG)
# ---------------------------------------------------------------------------


def chart(
    title: str,
    series: dict[str, list[tuple[float, float]]],
    unit: str = '',
    log: bool = False,
) -> str:
    series = {k: [(x, y) for x, y in v if y is not None and (not log or y > 0)] for k, v in series.items()}
    series = {k: v for k, v in series.items() if v}
    if not series:
        return f'<figure><figcaption>{html.escape(title)}</figcaption><p class="empty">no data yet</p></figure>'
    import math

    w, h, left, right, top, bottom = 720, 240, 64, 12, 12, 32
    xs = [x for v in series.values() for x, _ in v]
    ys = [y for v in series.values() for _, y in v]
    x0, x1 = min(xs), max(xs) or 1
    if x1 == x0:
        x1 = x0 + 1
    tf = (lambda y: math.log10(y)) if log else (lambda y: y)
    y0, y1 = (tf(min(ys)), tf(max(ys))) if log else (min(0.0, min(ys)), max(ys))
    if y1 == y0:
        y1 = y0 + 1

    def px(x):
        return left + (x - x0) / (x1 - x0) * (w - left - right)

    def py(y):
        return top + (1 - (tf(y) - y0) / (y1 - y0)) * (h - top - bottom)

    parts = [f'<svg viewBox="0 0 {w} {h}" role="img" aria-label="{html.escape(title)}">']
    for i in range(5):
        frac = i / 4
        yv = (10 ** (y0 + frac * (y1 - y0))) if log else (y0 + frac * (y1 - y0))
        yy = top + (1 - frac) * (h - top - bottom)
        parts.append(f'<line class="grid" x1="{left}" x2="{w - right}" y1="{yy:.1f}" y2="{yy:.1f}"/>')
        parts.append(f'<text class="tick" x="{left - 6}" y="{yy + 4:.1f}" text-anchor="end">{fmt(yv)}</text>')
    for i in range(6):
        xv = x0 + i / 5 * (x1 - x0)
        parts.append(f'<text class="tick" x="{px(xv):.1f}" y="{h - 10}" text-anchor="middle">{xv:.1f}h</text>')
    for n, (name, pts) in enumerate(sorted(series.items())):
        color = COLORS[n % len(COLORS)]
        path = ' '.join(f'{px(x):.1f},{py(y):.1f}' for x, y in pts)
        parts.append(
            f'<polyline fill="none" stroke="{color}" stroke-width="1.6" points="{path}"><title>{html.escape(name)}'
            f'</title></polyline>'
        )
    parts.append('</svg>')
    legend = ''.join(
        f'<span><i style="background:{COLORS[n % len(COLORS)]}"></i>{html.escape(k)}</span>'
        for n, k in enumerate(sorted(series))
    )
    suffix = f' ({unit})' if unit else ''
    return (
        f'<figure><figcaption>{html.escape(title)}{suffix}{" · log scale" if log else ""}</figcaption>{"".join(parts)}'
        f'<div class="legend">{legend}</div></figure>'
    )


def fmt(v: float) -> str:
    if abs(v) >= 1e6:
        return f'{v / 1e6:.1f}M'
    if abs(v) >= 1e3:
        return f'{v / 1e3:.1f}k'
    return f'{v:.0f}' if abs(v) >= 10 else f'{v:.2g}'


def col(rows: list[dict[str, Any]], key: str, scale: float = 1.0) -> list[tuple[float, float]]:
    return [(r['elapsed_h'], r[key] * scale) for r in rows if r.get(key) is not None]


def per_worker(workers: list[dict[str, Any]], key: str, long_only: bool) -> dict[str, list[tuple[float, float]]]:
    out: dict[str, list[tuple[float, float]]] = {}
    for r in workers:
        if long_only and not r.get('long_lived'):
            continue
        if r.get(key) is not None:
            out.setdefault(f'worker {r["id"]} ({r.get("python")})', []).append((r['elapsed_h'], r[key]))
    return out


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def build(run: Path, final: bool) -> dict[str, Any]:
    a = analyze(run)
    t = a['timeline']
    mib = 1 / 2**20
    charts = [
        chart('Throughput', {'ops/s': col(t, 'ops_per_s')}),
        chart(
            'get latency',
            {
                'p50': col(t, 'get_p50_us'),
                'p99 (worst worker)': col(t, 'get_p99_us'),
                'max': col(t, 'get_max_us'),
            },
            'µs',
            log=True,
        ),
        chart(
            'set latency',
            {
                'p50': col(t, 'set_p50_us'),
                'p99 (worst worker)': col(t, 'set_p99_us'),
                'max': col(t, 'set_max_us'),
            },
            'µs',
            log=True,
        ),
        chart(
            'RSS of long-lived workers (leak check)',
            per_worker(a['workers'], 'rss_mib', True),
            'MiB',
        ),
        chart(
            'Open fds of long-lived workers (leak check)',
            per_worker(a['workers'], 'fds', True),
        ),
        chart(
            'Total RSS and fds, all workers',
            {'RSS MiB': col(t, 'rss_total_mib'), 'fds': col(t, 'fds_total')},
        ),
        chart(
            'Database and WAL size',
            {
                k: col(t, k, mib)
                for k in (
                    'chaos_db_bytes',
                    'chaos_wal_bytes',
                    'ledger_db_bytes',
                    'ledger_wal_bytes',
                )
            },
            'MiB',
        ),
        chart(
            'Rows and value files',
            {
                'chaos rows': col(t, 'chaos_rows'),
                'chaos value files': col(t, 'chaos_value_files'),
                'ledger rows': col(t, 'ledger_rows'),
                'chaos orphan files': col(t, 'chaos_orphans'),
                'ledger orphan files': col(t, 'ledger_orphans'),
            },
        ),
        chart(
            'Cumulative problems, stalls and timeouts',
            {
                'problems': col(t, 'problems'),
                'stalls': col(t, 'stalls'),
                'timeouts': col(t, 'timeouts_total'),
            },
        ),
        chart(
            'Chaos: kills, restarts, forks and maintenance calls',
            {
                'kills': col(t, 'kills'),
                'restarts': col(t, 'restarts'),
                'forks ok': col(t, 'forks_ok'),
                'forks failed': col(t, 'forks_failed'),
                'maintenance calls': col(t, 'maint_calls'),
                'held commits': col(t, 'held_commits'),
                'held rollbacks': col(t, 'held_rollbacks'),
            },
        ),
        chart(
            'Forced WAL checkpoints (D-024)',
            {
                'attempts': col(t, 'wal_ckpt_attempts'),
                'busy': col(t, 'wal_ckpt_busy'),
                'slow (>= 100 ms)': col(t, 'wal_ckpt_slow'),
            },
        ),
        chart(
            'Worst forced-checkpoint pause',
            {'max pause': col(t, 'wal_ckpt_max_pause_us')},
            'µs',
        ),
        chart(
            'pop calls',
            {
                'chaos': col(t, 'pops_chaos'),
                'held': col(t, 'pops_held'),
                'fork': col(t, 'pops_fork'),
                'exactly-once': col(t, 'pops_pq'),
            },
        ),
        chart(
            'Exactly-once lane',
            {
                'produced': col(t, 'pq_produced'),
                'delivered': col(t, 'pq_delivered'),
                'duplicates': col(t, 'pq_duplicates'),
            },
        ),
        chart(
            'Durable lane',
            {
                'calls': col(t, 'durable_calls'),
                'answered as failures': col(t, 'durable_false'),
                'escaped': col(t, 'durable_escapes'),
            },
        ),
        chart(
            'Forks behind traffic and stuck forks',
            {
                'forks behind': col(t, 'forks_behind'),
                'over budget': col(t, 'budget_overruns'),
                'stuck forks': col(t, 'stuck_forks'),
                'callers hung': col(t, 'stuck_callers_hung'),
            },
        ),
        chart(
            'Forced checkpoints, auto group',
            {
                'rounds': col(t, 'auto_rounds'),
                'successes': col(t, 'auto_successes'),
                'busy': col(t, 'auto_busy'),
                'fork pending': col(t, 'auto_fork_pending'),
            },
        ),
        chart('Load', {'1-minute': col(t, 'load_1m')}),
        chart('Peak WAL, auto group', {'peak': col(t, 'auto_peak_wal', mib)}, 'MiB'),
        chart(
            'Maintenance call duration',
            {
                op: [(m['elapsed_h'], m['seconds']) for m in a['maint'] if m['op'] == op]
                for op in ('clear', 'expire', 'cull')
            },
            's',
        ),
    ]
    page = render_html(a, charts, final)
    tmp = run / 'report.tmp'
    tmp.write_text(page)
    tmp.replace(run / 'report.html')
    if final:
        (run / 'summary.md').write_text(render_summary(a))
    return a


def verdict_lines(a: dict[str, Any]) -> list[str]:
    lines = []
    if a['problems']:
        lines.append(
            f'{len(a["problems"])} problem event(s): '
            + ', '.join(f'{k} ×{n}' for k, n in sorted(a['problem_kinds'].items()))
        )
    for lk in a['leaks']:
        if lk['rss_flag']:
            lines.append(
                f'worker {lk["id"]} ({lk["python"]}): RSS grows {lk["rss_slope"]:.2f} MiB/h ({lk["rss_start"]} → '
                f'{lk["rss_end"]} MiB)'
            )
        if lk['fds_flag']:
            lines.append(
                f'worker {lk["id"]} ({lk["python"]}): open fds grow {lk["fds_slope"]:.2f}/h ({lk["fds_start"]} → '
                f'{lk["fds_end"]})'
            )
    for name, d in a['drift'].items():
        if d['flag']:
            lines.append(
                f'{name} drifted {d["ratio"]:.1f}× (first hour {d["first_hour"]:.0f} µs, last hour {d["last_hour"]:.0f}'
                f' µs)'
            )
    if a['wal_max'] > WAL_WARN_BYTES:
        lines.append(f'a WAL file reached {a["wal_max"] / 2**20:.0f} MiB (checkpoints starved?)')
    return lines or ['no problems, no leak or drift flags']


def lane_lines(a: dict[str, Any]) -> list[str]:
    """Plan 09's lanes, one line each, or nothing before the run's final checks."""
    lanes = a.get('lanes')
    if not lanes:
        return []
    t, pq = lanes['totals'], lanes.get('pq') or {}
    lines = [
        f'pop: {t["pops"]:,} calls (chaos {t["pops_chaos"]:,}, held {t["pops_held"]:,}, fork {t["pops_fork"]:,}, '
        f'exactly-once {t["pops_pq"]:,}). Locked-fallback hits: not observable, no counter reaches Python.',
        f'exactly-once: {pq.get("produced", 0):,} tokens recorded, {pq.get("delivered", 0):,} delivered, '
        f'{pq.get("duplicates", 0)} duplicates, {pq.get("queued", 0)} still queued, {pq.get("lost", 0)} lost '
        f'({pq.get("lost_excused_by_kills", 0)} excused by a kill, allowance {pq.get("kill_allowance", 0)}), '
        f'{pq.get("unrecorded", 0)} with no record (a kill between add and record).',
        f'durable: {t["durable_calls"]:,} calls, {t["durable_false"]:,} answered as failures with the read-back '
        f'checked, {t["durable_misses"]:,} misses on a key that was there, {t["durable_escapes"]} exceptions escaped. '
        f'Swallowed: busy {t["durable_busy"]:,}, broken {t["durable_broken"]}, lost deletes '
        f'{t["durable_lost_deletes"]:,}. Log lines by bucket: {lanes.get("durable_lines")}. `_durable_stats()` by '
        f'operation: {lanes.get("durable_by_op")}.',
        f'forks behind traffic: {t["forks_behind"]:,} forks over {t["behind_calls"]:,} budgeted calls, '
        f'{t["behind_timeouts"]:,} timed out, worst overrun {t["max_overrun_s"]} s, {t["budget_overruns"]} over the '
        f'allowance. Longest os.fork(): {t["fork_max_s"]} s, longest forked child: {t["child_max_s"]} s.',
        f'fork_timeouts: {t["fork_timeouts_normal"]} in the normal lanes (must be 0), {t["fork_timeouts_lanes"]} in '
        f'the lanes with small timeouts.',
        f'stuck forks: {t["stuck_forks"]} (callers timed out {t["stuck_callers_timed_out"]}, finished after the level '
        f'went {t["stuck_forks_completed"]}, callers hung {t["stuck_callers_hung"]}, longest caller '
        f'{t["stuck_caller_max_s"]} s).',
        f'peak 1-minute load: {max((row.get("load_1m") or 0 for row in a["timeline"]), default=0)} on '
        f'{a["start"].get("cpus")} CPUs.',
        f'side chaos on the lanes and auto group: {lanes.get("side_kills")} kills, {lanes.get("side_restarts")} '
        f'restarts.',
    ]
    if 'fanout_ops' in t:  # Runs from before the fanout role have none of these.
        lines.append(
            f'fanout: {t["fanout_ops"]:,} routed calls, {t["fanout_merges"]:,} merges, {t["fanout_full_walks"]:,} '
            f'full walks, {t["fanout_maint"]:,} maintenance calls ({t["fanout_maint_partial"]} partial), '
            f'{t["fanout_forks"]:,} forks. Highest sampled database fds in one process: '
            f'{t.get("fanout_max_sampled_fds")} (budget 32, checked every walk, not continuously).'
        )
    return lines


def auto_rows(a: dict[str, Any]) -> list[list[str]]:
    lanes = a.get('lanes') or {}
    rows = []
    for store, st in (lanes.get('auto') or {}).items():
        peak, limit = st['peak_wal'] / 2**20, (st.get('limit') or 0) / 2**20
        rate = '?' if st['success_rate'] is None else f'{st["success_rate"] * 100:.2f}%'
        rows.append(
            [
                str(store),
                str(st['writers']),
                f'{peak:.1f} / {limit:.0f}',
                f'{st["rounds"]:,}',
                f'{st["successes"]:,}',
                f'{st["busy"]:,}',
                f'{st["fork_pending"]:,}',
                rate,
                f'{st["max_pause_us"] / 1000:.1f}',
            ]
        )
    return rows


AUTO_HEADER = [
    'store',
    'writers',
    'peak WAL / limit (MiB)',
    'rounds',
    'successes',
    'busy',
    'fork_pending',
    'success rate',
    'worst pause (ms)',
]
PAUSE_CAVEAT = (
    'Pauses (`wal_ckpt_max_pause_us` and this table) now sum every TRUNCATE try in a round (plan 09, step 2b), so they '
    'are not comparable with soaks before commit 5b2ea85, which timed one try.'
)


def render_summary(a: dict[str, Any]) -> str:
    out = [
        f'# Soak summary: {a["verdict"]}',
        '',
        f'Ran {a["hours"]:.2f} h. Args: `{json.dumps(a["start"].get("args", {}))}`',
        '',
    ]
    out += ['## Verdict', ''] + [f'- {line}' for line in verdict_lines(a)] + ['']
    out += [
        '## Memory and fd trends (long-lived workers, after warm-up)',
        '',
        '| worker | python | RSS start → end (MiB) | RSS slope (MiB/h) | fds start → end | fds slope (/h) |',
        '|---|---|---|---|---|---|',
    ]
    for lk in a['leaks']:
        rs = '?' if lk['rss_slope'] is None else f'{lk["rss_slope"]:.2f}'
        fs = '?' if lk['fds_slope'] is None else f'{lk["fds_slope"]:.2f}'
        out.append(
            f'| {lk["id"]} | {lk["python"]} | {lk["rss_start"]} → {lk["rss_end"]} | {rs} | {lk["fds_start"]} → '
            f'{lk["fds_end"]} | {fs} |'
        )
    out += [
        '',
        '## Latency drift (median of each hour)',
        '',
        '| metric | first hour | last hour | ratio |',
        '|---|---|---|---|',
    ]
    for name, d in a['drift'].items():
        out.append(f'| {name} | {d["first_hour"]:.1f} | {d["last_hour"]:.1f} | {d["ratio"]:.2f} |')
    if a['timeline']:
        last = a['timeline'][-1]
        out += ['', '## Totals', '']
        for key in (
            'kills',
            'restarts',
            'forks_ok',
            'forks_failed',
            'maint_calls',
            'stalls',
            'timeouts_total',
            'chaos_orphans',
            'ledger_orphans',
            'held_commits',
            'held_rollbacks',
        ):
            out.append(f'- {key}: {last.get(key)}')
    if a.get('lanes'):
        out += ['', '## Plan 09 lanes', ''] + [f'- {line}' for line in lane_lines(a)]
        out += [
            '',
            '### Forced checkpoints, auto group',
            '',
            '| ' + ' | '.join(AUTO_HEADER) + ' |',
            '|' + '---|' * len(AUTO_HEADER),
        ]
        out += ['| ' + ' | '.join(row) + ' |' for row in auto_rows(a)]
        limit = a['lanes'].get('auto_min_success_rate', 0)
        out += [
            '',
            f'The run fails under {limit:.0%} on a one-writer store, or with under '
            f'{a["lanes"].get("auto_min_rounds", 0)} rounds on a store. Several writers in several processes are a '
            f'documented limit, reported and not judged. {PAUSE_CAVEAT}',
        ]
        out += ['', '### By interpreter', '']
        per_python = a['lanes'].get('per_python') or {}
        keys = sorted({k for counts in per_python.values() for k in counts})
        out += [
            '| counter | ' + ' | '.join(per_python) + ' |',
            '|---|' + '---|' * len(per_python),
        ]
        out += [f'| {k} | ' + ' | '.join(f'{per_python[py].get(k, 0):,}' for py in per_python) + ' |' for k in keys]
    out += ['', '## Problem events (first 50)', '']
    for e in a['problems'][:50]:
        detail = {k: v for k, v in e.items() if k not in ('time', 'kind', 'problem', 'elapsed_h')}
        out.append(f'- {e["elapsed_h"]:.2f} h `{e["kind"]}` {json.dumps(detail, default=str)[:600]}')
    if not a['problems']:
        out.append('- none')
    out += [
        '',
        'Charts: report.html. Raw data: timeline.csv, workers.csv, maint.csv, events.jsonl. Stall dumps: stalls/.',
        '',
    ]
    return '\n'.join(out)


CSS = """
:root{--bg:#ffffff;--fg:#1f2937;--muted:#6b7280;--grid:#e5e7eb;--card:#f9fafb;--ok:#15803d;--bad:#b91c1c;--warn:#b45309}
@media (prefers-color-scheme:dark){
:root:not([data-theme=light]){--bg:#0f1115;--fg:#e5e7eb;--muted:#9ca3af;--grid:#2a2f3a;--card:#171a21;--ok:#4ade80;--bad:#f87171;--warn:#fbbf24}
}
:root[data-theme=dark]{--bg:#0f1115;--fg:#e5e7eb;--muted:#9ca3af;--grid:#2a2f3a;--card:#171a21;--ok:#4ade80;--bad:#f87171;--warn:#fbbf24}
body{background:var(--bg);color:var(--fg);font:14px/1.5 -apple-system,system-ui,sans-serif;
margin:0 auto;max-width:1500px;padding:16px}
h1{font-size:20px;margin:4px 0}.muted{color:var(--muted)}
.verdict{font-weight:700}.PASS{color:var(--ok)}.FAIL{color:var(--bad)}.WARN{color:var(--warn)}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(100%,640px),1fr));gap:12px}
figure{background:var(--card);border-radius:8px;margin:0;padding:10px}figcaption{font-weight:600;margin-bottom:4px}
svg{width:100%;height:auto}svg .grid{stroke:var(--grid)}svg .tick{fill:var(--muted);font-size:11px}
.legend{display:flex;flex-wrap:wrap;gap:4px 12px;font-size:12px;color:var(--muted)}
.legend i{display:inline-block;width:10px;height:3px;margin-right:4px;vertical-align:middle}
table{border-collapse:collapse;margin:8px 0}td,th{border-bottom:1px solid var(--grid);padding:3px 8px;text-align:left}
ul{margin:4px 0 12px 18px;padding:0}.empty{color:var(--muted)}
"""


def event_detail(event: dict[str, Any]) -> str:
    """A problem event's fields beyond the time, kind and elapsed hours, as JSON cut to 300 characters."""
    fields = {k: v for k, v in event.items() if k not in ('time', 'kind', 'problem', 'elapsed_h')}
    return json.dumps(fields, default=str)[:300]


def render_html(a: dict[str, Any], charts: list[str], final: bool) -> str:
    state = 'finished' if a['finished'] else 'running (refreshes every minute)'
    lane_items = ''.join(f'<li>{html.escape(line)}</li>' for line in lane_lines(a))
    auto_table = ''
    if auto_rows(a):
        auto_table = (
            '<table><tr>'
            + ''.join(f'<th>{html.escape(h)}</th>' for h in AUTO_HEADER)
            + '</tr>'
            + ''.join('<tr>' + ''.join(f'<td>{html.escape(c)}</td>' for c in row) + '</tr>' for row in auto_rows(a))
            + f'</table><p class="muted">{html.escape(PAUSE_CAVEAT)}</p>'
        )
    lane_section = f'<h2>Plan 09 lanes</h2><ul>{lane_items}</ul>{auto_table}' if lane_items else ''
    refresh = '' if final or a['finished'] else '<meta http-equiv="refresh" content="60">'
    verdict = ''.join(f'<li>{html.escape(line)}</li>' for line in verdict_lines(a))
    leak_rows = ''.join(
        f'<tr><td>{lk["id"]}</td><td>{html.escape(str(lk["python"]))}</td><td>{lk["rss_start"]} → {lk["rss_end"]}</td>'
        f'<td>{"?" if lk["rss_slope"] is None else f"{lk['rss_slope']:.2f}"}</td><td>{lk["fds_start"]} → '
        f'{lk["fds_end"]}</td>'
        f'<td>{"?" if lk["fds_slope"] is None else f"{lk['fds_slope']:.2f}"}</td></tr>'
        for lk in a['leaks']
    )
    leaks = (
        '<table><tr><th>worker</th><th>python</th><th>RSS MiB</th><th>MiB/h</th><th>fds</th><th>fds/h</th></tr>'
        + leak_rows
        + '</table>'
        if leak_rows
        else '<p class="muted">Leak fits need at least 30 minutes after warm-up.</p>'
    )
    recent = (
        ''.join(
            f'<li>{e["elapsed_h"]:.2f} h <code>{html.escape(e["kind"])}</code> {html.escape(event_detail(e))}</li>'
            for e in a['problems'][-20:]
        )
        or '<li>none</li>'
    )
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport"
content="width=device-width, initial-scale=1">{refresh}
<title>Soak Report</title><style>{CSS}</style></head><body>
<h1>diskcache-oxide soak: <span class="verdict {a['verdict']}">{a['verdict']}</span></h1>
<p class="muted">{a['hours']:.2f} h, {state}. Args: {html.escape(json.dumps(a['start'].get('args', {})))}</p>
<ul>{verdict}</ul>
{lane_section}
<h2>Leak check (long-lived workers)</h2>{leaks}
<div class="grid">{''.join(charts)}</div>
<h2>Latest problem events</h2><ul>{recent}</ul>
</body></html>
"""


def main() -> int:
    run = Path(sys.argv[1]).resolve()
    a = build(run, final=True)
    print(f'{a["verdict"]}: {run / "report.html"}, {run / "summary.md"}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
