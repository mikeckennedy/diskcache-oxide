10 sets in every 1,000 calls, 1,000 keys of a 1 KB str unless the row says otherwise, one thread per process (the threaded row: one process), median of 5 rounds of 1.5 s. oxide 0.5.8, Python 3.14.10t, prototype-s (18 CPUs), ac power.

Calls a second, by process count:

| Variant | 1 | 2 | 4 | 8 |
|---|---|---|---|---|
| oxide Cache, threads in one process | 643k | 714k | 931k | 635k |
| oxide Cache, threads, WAL with frames | 651k | 708k | 918k | 641k |
| prototype S: oxide Cache, threads in one process | 950k | 779k | 864k | 661k |
| prototype S, quiet 64: oxide Cache, threads in one process | 726k | 714k | 893k | 643k |
| prototype S: oxide Cache, threads, WAL with frames | 960k | 759k | 850k | 601k |

Share of the workers' CPU time spent in the kernel:

| Variant | 1 | 2 | 4 | 8 |
|---|---|---|---|---|
| oxide Cache, threads in one process | 36% | 41% | 43% | 60% |
| oxide Cache, threads, WAL with frames | 36% | 41% | 44% | 60% |
| prototype S: oxide Cache, threads in one process | 20% | 35% | 44% | 59% |
| prototype S, quiet 64: oxide Cache, threads in one process | 31% | 39% | 44% | 60% |
| prototype S: oxide Cache, threads, WAL with frames | 20% | 36% | 45% | 59% |

WAL size as each point ended, in KiB (the largest of the rounds; the first shard of a FanoutCache):

| Variant | 1 | 2 | 4 | 8 |
|---|---|---|---|---|
| oxide Cache, threads in one process | 4,031 | 4,116 | 4,284 | 4,200 |
| oxide Cache, threads, WAL with frames | 4,224 | 4,224 | 4,381 | 4,381 |
| prototype S: oxide Cache, threads in one process | 4,031 | 4,152 | 4,309 | 4,695 |
| prototype S, quiet 64: oxide Cache, threads in one process | 4,031 | 4,116 | 4,369 | 4,272 |
| prototype S: oxide Cache, threads, WAL with frames | 4,381 | 4,381 | 4,381 | 4,381 |

WAL bytes before and after the run: oxide Cache, threads in one process: 0 and 0; oxide Cache, threads, WAL with frames: 4,120,032 and 4,486,712; prototype S: oxide Cache, threads in one process: 0 and 0; prototype S, quiet 64: oxide Cache, threads in one process: 0 and 0; prototype S: oxide Cache, threads, WAL with frames: 4,120,032 and 4,486,712.
