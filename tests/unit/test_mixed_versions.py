"""0.1.2 and this build on one cache directory, as during a deploy (plan 24, release 1).

This build stores typed keys (D-004); 0.1.2 must never see them in a keyed call or an iteration,
must keep working on the `str` keys both share, and must still count, expire, cull and clear the
typed rows, with `totals` exact. The old side runs the shipped 0.1.2 wheel, in a Python of its own
named by `DCO_OLD_PYTHON` (`scripts/mixed-version.sh` builds it); without it the test is skipped.
Each side runs in its own process, and the `totals` check uses the stdlib's sqlite3 in a third.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from diskcache_oxide import Cache

OLD_PYTHON = os.environ.get('DCO_OLD_PYTHON')
pytestmark = pytest.mark.skipif(not OLD_PYTHON, reason='set DCO_OLD_PYTHON to a Python with diskcache-oxide 0.1.2')

TYPED = [1, b'b', None, True, 2.5, ('m.f', 3, None)]

OLD_SIDE = """
import json, sys
import diskcache_oxide
from diskcache_oxide import Cache
assert diskcache_oxide.__version__ == '0.1.2', diskcache_oxide.__version__
step, directory = sys.argv[1], sys.argv[2]
out = {}
with Cache(directory) as c:
    if step == 'read':
        out['shared'] = c.get('shared')
        out['keys'] = list(c.iterkeys())
        out['iter'] = list(c)
        out['len'] = len(c)
        # Before any write: a write sweeps expired rows itself.
        out['expired'] = c.expire()
        out['len_after_expire'] = len(c)
        c.set('from-old', 'old')
    elif step == 'clear':
        out['cleared'] = c.clear()
        out['len'] = len(c)
print(json.dumps(out))
"""

TOTALS = """
import json, sqlite3, sys
db = sqlite3.connect(sys.argv[1] + '/diskcache-oxide.sqlite')
rows = db.execute('SELECT COUNT(*), COALESCE(SUM(size), 0) FROM cache').fetchone()
totals = db.execute('SELECT count, size FROM totals WHERE only = 1').fetchone()
print(json.dumps({'rows': list(rows), 'totals': list(totals)}))
"""


def run(python: str, script: str, *args: str) -> dict:
    proc = subprocess.run([python, '-c', script, *args], capture_output=True, text=True, timeout=60, cwd='/')
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def totals_exact(directory: Path) -> None:
    got = run(sys.executable, TOTALS, str(directory))
    assert got['rows'] == got['totals'], got


def test_0_1_2_works_beside_typed_keys_and_still_maintains_them(tmp_path: Path) -> None:
    assert OLD_PYTHON is not None
    directory = tmp_path / 'c'
    with Cache(directory) as new:
        new.set('shared', 'both')
        for key in TYPED:
            new.set(key, 'typed')
        big = b'x' * 70_000
        new.set((b'big',), big)  # A typed key with a value file, for 0.1.2's clear() to remove.
        # Stored already expired, and last (a later write would sweep it), for 0.1.2's expire().
        new.set(('expired', 1), 'gone', expire=-1)
    totals_exact(directory)

    old = run(OLD_PYTHON, OLD_SIDE, 'read', str(directory))
    assert old['shared'] == 'both'
    assert old['keys'] == old['iter'] == ['shared'], 'typed keys are invisible to 0.1.2'
    assert old['len'] == 1 + len(TYPED) + 2, 'but counted'
    assert old['expired'] == 1, "0.1.2's expire() removes an expired typed row"
    assert old['len_after_expire'] == old['len'] - 1
    totals_exact(directory)

    with Cache(directory) as new:
        assert new.get('from-old') == 'old'
        for key in TYPED:
            assert new.get(key) == 'typed', key
        assert new.get((b'big',)) == big
        assert ('expired', 1) not in new

    old = run(OLD_PYTHON, OLD_SIDE, 'clear', str(directory))
    assert old['cleared'] == 1 + 1 + len(TYPED) + 1
    assert old['len'] == 0
    totals_exact(directory)
    assert not list(directory.rglob('*.val')), "0.1.2's clear() removes typed rows' value files"
    with Cache(directory) as new:
        assert list(new) == []
