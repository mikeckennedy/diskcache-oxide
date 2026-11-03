"""The codecs recurse, so a value can be too deep for a thread's C stack even within the depth limit.
That has to be a `RecursionError`, never a dead process or a good row read as corrupt."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from tests.unit.stack_scenarios import SCENARIOS

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize('scenario', sorted(SCENARIOS))
def test_scenario(scenario: str, tmp_path: Path) -> None:
    proc = subprocess.run(
        [
            sys.executable,
            '-m',
            'tests.unit.stack_scenarios',
            scenario,
            str(tmp_path / 's'),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=50,
    )
    assert proc.returncode == 0, f'{proc.stdout}\n{proc.stderr}'
