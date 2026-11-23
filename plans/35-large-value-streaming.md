# Plan 35: a large-file cache, as streamed values (design spike)

Status: draft (2026-11-22), reviewed once by the-rust-architect (ready with changes, all folded in; see the Log). A time-boxed design spike: prototypes and a written design, then a go or no-go from Michael before any shipped code. Michael's decisions (2026-11-22): the main job is large blobs that Python code writes and reads back, not files a web server hands out by path; and the spike comes first, the build is a separate decision.

## The idea, narrowed

The vision's "large-file cache" imagined a sibling class that stores whole files unmodified and hands out paths for `sendfile`. Michael's choice of use case moves the centre: model outputs, datasets, rendered documents and downloads that the application itself reads. For those, the feature is a value that never has to be in memory at once:

- store from a file object or an iterator of chunks, with memory use that doesn't grow with the value;
- read back as a file object, with the same bound;
- everything a cache does for any other key still applies: expiry, tags, `size_limit` and culling, `delete`, `add()`'s atomicity, `check()`, the cross-process lock.

diskcache already has an API for exactly this: `cache.set(key, fileobj, read=True)` and `cache.get(key, read=True)` or `cache.read(key)`, which returns an open binary file. Plan 24's decision 14 folded those into this idea. So there is an oracle, which is how every other feature in this project started.

Handing out a path, and moving a file in without copying it, become secondary. The spike only has to say whether the design leaves room for them.

## What oxide does today

Values of 64 KiB or more already live in their own file (`spill.rs`): written to a temp name with no lock held, renamed and committed inside the write transaction, the replaced file unlinked after the commit, read back with a size check and a retry. The crash story, the orphan scan in `check()`, the quota accounting and the Windows share-delete handling exist and are soaked.

What's missing is only the streaming. `set` takes the whole encoded value as one slice; for `bytes` that is the caller's own buffer, with no extra copy. `get` reads the file into a buffer and then copies it into a `bytes` object, so about twice the value at its peak. The spike measures both first, because that is the case for the feature.

One fact from the review shapes everything below: a `bytes` value file is already the raw payload, with no framing, under the row's mode for `bytes`. So a streamed value can be an ordinary `bytes` value on disk, and nothing new has to be written there.

## Questions the spike answers

1. **How bad is it today?** Peak memory and throughput for `set` and `get` of `bytes` at 1 MiB, 100 MiB and 1 GiB, on oxide and on diskcache with and without `read=True`. Is there a size at which oxide fails outright?
2. **What goes on disk?** Answered in the review, to be proven in step 2: nothing new. A streamed value is stored as an ordinary `bytes` value file, so 0.5.8 and every later version reads it back as `bytes`, there is no new value mode and no format version bump, and the release tier is Full (`spill` and `ops` change), not Format. If a new mode were ever wanted, an older build would treat it safely: it refuses the row, never reads the file, raises `ForeignValueError` from `get`, and neither `pop` nor `on_decode_error` deletes it. Still to prove with the 0.5.8 wheel: a value file under 64 KiB reads back (the threshold is checked only on the write path), and so does a 0-byte one.
3. **Where does the API live?** Three shapes to prototype against real calling code:
   - diskcache's own: `set(key, fileobj, read=True)`, `get(key, read=True)`, `read(key)` on `Cache` and `FanoutCache`. It has the oracle and is what a diskcache user expects.
   - explicit methods with oxide's stricter style, for example `set_stream(key, source)` and `open(key)`, where `source` is a binary file object or an iterable of `bytes`.
   - the vision's sibling class.
   The spike recommends one. Mixing is possible (diskcache's names as thin aliases over explicit methods), and the divergence list depends on the choice.
4. **What does a streamed write promise?** The size isn't known until the source ends. So: when is `size_limit` checked, and what happens to a value larger than the whole limit; what a failing source leaves behind (the temp file, swept as plan 19 already does); what a full disk raises; whether a slow source holds anything (it must hold no lock, no connection and no gate level while it reads, as a spilled write does today); and what `add()` means when two writers stream the same key.
5. **What does a returned file promise?** D-017 and D-018 promise that no descriptor inside the cache directory stays open after the last `close()`; a returned file breaks both as written, so the design must say what the promise becomes. A reader gets the version it opened even if the key is overwritten, deleted or culled a moment later: an unlinked open file on POSIX, share-delete on Windows. Then: what type is it (a real Python file from `os.fdopen`, or our own reader object), how it counts against the fd budget of D-017, what happens to it after `cache.close()` and across a `fork()`, and what a value file left short by a crash reads as (today a miss, by the size check).
6. **Who else has to know?** `FanoutCache` routing, `NullCache`, the `Caches` registry, the `CacheLike` protocols, `durable=True` (what does a failed streamed read return?), `DjangoCache`, `memoize`, `transact()` (a streamed write inside a held transaction would hold the write lock for the length of the stream, so it's probably refused), and the maintenance thread.
7. **Does a non-streamed read of a streamed value work?** Yes, by the answer to question 2: `cache.get(key)` returns the bytes, as diskcache does. What's left to decide is whether `cache[key]` on a 1 GiB value should do that quietly.
8. **Is there room for paths later?** If the file on disk is the raw bytes, `get_path(key)` and a move-in `set` are small additions with one hard question each (a path outlives the file; a move crosses filesystems). The spike notes what the design would need, and builds neither.

## Steps

**Step 1: measure today** (question 1). A script under `bench/` that records peak RSS and throughput per size, on oxide and on diskcache. The memory-instrumented build (`.venv-mem`) gives SQLite's share. Result in `bench/results/`.

**Step 2: prove the on-disk answer** (question 2). With the released 0.5.8 wheel: place a raw file as a `bytes` value the way a streamed write would, at 0 bytes, under 64 KiB and well over it, and record what `get`, `iterkeys`, `check()` and `check(fix=True)` do. This step ends with the tier confirmed or the design changed.

**Step 3: the oracle's behaviour.** Conformance tests for diskcache's `read=True` and `read()`, written against diskcache 5.6.3 first as Phase 0 did: what `set` accepts, what `get` returns for each combination of how the value was stored and how it is read, misses and defaults, expiry, overwrite while a handle is open, `add`, `pop`, `incr` on such a key, the tag and expire arguments, and what `FanoutCache` does. Any diskcache bug found goes to `upstream/README.md` with a repro. These tests stay on the spike's branch until there's a go.

**Step 4: the prototype**, on one operating system (macOS), on a branch, never merged as is:

- a streamed write in the core: chunks to the existing temp file, the size counted as it goes, then today's rename and commit;
- a streamed read: open the value file, check its size against the row, hand back the file;
- the thinnest Python surface that lets step 3's tests and step 1's benchmark run against it;
- then the same measurements as step 1, and the peak-memory claim as a test (`tests/unit/test_memory.py` has the pattern).

**Step 5: the hard cases, on paper, with an experiment only where the answer isn't clear from the code.** The review's point was that a full matrix of kills and platforms is most of the build, so it moves to the build plan. The spike must still give each of these a designed answer:

- **A fork inside the source.** No gate level is held while the source is read, so after a `fork()` in the source's own code both parent and child would carry on the stream on one shared file offset and both commit. The write needs a generation check per chunk and at commit, and the child must not unlink the temp file.
- **Retries with a one-shot source.** The write closure can run more than once, and `durable` retries. The file is written once, before the transaction, and a drained source is never read again.
- **A stalled source.** The temp sweep removes a `.tmp` past its minimum age, so a source stalled for an hour loses its file, and `timeout` no longer bounds the call. The design says what bounds it.
- **Windows.** The returned handle must be opened in Rust with share-delete and wrapped, never Python's `open()`, and a handle a caller holds for a long time keeps the file in the delete-pending state for that long.
- Overwrite, delete, cull, `clear()` and a rolled-back transaction while a reader holds the file.
- When the `expire` clock starts (at the call or at the commit), and that bytes in flight aren't charged to `size_limit` until the commit.
- A value larger than `size_limit`, and a full disk.
- A `fork()` with a returned file open, and many open handles against the fd limit.
- One returned reader shared by several threads on 3.14t.
- A source that raises, or that yields `str` or another wrong type.

**Step 6: the design.** Written into this plan: the API with signatures and types, the write and read protocols with their crash and concurrency arguments, an answer to every item in step 5, every divergence from diskcache as a draft `D-NNN` entry (the reworded D-017 and D-018 among them), the gate it needs, the docs it needs (a guide page, the reference, the skill, the switching-from-diskcache page, the roadmap page that today says `read=True` "folds into the large-file cache"), and the build as a list of steps, with the kill and platform matrix as one of them. The architect reviews it.

**Step 7: the interview.** Michael decides go or no-go, and if go: the API shape, the name, and whether path handout is part of the first release or left for later.

## What would make this a "no"

- The oracle's behaviour turns out to need a design that puts work or a write on the read path of ordinary values.
- Step 2 disproves the on-disk answer and the feature turns out to need a format change after all.
- The measured gain is small: if today's cost at 100 MiB is tolerable and nobody is storing more, the feature is complexity without a user. Michael's own use for it is the best evidence either way, so the interview asks what he would store.

## Constraints the design must keep

- A streamed value is a new way to write and read a value file, not a second storage engine. The write lock, the fork gate, the pool and the WAL are not changed.
- Nothing on the read or write path of ordinary values gets slower: `bench.gate` stays GO.
- No Python code runs while a lock, a connection or a gate level is held. A source is read with none of them held.
- No pickle, and a value the feature can't take raises `TypeError` at the call (D-001's rule).
- `Optional[X]` in every signature, no leading underscores in new names, and the full docstrings in `_core.pyi`.

## Deliverables

The benchmark and its results, the conformance tests on the branch, the prototype as a branch, the design section of this plan with the architect's review folded in, and the interview. Time box: three working sessions. No release comes out of this plan.

## Log

- 2026-11-22, the-rust-architect's plan review: ready with changes, all folded in. It confirmed the description of today's `set` and `get` from the code and answered question 2 from it: a `bytes` value file is the raw payload, so a streamed value needs no new mode and no format bump, and the tier is Full, not Format; an unknown mode would in any case be refused safely by an older build. Missing hard cases it required: a fork inside the source callback, retries with a one-shot source, D-017 and D-018's promise about descriptors, the Windows handle and its delete-pending state, the temp sweep's minimum age against a stalled source, and four smaller ones. It reordered the steps (the on-disk proof before the oracle tests) and moved the Windows and kill matrix out of the spike, as most of a build. A docstring nit it found on the way, not part of this plan: `codec/mod.rs` says `CorruptValueError` for an unknown mode where the code raises its `ForeignValueError` subclass.
