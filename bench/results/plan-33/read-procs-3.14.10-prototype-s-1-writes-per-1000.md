1 sets in every 1,000 calls, 1,000 keys of a 1 KB str unless the row says otherwise, one thread per process (the threaded row: one process), median of 5 rounds of 1.5 s. oxide 0.5.8, Python 3.14.10, prototype-s (18 CPUs), ac power.

Calls a second, by process count:

| Variant | 1 | 2 | 4 | 8 |
|---|---|---|---|---|
| oxide Cache | 703k | 883k | 557k | 278k |
| oxide Cache, directory per process | 671k | 1,256k | 2,297k | 3,301k |
| oxide FanoutCache(8) | 644k | 1,080k | 1,531k | 1,603k |
| prototype S: oxide Cache | 1,067k | 1,540k | 2,254k | 1,399k |
| prototype S: oxide FanoutCache(8) | 1,012k | 1,723k | 2,921k | 3,581k |
| prototype S, quiet 64: oxide Cache | 1,064k | 1,564k | 1,838k | 698k |
| prototype S, quiet 512: oxide Cache | 854k | 1,078k | 747k | 291k |
| prototype S, quiet 64: oxide FanoutCache(8) | 984k | 1,644k | 2,621k | 2,052k |
| prototype S, quiet 512: oxide FanoutCache(8) | 945k | 1,526k | 2,278k | 1,770k |

As a share of "oxide Cache, directory per process" at the same count:

| Variant | 1 | 2 | 4 | 8 |
|---|---|---|---|---|
| oxide Cache | 105% | 70% | 24% | 8% |
| oxide FanoutCache(8) | 96% | 86% | 67% | 49% |
| prototype S: oxide Cache | 159% | 123% | 98% | 42% |
| prototype S: oxide FanoutCache(8) | 151% | 137% | 127% | 108% |
| prototype S, quiet 64: oxide Cache | 159% | 125% | 80% | 21% |
| prototype S, quiet 512: oxide Cache | 127% | 86% | 33% | 9% |
| prototype S, quiet 64: oxide FanoutCache(8) | 147% | 131% | 114% | 62% |
| prototype S, quiet 512: oxide FanoutCache(8) | 141% | 122% | 99% | 54% |

Share of the workers' CPU time spent in the kernel:

| Variant | 1 | 2 | 4 | 8 |
|---|---|---|---|---|
| oxide Cache | 34% | 54% | 82% | 93% |
| oxide Cache, directory per process | 34% | 38% | 40% | 48% |
| oxide FanoutCache(8) | 34% | 44% | 52% | 69% |
| prototype S: oxide Cache | 14% | 31% | 46% | 77% |
| prototype S: oxide FanoutCache(8) | 17% | 24% | 30% | 43% |
| prototype S, quiet 64: oxide Cache | 16% | 34% | 56% | 87% |
| prototype S, quiet 512: oxide Cache | 27% | 47% | 79% | 93% |
| prototype S, quiet 64: oxide FanoutCache(8) | 19% | 28% | 38% | 67% |
| prototype S, quiet 512: oxide FanoutCache(8) | 21% | 31% | 42% | 68% |

WAL size as each point ended, in KiB (the largest of the rounds; the first shard of a FanoutCache):

| Variant | 1 | 2 | 4 | 8 |
|---|---|---|---|---|
| oxide Cache | 4,031 | 4,840 | 4,079 | 4,079 |
| oxide Cache, directory per process | 4,031 | 4,031 | 4,031 | 4,031 |
| oxide FanoutCache(8) | 7,531 | 8,075 | 12,299 | 15,884 |
| prototype S: oxide Cache | 4,031 | 30,103 | 42,161 | 27,037 |
| prototype S: oxide FanoutCache(8) | 4,031 | 32,819 | 56,139 | 68,390 |
| prototype S, quiet 64: oxide Cache | 4,031 | 6,518 | 6,518 | 4,490 |
| prototype S, quiet 512: oxide Cache | 4,031 | 4,373 | 4,445 | 4,128 |
| prototype S, quiet 64: oxide FanoutCache(8) | 4,031 | 30,501 | 28,944 | 17,815 |
| prototype S, quiet 512: oxide FanoutCache(8) | 4,031 | 8,063 | 12,444 | 16,234 |

WAL bytes before and after the run: oxide Cache: 0 and 0; oxide Cache, directory per process: 0 and 0; oxide FanoutCache(8): 0 and 0; prototype S: oxide Cache: 0 and 0; prototype S: oxide FanoutCache(8): 0 and 0; prototype S, quiet 64: oxide Cache: 0 and 0; prototype S, quiet 512: oxide Cache: 0 and 0; prototype S, quiet 64: oxide FanoutCache(8): 0 and 0; prototype S, quiet 512: oxide FanoutCache(8): 0 and 0.
