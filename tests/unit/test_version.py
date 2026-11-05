"""The version lives only in Cargo.toml: maturin derives the wheel's from it, and the extension
derives `__version__`. Both must give the same PEP 440 string, or pip and `__version__` would name
different builds."""

from __future__ import annotations

import importlib.metadata

import diskcache_oxide


def test_version_matches_the_installed_metadata() -> None:
    assert diskcache_oxide.__version__ == importlib.metadata.version('diskcache-oxide')
