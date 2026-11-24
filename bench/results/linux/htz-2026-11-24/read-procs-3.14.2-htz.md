Reads only, 1,000 keys of a 1 KB str unless the row says otherwise, one thread per process (the threaded row: one process), median of 5 rounds of 1.5 s. oxide 0.5.8, Python 3.14.2, htz (8 CPUs), unknown power.

Calls a second, by process count:

| Variant | 1 | 2 | 3 | 4 | 8 |
|---|---|---|---|---|---|
| oxide Cache | 134k | 229k | 324k | 411k | 502k |
| oxide Cache, directory per process | 138k | 254k | 377k | 495k | 905k |
| oxide FanoutCache(8) | 136k | 239k | 351k | 433k | 751k |
| diskcache Cache | 71k | 168k | 233k | 295k | 405k |
| diskcache FanoutCache(8) | 65k | 161k | 242k | 305k | 560k |
| raw sqlite3, one WAL file | 110k | 192k | 256k | 330k | 382k |
| raw sqlite3, with a sentinel reader | 154k | 281k | 410k | 524k | 1,012k |
| raw sqlite3, WAL with frames | 149k | 273k | 347k | 420k | 421k |
| raw sqlite3, sentinel reader, WAL with frames | 261k | 494k | 739k | 1,010k | 1,839k |
| oxide Cache, 6 B value (fits the page cache) | 160k | 262k | 378k | 438k | 410k |
| oxide Cache, sqlite_cache_size=8192 | 143k | 240k | 340k | 400k | 430k |
| oxide Cache, maintenance off | 133k | 231k | 325k | 409k | 427k |
| oxide Cache, WAL with frames | 195k | 317k | 418k | 467k | 422k |
| oxide FanoutCache(8), WAL with frames | 210k | 384k | 545k | 683k | 1,119k |

As a share of "oxide Cache, directory per process" at the same count:

| Variant | 1 | 2 | 3 | 4 | 8 |
|---|---|---|---|---|---|
| oxide Cache | 97% | 90% | 86% | 83% | 55% |
| oxide FanoutCache(8) | 98% | 94% | 93% | 88% | 83% |
| diskcache Cache | 52% | 66% | 62% | 60% | 45% |
| diskcache FanoutCache(8) | 47% | 63% | 64% | 62% | 62% |
| raw sqlite3, one WAL file | 80% | 76% | 68% | 67% | 42% |
| raw sqlite3, with a sentinel reader | 111% | 111% | 109% | 106% | 112% |
| raw sqlite3, WAL with frames | 108% | 107% | 92% | 85% | 47% |
| raw sqlite3, sentinel reader, WAL with frames | 189% | 194% | 196% | 204% | 203% |
| oxide Cache, 6 B value (fits the page cache) | 116% | 103% | 100% | 89% | 45% |
| oxide Cache, sqlite_cache_size=8192 | 104% | 94% | 90% | 81% | 47% |
| oxide Cache, maintenance off | 96% | 91% | 86% | 83% | 47% |
| oxide Cache, WAL with frames | 141% | 125% | 111% | 95% | 47% |
| oxide FanoutCache(8), WAL with frames | 153% | 151% | 144% | 138% | 124% |

Share of the workers' CPU time spent in the kernel:

| Variant | 1 | 2 | 3 | 4 | 8 |
|---|---|---|---|---|---|
| oxide Cache | 57% | 65% | 67% | 67% | 77% |
| oxide Cache, directory per process | 58% | 61% | 60% | 61% | 62% |
| oxide FanoutCache(8) | 54% | 60% | 63% | 62% | 64% |
| diskcache Cache | 32% | 25% | 28% | 32% | 48% |
| diskcache FanoutCache(8) | 31% | 21% | 22% | 22% | 27% |
| raw sqlite3, one WAL file | 42% | 45% | 51% | 55% | 68% |
| raw sqlite3, with a sentinel reader | 31% | 34% | 35% | 36% | 38% |
| raw sqlite3, WAL with frames | 29% | 31% | 42% | 48% | 66% |
| raw sqlite3, sentinel reader, WAL with frames | 0% | 0% | 0% | 0% | 0% |
| oxide Cache, 6 B value (fits the page cache) | 63% | 67% | 67% | 72% | 84% |
| oxide Cache, sqlite_cache_size=8192 | 57% | 63% | 64% | 69% | 78% |
| oxide Cache, maintenance off | 58% | 64% | 66% | 66% | 80% |
| oxide Cache, WAL with frames | 44% | 55% | 60% | 64% | 80% |
| oxide FanoutCache(8), WAL with frames | 39% | 42% | 45% | 49% | 53% |

WAL bytes before and after the run: raw sqlite3, WAL with frames: 4,144,752 and 4,144,752; raw sqlite3, sentinel reader, WAL with frames: 4,144,752 and 4,144,752; oxide Cache, WAL with frames: 4,120,032 and 4,120,032; oxide FanoutCache(8), WAL with frames: 12,360,256 and 12,360,256.
