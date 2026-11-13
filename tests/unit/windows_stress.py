"""The Windows stress test (plan 25, decision 2), run from test_windows_stress.py or on its own:

    python -m tests.unit.windows_stress --seconds 7200 [--procs 6] [--threads 4] [--root DIR]

Spawned worker processes each run threads doing the 99:1 read:write mix (with a few deletes) on one
`Cache` and one `FanoutCache` over a small hot keyspace, with values on both sides of the spill
threshold, so readers race overwrites of spilled values and sweeps race writers across processes
(R-H4, R-M7), and a thread pushing, pulling and peeking such values on the `Cache`'s queue.
`--no-fanout` leaves the `FanoutCache` out, for a filesystem without hard links (exFAT). Each worker
also writes unique keys to a ledger cache and appends each one to its ack file once `set` has
returned. An admin process runs `clear`, `cull` and `check(fix=True)` at random, and the parent
kills a random process, the admin included, with `Process.kill()` (`TerminateProcess`) every few
seconds and starts a replacement. At the end every acknowledged ledger write must read back exactly, every
value left in the churned caches must be one a writer wrote for its key, no worker may have logged
an error, and `check()` must be clean once `check(fix=True)` has removed what the kills left.

Exits 0 and prints a summary if all of that holds; otherwise prints the failures and exits 1.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import multiprocessing
import random
import shutil
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path
from typing import Any

from tests.proctitle import set_title

TIMEOUT = 30.0
HOT_KEYS = 200
# Small, inline, and two spilled sizes (the threshold is 64 KiB).
SIZES = (100, 3_000, 70_000, 200_000)
LEDGER_SIZES = (100, 1_000, 100_000)
LEDGER_WEIGHTS = (6, 3, 1)
LEDGER_PAUSE = 0.5
SPAWN = multiprocessing.get_context('spawn')
STOP_FILE = 'stop'
NO_FANOUT = 'no-fanout'
QUEUE_PAUSE = 0.01
STOP_POLL = 0.2


def make_value(key: str, writer: str, seq: int, size: int) -> bytes:
    """A value that says which key, writer and write it is, and is checkable from itself alone."""
    head = f'{key}|{writer}|{seq}|{size}|'.encode()
    block = hashlib.blake2b(head, digest_size=32).digest()
    room = max(0, size - len(head))
    return head + (block * (room // len(block) + 1))[:room]


def value_problem(key: str, value: object) -> str | None:
    """Why `value` isn't one `make_value` made for `key`, or None if it is."""
    if not isinstance(value, bytes):
        return f'{key}: not bytes but {type(value).__name__}'
    parts = value.split(b'|', 4)
    if len(parts) != 5:
        return f'{key}: no header'
    got_key, writer, seq, size = (p.decode(errors='replace') for p in parts[:4])
    if got_key != key:
        return f'{key}: holds the value of {got_key}'
    if not seq.isdigit() or not size.isdigit() or value != make_value(key, writer, int(seq), int(size)):
        return f'{key}: damaged ({len(value)} bytes, header {value[:60]!r})'
    return None


class Log:
    """One worker's error file, appended and flushed at once, since the worker may be killed."""

    def __init__(self, path: Path) -> None:
        self._file = path.open('a', encoding='utf-8')
        self._lock = threading.Lock()

    def error(self, what: str) -> None:
        with self._lock:
            self._file.write(json.dumps(what) + '\n')
            self._file.flush()


def _stop_signal(base: Path) -> threading.Event:
    """Set once the parent creates the stop file. Not a `multiprocessing.Event`: its `is_set()` takes a lock
    every process shares, and a worker killed while holding it hung all the others."""
    stop = threading.Event()

    def poll() -> None:
        while not (base / STOP_FILE).exists():
            time.sleep(STOP_POLL)
        stop.set()

    threading.Thread(target=poll, name='stop-poll', daemon=True).start()
    return stop


def _open_caches(root: Path) -> tuple[Any, Any, Any | None]:
    from diskcache_oxide import Cache, FanoutCache

    ledger = Cache(root / 'ledger', timeout=TIMEOUT, size_limit=1 << 40)
    churn = Cache(root / 'churn', timeout=TIMEOUT)
    # Without hard links (exFAT) a FanoutCache can't make its manifest, so --no-fanout leaves it out.
    fan = None if (root / NO_FANOUT).exists() else FanoutCache(root / 'fanout', shards=4, timeout=TIMEOUT)
    return ledger, churn, fan


def _churn(cache: Any, name: str, rng: random.Random, stop: Any, log: Log, counts: dict[str, int]) -> None:
    seq = 0
    while not stop.is_set():
        key = f'h{rng.randrange(HOT_KEYS)}'
        roll = rng.random()
        try:
            if roll < 0.99:
                value = cache.get(key)
                counts['reads'] += 1
                if value is not None and (problem := value_problem(key, value)) is not None:
                    log.error(f'read: {problem}')
            elif roll < 0.997:
                seq += 1
                cache.set(key, make_value(key, name, seq, rng.choice(SIZES)))
                counts['writes'] += 1
            else:
                cache.delete(key)
                counts['deletes'] += 1
        except Exception:
            log.error(traceback.format_exc())


def _queue(cache: Any, name: str, rng: random.Random, stop: Any, log: Log, counts: dict[str, int]) -> None:
    """Pushes and pulls items on both sides of the spill threshold on the shared queue, and peeks:
    every worker's queue reads race the others' pulls (and the admin's clears) for the head, which
    goes through the same read path as `get`."""
    seq = 0
    while not stop.wait(QUEUE_PAUSE * rng.random()):
        try:
            roll = rng.random()
            if roll < 0.5:
                seq += 1
                cache.push(make_value('queue', name, seq, rng.choice(SIZES)))
                counts['pushed'] += 1
            else:
                _, value = cache.peek() if roll < 0.6 else cache.pull()
                if value is not None:
                    counts['pulled'] += roll >= 0.6
                    if (problem := value_problem('queue', value)) is not None:
                        log.error(f'queue: {problem}')
        except Exception:
            log.error(traceback.format_exc())


def _ledger(cache: Any, name: str, rng: random.Random, stop: Any, acks: Path, log: Log) -> None:
    with acks.open('a', encoding='utf-8') as out:
        n = 0
        while not stop.is_set():
            n += 1
            key = f'{name}-{n}'
            size = rng.choices(LEDGER_SIZES, LEDGER_WEIGHTS)[0]
            try:
                cache.set(key, make_value(key, name, n, size))
            except Exception:
                log.error(traceback.format_exc())
                continue
            out.write(f'{key}\n')
            out.flush()  # to the OS, which keeps it when the process is killed
            stop.wait(LEDGER_PAUSE * rng.random())


def worker(root: str, name: str, threads: int) -> None:
    set_title(f'dco-windows-stress[{name}]')
    base = Path(root)
    stop = _stop_signal(base)
    log = Log(base / 'logs' / f'{name}.errors')
    ledger, churn, fan = _open_caches(base)
    counts = {'reads': 0, 'writes': 0, 'deletes': 0, 'pushed': 0, 'pulled': 0}
    rng = random.Random(name)
    workers = [
        threading.Thread(target=_churn, args=(c, name, random.Random(rng.random()), stop, log, counts))
        for c in [churn, fan or churn] * ((threads + 1) // 2)
    ][:threads]
    workers.append(threading.Thread(target=_queue, args=(churn, name, random.Random(rng.random()), stop, log, counts)))
    workers.append(
        threading.Thread(
            target=_ledger, args=(ledger, name, random.Random(rng.random()), stop, base / 'acks' / name, log)
        )
    )
    for t in workers:
        t.start()
    for t in workers:
        t.join()
    counts['left_value_files'] = ledger._left_value_files() + churn._left_value_files()
    if fan is not None:
        counts['left_value_files'] += sum(fan._left_value_files())
    (base / 'logs' / f'{name}.counts').write_text(json.dumps(counts))
    for c in (ledger, churn, fan):
        if c is not None:
            c.close()


def admin(root: str, name: str) -> None:
    set_title(f'dco-windows-stress[{name}]')
    base = Path(root)
    stop = _stop_signal(base)
    log = Log(base / 'logs' / f'{name}.errors')
    _, churn, fan = _open_caches(base)
    targets = [c for c in (churn, fan) if c is not None]
    rng = random.Random(name)
    actions = {
        'clear': lambda c: c.clear(),
        'cull': lambda c: c.cull(),
        'check-fix': lambda c: c.check(fix=True),
    }
    done = dict.fromkeys(actions, 0)
    while not stop.wait(rng.uniform(1.0, 4.0)):
        action = rng.choice(list(actions))
        try:
            actions[action](rng.choice(targets))
            done[action] += 1
        except Exception:
            log.error(traceback.format_exc())
    (base / 'logs' / f'{name}.counts').write_text(json.dumps(done))
    for c in targets:
        c.close()


def sweep_racer(root: str, name: str, rounds: int) -> None:
    """Writes a spilled value under a fresh key and deletes it, `rounds` times: each delete can empty
    its value directory, and the sweep that removes it races the other racers' writes into it (the
    core's `writers_racing_a_sweeper_in_one_directory_never_fail`, across processes). Any error is
    logged, since every write must succeed."""
    from diskcache_oxide import Cache

    set_title(f'dco-sweep-racer[{name}]')
    base = Path(root)
    log = Log(base / 'logs' / f'{name}.errors')
    cache = Cache(base / 'sweep', timeout=TIMEOUT)
    for n in range(rounds):
        key = f'{name}-{n}'
        try:
            assert cache.set(key, make_value(key, name, n, 70_000)) is True
            assert cache.delete(key) is True
        except Exception:
            log.error(traceback.format_exc())
    cache.close()


def run_sweep_race(root: Path, procs: int = 6, rounds: int = 1500) -> list[str]:
    """Runs `procs` sweep racers to the end and returns their errors (which must be none)."""
    (root / 'logs').mkdir(parents=True, exist_ok=True)
    racers = [SPAWN.Process(target=sweep_racer, args=(str(root), f's{i}', rounds)) for i in range(procs)]
    for p in racers:
        p.start()
    failures = []
    for p in racers:
        p.join(600)
        if p.exitcode != 0:
            failures.append(f'{p.name} exited with {p.exitcode}')
    for errors in sorted((root / 'logs').glob('s*.errors')):
        failures += [f'{errors.stem}: {json.loads(line)}' for line in errors.read_text(encoding='utf-8').splitlines()]
    return failures


def run(
    root: Path, seconds: float, procs: int = 6, threads: int = 4, kill_every: float = 8.0, fanout: bool = True
) -> dict[str, Any]:
    """Runs the stress test in `root` for `seconds` and returns its summary; `failures` must be empty.
    `fanout=False` leaves the FanoutCache out, for a filesystem without hard links (exFAT)."""
    for sub in ('logs', 'acks'):
        (root / sub).mkdir(parents=True, exist_ok=True)
    (root / STOP_FILE).unlink(missing_ok=True)
    if fanout:
        (root / NO_FANOUT).unlink(missing_ok=True)
    else:
        (root / NO_FANOUT).touch()
    rng = random.Random(0)
    started = {'w': 0, 'admin': 0}

    def start(kind: str) -> Any:
        started[kind] += 1
        name = f'{kind}{started[kind]}'
        target, args = (worker, (str(root), name, threads)) if kind == 'w' else (admin, (str(root), name))
        p = SPAWN.Process(target=target, args=args, name=name)
        p.start()
        return p

    live = [start('admin'), *(start('w') for _ in range(procs))]
    kills = 0
    failures: list[str] = []
    deadline = time.monotonic() + seconds
    while (now := time.monotonic()) < deadline:
        time.sleep(min(rng.uniform(0.5, 1.5) * kill_every, max(0.0, deadline - now)))
        if time.monotonic() >= deadline:
            break
        for p in [p for p in live if not p.is_alive()]:
            failures.append(f'{p.name} exited by itself with {p.exitcode}')
            live.remove(p)
            live.append(start(p.name.rstrip('0123456789')))
        # The admin too, now and then: a clear, cull or check(fix=True) killed partway.
        victim = rng.choice(live)
        victim.kill()
        victim.join(30)
        kills += 1
        live.remove(victim)
        live.append(start(victim.name.rstrip('0123456789')))
    (root / STOP_FILE).touch()
    for p in live:
        p.join(120)
        if p.is_alive():
            failures.append(f"{p.name} didn't stop within 120 s")
            p.kill()
            p.join(30)
        elif p.exitcode != 0:
            failures.append(f'{p.name} exited with {p.exitcode} at the end')
    return _verify(root, kills, started, failures)


def _summed(paths: list[Path]) -> dict[str, int]:
    total: dict[str, int] = {}
    for found in paths:
        for k, v in json.loads(found.read_text()).items():
            total[k] = total.get(k, 0) + v
    return total


def _verify(root: Path, kills: int, started: dict[str, int], failures: list[str]) -> dict[str, Any]:
    for errors in sorted((root / 'logs').glob('*.errors')):
        for line in errors.read_text(encoding='utf-8').splitlines():
            failures.append(f'{errors.stem}: {json.loads(line)}')
    # Only processes that reached the end report: a killed one's counts are lost.
    counts = _summed(list((root / 'logs').glob('w*.counts')))
    admin_done = _summed(list((root / 'logs').glob('admin*.counts')))
    ledger, churn, fan = _open_caches(root)
    acked = 0
    for acks in (root / 'acks').iterdir():
        for key in acks.read_text(encoding='utf-8').splitlines():
            acked += 1
            value = ledger.get(key)
            if value is None:
                failures.append(f'ledger: the acknowledged write {key} is missing')
            elif (problem := value_problem(key, value)) is not None:
                failures.append(f'ledger: {problem}')
    fixed = {}
    for name, cache in (('churn', churn), ('fanout', fan)):
        if cache is None:
            continue
        for key in list(cache):
            value = cache.get(key)
            # A queue item's key is its number; its value names the queue.
            expected = key if isinstance(key, str) else 'queue'
            if value is not None and (problem := value_problem(expected, value)) is not None:
                failures.append(f'{name}: {problem}')
        fixed[name] = [repr(f) for f in cache.check(fix=True)]
        if remaining := cache.check():
            failures.append(f'{name}: check() after check(fix=True) still finds {remaining}')
    for c in (ledger, churn, fan):
        if c is not None:
            c.close()
    return {
        'kills': kills,
        'processes_started': started,
        'acked_ledger_writes': acked,
        'ops': counts,
        'admin': admin_done,
        'fixed_at_the_end': fixed,
        'failures': failures,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--seconds', type=float, default=600)
    parser.add_argument('--procs', type=int, default=6)
    parser.add_argument('--threads', type=int, default=4)
    parser.add_argument('--kill-every', type=float, default=8.0)
    parser.add_argument('--no-fanout', action='store_true', help='leave the FanoutCache out (exFAT has no hard links)')
    parser.add_argument(
        '--root', type=Path, help='kept afterwards; default a temporary directory, removed if it passes'
    )
    args = parser.parse_args()
    root = args.root or Path(tempfile.mkdtemp(prefix='dco-windows-stress-'))
    set_title('dco-windows-stress[parent]')
    summary = run(root, args.seconds, args.procs, args.threads, args.kill_every, fanout=not args.no_fanout)
    print(json.dumps({**summary, 'root': str(root)}, indent=2))
    if summary['failures']:
        return 1
    if args.root is None:
        shutil.rmtree(root, ignore_errors=True)
    return 0


if __name__ == '__main__':
    from tests import orphan_guard

    orphan_guard.install()
    sys.exit(main())
