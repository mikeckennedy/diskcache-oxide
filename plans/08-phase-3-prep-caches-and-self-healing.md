# Phase 3 prep: self-healing reads and the `caches` registry

Talk Python's `durable_cache.py` and `cache_service.py` come into the library (Michael, 2026-10-30; the reasoning is in `vision.md`, "DurableCache and the cache service"), so Phase 3 adopts the library versions directly instead of porting `DurableCache` and then replacing it. The other apps with copies (Training, Python Bytes, InterviewCue, Command Book, Expenses PDX) can then drop them too. Each step below is small; the architect reviews each one once it's built, and the suite runs on 3.14 and 3.14t after each.

Started 2026-10-30, while the Phase 2.5 24-hour soak runs on the mini. Nothing here changes the concurrency core's locking, so it can't spoil the soak's result. The soak's verdict still gates Phase 3 itself.

The architect reviewed the first draft of this plan (2026-10-30). The draft would have let a handle delete data it shouldn't in three ways, and it found a bug in today's `pop`. All of that is folded in below; the review's points are cited as (A1)-(A13).

## What Talk Python has today (read 2026-10-30)

- **`DurableCache`** wraps `diskcache.Cache`. Its one real feature is `get`: any exception is logged as a warning, the key is deleted (errors from the delete swallowed), and the default is returned. Everything else is forwarding, plus a name and `bool()` always True (already D-009 here).
- **`cache_service.py`** (about 800 lines):
  - eight physical caches, each a directory under one base dir that `TALKPYTHON_CACHE_DIR` overrides;
  - `create_cache(name, size_limit, purpose)` opens lazily into a module-level dict, with `timeout=1.0` on every open, and returns `NullCache(name)` when opening fails (not memoized, so the next call tries again);
  - 18 *purposes* map onto those 8 caches; a purpose switched off in the settings file gets a `PerfTestNullCache` (our `NullCache(invalidate=real)`);
  - an hourly lazy `expire_all_caches()` triggered from inside `create_cache`;
  - admin helpers (`get_all_caches()` for count and volume, `clear_*`), which each open a fresh `diskcache.Cache` with a 10 s maintenance timeout, so a chunked clear isn't cut off by the 1 s request timeout;
  - `close_all_caches()` at shutdown.
- 52 `create_cache(...)` call sites.

## Decisions (Michael, 2026-10-30)

- A cache that fails to open is remembered as failed, and the registry tries the open again after **10 s** (`retry_open_after=10.0`).
- `Timeout` is never turned into a miss. If Phase 3 wants misses on timeout, that will be its own explicit option, decided then.

## Step 0: `pop` must not delete a pickle row on a `pickle=False` handle (bug fix)

Found by the architect (A1), reproduced: with a `pickle=True` writer and a `pickle=False` reader, `r.pop('k')` raises `UnsafeValueError`, but the row is already gone. `Store::pop` (`ops.rs:251-295`) deletes inside its transaction, and `Cache::pop` (`cache.rs:330`) decodes only after the commit. D-001 says a handle never destroys a peer's data.

- **Fix:** core `pop` takes the mode to refuse, checked inside the transaction before the delete, for example `pop(timeout, key, now, refuse_mode: Option<i64>)` returning a `Refused` outcome. The py crate passes the pickle mode when `!self.pickle` and raises `UnsafeValueError` with the row kept.
- **Tests:** the reproduction, in both orders. After the refused `pop`, the `pickle=True` handle still reads the value, and a `pickle=True` `pop` still works.
- CHANGELOG entry.

## Step 1: per-call `timeout=` on `clear()`, `expire()` and `cull()`

- Keyword-only `timeout=None`: seconds each chunk's write may wait for the lock, overriding the handle's `timeout` for this call only. It bounds each chunk's wait, not the whole call. Keyword-only keeps diskcache's positional slots (`expire(now, retry)`, `clear(retry)`) free.
- The store functions already take a timeout on every call (`maintenance.rs`); the py crate passes `self.timeout` today (`cache.rs:357-376`). `checkpoint()` already parses an override (`cache.rs:253-265`): factor that into an `optional_timeout(...)` helper and reuse it.
- `NullCache` accepts and validates it, rejecting negatives (its `_check_seconds` doesn't today), and `clear(timeout=)` forwards it to `invalidate.clear(timeout=...)`. `CacheLike` in `protocol.py` gets the parameter.
- Stubs, docstrings and the user guide's API page.
- **Tests:** a peer process holds the write lock for about 1.5 s. `clear()` on a handle with `timeout=0.2` raises `Timeout`; `clear(timeout=5)` on the same handle succeeds. The same for `expire()` and `cull()`. `NullCache(invalidate=real).clear(timeout=...)` forwards it.
- Rationale: convenience. In oxide a fresh maintenance handle is already cheap (it joins the shared `Store`), but an admin call shouldn't need a second handle at all.

## Step 2a (core): read versions and a best-effort delete

Keep the core free of Python's error policy (A5). The py crate decides what to delete; the core just makes it possible.

```rust
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Version { id: i64, store_seq: i64 } // private fields: only the core makes them

pub struct Hit<T> { pub value: T, pub expire_time: Option<f64>, pub version: Version }

pub fn get_with<T>(..) -> Result<Option<Hit<T>>>;

/// D-013 compare-and-delete with no waiting for the write lock; unlinks the value file.
/// True if it removed the row.
pub fn delete_version_nowait(&self, version: Version) -> bool;
```

- `look_up` (`ops.rs:~404`) reads `id` and `store_seq` only for spilled rows today. The columns are already in `LOOKUP_SQL`, so inline rows just read two more integers. If the A/B gate run shows any cost, plan B is a lazy accessor read only on the error path.
- The spill path's existing compare-and-delete (`ops.rs:138-144`) moves onto `delete_version_nowait`.
- **Waiting:** it never waits for the write lock. Like the spill path today, it goes through `write_nowait`, which skips inside a held transaction, at gate depth > 0 and in a fork-poisoned child, and waits only if a fork is pending (A6). That's acceptable for the same reason it is on the spill path.
- **Tests (Rust):** the version matches the row, a newer version is never deleted, a busy write lock skips the delete, and spill-path behavior is unchanged.

## Step 2b (py): `Cache(..., on_decode_error=...)`

A constructor option, not a wrapper, so `get` doesn't pay for an extra Python frame (ruled out in Phase 1).

**The option.** `on_decode_error: Literal['raise', 'miss'] | Callable[[str, Exception], object] = 'raise'`.
- `'raise'` (the default) is today's behavior.
- `'miss'`: a value that can't be decoded is treated as a miss and logged as a warning on the `diskcache_oxide` stdlib logger, with the key, the cache's directory and the exception.
- A callable means `'miss'` and **replaces** the warning: the app has chosen how to report, so a Sentry logging integration doesn't get the failure twice. It's called with `(key, exc)` after the connection is released and the bad row is handled. An exception it raises is logged (`logger.exception`) and swallowed, and the read still returns the miss, because the point of the option is that a read can never crash a request.
- **No recursion (A4):** a thread-local "reporting" flag. A decode error inside a callback (the callback reads the same bad key while its delete was skipped, or a logging handler that writes to a cache) still returns the miss and logs, but doesn't call the callback again.
- Per handle, not persisted, like `pickle`. Parsed once at construction into an enum (`Raise`, `Miss`, `Report(Py<PyAny>)`). A wrong type raises `TypeError` and an unknown string raises `ValueError`, on both `Cache` and `NullCache` (which otherwise ignores it).
- The logger is looked up once per process (`PyOnceLock`), on the first decode error.

**Classify at the source, not by exception type (A2).** Store, lock and I/O failures come back as `Err` from `get_with` and are never touched by the policy. Within a decode, the codec returns an internal classification instead of a bare `PyErr`:

| Decode outcome | `'miss'` does | Why |
|---|---|---|
| **Unreadable**: the bytes are malformed for a mode and codec this build knows (truncated msgpack, bad JSON, a native number with the wrong storage class), or unpickling raised an `Exception` that isn't excluded below (`ModuleNotFoundError`, `AttributeError`, `ImportError`, `pickle.UnpicklingError`, an error from `__setstate__`) | miss, **delete that version** | The row can never be read by this or any build. The unpickle case is what `DurableCache` exists for: a class moved or renamed. |
| **Foreign**: an unknown mode, an unknown msgpack or JSON tag, an unknown msgpack extension, or a JSON value this host can't build (a zone key missing from its tzdata) | miss, **row left alone** | It may be a newer peer's data during a rolling deploy, or readable on another host (A3). D-001: a handle never destroys a peer's data. |
| **Passthrough**: `UnsafeValueError` (a pickle row on a `pickle=False` handle) | **raised** | A configuration bug, not rot. A miss would never delete it, so it would warn on every read of a hot key (A8). |
| **Passthrough**: from inside an unpickle, `MemoryError`, `RecursionError`, `diskcache_oxide.Timeout` and any other `OSError` (a nested cache call in `__setstate__`, including `WriteLockBusyInTransaction`), and any non-`Exception` (`KeyboardInterrupt`) | **raised** | Transient or not about the bytes. `Timeout` is never turned into a miss. |

- A missing spill file isn't in the table: it's already a silent miss, with that exact version deleted (D-013), and it isn't reported, since it's the normal race outcome (A7). Other spill I/O errors are `OSError` and raise.
- **Design rule, recorded in `04-design.md`:** adding a codec mode, tag or extension is backward compatible for readers only because of the "Foreign" row above. A change that older builds must not read bumps the format version instead.

**Which calls it covers:** `get` (including `expire_time=True`, which returns `(default, None)`), `cache[key]` (a miss raises `KeyError`), and `pop`. `pop` decodes after its delete has committed, so an Unreadable value is already gone: `'miss'` returns the default and reports. A Foreign value in `pop` gets step 0's treatment: refused inside the transaction, so the row stays, and `'miss'` returns the default. `in`, `iterkeys`, `touch` and `incr` never decode a value and are unaffected.

**Order on a decode failure:** classify, then `delete_version_nowait` (Unreadable only), then log or call the callback.

**Tests:**
- `'raise'` keeps today's behavior for every row in the table.
- Unreadable, unpickle: a class removed after the value was stored. `'miss'` returns the default, the row is gone, a warning names the key.
- Unreadable, bytes: a corrupt msgpack row rewritten through a `SqlitePeer`. Miss, row gone.
- Foreign: a row with an unknown mode, and one with an unknown msgpack tag. Miss, row still there.
- Passthrough: `UnsafeValueError` raises under `'miss'`, row kept; a `__setstate__` that raises `MemoryError`, and one whose nested cache call raises `Timeout`, both raise under `'miss'`.
- Delete only the version that failed: a `__setstate__` that overwrites the same key through another handle, then raises. The new value survives.
- The delete is skipped, and the read is still a fast miss, when the write lock is held by a peer process, by another thread of this process (`_hold_write_transaction`), inside this thread's held transaction, and nested in a pickle hook.
- The callable gets `(key, exc)` with the original exception, runs after the delete (it can `get` the key and sees a clean miss), replaces the warning, and an exception it raises is logged and swallowed.
- No recursion: a callback that reads the same bad key while another thread holds the write lock returns, and the callback ran once.
- `pop` under `'miss'`: Unreadable returns the default; Foreign returns the default and keeps the row.
- `NullCache(on_decode_error=...)` accepts it; a wrong type raises `TypeError` and an unknown string `ValueError` on both classes.
- The gate: `get_hit_small` must not regress (one interleaved A/B run, on this machine, not the mini, after the soak finishes).

## Step 3: the `caches` registry

Pure Python, in `diskcache_oxide/caches.py`, exported as `diskcache_oxide.Caches`. No new Rust.

```python
from diskcache_oxide import Caches

caches = Caches('/var/cache/app', timeout=1.0, on_decode_error=report, on_open_error=report_open)
caches.register('general')
caches.register('show_pages', size_limit=2 * GiB)
caches.register('rate_limits', directory='/fast/rate_limits')

general = caches['general']        # opened on first use, then the same object every time
'general' in caches                # registered?
list(caches)                       # names, in registration order
caches.disable('show_pages')       # later lookups get NullCache(invalidate=real)
caches.enable('show_pages')
caches.enabled('show_pages')       # bool
caches.stats()                     # {name: CacheStats(...)} for an admin page
caches.clear('general', timeout=10.0)
caches.expire_all(timeout=10.0)    # {name: count or None}
caches.close_all()                 # also on `with Caches(...) as caches:` exit
```

**Construction and registration:**
- `Caches(base_dir, *, on_open_error=None, retry_open_after=10.0, **defaults)`. `base_dir` becomes an absolute path at construction. `defaults` are any `Cache` constructor keywords, applied to every cache unless `register` overrides them; unknown keyword *names* raise `TypeError` at construction.
- `on_decode_error` given to the registry takes `(name, key, exc)`, so one reporter serves every cache (A9). The registry binds a per-name closure when it opens each `Cache`, which keeps `(key, exc)`. `'raise'`/`'miss'` pass through unchanged.
- `register(name, *, directory=None, **settings)`:
  - without `directory`, the name must be a plain directory name: non-empty, no path separators, not `.` or `..`, not absolute (A12);
  - two names that resolve to the same directory raise `ValueError`: they'd share a `Store`, and since `size_limit` is persisted, the last opener would silently win;
  - registering a name twice raises `ValueError`, and so does registering after the first lookup of that name. Nothing opens yet.

**Lookup, without a lock (A11):**
- One `_resolved: dict[str, CacheLike]` holds exactly what `caches[name]` returns. The fast path is `return self._resolved[name]`: one dict read, no clock, no lock.
- The slow path opens outside any lock, creating the directory (`parents=True`), then `winner = self._resolved.setdefault(name, opened)`, and closes the loser if two threads raced. That's safe because `Store::open` already shares one `Store` per database path and handles racing opens (`store.rs:312-338`). No registry lock means no at-fork machinery either.
- An unregistered name raises `KeyError`.

**Open failures:**
- Only operational errors become a `NullCache`: `OSError` (which includes `Timeout`), `FormatMismatchError`, and the not-WAL `RuntimeError`. `TypeError` and `ValueError` from a bad setting *value* raise, because they're configuration bugs (A10).
- On a failure, `caches[name]` returns a plain `NullCache()` and calls `on_open_error(name, exc)` (default: log at `ERROR` on the `diskcache_oxide` logger). The failure is kept in a separate dict with a deadline `retry_open_after` seconds out (10 s, Michael's choice), so the fast path never reads a clock, and the open is tried again after that. Two threads can race to report the same failure, which is accepted.
- A private `_monotonic=time.monotonic` parameter lets tests move time; it's separate from `Cache`'s `clock=`, which is for expiry (A13).

**Switches:**
- `disable(*names)` / `enable(*names)` change later lookups only, by popping `_resolved`. A disabled name returns `NullCache(invalidate=real)`, with the real cache opened as usual so invalidations land (if the open fails, a plain `NullCache`). A caller that already holds the real `Cache` keeps it. That matches how Talk Python uses switches (flags read once at startup), and it keeps lookups free of a proxy and its extra frame. The docs say so.
- `enabled(name)`; unknown names raise `KeyError`.

**Admin and lifecycle:**
- `stats()` → `dict[str, CacheStats]`, a frozen dataclass with `name`, `directory`, `enabled`, `count`, `volume` and `error` (`str | None`). It opens every registered cache, as an admin page wants, and records a failure in `error` instead of raising.
- `clear(name, *, timeout=None)` → `int`. It clears the real cache even when the name is disabled, and raises if the cache can't be opened: an admin action must not report 0 for a failure.
- `expire_all(*, timeout=None)` → `dict[str, int | None]`, with `None` for a cache that failed; a failure is logged and the rest still run.
- `close_all()` closes every opened handle and forgets them; a later lookup reopens. A lookup racing `close_all` can hand out a handle that's no longer tracked: it still works, since a closed handle reopens on use, and the docs say so. `__enter__`/`__exit__` call `close_all`.
- **Typing:** `class Caches[N: str = str]` (PEP 696 defaults work on the 3.14 floor), so `Caches[Literal['general', 'show_pages']]` makes `caches['genral']` a type error, and an untyped `Caches(...)` is `Caches[str]`. `caches[name]` is typed as `CacheLike`.

**What stays in the app:**
- reading the settings file and env vars (the app passes `base_dir`);
- purposes and their mapping onto physical caches: three lines on top of `caches[...]` and `NullCache(invalidate=...)`;
- descriptions for the admin page;
- Sentry and logbook wiring, and the MP3/HTTP helpers;
- when `expire_all()` runs. The hourly lazy expiry inside a lookup is deliberately not copied: a 200k-key expire takes seconds, and inside a request it stalls the whole single-threaded worker. The app should run it from a scheduled job until the background sweeper lands.

**Tests** (`tests/unit/test_caches.py`):
- lazy open, memoized identity, directory creation, per-cache settings overriding defaults, an unknown default keyword raising at construction;
- name validation (separators, `..`, absolute), two names on one directory, duplicate and post-lookup registration; unknown names raise `KeyError` everywhere;
- 16 threads racing `caches[name]` on 3.14t all get the same object, and the extra handles are closed;
- an open failure (a file where the directory should be, and a planted future-format database) returns a `NullCache`, reports, retries after 10 s with a fake `_monotonic`, and recovers; a bad setting value (`size_limit=True`) raises instead;
- the registry's `on_decode_error` callable gets `(name, key, exc)`;
- switches: a disabled name returns a `NullCache` whose `delete` and `clear` reach the real cache; enable returns the real one again;
- `stats()` on a mix of good, failing and never-opened caches; `clear` on a failing cache raises; `expire_all` returns `None` for one;
- `clear` and `expire_all` with a per-call timeout, against a peer holding the lock;
- `close_all`, the context manager, and a lookup after `close_all` reopening;
- fork: a child forked while other threads are mid-lookup can use the registry.

## Docs and bookkeeping

- User guide: a page for `Caches`, the `on_decode_error` section on the `Cache` page, and a "Coming from `DurableCache`" migration note (the mapping table from vision.md).
- Neither feature changes diskcache behavior by default, so no divergence entries. Step 0 is a bug fix in oxide's own `pop`. CHANGELOG entries for all of them.
- `04-design.md`: the codec-compatibility rule from step 2b.
- vision.md: mark the section as built, pointing here.
- Roadmap Phase 3: adopt `Caches` and `on_decode_error`; the purposes layer and the scheduled `expire_all` stay in the app.

## Progress

(Filled in as the steps land.)
