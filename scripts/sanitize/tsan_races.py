#!/usr/bin/env python3
"""Judges a ThreadSanitizer log against allowed-races.txt (plan 14, "Rules for reports").

Usage: tsan_races.py <log> <allowed-races.txt> [--expect-ours]
       tsan_races.py --self-test   (parses one frame line of each shape both platforms print)

Every `WARNING: ThreadSanitizer:` report in the log must pass three checks: it is a data race; its
two accesses' top frames (`#0` of each stack) together form a pair listed in the allow file, in
either order; and neither top frame is ours. Our frames further down, as the callers of SQLite or
of std, are expected: every SQLite race in this process has them. Anything else fails: a race whose
access is in our code, an unlisted pair, a lock-order inversion, a thread leak, a use-after-free.

The access frame is `#0`, unless `#0` is one of TSan's libc interceptors (`memcpy`, `memcmp`,
`memset`, ...), which Linux shows as the top frame of a C access made through them; then it is the
frame below. A frame is ours when its symbol's defining crate is `diskcache_oxide_core` or
`diskcache_oxide_py`: for a v0 mangled name (macOS, where `atos` leaves Rust names mangled and gives
no file:line for frames in a static archive) the first crate root is the defining one, and a std
generic we instantiated carries our crate only as a trailing suffix; for a demangled name (Linux)
the defining path is the part before the first generic argument, and for a method on a type,
`<T>::method`, the type's own path, so `std::thread::spawn_unchecked::<diskcache_oxide_core::...>`
is std's. A frame whose location is under `crates/diskcache-oxide` is ours either way. An allow
entry matches a frame whose name equals it, or, for a mangled fragment (an entry starting with a
digit, so it carries no crate hash, which changes with every std build), a frame whose name
contains it.

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
# A frame: `#N <name> <location> (<module>+0x<offset>)`, on both platforms; a demangled name can hold
# spaces (`<T as Trait>::f`, `dyn`, `, ` between generic arguments), so the name runs to the last
# space before the location, and the trailing module+offset group anchors the match.
FRAME = re.compile(r'^\s+#(\d+) (.+?) (\S+) \(\S+\+0x[0-9a-f]+\)', re.M)
# The first crate root in a v0 mangled symbol: `C` then a base-62 disambiguator (`s...`) then
# `<len><ident>`; the first one is the crate the item is defined in.
V0_FIRST_CRATE = re.compile(r'^_R[A-Za-z0-9]*?C(?:s[A-Za-z0-9]*_)?(\d+)([A-Za-z_][A-Za-z0-9_]*)')
OUR_CRATES = ('diskcache_oxide_core', 'diskcache_oxide_py')
OUR_LOCATION = re.compile(r'crates/diskcache-oxide')
# TSan's libc interceptors: the access is in the frame below them. Other TSan builds print them as
# `__interceptor_memcpy`, `__tsan_memcpy` or `memcpy.part.0`.
INTERCEPTORS = frozenset(
    'memcpy memmove memset memcmp bcopy bzero strlen strcpy strncpy strcmp strncmp strcat strchr memchr'.split()
)


def is_interceptor(func: str) -> bool:
    base = func.split('.', 1)[0]
    for prefix in ('__interceptor_', '__tsan_'):
        base = base.removeprefix(prefix)
    return base in INTERCEPTORS


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


def defining_crate(func: str) -> str:
    """The crate a demangled Rust path is defined in: the first segment of the path before any
    generic argument; for `<T>::method` (a method on a type), the first segment of T's path."""
    path = func
    if path.startswith('<'):
        path = path[1:]
    path = path.split('<', 1)[0]
    return path.split('::', 1)[0].strip()


def is_ours(func: str, loc: str) -> bool:
    if OUR_LOCATION.search(loc):
        return True
    m = V0_FIRST_CRATE.match(func)
    if m:
        n, ident = int(m.group(1)), m.group(2)
        return ident[:n] in OUR_CRATES
    return defining_crate(func) in OUR_CRATES


def access_frame(stack: list[tuple[str, str]]) -> tuple[str, str]:
    """The frame where the access happened: `#0`, or the frame below a libc interceptor."""
    if len(stack) > 1 and is_interceptor(stack[0][0]):
        return stack[1]
    return stack[0]


def stacks_of(report: str) -> list[list[tuple[str, str]]]:
    """The frames of each stack in the report, in order, split at every `#0`."""
    stacks: list[list[tuple[str, str]]] = []
    for n, func, loc in FRAME.findall(report):
        if n == '0':
            stacks.append([])
        if stacks:
            stacks[-1].append((func, loc))
    return stacks


# One frame line of each shape the two platforms print, with the name and location it must parse to.
SELF_TEST = [
    (
        '    #1 walIndexWriteHdr /opt/cargo/registry/src/x/sqlite3.c:67855 (bin-93a0+0x814270) (BuildId: c97)',
        ('walIndexWriteHdr', '/opt/cargo/registry/src/x/sqlite3.c:67855'),
    ),
    (
        '    #23 std::thread::spawn_unchecked::<diskcache_oxide_core::f::{closure#0}, ()> '
        '/opt/rustup/x/lifecycle.rs:70 (bin+0x1)',
        ('std::thread::spawn_unchecked::<diskcache_oxide_core::f::{closure#0}, ()>', '/opt/rustup/x/lifecycle.rs:70'),
    ),
    (
        '    #0 walTryBeginRead <null> (diskcache_oxide_core-920a:arm64+0x100734e90)',
        ('walTryBeginRead', '<null>'),
    ),
    (
        '    #2 _RINvNtNtCs2qRG8k3j1Aj_3std6thread9lifecycle lifecycle.rs:116 (bin:arm64+0x1005428a4)',
        ('_RINvNtNtCs2qRG8k3j1Aj_3std6thread9lifecycle', 'lifecycle.rs:116'),
    ),
    (
        '    #27 __rust_try 4igyana48a0aqh9ikopivkj86:? (bin:arm64+0x10057eec4)',
        ('__rust_try', '4igyana48a0aqh9ikopivkj86:?'),
    ),
    (
        '    #5 <alloc::vec::Vec<u8> as diskcache_oxide_core::codec::Encode>::encode '
        '/work/repo/crates/diskcache-oxide-core/src/codec.rs:9 (bin+0x2)',
        (
            '<alloc::vec::Vec<u8> as diskcache_oxide_core::codec::Encode>::encode',
            '/work/repo/crates/diskcache-oxide-core/src/codec.rs:9',
        ),
    ),
]


def self_test() -> int:
    bad = 0
    for line, (func, loc) in SELF_TEST:
        got = [(f, loc) for _, f, loc in FRAME.findall(line)]
        if got != [(func, loc)]:
            print(f'  self-test FAIL: {line.strip()!r} parsed as {got!r}')
            bad += 1
    checks = [
        (is_ours(SELF_TEST[1][1][0], SELF_TEST[1][1][1]), False, "a std generic over our type is std's"),
        (is_ours(SELF_TEST[5][1][0], SELF_TEST[5][1][1]), True, 'our impl on a foreign type is ours, by location'),
        (
            is_ours('<std::sys::thread::unix::Thread>::new', '/opt/rustup/x/mod.rs:1'),
            False,
            "a method on a std type is std's",
        ),
        (
            is_ours('_RNCNvNtNtCsgNRJR2KUrV4_20diskcache_oxide_core5store5tests0B3_', '<null>'),
            True,
            'a v0 name rooted in our crate',
        ),
        (is_interceptor('__interceptor_memcpy') and is_interceptor('memcpy.part.0'), True, 'interceptor name variants'),
    ]
    for got, want, what in checks:
        if got != want:
            print(f'  self-test FAIL: {what}: got {got}')
            bad += 1
    print('  self-test ok' if not bad else f'  self-test: {bad} failure(s)')
    return 1 if bad else 0


def main() -> int:
    if sys.argv[1:] == ['--self-test']:
        return self_test()
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
        tops = [access_frame(stack) for stack in stacks if stack]
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
