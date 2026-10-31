# Plan 09: a `pop` that keeps foreign values, and `durable`

Two follow-ups from plan 08, both Michael's decisions (2026-10-30). He also confirmed the name `ForeignValueError`. Each step gets the architect's review once built, and the suite runs on all four interpreters.

**The soak's scope.** The 24-hour run is on commit `0f1b9b8`. Plans 08 and 09 change `get_with` and `pop` after that commit, so its PASS doesn't cover them. The soak worker doesn't call `pop` at all today, so the kit needs it added. A shorter soak of the final code should run before Phase 3 goes to production. See step 3.

## Step 1: `pop` decodes before it deletes

**Problem.** `Store::pop` deletes inside its write transaction and decodes after the commit (decoding can run a pickle hook, which must not run inside the transaction). So a value in a known mode with an unknown tag or extension, or a missing time zone, is destroyed by a `pop` that can't read it. A newer peer's data is lost during a deploy, and under `on_decode_error='raise'` the caller gets an error with the row already gone.

**Design.** `Cache::pop` becomes optimistic:

1. Read and decode with `get_with`, exactly as `get` does: no write lock, and hooks run where a `get`'s hooks run. A miss returns the default.
2. On a clean decode, delete exactly that version with a new blocking `Store::delete_version(timeout, version) -> Result<bool>` (through `write_files`, so a held transaction defers the file's unlink as `pop` does today).
   - `true`: this caller removed it, so it returns the value. Two racing `pop`s can't both get `true`, so a value still goes to one caller.
   - `false`: another writer replaced or removed it first, so go back to 1, up to `POP_ATTEMPTS` (8, as `spill::OPTIMISTIC_ATTEMPTS`).
3. After that many losses to overwrites, fall back to today's locked `Store::pop`. It can't starve, and only then can a foreign tag be destroyed; the docs say so.

**When the decode fails:**

| Failure | `'raise'` | `'miss'` / callable |
|---|---|---|
| Foreign, `UnsafeValueError` | raises, row kept | Foreign: default, row kept. Unsafe: raises, row kept |
| Unreadable (malformed bytes, a failing unpickle) | deletes that version, then raises (today's "a value that fails to decode is still removed") | deletes that version, returns the default, reports |
| Passthrough (`Timeout`, `MemoryError` and so on from a hook) | row kept, raises | row kept, raises |

The last row changes today's behavior for the better: a transient error no longer costs the value. Under `'raise'` the delete is blocking, as `pop` is today, and a `Timeout` from that delete replaces the decode error.

**Costs and checks:**
- `pop` is now a read plus a write. It's off the gate's list and isn't a hot path, but measure it (`pop` of a small value, before and after).
- `refuse` and `Pop::Refused` stay for the fallback.
- Inside a held transaction, the read and the delete both use the transaction's connection.
- A D-020 child reads, then the delete raises as `pop` does today.
- Hooks now run before the row is gone, so a hook that overwrites its own key loses the race, retries and eventually takes the fallback. Bounded.

**Tests:**
- a foreign tag and a missing zone under both policies: row kept;
- N threads and processes racing `pop` over many keys: each value returned exactly once;
- `pop` losing to an overwrite returns the newer value or falls back;
- a hook raising `Timeout` under both policies keeps the row;
- the two existing hook tests (`test_pop_decodes_after_its_write_commits` is renamed, since the order is the point) and the held-transaction test;
- `test_pop_of_a_foreign_tag_says_it_was_deleted` flips to "kept".

## Step 2: `Cache(..., durable=False)`

Michael, 2026-10-30: "durable means DURABLE as much as we can deliver." An app whose every caller falls back to its source of truth wants a cache that can't take a request down. So this is one option covering every operational failure, not only timeouts (this replaces the `handle_timeouts` draft). Talk Python's `DurableCache` hides read errors today, and its `set` raises.

**The option.** `durable: bool = False`. Per handle, not persisted. `NullCache` accepts and ignores it; `Caches` passes it through like any setting. Off by default, so diskcache's behavior is unchanged.

With `durable=True`:
- `on_decode_error` defaults to `'miss'` instead of `'raise'`. An explicit value wins.
- An operational failure on a single-key call doesn't raise: `Timeout`, `OSError` (disk full, I/O), SQLite errors including a damaged database, and the `RuntimeError`s of a child forked mid-transaction that can't write.

| Call | On an operational failure |
|---|---|
| `get`, `pop`, `in` | the default, or False. `cache[key]` raises `KeyError` |
| `set`, `add`, `touch` | False. `cache[key] = v` drops the write |
| `incr`, `decr` | `default ± delta`, as `NullCache` does, so a rate limiter fails open. `default=None` raises `KeyError` |
| `delete` | False. `del cache[key]` raises `KeyError` |
| `clear`, `expire`, `cull`, `checkpoint`, `len`, `volume`, iteration | raise as today: admin callers need the truth, and these have their own `timeout=` |
| anything inside a held transaction | raises: swallowing it would break the transaction's atomicity |

Still raised, because they're bugs and not outages: `TypeError` (a non-`str` key, a value that can't be stored without pickle), `ValueError`, `UnsafeValueError`, a non-numeric `incr`, `NestedTransaction` and the other misuse errors. `MemoryError` and `KeyboardInterrupt` always raise.

**`delete` (Michael, 2026-10-30): returns False.** A failure never reaches the caller. A swallowed `delete` looks like "wasn't there", so that invalidation is lost and stale data is served until the entry expires. The log line is the signal, and the docs must say this plainly.

**Reporting.** A swallowed failure is logged as a warning on the `diskcache_oxide` logger, at most once per handle every 60 s, with a count of those suppressed since. An outage can be thousands a second.

**Tests:** every row of the table against a peer process holding the lock, and against a real I/O failure where one can be forced; the default raises; a held transaction raises; bugs still raise; the log limit, with a count; `Caches` passthrough; `NullCache` accepts and validates it (`bool` only).

## Step 3: re-soak

After steps 1 and 2 and the 24-hour result: add `pop` to the soak worker, with a check that each value is delivered exactly once, then stop the current run, keeping its partial results, and run 24 hours on the final commit on the mini (Michael, 2026-10-30). Phase 3's production cutover waits for it.

## Architect's review of this plan (2026-10-30): changes to make while building

Exactly-once holds: the compare-and-delete runs inside `BEGIN IMMEDIATE`, and `store_seq` never repeats. The design stands, with these changes. Nothing below is built yet.

**Step 1 (`pop`):**
- `decode_failed` can't be reused as written: it returns before classifying under `'raise'`, and it uses the non-blocking delete. Add `Row::Popping(version)` or a `pop_failed`, with the blocking delete under both policies, and define all three outcomes. `true`: raise, or return the default and report. `false`: go back to the read. `Err`: under `'raise'` it replaces the decode error, with that error as its `__cause__`; under `'miss'` it raises unless durable.
- Call `store.check_writable()` at the top of `pop`. Without it a D-020 child gets a miss where it raises today.
- One `Instant` deadline for the whole `pop`, not a full timeout per attempt (otherwise about 9× the budget). Keep one clock read across retries.
- Use `spill::OPTIMISTIC_ATTEMPTS`. The fallback isn't rare on a hot key, because the lost-race window includes the write-lock queue; say so in the docs.
- `tests/DIVERGENCES.md` entry, since the defaults change: a hook sees the key still present, a raising hook can keep the row, `pop` of a missing key no longer times out behind a writer, and a hook that pops its own key now recurses to `RecursionError`.
- A row whose value file is missing can stay after `pop` under contention, as with `get`. Accept, document, test.
- `test_decode_errors.py:167-169` flips too, not only the test the plan names.

**Step 2 (`durable`):**
- Classify with `Error::is_operational()` in `error.rs`, an exhaustive match with no wildcard. Operational: `Timeout`, `Io`, `DiskFull`, `SqliteIo`, `Sqlite` except `OutOfMemory`, `Unwritable`, `ForkPoisoned`. Raised: `TransactionAborted`, `KeyMissing`, `NotNumeric`, `Overflow`, and the hold-misuse errors. Map `SQLITE_NOMEM` to `MemoryError`, which falls through to `OSError` today.
- Swallow only at the store seam (`.map_err(to_py_err)`, one helper), so a `Timeout` raised by user code isn't taken for this cache's failure.
- **Decide:** `classify` still passes `OSError`, `Timeout` and `RecursionError` from a pickle hook out of a durable `get`. Recommendation: under durable they're a miss with the row kept, reported once per version. `MemoryError`, `BaseException` and `UnsafeValueError` still raise.
- Add to the table as "raises": the constructor (use `Caches`) and starting a hold.
- A durable `pop` whose delete fails returns the default, never the decoded value, which could otherwise be delivered twice. Step 1's table needs a durable column.
- The hold rule means "a hold on this store": add `Store::held_by_this_thread()`, on the error path only. `GATE.own_depth()` is wrong here, since it's process-wide and true inside any hook.
- `on_decode_error` becomes `Option<DecodePolicy>` with default `None`, in the signature, stubs and `NullCache`.
- **Reporting:** two log buckets, busy (`Timeout`, warning) and broken (the rest, error), so a timeout storm can't hide a damaged database. Add a third for swallowed `delete`, per-op counts in the message, and a stats accessor like `_wal_stats`, so "N invalidations lost" is visible. Atomics and compare-and-swap, the first failure logged at once, and time taken from the operation's `now` so tests can drive it.
- **Tests:** a peer holding the lock can't make `get` or `in` fail. Injections that work: `max_connections=1` with another thread in a slow hook; `chmod 0` on a value file (not as root); `RLIMIT_FSIZE` with `SIGXFSZ` ignored, in a forked child; fork inside a hold.
- **Docs, plainly:** a dropped `set`, `pop` or version-bump `incr` serves stale data just as a dropped `delete` does, and "until it expires" means forever with no expiry. A fake `incr` is a duplicate if used as an id. `add` is False under durable but True on `NullCache`, so claims fail closed: use a non-durable handle for them. Durable doesn't bound time, so pair it with a small `timeout=`. In database vocabulary "durable" means "survives a crash", and this option can drop writes, so the docs need a sentence saying what it means here.

**Step 3 (soak):** confirmed, the kit has no `pop`. Add:
- an exactly-once lane on the ledger: a producer's winning `add('pq:<slot>', token)`, consumers `pop` and then `add('delivered:<token>', worker)`, where a False is a duplicate, plus a final check in `maint` with an allowance for kills;
- `chaos.pop` in `one_op`; `pop` for half of `held_op`'s deletes; `pop` in `fork_test`;
- a durable lane: after `set` returns False the read-back is still the previous version, and no operational exception escapes;
- new counters in the status and report, and fail the run if the `pop` count is zero.

Per Michael (2026-10-30), the re-soak is a fresh full 24 hours on the final library commit, not 6-8 hours, replacing the current run once this plan is built.

## Progress
