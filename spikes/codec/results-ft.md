# Python 3.14.10 (free-threaded), debug build: False

| Payload | Codec | Stored size | Encode µs | Decode µs | Round trip µs | Exact |
|---|---|---|---|---|---|---|
| page_entry_450k | oxide json (serde_json) | 614,494 B | 270.72 | 144.30 | 415.01 | yes |
| page_entry_450k | oxide json (sonic-rs) | 614,494 B | 133.61 | 136.89 | 270.50 | yes |
| page_entry_450k | oxide msgpack (rmp) | 460,872 B | 12.34 | 5.45 | 17.79 | yes |
| page_entry_450k | oxide msgpack, direct write | 460,872 B | 5.49 | 5.43 | 10.92 | yes |
| page_entry_450k | pickle p5 | 460,910 B | 5.60 | 5.52 | 11.11 | yes |
| page_entry_450k | msgspec json | 614,483 B | 367.08 | 104.22 | 471.29 | **no** |
| page_entry_450k | msgspec msgpack | 460,872 B | 5.39 | 5.55 | 10.95 | yes |
| mcp_result | oxide json (serde_json) | 232 B | 0.58 | 0.56 | 1.14 | yes |
| mcp_result | oxide json (sonic-rs) | 232 B | 0.55 | 0.48 | 1.03 | yes |
| mcp_result | oxide msgpack (rmp) | 201 B | 0.49 | 0.46 | 0.95 | yes |
| mcp_result | oxide msgpack, direct write | 201 B | 0.54 | 0.43 | 0.97 | yes |
| mcp_result | pickle p5 | 233 B | 0.36 | 0.48 | 0.84 | yes |
| mcp_result | msgspec json | 232 B | 0.28 | 0.43 | 0.71 | yes |
| mcp_result | msgspec msgpack | 201 B | 0.19 | 0.39 | 0.59 | yes |
| rate_limit_keys_200 | oxide json (serde_json) | 11,691 B | 5.30 | 4.96 | 10.26 | yes |
| rate_limit_keys_200 | oxide json (sonic-rs) | 11,691 B | 2.30 | 3.88 | 6.17 | yes |
| rate_limit_keys_200 | oxide msgpack (rmp) | 11,493 B | 2.56 | 3.92 | 6.48 | yes |
| rate_limit_keys_200 | oxide msgpack, direct write | 11,493 B | 4.04 | 3.86 | 7.90 | yes |
| rate_limit_keys_200 | pickle p5 | 11,706 B | 4.00 | 3.50 | 7.50 | yes |
| rate_limit_keys_200 | msgspec json | 11,691 B | 2.83 | 4.09 | 6.92 | yes |
| rate_limit_keys_200 | msgspec msgpack | 11,493 B | 1.55 | 3.09 | 4.64 | yes |
| asdict_job_status | oxide json (serde_json) | 275 B | 1.35 | 0.58 | 1.93 | yes |
| asdict_job_status | oxide json (sonic-rs) | 275 B | 1.31 | 0.49 | 1.80 | yes |
| asdict_job_status | oxide msgpack (rmp) | 163 B | 0.45 | 0.42 | 0.88 | yes |
| asdict_job_status | oxide msgpack, direct write | 163 B | 0.50 | 0.42 | 0.91 | yes |
| asdict_job_status | pickle p5 | 301 B | 2.02 | 1.24 | 3.25 | yes |
| asdict_job_status | msgspec json | 237 B | 0.20 | 0.37 | 0.57 | **no** |
| asdict_job_status | msgspec msgpack | 163 B | 0.17 | 0.35 | 0.52 | yes |
| nested_10k | oxide json (serde_json) | 8,606 B | 20.01 | 22.99 | 42.99 | yes |
| nested_10k | oxide json (sonic-rs) | 8,606 B | 17.20 | 20.37 | 37.57 | yes |
| nested_10k | oxide msgpack (rmp) | 6,644 B | 16.85 | 19.70 | 36.55 | yes |
| nested_10k | oxide msgpack, direct write | 6,644 B | 19.93 | 19.69 | 39.62 | yes |
| nested_10k | pickle p5 | 5,266 B | 10.66 | 14.92 | 25.58 | yes |
| nested_10k | msgspec json | 8,606 B | 6.74 | 17.77 | 24.51 | yes |
| nested_10k | msgspec msgpack | 6,644 B | 5.94 | 16.60 | 22.54 | yes |

- mcp_result json decode, key cache on: 0.530 µs
- mcp_result json decode, key cache off: 0.619 µs
- nested_10k json decode, key cache on: 23.203 µs
- nested_10k json decode, key cache off: 24.956 µs
- diskcache get small: 1.692 µs
- diskcache get dict: 2.484 µs
- str decode (PyUnicode from 100 B): 0.016 µs
