"""Every page and endpoint, in one process, against a fresh directory per test."""

from __future__ import annotations

import time
from collections.abc import Callable

import pytest
from app import jobs, worker
from app.main import create_app
from app.settings import Settings
from diskcache_oxide import Timeout
from fastapi.testclient import TestClient


def test_the_episode_list_comes_from_the_cache_the_second_time(client: TestClient) -> None:
    first = client.get('/')
    assert first.status_code == 200
    assert 'Free-threaded Python, one year in' in first.text
    assert '0 hit' in first.text  # fragment and loader both missed
    second = client.get('/')
    assert 'pages · list' in second.text and '1 hit' in second.text and '0 miss' in second.text


def test_an_edit_evicts_the_cached_fragment(client: TestClient, sign_in: Callable[[TestClient], str]) -> None:
    client.get('/episodes/512')
    assert '1 hit' in client.get('/episodes/512').text
    csrf = sign_in(client)
    saved = client.post(
        '/episodes/512/edit',
        data={
            'title': 'Free-threading, revisited',
            'description': 'New notes.',
            'tags': 'threads',
            'minutes': 65,
            'csrf': csrf,
        },
    )
    assert saved.status_code == 303
    page = client.get('/episodes/512')
    assert 'Free-threading, revisited' in page.text, 'the edit must not be hidden by a stale fragment'
    assert 'Free-threading, revisited' in client.get('/').text, 'nor by a stale list'


def test_a_form_without_the_sessions_csrf_token_is_refused(
    client: TestClient, sign_in: Callable[[TestClient], str]
) -> None:
    sign_in(client)
    response = client.post(
        '/episodes/512/edit',
        data={'title': 'x', 'description': 'x', 'tags': '', 'minutes': 1, 'csrf': 'forged'},
    )
    assert response.status_code == 400 and 'The form expired' in response.text


def test_a_missing_page_is_the_apps_own_404(client: TestClient) -> None:
    for path in ('/episodes/1', '/no-such-page'):
        response = client.get(path)
        assert response.status_code == 404 and 'Not found' in response.text and '<html' in response.text


def test_editing_needs_a_session(client: TestClient) -> None:
    response = client.get('/episodes/512/edit')
    assert response.status_code == 303 and response.headers['location'].startswith('/signin')


def test_search_is_rate_limited_per_client(client: TestClient) -> None:
    assert [client.get('/', params={'q': 'sqlite'}).status_code for _ in range(4)] == [200, 200, 200, 429]
    assert 'SQLite as an application file format' in client.get('/', params={'q': ''}).text


def test_sign_in_and_out(client: TestClient, sign_in: Callable[[TestClient], str]) -> None:
    sign_in(client)
    assert 'Sign out mara@example.com' in client.get('/').text
    client.post('/signout')
    client.cookies.clear()
    assert 'Sign in' in client.get('/').text


def test_the_sitemap_is_claimed_once_per_period(client: TestClient) -> None:
    for _ in range(5):
        client.get('/')
    queued = jobs.snapshot(client.app.state.caches.jobs)['queued']
    assert [j['kind'] for j in queued] == ['sitemap']
    assert 'Claimed by' in client.get('/jobs').text


def test_a_requested_transcript_is_run_by_the_worker(
    client: TestClient, settings: Settings, sign_in: Callable[[TestClient], str]
) -> None:
    csrf = sign_in(client)
    assert client.post('/episodes/510/transcript', data={'csrf': csrf}).status_code == 303
    worker.main(settings, stop_after_idle=0.3)
    snap = jobs.snapshot(client.app.state.caches.jobs)
    assert snap['queued'] == [] and snap['running'] == []
    assert [(j['kind'], j['episode']) for j in snap['done']] == [('transcript', 510)]
    assert 'transcript for #510' in client.get('/jobs').text


def test_the_sitemap_job_writes_the_file_under_the_lock(client: TestClient, settings: Settings) -> None:
    client.get('/')
    worker.main(settings, stop_after_idle=0.3)
    sitemap = (settings.root / 'public' / 'sitemap.xml').read_text()
    assert '/episodes/512' in sitemap


def test_health_reports_every_cache(client: TestClient) -> None:
    report = client.get('/health/cache').json()
    assert report['status'] == 'ok'
    assert set(report['caches']) == {'pages', 'data', 'rate_limits'}
    assert set(report['state']) == {'sessions', 'coordination', 'jobs'}


def test_the_admin_page_answers_only_locally(settings: Settings) -> None:
    app = create_app(settings)
    with TestClient(app, client=('127.0.0.1', 50000)) as local:
        assert local.get('/admin/caches').status_code == 200
    with TestClient(app, client=('203.0.113.9', 50000)) as remote:
        assert remote.get('/admin/caches').status_code == 404


def test_a_busy_session_store_is_a_503_not_a_fake_login(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    class Busy:
        def set(self, *args: object, **kwargs: object) -> bool:
            raise Timeout('busy')

        def get(self, *args: object, **kwargs: object) -> tuple[None, None]:
            return None, None

    monkeypatch.setattr(client.app.state.caches, 'sessions', Busy())
    response = client.post('/signin', data={'email': 'mara@example.com', 'password': 'x'})
    assert response.status_code == 503 and 'sid' not in response.cookies


class _Session:
    """A session store whose reads work and whose writes time out, as when its lock is held."""

    def get(self, *args: object, **kwargs: object) -> tuple[dict, float]:
        # Close to its idle expiry, so the request tries to slide it with touch().
        return {'email': 'mara@example.com', 'csrf': 'token', 'started': time.time()}, time.time() + 10

    def touch(self, *args: object, **kwargs: object) -> bool:
        raise Timeout('busy')


def test_a_failing_queue_under_a_busy_session_store_is_a_503(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    class BrokenQueue:
        def push(self, *args: object, **kwargs: object) -> str:
            raise OSError(28, 'No space left on device')

    monkeypatch.setattr(client.app.state.caches, 'sessions', _Session())
    monkeypatch.setattr(client.app.state.caches, 'jobs', BrokenQueue())
    client.cookies.set('sid', 'x')
    assert client.get('/episodes/512').status_code == 200, 'a missed session refresh is not an error'
    response = client.post('/episodes/510/transcript', data={'csrf': 'token'})
    assert response.status_code == 503 and 'Unavailable' in response.text


def test_sign_in_fails_closed_when_it_cannot_count_attempts(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Busy:
        def add(self, *args: object, **kwargs: object) -> bool:
            raise Timeout('busy')

    monkeypatch.setattr(client.app.state.caches, 'coordination', Busy())
    response = client.post('/signin', data={'email': 'mara@example.com', 'password': 'x'})
    assert response.status_code == 503 and 'sid' not in response.cookies


def test_saving_a_missing_episode_is_a_404(client: TestClient, sign_in: Callable[[TestClient], str]) -> None:
    csrf = sign_in(client)
    data = {'title': 'x', 'description': 'x', 'tags': '', 'minutes': 1, 'csrf': csrf}
    assert client.post('/episodes/1/edit', data=data).status_code == 404
