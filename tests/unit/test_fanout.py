"""FanoutCache's oxide-only surface (plan 13, step 13d): layout, budgets, `shard_of`, and lifecycle.

The routed calls diskcache shares are in tests/conformance/test_fanout.py. The shard hash is frozen:
the golden table below is the one in `crates/diskcache-oxide-core/src/fanout.rs`, and a changed
vector is a format break, never a test update.
"""

from __future__ import annotations

import gc
import logging
import os
import pickle
import random
import signal
import subprocess
import sys
import threading
import time
import traceback
from collections.abc import Callable, Generator, Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any

import pytest
import xxhash
from diskcache_oxide import Cache, FanoutCache, Key, LayoutMismatchError, Timeout

from tests.conformance.proc_workers import SqlitePeer, stored_rows
from tests.keys import str_keys
from tests.platforms import unix_only
from tests.unit.fork_timeout_scenarios import fds_inside
from tests.unit.spawn_race import race

if sys.platform != 'win32':
    import resource

MiB = 1 << 20
GiB = 1 << 30
MANIFEST = 'diskcache-oxide-fanout'
SHARD_COUNTS = (1, 2, 7, 8, 64, 256)

# (key, xxh3-64 of b'\x01' + key, shard at each of SHARD_COUNTS), from the core's golden table.
GOLDEN: list[tuple[str, int, tuple[int, ...]]] = [
    ('', 0xE12E_F9D2_EB86_CEEB, (0, 1, 6, 7, 56, 225)),
    ('a', 0x21C8_3245_4304_9A44, (0, 0, 0, 1, 8, 33)),
    ('0123456789abcdef', 0x0B8B_7132_1597_C08E, (0, 0, 0, 0, 2, 11)),
    ('k' * 128, 0xF8FA_13E9_A5C3_E5C7, (0, 1, 6, 7, 62, 248)),
    ('k' * 240, 0x7222_48CC_57C9_97EC, (0, 0, 3, 3, 28, 114)),
    ('k' * 241, 0xA6F2_62B9_1A04_B20F, (0, 1, 4, 5, 41, 166)),
    ('k' * 239, 0xAE00_E6DB_4AEF_786B, (0, 1, 4, 5, 43, 174)),
    ('\0', 0xAB94_AC0A_CE72_76C0, (0, 1, 4, 5, 42, 171)),
    ('\U0001f40d\U0010ffffé', 0x3051_29DC_82D8_543D, (0, 0, 1, 1, 12, 48)),
]


def _expected_shard(key: str, shards: int) -> int:
    """The shard the format defines: multiply-high of xxh3-64 over the str type tag and the UTF-8 key."""
    return (xxhash.xxh3_64_intdigest(b'\x01' + key.encode()) * shards) >> 64


# Opening 256 shards takes about 1,030 file descriptors, over Linux's default soft limit of 1,024.
FDS_FOR_256 = 1200


@pytest.fixture
def fd_room() -> Iterator[tuple[int, ...]]:
    """The shard counts this process can open: raises the soft open-file limit (to at most 4,096) for
    the 256-shard cache, restores it afterwards, and leaves 256 out only when the hard limit can't fit it
    (`ulimit -n` in a shell lowers both)."""
    if sys.platform == 'win32':  # Win32 handles, which SQLite uses, have no per-process limit
        yield SHARD_COUNTS
        return
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    wanted = 4096 if hard == resource.RLIM_INFINITY else min(hard, 4096)
    resource.setrlimit(resource.RLIMIT_NOFILE, (max(soft, wanted), hard))
    yield SHARD_COUNTS if max(soft, wanted) >= FDS_FOR_256 else SHARD_COUNTS[:-1]
    resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard))


def _skip_if_short(counts: tuple[int, ...]) -> None:
    """Called after every count that fits was checked: says so when 256 couldn't be."""
    if counts != SHARD_COUNTS and sys.platform != 'win32':
        hard = resource.getrlimit(resource.RLIMIT_NOFILE)[1]
        pytest.skip(f'256 shards need about {FDS_FOR_256} fds; the hard limit is {hard}')


def _shards_of(root: Path, count: int, keys: list[str]) -> list[int]:
    """`shard_of` for each key on a FanoutCache of `count` shards, opened only for the call."""
    with FanoutCache(root / str(count), shards=count) as cache:
        return [cache.shard_of(k) for k in keys]


# -- the shard hash --------------------------------------------------------------------------------


def test_the_golden_table_holds_through_shard_of(tmp_path: Path, fd_room: tuple[int, ...]) -> None:
    keys = [key for key, _, _ in GOLDEN]
    for key, digest, _ in GOLDEN:
        assert xxhash.xxh3_64_intdigest(b'\x01' + key.encode()) == digest, f'{key!r}: the xxhash binding'
    for column, count in enumerate(fd_room):
        expected = [shards[column] for _, _, shards in GOLDEN]
        assert _shards_of(tmp_path, count, keys) == expected, f'{count} shards'
    _skip_if_short(fd_room)


def test_shard_of_matches_the_xxhash_binding_over_many_keys(tmp_path: Path, fd_room: tuple[int, ...]) -> None:
    rng = random.Random(13)
    alphabet = 'abc\0é\u4e2d\U0001f40d'
    keys = [''.join(rng.choice(alphabet) for _ in range(n)) for n in range(0, 2049, 7)]
    keys += [f'user:{i}' for i in range(500)]
    for count in fd_room:
        assert _shards_of(tmp_path, count, keys) == [_expected_shard(k, count) for k in keys], f'{count} shards'
    _skip_if_short(fd_room)


def test_a_shared_prefix_still_spreads_across_eight_shards(tmp_path: Path) -> None:
    counts = [0] * 8
    for shard in _shards_of(tmp_path, 8, ['p' * 60 + str(i) for i in range(1000)]):
        counts[shard] += 1
    assert counts == [109, 126, 126, 126, 127, 129, 114, 143]


def test_shard_of_takes_every_key_type_and_refuses_others(tmp_path: Path) -> None:
    with FanoutCache(tmp_path, shards=8) as cache:
        for key in (b'a', 1, None, 2.5, True, ('m.f', 1, None)):
            shard = cache.shard_of(key)
            assert 0 <= shard < 8
            cache.set(key, 'v')
            assert cache.get(key) == 'v'
        for key in ([1], frozenset(), bytearray(b'a')):
            with pytest.raises(TypeError, match='cache keys must be str, bytes, int'):
                cache.shard_of(key)  # ty: ignore[invalid-argument-type]


def test_keys_land_in_the_shard_shard_of_names(tmp_path: Path) -> None:
    cache = FanoutCache(tmp_path, shards=4)
    for i in range(40):
        cache.set(f'k{i}', i)
    cache.close()
    # Each shard is an ordinary database underneath; list its keys from another process.
    script = (
        'import sqlite3, sys\n'
        'for d in sys.argv[1:]:\n'
        '    con = sqlite3.connect(d + "/diskcache-oxide.sqlite")\n'
        '    print(",".join(k for (k,) in con.execute("SELECT key FROM cache ORDER BY key")))\n'
        '    con.close()'
    )
    dirs = [str(tmp_path / f'{i:03d}') for i in range(4)]
    out = subprocess.run([sys.executable, '-c', script, *dirs], check=True, capture_output=True, text=True)
    stored = [set(filter(None, line.split(','))) for line in out.stdout.splitlines()]
    assert len(stored) == 4
    for shard, keys in enumerate(stored):
        assert all(cache.shard_of(k) == shard for k in keys), shard
    assert set().union(*stored) == {f'k{i}' for i in range(40)}


# -- layout and settings ---------------------------------------------------------------------------


def test_a_new_root_has_a_manifest_and_eight_shard_directories(tmp_path: Path) -> None:
    root = tmp_path / 'root'
    with FanoutCache(root) as cache:
        assert cache.shards == 8
        assert cache.directory == str(root)
        # The path is quoted with its backslashes escaped (Windows), so the repr is a valid literal.
        quoted = str(root).replace('\\', '\\\\')
        assert repr(cache) == f'FanoutCache("{quoted}", shards=8)'
    assert sorted(p.name for p in root.iterdir()) == [f'{i:03d}' for i in range(8)] + [MANIFEST]
    assert (root / MANIFEST).read_text() == (
        'diskcache-oxide fanout\nformat = 1\nshards = 8\nhash = xxh3-64 seed=0 range=mulhi\n'
    )
    assert all((root / f'{i:03d}' / 'diskcache-oxide.sqlite').exists() for i in range(8))


def test_the_stored_count_wins_and_another_is_a_warning(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    FanoutCache(tmp_path, shards=4).close()
    with FanoutCache(tmp_path) as cache:
        assert cache.shards == 4
    with caplog.at_level(logging.WARNING, logger='diskcache_oxide'):
        for _ in range(3):
            with FanoutCache(tmp_path, shards=8) as cache:
                assert cache.shards == 4, 'the stored count, not the one asked for'
    warned = [r for r in caplog.records if r.name == 'diskcache_oxide' and r.levelno == logging.WARNING]
    assert len(warned) == 1, 'once per process per root and pair of counts'
    assert 'has 4 shards but shards=8 was asked for' in warned[0].getMessage()
    assert 'raise_on_mismatch=True' in warned[0].getMessage()
    extra = tuple(getattr(warned[0], k) for k in ('stored_shards', 'requested_shards'))
    assert extra == (4, 8)


def test_raise_on_mismatch_refuses_another_count(tmp_path: Path) -> None:
    FanoutCache(tmp_path, shards=4).close()
    with pytest.raises(LayoutMismatchError, match=r'has 4 shards but shards=8'):
        FanoutCache(tmp_path, shards=8, raise_on_mismatch=True)
    with FanoutCache(tmp_path, shards=4, raise_on_mismatch=True) as cache:
        assert cache.shards == 4, 'the same count opens as usual'
        copy = pickle.loads(pickle.dumps(cache))
    try:
        assert copy.shards == 4
    finally:
        copy.close()


def test_an_old_format_root_redirects_whatever_its_count(tmp_path: Path) -> None:
    # Plan 26: the count belongs to a cache this open won't use, so even raise_on_mismatch=True
    # redirects a root in another on-disk format rather than raising for its count.
    FanoutCache(tmp_path, shards=4).close()
    script = (
        'import sqlite3, sys\n'
        'for path in sys.argv[1:]:\n'
        '    con = sqlite3.connect(path)\n'
        '    con.execute("PRAGMA user_version = 2")\n'
        '    con.close()\n'
    )
    shards = [str(tmp_path / f'{i:03d}' / 'diskcache-oxide.sqlite') for i in range(4)]
    subprocess.run([sys.executable, '-c', script, *shards], check=True)
    with FanoutCache(tmp_path, shards=8, raise_on_mismatch=True) as cache:
        assert Path(cache.directory) == tmp_path / 'diskcache-oxide-format-3'
        assert cache.shards == 8


@pytest.mark.parametrize('shards', [0, -1, 257, 1 << 40])
def test_a_shard_count_out_of_range_is_refused(tmp_path: Path, shards: int) -> None:
    with pytest.raises(ValueError, match='shards must be between 1 and 256'):
        FanoutCache(tmp_path, shards=shards)
    assert not (tmp_path / MANIFEST).exists()


@pytest.mark.parametrize('shards', [True, 2.0, '8'])
def test_a_shard_count_must_be_an_int(tmp_path: Path, shards: Any) -> None:
    with pytest.raises(TypeError, match='shards must be an int'):
        FanoutCache(tmp_path, shards=shards)


def test_the_default_size_limit_is_a_gibibyte_in_all(tmp_path: Path) -> None:
    with FanoutCache(tmp_path) as cache:
        assert cache.size_limit == GiB


def test_an_explicit_size_limit_is_split_floored_and_kept_on_reopen(tmp_path: Path) -> None:
    with FanoutCache(tmp_path, shards=8, size_limit=80 * MiB) as cache:
        assert cache.size_limit == 80 * MiB
    with FanoutCache(tmp_path) as cache:
        assert cache.size_limit == 80 * MiB, 'a reopen without size_limit keeps what each shard stores'
    with FanoutCache(tmp_path / 'odd', shards=8, size_limit=9 * MiB + 7) as cache:
        assert cache.size_limit == 8 * ((9 * MiB + 7) // 8)


def test_cull_limit_is_every_shards_own_and_kept_on_reopen(tmp_path: Path) -> None:
    with FanoutCache(tmp_path, shards=4) as cache:
        assert cache.cull_limit == 10
    with FanoutCache(tmp_path, cull_limit=3) as cache:
        assert cache.cull_limit == 3, 'not divided between the shards'
    with FanoutCache(tmp_path) as cache:
        assert cache.cull_limit == 3


def test_an_empty_fanout_cache_is_enabled_and_truthy(tmp_path: Path) -> None:
    # D-009: a cache is usable even when empty.
    with FanoutCache(tmp_path, shards=2) as cache:
        assert len(cache) == 0
        assert cache.enabled is True
        assert bool(cache) is True


def test_a_size_limit_under_a_mebibyte_per_shard_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match=r'at least 8388608 bytes for 8 shards'):
        FanoutCache(tmp_path, shards=8, size_limit=8 * MiB - 1)


def test_max_connections_must_cover_every_shard(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match=r'at least the shard count \(16\), got 8'):
        FanoutCache(tmp_path, shards=16, max_connections=8)
    # With none given, more than 8 shards get one each.
    with FanoutCache(tmp_path, shards=16) as cache:
        assert cache.shards == 16
        assert cache.set('k', 1) is True


# -- refusing each other's directories -------------------------------------------------------------


def test_a_plain_cache_on_the_root_raises_and_creates_no_database(tmp_path: Path) -> None:
    FanoutCache(tmp_path).close()
    with pytest.raises(ValueError, match='FanoutCache root'):
        Cache(tmp_path)
    assert not (tmp_path / 'diskcache-oxide.sqlite').exists()


def test_a_fanout_cache_on_a_plain_cache_raises_and_writes_no_manifest(tmp_path: Path) -> None:
    Cache(tmp_path).close()
    with pytest.raises(ValueError, match='holds a plain Cache'):
        FanoutCache(tmp_path)
    assert not (tmp_path / MANIFEST).exists()


def test_a_plain_cache_on_a_live_shard_raises_and_leaves_the_count_alone(tmp_path: Path) -> None:
    with FanoutCache(tmp_path, shards=2) as cache:
        before = cache._handle_counts()
        with pytest.raises(ValueError, match='shard 1 of 2 of a FanoutCache'):
            Cache(tmp_path / '001')
        assert cache._handle_counts() == before


@unix_only('RLIMIT_NOFILE; Windows has no per-process handle limit')
def test_too_few_file_descriptors_for_every_shard_names_the_numbers(tmp_path: Path) -> None:
    # A fresh process, so the lowered limit and the fd count are this test's alone.
    script = """
import os, resource, sys
from diskcache_oxide import FanoutCache
soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
resource.setrlimit(resource.RLIMIT_NOFILE, (256, hard))
fds = lambda: len(os.listdir('/proc/self/fd' if sys.platform == 'linux' else '/dev/fd'))
before = fds()
try:
    FanoutCache(sys.argv[1], shards=256)
except OSError as e:
    print(e.errno, e, sep='\\n')
    print(type(e.__cause__).__name__)
else:
    raise SystemExit('opened 256 shards with 256 file descriptors')
print(before, fds())
"""
    out = subprocess.run(
        [sys.executable, '-c', script, str(tmp_path / 'root')], check=True, capture_output=True, text=True
    ).stdout.splitlines()
    errno, message, cause, counts = out
    assert int(errno) == 24, out  # EMFILE
    assert 'with 256 shards' in message and 'only 256 open' in message and 'ulimit -n' in message, message
    assert cause == 'OSError', 'the SQLite error is kept as the cause'
    before, after = map(int, counts.split())
    assert after == before, f'the failed open left {after - before} fds open'


FD_THREADS = 64


@unix_only('counts file descriptors; Windows has handles')
@pytest.mark.parametrize(('shards', 'max_connections'), [(8, None), (4, 16), (16, None), (8, 24)])
def test_many_threads_stay_within_the_fd_budget_and_close_leaves_none(
    tmp_path: Path, shards: int, max_connections: int | None
) -> None:
    """D-017's budget, once per shard (D-037): each pooled connection holds its shard's database and
    WAL, and each shard adds its `-shm` and its pin, so the root never holds more than
    `2 × max_connections + 2 × shards` fds, however many threads use it, and none after `close()`."""
    root = tmp_path / 'root'
    cap = max_connections if max_connections is not None else max(8, 2 * shards)  # two per shard (D-037)
    budget = 2 * cap + 2 * shards
    assert fds_inside(tmp_path) == 0
    cache = FanoutCache(root, shards=shards, max_connections=max_connections)
    opened = fds_inside(root)
    assert 0 < opened <= budget, f'{opened} fds after opening, budget {budget}'

    ready = threading.Barrier(FD_THREADS + 1)
    release = threading.Event()
    working = threading.Event()
    errors: list[BaseException] = []
    peak = [opened]

    def body(tid: int) -> None:
        try:
            for i in range(20):
                assert cache.set(f't{tid}-{i}', i) is True
                cache.get(f't{(tid + 1) % FD_THREADS}-{i}')
            ready.wait(30)
            release.wait(30)
        except BaseException as ex:
            errors.append(ex)
            ready.abort()

    def sampler() -> None:
        while working.is_set():
            peak[0] = max(peak[0], fds_inside(root))

    gc_was_enabled = gc.isenabled()
    gc.disable()  # A gc pass would hide a connection that close() left open.
    try:
        working.set()
        watcher = threading.Thread(target=sampler)
        watcher.start()
        workers = [threading.Thread(target=body, args=(tid,)) for tid in range(FD_THREADS)]
        for t in workers:
            t.start()
        ready.wait(30)
        held = fds_inside(root)
        working.clear()
        watcher.join(30)
        release.set()
        for t in workers:
            t.join(30)
        if errors:
            raise errors[0]
        assert len(cache) == FD_THREADS * 20
        cache.close()
        after = fds_inside(root)
    finally:
        if gc_was_enabled:
            gc.enable()
    assert max(peak[0], held) <= budget, f'peak {max(peak[0], held)} fds with {FD_THREADS} threads, budget {budget}'
    assert after == 0, f'{after} fds still open inside the root after close()'


@unix_only('counts file descriptors; Windows has handles')
def test_a_closed_handle_used_again_beside_a_live_one_adds_no_fd_on_any_shard(tmp_path: Path) -> None:
    """The plain-Cache case of tests/conformance/test_threads.py, once per shard: a closed FanoutCache
    used again rejoins each shard's store, and beside a live handle that must open nothing. Before the
    fix, each rejoin left one bare fd on the shard's database file (SQLite defers the close of a
    descriptor while another connection in the process holds a lock on the file), so a FanoutCache
    reopened while another thread iterated it went one over D-037's budget until the next fork."""
    root = tmp_path / 'root'
    live = FanoutCache(root, shards=8)
    keys = [f'k{i}' for i in range(64)]
    for key in keys:
        live.set(key, 1)
    other = FanoutCache(root, shards=8)
    for key in keys:
        assert other.get(key) == 1
    other.close()
    before = fds_inside(root)
    for key in keys:
        assert other.get(key) == 1  # Rejoins every shard.
    assert fds_inside(root) == before, 'a rejoin left an fd open on a shard database'
    other.close()
    live.close()
    assert fds_inside(root) == 0


@unix_only('counts file descriptors; Windows has handles')
def test_reopening_under_a_running_iteration_stays_within_the_fd_budget(tmp_path: Path) -> None:
    """The soak's shape (soak/worker.py, `reopen_fan` beside `walk`): one thread iterates the keys
    while another opens a new handle on the root and closes the old one, over and over. The
    iteration's later pages find their shards' handles closed and rejoin, which must leave the fds
    inside the root within `2 × max_connections + 2 × shards` (D-037) at every sample. One connection
    per shard, so the iteration alone fills the budget and one descriptor left behind shows."""
    root = tmp_path / 'root'
    shards = 8
    budget = 2 * shards + 2 * shards
    holder = [FanoutCache(root, shards=shards, max_connections=shards)]
    for i in range(200):
        holder[0].set(f'k{i}', i)
    stop = threading.Event()
    errors: list[BaseException] = []

    def iterate() -> None:
        try:
            while not stop.is_set():
                assert len(list(holder[0].iterkeys())) == 200
        except BaseException as ex:
            errors.append(ex)

    worst = 0
    opened = [holder[0]]
    thread = threading.Thread(target=iterate)
    thread.start()
    try:
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            old = holder[0]
            holder[0] = FanoutCache(root, shards=shards, max_connections=shards)
            opened.append(holder[0])
            old.close()
            worst = max(worst, fds_inside(root))
    finally:
        stop.set()
        thread.join(30)
    if errors:
        raise errors[0]
    assert worst <= budget, f'{worst} fds inside the root at a sample, budget {budget}'
    # An iteration that outlived its handle's close() rejoined it, so that handle is open again
    # until it is closed or collected: close every handle this test opened, then nothing is left.
    for cache in opened:
        cache.close()
    assert fds_inside(root) == 0


# -- maintenance over every shard -------------------------------------------------------------------

DB = 'diskcache-oxide.sqlite'

REMOVALS: dict[str, Callable[..., int]] = {
    'clear': lambda cache, **kw: cache.clear(**kw),
    'expire': lambda cache, **kw: cache.expire(**kw),
    'cull': lambda cache, **kw: cache.cull(**kw),
}


@contextmanager
def _locked(root: Path, shards: list[int], mode: str = 'IMMEDIATE') -> Generator[None]:
    """Each of `shards` locked by a SQLite connection in its own process (never this one, whose bundled
    SQLite and the stdlib's would break each other's POSIX locks): `IMMEDIATE` holds the write lock,
    `READ` a read snapshot that keeps the WAL from being reset."""
    with ExitStack() as stack:
        for shard in shards:
            peer = stack.enter_context(SqlitePeer(root / f'{shard:03d}' / DB))
            peer.begin(mode)
            stack.callback(peer.rollback)
        yield


def _stored(root: Path, shards: int = 8) -> int:
    """Rows on disk in every shard, expired ones included: `len()` hides expired rows (D-060)."""
    return stored_rows(*(root / f'{shard:03d}' / DB for shard in range(shards)))


def _expired_cache(root: Path, keys: list[str], shards: int = 8) -> tuple[FanoutCache, list[int]]:
    """A cache holding `keys`, every one already expired (so `clear`, `expire` and `cull` each remove
    them all), and how many of them each shard holds."""
    now = [1_000.0]
    cache = FanoutCache(root, shards=shards, timeout=10, clock=lambda: now[0])
    for key in keys:
        cache.set(key, 'v', expire=1)
    now[0] += 10
    counts = [0] * shards
    for key in keys:
        counts[cache.shard_of(key)] += 1
    return cache, counts


@pytest.mark.parametrize('op', REMOVALS)
def test_a_locked_shard_fails_alone_and_the_error_says_how_far_the_call_got(tmp_path: Path, op: str) -> None:
    keys = [f'k{i}' for i in range(400)]
    cache, counts = _expired_cache(tmp_path, keys)
    with _locked(tmp_path, [3]):
        with pytest.raises(Timeout) as raised:
            REMOVALS[op](cache, timeout=0.2)
    error: Any = raised.value
    assert error.failed_shards == (3,)
    assert error.removed == len(keys) - counts[3], 'every other shard was still done'
    assert f'FanoutCache.{op}() failed on shard 3 of 8; it removed {error.removed} entries in all' in error.__notes__
    assert 'after 0.2 s' in str(error), 'the timeout names the whole call budget'
    assert len(cache) == 0, 'every entry has expired'
    assert _stored(tmp_path) == counts[3]
    assert REMOVALS[op](cache) == counts[3], 'unlocked, the shard is done'
    cache.close()


def test_a_write_to_a_later_shard_while_clear_waits_on_an_earlier_one_survives(tmp_path: Path) -> None:
    # Every shard's snapshot is taken before anything is removed, so a write made while clear() waits on
    # shard 0 is spared on shard 5 too, as a write made during Cache.clear() is.
    keys = [f'k{i}' for i in range(400)]
    cache = FanoutCache(tmp_path, shards=8, timeout=10)
    counts = [0] * 8
    for key in keys:  # None expired, so the late set sweeps nothing from its shard.
        cache.set(key, 'v')
        counts[cache.shard_of(key)] += 1
    late = next(f'late-{i}' for i in range(10_000) if cache.shard_of(f'late-{i}') == 5)
    outcome: list[BaseException] = []

    def clear() -> None:
        try:
            cache.clear(timeout=0.3)
        except BaseException as error:  # noqa: BLE001 - handed to the test thread
            outcome.append(error)

    with _locked(tmp_path, [0]):
        worker = threading.Thread(target=clear)
        worker.start()
        time.sleep(0.1)  # clear() has its snapshots and is waiting on shard 0.
        assert worker.is_alive()
        cache.set(late, 'written during the clear')
        worker.join(10)
    assert not worker.is_alive()
    assert len(outcome) == 1 and isinstance(outcome[0], Timeout), outcome
    error: Any = outcome[0]
    assert error.failed_shards == (0,)
    assert error.removed == len(keys) - counts[0], 'the late write was not counted'
    assert cache.get(late) == 'written during the clear'
    cache.close()


def test_one_timeout_bounds_the_whole_call_not_one_per_shard(tmp_path: Path) -> None:
    keys = [f'k{i}' for i in range(400)]
    cache, counts = _expired_cache(tmp_path, keys)
    timeout = 0.25
    with _locked(tmp_path, [0, 2, 4, 6]):
        start = time.monotonic()
        with pytest.raises(Timeout) as raised:
            cache.clear(timeout=timeout)
        took = time.monotonic() - start
    error: Any = raised.value
    assert error.failed_shards == (0, 2, 4, 6)
    assert error.removed == sum(counts[1::2])
    assert timeout <= took < 3 * timeout, f'{took:.3f} s: four locked shards waited about one timeout, not four'
    cache.close()


def test_at_a_zero_timeout_every_idle_shard_is_still_emptied(tmp_path: Path) -> None:
    # Over 100 rows (one chunk) per shard, so every chunk past the first also gets its no-wait try.
    keys = [f'k{i}' for i in range(2_000)]
    cache, _ = _expired_cache(tmp_path, keys)
    cache.set('live', 'v')  # Its set sweeps a few expired rows from its shard, as any set does.
    assert len(cache) == 1
    expired = _stored(tmp_path) - 1
    assert expired > len(keys) - 20
    assert cache.expire(timeout=0) == expired
    assert cache.cull(timeout=0) == 0
    assert cache.clear(timeout=0) == 1
    assert len(cache) == 0
    assert cache.checkpoint(timeout=0) is True
    cache.close()


def _unopenable(root: Path, shard: int) -> None:
    """Replaces a closed shard's database with bytes SQLite won't open ("file is not a database")."""
    for p in (root / f'{shard:03d}').glob(f'{DB}*'):
        p.unlink()
    (root / f'{shard:03d}' / DB).write_bytes(b'not a database' * 300)


def test_a_checkpoint_that_fails_on_one_shard_names_it(tmp_path: Path) -> None:
    cache = FanoutCache(tmp_path, shards=8)
    cache.set('k', 'v')
    cache.close()
    _unopenable(tmp_path, 3)
    with pytest.raises(OSError, match='not a database') as raised:
        cache.checkpoint(timeout=1)
    error: Any = raised.value
    assert error.failed_shards == (3,)
    assert not hasattr(error, 'removed'), 'checkpoint removes nothing'
    assert 'FanoutCache.checkpoint() failed on shard 3 of 8' in error.__notes__


def test_a_signal_after_a_shard_failed_carries_the_partial_failure(tmp_path: Path) -> None:
    # Shard 0 can't open; shard 1's write lock holds clear() there past the signal, so the
    # KeyboardInterrupt is pending when clear() checks for signals before shard 2. SIGUSR1 from a
    # timer thread, since pytest-timeout owns SIGALRM.
    cache = FanoutCache(tmp_path, shards=4)
    keys = [f'k{i}' for i in range(200)]
    for key in keys:
        cache.set(key, 'v')
    later = [key for key in keys if cache.shard_of(key) >= 2]
    cache.close()
    _unopenable(tmp_path, 0)

    def interrupt(*_: object) -> None:
        raise KeyboardInterrupt

    # Windows has no SIGUSR1 and its os.kill terminates; there, SIGINT raised from the timer thread.
    interrupt_signal = signal.SIGINT if sys.platform == 'win32' else signal.SIGUSR1
    previous = signal.signal(interrupt_signal, interrupt)
    if sys.platform == 'win32':
        sender = threading.Timer(0.1, signal.raise_signal, (interrupt_signal,))
    else:
        sender = threading.Timer(0.1, os.kill, (os.getpid(), interrupt_signal))
    try:
        with _locked(tmp_path, [1]):
            sender.start()
            with pytest.raises(KeyboardInterrupt) as raised:
                cache.clear(timeout=0.6)
    finally:
        sender.cancel()
        sender.join()
        signal.signal(interrupt_signal, previous)
    error: Any = raised.value
    assert error.failed_shards == (0, 1)
    assert error.removed == 0
    assert 'FanoutCache.clear() failed on shards 0, 1 of 4; it removed 0 entries in all' in error.__notes__
    # Keyed reads open only their own shard, so the unopenable shard 0 is never touched here.
    assert all(key in cache for key in later), 'the shards after the interrupt were never cleared'
    cache.close()


def test_maintenance_on_a_closed_handle_reopens_every_shard(tmp_path: Path) -> None:
    keys = [f'k{i}' for i in range(100)]
    cache, _ = _expired_cache(tmp_path, keys, shards=4)
    cache.close()
    assert cache.clear() == len(keys)
    assert cache._handle_counts() == [1, 1, 1, 1]
    cache.close()
    assert cache.checkpoint() is True
    assert cache._handle_counts() == [1, 1, 1, 1]
    cache.close()


def test_checkpoint_is_true_only_when_every_shard_resets(tmp_path: Path) -> None:
    cache = FanoutCache(tmp_path, shards=4, timeout=10)
    keys = [f'k{i}' for i in range(200)]
    for key in keys:
        cache.set(key, 'v')
    assert cache.checkpoint() is True
    with _locked(tmp_path, [2], 'READ'):
        for key in keys:
            cache.set(key, 'w')  # WAL frames past the reader's snapshot on every shard.
        start = time.monotonic()
        assert cache.checkpoint(timeout=0.2) is False, 'a reader kept shard 2 busy; busy never raises'
        took = time.monotonic() - start
    assert took < 2.0, f'{took:.3f} s'
    assert cache.checkpoint() is True
    cache.close()


# -- iteration ---------------------------------------------------------------------------------------

# Keys whose sorted order a merge could get wrong: non-ASCII at every UTF-8 width (byte order on UTF-8
# is code-point order), a key that is a prefix of another, NUL, the largest code point, shared
# prefixes, and enough of them that every shard needs several merge pages.
ODD_KEYS = [
    '',
    'a',
    'a\0',
    'ab',
    'a\x7f',
    'a\x80',
    'aé',
    'a\u07ff',
    'a\u0800',
    'a中',
    'a\uffff',
    'a\U00010000',
    'a\U0001f40d',
    'a\U0010ffff',
    'b',
    'Z',
    '\U0010ffff',
    'é',
    'éa',
]
MANY_KEYS = ODD_KEYS + [f'user:{i:05}' for i in range(1_500)] + [f'usé:{i}' for i in range(300)]


def _filled_cache(root: Path, keys: list[str], shards: int = 8, **kwargs: Any) -> FanoutCache:
    cache = FanoutCache(root, shards=shards, **kwargs)
    for key in keys:
        cache.set(key, 1)
    return cache


def test_iterkeys_merges_every_shard_into_the_order_sorted_and_a_plain_cache_give(tmp_path: Path) -> None:
    keys = list(MANY_KEYS)
    random.Random(13).shuffle(keys)
    cache = _filled_cache(tmp_path / 'fanout', keys)
    plain = Cache(tmp_path / 'plain')
    for key in keys:
        plain.set(key, 1)
    assert len({cache.shard_of(k) for k in keys}) == 8
    for prefix in (None, '', 'a', 'user:', 'user:01', 'usé', 'é', 'a\U00010000', '\U0010ffff', 'nothing'):
        want = sorted(k for k in keys if k.startswith(prefix or ''))
        for reverse in (False, True):
            got = list(cache.iterkeys(prefix=prefix, reverse=reverse))
            assert got == (want[::-1] if reverse else want), (prefix, reverse)
            assert got == list(plain.iterkeys(prefix=prefix, reverse=reverse)), (prefix, reverse)
    plain.close()
    cache.close()


def test_iter_yields_every_key_once_shard_by_shard_each_in_insertion_order(tmp_path: Path) -> None:
    keys = [f'k{i}' for i in range(2_000)]
    cache = _filled_cache(tmp_path, keys)
    cache.set('k0', 2)  # An overwrite keeps its place, as on `Cache`.
    got = str_keys(cache)
    assert sorted(got) == sorted(keys)
    shards = [cache.shard_of(k) for k in got]
    assert shards == sorted(shards), 'shard 0 first, then shard 1, ...'
    for shard in range(8):
        assert [k for k in got if cache.shard_of(k) == shard] == [k for k in keys if cache.shard_of(k) == shard]
    cache.close()


@pytest.mark.parametrize('shards', [1, 64])
def test_iteration_over_one_shard_and_over_many_with_few_keys(tmp_path: Path, shards: int) -> None:
    cache = _filled_cache(tmp_path, ['c', 'a', 'b'], shards=shards)
    assert list(cache.iterkeys()) == ['a', 'b', 'c']
    assert list(cache.iterkeys(reverse=True)) == ['c', 'b', 'a']
    assert sorted(str_keys(cache)) == ['a', 'b', 'c']
    cache.clear()
    assert list(cache.iterkeys()) == [] and list(cache) == []
    cache.close()


def test_an_iterator_holds_no_connection_or_lock_between_yields(tmp_path: Path) -> None:
    # One connection per shard: had either iterator kept its shard's, the reads and writes below
    # would time out waiting for it, and a read transaction left open would keep the WAL from resetting.
    keys = [f'k{i}' for i in range(1_000)]
    cache = _filled_cache(tmp_path, keys, shards=4, max_connections=4, timeout=0.5)
    merged, walked = cache.iterkeys(), iter(cache)
    assert next(merged) == sorted(keys)[0]
    next(walked)
    for key in keys[:200]:
        assert cache.get(key) == 1
        cache.set(key, 2)
    assert cache.checkpoint() is True
    assert len(list(merged)) == len(keys) - 1
    assert len(list(walked)) == len(keys) - 1
    cache.close()


def test_iterating_while_writing_never_repeats_or_reorders_a_key(tmp_path: Path) -> None:
    # Keyset pages, as on `Cache`: a key stored or deleted mid-walk may or may not be seen, but no key
    # comes out twice, the merge stays sorted, and every key left alone throughout is seen.
    keys = [f'k{i:05}' for i in range(3_000)]
    cache = _filled_cache(tmp_path, keys)
    rng = random.Random(5)
    deleted = set(rng.sample(keys, 500))
    untouched = set(keys) - deleted
    for walk in ('merge', 'reverse', 'shards'):
        cache.clear()
        for key in keys:
            cache.set(key, 1)
        iterator = {'merge': cache.iterkeys(), 'reverse': cache.iterkeys(reverse=True), 'shards': iter(cache)}[walk]
        seen: list[str] = []
        doomed = sorted(deleted)
        for i, key in enumerate(iterator):
            assert isinstance(key, str)
            seen.append(key)
            if i % 10 == 0 and doomed:
                cache.delete(doomed.pop(rng.randrange(len(doomed))))
            if i % 7 == 0:
                cache.set(f'k{rng.randrange(6_000):05}x', 1)  # Ahead of or behind the walk.
        assert len(seen) == len(set(seen)), f'{walk}: a key came out twice'
        assert untouched <= set(seen), f'{walk}: a key left alone was missed'
        if walk != 'shards':
            assert seen == sorted(seen, reverse=walk == 'reverse'), f'{walk}: out of order'
    cache.close()


def test_iterating_a_closed_handle_reopens_the_shards_it_reads(tmp_path: Path) -> None:
    cache = _filled_cache(tmp_path, ['a', 'b', 'c', 'd'], shards=4)
    cache.close()
    assert list(cache.iterkeys()) == ['a', 'b', 'c', 'd']
    assert cache._handle_counts() == [1, 1, 1, 1]
    cache.close()


class _Stall:
    """Unpickling it blocks until `release` is set, inside the read, which holds its connection
    meanwhile (decoding runs with the row still borrowed from SQLite)."""

    entered = threading.Event()
    release = threading.Event()

    def __init__(self) -> None:
        self.n = 1  # Some state, so unpickling calls __setstate__.

    def __setstate__(self, state: dict[str, Any]) -> None:
        self.__dict__.update(state)
        type(self).entered.set()
        type(self).release.wait(10)


@contextmanager
def _stalled_shard(cache: FanoutCache, shard: int) -> Generator[None]:
    """`shard`'s only connection held by another thread until `_Stall.release` is set: a `get` of a
    value whose unpickling blocks. Needs a `pickle=True` cache with one connection per shard."""
    _Stall.entered.clear()
    _Stall.release.clear()
    stall = next(k for k in (f'stall-{i}' for i in range(10_000)) if cache.shard_of(k) == shard)
    cache.set(stall, _Stall())
    holder = threading.Thread(target=cache.get, args=(stall,))
    holder.start()
    assert _Stall.entered.wait(10), 'the unpickling never started'
    try:
        yield
    finally:
        _Stall.release.set()
        holder.join(10)


def test_a_merge_read_that_fails_loses_no_key_and_the_same_iterator_carries_on(tmp_path: Path) -> None:
    # A shard's refill happens at the start of the next `next()`, before a key is chosen: a refill
    # that times out raises with nothing taken, and the same iterator then picks up where it was.
    cache = FanoutCache(tmp_path, shards=2, max_connections=2, timeout=0.3, pickle=True)
    keys = [f'k{i:04}' for i in range(1_500)]
    for key in keys:
        cache.set(key, 1)
    iterator = cache.iterkeys()
    seen = [next(iterator) for _ in range(10)]  # Every shard's first page is buffered.
    with _stalled_shard(cache, 1):
        with pytest.raises(Timeout):
            while True:  # Until shard 1's page runs out and its refill can't get a connection.
                seen.append(next(iterator))
    seen.extend(iterator)
    assert seen == sorted(str_keys(cache.iterkeys())), 'a key was lost or repeated across the failed next()'
    assert len(seen) == len(keys) + 1
    cache.close()


def test_a_second_thread_inside_next_raises_instead_of_waiting(tmp_path: Path) -> None:
    cache = FanoutCache(tmp_path, shards=2, max_connections=2, timeout=10, pickle=True)
    for key in ('a', 'b', 'c', 'd'):
        cache.set(key, 1)
    iterator = cache.iterkeys()
    first: list[Key] = []
    with _stalled_shard(cache, 1):
        inside = threading.Thread(target=lambda: first.append(next(iterator)))
        inside.start()  # Waits inside next() for shard 1's connection.
        time.sleep(0.2)
        assert inside.is_alive()
        with pytest.raises(ValueError, match='iterator already executing'):
            next(iterator)
    inside.join(10)
    assert first == ['a']
    assert list(iterator) == [k for k in sorted(str_keys(cache.iterkeys())) if k != 'a']
    cache.close()


@pytest.mark.parametrize('walk', ['iterkeys', 'iter'])
def test_an_iterator_in_a_reference_cycle_is_collected_and_releases_its_handle(tmp_path: Path, walk: str) -> None:
    keeper = FanoutCache(tmp_path, shards=4)

    def make() -> None:
        box: dict[str, Any] = {}
        cache = FanoutCache(tmp_path, clock=lambda: (box, time.time())[1])
        box['iterator'] = cache.iterkeys() if walk == 'iterkeys' else iter(cache)  # Iterator -> cache -> clock -> box.

    make()
    assert keeper._handle_counts() == [2, 2, 2, 2], 'the cycle keeps the second handle open'
    gc.collect()
    assert keeper._handle_counts() == [1, 1, 1, 1], 'collecting the cycle released it'
    keeper.close()


def test_an_exhausted_iterator_stays_exhausted(tmp_path: Path) -> None:
    cache = _filled_cache(tmp_path, ['a'], shards=2)
    iterator = cache.iterkeys()
    assert list(iterator) == ['a']
    assert next(iterator, None) is None, 'an exhausted iterator stays exhausted'
    cache.close()


# -- lifecycle -------------------------------------------------------------------------------------


def test_close_releases_every_shard_and_a_later_call_reopens_only_its_own(tmp_path: Path) -> None:
    cache = FanoutCache(tmp_path, shards=4)
    assert cache._handle_counts() == [1, 1, 1, 1]
    cache.set('k', 'v')
    cache.close()
    assert cache._handle_counts() == [0, 0, 0, 0]
    cache.close()  # Harmless twice.
    assert cache.get('k') == 'v'
    counts = [0, 0, 0, 0]
    counts[cache.shard_of('k')] = 1
    assert cache._handle_counts() == counts
    assert len(cache) == 1, 'len() reopens every shard'
    assert cache._handle_counts() == [1, 1, 1, 1]
    cache.close()
    assert cache._handle_counts() == [0, 0, 0, 0]


def test_two_handles_share_each_shards_store(tmp_path: Path) -> None:
    a = FanoutCache(tmp_path, shards=4)
    b = FanoutCache(tmp_path)
    assert a._handle_counts() == [2, 2, 2, 2]
    b.close()
    assert a._handle_counts() == [1, 1, 1, 1]
    del b
    a.close()


def test_dropping_an_open_cache_releases_every_shard(tmp_path: Path) -> None:
    keeper = FanoutCache(tmp_path, shards=4)
    dropped = FanoutCache(tmp_path)
    assert keeper._handle_counts() == [2, 2, 2, 2]
    del dropped
    assert keeper._handle_counts() == [1, 1, 1, 1]
    keeper.close()


def _sql(db: Path, statement: str) -> None:
    script = 'import sqlite3, sys\ncon = sqlite3.connect(sys.argv[1])\ncon.execute(sys.argv[2])\ncon.commit()'
    subprocess.run([sys.executable, '-c', script, str(db), statement], check=True)


def test_a_bad_value_on_each_shard_is_reported_once_per_shard(tmp_path: Path) -> None:
    reports: list[str] = []
    cache = FanoutCache(tmp_path, shards=2, on_decode_error=lambda key, exc: reports.append(key))
    # The first row of each fresh shard: both get the same row id and sequence, so the same Version.
    keys = {cache.shard_of(k): k for k in (f'k{i}' for i in range(20))}
    assert sorted(keys) == [0, 1]
    for shard, key in keys.items():
        cache.set(key, 'v')
        # An unknown mode is foreign: the row stays and is reported once per stored version.
        _sql(tmp_path / f'{shard:03d}' / 'diskcache-oxide.sqlite', f"UPDATE cache SET mode = 99 WHERE key = '{key}'")
    for _ in range(3):
        assert cache.get(keys[0]) is None
        assert cache.get(keys[1]) is None
    assert reports == [keys[0], keys[1]], "one shard's report must not silence the other's"
    cache.close()


# -- racing creators -------------------------------------------------------------------------------

RACERS = 16


def _open_root(root: str, shards: int | None, barrier: Any, results: Any) -> None:
    try:
        barrier.wait(30)
        with FanoutCache(root, shards=shards) as cache:
            cache.set(f'pid-{shards}', 1)
            results.put(('ok', cache.shards))
    except ValueError as e:
        results.put(('ValueError', str(e)))
    except BaseException:
        results.put(('crash', traceback.format_exc()))


def _open_plain(root: str, barrier: Any, results: Any) -> None:
    try:
        barrier.wait(30)
        with Cache(root) as cache:
            cache.set('plain', 1)
            results.put(('ok', 0))
    except ValueError as e:
        results.put(('ValueError', str(e)))
    except BaseException:
        results.put(('crash', traceback.format_exc()))


@pytest.mark.timeout(120)
@pytest.mark.parametrize('same_count', [True, False])
def test_processes_racing_to_create_one_root_agree_on_one_manifest(tmp_path: Path, same_count: bool) -> None:
    root = tmp_path / 'root'
    counts = [4 if same_count else 1 + i % 8 for i in range(RACERS)]
    outcomes = race([(_open_root, (str(root), n)) for n in counts])
    winner = int((root / MANIFEST).read_text().split('shards = ')[1].split('\n')[0])
    assert {n for kind, n in outcomes if kind == 'ok'} == {winner}, outcomes
    for kind, detail in outcomes:
        assert kind == 'ok' or f'has {winner} shards' in detail, detail
    if same_count:
        assert all(kind == 'ok' for kind, _ in outcomes)
    assert sorted(p.name for p in root.iterdir()) == [f'{i:03d}' for i in range(winner)] + [MANIFEST]


@pytest.mark.timeout(120)
def test_a_plain_cache_racing_fanout_caches_for_one_root_never_both_open(tmp_path: Path) -> None:
    for round_ in range(3):
        root = tmp_path / f'root-{round_}'
        outcomes = race([(_open_plain, (str(root),)), *[(_open_root, (str(root), None))] * 3])
        # Results arrive in completion order: a plain Cache reports 0 shards, a FanoutCache 8.
        opened = {detail for kind, detail in outcomes if kind == 'ok'}
        assert opened in ({0}, {8}, set()), f'round {round_}: both kinds opened one root: {outcomes}'
