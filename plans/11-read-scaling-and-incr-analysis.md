# Plan 11: where reads stop scaling, and why `incr` is slow

Research notes from 2026-11-05, written for whoever picks up items 7 and 8 in `plans/10-phase-3-talk-python-handoff.md` (section 3). This is analysis, not a build plan: it says where the time goes, with measurements, and ranks the changes worth making. Correctness comes first in every option below; each names what it puts at risk. The scripts behind every number are in `bench/scaling/`.

## 1. Summary

- Small reads on one shared `Cache` don't scale across free-threaded threads mostly because of SQLite, not oxide. Every `get` is its own read transaction, and SQLite's unix VFS guards the shared-memory locks of each database file with one mutex per file per process (`unixShmLock`). Four threads doing 1 µs reads queue on it.
- Oxide adds its own cost on top: on Linux, four threads on one shared cache get about 1.5M reads/s against plain `sqlite3`'s 2.0M on one file. The pool mutex (two lock round-trips per `get`) and the shared gate and slot counters are the candidates.
- macOS makes all of this look worse than production will see: a contended `pthread` mutex parks in the kernel (`__psynch_mutexwait`). On Linux the same code scales several times better.
- `incr` is slower than stock's for two reasons: it runs the general upsert (an `INSERT … ON CONFLICT DO UPDATE` that recomputes `store_seq` with a subquery, plus the totals and cull checks) where stock does one in-place `UPDATE`, and its commit costs more for the same two pages, for a reason not yet found.
- The biggest win for Talk Python is not in either: grouping a request's three counter bumps into one write transaction cuts them from 79 to 48 µs on oxide (stock: 66 to 37 µs). That needs a public `transact()`.

## 2. Read scaling

### Measurements

Four threads, 6-byte values, free-threaded 3.15.2 (GIL confirmed off), reads per second:

| Setup | macOS (M5 Pro) | Linux (arm64 Docker VM, 18 CPUs) |
|---|---|---|
| Oxide, 1 thread | ~970k | ~1,075k |
| Oxide, 4 threads, one shared cache | ~770k | ~1,500k |
| Oxide, 4 threads, a cache each | ~1,830k | ~3,400k |
| Plain `sqlite3`, 4 threads, one file | 0.9-1.3M | ~2,000k |
| Plain `sqlite3`, 4 threads, a file each | ~890k | ~2,900k |

`max_connections` from 4 to 64 makes no difference on Linux (1.46-1.62M, interleaved runs; `conn_sweep.py`). An earlier 1.2M vs 1.68M split was run order. Connections aren't reopened either: the pool keeps `max_connections` idle.

### Where the time goes (macOS profile, `hot_reads.py` under `sample`, symbols kept)

Waits on a mutex, by caller (`callers.py`):

| Caller | Samples | Share |
|---|---|---|
| `unixShmLock` from `walTryBeginRead` (starting the read transaction) | 6,901 | 79% |
| `unixShmLock` from `pager_unlock` (ending it) | 1,261 | 14% |
| `Pool::checkin` and `Store::lease` (oxide's pool mutex) | 608 | 7% |

Plain `sqlite3` under the same profile waits in the same two `unixShmLock` callers (9,000 samples), so the first two rows are SQLite's, shared by every SQLite user in the process. The WAL's size makes no difference (`wal_effect.py`, in the scaling notes), and nothing in the bundled build's compile options touches this path (`DEFAULT_MEMSTATUS=0` is already set).

### Options, ranked

1. **Shard a hot cache across several SQLite files.** The only change that removes the per-file mutex, and the probe shows the ceiling: 3.4M vs 1.5M on Linux. Stock diskcache's `FanoutCache` is the precedent. It would be opt-in per cache (`shards=N`), so only the hot read caches pay for it. Correctness cost: `len`, `volume`, `clear`, iteration and `expire` fan out; `size_limit` and culling become per shard; a held transaction can only span one shard (or must be refused across shards); the on-disk layout changes, so it's a new format with its own conformance tests. Large, but well understood.
2. **Take oxide's own per-read cost out (worth up to the 1.5M to 2.0M gap on Linux).** Each `get` locks the pool mutex twice (checkout, checkin) and touches two shared atomics (`GATE.try_enter_shared`, `slots.try_acquire`), all on cache lines every thread writes. A thread-affine connection (each thread keeps the connection it last used for a store, falling back to the pool) removes both mutex round-trips and keeps a connection's page cache on one core. Correctness cost: the fork protocol. A thread-local connection must still be tagged with its generation, closed by the before-fork hook's quiescence, counted in `slots`, and released when the thread exits or the handle closes. That puts it in the gate's territory: model-check it and soak it.
3. **A spinning SQLite mutex (`SQLITE_CONFIG_MUTEX`).** Spin briefly before parking. It would mostly help macOS, where parking is what's expensive; Linux already does better. Low value for production. Correctness cost: every SQLite mutex goes through it, including the recursive and static ones, and it must survive `fork()`.
4. **Release the GIL during the SQLite read on GIL builds.** `Cache::get` doesn't `detach`, so on GIL Python a read holds the GIL through SQLite, and four GIL threads can't overlap (oxide on GIL Python measures exactly 1.0x at four threads). Detaching for the SQLite step means copying the row out before decoding, which costs a single thread something. Talk Python runs one single-threaded worker today, so this is the lowest priority.

## 3. `incr`

### Measurements

- Steady state, existing key, one thread, macOS, 3.14 with the GIL: stock 17 µs, oxide 24 µs (`incr_series.py`). Both hold the WAL at about 3 MB, so checkpoint policy isn't the difference. A smaller `wal_size_limit` makes oxide slower (30 µs at 4 MiB): more checkpoints.
- Both write exactly 2 WAL pages per `incr` (`frames.py`), and both run `synchronous = NORMAL`.
- Profiles of the same loop (`hot_incr.py`, stock and oxide): inside `sqlite3_step`, oxide spends about 18.6 µs a call against stock's 13.5. Of that gap, about 2.8 µs is the commit's `pwrite` for the same 8 KiB, and about 1 µs is executing the extra statements.
- Inside one held write transaction, so with one commit for all of them, three bumps still cost oxide about 12 µs more than stock (`batch_bumps.py`): roughly 4 µs of statement work per bump.

### Why

Stock's `incr` on a live key is `UPDATE Cache SET store_time = ?, value = ? WHERE rowid = ?`. Oxide's runs `prelude` (the row read), then `upsert_after`, an `INSERT … ON CONFLICT DO UPDATE` whose `store_seq` comes from `MAX(store_seq)` and `totals.next_seq` subqueries and which rewrites `mode`, `filename`, `size`, `expire_time` and `store_seq`, then `adjust_totals` and `cull`, all inside `write_files`.

### Options, ranked

1. **An in-place fast path for `incr` on a live, inline row.** One `UPDATE cache SET value = ?, store_time = ?, store_seq = ? WHERE rowid = ?`, with `store_seq` still bumped (the optimistic `get_with` and `pop` paths rely on versions, D-013), skipping the upsert's subqueries, and skipping `adjust_totals` and `cull` when the stored size is unchanged. Keep the general path for misses, expired rows, file-backed rows and size changes. Correctness cost: small, but it's a second write path for one operation, so the conformance suite's `incr` cases and the version tests must cover both.
2. **Find the commit gap.** The same two pages cost oxide about 2.8 µs more to write. Ruled out: SQLite's `assert()`s, since the amalgamation defines `NDEBUG` itself whenever `SQLITE_DEBUG` is off (`sqlite3.c`, `#if !defined(NDEBUG) && !defined(SQLITE_DEBUG)`). Still to rule out, in order: the cc crate's optimization flags for the C build, and SQLite 3.54.1 against 3.53.1, by building once against 3.53.1.

## 4. For Talk Python, without waiting on the library

- **Group the three rate-limit bumps of a request into one write transaction**, once `transact()` is public (today only the `_hold_write_transaction()` test hook exists). Measured: 79 to 48 µs per request on oxide, about 31 µs of a 134 µs episode-page request, and the biggest single win found. Semantics don't change: the counters commit together, and the transaction holds the write lock for about 50 µs.
- **Production isn't affected by the read-scaling limit yet.** It runs one single-threaded Granian worker. The read work matters when the app moves to free-threaded Python with several threads, and the numbers should be taken on Linux (the htz host), not macOS.
