"""0.1.2 and this build on one cache directory, as during a deploy (plan 24, release 3, decision 22;
plan 26).

Neither version may ever write into the other's database. 0.1.2 refuses a format-3 cache at open and
leaves its files byte for byte as they were. This build meets 0.1.2's format-2 cache by opening its
own in `diskcache-oxide-format-3` (plan 26), leaving the old one byte for byte as it was, and 0.1.2
keeps working on its cache beside it without ever touching the subdirectory (it only walks
two-character hex folders). `remove_old_formats()` removes the old cache, and when a 0.1.2 worker is
still running (which the docs warn against), whatever it does afterwards stays in old-format files at
the root: the subdirectory is untouched, and the next call removes what it left. 0.2.3 needs no wheel
of its own here: it writes format 3, so it only ever opens the root, which is the documented same-
format split once the old cache is gone, not a correctness case. A 0.1.2 handle open across the deletion writes
nothing into the new file either: its open connections still hold the deleted one, and once it opens
the new file by path (here, a closed handle used again) every write and maintenance call it makes
fails with "no such table: totals", because format 3 names that table `state` (architect, release 3's
review, M1). Only `touch` still reaches the new file. Under the old name a 0.1.2 `cull()` evicted a
'none' cache.

The old side runs the shipped 0.1.2 wheel, in a Python of its own named by `DCO_OLD_PYTHON`
(`scripts/mixed-version.sh` builds it); without it the test is skipped. Each side runs in its own
process, and the database is read with the stdlib's sqlite3 in a third.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from diskcache_oxide import Cache

OLD_PYTHON = os.environ.get('DCO_OLD_PYTHON')
SUB = 'diskcache-oxide-format-3'
pytestmark = pytest.mark.skipif(not OLD_PYTHON, reason='set DCO_OLD_PYTHON to a Python with diskcache-oxide 0.1.2')

OLD_SIDE = """
import json, sys
import diskcache_oxide
from diskcache_oxide import Cache
assert diskcache_oxide.__version__ == '0.1.2', diskcache_oxide.__version__
step, directory = sys.argv[1], sys.argv[2]
out = {}
if step == 'create':
    with Cache(directory) as c:
        c.set('old', 'value')
elif step == 'open':
    try:
        Cache(directory).close()
        out['error'] = None
    except Exception as e:
        out['error'] = type(e).__name__
        out['message'] = str(e)
elif step == 'use':
    with Cache(directory) as c:
        out['old'] = c.get('old')
        c.set('again', 1)
        c.set('big', b'x' * 200_000)
        out['deleted'] = c.delete('again')
        out['culled'] = c.cull()
        out['expired'] = c.expire()
        out['cleared'] = c.clear()
print(json.dumps(out))
"""

# Opens a cache, writes, then waits: the test deletes and recreates the directory, then says go. Then
# 16 threads write (into the deleted file, through the connections the handle already holds), and the
# handle is closed and used again, which opens the new file by path, where its write must fail.
OLD_HELD = """
import json, sys, threading
from diskcache_oxide import Cache
c = Cache(sys.argv[1])
c.set('before', 1)
print('ready', flush=True)
sys.stdin.readline()
errors = []
def write(i):
    try:
        c.set(f'after-{i}', i)
    except Exception as e:
        errors.append(type(e).__name__)
threads = [threading.Thread(target=write, args=(i,)) for i in range(16)]
for t in threads: t.start()
for t in threads: t.join()
c.close()  # A closed handle used again reopens by path: the new file.
calls = {
    'set': lambda: c.set('x', 1), 'add': lambda: c.add('y', 1), 'delete': lambda: c.delete('k0'),
    'pop': lambda: c.pop('k1'), 'incr': lambda: c.incr('n'), 'decr': lambda: c.decr('n'),
    'cull': c.cull, 'expire': c.expire, 'clear': c.clear,
    'touch': lambda: c.touch('nobody', 60),  # An absent key: a present one would get its expiry.
}
reopened = {}
for name, call in calls.items():
    try:
        reopened[name] = repr(call())
    except Exception as e:
        reopened[name] = f'{type(e).__name__}: {e}'
print(json.dumps({'errors': errors, 'reopened': reopened}), flush=True)
"""

# Opens the old cache and waits while the test removes it with `remove_old_formats()`; then writes
# through the connections it holds, and reuses a closed handle, which may reopen the root by path.
OLD_HELD_REMOVED = """
import json, sys
from diskcache_oxide import Cache
c = Cache(sys.argv[1])
c.set('before', 1)
print('ready', flush=True)
sys.stdin.readline()
results = []
for i in range(5):
    try:
        c.set(f'after-{i}', i)
        results.append('ok')
    except Exception as e:
        results.append(type(e).__name__)
c.close()
try:
    c.set('reopened', 1)
    results.append('ok')
except Exception as e:
    results.append(type(e).__name__)
c.close()
print(json.dumps({'results': results}), flush=True)
"""

ROWS = """
import json, sqlite3, sys
db = sqlite3.connect(f'file:{sys.argv[1]}/diskcache-oxide.sqlite?mode=ro', uri=True)
keys = [r[0] for r in db.execute('SELECT key FROM cache ORDER BY key')]
print(json.dumps({'version': db.execute('PRAGMA user_version').fetchone()[0], 'keys': keys}))
"""


def run(python: str, script: str, *args: str) -> dict:
    proc = subprocess.run([python, '-c', script, *args], capture_output=True, text=True, timeout=60, cwd='/')
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def fingerprint(directory: Path, *, exclude: str | None = None) -> dict[str, str]:
    """Every file in the directory, by content, leaving out the top-level entry `exclude` (the
    versioned subdirectory when the root alone is compared)."""
    return {
        str(p.relative_to(directory)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(directory.rglob('*'))
        if p.is_file() and (exclude is None or p.relative_to(directory).parts[0] != exclude)
    }


def test_0_1_2_refuses_a_format_3_cache_and_writes_nothing(tmp_path: Path) -> None:
    assert OLD_PYTHON is not None
    directory = tmp_path / 'c'
    with Cache(directory) as new:
        new.set('k', 'v', tag='t')
    before = fingerprint(directory)
    got = run(OLD_PYTHON, OLD_SIDE, 'open', str(directory))
    assert got['error'] == 'FormatMismatchError', got
    assert '3' in got['message'], got
    assert fingerprint(directory) == before


def test_this_build_opens_its_own_cache_beside_a_format_2_one_and_0_1_2_keeps_working(tmp_path: Path) -> None:
    assert OLD_PYTHON is not None
    directory = tmp_path / 'c'
    run(OLD_PYTHON, OLD_SIDE, 'create', str(directory))
    before = fingerprint(directory)
    with Cache(directory) as new:
        assert Path(new.directory) == directory / SUB
        assert 'old' not in new
        new.set('k', 'v', tag='t')
        new.set('big', b'y' * 200_000)
    assert fingerprint(directory, exclude=SUB) == before, 'this build wrote nothing at the root'
    sub_before = fingerprint(directory / SUB)

    got = run(OLD_PYTHON, OLD_SIDE, 'use', str(directory))
    assert got['old'] == 'value' and got['deleted'] is True, got
    assert fingerprint(directory / SUB) == sub_before, '0.1.2 never touched the subdirectory'
    with Cache(directory) as new:
        assert new.get('k', tag=True) == ('v', 't')


def test_remove_old_formats_with_a_0_1_2_worker_still_running_leaves_the_new_cache_alone(tmp_path: Path) -> None:
    assert OLD_PYTHON is not None
    directory = tmp_path / 'c'
    held = subprocess.Popen(
        [OLD_PYTHON, '-c', OLD_HELD_REMOVED, str(directory)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd='/',
    )
    try:
        assert held.stdout is not None and held.stdin is not None
        assert held.stdout.readline().strip() == 'ready'
        with Cache(directory) as new:
            new.set('k', 'v')
            removed = new.remove_old_formats()  # Against the docs' advice: the old worker still runs.
        assert str(directory / 'diskcache-oxide.sqlite') in removed, removed
        sub_before = fingerprint(directory / SUB)
        held.stdin.write('go\n')
        held.stdin.flush()
        out, err = held.communicate(timeout=60)
        assert held.returncode == 0, err
    finally:
        held.kill()
    # What the old worker got is undefined (the docs say so); only where it could write matters.
    assert len(json.loads(out)['results']) == 6, out
    assert fingerprint(directory / SUB) == sub_before, 'the old worker never reached the subdirectory'
    with Cache(directory) as new:
        assert Path(new.directory) == directory / SUB, 'still sticky'
        assert new['k'] == 'v'
        # What the worker recreated at the root: here an empty database (its reopen failed before
        # writing), which is kept while it's fresh, since a creator may be between open and first
        # write, and removed once it's stale (review, high 1).
        root_db = directory / 'diskcache-oxide.sqlite'
        if root_db.exists() and root_db.stat().st_size == 0:
            assert new.remove_old_formats() == []
            stale = root_db.stat().st_mtime - 3600
            os.utime(root_db, (stale, stale))
        new.remove_old_formats()
    left = sorted(p.name for p in directory.iterdir())
    assert left == [SUB], left


def test_a_0_1_2_handle_open_across_a_recreate_writes_nothing_into_the_new_file(tmp_path: Path) -> None:
    assert OLD_PYTHON is not None
    directory = tmp_path / 'c'
    held = subprocess.Popen(
        [OLD_PYTHON, '-c', OLD_HELD, str(directory)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd='/',
    )
    try:
        assert held.stdout is not None and held.stdin is not None
        assert held.stdout.readline().strip() == 'ready'
        shutil.rmtree(directory)  # The upgrade: the cache deleted while a worker still has it open.
        with Cache(directory, size_limit=2**20, eviction_policy='none') as new:
            for i in range(20):  # 2 MB in 1 MiB: an old cull() that ran would evict.
                new.set(f'k{i}', b'x' * 100_000, tag='t')
            new.set('n', 5, tag='t')
        before = fingerprint(directory)
        held.stdin.write('go\n')
        held.stdin.flush()
        out, err = held.communicate(timeout=60)
        assert held.returncode == 0, err
    finally:
        held.kill()
    # 0.1.2 doesn't check the format when a handle rejoins its store: each call is refused instead.
    reopened = json.loads(out)['reopened']
    assert reopened.pop('touch') == 'False', out
    for name, result in reopened.items():
        assert result == 'OSError: SQLite error: no such table: totals', (name, result)
    assert fingerprint(directory) == before, 'nothing was written into the new file'
    rows = run(sys.executable, ROWS, str(directory))
    assert rows['version'] == 3 and len(rows['keys']) == 21, rows
    with Cache(directory) as new:
        assert new.get('n', tag=True) == (5, 't')
