# Plan 18: the 49-of-48 file descriptors

A fix, not a phase. The soak's fanout lane checks that the fds open inside a FanoutCache's root stay within D-037's budget, `2 × max_connections + 2 × shards` (48 at the defaults), and htz run 2 and run 3 (2026-11-06 and 2026-11-07, commit a643109) each flagged a few samples at 49. Always one shard with four descriptors on its `diskcache-oxide.sqlite` instead of three, nothing extra on the `-wal` or `-shm`, always a 3.14t worker, gone again within ten seconds. The Mac soak of 2026-11-06 peaked at exactly 48.

## What the extra descriptor is

Per shard at the defaults: two pooled connections (each holds the database and the WAL), the `-shm` (one per process per file), and the store's pin (a read-only SQLite connection that never runs a statement, so it holds the database only). Three on the database, two on the WAL, one on the shm: six, times eight shards, 48. A fourth descriptor on the database with nothing else extra is a connection that has never read: a pin, or a connection closed before it did anything.

SQLite's unix VFS gave it its lifetime. `unixClose` (bundled `sqlite3.c`) does not close a descriptor while `pInode->nLock > 0`, that is, while any connection in this process holds a POSIX lock on the file; it puts it on the inode's `pUnused` list, and `closePendingFds` closes those only when the inode's lock count reaches zero. In WAL mode every connection takes a SHARED lock on the database when it opens the WAL and keeps it until it closes, so beside a live pool the count never reaches zero: a descriptor closed beside a live pool stays open until the pool empties, which is the last `close()` or the idle connections closed before a fork. `unixOpen` reuses a pending descriptor for the next open on that inode in the same mode (read-only for a pin, read-write for a connection), so a store carries at most one per mode, which is why the soak never saw 50.

## Where it came from

`Store::add_handle_until` (store.rs), the path a closed handle takes when it is used again (diskcache reopens lazily): it opened a pin on the database before taking the registry (which never waits on I/O), then found the store live, so the pin was unused and dropped. The drop closed it beside the live pool, and SQLite kept the descriptor.

Which soak thread rejoins: any call that was in flight on one thread when another thread closed the handle, and reached `ensure_open` after the close. The soak's `reopen_fan` (a new handle, then `close()` on the old) runs every 60 walks, and all three htz events fell on that boundary (1203 s, 1209 s and 603 s after their worker's spawn, against reopens every 300 s plus startup). The call is a `set` of one of the soak's large values (1% are 64 to 512 KiB): the value is encoded before the handle is checked, so the window is long enough for a `close()` on another thread to land inside it, where a small call's window is nanoseconds. Multi-shard calls rejoin the same way with a longer window: `FanoutKeyIterator.page`, `every_shard` and `remove_everywhere` call `ensure_open` per shard, so the later shards of an iteration, an `expire()`, a `cull()` or a `checkpoint()` running across a `close()` rejoin their stores. The soak's forks (every 10 s in that lane, plus the orchestrator's) empty the pools, which closes the pending descriptor, so htz saw 49 for one or two samples and never 50.

## Evidence

- Deterministic (a session scratch script; the three tests below are its durable form): a `Cache` with one op done, a second handle on the directory closed and used again, on 3.14 and 3.14t: the directory goes from 4 fds to 5 (`.sqlite` 2 → 3) and stays there; a second rejoin adds nothing; a fork in the process clears it; a FanoutCache gets one per shard (32 → 40). On the fix: 0 extra, both interpreters.
- The soak's shape sped up (a scratch script: a FanoutCache, 4 load threads, a reopen thread, a sample right after each reopen): 4 load threads, a reopen every 20 ms, a sample right after, on a643109's code. Small values only: 0 of 1,331 samples over budget on 3.14t (0 of 1,193 at 40 threads). With the soak's large values (10% of sets 64 to 512 KiB): 1,350 of 1,356 over on 3.14t, first one shard at `.sqlite: 4`, then more as the descriptors accumulate with no fork to clear them. With one thread running `list(fan.iterkeys())` throughout and small values: 873 of 874 (915 of 916 on a second run), every shard at `.sqlite: 4`. On 3.14 with the GIL, 0 of 42. On the fix, the iteration variant: 0 of 897.
- The soak's own fanout worker with `FANOUT_REOPEN_EVERY = 1` and `FANOUT_WALK_EVERY = 1.0` (a scratch copy, 4 minutes, 2 fanout processes, the unfixed build): 0 fd events in about 476 reopens, peak 48. Its large values are 1% of 15% of the calls, so about 3 a second a worker, and its forks every 3 s there cleared what it may have made between samples: the real soak's rate is a few per 100 reopens, which 4 minutes can't show.

## The fix

`add_handle_until` decides under the registry, where retirement can't change (`count_handle` is only called with it locked): a live store gets its handle counted with no pin, which is the usual case; only a retired store gets the pin opened, outside the registry as before, and claimed on a second take of it. A thread that revives the store between the two takes leaves this pin unused and its close deferred once, the price of never doing I/O under the registry.

Not changed: `open_store`'s slow path still opens a connection and a pin before taking the registry, and drops both if another thread registered the store meanwhile (two threads' first opens of the same directory racing). That leaves pending descriptors too (the connection's is recycled by the winner's pool at its next open; the pin's stays until the pool empties or the store revives); it is rare and it is not the soak's case. The architect's view: documented in D-017 is enough to commit; to remove it later, take the registry after `prepare_creating` and before `open_pin`, claim a live store with no pin and hand it the connection through `give_back`, and open the pin only when no live store exists, the same two-take pattern as `add_handle_until`. Also from the review, applied: `count_handle` hands a caller's pin back unused when an add lands at or below zero, instead of dropping it under the registry (a close there is the I/O the registry must never do).

## Tests

- `tests/conformance/test_threads.py::test_a_closed_handle_used_again_beside_a_live_one_adds_no_fd` (oxide-only, D-017): the deterministic case on a plain `Cache`.
- `tests/unit/test_fanout.py::test_a_closed_handle_used_again_beside_a_live_one_adds_no_fd_on_any_shard`: the same per shard.
- `tests/unit/test_fanout.py::test_reopening_under_a_running_iteration_stays_within_the_fd_budget`: the soak's shape for two seconds, every sample within the budget. With one connection per shard (`max_connections=shards`, budget 32), so the iteration alone fills the budget and one pending descriptor shows; at the default two per shard the iteration alone sits at 32 of 48 and the eight pending descriptors still fit.

All three fail on a643109's code (5 fds against 4; 40 against 32; samples of 40 against a budget of 32, three runs of three) and pass with the fix on 3.14 and 3.14t (the iteration test three runs of three on 3.14t).

## Documentation

D-017 gains the deferred-close rule and what still leaves one pending descriptor (a connection over the cap from a nested operation, an open that lost the registration race). CHANGELOG under Fixed.

## A flaky test, found on the way

`store::shards::tests::a_shard_that_fails_to_open_releases_the_shards_before_it` and `store::shard_identity::a_shard_directory_a_plain_cache_created_is_never_adopted` fail about one run in five of `cargo test --lib store::` (handles 2, not 1), on a643109 and on c6f9192 before plan 17 alike, so plan 17's note (§10) is answered: not its doing. A refused open releases its handle through `release_now_or_defer`, which defers the release when a fork is pending, and the `fork_timeout` tests in the same binary hold simulated forks for seconds, so the assertion can run while the release waits for a drain. `Store::settled_handles()` (handles net of deferred releases, test-only) replaces `handles()` in those two test modules; the `fork_timeout` tests keep `handles()`, since the deferral is what they test. Five runs of `store::` after the change: all green.

## Progress

- 2026-11-07: found, reproduced, fixed and tested as above; the flaky assertions fixed. The gates and the architect's review are recorded here as they land.
- 2026-11-07: the architect's review: PASS with required changes, all applied: D-017's reuse claim corrected to one pending descriptor per open mode; the soak's case named as a `set` in flight, with iteration as the regression test's shape; `count_handle` returns a caller's pin unused when the count lands at or below zero. From the suggestions: the test helper is `settled_handles()`, `#[cfg(test)]` and `pub(crate)`, with a saturating subtraction; D-017 lists the third remaining source of a pending descriptor (a rejoin that lost a revive). The `open_store` race is left documented, with the architect's recipe above for later. Done in plan 20 (2026-11-08).
