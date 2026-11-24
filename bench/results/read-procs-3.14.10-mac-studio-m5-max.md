Reads only, 1,000 keys of a 1 KB str unless the row says otherwise, one thread per process (the threaded row: one process), median of 5 rounds of 1.5 s. oxide 0.5.8, Python 3.14.10, mac-studio-m5-max (18 CPUs), ac power.

Calls a second, by process count:

| Variant | 1 | 2 | 3 | 4 | 8 | 16 |
|---|---|---|---|---|---|---|
| oxide Cache | 617k | 863k | 804k | 591k | 285k | 236k |
| oxide Cache, directory per process | 630k | 1,133k | 1,622k | 2,144k | 3,265k | 4,749k |
| oxide FanoutCache(8) | 662k | 1,087k | 1,457k | 1,761k | 1,661k | 1,550k |
| diskcache Cache | 428k | 746k | 932k | 671k | 281k | 243k |
| diskcache FanoutCache(8) | 392k | 778k | 1,109k | 1,378k | 1,681k | 1,578k |
| raw sqlite3, one WAL file | 608k | 883k | 833k | 552k | 287k | 242k |
| raw sqlite3, with a sentinel reader | 946k | 1,518k | 2,096k | 2,545k | 1,053k | 927k |
| raw sqlite3, WAL with frames | 774k | 1,037k | 884k | 540k | 287k | 239k |
| raw sqlite3, sentinel reader, WAL with frames | 1,255k | 2,438k | 3,543k | 4,313k | 8,081k | 14,475k |
| oxide Cache, 6 B value (fits the page cache) | 737k | 983k | 845k | 525k | 287k | 246k |
| oxide Cache, sqlite_cache_size=8192 | 655k | 916k | 852k | 547k | 284k | 243k |
| oxide Cache, maintenance off | 593k | 824k | 769k | 601k | 282k | 239k |
| oxide Cache, WAL with frames | 727k | 925k | 784k | 524k | 282k | 246k |
| oxide FanoutCache(8), WAL with frames | 763k | 1,304k | 1,706k | 1,990k | 1,658k | 1,530k |

As a share of "oxide Cache, directory per process" at the same count:

| Variant | 1 | 2 | 3 | 4 | 8 | 16 |
|---|---|---|---|---|---|---|
| oxide Cache | 98% | 76% | 50% | 28% | 9% | 5% |
| oxide FanoutCache(8) | 105% | 96% | 90% | 82% | 51% | 33% |
| diskcache Cache | 68% | 66% | 57% | 31% | 9% | 5% |
| diskcache FanoutCache(8) | 62% | 69% | 68% | 64% | 51% | 33% |
| raw sqlite3, one WAL file | 96% | 78% | 51% | 26% | 9% | 5% |
| raw sqlite3, with a sentinel reader | 150% | 134% | 129% | 119% | 32% | 20% |
| raw sqlite3, WAL with frames | 123% | 92% | 55% | 25% | 9% | 5% |
| raw sqlite3, sentinel reader, WAL with frames | 199% | 215% | 218% | 201% | 248% | 305% |
| oxide Cache, 6 B value (fits the page cache) | 117% | 87% | 52% | 25% | 9% | 5% |
| oxide Cache, sqlite_cache_size=8192 | 104% | 81% | 53% | 26% | 9% | 5% |
| oxide Cache, maintenance off | 94% | 73% | 47% | 28% | 9% | 5% |
| oxide Cache, WAL with frames | 115% | 82% | 48% | 24% | 9% | 5% |
| oxide FanoutCache(8), WAL with frames | 121% | 115% | 105% | 93% | 51% | 32% |

Share of the workers' CPU time spent in the kernel:

| Variant | 1 | 2 | 3 | 4 | 8 | 16 |
|---|---|---|---|---|---|---|
| oxide Cache | 39% | 57% | 69% | 83% | 95% | 97% |
| oxide Cache, directory per process | 39% | 43% | 45% | 45% | 53% | 57% |
| oxide FanoutCache(8) | 35% | 46% | 50% | 52% | 73% | 84% |
| diskcache Cache | 22% | 30% | 41% | 66% | 91% | 95% |
| diskcache FanoutCache(8) | 22% | 23% | 26% | 29% | 53% | 70% |
| raw sqlite3, one WAL file | 33% | 50% | 64% | 81% | 94% | 96% |
| raw sqlite3, with a sentinel reader | 16% | 30% | 33% | 39% | 85% | 91% |
| raw sqlite3, WAL with frames | 27% | 46% | 65% | 82% | 94% | 97% |
| raw sqlite3, sentinel reader, WAL with frames | 0% | 1% | 1% | 1% | 1% | 1% |
| oxide Cache, 6 B value (fits the page cache) | 38% | 57% | 71% | 86% | 95% | 96% |
| oxide Cache, sqlite_cache_size=8192 | 35% | 54% | 66% | 82% | 94% | 96% |
| oxide Cache, maintenance off | 39% | 57% | 70% | 82% | 94% | 97% |
| oxide Cache, WAL with frames | 33% | 54% | 72% | 84% | 93% | 96% |
| oxide FanoutCache(8), WAL with frames | 29% | 40% | 44% | 49% | 71% | 83% |

WAL bytes before and after the run: raw sqlite3, WAL with frames: 4,144,752 and 4,144,752; raw sqlite3, sentinel reader, WAL with frames: 4,144,752 and 4,144,752; oxide Cache, WAL with frames: 4,120,032 and 4,120,032; oxide FanoutCache(8), WAL with frames: 12,360,256 and 12,360,256.
