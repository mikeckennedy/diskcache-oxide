Reads only, 1,000 keys of a 1 KB str unless the row says otherwise, one thread per process (the threaded row: one process), median of 7 rounds of 1.5 s. oxide 0.5.8, Python 3.14.10t, prototype-s-threads7 (18 CPUs), ac power.

Calls a second, by process count:

| Variant | 1 | 2 | 4 | 8 |
|---|---|---|---|---|
| oxide Cache, threads, WAL with frames | 766k | 1,030k | 1,363k | 988k |
| prototype S: oxide Cache, threads, WAL with frames | 1,253k | 1,148k | 1,399k | 980k |

Share of the workers' CPU time spent in the kernel:

| Variant | 1 | 2 | 4 | 8 |
|---|---|---|---|---|
| oxide Cache, threads, WAL with frames | 34% | 27% | 21% | 47% |
| prototype S: oxide Cache, threads, WAL with frames | 11% | 14% | 25% | 52% |

WAL bytes before and after the run: oxide Cache, threads, WAL with frames: 4,120,032 and 4,120,032; prototype S: oxide Cache, threads, WAL with frames: 4,120,032 and 4,120,032.
