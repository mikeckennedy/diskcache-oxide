"""What a forked child can do with a cache, by what its parent had open at the fork (plan 32, D-020).

Each scenario runs in a fresh process (tests/unit/fork_inherited_scenarios.py), never in pytest
itself. The fork no before-hook sees is tested in the core, where a raw fork is possible:
crates/diskcache-oxide-core/tests/unhooked_fork.rs.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from tests.platforms import unix_only

REPO_ROOT = Path(__file__).resolve().parents[2]


@unix_only('fork')
@pytest.mark.parametrize(
    'scenario',
    [
        'later-before-hook-uses-the-cache',
        'later-before-hook-opens-a-cache',
        'fork-inside-a-transaction',
        'fork-from-a-hook-inside-get',
        'child-returns-from-the-inherited-get',
        'early-child-hook-inside-an-operation',
        'fanout-with-one-shard-held',
    ],
)
def test_fork_scenario(scenario: str, tmp_path: Path) -> None:
    proc = subprocess.run(
        [sys.executable, '-m', 'tests.unit.fork_inherited_scenarios', scenario, str(tmp_path / 'c')],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=50,
    )
    assert proc.returncode == 0, proc.stderr


@unix_only('fork')
def test_the_child_logs_one_warning_naming_the_cache_it_cant_use(tmp_path: Path) -> None:
    proc = subprocess.run(
        [sys.executable, '-m', 'tests.unit.fork_inherited_scenarios', 'fork-inside-a-transaction', str(tmp_path / 'c')],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=50,
    )
    assert proc.returncode == 0, proc.stderr
    warned = [line for line in proc.stderr.splitlines() if 'this process was forked while a connection' in line]
    assert len(warned) == 1, proc.stderr
    assert str(tmp_path / 'c') in warned[0] and 'RuntimeError' in warned[0]
