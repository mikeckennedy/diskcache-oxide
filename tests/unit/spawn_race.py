"""Racing spawned processes at a barrier, for the cross-process creation tests.

Each target is called as `target(*args, barrier, results)`: it waits on `barrier`, does its work,
and puts exactly one `(outcome, detail)` tuple on `results`, `('crash', traceback)` for anything
unexpected. Targets must live in an importable module, since `spawn` pickles them by name.
"""

from __future__ import annotations

import multiprocessing
from typing import Any

SPAWN = multiprocessing.get_context('spawn')


def race(targets: list[tuple[Any, tuple[Any, ...]]]) -> list[tuple[str, Any]]:
    """Starts every target at once and returns their outcomes, in the order they arrived."""
    barrier = SPAWN.Barrier(len(targets))
    results = SPAWN.Queue()
    procs = [SPAWN.Process(target=t, args=(*args, barrier, results)) for t, args in targets]
    for p in procs:
        p.start()
    outcomes = [results.get(timeout=60) for _ in procs]
    for p in procs:
        p.join(30)
    assert [p.exitcode for p in procs] == [0] * len(procs)
    assert not [o for o in outcomes if o[0] == 'crash'], outcomes
    return outcomes
