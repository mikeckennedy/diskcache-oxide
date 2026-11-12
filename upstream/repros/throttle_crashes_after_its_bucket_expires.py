"""throttle(expire=...) raises TypeError once its bucket expires (or a clear() removes it)."""

import tempfile

import diskcache
from diskcache import throttle

now = [1000.0]
with tempfile.TemporaryDirectory() as tmp, diskcache.Cache(tmp) as cache:

    @throttle(cache, 2, 1, expire=5, time_func=lambda: now[0], sleep_func=lambda s: None)
    def work() -> int:
        return 1

    print('first call ->', work())
    now[0] += 60  # Idle for longer than expire; the bucket's row expires.
    # diskcache reads expiry from time.time(), not time_func, so expire the row the same way it would.
    cache.clear()
    try:
        work()
    except TypeError as e:
        print('second call -> TypeError:', e)
        print('BUG REPRODUCED')
        raise SystemExit(1) from None
    print('not reproduced')
