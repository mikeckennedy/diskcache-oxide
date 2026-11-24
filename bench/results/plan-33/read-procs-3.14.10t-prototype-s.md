Reads only, 1,000 keys of a 1 KB str unless the row says otherwise, one thread per process (the threaded row: one process), median of 5 rounds of 1.5 s. oxide 0.5.8, Python 3.14.10t, prototype-s (18 CPUs), ac power.

Calls a second, by process count:

| Variant | 1 | 2 | 4 | 8 |
|---|---|---|---|---|
| oxide Cache, threads in one process | 609k | 790k | 1,102k | 756k |
| oxide Cache, threads, WAL with frames | 720k | 927k | 1,135k | 892k |
| prototype S: oxide Cache, threads in one process | 894k | 905k | 1,105k | 797k |
| prototype S, quiet 64: oxide Cache, threads in one process | 901k | 912k | 1,109k | 774k |
| prototype S: oxide Cache, threads, WAL with frames | 1,202k | 1,112k | 1,404k | 957k |

Share of the workers' CPU time spent in the kernel:

| Variant | 1 | 2 | 4 | 8 |
|---|---|---|---|---|
| oxide Cache, threads in one process | 39% | 40% | 39% | 56% |
| oxide Cache, threads, WAL with frames | 34% | 27% | 24% | 46% |
| prototype S: oxide Cache, threads in one process | 25% | 32% | 38% | 56% |
| prototype S, quiet 64: oxide Cache, threads in one process | 25% | 32% | 38% | 57% |
| prototype S: oxide Cache, threads, WAL with frames | 12% | 14% | 25% | 52% |

WAL bytes before and after the run: oxide Cache, threads, WAL with frames: 4,120,032 and 4,120,032; prototype S: oxide Cache, threads, WAL with frames: 4,120,032 and 4,120,032.
