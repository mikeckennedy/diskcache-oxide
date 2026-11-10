"""Type aliases shared by the stubs, the protocols and `NullCache`."""

type Key = str | bytes | int | float | bool | None | tuple[Key, ...]
"""A cache key: `str`, `bytes`, `int` (64-bit), `float` (not NaN), `bool`, `None`, or a tuple of those,
nested up to 32 deep. Only the exact types; keys compare by exact type, so `1`, `1.0` and `True` are
three keys."""
