"""Background maintenance's test hooks, set and undone in one place (plan 28)."""

from __future__ import annotations

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
