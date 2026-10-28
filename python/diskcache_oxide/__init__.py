"""diskcache-oxide: a clean-room Rust reimplementation of diskcache.

The cache itself lives in the compiled `diskcache_oxide._core` extension. This package re-exports
it, so `diskcache_oxide.Timeout` is the public name and the one exceptions pickle under.
"""

from diskcache_oxide._core import (
    BUNDLED_SQLITE_VERSION,
    Cache,
    Timeout,
    UnsafeValueError,
    __version__,
    sqlite_compile_options,
    sqlite_version,
)

__all__ = [
    'BUNDLED_SQLITE_VERSION',
    'Cache',
    'Timeout',
    'UnsafeValueError',
    '__version__',
    'sqlite_compile_options',
    'sqlite_version',
]
