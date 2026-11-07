"""The value-folder sweep through Python's entry points (plan 17, D-042)."""

from __future__ import annotations

import gc
import os
import time
from pathlib import Path

from diskcache_oxide import Cache, FanoutCache

BIG = b'x' * 70_000


def _backdate(directory: Path) -> None:
    then = time.time() - 60
    for path in directory.rglob('*'):
        if path.is_dir():
            os.utime(path, (then, then))


def _empty_dirs(directory: Path) -> list[Path]:
    return [p for p in directory.rglob('*') if p.is_dir() and not any(p.iterdir())]


def test_a_handle_dropped_without_close_sweeps(tmp_path: Path) -> None:
    directory = tmp_path / 'c'
    cache = Cache(str(directory))
    for i in range(8):
        cache.set(f'k{i}', BIG)
    for i in range(8):
        cache.delete(f'k{i}')
    _backdate(directory)
    del cache
    gc.collect()
    assert _empty_dirs(directory) == []


def test_a_fanout_cache_writes_value_files_one_level_deep_inside_each_shard(tmp_path: Path) -> None:
    directory = tmp_path / 'f'
    with FanoutCache(str(directory), shards=4) as cache:
        for i in range(16):
            cache.set(f'k{i}', BIG)
        files = [p.relative_to(directory) for p in directory.rglob('*.val')]
    assert len(files) == 16
    for relative in files:
        shard, folder, _ = relative.parts
        assert len(shard) == 3 and shard.isdigit(), relative
        assert len(folder) == 2 and int(folder, 16) >= 0, relative
    assert not [p for p in directory.iterdir() if p.is_dir() and len(p.name) == 2], 'the root holds shards only'
