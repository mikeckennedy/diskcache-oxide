## Key


A cache key: `str`, `bytes`, `int` (64-bit), `float` (not NaN), `bool`, `None`, or a tuple of those,


`type`` Key = Optional[str | bytes | int | float | bool | tuple[Key, …]]`


nested up to 32 deep. Only the exact types; keys compare by exact type, so `1`, `1.0` and `True` are three keys.
