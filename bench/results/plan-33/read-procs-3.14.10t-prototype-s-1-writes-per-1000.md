1 sets in every 1,000 calls, 1,000 keys of a 1 KB str unless the row says otherwise, one thread per process (the threaded row: one process), median of 5 rounds of 1.5 s. oxide 0.5.8, Python 3.14.10t, prototype-s (18 CPUs), ac power.

Calls a second, by process count:

| Variant | 1 | 2 | 4 | 8 |
|---|---|---|---|---|
| oxide Cache, threads in one process | 723k | 979k | 1,222k | 777k |
| oxide Cache, threads, WAL with frames | 721k | 960k | 1,220k | 781k |
| prototype S: oxide Cache, threads in one process | 1,212k | 1,069k | 1,232k | 780k |
| prototype S, quiet 64: oxide Cache, threads in one process | 1,109k | 1,059k | 1,205k | 759k |
| prototype S: oxide Cache, threads, WAL with frames | 1,183k | 1,068k | 1,218k | 747k |

Share of the workers' CPU time spent in the kernel:

| Variant | 1 | 2 | 4 | 8 |
|---|---|---|---|---|
| oxide Cache, threads in one process | 34% | 29% | 30% | 49% |
| oxide Cache, threads, WAL with frames | 34% | 29% | 29% | 49% |
| prototype S: oxide Cache, threads in one process | 12% | 19% | 31% | 53% |
| prototype S, quiet 64: oxide Cache, threads in one process | 15% | 20% | 31% | 51% |
| prototype S: oxide Cache, threads, WAL with frames | 12% | 19% | 31% | 52% |

WAL size as each point ended, in KiB (the largest of the rounds; the first shard of a FanoutCache):

| Variant | 1 | 2 | 4 | 8 |
|---|---|---|---|---|
| oxide Cache, threads in one process | 4,031 | 4,067 | 4,079 | 4,116 |
| oxide Cache, threads, WAL with frames | 4,248 | 4,248 | 4,248 | 4,248 |
| prototype S: oxide Cache, threads in one process | 4,031 | 4,043 | 4,067 | 4,128 |
| prototype S, quiet 64: oxide Cache, threads in one process | 4,031 | 4,079 | 4,128 | 4,224 |
| prototype S: oxide Cache, threads, WAL with frames | 4,091 | 4,091 | 4,091 | 4,091 |

WAL bytes before and after the run: oxide Cache, threads in one process: 0 and 0; oxide Cache, threads, WAL with frames: 4,120,032 and 4,350,752; prototype S: oxide Cache, threads in one process: 0 and 0; prototype S, quiet 64: oxide Cache, threads in one process: 0 and 0; prototype S: oxide Cache, threads, WAL with frames: 4,120,032 and 4,190,072.
