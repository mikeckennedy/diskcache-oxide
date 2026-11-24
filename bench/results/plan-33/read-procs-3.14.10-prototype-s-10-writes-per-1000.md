10 sets in every 1,000 calls, 1,000 keys of a 1 KB str unless the row says otherwise, one thread per process (the threaded row: one process), median of 5 rounds of 1.5 s. oxide 0.5.8, Python 3.14.10, prototype-s (18 CPUs), ac power.

Calls a second, by process count:

| Variant | 1 | 2 | 4 | 8 |
|---|---|---|---|---|
| oxide Cache | 588k | 667k | 577k | 255k |
| oxide Cache, directory per process | 657k | 1,145k | 2,111k | 3,014k |
| oxide FanoutCache(8) | 607k | 932k | 1,415k | 1,391k |
| prototype S: oxide Cache | 942k | 775k | 894k | 623k |
| prototype S: oxide FanoutCache(8) | 872k | 960k | 1,351k | 1,449k |
| prototype S, quiet 64: oxide Cache | 680k | 716k | 561k | 257k |
| prototype S, quiet 512: oxide Cache | 573k | 644k | 524k | 257k |
| prototype S, quiet 64: oxide FanoutCache(8) | 722k | 1,095k | 1,529k | 1,339k |
| prototype S, quiet 512: oxide FanoutCache(8) | 706k | 1,047k | 1,403k | 1,244k |

As a share of "oxide Cache, directory per process" at the same count:

| Variant | 1 | 2 | 4 | 8 |
|---|---|---|---|---|
| oxide Cache | 89% | 58% | 27% | 8% |
| oxide FanoutCache(8) | 92% | 81% | 67% | 46% |
| prototype S: oxide Cache | 143% | 68% | 42% | 21% |
| prototype S: oxide FanoutCache(8) | 133% | 84% | 64% | 48% |
| prototype S, quiet 64: oxide Cache | 103% | 63% | 27% | 9% |
| prototype S, quiet 512: oxide Cache | 87% | 56% | 25% | 9% |
| prototype S, quiet 64: oxide FanoutCache(8) | 110% | 96% | 72% | 44% |
| prototype S, quiet 512: oxide FanoutCache(8) | 107% | 91% | 66% | 41% |

Share of the workers' CPU time spent in the kernel:

| Variant | 1 | 2 | 4 | 8 |
|---|---|---|---|---|
| oxide Cache | 37% | 58% | 79% | 94% |
| oxide Cache, directory per process | 36% | 40% | 43% | 52% |
| oxide FanoutCache(8) | 35% | 45% | 55% | 70% |
| prototype S: oxide Cache | 20% | 51% | 65% | 78% |
| prototype S: oxide FanoutCache(8) | 20% | 39% | 49% | 60% |
| prototype S, quiet 64: oxide Cache | 32% | 57% | 77% | 91% |
| prototype S, quiet 512: oxide Cache | 37% | 58% | 77% | 92% |
| prototype S, quiet 64: oxide FanoutCache(8) | 27% | 41% | 54% | 68% |
| prototype S, quiet 512: oxide FanoutCache(8) | 29% | 43% | 55% | 68% |

WAL size as each point ended, in KiB (the largest of the rounds; the first shard of a FanoutCache):

| Variant | 1 | 2 | 4 | 8 |
|---|---|---|---|---|
| oxide Cache | 4,031 | 4,695 | 4,526 | 5,250 |
| oxide Cache, directory per process | 4,031 | 4,031 | 4,031 | 4,031 |
| oxide FanoutCache(8) | 16,126 | 24,285 | 32,469 | 32,759 |
| prototype S: oxide Cache | 4,031 | 129,550 | 169,587 | 127,293 |
| prototype S: oxide FanoutCache(8) | 16,126 | 177,868 | 275,806 | 279,379 |
| prototype S, quiet 64: oxide Cache | 4,031 | 4,236 | 4,490 | 4,695 |
| prototype S, quiet 512: oxide Cache | 4,031 | 4,490 | 4,526 | 5,274 |
| prototype S, quiet 64: oxide FanoutCache(8) | 16,126 | 24,333 | 35,824 | 32,783 |
| prototype S, quiet 512: oxide FanoutCache(8) | 16,210 | 24,309 | 32,505 | 32,771 |

WAL bytes before and after the run: oxide Cache: 0 and 0; oxide Cache, directory per process: 0 and 0; oxide FanoutCache(8): 0 and 0; prototype S: oxide Cache: 0 and 0; prototype S: oxide FanoutCache(8): 0 and 0; prototype S, quiet 64: oxide Cache: 0 and 0; prototype S, quiet 512: oxide Cache: 0 and 0; prototype S, quiet 64: oxide FanoutCache(8): 0 and 0; prototype S, quiet 512: oxide FanoutCache(8): 0 and 0.
