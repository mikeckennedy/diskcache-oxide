"""The background worker: `python -m app.worker`. Run as many as you like; each job goes to one of them.

It pulls jobs from the queue in state/jobs, runs them, and puts back jobs a crashed worker left
half-done. The sitemap job takes a Lock, which waits for its turn; that's fine here, and it's why
locks live in workers, never in a request (a request uses an add() claim, which skips instead).
"""

from __future__ import annotations

import logging
import logging.config
import os
import signal
import socket
import tempfile
import time
from pathlib import Path

from diskcache_oxide import Lock, Timeout

from app import jobs
from app.caching import AppCaches, open_caches
from app.logging_config import LOGGING
from app.settings import Settings
from app.store import Store

log = logging.getLogger('app')
LEASE = 120  # a job running longer than this is assumed lost and goes back on the queue


def write_sitemap(store: Store, path: Path) -> None:
    urls = ''.join(f'  <url><loc>/episodes/{e.number}</loc></url>\n' for e in store.all())
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix='.tmp')
    with os.fdopen(fd, 'w') as f:
        f.write(f'<?xml version="1.0" encoding="UTF-8"?>\n<urlset>\n{urls}</urlset>\n')
    os.replace(tmp, path)


def run_job(job: dict, caches: AppCaches, store: Store, settings: Settings) -> None:
    if job['kind'] == 'transcript':
        time.sleep(settings.load_delay * 5)  # stand-in for slow work
    elif job['kind'] == 'sitemap':
        # One writer at a time, across every worker process. expire= frees it if this process dies.
        with Lock(caches.coordination, 'lock:sitemap', expire=60):
            write_sitemap(store, settings.root / 'public' / 'sitemap.xml')


def main(settings: Settings | None = None, *, stop_after_idle: float | None = None) -> None:
    settings = settings or Settings.from_env()
    caches = open_caches(settings.root)
    store = Store(settings.root / 'db', settings.load_delay)
    name = f'{socket.gethostname()}:{os.getpid()}'
    stopping = False

    def stop(*_: object) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    log.info('worker %s started', name)
    idle_since = time.monotonic()
    next_sweep = 0.0
    last_beat, last_doing = 0.0, ''
    try:
        while not stopping:
            try:
                if time.monotonic() >= next_sweep:
                    if requeued := jobs.requeue_stale(caches.jobs, LEASE):
                        log.warning('requeued %s job(s) a worker left unfinished', requeued)
                    next_sweep = time.monotonic() + 30
                job = jobs.take(caches.jobs, name)
                doing = f'running {job["id"]}' if job else 'idle'
                if doing != last_doing or time.monotonic() - last_beat > jobs.HEARTBEAT / 3:
                    jobs.heartbeat(caches.jobs, name, doing)
                    last_beat, last_doing = time.monotonic(), doing
                if job is None:
                    if stop_after_idle is not None and time.monotonic() - idle_since > stop_after_idle:
                        break
                    time.sleep(0.25)
                    continue
                started = time.time()
                try:
                    run_job(job, caches, store, settings)
                except Exception as exc:
                    # One bad job must not stop the worker: record it as failed and move on.
                    log.exception('job %s (%s) failed', job['id'], job['kind'])
                    jobs.fail(caches.jobs, job, name, f'{type(exc).__name__}: {exc}')
                else:
                    jobs.finish(caches.jobs, job, name, started)
                idle_since = time.monotonic()
            except Timeout:
                log.warning('the job queue is busy; retrying')
                time.sleep(1)
            except OSError:
                # The queue itself failed (a full disk, say). A job it couldn't record as done goes back on
                # the queue after its lease; keep going rather than exit, and retry a little later.
                log.exception('the job queue failed; retrying in 5 s')
                time.sleep(5)
    finally:
        try:
            caches.jobs.delete(f'worker:{name}')  # the heartbeat expires on its own if this fails
        except OSError:
            pass
        caches.close()
        log.info('worker %s stopped', name)


if __name__ == '__main__':
    logging.config.dictConfig(LOGGING)
    main()
