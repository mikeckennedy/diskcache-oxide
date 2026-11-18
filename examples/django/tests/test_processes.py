"""The real thing: gunicorn with four workers forked from a master that already opened the cache
(preload_app). Asserts what must hold whichever worker answers, never how requests were spread."""

import os
import re
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

pytestmark = [
    pytest.mark.processes,
    pytest.mark.skipif(sys.platform == 'win32', reason='gunicorn and Unix sockets are Unix-only'),
]

APP_DIR = Path(__file__).resolve().parents[1]


def stop(process: subprocess.Popen) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=15)
    except ProcessLookupError, subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


WORKERS = 4


def rendered_at(html: str) -> str:
    match = re.search(r'data-rendered-at="([^"]+)"', html)
    assert match, html
    return match.group(1)


@pytest.fixture
def client(tmp_path: Path) -> Iterator[httpx.Client]:
    # A Unix socket's path is limited to about 104 bytes, and tmp_path is deeper than that on macOS.
    sock_dir = Path(tempfile.mkdtemp(prefix='dco-', dir='/tmp'))
    sock = sock_dir / 'web.sock'
    log = tmp_path / 'gunicorn.log'
    env = {
        **os.environ,
        'EXAMPLE_ROOT': str(tmp_path / 'root'),
        'EXAMPLE_LOAD_DELAY': '0.05',
        'WEB_WORKERS': str(WORKERS),
        'BIND': f'unix:{sock}',
        'ALLOWED_HOSTS': 'app',
        'DJANGO_SETTINGS_MODULE': 'demo.settings',
    }
    with log.open('wb') as output:
        # A session of its own, so the whole process group can be stopped, gunicorn's workers included.
        web = subprocess.Popen(
            [sys.executable, '-m', 'gunicorn', '-c', 'gunicorn.conf.py'],
            cwd=APP_DIR,
            env=env,
            start_new_session=True,
            stdout=output,
            stderr=subprocess.STDOUT,
        )
    http = httpx.Client(transport=httpx.HTTPTransport(uds=str(sock)), base_url='http://app', timeout=30)
    try:
        # Ready when every worker has answered: gunicorn boots them a few at a time, and a test that
        # starts after the first one would run on fewer workers than it claims.
        answered: set[str] = set()
        deadline = time.monotonic() + 60
        while len(answered) < WORKERS:
            try:
                response = http.get('/health')
                if response.status_code == 200:
                    answered.add(response.text)
            except httpx.TransportError:
                pass
            if time.monotonic() > deadline or web.poll() is not None:
                tail = log.read_text(errors='replace')[-2000:]
                raise RuntimeError(f'{len(answered)} of {WORKERS} gunicorn workers answered:\n{tail}')
            time.sleep(0.02)
        yield http
    finally:
        http.close()
        stop(web)
        shutil.rmtree(sock_dir, ignore_errors=True)


def test_every_worker_shares_one_counter(client: httpx.Client) -> None:
    with ThreadPoolExecutor(16) as pool:
        responses = list(pool.map(lambda _: client.get('/visits'), range(200)))
    # Atomic across processes: every count from 1 to 200 handed out exactly once.
    assert sorted(r.json()['visits'] for r in responses) == list(range(1, 201))
    assert len({r.headers['x-worker'] for r in responses}) > 1


def test_the_workers_were_forked_from_a_master_that_opened_the_cache(client: httpx.Client) -> None:
    with ThreadPoolExecutor(16) as pool:
        responses = list(pool.map(lambda _: client.get('/health'), range(40)))
    warmed_in = {r.headers['x-warmed-in'] for r in responses}
    workers = {r.headers['x-worker'] for r in responses}
    assert len(warmed_in) == 1 and not warmed_in & workers  # one process, and not a worker: the master
    assert len(workers) > 1


def test_a_session_follows_the_visitor_across_workers(client: httpx.Client) -> None:
    # httpx keeps the session cookie. Keep going until two workers have answered (they nearly always
    # have within a few requests), so per-process sessions couldn't pass.
    counts, workers = [], set()
    while len(workers) < 2 or len(counts) < 30:
        assert len(counts) < 500, 'one worker answered every request'
        response = client.get('/session')
        counts.append(response.json()['count'])
        workers.add(response.headers['x-worker'])
    assert counts == list(range(1, len(counts) + 1))


def test_every_worker_serves_the_same_cached_page(client: httpx.Client) -> None:
    assert client.get('/episode/2').status_code == 200  # stored by whichever worker answered
    with ThreadPoolExecutor(16) as pool:
        responses = list(pool.map(lambda _: client.get('/episode/2'), range(40)))
    assert len({r.text for r in responses}) == 1
    assert len({r.headers['x-worker'] for r in responses}) > 1


def test_the_fragment_and_get_or_set_are_shared(client: httpx.Client) -> None:
    first_page, first_stats = client.get('/').text, client.get('/stats').json()
    with ThreadPoolExecutor(16) as pool:
        pages = list(pool.map(lambda _: client.get('/'), range(40)))
        stats = list(pool.map(lambda _: client.get('/stats'), range(40)))
    assert {rendered_at(r.text) for r in pages} == {rendered_at(first_page)}  # rendered once, by one worker
    assert all(r.json() == first_stats for r in stats)
    assert len({r.headers['x-worker'] for r in pages + stats}) > 1
