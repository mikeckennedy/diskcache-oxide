"""Backend adapters for the conformance suite.

Tests never touch a backend's internals directly. Anything backend-specific (the SQLite filename,
which files are value files, the empty-database volume, options only one backend understands)
goes through these adapters, so the same test body runs against diskcache and diskcache-oxide.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any

import pytest

DISKCACHE_VERSION = '5.6.3'
COMMUNITY_VERSION = '5.8.1'

# Options only diskcache-oxide understands. Dropped when talking to diskcache.
OXIDE_ONLY_OPTIONS = frozenset({'pickle', 'serializer', 'clock'})

# The phase (plans/00-roadmap.md) each optional feature lands in. `requires(...)` must name one of
# these; oxide skips the features from later phases than OXIDE_PHASE. The diskcache backend
# supports everything.
PHASE_ORDER = ('2', '2.5', '3', '4', '5', '6a', '6b', '6c', '6d', '6e', '7', '8')
OXIDE_PHASE = '2'
FEATURE_PHASES: dict[str, str] = {
    'cull_limit': '2',
    'eviction_policy': '6a',
    'tags': '6a',
    'statistics': '6a',
    'memoize': '6a',
    'key_types': '6a',
    'retry': '6a',
    'reversed': '6a',
    'pop_expire_time': '6a',
    'pickling': '6a',
    'reset': '6a',
    'peek_pull_push': '6a',
    'read': '6a',
    'check': '6a',
    'transact': '6b',
    'fanout': '6c',
    'deque_index': '6d',
    'recipes': '6e',
}
assert set(FEATURE_PHASES.values()) <= set(PHASE_ORDER)
# Features that landed ahead of their phase, by name: bumping OXIDE_PHASE to 6b would turn on all of 6a.
OXIDE_EARLY_FEATURES = frozenset(
    {
        'transact',
        'fanout',
        'key_types',
        'retry',
        'memoize',
        'reversed',
        'pop_expire_time',
        'pickling',
        'reset',
        'statistics',
    }
)
assert OXIDE_EARLY_FEATURES <= set(FEATURE_PHASES)
OXIDE_SUPPORTED_FEATURES: frozenset[str] = (
    frozenset(f for f, phase in FEATURE_PHASES.items() if PHASE_ORDER.index(phase) <= PHASE_ORDER.index(OXIDE_PHASE))
    | OXIDE_EARLY_FEATURES
)

# Values of 64 KiB or more spill to their own file (04-design.md section 4, Spike 4a).
OXIDE_SPILL_THRESHOLD = 65_536

# The `max_connections` default: the connections pooled per database file, shared by every handle
# on it (04-design.md section 7). The fd tests use the default.
OXIDE_POOL_SIZE = 8
# Each pooled connection holds the database and its WAL open; every connection shares one -shm, and
# the per-file store keeps one fd pinned on the database while any handle is open.
OXIDE_FD_BUDGET = 2 * OXIDE_POOL_SIZE + 2


class Backend:
    name: str
    timeout_error: type[BaseException]
    # Raised by a pickle=False handle reading a pickled row (D-001). diskcache has no such error.
    unsafe_value_error: type[BaseException] | None = None
    db_filename: str
    # Values of at least this many bytes are stored in their own file instead of the database row.
    spill_threshold: int
    # True when the backend reads time via `time.time()`, so the fake clock monkeypatches it;
    # False when the clock is passed in with `clock=`.
    patches_time_time: bool = False
    # The imported backend package (diskcache, diskcache_oxide, ...).
    _module: Any

    def recipe(self, name: str) -> Any:
        """A recipe or constant from the backend's package (`Lock`, `throttle`, `ENOVAL`, ...)."""
        return getattr(self._module, name)

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

    def make_fanout_cache(
        self,
        directory: Path,
        *,
        shards: int = 8,
        timeout: float = 60.0,
        size_limit: int | None = None,
        clock: Callable[[], float] | None = None,
        **opts: Any,
    ) -> Any:
        """A FanoutCache with `shards` shards. `size_limit` is the whole cache's, which both backends split."""
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

    def fd_budget(self, threads: int) -> int:
        """The most fds inside the cache directory one shared Cache may hold with `threads` live threads using it."""
        raise NotImplementedError

    def hold_write_transaction(self, cache: Any) -> AbstractContextManager[Any]:
        """A context manager that holds `cache`'s own write transaction open until it exits.

        Operations the holding thread makes on `cache` inside it join that transaction, and it
        commits on exit. Other threads and processes are locked out of writing meanwhile.
        """
        raise NotImplementedError

    def fanout_transact(self, cache: Any, key: str) -> AbstractContextManager[Any]:
        """A FanoutCache transaction that covers `key`'s shard (stock: every shard, D-040).

        Calls on the entering thread for keys on that shard join it; it commits on exit and rolls
        back on an exception. The `as` target differs (stock yields None), so tests use the cache.
        """
        raise NotImplementedError

    def sqlite_info(self) -> dict[str, Any] | None:
        """Which SQLite the backend runs on, or None if it can't say.

        Keys: `runtime_version` (the library actually loaded), `bundled` (True when the backend
        ships its own SQLite), and for a bundled build `compile_version` and `compile_options`.
        For oxide, `BUNDLED_SQLITE_VERSION` must be the compile-time `SQLITE_VERSION` macro of the
        build it was compiled against, and `sqlite_version()` must call the runtime
        `sqlite3_libversion()`. If the extension ever links the system library instead of its
        bundled one, the two disagree, which is what test_oxide_uses_its_bundled_sqlite checks.
        """
        return None


class DiskcacheBackend(Backend):
    name = 'diskcache'
    module_name = 'diskcache'
    db_filename = 'cache.db'
    # Observed, not read from source: a 32767-byte str or bytes value stays in the row, and a
    # 32768-byte one adds a file to spill_files().
    spill_threshold = 32_768
    patches_time_time = True
    expected_version = DISKCACHE_VERSION

    def __init__(self) -> None:
        self._module = importlib.import_module(self.module_name)
        self.timeout_error = self._module.Timeout
        if self._module.__version__ != self.expected_version:
            raise ImportError(
                f'the conformance suite expects {self.module_name} {self.expected_version}, found '
                f'{self._module.__version__}'
            )

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

    def make_fanout_cache(
        self,
        directory: Path,
        *,
        shards: int = 8,
        timeout: float = 60.0,
        size_limit: int | None = None,
        clock: Callable[[], float] | None = None,
        **opts: Any,
    ) -> Any:
        # The clock is the fake_clock fixture's monkeypatched time.time, as for make_cache.
        kwargs: dict[str, Any] = {k: v for k, v in opts.items() if k not in OXIDE_ONLY_OPTIONS}
        if size_limit is not None:
            kwargs['size_limit'] = size_limit
        return self._module.FanoutCache(str(directory), shards=shards, timeout=timeout, **kwargs)

    def supports(self, feature: str) -> bool:
        return True

    def fd_budget(self, threads: int) -> int:
        # Observed: one connection per thread that has used the cache, plus the opening thread's,
        # each holding the database and its WAL, plus one shared -shm (64 threads -> 131 fds).
        return 2 * (threads + 1) + 1

    def hold_write_transaction(self, cache: Any) -> AbstractContextManager[Any]:
        return cache.transact()

    def fanout_transact(self, cache: Any, key: str) -> AbstractContextManager[Any]:
        # Stock's FanoutCache.transact() locks every shard in order and requires retry=True.
        return cache.transact(retry=True)

    def sqlite_info(self) -> dict[str, Any] | None:
        # diskcache runs on the stdlib sqlite3 module, i.e. whatever SQLite this Python links.
        import sqlite3

        return {'runtime_version': sqlite3.sqlite_version, 'bundled': False}


class CommunityBackend(DiskcacheBackend):
    """The diskcache-community fork (https://github.com/secondwindoss/diskcache-community), a second witness.

    It keeps 5.6.3's semantics and on-disk format; where it differs, the suite reports a failure and
    the difference is listed in KNOWN_COMMUNITY_DIFFERENCES (a strict xfail on this backend only)
    and in DIVERGENCES.md.
    """

    name = 'community'
    module_name = 'diskcache_community'
    expected_version = COMMUNITY_VERSION


class OxideBackend(Backend):
    name = 'oxide'
    db_filename = 'diskcache-oxide.sqlite'
    spill_threshold = OXIDE_SPILL_THRESHOLD

    def __init__(self) -> None:
        self._module = importlib.import_module('diskcache_oxide')
        self.timeout_error = self._module.Timeout
        self.unsafe_value_error = self._module.UnsafeValueError

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

    def make_fanout_cache(
        self,
        directory: Path,
        *,
        shards: int = 8,
        timeout: float = 60.0,
        size_limit: int | None = None,
        clock: Callable[[], float] | None = None,
        **opts: Any,
    ) -> Any:
        if not self.supports('fanout'):
            raise NotImplementedError('oxide does not support FanoutCache yet; mark the test requires("fanout")')
        kwargs: dict[str, Any] = dict(opts)
        if size_limit is not None:
            kwargs['size_limit'] = size_limit
        if clock is not None:
            kwargs['clock'] = clock
        return self._module.FanoutCache(str(directory), timeout=timeout, shards=shards, **kwargs)

    def supports(self, feature: str) -> bool:
        return feature in OXIDE_SUPPORTED_FEATURES

    def fd_budget(self, threads: int) -> int:
        # Bounded by the pool, whatever the thread count (D-017).
        return OXIDE_FD_BUDGET

    def hold_write_transaction(self, cache: Any) -> AbstractContextManager[Any]:
        return cache.transact()

    def fanout_transact(self, cache: Any, key: str) -> AbstractContextManager[Any]:
        return cache.transact(key=key)

    def sqlite_info(self) -> dict[str, Any] | None:
        mod = self._module
        return {
            'runtime_version': mod.sqlite_version(),
            'compile_version': mod.BUNDLED_SQLITE_VERSION,
            'compile_options': mod.sqlite_compile_options(),
            'bundled': True,
        }


BACKENDS: dict[str, type[Backend]] = {
    'diskcache': DiskcacheBackend,
    'community': CommunityBackend,
    'oxide': OxideBackend,
}

# Where the community fork (5.8.1) fails a test written against 5.6.3, keyed by test node id. A key
# with no `[...]` part also matches every parametrized case of that test. conftest.py turns each
# match into a strict xfail (raises=AssertionError) on the community backend only, and
# test_registry.py checks every id still exists and every D-number is described in the
# "Community fork differences" section of DIVERGENCES.md.
KNOWN_COMMUNITY_DIFFERENCES: dict[str, str] = {
    'tests/conformance/test_basic.py::test_new_directory_follows_the_umask': 'D-015',
    'tests/conformance/test_threads.py::test_peak_fds_bounded_by_the_pool': 'D-017',
    'tests/conformance/test_values.py::test_value_roundtrips[float-nan]': 'D-008',
    'tests/conformance/test_values.py::test_value_roundtrips_across_reopen[float-nan]': 'D-008',
    'tests/conformance/test_fanout.py::test_peak_fds_bounded_by_the_shard_budget': 'D-037',
    'tests/conformance/test_fanout.py::test_a_write_to_a_locked_shard_raises_timeout[set]': 'D-034',
    'tests/conformance/test_fanout.py::test_a_write_to_a_locked_shard_raises_timeout[add]': 'D-034',
    'tests/conformance/test_fanout.py::test_a_write_to_a_locked_shard_raises_timeout[incr]': 'D-034',
    'tests/conformance/test_fanout.py::test_a_write_to_a_locked_shard_raises_timeout[delete]': 'D-034',
    'tests/conformance/test_fanout.py::test_a_write_to_a_locked_shard_raises_timeout[pop]': 'D-034',
    'tests/conformance/test_fanout.py::test_a_write_to_a_locked_shard_raises_timeout[touch]': 'D-034',
    'tests/conformance/test_fanout.py::test_a_write_to_another_shard_doesnt_wait_for_a_transaction': 'D-040',
}


def community_difference(nodeid: str) -> str | None:
    """The D-number KNOWN_COMMUNITY_DIFFERENCES lists for this node id, if any."""
    if nodeid in KNOWN_COMMUNITY_DIFFERENCES:
        return KNOWN_COMMUNITY_DIFFERENCES[nodeid]
    base = nodeid.split('[', 1)[0]
    return KNOWN_COMMUNITY_DIFFERENCES.get(base) if base != nodeid else None


# Where tests/conftest.py stores the selected backend instance for the session.
BACKEND_KEY = pytest.StashKey[Backend]()
