from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from app.main import create_app
from app.settings import Settings
from fastapi.testclient import TestClient


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    # Windows of 10**9 s are aligned to the epoch like the real ones, and none ends before 2033, so no test
    # can straddle a boundary and split a count.
    return Settings(
        root=tmp_path / 'my-app', load_delay=0.02, search_limit=3, signin_limit=5, rate_window=10**9, claim_period=10**9
    )


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings), follow_redirects=False) as client:
        yield client


@pytest.fixture
def sign_in() -> Callable[[TestClient], str]:
    return _sign_in


def _sign_in(client: TestClient) -> str:
    """Signs in and returns the session's CSRF token, read from a form the way a browser would."""
    response = client.post('/signin', data={'email': 'mara@example.com', 'password': 'x'})
    assert response.status_code == 303, response.text
    page = client.get('/episodes/512/edit')
    match = re.search(r'name="csrf" value="([^"]+)"', page.text)
    assert match, page.text
    return match.group(1)
