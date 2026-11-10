# Plan 24: Phase 6, parity with diskcache where it fits the vision

Written 2026-11-10, on the branch `phase-6-parity` (cut from `plan-23-deferred-tests`, PR #2, so the two don't conflict in the core's test modules). Built from the parity inventory of diskcache 5.6.3 against oxide 0.1.2 (`abb66fd`) and Michael's interview the same day. Four releases, in this order (Michael), each committed on the branch when it's done, each green on 3.14 and 3.14t, and each reviewed by the architect: the plan before work starts, and each release when it ends. Conformance tests are written and green against diskcache 5.6.3 before oxide's implementation (roadmap, Phase 6).

## Decisions (Michael, 2026-11-10)

1. **Key types:** `str`, `bytes`, `int` (64-bit), `float`, `bool`, `None`, and tuples of those, nested. Our own canonical encoding, never pickled. Any other type raises `TypeError`, as today.
2. **Key equality is type-exact:** `1`, `1.0` and `True` are three keys. A divergence that narrows D-004 (diskcache: `1` and `1.0` are one key, `True` another).
3. **Keys and `memoize` ship as 0.2.0, with no cache deletion.** The schema and `FORMAT_VERSION` don't change, and 0.1.2 never matches rows of a key type it doesn't know, so mixed versions during a deploy are safe. The changelog says first that no cache needs deleting.
4. **One format bump for all of Phase 6, after the format-free work** (release 3). Its schema is designed before release 1 ships, so the key encoding and the tag column are designed together.
5. **Tags and `evict(tag)` are in, always indexed** (a partial index). No `tag_index` setting; `create_tag_index`/`drop_tag_index` are absent.
6. **Eviction:** `eviction_policy` gains `'none'` beside least-recently-stored. LRU and LFU are out: in diskcache's design every `get` becomes a write, and write-free reads are load-bearing (roadmap). The reasons are written in the divergences and on the roadmap page, which retracts "eviction policies are coming". They come back only with a design that keeps reads write-free.
7. **Statistics are per process:** `stats(enable=True, reset=False)` with diskcache's signature, counted in memory, not persisted and not shared across processes. They reset when the process restarts; expiry and eviction never read them. A divergence.
8. **`retry=` on every call that takes it in diskcache**, looping on a plain `Timeout` as `transact(retry=True)` does (D-030). `cache[k]`, `cache[k] = v` and `del cache[k]` keep raising `Timeout` after `timeout`, where diskcache retries forever: a D-entry, as D-034 already says for FanoutCache.
9. **Queue calls in, collections later:** `push`, `pull`, `peek`, `peekitem` in release 4. `Deque` and `Index` deferred until someone asks, with the reasons on the roadmap page.
10. **Coordination recipes raise on a `NullCache` or a `durable=True` handle.** A lock that silently doesn't lock is worse than an error.
11. **`disk=`, `Disk` subclasses and `JSONDisk` are out for good.** The README and `index.qmd` move them from "not here yet" to the divergences.
12. **Django: an oxide backend of its own, as its own phase right after Phase 6** ("it's going to be a big job"). diskcache's `DjangoCache` stays out; the roadmap's non-goal is reworded so it and the vision agree.
13. **Windows after Phase 6**, as its own plan.
14. **`read=True` and `read()` fold into the vision's large-file cache**, and the roadmap page says so.
15. **`memoize`:** diskcache's key shape exactly (`(name-or-qualname,) + args + (None,) + sorted kwargs flat`), so `__cache_key__` returns the same tuple diskcache would. `typed=True` appends each type's `'module.qualname'` string where diskcache appends the class (a divergence: we can't store a class without pickle). An argument that isn't a key type raises `TypeError` naming it, after nothing has run. `async def` functions are refused at decoration with a `TypeError` until `AsyncCache` is decided.

## Release 1 (0.2.0): keys, `memoize`, `retry=`

### The key encoding

- **`str` keys are unchanged:** `key_type = 1`, the UTF-8 text in the TEXT column, as every existing row is. Nothing about a `str` key's storage, lookup, shard or order changes, so existing caches keep working and 0.1.2 and 0.2.0 agree on every `str` key.
- **Every other key is one new `key_type = 2` ("typed"), stored as a BLOB** (the `key` column has TEXT affinity, under which a bound integer would become text; a BLOB keeps its bytes). The bytes are an order-preserving tuple-layer encoding in the style of FoundationDB's: a type code byte, then the value.
  - `None` `0x00`; `False` `0x01`; `True` `0x02`; `int` `0x03` + 8 bytes big-endian with the sign bit flipped (byte order is numeric order); `float` `0x04` + 8 bytes IEEE 754 with the total-order transform (negative: flip all bits; positive: flip the sign bit), NaN refused (it isn't equal to itself, so it can't be a key) and `-0.0` normalized to `0.0`; `bytes` `0x05` + the bytes with `0x00` escaped as `0x00 0xFF` + terminator `0x00`; `str` inside a tuple `0x06` + UTF-8, escaped and terminated the same way; tuple `0x07` + elements + terminator `0x00`.
  - A lone non-`str` key is its one element (`b'x'` is `0x05 'x' 0x00`); a tuple key is `0x07 …`. So a key's bytes depend only on its value and type, in every process and release (golden vectors pin each type's bytes and its shard).
  - Within the typed keys, byte order sorts by type code, then by value, element by element for tuples, with a tuple sorting before any longer tuple it is a prefix of.
- **Ints past 64 bits raise `TypeError`** in release 1 (diskcache pickles them). A later code can add them without changing these.
- **The encoding lives in the extension** (`crates/diskcache-oxide-py/src/codec/key.rs`), as the vision asks; the core deals in `(key_type, bytes)` pairs and never interprets typed bytes.

### Engine (core)

- A `Key<'a>` type: `Key::Str(&str)` or `Key::Typed(&[u8])`, binding as TEXT with `key_type = 1` or as a BLOB with `key_type = 2`. Every keyed call takes `Key` where it took `&str` today (18 functions in `ops.rs`, `rows.rs`, `maintenance.rs`), and the iterators and `keys_page` return an owned key (`OwnedKey::Str(String)` / `OwnedKey::Typed(Vec<u8>)`).
- The shard hash already takes the type byte (`shard_hash(key_type, bytes)`, `fanout.rs`): typed keys hash `(2, encoded bytes)`, and `str` keys are unchanged. `HASH_IDENTITY` and the manifest don't change.
- **Order:** `iterkeys()` is `(key_type, key)` order: every `str` key first (as today), then the typed keys in encoding order. diskcache orders numbers, then text, then BLOBs, by SQLite's comparison. A divergence; `prefix=` stays `str`-only (it filters to `key_type = 1`), and `iterkeys(prefix=…)` with a non-`str` prefix raises `TypeError`.
- `iter(cache)` (insertion order) yields keys of every type. FanoutCache's k-way merge compares `(key_type, bytes)`.

### Binding and Python

- One key conversion (`cache.rs`'s key check becomes `codec::key::encode`), used by every keyed method, `transact(key=)`, `shard_of`, and `on_decode_error`'s callback, whose key argument becomes the decoded key.
- Decoding a typed key back to Python, for iteration: exact types back (`int` stays `int`, a tuple of a `str` and `None` comes back so).
- Stubs and protocols: a `Key` type alias (`str | bytes | int | float | bool | None | tuple[Key, ...]`) for every key parameter, and `Iterator[Key]` for iteration. `NullCache._check_key` takes the same set. `tests/unit/test_stubs.py` keeps them honest.

### `memoize`

- `Cache.memoize`, `FanoutCache.memoize` and `NullCache.memoize`, with diskcache's signature `memoize(name=None, typed=False, expire=None, tag=None, ignore=())`. `tag=` is accepted only from release 3 (until then it isn't in the signature, rather than accepted and ignored). The logic is pure Python (`python/diskcache_oxide/_memoize.py`); the compiled classes delegate to it.
- `@cache.memoize` without parentheses raises `TypeError('name cannot be callable')`, as diskcache does. `expire=0` or negative means "never store"; `None` results are cached; an exception stores nothing. `wrapper.__cache_key__(*args, **kwargs)` returns the key tuple.
- On a `NullCache` the function runs every time (instant expiry). Under `durable=True` an outage runs the function and drops the store (D-028), with no special case.

### `retry=`

- Keyword-only `retry=False` on `set`, `add`, `get`, `pop`, `delete`, `touch`, `incr`, `decr`, `expire`, `cull`, `clear` (and later `evict`, `push`, `pull`, `peek`, `peekitem`), on `Cache`, `FanoutCache`, `NullCache` and the protocols. With `retry=True` a plain `Timeout` is retried with the call's own `timeout` each round, until it succeeds; nothing else is retried (`TransactionBusy`, `RuntimeError` in a poisoned child, decode errors). The loop sleeps between rounds with the interpreter released and checks for signals, so Ctrl-C ends it. Under `durable=True` there's nothing to retry (an outage is already a miss).

### Conformance and divergences

- Conformance first, against diskcache: per-type key round trips (`test_non_str_keys_round_trip` stops being a D-004 xfail), keys mixed across types, `memoize`'s key shape, `typed`, `ignore`, `name`, `expire <= 0`, cached `None`, an exception storing nothing, `__cache_key__`, and `retry=` accepted everywhere.
- Rewrite D-004: the key types, compared by exact type (`1`, `1.0`, `True` three keys), no pickled keys, no ints past 64 bits. New entries: `iterkeys()` order across types; `memoize(typed=True)` stores type names; a non-key argument to a memoized function raises; dict syntax raises `Timeout` on `Cache` (D-034's rule extended); `get`'s `expire_time` and `iterkeys`'s `reverse` keyword-only; and the other small gaps the inventory found with no entry (section 1.9).
- Oxide-only tests: golden vectors for every type's bytes and shard; order-preservation by property test (Hypothesis: `encode(a) < encode(b)` iff `a < b` within one type); round trips through iteration; a 0.1.2 binary on a directory with typed keys (a test that opens a database holding type-2 rows with the 0.1.2 queries, so mixed versions are shown safe); the model test (`test_model.py`) extended to mixed key types.

### Docs

README, `index.qmd`, the switching guide, the API reference, the skill and `llms.txt`: key types, `memoize`, `retry=`. `CHANGELOG.md` gets 0.2.0, opening with "no cache needs deleting". The roadmap page records decisions 6, 9, 11, 12, 13 and 14.

## Release 2 (0.3.0): small parity and the recipes, no format change

- `reversed(cache)`, `pop(expire_time=)`, pickling `Cache` and `FanoutCache` (rebuilt from directory and options; a `clock=` or `on_decode_error=` that won't pickle raises), setters or `reset` for `size_limit` and `cull_limit`, per-process `stats()` (decision 7; its name sits beside `Caches.stats()`, which keeps its meaning), `check(fix=False, retry=False)` (integrity, rows against files, orphan `.val` files, totals; under the write lock), the warning classes `check()` uses, and the `ENOVAL`/`UNKNOWN` exports if `memoize`-style code needs them.
- Recipes in Python: `Lock`, `RLock`, `BoundedSemaphore`, `throttle`, `barrier`, `Averager`, `memoize_stampede`. They raise on a `NullCache` or `durable=True` handle (decision 10). On a FanoutCache they use `transact(key=)`. `memoize_stampede` can't copy diskcache's `with cache:` (which closes our handle, D-018).

## Release 3 (0.4.0): format 3, tags and `eviction_policy='none'`

- A `tag` column with a partial index, `tag=` on writes and `tag=True` on reads, `evict(tag, retry=False)` chunked and bounded like `clear()` (D-011), fanned out per D-035. `memoize(tag=)` turns on.
- A persisted `eviction_policy` setting, `'least-recently-stored'` (default) or `'none'`; any other value raises `ValueError` naming why LRU/LFU are out.
- `FORMAT_VERSION` 3: a minor bump, caches deleted on upgrade, every worker stopped, said first in the changelog.

## Release 4 (0.5.0): queue calls

- `push(value, prefix=None, side='back', expire=None, tag=None, retry=False)`, `pull`, `peek`, `peekitem`: one Rust call each, finding the end of a key range and inserting or removing inside one write transaction. With `prefix=None` the keys are ints from 500,000,000,000,000 (diskcache's), with a prefix `'{prefix}-{n:015d}'` strings.
- `Deque` and `Index` stay deferred (decision 9).

## Risks

- **The encoding is permanent** once 0.2.0 ships: it is a key's identity and its shard. Golden vectors, the architect's review of this section before any code, and property tests are the guard.
- **The `&str` to `Key` change touches every keyed path** in the core, including the D-013/D-016 read paths. The existing suite, the model test and the soak run unchanged on `str` keys; a soak with mixed key types runs before 0.2.0.
- **Typing churn:** `Iterator[str]` becoming `Iterator[Key]` reaches every caller that iterates. Talk Python, Python Bytes and Training use `str` keys only and iterate rarely; check them before release.

## Log

- 2026-11-10: plan written from the parity inventory and Michael's interview (decisions above). Next: the architect reviews this plan, the encoding section first.
- 2026-11-10: release 1's conformance tests, written first and green on diskcache 5.6.3 and the community fork, skipped on oxide until each feature turns on (new feature flags `key_types` and `retry` in `backends.py`): `test_keys.py` (every key type round trips through every keyed call and iteration, types stay distinct, `-0.0` is `0.0`, `1`/`1.0` one key on diskcache as D-044, other hashables as D-004) plus a FanoutCache case in `test_fanout.py`; `test_memoize.py` (diskcache's key shape, unbound arguments, `name`, `ignore`, `expire <= 0`, cached `None`, exceptions, `__wrapped__`, `typed` as D-045, FanoutCache); `test_retry.py` (a retried write waits out another process's lock; every call takes `retry=`). D-044 and D-045 written.
