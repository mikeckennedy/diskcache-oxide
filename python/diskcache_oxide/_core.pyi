"""Type stubs for the compiled `diskcache_oxide._core` extension (hand-written, checked by stubtest)."""

from contextlib import AbstractContextManager
from typing import final

# PyO3 generates __all__ from everything the module adds, in the order it adds them.
__all__ = [
    '__version__',
    'BUNDLED_SQLITE_VERSION',
    '_DEBUG_BUILD',
    'Timeout',
    'UnsafeValueError',
    'sqlite_version',
    'sqlite_compile_options',
    'Cache',
]

__version__: str
BUNDLED_SQLITE_VERSION: str
"""The SQLite version the bundled library was compiled from (the `SQLITE_VERSION` macro)."""
_DEBUG_BUILD: bool
"""True for an unoptimized (debug) build of the extension."""

class Timeout(TimeoutError):
    """The database stayed locked for longer than the cache's timeout."""

class UnsafeValueError(TypeError):
    """A pickle=False cache read a value that was stored with pickle."""

def sqlite_version() -> str:
    """The SQLite version reported at runtime by `sqlite3_libversion()`."""

def sqlite_compile_options() -> list[str]:
    """The compile-time options of the linked SQLite (`PRAGMA compile_options`)."""

@final
class Cache:
    """Phase 1 spike: get/set/incr for str, bytes and int only. Replaced by the full Cache in Phase 2."""

    def __new__(cls, directory: str, timeout: float = 60.0, size_limit: int | None = None, *, spike_detach: bool = False) -> Cache: ...
    def get(self, key: str, default: object = None) -> object: ...
    def set(self, key: str, value: str | bytes | int, expire: float | None = None) -> bool: ...
    def incr(self, key: str, delta: int = 1, default: int | None = 0) -> int: ...
    def _hold_write_transaction(self) -> AbstractContextManager[None]:
        """Test hook until transact() lands: holds a write transaction until the block exits."""
    def __len__(self) -> int: ...
    def close(self) -> None: ...
    def __enter__(self) -> Cache: ...
    def __exit__(self, *_args: object) -> None: ...
