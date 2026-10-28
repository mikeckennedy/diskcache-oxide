"""Forking while another thread is inside a write transaction makes the child's writes time out.

The child inherits SQLite's in-process lock bookkeeping for a transaction that only exists in the
parent. The parent commits right after the fork, but the child's writes still raise Timeout.
"""

import os
import sys
import tempfile
import threading

import diskcache

if sys.platform == 'win32':
    raise SystemExit('needs os.fork')

with tempfile.TemporaryDirectory() as tmp:
    cache = diskcache.Cache(tmp, timeout=2)
    cache.set('warm', 1)
    in_txn, may_commit = threading.Event(), threading.Event()

    def hold_write_transaction() -> None:
        with cache.transact():
            cache.set('a', 1)
            in_txn.set()
            may_commit.wait()

    t = threading.Thread(target=hold_write_transaction)
    t.start()
    in_txn.wait()

    pid = os.fork()
    if pid == 0:
        try:
            cache.get('warm')
            cache.set('from-child', 1)
            os._exit(0)
        except diskcache.Timeout:
            os._exit(1)

    may_commit.set()  # The parent's transaction commits immediately after the fork.
    t.join()
    _, status = os.waitpid(pid, 0)
    bug = os.waitstatus_to_exitcode(status) == 1
    print("child's set raised Timeout:", bug)
    print('BUG REPRODUCED' if bug else 'not reproduced')
    raise SystemExit(1 if bug else 0)
