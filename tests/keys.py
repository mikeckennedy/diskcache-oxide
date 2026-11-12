"""Helpers for tests whose caches hold `str` keys only."""

from collections.abc import Iterable


def str_keys(keys: Iterable[object]) -> list[str]:
    """`keys` as a list, checking that every one is a `str` (iteration is typed for every key type)."""
    out: list[str] = []
    for key in keys:
        assert isinstance(key, str), f'expected only str keys, got {key!r}'
        out.append(key)
    return out
