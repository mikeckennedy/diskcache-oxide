"""SQLite runs on the extension's own mutex methods, installed when the module loads (plan 12 step 5a), and the
standard library's `sqlite3` (a different SQLite) is untouched. Windows installs nothing and reports 'native': SQLite
runs on its own Win32 mutexes there (plan 25)."""

from __future__ import annotations

import subprocess
import sys

from diskcache_oxide import _core

# What the module init leaves: our methods, or SQLite's own on Windows.
EXPECTED = 'native' if sys.platform == 'win32' else 'installed'


def _run(code: str) -> str:
    result = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, timeout=60, check=True)
    return result.stdout.strip()


def test_the_methods_are_installed_at_import() -> None:
    assert _core._sqlite_mutex_status() == EXPECTED


def test_not_a_spin_sweep_build() -> None:
    # A wheel built with RUSTFLAGS="--cfg dco_spin_sweep" reads DCO_SPIN_HINTS from the environment,
    # a knob that must never ship.
    assert not hasattr(_core, '_SWEEP_SPIN_BUDGET'), 'built with --cfg dco_spin_sweep (the spin sweep build)'


def test_installed_in_a_fresh_process_before_any_cache() -> None:
    # Nothing opens a cache here: the module init alone installs them.
    assert _run('import diskcache_oxide._core as c; print(c._sqlite_mutex_status())') == EXPECTED


def test_stdlib_sqlite3_is_unchanged() -> None:
    before = _run('import sqlite3; print(sqlite3.threadsafety)')
    after = _run(
        'import diskcache_oxide, sqlite3\n'
        'c = sqlite3.connect(":memory:")\n'
        'assert c.execute("select 1 + 1").fetchone() == (2,)\n'
        'print(sqlite3.threadsafety)'
    )
    assert after == before
