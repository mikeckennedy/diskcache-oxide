"""A pending fork can't outlast `timeout` (plan 09, step 1b; D-027).

Every stuck fork runs in a fresh process (tests/unit/fork_timeout_scenarios.py), since the gate is
process-wide, with a subprocess timeout as the watchdog: the wedge these pin hung `cargo test` for 28
minutes.
"""

from __future__ import annotations

import subprocess
import sys
import threading
from pathlib import Path

import pytest
from diskcache_oxide import Cache

from tests.platforms import unix_only
from tests.unit.fork_timeout_scenarios import SCENARIOS, fds_inside

REPO_ROOT = Path(__file__).resolve().parents[2]
GIL_ONLY = {'ticker-gap'}


@unix_only('fork')
@pytest.mark.parametrize('scenario', sorted(SCENARIOS))
def test_stuck_fork_scenario(scenario: str, tmp_path: Path) -> None:
    if scenario in GIL_ONLY and not sys._is_gil_enabled():
        pytest.skip('only a GIL build can see a wait that stays attached this way')
    proc = subprocess.run(
        [
            sys.executable,
            '-m',
            'tests.unit.fork_timeout_scenarios',
            scenario,
            str(tmp_path / 's'),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=50,
    )
    assert proc.returncode == 0, proc.stderr


@unix_only('counts file descriptors; Windows has handles')
def test_racing_reopens_and_closes_keep_the_handle_count(tmp_path: Path) -> None:
    """`ensure_open` loads the closed flag and flips it under a gate level: N threads reopening and
    closing one handle end with the right count, and D-018 clean."""
    directory = tmp_path / 'c'
    keeper = Cache(directory)
    keeper.set('k', 1)
    handle = Cache(directory)
    stop = threading.Event()
    failures: list[BaseException] = []

    def churn(use: bool) -> None:
        try:
            while not stop.is_set():
                if use:
                    assert handle.get('k') == 1
                else:
                    handle.close()
        except BaseException as exc:
            failures.append(exc)
            raise

    threads = [threading.Thread(target=churn, args=(i % 2 == 0,)) for i in range(8)]
    for thread in threads:
        thread.start()
    stop.wait(1.0)
    stop.set()
    for thread in threads:
        thread.join(30.0)
        assert not thread.is_alive()
    assert not failures, failures
    handle.close()
    assert keeper._handle_count() == 1, 'the handle count drifted'
    keeper.close()
    assert fds_inside(directory) == 0, 'D-018: a connection stayed open'
