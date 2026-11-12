"""Unpickling a `Cache` or `FanoutCache` (plan 24, release 2): reopen the directory with the options
the pickled handle was opened with. Options are passed by name, so a pickle made by one release still
opens in a later one that added options."""

from typing import Any


def rebuild(kind: str, options: dict[str, Any]) -> Any:
    from diskcache_oxide import Cache, FanoutCache

    cls = {'Cache': Cache, 'FanoutCache': FanoutCache}[kind]
    return cls(**options)


def global_registry() -> Any:
    """`diskcache_oxide.registry` in the unpickling process."""
    from diskcache_oxide import registry

    return registry


def rebuild_registry(entries: list[tuple[type, str, Any, Any]]) -> Any:
    """A `TypeRegistry` with these registrations, in name order."""
    from diskcache_oxide import TypeRegistry

    types = TypeRegistry()
    for cls, name, encode, decode in entries:
        types.register(cls, name=name, encode=encode, decode=decode)
    return types
