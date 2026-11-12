"""tests/orphan_guard.py: a spawned child outlives a killed test run by at most a second or so."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]

# A parent that installs the guard, spawns a child that would sleep for ten
# minutes, waits until the child is running, prints its pid and then waits to be killed.
PARENT = """
import multiprocessing, time
from tests import orphan_guard
from tests.unit.test_orphan_guard import nap
orphan_guard.install()
if __name__ == '__main__':
    spawn = multiprocessing.get_context('spawn')
    running = spawn.Event()
    child = spawn.Process(target=nap, args=(running,))
    child.start()
    assert running.wait(30)
    print(child.pid, flush=True)
    time.sleep(600)
"""


def nap(running: Any) -> None:
    running.set()
    time.sleep(600)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_a_spawned_child_exits_when_its_parent_is_killed(tmp_path: Path) -> None:
    script = tmp_path / 'parent.py'
    script.write_text(PARENT)
    parent = subprocess.Popen(
        [sys.executable, str(script)],
        cwd=REPO_ROOT,
        stdout=subprocess.PIPE,
        text=True,
        env={**os.environ, 'PYTHONPATH': str(REPO_ROOT)},
    )
    try:
        assert parent.stdout is not None
        child = int(parent.stdout.readline())
        assert _alive(child)
        parent.send_signal(signal.SIGKILL)
        parent.wait(10)
        deadline = time.monotonic() + 10
        while _alive(child) and time.monotonic() < deadline:
            time.sleep(0.05)
        alive = _alive(child)
        if alive:
            os.kill(child, signal.SIGKILL)
        assert not alive, 'the spawned child outlived its killed parent by 10 s'
    finally:
        if parent.poll() is None:
            parent.kill()
