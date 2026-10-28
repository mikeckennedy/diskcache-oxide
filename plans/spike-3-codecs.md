# Spike 3: codecs (results, 2026-10-28)

Parent: [03-phase-1-foundation-spikes.md](03-phase-1-foundation-spikes.md), "Spike 3: codecs".

## Summary

- **JSON library: serde_json**, with the `float_roundtrip` feature (it's required; without it, about 1 in 1,500 random floats came back one ULP off). sonic-rs is 10-60% faster on string-heavy values but has a real correctness bug: 0.5.10 decodes `-0.0` as `+0.0`. It also adds 30 crates and roughly 430 `unsafe` sites. Not worth it now.
- **msgpack ships in Phase 2.** On the 450 KB page entry it round-trips in 11-16 µs against 261-389 µs for tagged JSON, and stores 461 KB instead of 614 KB. Tagged JSON's decode alone (141 µs) is about 5x diskcache's entire `get_page_entry_450k` (29 µs). msgpack is also faster than our JSON on every other payload measured.
- **Second latency gate: projected PASS with a wide margin.** diskcache's pickle `get` of `mcp_result()` measured 2.71 µs (3.14) and 2.48 µs (3.14t). The projected oxide `get_hit_small_dict` is about 1.3 µs (0.47×) on 3.14 and 1.36 µs (0.55×) on 3.14t.
- **The TBD value cases all become `roundtrip`:** `date`, `Decimal`, `set` (and `frozenset`), and `ZoneInfo` datetimes. naive and fixed-offset datetimes with `fold=1` also round-trip; I first rejected them, but `datetime.now()` returns `fold=1` during the DST fall-back hour.
- Every codec passes 30,000 Hypothesis examples on 3.14 (exact values and exact types, recursively) and 1,500 on 3.14t, plus the `value_cases.py` table and 19 explicit rejected types. The only failures are the two sonic-rs `-0.0` cases, marked xfail.

## Setup

- Spike code is in `spikes/codec/`, a standalone Cargo workspace building the `dco_codec_spike` extension: PyO3 0.30.1, `#[pymodule(gil_used = false)]`, release build with thin LTO. It's built into `spikes/codec/.venv` (3.14.10) and `.venv-ft` (3.14.10t); the GIL stays off on 3.14t.
- `src/json.rs`: one `Serialize` impl over Python objects, which serde_json and sonic-rs both drive, and one `DeserializeSeed`/`Visitor` that builds Python objects directly. Dict keys go through a direct-mapped, 1024-slot, per-thread cache for keys up to 64 bytes.
- `src/mp.rs`: a msgpack encoder on `rmp::encode` and a hand-written slice decoder; rmp-serde isn't used.
- `src/common.rs`: tag names, cached types, the key cache, and datetime/date/Decimal handling.
- `tests/test_codec.py` holds the tests and `bench_codec.py` the benchmark. Raw output is in `results-gil.md` and `results-ft.md`.
- Timing uses the bench/baseline.py method: batched p50, the median over batches of about 2 ms each, with the harness loop overhead subtracted, on the same machine and in the same session. Every row includes the conversion to and from Python objects. orjson has no 3.14t build, so it appears only in the 3.14 table.
- **Exact** means `exact_equal(decode(encode(v)), v)` from `tests/conformance/value_cases.py`. msgspec json returns base64 text for bytes, and msgspec json and orjson return ISO strings for datetimes, so they fail it where noted.

## Results

### 3.14 (GIL), µs per call

| Payload | Codec | Stored size | Encode | Decode | Round trip | Exact |
|---|---|---|---|---|---|---|
| page_entry_450k | oxide json (serde_json) | 614,494 B | 247.43 | 141.12 | 388.55 | yes |
| page_entry_450k | oxide json (sonic-rs) | 614,494 B | 126.64 | 134.56 | 261.20 | yes |
| page_entry_450k | oxide msgpack (scratch buffer) | 460,872 B | 10.33 | 5.23 | 15.56 | yes |
| page_entry_450k | oxide msgpack (direct write) | 460,872 B | 5.46 | 5.26 | 10.71 | yes |
| page_entry_450k | pickle p5 | 460,910 B | 5.57 | 5.44 | 11.01 | yes |
| page_entry_450k | msgspec json | 614,483 B | 368.20 | 103.75 | 471.95 | no |
| page_entry_450k | msgspec msgpack | 460,872 B | 5.28 | 5.36 | 10.65 | yes |
| page_entry_450k | orjson | refused (bytes) | | | | |
| mcp_result | oxide json (serde_json) | 232 B | 0.37 | 0.50 | 0.86 | yes |
| mcp_result | oxide json (sonic-rs) | 232 B | 0.35 | 0.42 | 0.77 | yes |
| mcp_result | oxide msgpack (scratch) | 201 B | 0.28 | 0.39 | 0.67 | yes |
| mcp_result | pickle p5 | 233 B | 0.39 | 0.49 | 0.88 | yes |
| mcp_result | msgspec json | 232 B | 0.25 | 0.33 | 0.59 | yes |
| mcp_result | msgspec msgpack | 201 B | 0.16 | 0.28 | 0.43 | yes |
| mcp_result | orjson | 232 B | 0.16 | 0.31 | 0.47 | yes |
| rate_limit_keys_200 | oxide json (serde_json) | 11,691 B | 4.15 | 4.87 | 9.02 | yes |
| rate_limit_keys_200 | oxide json (sonic-rs) | 11,691 B | 1.52 | 3.98 | 5.50 | yes |
| rate_limit_keys_200 | oxide msgpack (scratch) | 11,493 B | 1.73 | 3.93 | 5.66 | yes |
| rate_limit_keys_200 | pickle p5 | 11,706 B | 3.96 | 3.79 | 7.75 | yes |
| rate_limit_keys_200 | msgspec json | 11,691 B | 2.76 | 4.42 | 7.18 | yes |
| rate_limit_keys_200 | msgspec msgpack | 11,493 B | 1.52 | 3.18 | 4.70 | yes |
| rate_limit_keys_200 | orjson | 11,691 B | 0.67 | 4.29 | 4.96 | yes |
| asdict_job_status | oxide json (serde_json) | 275 B | 1.12 | 0.54 | 1.67 | yes |
| asdict_job_status | oxide json (sonic-rs) | 275 B | 1.09 | 0.45 | 1.54 | yes |
| asdict_job_status | oxide msgpack (scratch) | 163 B | 0.32 | 0.36 | 0.68 | yes |
| asdict_job_status | pickle p5 | 301 B | 1.90 | 1.20 | 3.10 | yes |
| asdict_job_status | msgspec json | 237 B | 0.18 | 0.28 | 0.46 | no |
| asdict_job_status | msgspec msgpack | 163 B | 0.13 | 0.25 | 0.39 | yes |
| asdict_job_status | orjson | 247 B | 0.22 | 0.28 | 0.49 | no |
| nested_10k | oxide json (serde_json) | 8,606 B | 11.76 | 21.58 | 33.34 | yes |
| nested_10k | oxide json (sonic-rs) | 8,606 B | 10.40 | 19.13 | 29.53 | yes |
| nested_10k | oxide msgpack (scratch) | 6,644 B | 9.66 | 17.80 | 27.46 | yes |
| nested_10k | pickle p5 | 5,266 B | 10.55 | 14.06 | 24.61 | yes |
| nested_10k | msgspec json | 8,606 B | 5.70 | 13.62 | 19.32 | yes |
| nested_10k | msgspec msgpack | 6,644 B | 4.77 | 11.92 | 16.69 | yes |
| nested_10k | orjson | 8,606 B | 4.71 | 12.71 | 17.42 | yes |

The payloads:
- `page_entry_450k` and `mcp_result` are copied from bench/baseline.py.
- `rate_limit_keys_200` is 200 strings of about 55 characters each.
- `asdict_job_status` is the `dataclasses.asdict` output of a 10-field JobStatus with two aware-UTC datetimes and one `None`.
- `nested_10k` is 28 episode dicts, each with nested guest dicts and lists.

### 3.14t (free-threaded), round trip in µs (encode + decode)

| Payload | oxide json (serde_json) | oxide json (sonic-rs) | oxide msgpack (scratch / direct) | pickle p5 | msgspec json | msgspec msgpack |
|---|---|---|---|---|---|---|
| page_entry_450k | 415.01 | 270.50 | 17.79 / 10.92 | 11.11 | 471.29 (not exact) | 10.95 |
| mcp_result | 1.14 | 1.03 | 0.95 | 0.84 | 0.71 | 0.59 |
| rate_limit_keys_200 | 10.26 | 6.17 | 6.48 | 7.50 | 6.92 | 4.64 |
| asdict_job_status | 1.93 | 1.80 | 0.88 | 3.25 | 0.57 (not exact) | 0.52 |
| nested_10k | 42.99 | 37.57 | 36.55 | 25.58 | 24.51 | 22.54 |

### Other measurements

| Measurement | 3.14 | 3.14t |
|---|---|---|
| diskcache 5.6.3 `get` of a 100-char str | 1.95 µs | 1.69 µs |
| diskcache 5.6.3 `get` of `mcp_result()` (pickle) | 2.71 µs | 2.48 µs |
| oxide JSON decode of `mcp_result`, key cache on / off | 0.505 / 0.618 µs | 0.530 / 0.619 µs |
| oxide JSON decode of `nested_10k`, key cache on / off | 22.4 / 24.8 µs | 23.2 / 25.0 µs |
| `bytes.decode()` of 100 B (what the str get path pays) | 0.016 µs | 0.016 µs |

### Findings

- **The key cache pays for itself**: 18% off `mcp_result` decode and 10% off `nested_10k`. It's per-thread and lock-free, so keep it.
- **Output buffering strategy matters a lot**, and in opposite directions:
  - writing into a reused per-thread scratch `Vec` and then copying once into `bytes` is fastest for small and medium values (msgpack on rate-limit keys: 1.73 vs 3.37 µs written directly);
  - writing directly into `PyBytes::new_with_writer` (PyO3 0.30's `PyBytesWriter`) is fastest for large values (page entry: 5.5 vs 10.3 µs), and on one earlier run the scratch path took 33 µs;
  - Phase 2 should choose by size: scratch by default, and direct when a cheap pre-scan sees a `bytes` or `str` over about 64 KB.
- **Our container encode is about 2x msgspec/orjson** (nested_10k encode: 11.8 µs vs 4.7-5.7 µs). That's the price of PyO3's safe API: owned references and a checked cast per item. Even so, oxide msgpack beats pickle on 4 of 5 payloads. The remaining gap closes with borrowed-reference `PyDict_Next`/`PyList_GET_ITEM` fast paths, which need `unsafe` in the py crate. That's Phase 4 tuning, not a blocker.
- **3.14t encode is slower** (nested_10k: 20.0 vs 11.8 µs). This is likely PyO3's per-step critical sections when iterating dicts and lists on the free-threaded build. Decode is barely affected. Recorded for Phase 4.
- **JSON datetime encode is slow** (about 0.45 µs per datetime) because it calls Python's `isoformat()` and reads `tzinfo`/`fold` through attribute lookups. Formatting in Rust from the C-API accessors would remove most of it. msgpack's UTC timestamp path is already fast.
- **The serde visitor approach works unchanged on sonic-rs**, so the backend choice can be revisited later without touching the conversion code.

## serde_json vs sonic-rs

Recommendation: **serde_json with `features = ["float_roundtrip"]`.**

- **Speed.** sonic-rs is faster where strings dominate: rate-limit keys encode 1.5 vs 4.2 µs, base64-heavy page-entry encode 127 vs 247 µs. On `mcp_result` and `nested_10k` it's only 5-12% faster. On the value the latency gate cares about (`mcp_result` decode), the gap is 0.08 µs.
- **Correctness.** sonic-rs 0.5.10 decodes the JSON text `-0.0` as `+0.0` (serde_json's output for `-0.0` is `-0.0`, and serde_json reads it back correctly). It handles `-1e-400` correctly, so the bug is in the fast path for short numbers. A codec whose whole point is "never silently changes a value" can't ship on a parser that does exactly that. We could work around it by tagging `-0.0`, but that's a sign the parser hasn't had the scrutiny serde_json has.
- **Supply chain.**
  - sonic-rs pulls in 30 crates (serde_json pulls in 5), including ahash, bumpalo, bytes, faststr, simdutf8, ref-cast, zmij and its own sonic-number and sonic-simd.
  - `unsafe` sites, counted by grepping `src/`: sonic-rs has 248, sonic-simd 91, faststr 63 and sonic-number 24, about 426 in total. serde_json has 16, rmp 1 and base64 3.
  - Most of sonic-rs's unsafe code is architecture-specific SIMD, and the aarch64 and x86_64 paths differ, so both would need fuzzing.
  - cargo-deny would need several new license and advisory entries.
- **serde_json's `float_roundtrip` is mandatory.** Without it, Hypothesis found `2.1617277350753162e+17` decoding to `2.161727735075316e+17`. With it, 30,000 examples pass.
- **Revisit sonic-rs** if JSON string throughput ever matters. The better lever for big values is msgpack anyway.

## Tag format spec

This applies to the per-row `mode` values for structured data: `json` (plain), `json_tagged`, and `msgpack`. Top-level `str` and `bytes` values never reach the codec; they use the raw modes.

### Shared rules

1. **Exact types only.** The encoder dispatches on the exact type (`type(v) is T`). Any subclass of a supported type, and any type not listed below, raises `TypeError` at encode time, whether at top level, nested, or as a dict key. This covers StrEnum, IntEnum, IntFlag, namedtuple, OrderedDict, defaultdict, Counter, str and int subclasses, bytearray, memoryview, complex, `time`, `timedelta`, dataclasses and arbitrary objects. All are tested.
2. **A tag** is a map with exactly one entry whose key is a reserved tag name. On decode, a map whose **first** key starts with `$` is read as a tag:
   - an unknown `$` name is a decode error (`ValueError`), never data;
   - a tag map with more than one entry is a decode error.
3. **Reserved namespace and escape.** Every first key starting with `$` is reserved, so future tags can't collide with old data. When the encoder meets a dict whose keys are all exact `str` and whose first key starts with `$`, it writes `{"$esc": {…the dict, entries in order…}}`.
   - Inside `$esc`, the first key isn't checked for a tag. Values are still decoded normally.
   - `$` keys in any position other than first need no escaping.
   - JSON-Schema-style `{"$schema": …}` dicts pay one wrapper and make the row `json_tagged`. That's the only cost.
4. **Dict order** is preserved in all formats. `bool` is checked as its own type before `int`, so it always stays `bool`.
5. **Depth limit.** Nesting is limited to 120 output containers, and each tag wrapper counts as one. The encoder raises `TypeError` beyond that, which also catches cycles such as `a = []; a.append(a)`. Decoders refuse deeper input with `ValueError`: serde_json's own limit is 128, and the msgpack reader stops at 240, so a crafted row can't overflow the stack.

### JSON encoding

| Python value (exact type) | JSON |
|---|---|
| `None`, `bool` | `null`, `true`/`false` |
| `int` in [-2^63, 2^64-1] | a JSON integer |
| `int` outside that range | `{"$int": "<decimal digits, optional leading ->"}` |
| finite `float` | the shortest round-trip form, always with a `.` or an exponent (ryu), so it decodes as a float. `-0.0` is written `-0.0`. Requires `float_roundtrip` on decode. |
| NaN, +inf, -inf | `{"$float": "nan"}`, `{"$float": "inf"}`, `{"$float": "-inf"}` |
| `str` | a JSON string |
| `list` | an array |
| `tuple` | `{"$tuple": [...]}` |
| `dict`, all keys exact `str`, first key not starting with `$` | an object |
| `dict`, all keys exact `str`, first key starting with `$` | `{"$esc": {...}}` |
| `dict` with any key that isn't an exact `str` | `{"$dict": [[k, v], ...]}`, with keys encoded recursively by these same rules |
| `bytes` (nested) | `{"$bytes": "<RFC 4648 standard base64, padded>"}` |
| `datetime` | `{"$datetime": "<text>"}`, see below |
| `date` | `{"$date": "YYYY-MM-DD"}` (`date.isoformat()`) |
| `decimal.Decimal` | `{"$decimal": "<str(d)>"}`, read back with `Decimal(text)`. This is exact, keeping the exponent (`1.10` stays `1.10`), the sign, and `NaN`/`sNaN`/`Infinity`, and doesn't depend on the decimal context. |
| `set`, `frozenset` | `{"$set": [...]}`, `{"$frozenset": [...]}` (elements in iteration order) |

The encoder reports whether it wrote any tag or `$esc`. If it didn't, the row is stored as mode `json`: standard JSON that any reader decodes to the same values. Otherwise the row is `json_tagged`.

### Datetime text

The text is `dt.isoformat()`, followed by optional suffixes in RFC 9557 (IXDTF) syntax.

- **Accepted `tzinfo`:**
  - `None` (naive);
  - `datetime.timezone` without a custom name, checked as `tz.tzname(None) == timezone(tz.utcoffset(None)).tzname(None)`, with `timezone.utc` taking a fast path;
  - `zoneinfo.ZoneInfo` with a non-`None` `key`.
  - Anything else raises `TypeError`: pytz, dateutil, custom `tzinfo` classes, a named `timezone`, or a `ZoneInfo.from_file` without a key.
- **`ZoneInfo`** appends `[<key>]`, for example `2026-11-01T01:30:00-08:00[America/Los_Angeles]`.
- **`fold == 1`** appends `[_fold=1]` after any zone. It uses an RFC 9557 experimental (underscore) key and is written only when fold is 1.
- **Decode:**
  - remove `[_fold=1]` if present;
  - remove a trailing `[key]` if present;
  - parse the rest with `datetime.fromisoformat`, which returns `timezone.utc` itself for `+00:00`, so the UTC singleton survives;
  - then `replace(tzinfo=ZoneInfo(key))` and/or `replace(fold=1)`.
  - The wall time, zone and fold are authoritative. If the reader's tzdata disagrees with the stored offset, the zone's current rules win. That's the same as pickle, which also stores wall-clock fields plus the key.

### msgpack encoding

Uses the same tag maps and escape rule as JSON, with these differences:
- **Native types:** `bytes` is a `bin` value, non-`str` dict keys are native map keys (keys encoded recursively), and NaN and ±inf are native. Floats are always float64; ints use the smallest int/uint form up to 64 bits.
- **UTC datetimes** (`tzinfo is timezone.utc` and `fold == 0`) use the standard timestamp extension (type -1), choosing timestamp32, timestamp64 or timestamp96 by range. Decoding builds a `datetime` with `tzinfo=timezone.utc`, so msgspec and other msgpack readers see the same instant.
- **Other datetimes** (naive, fixed offset, `ZoneInfo`, or fold) use the `$datetime` tag with the text above.
- **Not used in msgpack:** `$bytes`, `$dict` and `$float`; the decoder rejects them. `$tuple`, `$set`, `$frozenset`, `$int`, `$date`, `$decimal`, `$datetime` and `$esc` are used as in JSON.
- **Decoder errors:** a timestamp extension with sub-microsecond nanoseconds is a decode error, since datetime can't hold it. Unknown extension types are also decode errors.

## Outcomes for the value_cases TBDs

All four become `'roundtrip'` under the default `pickle=False`. I haven't edited `tests/conformance/value_cases.py`; flip them there.

- **`date` → roundtrip.** ISO dates are unambiguous, exact and cheap. `dataclasses.asdict` output, a main source of structured values, often contains them. Rejecting them would push people toward `pickle=True` for no safety gain. The `datetime` check comes before the `date` check, so a `datetime` never takes the `date` path.
- **`Decimal` → roundtrip.** `Decimal(str(d))` reproduces the value exactly, including the exponent and special values, without depending on the decimal context. Caveat for the tests: `Decimal('NaN') != Decimal('NaN')`, so `exact_equal` can't check a NaN Decimal. It does round-trip, and Hypothesis excludes Decimal NaN only for that reason.
- **`set` → roundtrip, and `frozenset` too.** `exact_equal` holds, and elements must be supported hashable types. Iteration order may differ after decode, and the two ways of writing the same set aren't byte-identical, which doesn't matter for a cache. A `frozenset` is also accepted as a dict key or set element (through `$dict`).
- **`ZoneInfo` datetime → roundtrip**, with the `[key]` suffix. `ZoneInfo(key)` returns the cached instance, so `out.tzinfo is original.tzinfo`, and fold is preserved on both sides of a DST transition (tested).
  - Caveats: a `ZoneInfo.no_cache(...)` instance comes back as the cached instance, so it's equal by wall time but `tzinfo is` fails. A keyless `ZoneInfo.from_file` raises `TypeError`.
  - A tzdata change between write and read keeps the wall time, as pickle does.

There's one change to a case that wasn't TBD: **`fold=1` on naive and fixed-offset datetimes round-trips** through `[_fold=1]`. I originally rejected it. Hypothesis generates it, and more importantly `datetime.now()` sets `fold=1` during the repeated hour at DST fall-back, so rejecting it would make `set` fail for one hour a year.

## msgpack: Phase 2 or Phase 6a?

**Ship msgpack in Phase 2.** The page entry decides it, and the result isn't close:
- **Stored size:** tagged JSON stores 614,494 B against 460,872 B for msgpack (+33%, because base64 is applied to the nested gzip body on every set).
- **Round trip:** tagged JSON takes 389 µs (serde_json) or 261 µs (sonic-rs); msgpack takes 10.7-15.6 µs, the same as pickle and msgspec.
- **Against diskcache:** diskcache's whole `get_page_entry_450k` is 29 µs and its `set_page_entry_450k` is 465 µs. With tagged JSON, oxide's decode alone (141 µs) would make `get` about 5x slower than diskcache on Talk Python's largest value. With msgpack, the codec costs about 5 µs on the read path.
- **Smaller values:** msgpack is also faster than our JSON on every other payload: `mcp_result` 0.67 vs 0.86 µs, `asdict_job_status` 0.68 vs 1.67 µs, `nested_10k` 27.5 vs 33.3 µs, plus smaller rows. Its implementation is small (`rmp` is about 2,700 lines with 1 `unsafe`, and our decoder is about 250 lines), and it reuses the tag scheme.

**Open question for Michael: should msgpack be the default `serializer`?** On these numbers it wins everywhere. JSON's remaining advantages are that rows can be read with the sqlite3 CLI or `json_extract`, and that plain JSON rows are readable by other tools. Those matter less for a cache than speed and size. At a minimum, Talk Python's `rendered_cache` should use `serializer="msgpack"`. Making it the default is a roadmap decision, so I haven't assumed it.

## Second latency gate: `get_hit_small_dict` projection

- **3.14 projection:**
  - Spike 2 measured oxide's `get_hit_small` (a 100-char str) at 0.80 µs. That path builds the Python value with `PyUnicode` creation, which costs 0.016 µs.
  - Swapping that for a JSON decode of `mcp_result()` (0.50 µs with the key cache) gives about **0.80 − 0.02 + 0.50 ≈ 1.28 µs**. The row is 232 B rather than 100 B, which adds a negligible amount.
  - Measured diskcache 5.6.3 pickle `get` of `mcp_result()` in this venv: **2.71 µs**. Projected ratio **0.47×**, against the 1.05× tolerance.
- **3.14t projection:** 0.82 − 0.02 + 0.53 ≈ 1.33-1.36 µs, against diskcache's 2.48 µs, for **about 0.55×**.
- **With msgpack**, the decode is 0.39 µs, giving about 1.17 µs on 3.14.
- **Risk:** the projection assumes Phase 2 keeps Spike 2's structure, where the decode runs straight from SQLite's row buffer with no copy. JSON conversion happens after the connection goes back to the pool, per the roadmap rule. If that forces a copy of the row bytes first, the copy costs about 0.02 µs for 232 B. Even a 2x miss on the decode estimate passes.

## Notes for Phase 2

- Promote `json.rs`, `mp.rs` and `common.rs` into the py crate. They are written against the safe PyO3 API and contain no `unsafe`, so they pass the workspace `unsafe_code = "deny"`.
  - Decode errors currently become `ValueError` with serde's message. Phase 2 should map them to a corrupt-row error type.
  - Encode errors keep the original Python exception, which is a `TypeError` for unsupported types.
- Use the size-based output strategy (scratch Vec vs direct `PyBytesWriter`) and the per-thread key cache.
- Encode before taking a connection, as the roadmap requires. The encoder calls a few C-implemented Python methods: `isoformat`, `str()` of `Decimal` and big ints, `tzname`, and `ZoneInfo.key`. It never runs user code, because custom `tzinfo` and subclasses are rejected before any call.
- Phase 4 candidates:
  - borrowed-reference iteration fast paths (about 2x on container encode);
  - Rust-side ISO datetime formatting;
  - a look at 3.14t container iteration cost.

## Files

- `spikes/codec/` holds the spike: `src/{lib,common,json,mp}.rs`, `tests/test_codec.py`, `bench_codec.py`, and the raw results in `results-gil.md` and `results-ft.md`.
- Rebuild: `VIRTUAL_ENV=spikes/codec/.venv spikes/codec/.venv/bin/maturin develop --release -m spikes/codec/Cargo.toml`.
- Test: `spikes/codec/.venv/bin/python -m pytest -c spikes/codec/pyproject.toml --rootdir spikes/codec spikes/codec/tests`, with `HYPOTHESIS_EXAMPLES=30000` for the long run.
- Benchmark: `spikes/codec/.venv/bin/python spikes/codec/bench_codec.py`.
