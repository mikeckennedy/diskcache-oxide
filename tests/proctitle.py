"""Name test and benchmark processes so they're easy to spot in Activity Monitor, `top` and `ps`.

Harness only: the library itself never renames a process. Every title starts with PREFIX, so
`pgrep -fl dco-` (or the Activity Monitor search box) lists exactly the processes the suite and
benchmarks started. Without the optional `setproctitle` package this is a no-op.

Never call it in a child created by fork() (os.fork or the multiprocessing fork context):
setproctitle can hang in a child forked from a multi-threaded process. Forked children keep their
parent's title. Spawned children and fresh interpreters are fine.
"""

from __future__ import annotations

import functools
from collections.abc import Callable
from typing import ParamSpec, TypeVar

PREFIX = 'dco-'

P = ParamSpec('P')
R = TypeVar('R')


def set_title(role: str) -> None:
    try:
        import setproctitle
    except ImportError:
        return
    setproctitle.setproctitle(PREFIX + role)


def titled(role: str) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """Decorate a multiprocessing target so the child names itself `dco-<role>` when it starts.

    functools.wraps keeps the target's name and module, so it still pickles by reference for spawn.
    """

    def decorate(target: Callable[P, R]) -> Callable[P, R]:
        @functools.wraps(target)
        def run(*args: P.args, **kwargs: P.kwargs) -> R:
            set_title(role)
            return target(*args, **kwargs)

        return run

    return decorate
