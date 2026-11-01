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
    durable/, behind/, stuck/, auto-<n>/   the caches of the plan 09 lanes (soak/README.md)
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
# A gracefully restarted worker (SIGTERM) must have finished, verified its keys and exited by then.
RESTART_TIMEOUT = 90.0
# The end-of-run checks walk the whole ledger, and at the full shape that's about 4.6M rows.
VERIFY_TIMEOUT = 3600.0
# A lane proving something moves its counters every few minutes at most, so one that hasn't for this
# long has stopped proving anything, even if its thread still beats.
QUIET_AFTER = 1800.0
QUIET_GAPS = 20
# Files younger than this are skipped by the orphan scan (writes in flight).
ORPHAN_MIN_AGE = 120.0
# More unexpected worker deaths than this within CRASH_WINDOW seconds stops the run: the build or
# environment is broken, and respawning would only bury the first error.
CRASH_LIMIT = 5
CRASH_WINDOW = 60.0
# A token the exactly-once lane recorded and then lost is excused only by a kill this soon after it was
# produced: a consumer pops within a tick or so, and dies between its pop and its add. Without this,
# the count of kills alone (hundreds in 24 hours) would hide a slow real loss.
PQ_KILL_WINDOW = 30.0
# Forced checkpoints, in the shape where the rate means something: one writer per store, beside readers
# in another process. The rate is successes / (successes + busy), since a round skipped for a pending
# fork or a full pool says nothing about the WAL. Measured in this group's own shape on macOS, per store
# (plan 09, step 3): 8 tries, about 6,000 rounds across runs with chaos kills: 100%. 8 tries with 24 CPU
# hogs on 18 CPUs (load 48): 96.9% and 97.6%. 1 try (a scratch build): 94.2%, and 41% and 53% with the
# hogs. Step 2b's measurements agree: the build before the retries got under 25%, 5 tries about 94%, and
# 8 tries left 0.1-0.4% of rounds busy on a saturated load. So 95% says forced checkpoints work under
# this load, and passes a badly saturated machine. It doesn't catch lost retries: under this kit's load, a
# 1-try build can pass it. Kills can't pull it down: no busy round in those runs, as a dead peer's locks
# go with it and a dead writer just stops counting, with every writer summed.
AUTO_MIN_SUCCESS_RATE = 0.95
# Fewer judged rounds than this per store fails the run, per hour once it passes an hour, since a rate
# from a few rounds is noise. Low on purpose: a busy round costs a second of backoff, so a failing store
# forces few rounds (76 and 93 in 4.5 minutes, at 41% and 53%). The full shape does about 6,000 an hour.
AUTO_MIN_ROUNDS = 50


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
    role: str = 'run'
    store: int = -1  # An auto store, or a `lanes` worker's private slot.
    # A lane thread's name -> its last heartbeat count, when it moved, and if it has been reported.
    lanes: dict[str, list[Any]] = field(default_factory=dict)
    fork_timeouts_seen: int = 0


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
        # The caches under test can live elsewhere (DCO_SOAK_CACHE_ROOT), linked in as chaos/ and
        # ledger/: scripts/linux-soak.sh keeps them on the container's own disk, because SQLite on
        # the host folder shared into the VM pays the file-sharing layer on every lock and fsync.
        cache_root = os.environ.get('DCO_SOAK_CACHE_ROOT')
        if cache_root:
            for name in ('chaos', 'ledger', *self.extra_stores(), 'stuck'):
                target = Path(cache_root) / self.dir.name / name
                target.mkdir(parents=True)
                (self.dir / name).symlink_to(target)
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
        self.counts = {'kills': 0, 'restarts': 0, 'respawns': 0, 'maint': 0, 'stalls': 0, 'side_kills': 0, 'side_restarts': 0}
        self.kill_times: list[float] = []
        self.pq_result: dict[str, Any] = {}
        # Each worker's latest stats, kept after it exits, so run totals include killed workers.
        self.last_stats: dict[int, dict[str, Any]] = {}
        self.maint_running = False
        self.crashes: list[float] = []
        self.fatal = False
        self.events = (self.dir / 'events.jsonl').open('a', buffering=1)
        per_store = [f'auto_{n}_{key}' for n in range(args.auto_stores) for key in ('successes', 'busy')]
        self.timeline = self._csv('timeline.csv', TIMELINE_FIELDS + per_store)
        self.quiet: dict[str, list[Any]] = {}
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

    def extra_stores(self) -> list[str]:
        return ['durable', 'behind', *(f'auto-{n}' for n in range(self.args.auto_stores))]

    # -- workers ------------------------------------------------------------------------------

    def spawn(self, python: str, long_lived: bool, role: str = 'run', store: int = -1) -> None:
        wid = self.next_id
        self.next_id += 1
        log = (self.dir / 'logs' / f'{wid}.log').open('ab')
        thread_rate = self.args.rate / (self.args.procs * self.args.threads) if self.args.rate else 0.0
        cmd = [
            python, '-m', 'soak.worker', 'run', str(self.dir), str(wid), str(self.args.threads),
            str(self.args.seed * 10_000 + wid), str(self.args.mix), str(thread_rate),
        ]  # fmt: skip
        seed = str(self.args.seed * 10_000 + wid)
        if role == 'lanes':
            cmd = [python, '-m', 'soak.worker', 'lanes', str(self.dir), str(wid), seed, str(store)]
        elif role != 'run':
            cmd = [python, '-m', 'soak.worker', role, str(self.dir), str(wid), seed, str(store), str(self.args.auto_stores)]
        proc = subprocess.Popen(cmd, cwd=REPO, stdout=log, stderr=log)
        self.workers[wid] = WorkerProc(wid, python, long_lived, proc, time.time(), log, role=role, store=store)
        self.event('spawn', id=wid, pid=proc.pid, python=python, long_lived=long_lived, role=role, store=store)

    def start_workers(self) -> None:
        for n in range(self.args.procs):
            # Alternate interpreters on the same caches. The first worker per interpreter is never
            # killed, so its memory and fd trends cover the whole run.
            self.spawn(self.pythons[n % len(self.pythons)], long_lived=n < len(self.pythons))
        # The first worker of the lanes per interpreter is never killed either, so its trends and its
        # counters cover the whole run; the rest are.
        for n in range(self.args.lanes):
            self.spawn(self.pythons[n % len(self.pythons)], long_lived=n < len(self.pythons), role='lanes', store=n)
        # Readers on the last interpreter (free-threaded, in soak/run.sh), since only readers truly in
        # parallel hold read marks enough to force a checkpoint; writers alternate.
        for store in range(self.args.auto_stores):
            for n in range(self.args.auto_writers):
                self.spawn(self.pythons[(store + n) % len(self.pythons)], long_lived=False, role='auto-writer', store=store)
            self.spawn(self.pythons[-1], long_lived=False, role='auto-reader', store=store)

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
            self.spawn(w.python, w.long_lived, w.role, w.store)
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
        # With timeouts of 30 and 60 s, even a fork behind busy threads is far inside the bound, so
        # outside the lanes that use small timeouts on purpose, any count means a fork was stuck.
        timeouts = (stats.get('gate') or {}).get('fork_timeouts', 0)
        if w.role != 'lanes' and timeouts > w.fork_timeouts_seen:
            self.event('fork-timeouts-in-a-normal-lane', problem=True, id=w.id, role=w.role, count=timeouts)
        w.fork_timeouts_seen = max(w.fork_timeouts_seen, timeouts)

    # -- chaos --------------------------------------------------------------------------------

    def chaos(self) -> None:
        # 'checkpoint' is the cookbook pattern for long-running apps: call Cache.checkpoint() now and then.
        # 'restart' is what gunicorn and friends do: SIGTERM, a clean shutdown, a fresh worker.
        action = self.rng.choice(['kill', 'kill', 'restart', 'fork', 'fork', 'clear', 'expire', 'cull', 'checkpoint'])
        if action == 'restart':
            # Old enough to have installed its SIGTERM handler (a just-spawned one exits -15).
            victims = [w for w in self.workers.values() if w.role == 'run' and not w.long_lived and time.time() - w.started > 30]
            if victims:
                self.restart(self.rng.choice(victims))
            return
        if action == 'kill':
            victims = [w for w in self.workers.values() if w.role == 'run' and not w.long_lived]
            if victims:
                self.kill_worker(self.rng.choice(victims))
        elif action == 'fork':
            w = self.rng.choice([w for w in self.workers.values() if w.role == 'run'])
            w.proc.send_signal(signal.SIGUSR1)
            self.event('fork-request', id=w.id)
        elif not self.maint_running:
            threading.Thread(target=self.maint, args=(action,), daemon=True).start()

    def side_chaos(self) -> None:
        """Kills and restarts for the lanes and the auto group, on a timer of their own so that the
        `run` workers see as much chaos as they did before those groups existed."""
        victims = [w for w in self.workers.values() if w.role != 'run' and not w.long_lived and time.time() - w.started > 30]
        if not victims:
            return
        w = self.rng.choice(victims)
        if self.rng.random() < 2 / 3:
            self.kill_worker(w)
        else:
            self.restart(w)

    def kill_worker(self, w: WorkerProc) -> None:
        self.read_stats(w)
        w.proc.kill()
        if w.role == 'run':
            self.counts['kills'] += 1
            self.kill_times.append(time.time())
        else:
            self.counts['side_kills'] += 1
        self.event('kill', id=w.id, role=w.role, acked_reported=self.reported_acked.get(w.id, 0))
        w.proc.wait()
        self.reap()
        self.spawn(w.python, False, w.role, w.store)
        self.counts['respawns'] += 1

    def restart(self, w: WorkerProc) -> None:
        """Gracefully restarts `w`: SIGTERM, then it must finish its calls, verify every key it owns,
        write exact final counts (checked against the ledger at the end of the run), close its caches
        (with the checkpoint on close) and exit 0, within RESTART_TIMEOUT. Then a fresh worker."""
        start = time.time()
        w.proc.terminate()
        try:
            code = w.proc.wait(RESTART_TIMEOUT)
        except subprocess.TimeoutExpired:
            self.capture_stall(f'worker-{w.id}-restart', w.proc.pid, w.python)
            w.proc.kill()
            code = w.proc.wait()
            self.event('restart-hang', problem=True, id=w.id, seconds=round(time.time() - start, 1))
        del self.workers[w.id]
        w.log.close()
        self.read_stats(w)
        self.counts['restarts' if w.role == 'run' else 'side_restarts'] += 1
        if code != 0:
            tail = (self.dir / 'logs' / f'{w.id}.log').read_text(errors='replace')[-3000:]
            self.event('restart-bad-exit', problem=True, id=w.id, exit=code, log_tail=tail)
        elif not w.stats.get('final'):
            self.event('restart-no-final-stats', problem=True, id=w.id)
        else:
            self.event('restart', id=w.id, role=w.role, seconds=round(time.time() - start, 2), acked=w.stats.get('acked'))
        self.spawn(w.python, False, w.role, w.store)
        self.counts['respawns'] += 1

    def verify_held(self) -> None:
        """The end-of-run check of every worker's held transactions (soak/worker.py)."""
        try:
            proc = subprocess.run([self.pythons[0], '-m', 'soak.worker', 'maint', str(self.dir), 'verify-held'], cwd=REPO, capture_output=True, text=True, timeout=VERIFY_TIMEOUT)
        except subprocess.TimeoutExpired:
            self.event('final-held-error', problem=True, detail=f'ran over {VERIFY_TIMEOUT:.0f}s')
            return
        lines = proc.stdout.splitlines()
        problems = [line.removeprefix('problem: ') for line in lines if line.startswith('problem: ')]
        for text in problems[:50]:
            self.event('held-atomicity', problem=True, detail=text)
        if proc.returncode != 0:
            self.event('final-held-error', problem=True, exit=proc.returncode, detail=(proc.stdout + proc.stderr)[-2000:])
        self.event('final-held', problems=len(problems), summary=lines[-1] if lines else '')

    def verify_pq(self) -> None:
        """The end-of-run check of the exactly-once lane (soak/worker.py)."""
        try:
            proc = subprocess.run([self.pythons[0], '-m', 'soak.worker', 'maint', str(self.dir), 'verify-pq'], cwd=REPO, capture_output=True, text=True, timeout=VERIFY_TIMEOUT)
            result = json.loads(proc.stdout)
        except (subprocess.TimeoutExpired, ValueError) as ex:
            self.event('final-pq-error', problem=True, detail=repr(ex))
            return
        lost = result.pop('lost')
        both = result.pop('delivered_and_queued')
        for token in both[:50]:
            self.event('pq-delivered-and-queued', problem=True, token=token)
        ticks_per_s, threads = result.pop('ticks_per_s'), result.pop('threads')
        unexcused = assign_lost_to_kills(lost, self.kill_times, ticks_per_s, threads, PQ_KILL_WINDOW)
        for token in unexcused:
            self.event('pq-lost', problem=True, token=token, detail=f'recorded, then neither delivered nor queued, with no kill left to excuse it (at most {threads} per kill, within {PQ_KILL_WINDOW:.0f}s after it)')
        excused = len(lost) - len(unexcused)
        allowance = len(self.kill_times) * threads
        self.pq_result = {**result, 'lost': len(lost), 'lost_excused_by_kills': excused, 'kill_allowance': allowance, 'delivered_and_queued': len(both), 'duplicates': self.ctotal('pq_duplicates')}
        self.event('final-pq', **self.pq_result)

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
        self.check_lane_threads(now)

    def check_lane_threads(self, now: float) -> None:
        """A worker's op count moves if any thread moves, so each lane thread's heartbeat is checked too:
        a call that hangs in one lane, or a fork that never returns, stops only its own thread."""
        for w in self.workers.values():
            if w.stats.get('pid') != w.proc.pid:
                continue
            for lane, beats in (w.stats.get('beats') or {}).items():
                state = w.lanes.setdefault(lane, [beats, now, False])
                if beats != state[0]:
                    state[:] = [beats, now, False]
                elif not state[2] and now - state[1] > self.args.stall_after:
                    state[2] = True
                    self.event('lane-stall', problem=True, id=w.id, role=w.role, lane=lane, python=w.stats.get('python'), no_progress_s=round(now - state[1]))
                    self.capture_stall(f'worker-{w.id}-{lane}', w.proc.pid, w.python)

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
        db = {name: self.check_db(name, deep, orphans) for name in ('chaos', 'ledger', *self.extra_stores())}
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
                'role': w.role,
            }
            self.workers_csv[1].writerow(row)
        ops_rate = 0.0
        if hasattr(self, '_prev_ops'):
            dt = now - self._prev_time
            # Respawned workers restart at zero, so count only growth per worker.
            # The `run` workers only, so this stays comparable with --rate and with earlier soaks.
            ops_rate = sum(max(0, w.stats.get('ops', 0) - self._prev_ops.get(w.id, 0)) for w in live if w.role == 'run') / dt if dt else 0.0
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
            'restarts': self.counts['restarts'],
            'forks_ok': self.total('forks_ok'),
            'forks_failed': self.total('forks_failed'),
            'maint_calls': self.counts['maint'],
            'stalls': self.counts['stalls'],
            **{f'{n}_{k}': db.get(n, {}).get(k) for n in ('chaos', 'ledger') for k in ('rows', 'db_bytes', 'wal_bytes', 'value_files')},
            'chaos_orphans': self.last_orphans.get('chaos'),
            'ledger_orphans': self.last_orphans.get('ledger'),
            # Held write transactions on the ledger, over every worker of the run.
            'held_commits': self.total('held_commits'),
            'held_rollbacks': self.total('held_rollbacks'),
            # Forced WAL checkpoints (D-024), over every worker of the run (killed ones included).
            'wal_ckpt_attempts': self.wal_total('attempts'),
            'wal_ckpt_busy': self.wal_total('busy'),
            'wal_ckpt_max_pause_us': max(((s.get('wal') or {}).get('max_pause_us', 0) for s in self.last_stats.values()), default=0),
            'wal_ckpt_slow': self.wal_total('slow'),
            **self.lane_totals(),
            'load_1m': round(os.getloadavg()[0], 2),
            **{f'auto_{n}_{key}': st[key] for n, st in self.auto_summary().items() for key in ('successes', 'busy')},
        }
        self.timeline[1].writerow(trow)
        self.check_quiet(now)
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

    def ctotal(self, key: str, python: str | None = None) -> int:
        """A plan 09 lane's counter summed over every worker of the run, or one interpreter's."""
        return sum((s.get('c') or {}).get(key, 0) for s in self.last_stats.values() if python is None or s.get('python') == python)

    def durable_buckets(self) -> dict[str, int]:
        """What durable handles swallowed, by bucket, over every worker of the run."""
        out = {'busy': 0, 'broken': 0, 'lost_deletes': 0}
        for s in self.last_stats.values():
            for bucket, ops in (s.get('durable') or {}).items():
                out[bucket] += sum(ops.values())
        return out

    def auto_summary(self) -> dict[int, dict[str, Any]]:
        """Each auto store's forced checkpoints, over all of its writers, killed ones included."""
        stores: dict[int, dict[str, Any]] = {
            n: dict.fromkeys(('rounds', 'successes', 'busy', 'errors', 'no_slot', 'fork_pending', 'checks', 'slow', 'max_pause_us', 'peak_wal'), 0) for n in range(self.args.auto_stores)
        }
        for s in self.last_stats.values():
            auto = s.get('auto')
            if not auto:
                continue
            st = stores[s['store']]
            st['rounds'] += auto.get('attempts', 0)
            for key in ('successes', 'busy', 'errors', 'no_slot', 'fork_pending', 'checks', 'slow'):
                st[key] += auto.get(key, 0)
            st['max_pause_us'] = max(st['max_pause_us'], auto.get('max_pause_us', 0))
            st['peak_wal'] = max(st['peak_wal'], auto.get('peak_wal', 0))
            st['limit'] = auto.get('limit')
        for st in stores.values():
            judged = st['successes'] + st['busy']
            st['success_rate'] = round(st['successes'] / judged, 4) if judged else None
            st['writers'] = self.args.auto_writers
        return stores

    def lane_totals(self) -> dict[str, Any]:
        """The plan 09 lanes' counters for the timeline and the status."""
        pops = {key: self.ctotal(key) for key in ('pops_chaos', 'pops_held', 'pops_fork', 'pops_pq')}
        durable = self.durable_buckets()
        auto = self.auto_summary().values()
        return {
            'pops': sum(pops.values()),
            **pops,
            **{key: self.ctotal(key) for key in ('pq_produced', 'pq_delivered', 'pq_duplicates', 'durable_calls', 'durable_false', 'durable_misses', 'durable_escapes', 'behind_calls', 'behind_timeouts', 'forks_behind', 'budget_overruns', 'stuck_forks', 'stuck_callers_timed_out', 'stuck_forks_completed', 'stuck_callers_hung', 'auto_writes')},
            'durable_busy': durable['busy'],
            'durable_broken': durable['broken'],
            'durable_lost_deletes': durable['lost_deletes'],
            'durable_log_lines': sum(sum((s.get('log_lines') or {}).values()) for s in self.last_stats.values()),
            'max_overrun_s': round(max((s.get('max_overrun', 0.0) for s in self.last_stats.values()), default=0.0), 3),
            'fork_max_s': round(max((s.get('fork_max_s', 0.0) for s in self.last_stats.values()), default=0.0), 3),
            'child_max_s': round(max((s.get('child_max_s', 0.0) for s in self.last_stats.values()), default=0.0), 3),
            'stuck_caller_max_s': round(max((s.get('stuck_caller_max_s', 0.0) for s in self.last_stats.values()), default=0.0), 3),
            'fork_timeouts_normal': sum((s.get('gate') or {}).get('fork_timeouts', 0) for s in self.last_stats.values() if s.get('role') != 'lanes'),
            'fork_timeouts_lanes': sum((s.get('gate') or {}).get('fork_timeouts', 0) for s in self.last_stats.values() if s.get('role') == 'lanes'),
            'auto_rounds': sum(st['rounds'] for st in auto),
            'auto_successes': sum(st['successes'] for st in auto),
            'auto_busy': sum(st['busy'] for st in auto),
            'auto_fork_pending': sum(st['fork_pending'] for st in auto),
            'auto_peak_wal': max((st['peak_wal'] for st in auto), default=0),
        }

    def proof_counters(self) -> dict[str, int]:
        """The counts that show each lane is still proving something, not only still running."""
        counters = {key: self.ctotal(key) for key in PROOF_COUNTERS}
        for python in {s['python'] for s in self.last_stats.values() if s.get('python')}:
            counters.update({f'{key}[{python}]': self.ctotal(key, python) for key in PROOF_COUNTERS})
        counters.update({f'auto_rounds[{n}]': st['rounds'] for n, st in self.auto_summary().items()})
        return counters

    def check_quiet(self, now: float) -> None:
        """Every interval: has a proof counter gone QUIET_AFTER without moving? So a lane that keeps
        its heartbeat but stops doing its job fails, as the end-of-run zero checks can't see that."""
        for name, value in self.proof_counters().items():
            state = self.quiet.setdefault(name, [value, self.started, False])
            if value != state[0]:
                state[:] = [value, now, state[2]]
                continue
            # A counter that moves rarely on a low --rate isn't quiet just for being slow, so the window
            # is also 20 of its own mean gaps. At the full shape every one moves far faster than that.
            window = max(QUIET_AFTER, QUIET_GAPS * (now - self.started) / value) if value else QUIET_AFTER
            if not state[2] and now - state[1] > window:
                state[2] = True
                self.event('lane-went-quiet', problem=True, counter=name, value=value, quiet_s=round(now - state[1]), detail=f'no movement for over {window:.0f}s')

    def check_lanes(self) -> dict[str, Any]:
        """The end-of-run checks that span workers. A lane that silently stopped can't pass: each
        one's count must be non-zero. Returns what the report shows."""
        t = self.lane_totals()
        for key in ('pops', 'durable_calls', 'durable_false', 'forks_behind', 'stuck_forks', 'pq_delivered'):
            if not t[key]:
                self.event('lane-silent', problem=True, counter=key, detail='this count is zero, so its lane proved nothing')
        # Each pop site separately only on a long run: at a low rate, a short one can miss a rare site.
        if self.elapsed_h() >= 1:
            for key in ('pops_chaos', 'pops_held', 'pops_fork', 'pops_pq'):
                if not t[key]:
                    self.event('lane-silent', problem=True, counter=key, detail='this count is zero, so its lane proved nothing')
        if t['durable_broken']:
            self.event('durable-broken', problem=True, count=t['durable_broken'], detail='a durable handle answered something other than a timeout as a miss: nothing in this soak should break a store')
        if t['stuck_forks'] != t['stuck_forks_completed']:
            # A worker killed mid-way, between the two counts, shows up here only if the gap is large.
            gap = t['stuck_forks'] - t['stuck_forks_completed']
            if gap > self.counts['side_kills'] + self.counts['side_restarts']:
                self.event('stuck-forks-unfinished', problem=True, started=t['stuck_forks'], completed=t['stuck_forks_completed'])
        logged = {'busy': 0, 'broken': 0, 'lost_deletes': 0}
        for s in self.last_stats.values():
            for bucket, lines in (s.get('log_lines') or {}).items():
                logged[bucket] += lines
        durable = self.durable_buckets()
        for bucket, failures in durable.items():
            if failures and not logged[bucket]:
                self.event('durable-not-logged', problem=True, bucket=bucket, failures=failures)
        auto = self.auto_summary()
        need = max(AUTO_MIN_ROUNDS, AUTO_MIN_ROUNDS * self.elapsed_h())
        for store, st in auto.items():
            # Rounds that reached the WAL. One skipped for a full pool or a pending fork proves nothing.
            rounds = st['successes'] + st['busy']
            if not rounds:
                self.event('lane-silent', problem=True, counter=f'auto_rounds[{store}]', detail='no forced checkpoint reached the WAL on this store')
            elif rounds < need:
                self.event('auto-too-few-rounds', problem=True, store=store, rounds=rounds, need=round(need))
            if st['errors']:
                self.event('auto-checkpoint-errors', problem=True, store=store, errors=st['errors'])
            judged = st['successes'] + st['busy']
            # Several writers in several processes outrun the checkpoint: a documented limit (D-024).
            if st['writers'] == 1 and judged and st['successes'] / judged < AUTO_MIN_SUCCESS_RATE:
                self.event('auto-low-success-rate', problem=True, store=store, successes=st['successes'], busy=st['busy'], threshold=AUTO_MIN_SUCCESS_RATE)
        per_python = {
            python: {key: self.ctotal(key, python) for key in ('pops_chaos', 'pops_held', 'pops_fork', 'pops_pq', 'pq_delivered', 'durable_calls', 'durable_false', 'forks_behind', 'stuck_forks', 'stuck_callers_timed_out', 'auto_writes')}
            for python in sorted({s['python'] for s in self.last_stats.values() if s.get('python')})
        }
        by_op: dict[str, dict[str, int]] = {}
        for s in self.last_stats.values():
            for bucket, ops in (s.get('durable') or {}).items():
                for op, n in ops.items():
                    if n:
                        by_op.setdefault(bucket, {}).setdefault(op, 0)
                        by_op[bucket][op] += n
        return {'totals': t, 'durable_lines': logged, 'durable_by_op': by_op, 'auto': auto, 'per_python': per_python, 'pq': self.pq_result, 'auto_min_success_rate': AUTO_MIN_SUCCESS_RATE, 'auto_min_rounds': round(need), 'side_kills': self.counts['side_kills'], 'side_restarts': self.counts['side_restarts']}

    def write_status(self, t: dict[str, Any], ops: int) -> None:
        remaining = max(0.0, self.ends - time.time()) / 3600
        lines = [
            f'diskcache-oxide soak  {time.strftime("%Y-%m-%d %H:%M:%S")}  elapsed {self.elapsed_h():.2f} h, {remaining:.2f} h to go',
            f'verdict so far: {"OK" if self.problems == 0 else f"{self.problems} PROBLEM(S), see events.jsonl"}',
            f'workers {t["workers"]}  ops/s {t["ops_per_s"]:,}  get p50/p99 {t["get_p50_us"]}/{t["get_p99_us"]} us  set p50/p99 {t["set_p50_us"]}/{t["set_p99_us"]} us',
            f'rss total {t["rss_total_mib"]} MiB  fds total {t["fds_total"]}  timeouts {t["timeouts_total"]}  stalls {t["stalls"]}',
            f'kills {t["kills"]}  restarts {t["restarts"]}  respawns {self.counts["respawns"]}  forks ok/failed {t["forks_ok"]}/{t["forks_failed"]}  maintenance calls {t["maint_calls"]}',
            f'chaos: {t["chaos_rows"]} rows, db {mib(t["chaos_db_bytes"])} MiB, wal {mib(t["chaos_wal_bytes"])} MiB, {t["chaos_value_files"]} value files, orphans {t["chaos_orphans"]}',
            f'ledger: {t["ledger_rows"]} rows, db {mib(t["ledger_db_bytes"])} MiB, wal {mib(t["ledger_wal_bytes"])} MiB, {t["ledger_value_files"]} value files, orphans {t["ledger_orphans"]}',
            f'held transactions: {t["held_commits"]} committed, {t["held_rollbacks"]} rolled back',
            f'pop: {t["pops"]:,} calls (chaos {t["pops_chaos"]:,}, held {t["pops_held"]:,}, fork {t["pops_fork"]:,}, exactly-once {t["pops_pq"]:,})',
            f'exactly-once: {t["pq_produced"]:,} produced, {t["pq_delivered"]:,} delivered, {t["pq_duplicates"]} duplicates',
            f'durable: {t["durable_calls"]:,} calls, {t["durable_false"]:,} answered as failures, swallowed busy {t["durable_busy"]:,} broken {t["durable_broken"]} lost deletes {t["durable_lost_deletes"]:,}, {t["durable_log_lines"]} log lines, {t["durable_escapes"]} escaped',
            f'forks behind traffic: {t["forks_behind"]:,} over {t["behind_calls"]:,} calls, {t["behind_timeouts"]:,} timed out, worst overrun {t["max_overrun_s"]} s, {t["budget_overruns"]} over budget, longest fork {t["fork_max_s"]} s, longest child {t["child_max_s"]} s',
            f'fork_timeouts: normal lanes {t["fork_timeouts_normal"]}, lanes {t["fork_timeouts_lanes"]}.  stuck forks: {t["stuck_forks"]} (callers timed out {t["stuck_callers_timed_out"]}, finished after {t["stuck_forks_completed"]}, callers hung {t["stuck_callers_hung"]}, longest caller {t["stuck_caller_max_s"]} s)',
            f'load {t["load_1m"]} on {os.cpu_count()} CPUs',
            *self.auto_status(),
            f'side chaos on the lanes and auto group: {self.counts["side_kills"]} kills, {self.counts["side_restarts"]} restarts',
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

    def auto_status(self) -> list[str]:
        return [
            f'auto store {n}: {st["rounds"]:,} forced rounds, {st["successes"]:,} ok, {st["busy"]} busy, {st["fork_pending"]} fork_pending, success rate {st["success_rate"]}, peak WAL {mib(st["peak_wal"])} MiB of {mib(st.get("limit"))}, {st["writers"]} writer(s)'
            for n, st in self.auto_summary().items()
        ]

    def run(self) -> int:
        self.event('start', args=vars(self.args), pythons=self.pythons, dir=str(self.dir), cpus=os.cpu_count())
        print(f'soak: {self.dir}\n  watch: tail -F {self.dir}/status.txt   report: open {self.dir}/report.html', flush=True)
        self.start_workers()
        stop = threading.Event()
        signal.signal(signal.SIGINT, lambda *_: stop.set())
        signal.signal(signal.SIGTERM, lambda *_: stop.set())
        next_interval = time.time() + self.args.interval
        next_chaos = time.time() + self.rng.uniform(10, self.args.chaos_every)
        next_side_chaos = time.time() + self.rng.uniform(30, self.args.chaos_every * 2)
        while not stop.is_set() and not self.fatal and time.time() < self.ends:
            stop.wait(1.0)
            self.reap()
            if time.time() >= next_chaos:
                self.chaos()
                next_chaos = time.time() + self.rng.uniform(self.args.chaos_every / 3, self.args.chaos_every)
            if time.time() >= next_side_chaos:
                self.side_chaos()
                next_side_chaos = time.time() + self.rng.uniform(self.args.chaos_every, self.args.chaos_every * 4)
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
        self.verify_held()
        self.verify_pq()
        for name in ('chaos', 'ledger', *self.extra_stores()):
            self.check_db(name, deep=True, orphans=True)
        self.event('lanes', **self.check_lanes())
        totals = {k: self.total(k) for k in ('ops', 'timeouts', 'forks_ok', 'forks_failed', 'reopens', 'once_tried', 'held_commits', 'held_rollbacks')}
        self.event('finish', problems=self.problems, counts=self.counts, totals=totals)
        from soak.report import build

        build(self.dir, final=True)
        verdict = 'PASS' if self.problems == 0 else f'FAIL ({self.problems} problems)'
        print(f'soak: {verdict}. Summary: {self.dir}/summary.md  report: {self.dir}/report.html', flush=True)
        return 0 if self.problems == 0 else 1


def assign_lost_to_kills(lost: list[str], kills: list[float], ticks_per_s: int, per_kill: int, window: float) -> list[str]:
    """The tokens no kill can explain. A kill takes at most `per_kill` tokens (a consumer per thread, each
    between its pop and its add), each produced at most `window` seconds before it. Oldest token first,
    to the earliest kill it can have, which excuses as many as any assignment can."""
    left = {n: per_kill for n in range(len(kills))}
    ordered = sorted(kills)
    unexcused = []
    for at, token in sorted((int(token.rsplit('.', 1)[1]) / ticks_per_s, token) for token in lost):
        for n, kill in enumerate(ordered):
            if at <= kill <= at + window and left[n]:
                left[n] -= 1
                break
        else:
            unexcused.append(token)
    return unexcused


def mib(n: Any) -> str:
    return '?' if n is None else f'{n / 2**20:.1f}'


PROOF_COUNTERS = (
    'pops_chaos', 'pops_held', 'pops_fork', 'pops_pq', 'pq_delivered', 'durable_calls', 'durable_false',
    'forks_behind', 'stuck_forks', 'stuck_callers_timed_out',
)  # fmt: skip
TIMELINE_FIELDS = [
    'time', 'elapsed_h', 'workers', 'ops_per_s', 'get_p50_us', 'get_p99_us', 'set_p50_us', 'set_p99_us',
    'rss_total_mib', 'fds_total', 'timeouts_total', 'problems', 'kills', 'forks_ok', 'forks_failed', 'maint_calls',
    'stalls', 'chaos_rows', 'chaos_db_bytes', 'chaos_wal_bytes', 'chaos_value_files', 'ledger_rows', 'ledger_db_bytes',
    'ledger_wal_bytes', 'ledger_value_files', 'chaos_orphans', 'wal_ckpt_attempts', 'wal_ckpt_busy',
    'wal_ckpt_max_pause_us', 'wal_ckpt_slow', 'get_max_us', 'set_max_us', 'restarts', 'ledger_orphans',
    'held_commits', 'held_rollbacks',
    'pops', 'pops_chaos', 'pops_held', 'pops_fork', 'pops_pq', 'pq_produced', 'pq_delivered', 'pq_duplicates',
    'durable_calls', 'durable_false', 'durable_misses', 'durable_escapes', 'durable_busy', 'durable_broken',
    'durable_lost_deletes', 'durable_log_lines', 'behind_calls', 'behind_timeouts', 'forks_behind', 'budget_overruns',
    'max_overrun_s', 'fork_max_s', 'fork_timeouts_normal', 'fork_timeouts_lanes', 'stuck_forks',
    'stuck_callers_timed_out', 'stuck_forks_completed', 'stuck_callers_hung', 'auto_writes', 'auto_rounds',
    'auto_successes', 'auto_busy', 'auto_fork_pending', 'auto_peak_wal', 'load_1m', 'child_max_s', 'stuck_caller_max_s',
]  # fmt: skip
WORKER_FIELDS = [
    'time', 'elapsed_h', 'id', 'python', 'long_lived', 'rss_mib', 'fds', 'ops', 'timeouts', 'problems', 'forks_ok',
    'forks_failed', 'get_p50_us', 'get_p99_us', 'set_p50_us', 'set_p99_us', 'stalled', 'get_max_us', 'set_max_us', 'role',
]  # fmt: skip


def last_python_is_free_threaded(pythons: str) -> bool:
    last = str(Path(pythons.split(',')[-1]).absolute())
    probe = subprocess.run([last, '-c', 'import sys; print(sys._is_gil_enabled())'], capture_output=True, text=True, timeout=60)
    return probe.stdout.strip() == 'False'


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
    parser.add_argument('--lanes', type=int, default=4, help='Extra processes for the durable, fork-behind and stuck-fork lanes, on top of --procs.')
    parser.add_argument('--auto-stores', type=int, default=2, help="Stores with wal_checkpoint='auto': each adds its writers and a reader process, on top of --procs.")
    parser.add_argument('--auto-writers', type=int, default=1, help='Writers per auto store. Over 1 is the documented limit: it is reported, and its success rate does not fail the run.')
    parser.add_argument('--seed', type=int, default=int(time.time()) % 100_000)
    parser.add_argument('--out', default=str(REPO / 'soak-results'))
    args = parser.parse_args()
    if not args.hours and not args.minutes:
        parser.error('give --hours and/or --minutes')
    if args.auto_stores and not last_python_is_free_threaded(args.pythons):
        parser.error('the last of --pythons must be free-threaded: only readers truly in parallel force the auto group\'s checkpoints (or use --auto-stores 0, which fails its zero-count checks)')
    try:
        from tests.proctitle import set_title

        set_title('soak-orchestrator')
    except Exception:
        pass
    return Soak(args).run()


if __name__ == '__main__':
    sys.exit(main())
