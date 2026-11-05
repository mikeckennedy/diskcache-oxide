"""Count WAL frames written per incr, stock diskcache versus diskcache-oxide.

Usage (from the repo root):
    venv/bin/python bench/scaling/frames.py stock|oxide TMPDIR

Scratch directories are created under TMPDIR.
"""

import os
import random
import sqlite3
import sys
import tempfile

backend, tmp = sys.argv[1], sys.argv[2]
d = tempfile.mkdtemp(dir=tmp)
if backend == 'stock':
    import diskcache

    c = diskcache.Cache(d, timeout=0.25)
else:
    import diskcache_oxide

    c = diskcache_oxide.Cache(d, timeout=0.25, durable=True)
keys = [f'rl:h:10.0.{i // 256}.{i % 256}:2026110512' for i in range(3000)]
for k in keys:
    c.add(k, 0, expire=3600)
if backend == 'stock':
    c._sql('PRAGMA wal_checkpoint(TRUNCATE)')  # ty: ignore[unresolved-attribute] stock-only private API
else:
    c.checkpoint()  # ty: ignore[unresolved-attribute] oxide-only; the union includes stock diskcache
db = 'cache.db' if backend == 'stock' else 'diskcache-oxide.sqlite'
wal = os.path.join(d, db + '-wal')
size0 = os.path.getsize(wal) if os.path.exists(wal) else 0
picks = random.Random(1).choices(keys, k=500)
for k in picks:
    c.incr(k, default=None)
frames = (os.path.getsize(wal) - size0) / (4096 + 24)
grew_kib = (os.path.getsize(wal) - size0) // 1024
print(
    f'{backend}: {frames / len(picks):.2f} WAL frames (pages) per incr; WAL grew {grew_kib} KiB over {len(picks)} incrs'
)
con = sqlite3.connect(os.path.join(d, db))
print('  indexes:', [r[0] for r in con.execute("select sql from sqlite_master where type='index' and sql is not null")])
print('  synchronous:', con.execute('pragma synchronous').fetchone(), "(this fresh connection, not the library's)")
