"""The warning categories `check()` reports with, as diskcache names them (plan 24, release 2)."""


class UnknownFileWarning(UserWarning):
    """`check()` found a file in a cache directory that no entry names (removed with `fix=True` when
    it's in a value folder and over an hour old)."""


class EmptyDirWarning(UserWarning):
    """diskcache's warning for an empty folder. Kept for code that imports it: `check()` never issues
    it, since empty value folders are normal on an open cache (`check(fix=True)` sweeps them)."""
