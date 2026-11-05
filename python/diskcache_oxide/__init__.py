"""diskcache-oxide: a clean-room Rust reimplementation of diskcache.

The cache itself lives in the compiled `diskcache_oxide._core` extension. This package re-exports
it, so `diskcache_oxide.Timeout` is the public name and the one exceptions pickle under.
`NullCache` (a switched-off cache), `CacheLike` (the interface both share) and `Caches` (a registry of
named caches) are pure Python.
"""

from diskcache_oxide._core import (
    BUNDLED_SQLITE_VERSION,
    Cache,
    CorruptValueError,
    ForeignValueError,
    FormatMismatchError,
    Timeout,
    TransactionBusy,
    UnsafeValueError,
    __version__,
    sqlite_compile_options,
    sqlite_version,
)
from diskcache_oxide.caches import Caches, CacheStats
from diskcache_oxide.null import NullCache
from diskcache_oxide.protocol import CacheLike

__all__ = [
    'BUNDLED_SQLITE_VERSION',
    'Cache',
    'CacheLike',
    'CacheStats',
    'Caches',
    'CorruptValueError',
    'ForeignValueError',
    'FormatMismatchError',
    'NullCache',
    'Timeout',
    'TransactionBusy',
    'UnsafeValueError',
    '__version__',
    'sqlite_compile_options',
    'sqlite_version',
]
