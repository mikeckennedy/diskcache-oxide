# The shared-handle read ceiling (2026-11-05)

Why four free-threaded threads on one handle read no faster than one thread, while four handles on four directories scale. This diagnosis follows plan 13's benchmark (13i) and feeds plan 12 step 5. Release builds, Python 3.14t, 6-byte values, 700 URL-shaped keys, reads only. The prototypes are in `read-ceiling-prototypes-2026-11-05.diff`, gated by environment variables, and never shipped.

## Per handle or per store?

Thousands of reads per second, 4 threads:

| Case | Mac (M5 Pro) | Linux VM (arm64 Docker) |
|---|---|---|
| `Cache`, one shared handle | 831 | 1,187 |
| `Cache`, 4 handles on the same directory | 827 | 1,169 |
| `Cache`, 4 directories | 1,890 | 3,375 |
| `FanoutCache(8)`, one shared handle | 768 | 1,084 |
| `FanoutCache(8)`, 4 handles on the same root | 771 | 1,078 |
| `FanoutCache(8)`, 4 roots | 1,844 | 3,380 |
| Control: `list.count`, shared / own list | 59,072 / 59,241 | 59,888 / 59,992 |

Four handles on one directory perform like one shared handle, so the contended state lives in the `Store` (its pool, slots and SQLite file), not in the Python object, PyO3, refcounts or `HandleConfig`.

## Where the threads wait

macOS `sample`, 5 s, about 20,000 worker samples per case:

- **`Cache`, shared:** 4,986 samples waiting on a mutex. 4,644 are in SQLite's `unixShmLock` (from `walTryBeginRead` and `pager_unlock`) and 336 in oxide's pool (`Pool::checkin`, `Store::lease`). The mutex is `unixShmNode.pShmMutex` (`sqlite3.c:45938`, SQLite 3.54.1). Every read takes it twice, and it's held across the `fcntl` lock syscall, so a waiter parks in the kernel.
- **`Cache`, 4 directories:** no mutex waits, but 6,433 samples in `__fcntl` from `unixShmLock`. With one connection per file, each read still does two lock syscalls, but nothing contends.
- **`FanoutCache(8)`, shared, default budget (one connection per shard):** 6,134 samples in `nanosleep`, from `Slots::acquire_until` called by `Store::reserve_slot` called by `get_with`. A thread that lands on a busy shard fails `slots.try_acquire` and sleeps the fixed 50 µs poll (`genlock.rs:22` `POLL`, `genlock.rs:284`).
- **`FanoutCache(8)`, shared, 32 connections:** 2,008 samples waiting in `unixShmLock` and 3,558 in `fcntl`. Threads still meet on a shard's mutex while it's held across the syscall.
- **Ruled out:** the `GATE` and `slots` atomics, PyO3 extraction and `HandleConfig` state, each under 1% of self time.

## Prototypes

1. **Spinning SQLite mutex:** wraps SQLite's mutex enter (`SQLITE_CONFIG_MUTEX`) to retry `xMutexTry` up to 1,000 times before blocking.
2. **Slot spin:** a short spin on `try_acquire` before the 50 µs sleep.
3. **Pool mutex spin:** a `try_lock` spin before `PoolShared::lock` (measured on the Mac only).

Thousands of reads per second, 4 threads on one shared handle:

| Case | Mac before | Mac after | Linux VM before | Linux VM after |
|---|---|---|---|---|
| `Cache`, prototype 1 | 805-831 | 1,442 | 1,187 | 1,708 |
| `Cache`, prototypes 1 and 2 | | | | 1,898 |
| `Cache`, prototypes 1 and 3 | | 1,613 | | |
| `FanoutCache(8)` default, prototypes 1 and 2 | 768-781 | 1,192-1,204 | 1,084 | 1,518 |
| `FanoutCache(8)` at 32 connections, prototype 1 | 1,223 | 1,546 | 1,292 | 1,959 |

Single-thread and separate-directory numbers don't change.

## Correctness notes

- **Mutex spin:** the underlying pthread mutexes are unchanged, so fork behaves as now: the gate quiesces before a fork, and the bundled SQLite is private to `_core.so`. D-018 and D-023 are unaffected (no new fds, connections or memory). The cost is bounded CPU spent spinning on an oversubscribed host. The prototype's install order (initialize, shutdown, then `SQLITE_CONFIG_GETMUTEX`, since GETMUTEX returns zeros before init) only works before any connection exists and must not ship: a real version supplies its own `sqlite3_mutex_methods`, pthread underneath, spinning first, then locking.
- **Slot wait:** replace the fixed poll with a spin or a notify when a permit drops, or default FanoutCache to more than one connection per shard (fd budget D-037: `2 × max_connections + 2 × shards`). The spin holds no gate level (`reserve_slot` runs before `enter_until`), so the fork protocol is untouched.
- **Pool mutex:** after the SQLite fix it's the next wait, but it's worth only +12% on the Mac, and Linux's std mutex already spins. Measure on Linux before building it.

## Open questions

- With the fixes, Linux shared reads stop at about 1.9M against 3.4M for separate directories. Candidates: the remaining shared-memory mutex holds (which include `fcntl` whenever the in-process lock count goes from 0 to 1), connections moving between cores (the LIFO pool hands each read a different connection), and the pool mutex. A thread-affine connection prototype would separate them.
- None of this has run on the htz host, so production numbers are still to come.
