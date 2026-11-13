"""Startup hooks for the whole test tree.

Command-line options and anything that may raise a usage error must live in a conftest pytest
loads at startup, so backend selection is here; the fixtures and markers it drives live in
tests/conformance/conftest.py.
"""

import os
import sys

import pytest

from tests import orphan_guard
from tests.conformance.backends import BACKEND_KEY, BACKENDS
from tests.proctitle import set_title


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        '--backend',
        default=os.environ.get('DCO_BACKEND', 'diskcache'),
        choices=sorted(BACKENDS),
        help='Cache implementation to run the conformance suite against.',
    )


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config: pytest.Config) -> None:
    # Windows has no SIGALRM, so pyproject's signal method would raise in every test. Runs before
    # pytest-timeout reads its settings, and leaves an explicit --timeout_method alone.
    if sys.platform == 'win32' and config.option.timeout_method is None:
        config.option.timeout_method = 'thread'
    name = config.getoption('--backend')
    set_title(f'pytest[{name}]')
    orphan_guard.install()
    try:
        config.stash[BACKEND_KEY] = BACKENDS[name]()
    except ImportError as ex:
        raise pytest.UsageError(f'Backend {name!r} is not importable: {ex}') from ex
