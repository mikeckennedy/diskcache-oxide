"""Where do reader threads wait? The 5c rule of plan 12 step 5: what share of wait samples is in `PoolShared::lock`.

Reads a macOS `sample` report (`/usr/bin/sample PID 10 -file out.txt`) or `perf script` output, and counts
samples by the blocking or spinning leaf they end in, then attributes each one to the oxide or SQLite
frame that called it:

- macOS: a wait sample ends in a kernel wait trap (`__psynch_mutexwait`, `__ulock_wait*`,
  `__semwait_signal`, ...). Every thread is sampled, so these are blocked time.
- `perf script` from `perf record -e cpu-clock -g` (on-CPU samples): a sample is a wait if it is in a
  lock or spin path in its top three frames (a mutex slow path, `futex`, our spin loops); blocked time
  doesn't appear.
- `perf script` from `perf record -e sched:sched_switch -g` (off-CPU events): every event is a thread
  switching out, so every one is a wait; counts are blocks, not time.

Usage:
    python bench/scaling/wait_share.py sample-report.txt
    perf script -i perf.data | python bench/scaling/wait_share.py --perf - [--all-on-cpu]
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import Counter

# The caller that owns a wait, checked from the leaf up (first match wins).
OWNERS = [
    # Rust symbols in a stripped-off release build are v0-mangled (`_RNv...4Pool7checkin`); PoolShared::lock
    # is inlined into Pool::checkin and Pool::checkout, and checkout into Store::lease.
    (
        'pool mutex (PoolShared::lock)',
        ('PoolShared', 'Pool::checkin', 'Pool::checkout', 'Store::lease')
        + ('Pool>::checkin', 'Pool>::checkout', 'Store>::lease')
        + ('4Pool7checkin', '4Pool8checkout', '5Store5lease'),
    ),
    ('SQLite shm mutex (unixShmLock)', ('unixShmLock',)),
    ('slot wait (Slots::acquire_until)', ('acquire_until', 'reserve_slot', '13acquire_until', '12reserve_slot')),
    ('other SQLite mutex', ('sqlite3_mutex_enter', 'sqlite_mutex', 'pthreadMutexEnter', 'SpinMutex')),
    ('gate', ('gate::', 'Gate::')),
    ('Python runtime', ('_PyMutex', 'PyMutex', 'PyThread', 'stop_the_world', '_PySemaphore', 'take_gil')),
]
# macOS wait traps.
MAC_WAITS = (
    '__psynch_mutexwait',
    '__ulock_wait',
    '__ulock_wait2',
    '__semwait_signal',
    '__psynch_cvwait',
    'swtch_pri',
    'thread_switch',
    'os_sync_wait_on_address',
)
# On-CPU frames that mean the thread is waiting (spinning or entering a slow path).
CPU_WAITS = (
    'lock_contended',
    'futex',
    'mutex_enter',
    'pthread_mutex',
    '__lll_lock_wait',
    'nanosleep',
    'sched_yield',
    'acquire_until',
    'spin_loop',
    'pthreadMutexEnter',
)
# The main thread waiting for the workers to finish.
JOINS = ('ThreadHandle_join', 'thread_join', 'pthread_join')
NOT_WAITS = ('select', 'poll', 'Barrier', 'thread_join', 'pthread_join', 'kevent', 'mach_msg', 'workq_kernreturn')


def owner(stack: list[str]) -> str:
    """stack is leaf first."""
    for name, needles in OWNERS:
        if any(n in frame for frame in stack for n in needles):
            return name
    return 'other: ' + next((f for f in stack if '_core' in f or 'diskcache' in f), stack[0])[:90]


def mac_stacks(text: str):
    """Yield (count, leaf-first stack) for each leaf of a `sample` call graph."""
    lines = text.split('Call graph:', 1)[-1].split('Total number in stack', 1)[0].splitlines()
    pat = re.compile(r'^([\s+!:|]*)(\d+) (.+)$')
    path: list[tuple[int, str, int]] = []  # (depth, symbol, count)
    nodes = []
    for line in lines:
        m = pat.match(line)
        if not m:
            continue
        depth, count, sym = len(m.group(1)), int(m.group(2)), m.group(3).split('  (in ')[0].strip()
        while path and path[-1][0] >= depth:
            path.pop()
        path.append((depth, sym, count))
        nodes.append(list(path))
    # A node's self count is its count minus its children's.
    for i, node in enumerate(nodes):
        depth, _, count = node[-1]
        children = 0
        for later in nodes[i + 1 :]:
            if later[-1][0] <= depth:
                break
            if len(later) == len(node) + 1:
                children += later[-1][2]
        if count - children > 0:
            yield count - children, [s for _, s, _ in reversed(node)]


def perf_stacks(text: str):
    for block in text.split('\n\n'):
        frames = [ln.strip() for ln in block.splitlines()[1:] if ln.strip()]
        if frames:
            yield 1, [f.split(' ', 1)[-1] for f in frames]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('path')
    ap.add_argument('--perf', action='store_true', help='Input is `perf script` output.')
    ap.add_argument('--all-waits', action='store_true', help='perf: count every sample as a wait (sched_switch).')
    ap.add_argument('--only-thread', default='', help='Only stacks containing this frame (e.g. a worker entry).')
    a = ap.parse_args()
    text = sys.stdin.read() if a.path == '-' else open(a.path).read()
    total = 0
    waits: Counter[str] = Counter()
    for count, stack in perf_stacks(text) if a.perf else mac_stacks(text):
        if a.only_thread and not any(a.only_thread in f for f in stack):
            continue
        total += count
        if any(n in f for f in stack[:3] for n in NOT_WAITS) or any(n in f for f in stack for n in JOINS):
            continue
        leafs = stack[:3] if a.perf else stack[:1]
        if a.all_waits or any(w in f for f in leafs for w in (CPU_WAITS if a.perf else MAC_WAITS)):
            waits[owner(stack)] += count
    n = sum(waits.values())
    print(f'{total:,} samples, {n:,} waiting ({n / max(total, 1):.0%})')
    for name, c in waits.most_common(12):
        print(f'  {c:8,}  {c / max(n, 1):6.1%}  {name}')


if __name__ == '__main__':
    main()
