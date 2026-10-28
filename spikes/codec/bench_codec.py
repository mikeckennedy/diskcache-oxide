"""Spike 3 codec round-trip benchmarks. Run with the spike venv's python: `python bench_codec.py [--quick]`.

Times are batched p50s in µs per call (median over batches, loop overhead subtracted), the same
method as bench/baseline.py. "exact" says whether decode(encode(v)) is exact_equal to v.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import gc
import json
import pickle
import platform
import random
import statistics
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

import msgspec

import dco_codec_spike as m

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'tests' / 'conformance'))
from value_cases import exact_equal  # noqa: E402

try:
    import orjson
except ImportError:  # no 3.14t build
    orjson = None

QUICK = '--quick' in sys.argv


# ---- payloads (page_entry and mcp_result copied verbatim from bench/baseline.py) ----


def page_entry(size: int = 450 * 1024) -> dict[str, Any]:
    body = random.Random(42).randbytes(size)
    return {'content_gz': body, 'created': '2026-10-27T12:00:00+00:00', 'etag': 'abc123', 'version': 2}


def mcp_result() -> dict[str, Any]:
    return {
        'tool': 'search_episodes',
        'ok': True,
        'count': 3,
        'results': [{'id': 500 + n, 'title': f'Episode {500 + n}: Python news', 'score': 0.9 - n / 10} for n in range(3)],
    }


def rate_limit_keys() -> list[str]:
    return [f'rl:v1:ip:203.0.{n // 50}.{n % 250}:route:/api/episodes/search:{29_384_756 + n}' for n in range(200)]


@dataclasses.dataclass
class JobStatus:
    job_id: str
    kind: str
    state: str
    created: dt.datetime
    started: dt.datetime | None
    finished: dt.datetime | None
    progress: float
    attempts: int
    message: str
    tags: list[str]


def job_status() -> dict[str, Any]:
    t0 = dt.datetime(2026, 10, 27, 12, 0, 0, 123456, tzinfo=dt.UTC)
    return dataclasses.asdict(
        JobStatus('job-7f3a9c', 'transcode', 'running', t0, t0 + dt.timedelta(seconds=3), None, 0.42, 1, 'encoding audio', ['episode', 'mp3'])
    )


def nested_10k() -> dict[str, Any]:
    rnd = random.Random(7)
    return {
        'page': 1,
        'has_more': True,
        'episodes': [
            {
                'id': 480 + i,
                'title': f'Episode {480 + i}: ' + ' '.join(rnd.choice(['Python', 'Rust', 'async', 'typing', 'web', 'data', 'AI']) for _ in range(5)),
                'published': f'2026-0{1 + i % 9}-1{i % 9}',
                'duration_s': rnd.randint(1800, 5400),
                'rating': round(rnd.random() * 5, 2),
                'explicit': False,
                'guests': [{'name': f'Guest {i}-{g}', 'twitter': None, 'episodes': [rnd.randint(1, 500) for _ in range(3)]} for g in range(2)],
                'tags': ['python', 'podcast', f'topic-{i}'],
            }
            for i in range(28)
        ],
    }


PAYLOADS: dict[str, Callable[[], Any]] = {
    'page_entry_450k': page_entry,
    'mcp_result': mcp_result,
    'rate_limit_keys_200': rate_limit_keys,
    'asdict_job_status': job_status,
    'nested_10k': nested_10k,
}


# ---- codecs: name -> (encode, decode) ----

CODECS: dict[str, tuple[Callable[[Any], bytes], Callable[[bytes], Any]]] = {
    'oxide json (serde_json)': (m.json_encode, m.json_decode),
    'oxide json (sonic-rs)': (m.sonic_encode, m.sonic_decode),
    'oxide msgpack (rmp)': (m.msgpack_encode, m.msgpack_decode),
    'oxide msgpack, direct write': (m.msgpack_encode_direct, m.msgpack_decode),
    'pickle p5': (lambda v: pickle.dumps(v, protocol=5), pickle.loads),
    'msgspec json': (msgspec.json.encode, msgspec.json.decode),
    'msgspec msgpack': (msgspec.msgpack.encode, msgspec.msgpack.decode),
}
if orjson is not None:
    CODECS['orjson'] = (orjson.dumps, orjson.loads)


def batched_us(fn: Callable[[], Any], target_s: float) -> float:
    """Median per-call µs over batches; each batch is sized to take about 2 ms."""
    t = time.perf_counter()
    fn()
    one = max(time.perf_counter() - t, 1e-8)
    batch = max(1, min(20_000, int(0.002 / one)))
    n_batches = max(15, int(target_s / (batch * one))) if not QUICK else 10
    loop = range(batch)

    def empty() -> None:
        pass

    def run(f: Callable[[], Any]) -> list[float]:
        out = []
        for _ in range(n_batches):
            s = time.perf_counter_ns()
            for _ in loop:
                f()
            out.append((time.perf_counter_ns() - s) / batch)
        return out

    gc.disable()
    try:
        overhead = statistics.median(run(empty))
        per = statistics.median(run(fn))
    finally:
        gc.enable()
    return max(per - overhead, 0.0) / 1000


def bench_codecs() -> list[dict[str, Any]]:
    rows = []
    for pname, make in PAYLOADS.items():
        value = make()
        for cname, (enc, dec) in CODECS.items():
            try:
                data = enc(value)
            except Exception as e:  # noqa: BLE001
                rows.append({'payload': pname, 'codec': cname, 'error': f'{type(e).__name__}: {e}'[:60]})
                continue
            try:
                exact = exact_equal(dec(data), value)
            except Exception:  # noqa: BLE001
                exact = False
            target = 0.4
            e_us = batched_us(lambda: enc(value), target)
            d_us = batched_us(lambda: dec(data), target)
            rows.append({'payload': pname, 'codec': cname, 'size': len(data), 'encode_us': e_us, 'decode_us': d_us, 'exact': exact})
    return rows


def bench_key_cache() -> dict[str, float]:
    out = {}
    for pname in ('mcp_result', 'nested_10k'):
        data = m.json_encode(PAYLOADS[pname]())
        for on in (True, False):
            m.set_key_cache(on)
            out[f'{pname} json decode, key cache {"on" if on else "off"}'] = batched_us(lambda: m.json_decode(data), 0.4)
    m.set_key_cache(True)
    return out


def bench_diskcache() -> dict[str, float]:
    import diskcache

    out = {}
    with tempfile.TemporaryDirectory(prefix='dco-codec-') as tmp:
        c = diskcache.Cache(tmp)
        c.set('small', 'x' * 100)
        c.set('dict', mcp_result())
        for k in ('small', 'dict'):
            samples = [batched_us(lambda: c.get(k), 0.5) for _ in range(5)]
            out[f'diskcache get {k}'] = statistics.median(samples)
        c.close()
    small = ('x' * 100).encode()
    out['str decode (PyUnicode from 100 B)'] = batched_us(lambda: small.decode(), 0.3)
    return out


def main() -> None:
    gil = getattr(sys, '_is_gil_enabled', lambda: True)()
    print(f'# Python {platform.python_version()} {"(GIL)" if gil else "(free-threaded)"}, debug build: {m._DEBUG_BUILD}\n')
    rows = bench_codecs()
    print('| Payload | Codec | Stored size | Encode µs | Decode µs | Round trip µs | Exact |')
    print('|---|---|---|---|---|---|---|')
    for r in rows:
        if 'error' in r:
            print(f'| {r["payload"]} | {r["codec"]} | refused: {r["error"]} | | | | |')
            continue
        print(
            f'| {r["payload"]} | {r["codec"]} | {r["size"]:,} B | {r["encode_us"]:.2f} | {r["decode_us"]:.2f} | '
            f'{r["encode_us"] + r["decode_us"]:.2f} | {"yes" if r["exact"] else "**no**"} |'
        )
    print()
    for k, v in {**bench_key_cache(), **bench_diskcache()}.items():
        print(f'- {k}: {v:.3f} µs')



if __name__ == '__main__':
    main()
