"""diskcache's sentinels, for code written against diskcache (plan 24, release 2).

`ENOVAL` means "no value" (a `default=` that tells a miss from a stored `None`, `reset(key)` to read)
and `UNKNOWN` an unknown value. Each is a singleton that pickles by reference, so it stays itself
across processes. diskcache's are `tuple` subclasses, which a cache would hand back as plain tuples;
these aren't, and can't be stored as values (they aren't a type the codec knows).
"""


class _Constant:
    __slots__ = ('_name',)

    def __init__(self, name: str) -> None:
        self._name = name

    def __repr__(self) -> str:
        return self._name

    def __reduce__(self) -> str:
        # Pickled by reference: unpickling looks the name up in this module.
        return self._name


ENOVAL = _Constant('ENOVAL')
UNKNOWN = _Constant('UNKNOWN')
