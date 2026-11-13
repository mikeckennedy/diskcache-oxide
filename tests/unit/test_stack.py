"""The codecs recurse, so a value can be too deep for a thread's C stack even within the depth limit.
That has to be a `RecursionError`, never a dead process or a good row read as corrupt."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from tests.platforms import unix_only
from tests.unit.stack_scenarios import SCENARIOS

REPO_ROOT = Path(__file__).resolve().parents[2]
# Windows gives every thread its executable's stack (2 MB for python.exe): threading.stack_size only
# sets how much is committed up front. The others pass there without a small stack to stand on.
SMALL_STACK_ONLY = unix_only('a small thread stack; threading.stack_size does not shrink one on Windows')


@pytest.mark.parametrize(
    'scenario', [pytest.param(s, marks=SMALL_STACK_ONLY) if s == 'the-deepest-row' else s for s in sorted(SCENARIOS)]
)
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
