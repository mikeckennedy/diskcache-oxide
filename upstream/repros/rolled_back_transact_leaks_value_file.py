"""A large value set inside a transact() that rolls back leaves its file on disk, with no row."""

import pathlib
import tempfile

import diskcache

with tempfile.TemporaryDirectory() as tmp, diskcache.Cache(tmp) as cache:
    try:
        with cache.transact():
            cache.set('big', b'x' * 100_000)  # Large enough to be stored as a separate file.
            raise RuntimeError('roll back')
    except RuntimeError:
        pass
    files = [p for p in pathlib.Path(tmp).rglob('*') if p.is_file() and not p.name.startswith('cache.db')]
    print('rows ->', len(cache), '| value files on disk ->', [str(p.relative_to(tmp)) for p in files])
    bug = len(cache) == 0 and bool(files)
    print('LEAK REPRODUCED (the rolled-back value file stays, and nothing will remove it)' if bug else 'not reproduced')
    raise SystemExit(1 if bug else 0)
