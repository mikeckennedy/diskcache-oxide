# diskcache bugs found while building diskcache-oxide

Bugs and gotchas in [python-diskcache](https://github.com/grantjenks/python-diskcache) 5.6.3, found while writing the diskcache-oxide conformance suite. Each has a standalone repro in `repros/`. The repros exit with 1 when the bug reproduces and print what they observed. Run them with an environment that has `diskcache==5.6.3` installed, e.g. `venv/bin/python upstream/repros/<name>.py` from the repo root.

None of these has been filed yet. Filing them upstream is Michael's call. Where to file: [upstream](https://github.com/grantjenks/python-diskcache/issues) is largely unmaintained; the [diskcache-community](https://github.com/secondwindoss/diskcache-community) fork (as of 2026-10-27) has no issues filed but is actively merging fixes.

Status checked on 2026-10-27, against open and closed issues and PRs in both repos.

| # | Bug | Repro | Upstream | Community fork | diskcache-oxide |
|---|---|---|---|---|---|
| 1 | A top-level NaN reads back as `None` | `nan_reads_back_as_none.py` | not reported | not reported | fixed (D-008) |
| 2 | `expire()` stops after 100 rows that share an expire time | `expire_stops_after_100_shared_expire_time.py` | not reported (nearest: #284) | not reported | fixed (D-010) |
| 3 | `clear()` deletes writes made while it runs | `clear_deletes_concurrent_writes.py` | not reported (#159 is speed only) | not reported | fixed (D-011) |
| 4 | A `size_limit` below the empty-database volume silently evicts everything | `tiny_size_limit_wipes_cache.py` | not reported (related: #140) | not reported | refused with `ValueError` (D-005) |
| 5 | Forking during another thread's write transaction makes the child's writes time out | `fork_during_write_transaction.py` | reported: #300, PR #299 (open), related #325 | fixed (PR #13) | fixed (D-012) |
| 6 | `incr` on an expired or missing key creates a counter with no expiry | `incr_on_expired_key_has_no_expiry.py` | not reported (nearby: #340, PR #371) | not reported | kept for compatibility; documented |
| 7 | A deleted value file reads as a miss, but the row stays (`in` and `len` still see it) | `missing_value_file_leaves_row.py` | not reported (related: #258, #294) | not reported; the fork's `on_error='miss'` deliberately excludes missing files | fixed: a miss, and the row is removed (D-013) |
| 8 | A key that's never deleted sometimes reads as missing while its large value is being overwritten | `overwrite_spilled_value_reads_as_missing.py` | reported: #294 (open); #258 (closed) is the same symptom with threads | not reported; same bug in 5.8.1 | fixed: the read retries (D-016) |
| 9 | `close()` leaves other threads' connections open | `close_leaves_other_threads_connections_open.py` | reported: #295, #336 (fd exhaustion) | fixed (PR #13 closes exited threads' connections) | fixed: pooled connections, all closed by `close()` (D-017, D-018) |
| 10 | Opening a cache with unchanged settings writes to the database | `open_writes_unchanged_settings.py` | not reported (related: #113, read-only caches) | fixed (PR #17) | fixed (D-019) |
| 11 | `iterkeys()` takes quadratic time | `iterkeys_is_quadratic.py` | not reported (Talk Python measured 23 s at 400k keys) | not reported | fixed: keyset-paged range scan, 10 ms at 400k keys (Spike 4a) |
| 12 | A large value set inside a `transact()` that rolls back leaves its file on disk | `rolled_back_transact_leaks_value_file.py` | not checked | not checked | not affected: a rolled-back transaction's files are removed (pinned in plan 17's tests) |
| 13 | `throttle(expire=...)` raises `TypeError` once its bucket expires or is cleared | `throttle_crashes_after_its_bucket_expires.py` | not checked | not checked | fixed: a missing bucket is a full one (D-051) |
| 14 | Every decoration of a throttled function refills the shared bucket | `throttle_decoration_refills_the_bucket.py` | not checked | not checked | fixed: decoration adds the bucket (D-051) |
| 15 | `cull()` under `eviction_policy='none'` reports 0 after removing expired entries | `cull_under_none_reports_zero.py` | not checked | not checked | fixed: the count is returned (D-053) |

## 1. A top-level NaN reads back as None

`cache.set('k', float('nan'))` returns True, and `'k' in cache` is True and `len(cache)` counts it, but `cache.get('k', default)` returns `None`, ignoring the default. A NaN nested in a container (e.g. `[nan]`) round-trips fine. It looks as if top-level floats are stored natively in SQLite, where NaN becomes NULL. That's an inference from behavior; we haven't checked the code. **Impact:** silent data corruption for any float value, and `get` breaks its own contract by ignoring the default.

## 2. expire() stops after 100 rows that share an expire time

With 250 entries that all expired at the same timestamp (e.g. written in one burst, or under a frozen clock), one `expire()` call removes 100 and reports 100. Entries with distinct expire times are all removed. **Impact:** periodic sweeps silently leave expired rows behind under bursty writes. They still count in `len()`/`volume()` until enough sweeps run.

## 3. clear() deletes writes made while it runs

`clear()` keeps deleting in chunks until the cache is empty, so it also deletes entries other writers add while it runs. In one repro, a 50k-entry cache plus a concurrent writer thread led `clear()` to remove 133,075 rows: 76,681 writes that had already succeeded were deleted, and 1 survived. Its running time grows with the write rate. **Impact:** acknowledged writes are lost, and a clear under heavy write load can take far longer than its starting size suggests.

## 4. A tiny size_limit silently evicts everything

An empty database already reports a `volume()` of about 32 KB. With `size_limit=1000`, every `set` returns True and then culls every entry, including the one just stored, so the cache is always empty. There's no error or warning. **Impact:** a misconfiguration (e.g. a limit given in the wrong units) looks like a working cache that never hits.

## 5. Fork during another thread's write transaction

If a process forks while another of its threads is inside a write transaction on the same `Cache`, the child's reads work but its writes raise `Timeout` (after the cache's timeout, 60 s by default), even though the parent commits right after the fork. The child inherits SQLite's in-process lock state. Already reported upstream as #300 and fixed in diskcache-community by PR #13 (a before-fork gate). It also happens when the writing thread uses a different `Cache` object on the same directory than the one the fork goes through (upstream #325): in our Phase 0.5 test a child's write timed out in about half of the runs, and the fork's gate covers that case too. **Impact:** pre-fork servers (gunicorn, Celery prefork) can start workers whose cache writes stall.

## 6. incr on an expired or missing key creates a counter with no expiry

`add(k, 0, expire=60)` followed by `incr(k)` keeps the 60 s expiry. But if the key has expired or been evicted before `incr` runs, `incr` creates a fresh counter with no expiry at all. **Impact:** fixed-window rate limiters leak keys that never expire. This hit Talk Python, which worked around it with `incr(default=None)` and a retry. This may be intended behavior, but it's undocumented and surprising. A doc note, or an `expire=` parameter on `incr`, would help.

## 7. A missing value file leaves an inconsistent row

If a large value's file is removed from disk (by manual cleanup, a disk tool, or a crash between steps), `get` returns the default but the row stays: `in` is True and `len` counts it. Nothing removes the row except an explicit `delete` or `check()`. **Impact:** a "present" key that can never be read; `in`-then-`get` patterns misbehave.

## 8. A large value reads as missing while it's being overwritten

One process overwrites a single key with large values (stored in their own files), and other processes `get` it with a default. The key is never deleted, yet some reads return the default: about 40 in 325,000 reads in the repro, every run, on 3.14 and 3.14t. No torn values or exceptions were seen, and the cache is consistent afterwards. Presumably a reader picks up the old row and then finds that the overwrite has already removed the old file; that's an inference from behavior. The diskcache-community fork (5.8.1) has the same bug. **Impact:** spurious misses on hot, large values, so callers recompute them needlessly, and the cache looks flaky under exactly the load where it matters.

## 9. close() leaves other threads' connections open

diskcache keeps one SQLite connection per thread. `close()` closes only the calling thread's connection, so after 16 worker threads have used a shared `Cache`, `close()` leaves 16 connections (about 2 fds each) open until garbage collection. Garbage collection then reports a `ResourceWarning: unclosed database` for each one. With 64 threads, the Phase 0.5 test saw 130 fds still open after `close()`. While the threads are alive, the count grows with the thread count (131 fds at 64 threads). **Impact:** thread-pool servers leak fds and can hit the open-file limit (upstream #295, #336), and `-W error` test runs fail on the warnings. The diskcache-community fork closes exited threads' connections.

## 10. Opening a cache with unchanged settings writes to the database

Reopening an existing directory with the same settings and only reading still commits a write. Another connection's `PRAGMA data_version` changes, even though the main database file's hash and mtime may not. It looks as if the settings are rewritten on every open; that's an inference from behavior. **Impact:** every worker start takes the write lock, and read-only use of a cache (e.g. on a read-only mount, upstream #113) isn't possible. The diskcache-community fork fixed this (PR #17).

## 11. iterkeys() takes quadratic time

Iterating keys over 20k entries takes 0.05 s; over 80k it takes 0.76 s, 16× the time for 4× the keys. At 400k keys a full `iterkeys()` takes about 26 s. Each page of keys is fetched with a query of the form `WHERE key = ? AND raw = ? OR key > ? ORDER BY key LIMIT 100`. With bound parameters, the `OR` keeps SQLite from using the key index, so every page scans the whole table (about 2M VM steps per page at 400k keys). Note that `EXPLAIN` on the same SQL with literal values reports an index search, which hides the problem. Rewriting the condition as a row-value comparison, `(key, raw) > (?, ?)`, lets SQLite use the index, and a keyset-paged range scan runs in 2.3 ms at 100k keys and 10.2 ms at 400k. **Impact:** any code that scans keys (prefix invalidation, admin pages) slows quadratically as the cache grows. Talk Python measured 1.3 s at 100k and 23 s at 400k keys.

## 12. A rolled-back transaction leaks its large values' files

Inside `with cache.transact():`, a `set` of a large value writes its file at once and remembers its name, but `_transact` removes the remembered files only when the transaction commits (core.py:732-747). If the block raises, the rows roll back and the files stay, with nothing pointing at them. Only `check(fix=True)` would remove them, and only if someone runs it. Found by the architect's review of plan 17 on 2026-11-06; upstream and community-fork status not checked yet. **Impact:** disk use grows with every rolled-back transaction that wrote a large value, invisibly to `volume()` and `size_limit`.

## Not bugs, but worth noting upstream

- `clear()` and `expire()` commit in 100-row chunks, but a writer waiting on a single blocking call (`timeout=1.0`) got in between chunks in only about 3 of 10 runs, because SQLite's busy handler polls rather than queues.
- `incr` on `True` raises `TypeError: can't concat int to bytes` (a bool is stored pickled).
- `pop` on a missing key returns `None` instead of raising like `dict.pop`.
- An empty open cache is falsy because `__len__` is 0. Upstream PR #350 proposes `__bool__`.

## 13. throttle(expire=...) raises TypeError once its bucket expires

`throttle` keeps its token bucket under one key, set when the function is decorated. Each call does `last, tally = cache.get(key)` with no default (recipes.py, `throttle`). Once the row is gone (it expired after an idle gap longer than `expire`, a `clear()` removed it, or culling evicted it), `get` returns `None` and unpacking it raises `TypeError` on every later call. Found while porting the recipes for plan 24 (2026-11-10). **Impact:** a throttled function stops working entirely after a quiet period, or after any cache maintenance.

## 14. Every decoration of a throttled function refills the shared bucket

At decoration `throttle` runs `cache.set(key, (now, count))`, resetting the bucket to full. Every worker process that imports the module decorates the function again, so each one refills the bucket the others share, and a limit of `count` per `seconds` becomes up to `count` per worker start. Found with bug 13. **Impact:** the throttle doesn't limit a multi-process app, which is when it's needed.

## 15. cull() under eviction_policy='none' reports 0 after removing expired entries

`cull()` begins with `count = self.expire(now)`, then, when the policy selects nothing to evict, `return 0` (core.py, `cull`). The expired entries it just removed are gone, but the count says none were. Found by the architect's review of plan 24's release 3 (2026-11-10). **Impact:** a maintenance job that logs or alerts on `cull()`'s count sees nothing happen on a `'none'` cache.

## 16. peek() and peekitem() loop forever when the head's value file is missing

Both read the head in a loop: if the value file can't be read they `continue`, but only expired rows are deleted, so the same row comes back and the call never returns (core.py, `peek` and `peekitem`). `pull` doesn't hang only because it deletes the row first. Found by the architect's review of plan 24's release 4 (2026-11-11); repro: `repros/peek_spins_on_a_missing_file.py`. **Impact:** a value file lost to a crash or a cleanup hangs every consumer that peeks at the queue.

## 17. An ordinary key that shares a queue's prefix sets the queue's next number

`push(prefix='user')` takes the largest string between `'user-000000000000000'` and `'user-999999999999999'` and parses the digits after its last `-`. An ordinary key `'user-9'` is in that range, so the first push returns `'user-000000000000010'`, and because `'user-9'` still sorts last, the second push computes the same key and raises `sqlite3.IntegrityError`. Found with bug 16; repro: `repros/queue_shared_prefix.py`. **Impact:** a queue named after a key family (`user`, `job`) breaks on its second push.

## 18. Nested queue prefixes cross

`'q-1-500000000000000'`, an item of the queue `'q-1'`, lies between `'q-000000000000000'` and `'q-999999999999999'`, so `pull(prefix='q')` takes it. Found with bug 16; repro: `repros/queue_nested_prefixes_cross.py`. **Impact:** consumers of one queue silently take another queue's items.

## 19. On Windows, a value file deleted or overwritten under a reader is leaked

`Disk.remove` suppresses every `OSError` from `os.remove` (core.py, `Disk.remove`: two caches deleting the same file at once is the case it means). On Windows, Python's `open()` doesn't share delete, so while any reader has the value file open (another thread's or process's `get`, or a `get(..., read=True)` handle) the remove fails with `PermissionError`, and the row is deleted or replaced while its file stays with nothing pointing at it. Found by the conformance suite's first Windows run (2026-11-12): `test_spill_files_concurrent_overwrite_and_delete` left 154 value files for 6 live rows. Repro: `repros/windows_delete_under_a_reader_leaks_value_file.py` (Windows only). The community fork (5.8.1) fails the same test on Windows. diskcache-oxide isn't affected (D-058). **Impact:** on Windows, disk use grows with read concurrency, invisibly to `volume()` and `size_limit`, until `check(fix=True)`.
