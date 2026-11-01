# Deep review of `5b2ea85`

**Follow-up:** The fixes were reviewed below under “Fix review.” Findings 1, 2 and 4 are addressed in the reviewed fixes; finding 3 is improved but remains open on the contention fallback. The original findings and evidence are preserved as history.

Reviewed 2026-10-31. Exact revision: `5b2ea8552b717c0542cfa287a82e533ad550e265`.

**Findings: 0 critical, 3 high, 1 medium. I would fix the two tier-1 spill-file findings before shipping this revision to Talk Python.** Both concern failure handling, not ordinary successful operations: a corrupt size can terminate the worker, and a failed spill write leaves disk space permanently occupied. The other findings concern deferred-close lifetime management and small thread stacks. No new wrong-key or duplicate-pop bug was demonstrated.

The review used a `git archive` snapshot in `/private/tmp/dco-review-5b2ea85`, not the changing working tree. All source references below name the reviewed revision. No repository code or tests were changed; this document is the only review deliverable. Temporary builds, probes and disposable databases were isolated outside the repository. Existing working-tree changes, including changes arriving during the review, were left alone.

## Findings

### 1. A corrupt spill-size field terminates a durable reader

- **Severity:** high — a damaged cache takes down the entire worker instead of producing a miss or exception.
- **Tier:** 1; ordinary `get`/`pop` of a spilled value. Requires corrupt or externally modified row metadata; ordinary writes do not generate this size.
- **Where:** `crates/diskcache-oxide-core/src/spill.rs:103`, `read_open`, especially line 105.
- **Trigger:** Store a spilled value; change its SQLite `size` to `9223372036854775807`; read it through `Cache(..., durable=True)`.
- **Effect:** `Vec::with_capacity(expected)` attempts the allocation before inspecting the actual file length. The Rust allocation failure aborts the process. Neither durable error classification nor Python's exception handling runs. The same happens with a one-byte file passed directly to `spill::read`.
- **Evidence:** Reproduced against the freshly built release extension from the reviewed revision on macOS arm64, CPython 3.14 with the GIL. Subprocess return code was `-6` (`SIGABRT`), with `memory allocation of 9223372036854775807 bytes failed`. The file-length comparison at line 119 is never reached.
- **Confidence:** certain for the demonstrated input. This does not establish how frequently real storage corruption produces such a field.
- **Fix idea:** Validate the opened file's length before allocating from database metadata, bound the read, and use fallible allocation. Also avoid `read_to_end` growing without limit if the file is longer than advertised or changes while being read.

Reproduction, run in a disposable subprocess because it intentionally terminates the interpreter:

```python
import pathlib
import sqlite3
import tempfile
from diskcache_oxide import Cache

p = pathlib.Path(tempfile.mkdtemp())
c = Cache(str(p), durable=True)
c.set('k', b'x' * 65536)
with sqlite3.connect(str(p / 'diskcache-oxide.sqlite')) as db:
    db.execute('UPDATE cache SET size=? WHERE key=?', (2**63 - 1, 'k'))
c.get('k')  # SIGABRT, not a miss
```

This is distinct from accepting `SQLITE_CORRUPT` as an operational failure: SQLite can successfully return a structurally valid integer that the spill reader cannot safely trust.

### 2. Failed spill writes leave partial temporary files permanently behind

- **Severity:** high — disk-space leak on a realistic operational-failure path.
- **Tier:** 1; large `set`/`add`, including durable request-path writes.
- **Where:** `crates/diskcache-oxide-core/src/spill.rs:38`, `PendingFile::write`, especially lines 50–60; `crates/diskcache-oxide-core/src/ops.rs:294`, `set`, and `:470`, `settle_with`.
- **Trigger:** The temporary file is created, `write_all` writes some bytes and then fails, and `PendingFile::write` returns the error through line 60.
- **Effect:** `PendingFile` has no destructor that removes the file. The caller never receives the pending object, so `settle_with` cannot discard it. A durable handle reports `False`, but the partial `.tmp` remains without a database row. Repeated failures accumulate files and consume space that `volume()`/`cull()` do not count or reclaim. This can impede recovery from a disk-space outage.
- **Evidence:** Reproduced with a 4096-byte soft `RLIMIT_FSIZE`, ignoring `SIGXFSZ`, and two 65536-byte writes. Both durable calls returned `False`; `len(c)` was zero; two `.tmp` files of 4096 bytes remained. The injected error was `EFBIG`, not `ENOSPC`; partial `ENOSPC` and I/O errors take the same unchecked cleanup branch. No crash or kill was needed.
- **Confidence:** certain.
- **Fix idea:** Give a newly created temporary file scoped cleanup ownership until `write` successfully transfers that ownership to its caller. Remove only files actually created by this attempt; preserve `create_new` collision safety.

Reproduction in a disposable process:

```python
import pathlib
import resource
import signal
import tempfile
from diskcache_oxide import Cache

p = pathlib.Path(tempfile.mkdtemp())
c = Cache(str(p), durable=True)
old_limit = resource.getrlimit(resource.RLIMIT_FSIZE)
old_handler = signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
try:
    resource.setrlimit(resource.RLIMIT_FSIZE, (4096, old_limit[1]))
    print([c.set('k', b'x' * 65536) for _ in range(2)])
finally:
    resource.setrlimit(resource.RLIMIT_FSIZE, old_limit)
    signal.signal(signal.SIGXFSZ, old_handler)
print(sorted(f.stat().st_size for f in p.rglob('*.tmp')), len(c))
c.close()
```

Observed output, excluding durable log messages:

```text
[False, False]
[4096, 4096] 0
```

This is additional to the known crash-orphan problem in brief §6.10: cleanly handled write errors leak too.

### 3. Draining a deferred close never releases its retained `Arc`

- **Severity:** high under the brief's leak classification; impact depends strongly on cache-directory churn.
- **Tier:** 2; a close that outlasts a pending fork. The failed-join deferral reaches the same code.
- **Where:** `crates/diskcache-oxide-core/src/store.rs:614`, `defer_release`, and `:1569`, `drain_deferred_releases`.
- **Trigger:** `release_handle` cannot enter the gate within its release budget. `defer_release` forgets an `Arc::clone(self)` to keep the weak registry entry alive. The fork subsequently ends and the drain processes the release.
- **Effect:** The drain decrements the logical handle count and closes the pin/pool, but never balances the forgotten strong reference. The store, paths, pool state and registry reachability survive after all real owners disappear. Each distinct affected store remains allocated until process exit. Repeated deferrals on the same store increase its strong count; they do **not** allocate another whole store each time. This reproduction did not demonstrate a post-drain file-descriptor leak.
- **Evidence:** An external Rust probe linked against the unchanged snapshot used public APIs to hold the global fork claim on a second thread, release the sole handle, end the claim, drain, and drop the final real `Arc`. Output was `handles=0 strong_count=2` before dropping that owner, followed by `store_alive_after_last_drop=true`. One strong reference should have remained before the drop, and the weak reference should then have expired.
- **Confidence:** certain.
- **Fix idea:** Retain deferred stores through an explicitly owned queue/registry entry that the drain can remove and drop safely. Keep the existing gate and child-generation discipline; merely decrementing the handle count does not reclaim an `Arc`.

Minimal Rust probe; compile as an external binary against the core library, not as a repository edit:

```rust
use diskcache_oxide_core::{gate, open::Requested, store::{OpenOptions, Store}};
use std::{sync::{Arc, mpsc}, time::Duration};

fn main() {
    let directory = std::env::args().nth(1).unwrap().into();
    let store = Store::open(&OpenOptions {
        directory,
        timeout: Duration::from_secs(1),
        requested: Requested::default(),
        max_connections: 8,
        cache_pages: 64,
        wal_size_limit: 64 * 1024 * 1024,
        auto_checkpoint: false,
    }).unwrap();
    let weak = Arc::downgrade(&store);
    let (ready, started) = mpsc::channel();
    let (finish, done) = mpsc::channel();
    let thread = std::thread::spawn(move || {
        let fork = gate::GATE.begin_fork();
        ready.send(()).unwrap();
        done.recv().unwrap();
        fork.end_parent();
    });
    started.recv().unwrap();
    store.release_handle();
    finish.send(()).unwrap();
    thread.join().unwrap();
    diskcache_oxide_core::store::drain_deferred_releases();
    println!("handles={} strong_count={}", store.handles(), Arc::strong_count(&store));
    drop(store);
    println!("store_alive_after_last_drop={}", weak.upgrade().is_some());
}
```

The existing `row_5_a_release_behind_a_stuck_fork_defers_and_the_drain_finishes_it` test checks handles, retirement, pool contents and fds, but not expiration of its weak reference after the final owner drops.

### 4. The msgpack depth limit does not protect small thread stacks

- **Severity:** medium — process termination, but requires an unusually small configured thread stack. No failure was established on the production server's stack size.
- **Tier:** 2; custom worker-thread stack configurations. The input can be written through the normal API.
- **Where:** `crates/diskcache-oxide-py/src/codec/msgpack.rs:394`, `Reader::value`, and recursive `items`/`value_inner`; `codec/common.rs:15`, `MAX_DEPTH`.
- **Trigger:** Write a value nested in 120 lists on the main thread, then read it on a Python thread configured with `threading.stack_size(65536)`.
- **Effect:** The recursive Rust decoder exhausts the stack before reaching its depth rejection. The whole interpreter dies, including with `durable=True`. The logical depth cap is not a stack-space guarantee.
- **Evidence:** Fresh release extension, macOS arm64, CPython 3.14 GIL: `set` returned `True`; the read subprocess exited `-10` (`SIGBUS`). Control reads at depths 10 and 60 on the same 64 KiB stack succeeded. A crafted 240-list msgpack value also terminated a 128 KiB thread, while 256 KiB and 512 KiB controls succeeded. These thresholds are build/platform-specific, not recommended portable limits.
- **Confidence:** certain for the reproduced configurations; not reproduced on Linux or free-threaded Python.
- **Fix idea:** Prefer an iterative decoder or explicit stack-growth management. If small stacks are unsupported, document a tested minimum; a fixed recursion count alone does not prove safety on all supported thread configurations.

Reproduction, again in a disposable subprocess:

```python
import tempfile
import threading
from diskcache_oxide import Cache

c = Cache(tempfile.mkdtemp(), durable=True)
value = None
for _ in range(120):
    value = [value]
print(c.set('k', value), flush=True)  # True
threading.stack_size(65536)
t = threading.Thread(target=lambda: c.get('k'))
t.start()
t.join()  # Process terminates before completing this.
```

## Validation performed

All builds used the archived revision, with offline dependencies. The Python extension was freshly compiled in release mode and imported from the snapshot; it reported SQLite **3.54.1**. The first direct Cargo extension build lacked macOS dynamic-lookup linker arguments; rebuilding with `cargo rustc ... -- -C link-arg=-undefined -C link-arg=dynamic_lookup` succeeded. No existing editable extension was rebuilt or overwritten.

| Check | Result |
|---|---|
| `cargo test --offline -p diskcache-oxide-core` | 171 passed |
| Existing Shuttle models, `DCO_MODEL_SECONDS=2 scripts/model-check.sh 10000`, offline | 17 model tests passed; requested 10,000 schedules per scheduler with a 2-second runner cap; not a million-schedule audit |
| Python `test_pop.py`, `test_durable.py`, `test_fork_timeout.py` | 155 passed in 37.39 s |
| Python `test_codec.py`, `test_decode_errors.py`, `test_caches.py`, `test_null_cache.py` | 437 passed in 8.62 s |
| External deferred-release lifetime probe | Demonstrated retained store after drain and last-owner drop |
| Resource-limited spill writes | Demonstrated partial-file leak through both core and durable Python paths |
| Corrupt spill-size subprocess | Demonstrated `SIGABRT` through both core and durable Python paths |
| Small-stack decode subprocesses | Demonstrated fatal signal, with larger-stack and shallower-input controls |

Python tests ran on CPython 3.14 GIL on macOS arm64. Slow-marked tests were excluded by the repository's normal pytest configuration. The brief's count of 189 core tests does not match this run: this revision ran 171 ordinary core tests plus 17 separately selected model tests. No new model was written because no new atomic interleaving bug was established.

## Checked and found sound, with boundaries

“Sound” here means no new defect found in the stated path, not proof of every possible interleaving.

| Brief area / invariants | Assessment |
|---|---|
| A: gate increment/check, parked counts, cross-thread share drop; invariants 1–5 | Read the shared-entry, claim, quiescence, park/unpark and share-drop protocols. The SeqCst increment-before-check pairing and packed snapshot address the described missed-holder races. Park sets the owner flag before adding parked levels, so a racing drop temporarily undercounts parked levels; unpark's opposite transient occurs on the sole claiming thread. Existing models passed. No new gate race found. Nested real forks, early hooks with active levels and missing-before-hook recovery are not fully certified here. |
| B: bounded entry and deferred releases; invariants 5, 6 | A refused gate entry removes its transient holder count; slow waiters retain no permit in the gate itself. Clearing the deferred flag only after acquiring a level avoids the documented second-fork stranding race. The drain's inherited pool/pin cleanup checks generations. Logical handle accounting is separate from the unbalanced strong ownership in finding 3. No injected panic tested during drain. |
| C: handle counts and registry; invariant 6 | Claims and last releases serialize under the registry mutex. Signed counts cover a release landing before its matching reopen claim. `give_back` checks retirement both before and after checkin. Existing open/close race tests passed. Arbitrary file replacement during the multi-connection open sequence was not exhaustively tested. |
| D: successful spill writes and process death; invariants 8, 9 | The ordinary path renames before row publication, unlinks replaced files after successful commit, and discards new files on later write failure. Held writes keep created/removed lists and savepoint marks. No successful-commit cleanup ordering bug found. Finding 2 occurs earlier, before ownership reaches this machinery. Power-loss consistency is a separate limitation below. |
| E: `pop`; invariant 7 | Optimistic callers return a decoded value only after winning deletion of `(id, store_seq)`. The writer raises the sequence floor before deletes; upserts allocate above both the live maximum and the floor. `incr` uses upsert; `touch` retains the stored value/version, intentionally. Maintenance raises the floor too. No repeat of committed versions found under normal operation, including emptying the table. Rollback, restored database files and power-loss recovery are not an everlasting global sequence guarantee. |
| E: fallback and decode policies; invariant 10 | Timeout after a lost race exits before entering the destructive fallback. The fallback follows eight lost optimistic attempts, rejects refused modes before deletion, and reports when a foreign/transient decode failure occurs after removal. A failed durable compare-and-delete does not return the decoded value. No new wrong result found in the inspected `Failure`/`Row` branches. Decoding before winning can run pickle side effects more than once; exactly-once delivery is not exactly-once hook execution. |
| F: durable boundary; invariant 10 | Clock errors occur outside store-result swallowing; storage failures are handled after returning from the store; own-store held transactions bypass durable suppression. SQLite OOM is excluded. The SQLite catch-all also covers programming/misuse errors, so the claim that *all* bugs escape is stronger than the classifier; no reachable library misuse was demonstrated. Findings 1 and 4 bypass the boundary entirely. |
| G: WAL | Checked `checkpoint_once`, checkpoint control and bundled SQLite's actual checkpoint implementation. Detailed conclusions follow. Existing manual/automatic checkpoint core tests passed. No new checkpoint corruption proved. |
| H: interpreter waits; invariants 3, 4 | The normal write-lock, slot, fork and retry waits use the blocker. The `crate::detach` flag prevents reattachment for the inspected Rust-only maintenance/open paths. Long forced-checkpoint work releases the interpreter. This is a source audit plus existing fork-timeout tests, not a shutdown/free-threaded GC proof. |
| I: codecs | Msgpack array preallocation is capped at the available bytes and 4096 elements; byte/string slices validate lengths. Tags and escaped maps still descend through bounded value recursion. Free-threaded dictionaries snapshot entries; msgpack lists detect shortening rather than emit an undersized array. JSON uses serde_json's recursion protection. No tag-collision bug found; small-stack safety fails as in finding 4. |
| J: Python registry | Read lazy open, winner selection, disable/enable, failure retry and close-all paths. Normal loser handles close; disabled wrappers forward explicit removals. Existing registry/NullCache tests passed. Concurrent switching and fork-during-open were not given a new systematic scheduler test. |
| K: held transactions | Read lending, operation savepoints, abandonment, inherited connection disposal and final cleanup; existing tests passed. This remains partial coverage and is not approval to ship public `transact()`. Known unbounded end waits remain. |
| Database descriptor ownership; invariant 11 | Production core filesystem-open search found value-file opens and `/dev/urandom`, while the database pin is opened through SQLite. No ordinary path was found that opens/closes the database through a plain Rust `File`. This does not certify behavior if stored filenames are maliciously rewritten. |

## SQLite source verification and the open problems

The dependency is `libsqlite3-sys 0.39.1` from `Cargo.lock`, containing SQLite 3.54.1. Source inspected locally: `libsqlite3-sys-0.39.1/sqlite3/sqlite3.c` in the Cargo registry. Line references in this section refer to that bundled amalgamation, so they remain meaningful independently of a newer SQLite website.

- **§6.7, checkpoint retries:** `walCheckpoint` at line 69153 limits copying to `mxSafeFrame`. An occupied old read mark can lower that boundary; when it is at the already-backfilled boundary there may be no new work. It does **not** follow that every older reader prevents *all* copying. Lines 69208–69209 only create a copy iterator when `nBackfill < mxSafeFrame`.
- **Why another TRUNCATE can help:** lines 69282–69291 advance `nBackfill` after successful copy/sync. Lines 69313–69325 can then report busy while acquiring the reader locks required to restart/truncate. A later call can retain the copying progress and have a fresh busy window for those locks. This supports the retry mechanism, not the empirical choice of eight tries or a guarantee of eventual success under sustained writers.
- **PASSIVE's busy deadline:** the implementation explicitly requires no busy callback for PASSIVE (lines 69178–69180 and 71278–71282). There is no stale per-connection deadline to consume there; the project's busy window is thread-local, and PASSIVE does not invoke it.
- **Several writers / §6.1:** `sqlite3WalCheckpoint` takes the checkpoint lock without invoking the busy handler on contention (around line 71296). TRUNCATE also needs the writer lock. It can fall back to passive work when that writer lock remains busy. Peer writes between rounds can create more work, so repeated 2 ms windows cannot enforce a disk-space bound. A stronger bound requires a coordinated write-admission/back-pressure policy or an explicit willingness to fail/drop writes; this review did not design or benchmark one.
- **Manual mode:** it disables the project's forced post-write rounds, not SQLite's ordinary passive autocheckpoints. `Store::lease` still sets `wal_autocheckpoint`; the normal default computes to 1000 pages. A long-lived reader can prevent reset while writers extend the WAL. `journal_size_limit` trims at reset, not during an indefinitely pinned log. Last-close behavior is also not a reliable bound while other processes retain connections.
- **§6.2–5, stuck forks and held waits:** remain as documented. No new solution or proof that the known indefinite waits are harmless. The ownership leak in finding 3 persists even after the fork finishes and is separate from the accepted count-before-flag child race.
- **§6.6, logging:** the documented exception-handling difference remains. Durable buckets claim the logging interval before calling a handler, which suppresses ordinary immediate re-entry into the same bucket. This is not a complete recursion guard for adversarial clocks or handlers that repeatedly flush/repopulate buckets. No new realistic production failure was reproduced, so this is not counted as a finding.
- **§6.8, pop fallback frequency:** still unmeasured. The tests establish behavior, not its production frequency.
- **§6.9, doomed transactions:** existing tests passed, but no real disk-full transaction or failed rollback was injected. The partial-file probe fails before a write transaction starts and does not close this gap.
- **§6.10, power loss:** there is no value-file checksum or fsync in the inspected write/read path. A same-length wrong payload is not detected by the length test; raw bytes in particular have no decoder-level integrity check. Also, an overwrite whose SQLite commit is lost can leave a recovered old row referring to an old file already unlinked after the apparent commit. Both are outside process-kill-only evidence. No physical power-loss experiment was run, so these are diagnoses of the known gap, not newly proven power-loss incidents.
- **§6.11–13:** production-scale fork timing, the two historical unexplained failures and the long-run memory slope remain unresolved. Finding 3 is not evidence that it caused the recorded slope; the older soak predates these deferred releases.

## Comments and documentation needing correction

These are follow-ups, not extra severity-counted findings:

1. `spill.rs:81` and `:102` describe a missing/length-mismatched file as a safe unreadable result, but oversized metadata can abort before the comparison (finding 1).
2. `codec/msgpack.rs:395` says a crafted row must not overflow the stack; the depth check does not establish that on small stacks (finding 4).
3. `store.rs:609` onward explains keeping a store alive until drain. The temporary-lifetime implication is false: the retained strong reference is never reclaimed (finding 3).
4. `store.rs:977` onward, `wal.rs` module comments and plan 09's step-2b mechanism overgeneralize “an older read mark stops it copying anything.” The correct condition depends on `mxSafeFrame` relative to `nBackfill`.
5. `wal.rs:1` and its “keep it bounded” wording are stronger than the documented multi-process behavior. The main docs already correctly describe a target rather than a bound.
6. `NullCache` and `Caches` say nothing stale is served after re-enable. They forward explicit removals, but drop `set`/`incr`/`touch`. For example: real cache contains `k=old`; disable; wrapper `set(k, new)` reports success but does nothing; enable; `get(k)` returns `old`. Dropping writes is explicitly the chosen NullCache behavior; narrow the blanket freshness claim to invalidation patterns that actually call the forwarded removals. Version-bump invalidation via `incr` is not preserved.
7. The handoff at `plans/10-phase-3-talk-python-handoff.md:15` still calls durable support “designed and reviewed, not built,” which is historical at the reviewed revision.

## Not checked / remaining limits

- No Linux execution, CPython 3.15 run, free-threaded extension build, full four-interpreter `scripts/check.sh`, full conformance run or fresh 24-hour soak. The isolated tests above should not substitute for the planned deployment gates.
- No new store-level concurrency model, systematic file-replacement scheduler, or exhaustive proof of all counter/flag/panic interleavings. Existing gate/genlock models do not model interpreter attachment or store lifetime ownership.
- No physical power failure, actual `ENOSPC` filesystem, fd-exhaustion campaign, read-only/deleted-directory recovery campaign, or SQLite rollback I/O-failure injection. Resource limits reproduced the partial-write cleanup branch without filling the user's disk.
- No exhaustive fork-from-pickle / nested-after-child-hook experiment. In particular, an inline decode can run Python while rusqlite row/statement guards exist; cleanup after a fork there deserves dedicated instrumentation beyond checking that pooled connections are forgotten.
- No CPython shutdown/daemon-thread/stop-the-world experiment or independent CPython 3.14/3.15 source verification of every hook-order assumption. The local hook-registration code imports logging/executor modules first; third-party later hooks remain the documented limitation.
- No claim that an unchanged successful test suite proves the failure paths safe. The four findings were outside the assertions that those passing tests currently make.

## Fix review

Reviewed `81aa84df220f97255b26e66058d0738a479ee79e` **plus the uncommitted deferred-owner changes** in `store.rs` and `store/fork_timeout.rs`. The changes were copied into a separate snapshot at `/private/tmp/dco-fix-review-81aa84d`. Their SHA-256 hashes were:

```text
store.rs              3be23d11a0372c59e70a5aa093a27c10df4069625559f3822b5a0ab747e01579
store/fork_timeout.rs  ba27c1ef49adb383d3ea3479d3a6d8040d6e2d7370dee1e3f07e382928753ff7
```

### Remaining issue: ordinary contention still permanently leaks a deferred owner

**High, tier 2; finding 3 partially resolved.** In the reviewed working-tree `store.rs:89`, `deferred_owners()` gives up after 64 `try_lock`/`yield_now` attempts. At `store.rs:650`, that result still executes `mem::forget(Arc::clone(self))`.

The comment describes a mutex inherited locked from a vanished parent thread, but there is no generation or process check here. An ordinary live thread can be descheduled while holding the mutex around `owners.push(...)` or the drain's `mem::take`. Another deferrer can exhaust its 64 attempts before that thread runs again. `yield_now` is not a guarantee that the lock holder runs. That deferrer then leaks its strong reference permanently, even after the mutex becomes free, the fork finishes and every release is drained. Later drains cannot reclaim a reference that was never stored in the queue.

The new `row_5_a_deferral_with_the_owners_locked_for_good_still_releases` test demonstrates the fallback in the parent without actually forking: it locks the mutex, calls `defer_release`, unlocks, and explicitly asserts a remaining strong count of 2. This proves the retained owner; the ordinary two-thread scheduling trigger above follows from the same branch. I did not run a new adversarial scheduler to measure its frequency.

**Requested follow-up:** Separate actual inherited-lock recovery from temporary contention, and retain recoverable ownership in the latter case. Preserve the bounded fork behavior; replacing the bounded attempts with an unconditional blocking mutex acquisition would reintroduce the child deadlock. If an inherited-child leak is deliberately accepted, document that narrowly and test that live-parent contention does not take the same permanent-leak path.

No new fd leak or incorrect handle count was demonstrated. The normal queue/drain path is fixed: the original external probe now prints:

```text
handles=0 strong_count=1
store_alive_after_last_drop=false
```

### Assessment of the other fixes

| Original finding | Assessment |
|---|---|
| 1: allocation from corrupt spill size | **Addressed.** `read_checked` checks the opened file's metadata before allocating, uses fallible reservation, and caps reads at the expected length plus one byte. That extra byte detects growth without reading an unbounded file. Allocation failure maps to `MemoryError` and remains excluded from durable suppression. Tests cover mismatched huge sizes, negative sizes, get/pop, and an agreeing but unallocatable sparse file. |
| 2: partial temporary files | **Addressed.** `write_new` only removes the file after this attempt successfully created it and then failed to write it. A `create_new` collision leaves the pre-existing file alone. The resource-limit scenario now checks that failed large writes leave no `.tmp` file. Unlink failure and process death can still leave an orphan, but the original normal error-return leak is fixed. |
| 3: retained deferred `Arc` | **Partially addressed.** The owned queue balances normal deferrals, including multiple releases and last-owner destruction. The permanent-leak fallback above remains reachable under ordinary contention. |
| 4: small-stack codec crash | **Addressed for the tested platform/build.** Both codecs now check remaining C stack during recursive container processing. Decode preserves `RecursionError`, so durable reads can miss without deleting a valid row; failed encodes still raise. Constructor-time type loading avoids deferring imports until the first container operation on a small worker stack. Regression coverage includes reads, writes, several container shapes and progressively consumed stack. |

The immediate enter/leave pairing in `check_stack` is consistent with CPython 3.14's implementation: its recursion-entry check tests the machine stack pointer, and the corresponding leave helper is empty. See the [CPython 3.14 recursion helpers](https://github.com/python/cpython/blob/3.14/Include/internal/pycore_ceval.h) and [stack-limit/error implementation](https://github.com/python/cpython/blob/3.14/Python/ceval.c). This source check does not replace Linux, free-threaded or 3.15 execution coverage.

### Follow-up validation

- Fresh isolated core build: **182 tests passed**.
- Original external lifetime probe: normal deferred-release ownership now fully reclaimed.
- Fresh release extension built from the follow-up snapshot, without modifying the repository's installed extension.
- Python regression result recorded below after completion.

**Disposition:** The two tier-1 issues that blocked the original review are addressed. I would not label all four findings closed until the live-parent contention leak is resolved or explicitly accepted. This follow-up does not re-review unrelated maintenance/soak changes or replace the original Linux and final-soak deployment gates. Only this review document was edited.
