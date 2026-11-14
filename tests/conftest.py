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
    parser.addoption(
        '--maintenance-interval',
        type=float,
        default=float(os.environ.get('DCO_MAINTENANCE_INTERVAL', '0') or 0),
        help=(
            'Run diskcache-oxide background maintenance every this many seconds in every cache a test '
            'opens without its own maintenance_interval, so it runs beside every test (the sanitizer '
            'lanes and the conformance suite). 0, the default, leaves the library default (5 minutes, '
            'never reached in a test). Tests marked races_maintenance are left out.'
        ),
    )


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config: pytest.Config) -> None:
    # Windows has no SIGALRM, so pyproject's signal method would raise in every test. Runs before
    # pytest-timeout reads its settings, and leaves an explicit --timeout_method alone.
    if sys.platform == 'win32' and config.option.timeout_method is None:
        config.option.timeout_method = 'thread'
    interval = config.getoption('--maintenance-interval')
    if interval:
        from tests import maintenance

        # Exported, so the processes tests start (fork scenarios, spawn workers) apply it too.
        os.environ[maintenance.ENV] = str(interval)
        maintenance.apply_from_env()
        marks = config.option.markexpr
        config.option.markexpr = f'({marks}) and not races_maintenance' if marks else 'not races_maintenance'
    name = config.getoption('--backend')
    set_title(f'pytest[{name}]')
    orphan_guard.install()
    try:
        config.stash[BACKEND_KEY] = BACKENDS[name]()
    except ImportError as ex:
        raise pytest.UsageError(f'Backend {name!r} is not importable: {ex}') from ex


#: Under --maintenance-interval, the fewest maintenance turns the pytest process itself must see, so
#: the mode can't pass with maintenance never having run beside the tests.
MIN_TURNS = 20


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    if not session.config.getoption('--maintenance-interval') or exitstatus != 0:
        return
    from diskcache_oxide import _core

    turns, removed = _core._maintenance_totals()
    reporter = session.config.pluginmanager.get_plugin('terminalreporter')
    if reporter is not None:
        reporter.write_line(f'background maintenance ran {turns} turns, removing {removed} rows, in this process')
    if turns < MIN_TURNS:
        if reporter is not None:
            reporter.write_line(f'FAILED: fewer than {MIN_TURNS} maintenance turns: the mode ran for nothing')
        session.exitstatus = 1
