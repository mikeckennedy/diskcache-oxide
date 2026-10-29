"""The soak orchestrator: runs workers, injects chaos, checks invariants and records everything.

Usage (soak/run.sh calls this; see soak/README.md):
    python -m soak.soak --hours 24 --pythons .venv-soak/bin/python,.venv-soak-ft/bin/python

Uses only the standard library and never imports diskcache_oxide, so its own SQLite reads (stdlib
sqlite3) can't disturb the extension's bundled copy in the same process.

Everything goes into one run directory (soak-results/<timestamp>/):
    status.txt     the latest status, rewritten every interval (`tail -F` or `watch cat` it)
    report.html    charts and the verdict, regenerated every interval (soak/report.py)
    timeline.csv   one row per interval for the whole system
    workers.csv    one row per worker per interval (RSS, fds, ops, latency, problems)
    maint.csv      every clear()/expire()/cull() call: duration, rows removed, outcome
    events.jsonl   every problem, stall, kill, respawn and check result, as JSON lines
    stalls/        native stack samples and Python tracebacks captured when something stalls
    logs/          each worker's stderr
    summary.md     the final verdict (written at the end)
    chaos/, ledger/, stats/   the caches under test, and the workers' latest counters
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import random
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
DB_NAME = 'diskcache-oxide.sqlite'

# A worker whose op count hasn't moved for this long is stalled (a deadlock or livelock suspect).
STALL_AFTER = 120.0
# A maintenance call (clear/expire/cull) that takes longer than this is a stall too.
MAINT_TIMEOUT = 300.0
# Files younger than this are skipped by the orphan scan (writes in flight).
ORPHAN_MIN_AGE = 120.0
# More unexpected worker deaths than this within CRASH_WINDOW seconds stops the run: the build or
# environment is broken, and respawning would only bury the first error.
CRASH_LIMIT = 5
CRASH_WINDOW = 60.0


@dataclass
class WorkerProc:
    id: int
    python: str
    long_lived: bool
    proc: subprocess.Popen[bytes]
    started: float
    log: Any
    last_ops: int = -1
    last_progress: float = field(default_factory=time.time)
    stalled: bool = False
    stats: dict[str, Any] = field(default_factory=dict)


class Soak:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.rng = random.Random(args.seed)
        # A fresh directory per run, even for two runs started in the same second.
        out = Path(args.out).resolve()
        out.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime('%Y%m%d-%H%M%S')
        for n in range(100):
            self.dir = out / (stamp if n == 0 else f'{stamp}-{n}')
            try:
                self.dir.mkdir()
                break
            except FileExistsError:
                continue
        for sub in ('stats', 'logs', 'stalls'):
            (self.dir / sub).mkdir()
        # Point `latest` here atomically: a new link, renamed over the old one.
        link = out / f'.latest-{os.getpid()}'
        link.unlink(missing_ok=True)
        link.symlink_to(self.dir.name)
        os.replace(link, out / 'latest')
        # absolute(), not resolve(): a venv's python is a symlink to the base interpreter, and
        # resolving it would run the workers outside the venv.
        self.pythons = [str(Path(p).absolute()) for p in args.pythons.split(',')]
        self.started = time.time()
        self.ends = self.started + args.hours * 3600 + args.minutes * 60
        self.workers: dict[int, WorkerProc] = {}
        self.next_id = 0
        self.problems = 0
        self.lock = threading.Lock()
        # Ledger bookkeeping: the last acknowledged count each worker reported (killed workers
        # keep theirs), every add() race a worker reported winning, and exact final counts.
        self.reported_acked: dict[int, int] = {}
        self.final_acked: dict[int, int] = {}
        self.once_winners: dict[str, set[int]] = {}
        self.counts = {'kills': 0, 'respawns': 0, 'maint': 0, 'stalls': 0}
        # Each worker's latest stats, kept after it exits, so run totals include killed workers.
        self.last_stats: dict[int, dict[str, Any]] = {}
        self.maint_running = False
        self.crashes: list[float] = []
        self.fatal = False
        self.events = (self.dir / 'events.jsonl').open('a', buffering=1)
        self.timeline = self._csv('timeline.csv', TIMELINE_FIELDS)
        self.workers_csv = self._csv('workers.csv', WORKER_FIELDS)
        self.maint_csv = self._csv('maint.csv', ['time', 'elapsed_h', 'op', 'seconds', 'removed', 'ok', 'detail'])
        self.last_total_ops = -1
        self.last_total_progress = time.time()
        self.interval_no = 0
        self.last_db: dict[str, dict[str, Any]] = {}
        self.last_orphans: dict[str, int] = {}

    def _csv(self, name: str, fields: list[str]) -> tuple[Any, csv.DictWriter[str]]:
        handle = (self.dir / name).open('a', newline='', buffering=1)
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        return handle, writer

    # -- events -------------------------------------------------------------------------------

    def event(self, kind: str, problem: bool = False, **data: Any) -> None:
        record = {'time': time.time(), 'elapsed_h': round(self.elapsed_h(), 4), 'kind': kind, 'problem': problem, **data}
        with self.lock:
            if problem:
                self.problems += 1
            self.events.write(json.dumps(record, default=str) + '\n')
        if problem:
            print(f'!!! {time.strftime("%H:%M:%S")} {kind}: {json.dumps(data, default=str)[:400]}', flush=True)

    def elapsed_h(self) -> float:
        return (time.time() - self.started) / 3600

    # -- workers ------------------------------------------------------------------------------

    def spawn(self, python: str, long_lived: bool) -> None:
        wid = self.next_id
        self.next_id += 1
        log = (self.dir / 'logs' / f'{wid}.log').open('ab')
        thread_rate = self.args.rate / (self.args.procs * self.args.threads) if self.args.rate else 0.0
        cmd = [
            python, '-m', 'soak.worker', 'run', str(self.dir), str(wid), str(self.args.threads),
            str(self.args.seed * 10_000 + wid), str(self.args.mix), str(thread_rate),
        ]  # fmt: skip
        proc = subprocess.Popen(cmd, cwd=REPO, stdout=log, stderr=log)
        self.workers[wid] = WorkerProc(wid, python, long_lived, proc, time.time(), log)
        self.event('spawn', id=wid, pid=proc.pid, python=python, long_lived=long_lived)

    def start_workers(self) -> None:
        for n in range(self.args.procs):
            # Alternate interpreters on the same caches. The first worker per interpreter is never
            # killed, so its memory and fd trends cover the whole run.
            self.spawn(self.pythons[n % len(self.pythons)], long_lived=n < len(self.pythons))

    def reap(self) -> None:
        for wid, w in list(self.workers.items()):
            code = w.proc.poll()
            if code is None:
                continue
            del self.workers[wid]
            w.log.close()
            self.read_stats(w)
            if code == -signal.SIGKILL and not w.long_lived:
                continue  # Our own chaos kill; already logged.
            tail = (self.dir / 'logs' / f'{wid}.log').read_text(errors='replace')[-3000:]
            self.event('worker-died', problem=True, id=wid, exit=code, log_tail=tail)
            now = time.time()
            self.crashes = [t for t in self.crashes if now - t < CRASH_WINDOW] + [now]
            if len(self.crashes) > CRASH_LIMIT:
                self.event('crash-loop', problem=True, deaths=len(self.crashes), window_s=CRASH_WINDOW, last_log_tail=tail)
                print(f'soak: {len(self.crashes)} workers died within {CRASH_WINDOW:.0f}s; stopping. Last log:\n{tail}', flush=True)
                self.fatal = True
                return
            self.spawn(w.python, w.long_lived)
            self.counts['respawns'] += 1

    def read_stats(self, w: WorkerProc) -> None:
        path = self.dir / 'stats' / f'{w.id}.json'
        try:
            stats = json.loads(path.read_text())
        except (OSError, ValueError):
            return
        new_problems = stats.get('problems', [])[len(w.stats.get('problems', [])) :]
        for text in new_problems:
            self.event('worker-problem', problem=True, id=w.id, python=stats.get('python'), detail=text)
        w.stats = stats
        self.last_stats[w.id] = stats
        self.reported_acked[w.id] = max(self.reported_acked.get(w.id, 0), stats.get('acked', 0))
        if stats.get('final'):
            self.final_acked[w.id] = stats['acked']
        for slot in stats.get('once_won', []):
            self.once_winners.setdefault(slot, set()).add(w.id)

    # -- chaos --------------------------------------------------------------------------------

    def chaos(self) -> None:
        action = self.rng.choice(['kill', 'kill', 'fork', 'fork', 'clear', 'expire', 'cull'])
        if action == 'kill':
            victims = [w for w in self.workers.values() if not w.long_lived]
            if victims:
                w = self.rng.choice(victims)
                self.read_stats(w)
                w.proc.kill()
                self.counts['kills'] += 1
                self.event('kill', id=w.id, acked_reported=self.reported_acked.get(w.id, 0))
                w.proc.wait()
                self.reap()
                self.spawn(w.python, long_lived=False)
                self.counts['respawns'] += 1
        elif action == 'fork':
            w = self.rng.choice(list(self.workers.values()))
            w.proc.send_signal(signal.SIGUSR1)
            self.event('fork-request', id=w.id)
        elif not self.maint_running:
            threading.Thread(target=self.maint, args=(action,), daemon=True).start()

    def maint(self, op: str) -> None:
        self.maint_running = True
        python = self.rng.choice(self.pythons)
        start = time.time()
        proc = subprocess.Popen([python, '-m', 'soak.worker', 'maint', str(self.dir), op], cwd=REPO, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        try:
            out, _ = proc.communicate(timeout=MAINT_TIMEOUT)
            text = out.decode(errors='replace').strip()
            ok = proc.returncode == 0
            parts = text.split()
            removed = parts[1] if ok and len(parts) >= 3 else ''
            row = {'op': op, 'seconds': round(time.time() - start, 3), 'removed': removed, 'ok': ok, 'detail': '' if ok else text[-500:]}
            if not ok:
                self.event('maint-failed', problem=True, **row)
        except subprocess.TimeoutExpired:
            self.capture_stall(f'maint-{op}', proc.pid, python)
            proc.kill()
            proc.communicate()
            row = {'op': op, 'seconds': round(time.time() - start, 3), 'removed': '', 'ok': False, 'detail': f'ran over {MAINT_TIMEOUT:.0f}s'}
            self.event('maint-stall', problem=True, **row)
        self.counts['maint'] += 1
        with self.lock:
            self.maint_csv[1].writerow({'time': round(start, 3), 'elapsed_h': round((start - self.started) / 3600, 4), **row})
        self.maint_running = False

    # -- stalls -------------------------------------------------------------------------------

    def capture_stall(self, label: str, pid: int, python: str) -> None:
        """Native stacks (macOS `sample`, or `eu-stack`/`gdb` if present on Linux) and, for workers,
        every Python thread's traceback (faulthandler on SIGUSR2, into logs/<id>.log)."""
        self.counts['stalls'] += 1
        stamp = time.strftime('%H%M%S')
        out = self.dir / 'stalls' / f'{label}-{stamp}.txt'
        if label.startswith('worker-'):
            try:
                os.kill(pid, signal.SIGUSR2)
            except OSError:
                pass
        if sys.platform == 'darwin':
            cmd = ['sample', str(pid), '5', '-file', str(out)]
        else:
            cmd = ['gdb', '-p', str(pid), '-batch', '-ex', 'thread apply all bt']
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            if not out.exists():
                out.write_text(result.stdout + result.stderr)
        except (OSError, subprocess.TimeoutExpired) as ex:
            out.write_text(f'could not sample {pid}: {ex!r}\n')
        self.event('stall-captured', problem=False, label=label, pid=pid, python=python, file=str(out.relative_to(self.dir)))

    def check_progress(self) -> None:
        now = time.time()
        total = 0
        for w in self.workers.values():
            self.read_stats(w)
            ops = w.stats.get('ops', 0) if w.stats.get('pid') == w.proc.pid else 0
            total += ops
            if ops != w.last_ops:
                w.last_ops, w.last_progress = ops, now
                if w.stalled:
                    self.event('worker-recovered', id=w.id, after_s=round(now - w.last_progress, 1))
                w.stalled = False
            elif not w.stalled and now - w.last_progress > self.args.stall_after and now - w.started > self.args.stall_after:
                w.stalled = True
                self.event('worker-stall', problem=True, id=w.id, python=w.stats.get('python'), no_progress_s=round(now - w.last_progress))
                self.capture_stall(f'worker-{w.id}', w.proc.pid, w.python)
        if total != self.last_total_ops:
            self.last_total_ops, self.last_total_progress = total, now

    # -- database checks (stdlib sqlite3, read-only queries) ------------------------------------

    def connect(self, name: str) -> sqlite3.Connection:
        conn = sqlite3.connect(self.dir / name / DB_NAME, timeout=30, isolation_level=None)
        conn.execute('PRAGMA busy_timeout = 30000')
        return conn

    def check_db(self, name: str, deep: bool, orphans: bool) -> dict[str, Any]:
        info: dict[str, Any] = {}
        path = self.dir / name / DB_NAME
        if not path.exists():
            return info
        info['db_bytes'] = path.stat().st_size
        wal = path.with_name(DB_NAME + '-wal')
        info['wal_bytes'] = wal.stat().st_size if wal.exists() else 0
        conn = self.connect(name)
        try:
            conn.execute('BEGIN')  # One snapshot for the totals check.
            count, size = conn.execute('SELECT count, size FROM totals').fetchone()
            rows, row_size = conn.execute('SELECT COUNT(*), COALESCE(SUM(size), 0) FROM cache').fetchone()
            info.update(rows=rows, totals_ok=(count, size) == (rows, row_size))
            if (count, size) != (rows, row_size):
                self.event('totals-mismatch', problem=True, db=name, totals=[count, size], actual=[rows, row_size])
            files = {r[0] for r in conn.execute('SELECT filename FROM cache WHERE filename IS NOT NULL')}
            info['value_files'] = len(files)
            conn.execute('COMMIT')
            if name == 'ledger':
                self.check_ledger(conn)
            if orphans:
                self.scan_files(name, conn, files)
            if deep:
                result = conn.execute('PRAGMA integrity_check').fetchall()
                ok = result == [('ok',)]
                info['integrity_ok'] = ok
                self.event('integrity-check', problem=not ok, db=name, result=[r[0] for r in result[:20]])
        except sqlite3.Error as ex:
            self.event('db-check-error', problem=True, db=name, error=repr(ex))
        finally:
            conn.close()
        return info

    def scan_files(self, name: str, conn: sqlite3.Connection, files: set[str]) -> None:
        root = self.dir / name
        now = time.time()
        on_disk: set[str] = set()
        for path in root.glob('??/??/*'):
            rel = path.relative_to(root).as_posix()
            try:
                if now - path.stat().st_mtime >= ORPHAN_MIN_AGE:
                    on_disk.add(rel)
            except FileNotFoundError:
                pass
        orphans = {f for f in on_disk - files}
        self.last_orphans[name] = len(orphans)
        # A row whose file is gone is a real bug, unless the row was deleted after our snapshot.
        missing = [f for f in files if not (root / f).exists()]
        still = [f for f in missing if conn.execute('SELECT 1 FROM cache WHERE filename = ?', (f,)).fetchone() and not (root / f).exists()]
        if still:
            self.event('value-file-missing', problem=True, db=name, files=still[:20], count=len(still))
        self.event('file-scan', db=name, orphans=len(orphans), orphan_tmp=sum(f.endswith('.tmp') for f in orphans), missing=len(still))

    def check_ledger(self, conn: sqlite3.Connection) -> None:
        counters = {int(k.split(':')[1]): v for k, v in conn.execute("SELECT key, value FROM cache WHERE key LIKE 'ctr:%'")}
        for wid, acked in self.reported_acked.items():
            value = counters.get(wid, 0)
            if not isinstance(value, int) or value < acked:
                self.event('lost-increment', problem=True, id=wid, stored=value, acknowledged=acked)
        for slot, winners in self.once_winners.items():
            if len(winners) > 1:
                self.event('add-two-winners', problem=True, slot=slot, winners=sorted(winners))
        self.once_winners = {s: w for s, w in self.once_winners.items() if len(w) <= 1}

    # -- the interval ---------------------------------------------------------------------------

    def interval(self) -> None:
        self.interval_no += 1
        self.check_progress()
        deep = self.interval_no % 10 == 0
        orphans = self.interval_no % 5 == 0
        db = {name: self.check_db(name, deep, orphans) for name in ('chaos', 'ledger')}
        self.last_db = db
        now = time.time()
        live = list(self.workers.values())
        ops = sum(w.stats.get('ops', 0) for w in live)
        lat = {op: [w.stats['latency'][op] for w in live if w.stats.get('latency', {}).get(op)] for op in ('get', 'set')}
        if self.last_total_ops >= 0 and now - self.last_total_progress > self.args.stall_after:
            self.event('system-stall', problem=True, no_progress_s=round(now - self.last_total_progress))
        for w in live:
            s = w.stats
            if not s:
                continue
            row = {
                'time': round(now, 3),
                'elapsed_h': round(self.elapsed_h(), 4),
                'id': w.id,
                'python': s.get('python'),
                'long_lived': int(w.long_lived),
                'rss_mib': round(s.get('rss', 0) / 2**20, 2),
                'fds': s.get('fds'),
                'ops': s.get('ops'),
                'timeouts': s.get('timeouts'),
                'problems': s.get('problem_count'),
                'forks_ok': s.get('forks_ok'),
                'forks_failed': s.get('forks_failed'),
                'get_p50_us': (s.get('latency', {}).get('get') or {}).get('p50_us'),
                'get_p99_us': (s.get('latency', {}).get('get') or {}).get('p99_us'),
                'get_max_us': (s.get('latency', {}).get('get') or {}).get('max_us'),
                'set_p50_us': (s.get('latency', {}).get('set') or {}).get('p50_us'),
                'set_p99_us': (s.get('latency', {}).get('set') or {}).get('p99_us'),
                'set_max_us': (s.get('latency', {}).get('set') or {}).get('max_us'),
                'stalled': int(w.stalled),
            }
            self.workers_csv[1].writerow(row)
        ops_rate = 0.0
        if hasattr(self, '_prev_ops'):
            dt = now - self._prev_time
            # Respawned workers restart at zero, so count only growth per worker.
            ops_rate = sum(max(0, w.stats.get('ops', 0) - self._prev_ops.get(w.id, 0)) for w in live) / dt if dt else 0.0
        self._prev_ops = {w.id: w.stats.get('ops', 0) for w in live}
        self._prev_time = now
        median = lambda xs: sorted(xs)[len(xs) // 2] if xs else None  # noqa: E731
        trow = {
            'time': round(now, 3),
            'elapsed_h': round(self.elapsed_h(), 4),
            'workers': len(live),
            'ops_per_s': round(ops_rate),
            'get_p50_us': median([x['p50_us'] for x in lat['get']]),
            'get_p99_us': max([x['p99_us'] for x in lat['get']], default=None),
            'set_p50_us': median([x['p50_us'] for x in lat['set']]),
            'set_p99_us': max([x['p99_us'] for x in lat['set']], default=None),
            'get_max_us': max([x['max_us'] for x in lat['get']], default=None),
            'set_max_us': max([x['max_us'] for x in lat['set']], default=None),
            'rss_total_mib': round(sum(w.stats.get('rss', 0) for w in live) / 2**20, 1),
            'fds_total': sum(w.stats.get('fds', 0) or 0 for w in live),
            'timeouts_total': self.total('timeouts'),
            'problems': self.problems,
            'kills': self.counts['kills'],
            'forks_ok': self.total('forks_ok'),
            'forks_failed': self.total('forks_failed'),
            'maint_calls': self.counts['maint'],
            'stalls': self.counts['stalls'],
            **{f'{n}_{k}': db.get(n, {}).get(k) for n in ('chaos', 'ledger') for k in ('rows', 'db_bytes', 'wal_bytes', 'value_files')},
            'chaos_orphans': self.last_orphans.get('chaos'),
            # Forced WAL checkpoints (D-024), over every worker of the run (killed ones included).
            'wal_ckpt_attempts': self.wal_total('attempts'),
            'wal_ckpt_busy': self.wal_total('busy'),
            'wal_ckpt_max_pause_us': max(((s.get('wal') or {}).get('max_pause_us', 0) for s in self.last_stats.values()), default=0),
            'wal_ckpt_slow': self.wal_total('slow'),
        }
        self.timeline[1].writerow(trow)
        self.write_status(trow, ops)
        try:
            from soak.report import build

            build(self.dir, final=False)
        except Exception as ex:  # The report must never stop the soak.
            self.event('report-error', error=repr(ex))

    def wal_total(self, key: str) -> int:
        """A forced-checkpoint counter summed over every worker of the run."""
        return sum((s.get('wal') or {}).get(key, 0) or 0 for s in self.last_stats.values())

    def total(self, key: str) -> int:
        """A counter summed over every worker of the run, including ones that exited."""
        return sum(s.get(key, 0) or 0 for s in self.last_stats.values())

    def write_status(self, t: dict[str, Any], ops: int) -> None:
        remaining = max(0.0, self.ends - time.time()) / 3600
        lines = [
            f'diskcache-oxide soak  {time.strftime("%Y-%m-%d %H:%M:%S")}  elapsed {self.elapsed_h():.2f} h, {remaining:.2f} h to go',
            f'verdict so far: {"OK" if self.problems == 0 else f"{self.problems} PROBLEM(S), see events.jsonl"}',
            f'workers {t["workers"]}  ops/s {t["ops_per_s"]:,}  get p50/p99 {t["get_p50_us"]}/{t["get_p99_us"]} us  set p50/p99 {t["set_p50_us"]}/{t["set_p99_us"]} us',
            f'rss total {t["rss_total_mib"]} MiB  fds total {t["fds_total"]}  timeouts {t["timeouts_total"]}  stalls {t["stalls"]}',
            f'kills {t["kills"]}  respawns {self.counts["respawns"]}  forks ok/failed {t["forks_ok"]}/{t["forks_failed"]}  maintenance calls {t["maint_calls"]}',
            f'chaos: {t["chaos_rows"]} rows, db {mib(t["chaos_db_bytes"])} MiB, wal {mib(t["chaos_wal_bytes"])} MiB, {t["chaos_value_files"]} value files, orphans {t["chaos_orphans"]}',
            f'ledger: {t["ledger_rows"]} rows, db {mib(t["ledger_db_bytes"])} MiB, wal {mib(t["ledger_wal_bytes"])} MiB',
            '',
            f'{"id":>4} {"python":>9} {"long":>4} {"rss MiB":>8} {"fds":>5} {"ops":>12} {"tmo":>5} {"probs":>5}',
        ]
        for w in sorted(self.workers.values(), key=lambda w: w.id):
            s = w.stats
            lines.append(
                f'{w.id:>4} {s.get("python", "?"):>9} {"yes" if w.long_lived else "":>4} {s.get("rss", 0) / 2**20:8.1f} '
                f'{s.get("fds", 0):>5} {s.get("ops", 0):>12,} {s.get("timeouts", 0):>5} {s.get("problem_count", 0):>5}'
                + ('  STALLED' if w.stalled else '')
            )
        text = '\n'.join(lines) + '\n'
        tmp = self.dir / 'status.tmp'
        tmp.write_text(text)
        os.replace(tmp, self.dir / 'status.txt')
        print(lines[0].split('  ', 1)[1] + ' | ' + lines[1] + ' | ' + lines[2], flush=True)

    # -- run ----------------------------------------------------------------------------------

    def run(self) -> int:
        self.event('start', args=vars(self.args), pythons=self.pythons, dir=str(self.dir))
        print(f'soak: {self.dir}\n  watch: tail -F {self.dir}/status.txt   report: open {self.dir}/report.html', flush=True)
        self.start_workers()
        stop = threading.Event()
        signal.signal(signal.SIGINT, lambda *_: stop.set())
        signal.signal(signal.SIGTERM, lambda *_: stop.set())
        next_interval = time.time() + self.args.interval
        next_chaos = time.time() + self.rng.uniform(10, self.args.chaos_every)
        while not stop.is_set() and not self.fatal and time.time() < self.ends:
            stop.wait(1.0)
            self.reap()
            if time.time() >= next_chaos:
                self.chaos()
                next_chaos = time.time() + self.rng.uniform(self.args.chaos_every / 3, self.args.chaos_every)
            if time.time() >= next_interval:
                self.interval()
                next_interval += self.args.interval
        return self.finish()

    def finish(self) -> int:
        print('soak: stopping workers and running the final checks', flush=True)
        while self.maint_running:
            time.sleep(1)
        for w in self.workers.values():
            w.proc.terminate()
        deadline = time.time() + 120
        for w in list(self.workers.values()):
            try:
                w.proc.wait(max(1.0, deadline - time.time()))
            except subprocess.TimeoutExpired:
                self.capture_stall(f'worker-{w.id}-shutdown', w.proc.pid, w.python)
                self.event('shutdown-hang', problem=True, id=w.id)
                w.proc.kill()
                w.proc.wait()
            self.read_stats(w)
            if w.proc.returncode == -signal.SIGTERM and not w.stats:
                # Still starting up (no handler, no stats yet): nothing of its own to verify.
                self.event('worker-stopped-during-startup', id=w.id)
            elif w.proc.returncode != 0:
                self.event('worker-bad-exit', problem=True, id=w.id, exit=w.proc.returncode)
        self.workers.clear()
        if not (self.dir / 'ledger' / DB_NAME).exists():
            self.event('no-ledger', problem=True, detail='no worker ever opened the ledger cache')
            from soak.report import build

            build(self.dir, final=True)
            return 1
        # Exact check for cleanly stopped workers: every incr that returned is stored, once each.
        conn = self.connect('ledger')
        counters = {int(k.split(':')[1]): v for k, v in conn.execute("SELECT key, value FROM cache WHERE key LIKE 'ctr:%'")}
        winners = dict(conn.execute("SELECT key, value FROM cache WHERE key LIKE 'once:%'"))
        conn.close()
        for wid, acked in self.final_acked.items():
            if counters.get(wid, 0) != acked:
                self.event('ledger-final-mismatch', problem=True, id=wid, stored=counters.get(wid), acknowledged=acked)
        for slot, ids in self.once_winners.items():
            if len(ids) == 1 and slot in winners and winners[slot] not in ids:
                self.event('add-winner-mismatch', problem=True, slot=slot, stored=winners[slot], reported=sorted(ids))
        self.event('final-ledger', workers_checked=len(self.final_acked), slots_checked=len(self.once_winners))
        for name in ('chaos', 'ledger'):
            self.check_db(name, deep=True, orphans=True)
        totals = {k: self.total(k) for k in ('ops', 'timeouts', 'forks_ok', 'forks_failed', 'reopens', 'once_tried')}
        self.event('finish', problems=self.problems, counts=self.counts, totals=totals)
        from soak.report import build

        build(self.dir, final=True)
        verdict = 'PASS' if self.problems == 0 else f'FAIL ({self.problems} problems)'
        print(f'soak: {verdict}. Summary: {self.dir}/summary.md  report: {self.dir}/report.html', flush=True)
        return 0 if self.problems == 0 else 1


def mib(n: Any) -> str:
    return '?' if n is None else f'{n / 2**20:.1f}'


TIMELINE_FIELDS = [
    'time', 'elapsed_h', 'workers', 'ops_per_s', 'get_p50_us', 'get_p99_us', 'set_p50_us', 'set_p99_us',
    'rss_total_mib', 'fds_total', 'timeouts_total', 'problems', 'kills', 'forks_ok', 'forks_failed', 'maint_calls',
    'stalls', 'chaos_rows', 'chaos_db_bytes', 'chaos_wal_bytes', 'chaos_value_files', 'ledger_rows', 'ledger_db_bytes',
    'ledger_wal_bytes', 'ledger_value_files', 'chaos_orphans', 'wal_ckpt_attempts', 'wal_ckpt_busy',
    'wal_ckpt_max_pause_us', 'wal_ckpt_slow', 'get_max_us', 'set_max_us',
]  # fmt: skip
WORKER_FIELDS = [
    'time', 'elapsed_h', 'id', 'python', 'long_lived', 'rss_mib', 'fds', 'ops', 'timeouts', 'problems', 'forks_ok',
    'forks_failed', 'get_p50_us', 'get_p99_us', 'set_p50_us', 'set_p99_us', 'stalled', 'get_max_us', 'set_max_us',
]  # fmt: skip


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--hours', type=float, default=0.0)
    parser.add_argument('--minutes', type=float, default=0.0)
    parser.add_argument('--pythons', required=True, help='Comma-separated interpreters; workers alternate between them.')
    parser.add_argument('--procs', type=int, default=max(4, (os.cpu_count() or 8) // 2), help='Worker processes (default: half the CPUs).')
    parser.add_argument('--threads', type=int, default=4, help='Threads per worker.')
    parser.add_argument('--interval', type=float, default=60.0, help='Seconds between checks and data rows.')
    parser.add_argument('--mix', type=int, default=0, help='One write per this many operations (default: 99:1 and 1,000:1, alternating by thread).')
    parser.add_argument('--rate', type=float, default=0.0, help='Total cache operations per second across all workers (default: flat out).')
    parser.add_argument('--chaos-every', type=float, default=60.0, help='Upper bound on seconds between chaos actions.')
    parser.add_argument('--stall-after', type=float, default=STALL_AFTER, help='Seconds without progress before a worker counts as stalled.')
    parser.add_argument('--seed', type=int, default=int(time.time()) % 100_000)
    parser.add_argument('--out', default=str(REPO / 'soak-results'))
    args = parser.parse_args()
    if not args.hours and not args.minutes:
        parser.error('give --hours and/or --minutes')
    try:
        from tests.proctitle import set_title

        set_title('soak-orchestrator')
    except Exception:
        pass
    return Soak(args).run()


if __name__ == '__main__':
    sys.exit(main())
