"""The work queue's failure paths: a crashed worker's job comes back, a poison job stops coming back,
and a job that raises doesn't take the worker down."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from app import jobs, worker
from app.caching import AppCaches, open_caches
from app.settings import Settings


@pytest.fixture
def caches(settings: Settings) -> Iterator[AppCaches]:
    caches = open_caches(settings.root)
    yield caches
    caches.close()


def test_a_job_its_worker_left_unfinished_goes_back_to_the_front(caches: AppCaches) -> None:
    first = jobs.enqueue(caches.jobs, 'transcript', episode=510)
    jobs.enqueue(caches.jobs, 'transcript', episode=511)
    assert jobs.take(caches.jobs, 'w1') == first  # ...and then w1 dies without finish()

    assert jobs.requeue_stale(caches.jobs, lease=0) == 1
    again = jobs.take(caches.jobs, 'w2')
    assert again is not None and again['id'] == first['id'] and again['attempts'] == 2


def test_a_job_left_unfinished_max_attempts_times_is_failed_not_requeued(caches: AppCaches) -> None:
    job = jobs.enqueue(caches.jobs, 'transcript', episode=510)
    for _ in range(jobs.MAX_ATTEMPTS):
        assert jobs.take(caches.jobs, 'w') is not None
        jobs.requeue_stale(caches.jobs, lease=0)
    snap = jobs.snapshot(caches.jobs)
    assert snap['queued'] == [] and snap['running'] == []
    assert [j['id'] for j in snap['failed']] == [job['id']]


def test_a_job_that_raises_is_failed_and_the_worker_carries_on(
    caches: AppCaches, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    bad = jobs.enqueue(caches.jobs, 'transcript', episode=1)
    good = jobs.enqueue(caches.jobs, 'transcript', episode=510)
    run_job = worker.run_job

    def flaky(job: dict, *args: object) -> None:
        if job['id'] == bad['id']:
            raise KeyError('no such episode')
        run_job(job, *args)  # ty: ignore[invalid-argument-type]

    monkeypatch.setattr(worker, 'run_job', flaky)
    worker.main(settings, stop_after_idle=0.3)
    snap = jobs.snapshot(caches.jobs)
    assert [j['id'] for j in snap['failed']] == [bad['id']] and 'KeyError' in snap['failed'][0]['error']
    assert [j['id'] for j in snap['done']] == [good['id']]
    assert caches.jobs.get(f'runs:{good["id"]}') == 1


def test_the_pending_count_for_a_deploy(
    caches: AppCaches, settings: Settings, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    jobs.enqueue(caches.jobs, 'sitemap')
    monkeypatch.setenv('EXAMPLE_ROOT', str(settings.root))
    jobs.main()
    assert capsys.readouterr().out.strip() == '1 queued, 0 running'
