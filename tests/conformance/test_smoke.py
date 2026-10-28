from typing import Any

from tests.conformance.backends import Backend
from tests.conformance.conftest import FakeClock


def test_set_then_get(cache: Any) -> None:
    assert cache.set('k', 'v') is True
    assert cache.get('k') == 'v'


def test_fake_clock_drives_expiry(cache: Any, fake_clock: FakeClock) -> None:
    cache.set('k', 'v', expire=10)
    fake_clock.advance(9)
    assert cache.get('k') == 'v'
    fake_clock.advance(2)
    assert cache.get('k') is None


def test_db_path_points_at_the_database(cache: Any, cache_dir: Any, backend: Backend) -> None:
    cache.set('k', 'v')
    assert backend.db_path(cache_dir).is_file()
