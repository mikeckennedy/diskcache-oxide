"""A cache whose directory holds another format opens in `diskcache-oxide-format-N` (plan 26, D-059)."""

from __future__ import annotations

import logging
import os
import pickle
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from diskcache_oxide import Cache, Caches, FanoutCache, LayoutMismatchError

SUB = 'diskcache-oxide-format-3'


def _restamp(databases: list[Path], version: int) -> None:
    """Sets `user_version` on each database from another process, as an older or newer build would
    have left it. A subprocess, so closing its connections can't drop this process's SQLite locks."""
    script = (
        'import sqlite3, sys\n'
        'for path in sys.argv[2:]:\n'
        '    con = sqlite3.connect(path)\n'
        '    con.execute(f"PRAGMA user_version = {sys.argv[1]}")\n'
        '    con.execute("PRAGMA wal_checkpoint(TRUNCATE)")\n'
        '    con.close()\n'
    )
    subprocess.run([sys.executable, '-c', script, str(version), *map(str, databases)], check=True)


def _old_cache(directory: Path, version: int = 2) -> None:
    with Cache(directory) as cache:
        cache['old'] = 'value'
    _restamp([directory / 'diskcache-oxide.sqlite'], version)


def _old_fanout(root: Path, shards: int, version: int = 2) -> None:
    FanoutCache(root, shards=shards).close()
    _restamp([root / f'{i:03}' / 'diskcache-oxide.sqlite' for i in range(shards)], version)


def _records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == 'diskcache_oxide' and r.levelno == logging.WARNING]


def test_a_cache_on_an_older_format_opens_in_the_subdirectory_and_warns_once(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    root = tmp_path / 'c'
    _old_cache(root)
    with caplog.at_level(logging.WARNING, logger='diskcache_oxide'):
        with Cache(root) as cache:
            assert Path(cache.directory) == root / SUB
            assert SUB in repr(cache)
            assert 'old' not in cache, 'the old cache is never read'
            cache['new'] = 1
        with Cache(root) as again:
            assert again['new'] == 1, 'later opens use the same subdirectory'
    [record] = _records(caplog)
    message = record.getMessage()
    assert 'older diskcache-oxide' in message and str(root / SUB) in message, message
    assert 'remove_old_formats()' in message, message
    extra = tuple(getattr(record, k) for k in ('directory', 'using', 'found'))
    assert extra == (str(root), str(root / SUB), 'format 2')


def test_a_newer_format_says_to_upgrade(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    root = tmp_path / 'c'
    _old_cache(root, version=999)
    with caplog.at_level(logging.WARNING, logger='diskcache_oxide'), Cache(root) as cache:
        assert Path(cache.directory) == root / SUB
    [record] = _records(caplog)
    assert 'newer diskcache-oxide' in record.getMessage() and 'upgrade' in record.getMessage()


def test_another_directory_warns_again(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    for name in 'ab':
        _old_cache(tmp_path / name)
    with caplog.at_level(logging.WARNING, logger='diskcache_oxide'):
        for name in 'abab':
            Cache(tmp_path / name).close()
    assert len(_records(caplog)) == 2


def test_a_new_process_warns_while_the_old_files_remain(tmp_path: Path) -> None:
    # The once-per-process dedupe means a restart repeats the warning, worded for a cache already
    # in the subdirectory, until the old files go.
    root = tmp_path / 'c'
    _old_cache(root)
    Cache(root).close()
    script = (
        'import logging, sys\n'
        'logging.basicConfig(level=logging.WARNING, format="%(message)s", stream=sys.stdout)\n'
        'from diskcache_oxide import Cache\n'
        'print(Cache(sys.argv[1]).directory)\n'
    )
    run = subprocess.run([sys.executable, '-c', script, str(root)], check=True, capture_output=True, text=True)
    assert 'still holds a cache made by an older diskcache-oxide' in run.stdout, run.stdout
    assert run.stdout.strip().endswith(str(root / SUB))

    for path in root.glob('diskcache-oxide.sqlite*'):
        path.unlink()
    run = subprocess.run([sys.executable, '-c', script, str(root)], check=True, capture_output=True, text=True)
    assert run.stdout.strip() == str(root / SUB), 'sticky, and quiet once the old files are gone'


def test_a_pickled_cache_resolves_afresh(tmp_path: Path) -> None:
    root = tmp_path / 'c'
    _old_cache(root)
    with Cache(root) as cache:
        cache['k'] = 'v'
        copy = pickle.loads(pickle.dumps(cache))
    try:
        assert Path(copy.directory) == root / SUB
        assert copy['k'] == 'v'
    finally:
        copy.close()


@pytest.mark.filterwarnings(r'ignore:This process .* is multi-threaded:DeprecationWarning')
@pytest.mark.skipif(not hasattr(os, 'fork'), reason='needs fork()')
def test_a_redirected_cache_works_across_fork(tmp_path: Path) -> None:
    root = tmp_path / 'c'
    _old_cache(root)
    with Cache(root) as cache:
        pid = os.fork()
        if pid == 0:  # pragma: no cover - the child
            code = 0
            try:
                cache['child'] = os.getpid()
            except BaseException:
                code = 1
            os._exit(code)
        _, status = os.waitpid(pid, 0)
        assert os.waitstatus_to_exitcode(status) == 0
        assert cache['child'] == pid


@pytest.mark.filterwarnings(r'ignore:This process .* is multi-threaded:DeprecationWarning')
@pytest.mark.skipif(not hasattr(os, 'fork'), reason='needs fork()')
def test_the_warning_never_hangs_a_child_forked_while_threads_report(tmp_path: Path) -> None:
    # The dedupe holds no lock a thread could own at fork() (plan 26, H3). On 3.14t threads really
    # run at once; a child that hung would never exit and the wait would time out.
    roots = [tmp_path / f'c{i}' for i in range(16)]
    for root in roots:
        _old_cache(root)
    child_root = tmp_path / 'child'
    _old_cache(child_root)

    def open_all(part: list[Path]) -> None:
        for root in part:
            Cache(root).close()

    threads = [threading.Thread(target=open_all, args=(roots[i::4],)) for i in range(4)]
    for t in threads:
        t.start()
    pid = os.fork()
    if pid == 0:  # pragma: no cover - the child
        code = 0
        try:
            Cache(child_root).close()
        except BaseException:
            code = 1
        os._exit(code)
    for t in threads:
        t.join(30)
    # Bounded by pytest's timeout: a hung child fails the test rather than the run.
    _, status = os.waitpid(pid, 0)
    assert os.waitstatus_to_exitcode(status) == 0


def test_a_fanout_root_with_old_shards_opens_in_the_subdirectory(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    root = tmp_path / 'f'
    _old_fanout(root, shards=2)
    with caplog.at_level(logging.WARNING, logger='diskcache_oxide'), FanoutCache(root) as cache:
        assert Path(cache.directory) == root / SUB
        assert cache.shards == 8, 'a new root, with the default count'
        cache['k'] = 'v'
    [record] = _records(caplog)
    assert 'format 2' in record.getMessage()


def test_a_plain_cache_on_an_old_shard_is_still_a_layout_error(tmp_path: Path) -> None:
    root = tmp_path / 'f'
    _old_fanout(root, shards=2)
    with pytest.raises(LayoutMismatchError):
        Cache(root / '000')
    assert not (root / '000' / SUB).exists()


def test_the_registry_redirects_an_old_fanout_root_whose_count_would_refuse_its_settings(tmp_path: Path) -> None:
    # Two connections don't split over the old root's 16 shards, which used to raise
    # StoredShardsRefuseSettings; the root is old, so its count no longer matters (plan 26).
    _old_fanout(tmp_path / 'f', shards=16)
    reported: list[str] = []
    caches = Caches(tmp_path, on_open_error=lambda name, exc: reported.append(name))
    caches.register('f', shards=2, max_connections=2)
    try:
        cache = caches.fanout('f')
        assert isinstance(cache, FanoutCache)
        assert Path(cache.directory) == tmp_path / 'f' / SUB
        assert cache.shards == 2
        assert reported == []
    finally:
        caches.close_all()


# -- remove_old_formats() ---------------------------------------------------------------------------


def test_remove_old_formats_removes_the_old_cache_and_keeps_the_rest(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    root = tmp_path / 'c'
    _old_cache(root)
    (root / 'notes.txt').write_text('mine')
    with Cache(root) as cache:
        cache['k'] = 'v'
        with caplog.at_level(logging.INFO, logger='diskcache_oxide'):
            removed = cache.remove_old_formats()
        assert str(root / 'diskcache-oxide.sqlite') in removed
        assert sorted(p.name for p in root.iterdir()) == [SUB, 'notes.txt']
        assert cache['k'] == 'v', 'the cache in use is untouched'
        assert cache.remove_old_formats() == [], 'nothing left to do'
    assert any(r.levelno == logging.INFO and 'removed old-format caches' in r.getMessage() for r in caplog.records)
    # The cache stays in the subdirectory: moving back to the root would be a cold start.
    with Cache(root) as cache:
        assert Path(cache.directory) == root / SUB
        assert cache['k'] == 'v'


def test_remove_old_formats_never_removes_a_newer_format(tmp_path: Path) -> None:
    root = tmp_path / 'c'
    _old_cache(root, version=999)
    with Cache(root) as cache:
        assert cache.remove_old_formats() == []
    assert (root / 'diskcache-oxide.sqlite').exists()


def test_remove_old_formats_on_a_fanout_root(tmp_path: Path) -> None:
    root = tmp_path / 'f'
    _old_fanout(root, shards=3)
    with FanoutCache(root) as cache:
        removed = cache.remove_old_formats()
    assert sorted(p.name for p in root.iterdir()) == [SUB]
    names = [Path(p).name for p in removed if Path(p).parent == root]
    assert names == ['002', '001', '000', 'diskcache-oxide-fanout'], removed


def test_remove_old_formats_on_a_null_cache_and_a_registry(tmp_path: Path) -> None:
    from diskcache_oxide import NullCache

    assert NullCache().remove_old_formats() == []
    _old_cache(tmp_path / 'a')
    caches = Caches(tmp_path)
    caches.register('a')
    caches.register('b')
    try:
        result = caches.remove_old_formats()
        assert set(result) == {'a', 'b'}
        assert str(tmp_path / 'a' / 'diskcache-oxide.sqlite') in (result['a'] or [])
        assert result['b'] == []
    finally:
        caches.close_all()


def test_remove_old_formats_with_nothing_old_returns_nothing_and_logs_nothing(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with Cache(tmp_path / 'c') as cache, caplog.at_level(logging.INFO, logger='diskcache_oxide'):
        cache['k'] = 'v'
        assert cache.remove_old_formats() == []
    assert [r for r in caplog.records if r.name == 'diskcache_oxide'] == []


@pytest.mark.skipif(sys.platform == 'win32', reason='Windows refuses to delete an open file')
def test_remove_old_formats_does_not_wait_for_another_process(tmp_path: Path) -> None:
    # No check for other processes (Michael's call): a holder of the old file doesn't stop it. Its
    # connection keeps the unlinked file; the docs warn what that means for an old worker.
    root = tmp_path / 'c'
    _old_cache(root)
    script = (
        'import sqlite3, sys\n'
        'con = sqlite3.connect(sys.argv[1])\n'
        'con.execute("SELECT count(*) FROM sqlite_master").fetchone()\n'
        'print("ready", flush=True)\n'
        'sys.stdin.readline()\n'
        'print(con.execute("PRAGMA user_version").fetchone()[0], flush=True)\n'
    )
    holder = subprocess.Popen(
        [sys.executable, '-c', script, str(root / 'diskcache-oxide.sqlite')],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None and holder.stdin is not None
        assert holder.stdout.readline().strip() == 'ready'
        with Cache(root) as cache:
            assert str(root / 'diskcache-oxide.sqlite') in cache.remove_old_formats()
        assert not (root / 'diskcache-oxide.sqlite').exists()
        out, _ = holder.communicate('go\n', timeout=30)
        assert out.strip() == '2', 'the holder still reads the unlinked file'
    finally:
        holder.kill()
