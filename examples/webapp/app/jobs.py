"""The work queue, as the work-queues recipe builds it: each job goes to exactly one worker, and a job
a worker crashed on goes back on the queue after its lease, up to MAX_ATTEMPTS times. A job that
raises is recorded as failed, not retried.

`python -m app.jobs` prints what is queued and running, for a deploy that has to drain the queue.

The queue lives in a plain, non-durable cache with eviction off (state/jobs): on a NullCache push()
would store nothing, and a queued job must never be evicted for size.
"""

from __future__ import annotations

import os
import time
import uuid
from typing import Any

from diskcache_oxide import Cache

QUEUE = 'job'
DONE_KEEP = 24 * 3600  # how long finished jobs stay listed on the jobs page
HEARTBEAT = 15  # a worker not seen for this long is shown as gone
MAX_ATTEMPTS = 3  # a job left unfinished this many times is a poison job: it goes to failed:, not the queue


def enqueue(jobs: Cache, kind: str, **fields: Any) -> dict:
    job = {'id': uuid.uuid4().hex[:8], 'kind': kind, 'queued': time.time(), **fields}
    jobs.push(job, prefix=QUEUE)
    return job


def take(jobs: Cache, worker: str) -> dict | None:
    """The next job, recorded as running in the same transaction as the pull, so no crash can fall
    between the two. None if the queue is empty."""
    if jobs.peek(prefix=QUEUE) == (None, None):  # an idle worker reads; it doesn't open a write transaction
        return None
    with jobs.transact():
        key, job = jobs.pull(prefix=QUEUE)
        if key is None:
            return None
        jobs.set(f'running:{job["id"]}', (time.time(), worker, job))
    return job


def finish(jobs: Cache, job: dict, worker: str, started: float) -> None:
    with jobs.transact():
        jobs.delete(f'running:{job["id"]}')
        jobs.set(f'done:{job["id"]}', {**job, 'worker': worker, 'took': time.time() - started}, expire=DONE_KEEP)
        jobs.add(f'runs:{job["id"]}', 0, expire=DONE_KEEP)
        jobs.incr(f'runs:{job["id"]}')  # how many times it ran to the end: 1, unless something is wrong


def fail(jobs: Cache, job: dict, worker: str, error: str) -> None:
    """A job that raised: recorded, never retried (it would most likely raise again)."""
    with jobs.transact():
        jobs.delete(f'running:{job["id"]}')
        jobs.set(f'failed:{job["id"]}', {**job, 'worker': worker, 'error': error}, expire=DONE_KEEP)


def requeue_stale(jobs: Cache, lease: float) -> int:
    """Put back jobs whose worker died mid-way, at the front of the queue; a job that has been left
    unfinished MAX_ATTEMPTS times goes to failed: instead. Run it from the workers now and then."""
    requeued = 0
    for record in jobs.iterkeys(prefix='running:'):
        with jobs.transact():
            started, worker, job = jobs.get(record, (None, None, None))  # read again: it may have finished
            if started is None or time.time() - started <= lease:
                continue
            jobs.delete(record)
            attempts = job.get('attempts', 1)
            if attempts >= MAX_ATTEMPTS:
                error = f'left unfinished {attempts} times'
                jobs.set(f'failed:{job["id"]}', {**job, 'worker': worker, 'error': error}, expire=DONE_KEEP)
            else:
                jobs.push({**job, 'attempts': attempts + 1}, prefix=QUEUE, side='front')
                requeued += 1
    return requeued


def heartbeat(jobs: Cache, worker: str, doing: str) -> None:
    jobs.set(f'worker:{worker}', {'pid': os.getpid(), 'doing': doing, 'seen': time.time()}, expire=HEARTBEAT)


def snapshot(jobs: Cache) -> dict:
    """What the jobs page shows: queued, running, recently done, and the workers alive."""
    queued = [job for key in jobs.iterkeys(prefix=f'{QUEUE}-') if (job := jobs.get(key)) is not None]
    running = []
    for key in jobs.iterkeys(prefix='running:'):
        if (record := jobs.get(key)) is not None:
            started, worker, job = record
            running.append({**job, 'worker': worker, 'took': time.time() - started})
    done = [job for key in jobs.iterkeys(prefix='done:') if (job := jobs.get(key)) is not None]
    done.sort(key=lambda job: job['queued'], reverse=True)
    failed = [job for key in jobs.iterkeys(prefix='failed:') if (job := jobs.get(key)) is not None]
    failed.sort(key=lambda job: job['queued'], reverse=True)
    workers = {key.removeprefix('worker:'): w for key in jobs.iterkeys(prefix='worker:') if (w := jobs.get(key))}
    return {'queued': queued, 'running': running, 'done': done[:20], 'failed': failed[:20], 'workers': workers}


def main() -> None:
    """Before a deploy that changes the on-disk format, run this in the old release's worker container
    until it prints "0 queued, 0 running" (see the Docker recipe)."""
    from app.caching import open_caches
    from app.settings import Settings

    caches = open_caches(Settings.from_env().root)
    try:
        snap = snapshot(caches.jobs)
        print(f'{len(snap["queued"])} queued, {len(snap["running"])} running')
    finally:
        caches.close()


if __name__ == '__main__':
    main()
