"""close() closes only the calling thread's connection; other threads' connections stay open.

After 16 worker threads each use a shared Cache and exit, close() leaves their SQLite connections
(and file descriptors) open until garbage collection, which then reports a ResourceWarning
("unclosed database") for each one.
"""

import gc
import sys
import tempfile
import threading
import warnings

import diskcache

# sqlite3 reports unclosed connections from dealloc, sometimes as an unraisable rather than a warning.
unraisable: list[object] = []
sys.unraisablehook = unraisable.append
gc.disable()

with tempfile.TemporaryDirectory() as tmp:
    cache = diskcache.Cache(tmp)

    def work(i: int) -> None:
        cache.set(f'k{i}', i)
        cache.get(f'k{i}')

    threads = [threading.Thread(target=work, args=(i,)) for i in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    cache.close()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter('always', ResourceWarning)
        gc.enable()
        gc.collect()

    warned = [w for w in caught if issubclass(w.category, ResourceWarning)]
    raised = [u for u in unraisable if isinstance(getattr(u, 'exc_value', None), ResourceWarning)]
    leaked = len(warned) + len(raised)
    print(f'unclosed-connection warnings after close() + gc: {leaked} (16 worker threads)')
    bug = leaked > 0
    print('BUG REPRODUCED' if bug else 'not reproduced')
    raise SystemExit(1 if bug else 0)
