"""The Windows stress test (plan 25, decision 2): `slow`, Windows only. See windows_stress.py.

The release runs it for 2 hours with Defender's real-time protection on:

    venv\\Scripts\\python -m tests.unit.windows_stress --seconds 7200

The slow run here takes DCO_STRESS_SECONDS (default 120).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from tests.unit import windows_stress

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(sys.platform != 'win32', reason='Windows only: the Windows stress test'),
]


@pytest.mark.timeout(0)
def test_processes_killed_while_reading_writing_and_sweeping_lose_no_acknowledged_write(tmp_path: Path) -> None:
    seconds = float(os.environ.get('DCO_STRESS_SECONDS', '120'))
    summary = windows_stress.run(tmp_path, seconds)
    assert not summary['failures'], summary
    assert summary['kills'] > 0 and summary['acked_ledger_writes'] > 0, summary


@pytest.mark.timeout(600)
def test_writers_racing_sweepers_across_processes_never_fail(tmp_path: Path) -> None:
    assert windows_stress.run_sweep_race(tmp_path) == []
