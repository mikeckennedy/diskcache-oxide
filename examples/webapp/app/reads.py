"""The request strip: which cache reads served this page, hit or miss, and how long each took.

Not something a production app needs. It's here so you can watch the cache work.
"""

from __future__ import annotations

import time
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from diskcache_oxide import CacheLike


@dataclass(frozen=True)
class Read:
    cache: str
    key: str
    hit: bool
    seconds: float

    @property
    def time(self) -> str:
        if self.seconds >= 0.001:
            return f'{self.seconds * 1000:.0f} ms'
        return f'{self.seconds * 1_000_000:.1f} µs'


_reads: ContextVar[list[Read] | None] = ContextVar('reads', default=None)


def start() -> list[Read]:
    reads: list[Read] = []
    _reads.set(reads)
    return reads


def record(cache: str, key: str, hit: bool, seconds: float) -> None:
    reads = _reads.get()
    if reads is not None:
        reads.append(Read(cache, key, hit, seconds))


def get(cache: CacheLike, name: str, key: str) -> Any:
    """`cache.get(key)`, recorded."""
    started = time.perf_counter()
    value = cache.get(key)
    record(name, key, value is not None, time.perf_counter() - started)
    return value
