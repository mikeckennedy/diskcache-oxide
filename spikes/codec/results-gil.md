# Python 3.14.10 (GIL), debug build: False

| Payload | Codec | Stored size | Encode µs | Decode µs | Round trip µs | Exact |
|---|---|---|---|---|---|---|
| page_entry_450k | oxide json (serde_json) | 614,494 B | 247.43 | 141.12 | 388.55 | yes |
| page_entry_450k | oxide json (sonic-rs) | 614,494 B | 126.64 | 134.56 | 261.20 | yes |
| page_entry_450k | oxide msgpack (rmp) | 460,872 B | 10.33 | 5.23 | 15.56 | yes |
| page_entry_450k | oxide msgpack, direct write | 460,872 B | 5.46 | 5.26 | 10.71 | yes |
| page_entry_450k | pickle p5 | 460,910 B | 5.57 | 5.44 | 11.01 | yes |
| page_entry_450k | msgspec json | 614,483 B | 368.20 | 103.75 | 471.95 | **no** |
| page_entry_450k | msgspec msgpack | 460,872 B | 5.28 | 5.36 | 10.65 | yes |
| page_entry_450k | orjson | refused: TypeError: Type is not JSON serializable: bytes | | | | |
| mcp_result | oxide json (serde_json) | 232 B | 0.37 | 0.50 | 0.86 | yes |
| mcp_result | oxide json (sonic-rs) | 232 B | 0.35 | 0.42 | 0.77 | yes |
| mcp_result | oxide msgpack (rmp) | 201 B | 0.28 | 0.39 | 0.67 | yes |
| mcp_result | oxide msgpack, direct write | 201 B | 0.35 | 0.40 | 0.75 | yes |
| mcp_result | pickle p5 | 233 B | 0.39 | 0.49 | 0.88 | yes |
| mcp_result | msgspec json | 232 B | 0.25 | 0.33 | 0.59 | yes |
| mcp_result | msgspec msgpack | 201 B | 0.16 | 0.28 | 0.43 | yes |
| mcp_result | orjson | 232 B | 0.16 | 0.31 | 0.47 | yes |
| rate_limit_keys_200 | oxide json (serde_json) | 11,691 B | 4.15 | 4.87 | 9.02 | yes |
| rate_limit_keys_200 | oxide json (sonic-rs) | 11,691 B | 1.52 | 3.98 | 5.50 | yes |
| rate_limit_keys_200 | oxide msgpack (rmp) | 11,493 B | 1.73 | 3.93 | 5.66 | yes |
| rate_limit_keys_200 | oxide msgpack, direct write | 11,493 B | 3.37 | 3.87 | 7.24 | yes |
| rate_limit_keys_200 | pickle p5 | 11,706 B | 3.96 | 3.79 | 7.75 | yes |
| rate_limit_keys_200 | msgspec json | 11,691 B | 2.76 | 4.42 | 7.18 | yes |
| rate_limit_keys_200 | msgspec msgpack | 11,493 B | 1.52 | 3.18 | 4.70 | yes |
| rate_limit_keys_200 | orjson | 11,691 B | 0.67 | 4.29 | 4.96 | yes |
| asdict_job_status | oxide json (serde_json) | 275 B | 1.12 | 0.54 | 1.67 | yes |
| asdict_job_status | oxide json (sonic-rs) | 275 B | 1.09 | 0.45 | 1.54 | yes |
| asdict_job_status | oxide msgpack (rmp) | 163 B | 0.32 | 0.36 | 0.68 | yes |
| asdict_job_status | oxide msgpack, direct write | 163 B | 0.37 | 0.36 | 0.73 | yes |
| asdict_job_status | pickle p5 | 301 B | 1.90 | 1.20 | 3.10 | yes |
| asdict_job_status | msgspec json | 237 B | 0.18 | 0.28 | 0.46 | **no** |
| asdict_job_status | msgspec msgpack | 163 B | 0.13 | 0.25 | 0.39 | yes |
| asdict_job_status | orjson | 247 B | 0.22 | 0.28 | 0.49 | **no** |
| nested_10k | oxide json (serde_json) | 8,606 B | 11.76 | 21.58 | 33.34 | yes |
| nested_10k | oxide json (sonic-rs) | 8,606 B | 10.40 | 19.13 | 29.53 | yes |
| nested_10k | oxide msgpack (rmp) | 6,644 B | 9.66 | 17.80 | 27.46 | yes |
| nested_10k | oxide msgpack, direct write | 6,644 B | 13.35 | 17.78 | 31.13 | yes |
| nested_10k | pickle p5 | 5,266 B | 10.55 | 14.06 | 24.61 | yes |
| nested_10k | msgspec json | 8,606 B | 5.70 | 13.62 | 19.32 | yes |
| nested_10k | msgspec msgpack | 6,644 B | 4.77 | 11.92 | 16.69 | yes |
| nested_10k | orjson | 8,606 B | 4.71 | 12.71 | 17.42 | yes |

- mcp_result json decode, key cache on: 0.505 µs
- mcp_result json decode, key cache off: 0.618 µs
- nested_10k json decode, key cache on: 22.377 µs
- nested_10k json decode, key cache off: 24.771 µs
- diskcache get small: 1.952 µs
- diskcache get dict: 2.708 µs
- str decode (PyUnicode from 100 B): 0.016 µs
