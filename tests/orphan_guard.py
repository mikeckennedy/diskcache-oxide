"""Spawned test children exit when the test run that started them dies.

Harness only. Many spawned workers loop until the parent sets an Event or SIGKILLs them, so a
killed or crashed pytest used to leave them running for hours (18 on the MacBook, 2026-11-11).
`install()`, called once from tests/conftest.py, wraps the target of every process the spawn
context creates (workers, Barrier and Manager servers alike) in `guarded`, which starts a daemon
thread in the child that exits the moment the parent is gone.

Spawn children only. Forked children are left alone: a forked child with an extra thread would
change what the fork tests measure, and the fork checks run in fresh interpreters started with
subprocess anyway, under their own timeouts.
"""

from __future__ import annotations

import functools
import os
import threading
import time
from collections.abc import Callable
from multiprocessing import context
from typing import Any

# How often a child checks that its parent is still alive.
POLL = 0.5
# The exit status of a child that outlived its parent, so it's recognizable in a log.
ORPHANED = 70


def _watch(parent: int) -> None:
    while True:
        if os.getppid() != parent:  # reparented: the parent died
            os._exit(ORPHANED)
        time.sleep(POLL)


def guarded(target: Callable[..., Any], parent: int, *args: Any, **kwargs: Any) -> Any:
    """Run `target` in a spawned child, exiting at once if `parent` stops being the parent."""
    threading.Thread(target=_watch, args=(parent,), name='dco-orphan-guard', daemon=True).start()
    return target(*args, **kwargs)


def install() -> None:
    """Wrap every spawn-context Process's target in `guarded`. Idempotent."""
    cls = context.SpawnProcess
    if getattr(cls, '_dco_orphan_guard', False):
        return
    original = cls.__init__

    @functools.wraps(original)
    def __init__(self: Any, group: Any = None, target: Any = None, *args: Any, **kwargs: Any) -> None:
        if target is not None:
            # A partial of module-level functions pickles by reference, as spawn requires.
            target = functools.partial(guarded, target, os.getpid())
        original(self, group, target, *args, **kwargs)

    cls.__init__ = __init__  # ty: ignore[invalid-assignment]
    cls._dco_orphan_guard = True  # ty: ignore[unresolved-attribute]
