#!/usr/bin/env python3
"""Judges a ThreadSanitizer log against allowed-races.txt (plan 14, "Rules for reports").

Usage: tsan_races.py <log> <allowed-races.txt> [--expect-ours]

Every `WARNING: ThreadSanitizer:` report in the log must pass three checks: it is a data race; its
two accesses' top frames (`#0` of each stack) together form a pair listed in the allow file, in
either order; and neither top frame is ours. Our frames further down, as the callers of SQLite or
of std, are expected: every SQLite race in this process has them. Anything else fails: a race whose
access is in our code, an unlisted pair, a lock-order inversion, a thread leak, a use-after-free.

A frame is ours when its symbol's defining crate is `diskcache_oxide_core` or `diskcache_oxide_py`
(read from the v0 mangled name, where the first crate root is the defining one; a std generic that
we instantiated carries our crate only as a trailing suffix), or when a demangled or located frame
names our crate's path. TSan's symbolizer on macOS (`atos`) gives no file:line for frames inside a
static archive, so SQLite's and std's frames are judged by name. An allow entry matches a frame
whose name equals it, or, for a mangled fragment (an entry starting with a digit, so it carries no
crate hash, which changes with every std build), a frame whose name contains it.

With --expect-ours (the canary run) the judgement flips: the log must hold at least one data race
whose `#0` frame is ours, which proves the run can see a race in our code at all.

Prints what it found and exits 0 for a pass, 1 for a fail. Standard library only; it runs on the
mini and in the Linux container.
"""

from __future__ import annotations

import collections
import re
import sys
from pathlib import Path

REPORT = re.compile(r'^WARNING: ThreadSanitizer: (.*)$', re.M)
FRAME = re.compile(r'^\s+#(\d+) (\S+) (\S+)', re.M)
# The first crate root in a v0 mangled symbol: `C` then a base-62 disambiguator (`s...`) then
# `<len><ident>`; the first one is the crate the item is defined in.
V0_FIRST_CRATE = re.compile(r'^_R[A-Za-z0-9]*?C(?:s[A-Za-z0-9]*_)?(\d+)([A-Za-z_][A-Za-z0-9_]*)')
OUR_CRATES = ('diskcache_oxide_core', 'diskcache_oxide_py')
OUR_PATH = re.compile(r'diskcache_oxide_(?:core|py)::|crates/diskcache-oxide')


def allowed_pairs(path: Path) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for raw in path.read_text().splitlines():
        line = raw.split('#', 1)[0].strip()
        if line:
            a, b, *_ = line.split()
            pairs.append((a, b))
    return pairs


def entry_matches(entry: str, func: str) -> bool:
    return func == entry or (entry[:1].isdigit() and entry in func)


def pair_for(f1: str, f2: str, pairs: list[tuple[str, str]]) -> tuple[str, str] | None:
    for a, b in pairs:
        if (entry_matches(a, f1) and entry_matches(b, f2)) or (entry_matches(b, f1) and entry_matches(a, f2)):
            return (a, b)
    return None


def is_ours(func: str, loc: str) -> bool:
    m = V0_FIRST_CRATE.match(func)
    if m:
        n, ident = int(m.group(1)), m.group(2)
        return ident[:n] in OUR_CRATES
    return bool(OUR_PATH.search(func) or OUR_PATH.search(loc))


def stacks_of(report: str) -> list[list[tuple[str, str]]]:
    """The frames of each stack in the report, in order, split at every `#0`."""
    stacks: list[list[tuple[str, str]]] = []
    for n, func, loc in FRAME.findall(report):
        if n == '0':
            stacks.append([])
        if stacks:
            stacks[-1].append((func, loc))
    return stacks


def main() -> int:
    log, allow = Path(sys.argv[1]), Path(sys.argv[2])
    expect_ours = '--expect-ours' in sys.argv[3:]
    text = log.read_text(errors='replace')
    heads = list(REPORT.finditer(text))
    reports = [text[m.start() : heads[i + 1].start() if i + 1 < len(heads) else len(text)] for i, m in enumerate(heads)]
    pairs = allowed_pairs(allow)

    seen: collections.Counter[str] = collections.Counter()
    bad: list[str] = []
    ours = 0
    for report in reports:
        head = REPORT.match(report)
        kind = head.group(1).split(' (pid')[0].strip() if head else '(unparsed)'
        stacks = stacks_of(report)
        tops = [stack[0] for stack in stacks if stack]
        if kind != 'data race':
            bad.append(f'{kind}: {_top_line(tops)}')
            continue
        if len(tops) < 2:
            bad.append(f'data race with one stack only: {_top_line(tops)}')
            continue
        if any(is_ours(func, loc) for func, loc in tops[:2]):
            ours += 1
            if not expect_ours:
                bad.append(f'data race with an access in our code: {_top_line(tops)}')
            continue
        (f1, _), (f2, _) = tops[0], tops[1]
        pair = pair_for(f1, f2, pairs)
        if pair is None:
            bad.append(f'data race not in {allow.name}: {_top_line(tops)}')
            continue
        seen[f'{pair[0]} / {pair[1]}'] += 1

    for name, n in sorted(seen.items()):
        print(f'  allowed: {n:>4} x {name}')
    if expect_ours:
        if ours:
            print(f'  canary: {ours} report(s) with an access in our code, as required')
            return 0
        print('  canary: no report with an access in our code: this run cannot see races in our code')
        return 1
    for line in bad:
        print(f'  FAIL: {line}')
    print(f'  {len(reports)} report(s): {sum(seen.values())} allowed, {len(bad)} not')
    return 1 if bad else 0


def _top_line(tops: list[tuple[str, str]]) -> str:
    return ' / '.join(f'{func} {loc}' for func, loc in tops[:2]) or '(no frames parsed)'


if __name__ == '__main__':
    raise SystemExit(main())
