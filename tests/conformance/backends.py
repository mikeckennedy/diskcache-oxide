"""Backend adapters for the conformance suite.

Tests never touch a backend's internals directly. Anything backend-specific (the SQLite filename,
which files are value files, the empty-database volume, options only one backend understands)
goes through these adapters, so the same test body runs against diskcache and diskcache-oxide.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

# Options only diskcache-oxide understands. Dropped when talking to diskcache.
OXIDE_ONLY_OPTIONS = frozenset({'pickle', 'serializer', 'clock'})

# Features scheduled for later oxide phases. The diskcache backend supports everything.
OXIDE_SUPPORTED_FEATURES: frozenset[str] = frozenset()

# TODO(Phase 1): placeholder until oxide's storage design fixes its spill threshold.
OXIDE_SPILL_THRESHOLD = 32_768


class Backend:
    name: str
    timeout_error: type[BaseException]
    db_filename: str
    # Values of at least this many bytes are stored in their own file instead of the database row.
    spill_threshold: int

    def make_cache(
        self,
        directory: Path,
        *,
        timeout: float = 60.0,
        size_limit: int | None = None,
        cull_limit: int | None = None,
        clock: Callable[[], float] | None = None,
        **opts: Any,
    ) -> Any:
        raise NotImplementedError

    def db_path(self, directory: Path) -> Path:
        return Path(directory) / self.db_filename

    def spill_files(self, directory: Path) -> set[Path]:
        """Every file in the cache directory that isn't the database or its WAL/SHM files."""
        root = Path(directory)
        return {p for p in root.rglob('*') if p.is_file() and not p.name.startswith(self.db_filename)}

    def empty_volume(self, directory: Path) -> int:
        """The volume() of a freshly created, empty cache in `directory` (which must not exist yet)."""
        with self.make_cache(directory) as cache:
            return int(cache.volume())

    def supports(self, feature: str) -> bool:
        raise NotImplementedError


class DiskcacheBackend(Backend):
    name = 'diskcache'
    db_filename = 'cache.db'
    # Observed, not read from source: a 32767-byte str or bytes value stays in the row, and a
    # 32768-byte one adds a file to spill_files().
    spill_threshold = 32_768

    def __init__(self) -> None:
        self._module = importlib.import_module('diskcache')
        self.timeout_error = self._module.Timeout

    def make_cache(
        self,
        directory: Path,
        *,
        timeout: float = 60.0,
        size_limit: int | None = None,
        cull_limit: int | None = None,
        clock: Callable[[], float] | None = None,
        **opts: Any,
    ) -> Any:
        # The clock is handled by the fake_clock fixture monkeypatching time.time for this backend.
        kwargs: dict[str, Any] = {k: v for k, v in opts.items() if k not in OXIDE_ONLY_OPTIONS}
        kwargs['timeout'] = timeout
        if size_limit is not None:
            kwargs['size_limit'] = size_limit
        if cull_limit is not None:
            kwargs['cull_limit'] = cull_limit
        return self._module.Cache(str(directory), **kwargs)

    def supports(self, feature: str) -> bool:
        return True


class OxideBackend(Backend):
    name = 'oxide'
    db_filename = 'diskcache-oxide.sqlite'
    spill_threshold = OXIDE_SPILL_THRESHOLD

    def __init__(self) -> None:
        self._module = importlib.import_module('diskcache_oxide')
        self.timeout_error = self._module.Timeout

    def make_cache(
        self,
        directory: Path,
        *,
        timeout: float = 60.0,
        size_limit: int | None = None,
        cull_limit: int | None = None,
        clock: Callable[[], float] | None = None,
        **opts: Any,
    ) -> Any:
        kwargs: dict[str, Any] = dict(opts)
        kwargs['timeout'] = timeout
        if size_limit is not None:
            kwargs['size_limit'] = size_limit
        if cull_limit is not None:
            # Never drop it silently: a test that sets cull_limit must carry requires('cull_limit').
            if not self.supports('cull_limit'):
                raise NotImplementedError('oxide does not support cull_limit yet; mark the test requires("cull_limit")')
            kwargs['cull_limit'] = cull_limit
        if clock is not None:
            kwargs['clock'] = clock
        return self._module.Cache(str(directory), **kwargs)

    def supports(self, feature: str) -> bool:
        return feature in OXIDE_SUPPORTED_FEATURES


BACKENDS: dict[str, type[Backend]] = {
    'diskcache': DiskcacheBackend,
    'oxide': OxideBackend,
}

# Where tests/conftest.py stores the selected backend instance for the session.
BACKEND_KEY = pytest.StashKey[Backend]()
