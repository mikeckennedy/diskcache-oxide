"""Windows: deleting or overwriting a large value while a reader has its file open leaks the file."""

import pathlib
import sys
import tempfile

import diskcache

# Windows only: there, Python's open() doesn't share delete, so the reader's handle blocks os.remove.
assert sys.platform == 'win32'

with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp, diskcache.Cache(tmp) as cache:
    cache.set('big', b'x' * 100_000)  # Large enough to be stored as a separate file.
    cache.set('overwritten', b'y' * 100_000)
    reader = cache.get('big', read=True)  # What a reader in another thread or process holds open.
    other = cache.get('overwritten', read=True)
    cache.delete('big')
    cache.set('overwritten', b'z' * 100_000)
    reader.close()
    other.close()
    files = [p for p in pathlib.Path(tmp).rglob('*.val')]
    print('rows ->', len(cache), '| value files on disk ->', len(files), '(one is live)')
    bug = len(files) > 1
    leaked = 'LEAK REPRODUCED (both removes failed and were suppressed; nothing will remove the files)'
    print(leaked if bug else 'not reproduced')
    raise SystemExit(1 if bug else 0)
