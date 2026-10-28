"""Fixtures and markers for the conformance suite.

Pick the backend with `--backend=diskcache|oxide` (or the DCO_BACKEND environment variable).

Markers:
    diverges(backend=..., ref="D-00N", reason=..., raises=None)
        A deliberate, documented difference from diskcache. On the named backend the test becomes
        a strict xfail, so if the divergence ever disappears the run fails and forces an update.
        Every ref must exist in tests/DIVERGENCES.md. `raises` (an exception type or tuple) is
        passed to the xfail, so only that exception counts as the divergence; anything else (an
        unrelated crash) is a real failure. Use AssertionError when the divergence shows up as a
        failed assertion.
    backend_only("name")
        The test asserts behavior specific to one backend (usually the oxide side of a
        divergence) and is skipped on the others.
    requires("feature")
        Skipped with "not yet: <feature>" when the backend doesn't support it yet. These skips are
        counted in the terminal summary so they can't hide real failures.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from tests.conformance.backends import BACKEND_KEY as _backend_key
from tests.conformance.backends import Backend

DIVERGENCES_FILE = Path(__file__).parent.parent / 'DIVERGENCES.md'
_REF_HEADING = re.compile(r'^##+\s+(D-\d{3})\b', re.MULTILINE)
_NOT_YET_PREFIX = 'not yet: '


def known_divergences() -> set[str]:
    if not DIVERGENCES_FILE.exists():
        return set()
    return set(_REF_HEADING.findall(DIVERGENCES_FILE.read_text()))


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    backend = config.stash[_backend_key]
    known = known_divergences()

    for item in items:
        for mark in item.iter_markers('diverges'):
            ref = mark.kwargs.get('ref')
            if ref not in known:
                raise pytest.UsageError(f'{item.nodeid}: divergence {ref!r} is not documented in {DIVERGENCES_FILE.name}')
            if mark.kwargs.get('backend') == backend.name:
                item.add_marker(
                    pytest.mark.xfail(strict=True, raises=mark.kwargs.get('raises'), reason=f'{ref}: {mark.kwargs.get("reason", "")}')
                )

        for mark in item.iter_markers('backend_only'):
            if mark.args[0] != backend.name:
                item.add_marker(pytest.mark.skip(reason=f'{mark.args[0]}-only behavior'))

        for mark in item.iter_markers('requires'):
            feature = mark.args[0]
            if not backend.supports(feature):
                item.add_marker(pytest.mark.skip(reason=f'{_NOT_YET_PREFIX}{feature}'))


def pytest_terminal_summary(terminalreporter: Any) -> None:
    skipped = terminalreporter.stats.get('skipped', [])
    not_yet = [r for r in skipped if _NOT_YET_PREFIX in str(r.longrepr)]
    if not_yet:
        terminalreporter.write_sep('=', f'{len(not_yet)} test(s) skipped as not yet supported by this backend')


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class FakeClock:
    """A controllable clock. Starts at the real time so stored timestamps look plausible.

    In-process only: spawned and forked processes see real time.
    """

    def __init__(self) -> None:
        self.now = time.time()

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture(scope='session')
def backend(pytestconfig: pytest.Config) -> Backend:
    return pytestconfig.stash[_backend_key]


@pytest.fixture
def fake_clock(backend: Backend, monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    clock = FakeClock()
    if backend.name == 'diskcache':
        # diskcache reads time through `time.time()`, the same hook Talk Python's tests patch.
        monkeypatch.setattr(time, 'time', clock)
    return clock


@pytest.fixture
def cache_dir(tmp_path: Path) -> Path:
    return tmp_path / 'cache'


@pytest.fixture
def make_cache(backend: Backend, request: pytest.FixtureRequest) -> Iterator[Callable[..., Any]]:
    """Factory for caches. Uses the fake clock when the test requested one; closes everything at teardown."""
    created: list[Any] = []
    clock = request.getfixturevalue('fake_clock') if 'fake_clock' in request.fixturenames else None

    def factory(directory: Path, **kwargs: Any) -> Any:
        if clock is not None:
            kwargs.setdefault('clock', clock)
        cache = backend.make_cache(directory, **kwargs)
        created.append(cache)
        return cache

    yield factory

    for cache in created:
        cache.close()


@pytest.fixture
def cache(make_cache: Callable[..., Any], cache_dir: Path) -> Any:
    return make_cache(cache_dir)


@pytest.fixture(scope='session')
def empty_volume(backend: Backend, tmp_path_factory: pytest.TempPathFactory) -> int:
    return backend.empty_volume(tmp_path_factory.mktemp('empty-volume') / 'cache')
