"""Background maintenance's test hooks, set and undone in one place (plan 28)."""

from __future__ import annotations

import os
from collections.abc import Generator
from contextlib import contextmanager

from diskcache_oxide import _core

#: The public floor for `maintenance_interval`, restored after a test lowers it.
PUBLIC_FLOOR = 1.0


@contextmanager
def fast_maintenance(floor: float = 0.01) -> Generator[None]:
    """Lets `maintenance_interval` go down to `floor` seconds while the block runs, so a test can watch
    maintenance work, and clears any failures a test injected but didn't use up."""
    _core._set_maintenance_floor(floor)
    try:
        yield
    finally:
        _core._set_maintenance_floor(PUBLIC_FLOOR)
        _core._fail_next_maintenance_turns(0)


#: Where a test run's short maintenance interval reaches the processes its tests start.
ENV = 'DCO_MAINTENANCE_INTERVAL'


def interval_from_env() -> float:
    return float(os.environ.get(ENV, '0') or 0)


def apply_from_env() -> float:
    """In any process a test starts: the run's short maintenance interval, from the environment,
    for every cache opened here without its own (`--maintenance-interval`). Returns it, 0 if none."""
    interval = interval_from_env()
    if interval:
        _core._set_default_maintenance_interval(interval)
    return interval
