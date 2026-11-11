"""Every decoration of a throttled function refills the shared bucket, so each worker process that
imports the module gets a full bucket of its own and the limit is multiplied by the worker count."""

import tempfile

import diskcache
from diskcache import throttle

now = [1000.0]
slept: list[float] = []


def sleep(seconds: float) -> None:
    slept.append(seconds)
    now[0] += seconds


with tempfile.TemporaryDirectory() as tmp, diskcache.Cache(tmp) as cache:

    def decorate():
        @throttle(cache, 1, 10, name='api', time_func=lambda: now[0], sleep_func=sleep)
        def call() -> int:
            return 1

        return call

    decorate()()  # Takes the one token per 10 s.
    decorate()()  # A second "worker" decorates: the bucket is full again, so no wait.
    print('waited', sum(slept), 's for the second call (should be 10)')
    bug = sum(slept) < 9.9
    print('BUG REPRODUCED' if bug else 'not reproduced')
    raise SystemExit(1 if bug else 0)
