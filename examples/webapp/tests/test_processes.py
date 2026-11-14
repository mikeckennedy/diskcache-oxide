"""The real thing: gunicorn with four workers and two background workers, as separate processes on one
directory. Asserts what must hold whichever worker answers, never how the kernel spread requests."""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pytest
from app import jobs
from app.caching import open_caches

pytestmark = [
    pytest.mark.processes,
    pytest.mark.skipif(sys.platform == 'win32', reason='gunicorn and Unix sockets are Unix-only'),
]

APP_DIR = Path(__file__).resolve().parents[1]
SEARCH_LIMIT = 5


def start(args: list[str], env: dict[str, str]) -> subprocess.Popen:
    # A session of its own, so the whole process group can be stopped, gunicorn's workers included.
    return subprocess.Popen(
        args, cwd=APP_DIR, env=env, start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )


def stop(process: subprocess.Popen) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=15)
    except ProcessLookupError, subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


@pytest.fixture
def stack(tmp_path: Path) -> Iterator[tuple[httpx.Client, Path]]:
    root = tmp_path / 'my-app'
    # A Unix socket's path is limited to about 104 bytes, and tmp_path is deeper than that on macOS.
    sock_dir = Path(tempfile.mkdtemp(prefix='dco-', dir='/tmp'))
    sock = sock_dir / 'web.sock'
    env = {
        **os.environ,
        'EXAMPLE_ROOT': str(root),
        'EXAMPLE_LOAD_DELAY': '0.01',
        'EXAMPLE_SEARCH_LIMIT': str(SEARCH_LIMIT),
        # One window, and one claim period, for the whole test: none ends before 2033.
        'EXAMPLE_RATE_WINDOW': str(10**9),
        'EXAMPLE_CLAIM_PERIOD': str(10**9),
        'WEB_WORKERS': '4',
        'BIND': f'unix:{sock}',
    }
    web = start([sys.executable, '-m', 'gunicorn', '-c', 'gunicorn.conf.py'], env)
    processes = [web]
    client = httpx.Client(transport=httpx.HTTPTransport(uds=str(sock)), base_url='http://app', timeout=30)
    try:
        deadline = time.monotonic() + 60
        while True:  # ready when the socket answers
            try:
                if client.get('/health/cache').status_code == 200:
                    break
            except httpx.TransportError:
                pass
            if time.monotonic() > deadline or web.poll() is not None:
                raise RuntimeError('gunicorn did not come up')
            time.sleep(0.2)
        processes += [start([sys.executable, '-m', 'app.worker'], env) for _ in range(2)]
        yield client, root
    finally:
        client.close()
        for process in processes:
            stop(process)
        shutil.rmtree(sock_dir, ignore_errors=True)


def test_workers_share_one_claim_one_rate_limit_and_one_queue(stack: tuple[httpx.Client, Path]) -> None:
    client, root = stack
    with ThreadPoolExecutor(16) as pool:
        # Many concurrent visits, spread over four web workers: the hourly claim is won exactly once.
        assert all(r.status_code == 200 for r in pool.map(lambda _: client.get('/'), range(40)))
        # One client, many concurrent searches: exactly SEARCH_LIMIT get through, across every worker.
        statuses = list(pool.map(lambda _: client.get('/', params={'q': 'rust'}).status_code, range(20)))
    assert statuses.count(200) == SEARCH_LIMIT and statuses.count(429) == 20 - SEARCH_LIMIT

    caches = open_caches(root)
    try:
        queued = [jobs.enqueue(caches.jobs, 'transcript', episode=n) for n in range(500, 512)]
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            snap = jobs.snapshot(caches.jobs)
            if not snap['queued'] and not snap['running'] and len(snap['done']) >= len(queued) + 1:
                break
            time.sleep(0.2)
        snap = jobs.snapshot(caches.jobs)
        kinds = sorted(j['kind'] for j in snap['done'])
        assert kinds == ['sitemap'] + ['transcript'] * len(queued), snap  # the sitemap was queued once
        assert {j['id'] for j in snap['done']} >= {j['id'] for j in queued}
        # Each job ran to the end exactly once: finish() counts runs, which a second run would make 2.
        assert all(caches.jobs.get(f'runs:{j["id"]}') == 1 for j in snap['done']), snap
        assert snap['failed'] == []
    finally:
        caches.close()
    assert (root / 'public' / 'sitemap.xml').exists()
