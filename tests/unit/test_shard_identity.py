"""A plain Cache refuses a FanoutCache root and a FanoutCache shard (plan 13, step 13b).

FanoutCache itself arrives in 13d, so these build its on-disk markers by hand: the root manifest,
and a shard's `settings` stamp. The core tests cover the rest, including the fast path.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from diskcache_oxide import Cache

MANIFEST = 'diskcache-oxide fanout\nformat = 1\nshards = 8\nhash = xxh3-64 seed=0 range=mulhi\n'


def test_a_cache_on_a_fanout_root_raises_and_creates_no_database(tmp_path: Path) -> None:
    (tmp_path / 'diskcache-oxide-fanout').write_text(MANIFEST)
    with pytest.raises(ValueError, match='FanoutCache root'):
        Cache(tmp_path)
    assert not (tmp_path / 'diskcache-oxide.sqlite').exists()


def test_a_cache_on_a_shard_raises(tmp_path: Path) -> None:
    Cache(tmp_path).close()
    # A separate process, so this one never holds a second SQLite connection on the file.
    script = (
        'import sqlite3, sys\ncon = sqlite3.connect(sys.argv[1])\n'
        "con.execute(\"INSERT INTO settings VALUES ('fanout', '3/8/xxh3-64 seed=0 range=mulhi')\")\n"
        'con.commit()\ncon.close()'
    )
    subprocess.run([sys.executable, '-c', script, str(tmp_path / 'diskcache-oxide.sqlite')], check=True)
    with pytest.raises(ValueError, match='shard 3 of 8 of a FanoutCache'):
        Cache(tmp_path)
