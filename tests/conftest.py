"""Startup hooks for the whole test tree.

Command-line options and anything that may raise a usage error must live in a conftest pytest
loads at startup, so backend selection is here; the fixtures and markers it drives live in
tests/conformance/conftest.py.
"""

import os

import pytest

from tests.conformance.backends import BACKEND_KEY, BACKENDS
from tests.proctitle import set_title


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        '--backend',
        default=os.environ.get('DCO_BACKEND', 'diskcache'),
        choices=sorted(BACKENDS),
        help='Cache implementation to run the conformance suite against.',
    )


def pytest_configure(config: pytest.Config) -> None:
    name = config.getoption('--backend')
    set_title(f'pytest[{name}]')
    try:
        config.stash[BACKEND_KEY] = BACKENDS[name]()
    except ImportError as ex:
        raise pytest.UsageError(f'Backend {name!r} is not importable: {ex}') from ex
