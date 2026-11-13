"""peek() and peekitem() loop forever when the head's value file is missing."""

import os
import signal
import sys
import tempfile

import diskcache

# Bounds the spin with SIGALRM, which Windows lacks.
assert sys.platform != 'win32'


def hang(*_):
    print('BUG REPRODUCED: still looping after 3 s')
    raise SystemExit(1)


with tempfile.TemporaryDirectory() as tmp, diskcache.Cache(tmp) as cache:
    cache.push(b'x' * 100_000)  # Large enough to live in its own file.
    for root, _, files in os.walk(tmp):
        for name in files:
            if name.endswith('.val'):
                os.remove(os.path.join(root, name))
    signal.signal(signal.SIGALRM, hang)
    signal.alarm(3)
    print('peek() ->', cache.peek())
    signal.alarm(0)
    print('not reproduced')
