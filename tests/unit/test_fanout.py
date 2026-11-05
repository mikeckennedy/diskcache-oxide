"""FanoutCache's oxide-only surface (plan 13, step 13d): layout, budgets, `shard_of`, and lifecycle.

The routed calls diskcache shares are in tests/conformance/test_fanout.py. The shard hash is frozen:
the golden table below is the one in `crates/diskcache-oxide-core/src/fanout.rs`, and a changed
vector is a format break, never a test update.
"""

from __future__ import annotations

import multiprocessing
import random
import resource
import subprocess
import sys
import traceback
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import xxhash
from diskcache_oxide import Cache, FanoutCache

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
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    wanted = 4096 if hard == resource.RLIM_INFINITY else min(hard, 4096)
    resource.setrlimit(resource.RLIMIT_NOFILE, (max(soft, wanted), hard))
    yield SHARD_COUNTS if max(soft, wanted) >= FDS_FOR_256 else SHARD_COUNTS[:-1]
    resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard))


def _skip_if_short(counts: tuple[int, ...]) -> None:
    """Called after every count that fits was checked: says so when 256 couldn't be."""
    if counts != SHARD_COUNTS:
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


def test_shard_of_takes_str_keys_only(tmp_path: Path) -> None:
    with FanoutCache(tmp_path) as cache:
        for key in (b'a', 1, None):
            with pytest.raises(TypeError, match='must be str'):
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
        assert repr(cache) == f'FanoutCache("{root}", shards=8)'
    assert sorted(p.name for p in root.iterdir()) == [f'{i:03d}' for i in range(8)] + [MANIFEST]
    assert (root / MANIFEST).read_text() == (
        'diskcache-oxide fanout\nformat = 1\nshards = 8\nhash = xxh3-64 seed=0 range=mulhi\n'
    )
    assert all((root / f'{i:03d}' / 'diskcache-oxide.sqlite').exists() for i in range(8))


def test_the_stored_count_wins_and_another_is_refused(tmp_path: Path) -> None:
    FanoutCache(tmp_path, shards=4).close()
    with FanoutCache(tmp_path) as cache:
        assert cache.shards == 4
    with pytest.raises(ValueError, match=r'has 4 shards but shards=8'):
        FanoutCache(tmp_path, shards=8)


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

SPAWN = multiprocessing.get_context('spawn')
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


def _race(targets: list[tuple[Any, tuple[Any, ...]]]) -> list[tuple[str, Any]]:
    barrier = SPAWN.Barrier(len(targets))
    results = SPAWN.Queue()
    procs = [SPAWN.Process(target=t, args=(*args, barrier, results)) for t, args in targets]
    for p in procs:
        p.start()
    outcomes = [results.get(timeout=60) for _ in procs]
    for p in procs:
        p.join(30)
    assert [p.exitcode for p in procs] == [0] * len(procs)
    assert not [o for o in outcomes if o[0] == 'crash'], outcomes
    return outcomes


@pytest.mark.timeout(120)
@pytest.mark.parametrize('same_count', [True, False])
def test_processes_racing_to_create_one_root_agree_on_one_manifest(tmp_path: Path, same_count: bool) -> None:
    root = tmp_path / 'root'
    counts = [4 if same_count else 1 + i % 8 for i in range(RACERS)]
    outcomes = _race([(_open_root, (str(root), n)) for n in counts])
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
        outcomes = _race([(_open_plain, (str(root),)), *[(_open_root, (str(root), None))] * 3])
        # Results arrive in completion order: a plain Cache reports 0 shards, a FanoutCache 8.
        opened = {detail for kind, detail in outcomes if kind == 'ok'}
        assert opened in ({0}, {8}, set()), f'round {round_}: both kinds opened one root: {outcomes}'
