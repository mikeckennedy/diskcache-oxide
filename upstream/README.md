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
| 7 | A deleted value file reads as a miss, but the row stays (`in` and `len` still see it) | `missing_value_file_leaves_row.py` | not reported (related: #258, #294) | not reported; the fork's `on_error='miss'` deliberately excludes missing files | open decision |

## 1. A top-level NaN reads back as None

`cache.set('k', float('nan'))` returns True, and `'k' in cache` is True and `len(cache)` counts it, but `cache.get('k', default)` returns `None`, ignoring the default. A NaN nested in a container (e.g. `[nan]`) round-trips fine. It looks as if top-level floats are stored natively in SQLite, where NaN becomes NULL. That's an inference from behavior; we haven't checked the code. **Impact:** silent data corruption for any float value, and `get` breaks its own contract by ignoring the default.

## 2. expire() stops after 100 rows that share an expire time

With 250 entries that all expired at the same timestamp (e.g. written in one burst, or under a frozen clock), one `expire()` call removes 100 and reports 100. Entries with distinct expire times are all removed. **Impact:** periodic sweeps silently leave expired rows behind under bursty writes. They still count in `len()`/`volume()` until enough sweeps run.

## 3. clear() deletes writes made while it runs

`clear()` keeps deleting in chunks until the cache is empty, so it also deletes entries other writers add while it runs. In one repro, a 50k-entry cache plus a concurrent writer thread led `clear()` to remove 133,075 rows: 76,681 writes that had already succeeded were deleted, and 1 survived. Its running time grows with the write rate. **Impact:** acknowledged writes are lost, and a clear under heavy write load can take far longer than its starting size suggests.

## 4. A tiny size_limit silently evicts everything

An empty database already reports a `volume()` of about 32 KB. With `size_limit=1000`, every `set` returns True and then culls every entry, including the one just stored, so the cache is always empty. There's no error or warning. **Impact:** a misconfiguration (e.g. a limit given in the wrong units) looks like a working cache that never hits.

## 5. Fork during another thread's write transaction

If a process forks while another of its threads is inside a write transaction on the same `Cache`, the child's reads work but its writes raise `Timeout` (after the cache's timeout, 60 s by default), even though the parent commits right after the fork. The child inherits SQLite's in-process lock state. Already reported upstream as #300 and fixed in diskcache-community by PR #13 (a before-fork gate). **Impact:** pre-fork servers (gunicorn, Celery prefork) can start workers whose cache writes stall.

## 6. incr on an expired or missing key creates a counter with no expiry

`add(k, 0, expire=60)` followed by `incr(k)` keeps the 60 s expiry. But if the key has expired or been evicted before `incr` runs, `incr` creates a fresh counter with no expiry at all. **Impact:** fixed-window rate limiters leak keys that never expire. This hit Talk Python, which worked around it with `incr(default=None)` and a retry. This may be intended behavior, but it's undocumented and surprising. A doc note, or an `expire=` parameter on `incr`, would help.

## 7. A missing value file leaves an inconsistent row

If a large value's file is removed from disk (by manual cleanup, a disk tool, or a crash between steps), `get` returns the default but the row stays: `in` is True and `len` counts it. Nothing removes the row except an explicit `delete` or `check()`. **Impact:** a "present" key that can never be read; `in`-then-`get` patterns misbehave.

## Not bugs, but worth noting upstream

- `clear()` and `expire()` commit in 100-row chunks, but a writer waiting on a single blocking call (`timeout=1.0`) got in between chunks in only about 3 of 10 runs, because SQLite's busy handler polls rather than queues.
- `incr` on `True` raises `TypeError: can't concat int to bytes` (a bool is stored pickled).
- `pop` on a missing key returns `None` instead of raising like `dict.pop`.
- An empty open cache is falsy because `__len__` is 0. Upstream PR #350 proposes `__bool__`.
