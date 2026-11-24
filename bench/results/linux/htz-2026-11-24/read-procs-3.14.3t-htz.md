Reads only, 1,000 keys of a 1 KB str unless the row says otherwise, one thread per process (the threaded row: one process), median of 5 rounds of 1.5 s. oxide 0.5.8, Python 3.14.3t, htz (8 CPUs), unknown power.

Calls a second, by process count:

| Variant | 1 | 2 | 3 | 4 | 8 |
|---|---|---|---|---|---|
| oxide Cache | 141k | 228k | 338k | 396k | 438k |
| oxide Cache, directory per process | 143k | 261k | 382k | 505k | 925k |
| oxide FanoutCache(8) | 136k | 250k | 364k | 458k | 776k |
| diskcache Cache | 105k | 248k | 332k | 425k | 501k |
| diskcache FanoutCache(8) | 94k | 239k | 336k | 437k | 747k |
| raw sqlite3, one WAL file | 143k | 265k | 327k | 399k | 429k |
| raw sqlite3, with a sentinel reader | 218k | 407k | 572k | 741k | 1,355k |
| raw sqlite3, WAL with frames | 210k | 357k | 457k | 484k | 414k |
| raw sqlite3, sentinel reader, WAL with frames | 478k | 951k | 1,413k | 1,817k | 3,379k |
| oxide Cache, threads in one process | 137k | 259k | 379k | 453k | 668k |
| oxide Cache, 6 B value (fits the page cache) | 157k | 260k | 367k | 450k | 407k |
| oxide Cache, sqlite_cache_size=8192 | 146k | 266k | 346k | 414k | 408k |
| oxide Cache, maintenance off | 143k | 236k | 326k | 392k | 447k |
| oxide Cache, WAL with frames | 206k | 332k | 451k | 442k | 398k |
| oxide Cache, threads, WAL with frames | 203k | 342k | 489k | 566k | 720k |
| oxide FanoutCache(8), WAL with frames | 213k | 422k | 574k | 736k | 1,172k |

As a share of "oxide Cache, directory per process" at the same count:

| Variant | 1 | 2 | 3 | 4 | 8 |
|---|---|---|---|---|---|
| oxide Cache | 98% | 88% | 88% | 79% | 47% |
| oxide FanoutCache(8) | 95% | 96% | 95% | 91% | 84% |
| diskcache Cache | 73% | 95% | 87% | 84% | 54% |
| diskcache FanoutCache(8) | 66% | 92% | 88% | 87% | 81% |
| raw sqlite3, one WAL file | 100% | 101% | 85% | 79% | 46% |
| raw sqlite3, with a sentinel reader | 153% | 156% | 149% | 147% | 147% |
| raw sqlite3, WAL with frames | 147% | 137% | 120% | 96% | 45% |
| raw sqlite3, sentinel reader, WAL with frames | 334% | 365% | 370% | 360% | 365% |
| oxide Cache, threads in one process | 96% | 99% | 99% | 90% | 72% |
| oxide Cache, 6 B value (fits the page cache) | 110% | 100% | 96% | 89% | 44% |
| oxide Cache, sqlite_cache_size=8192 | 102% | 102% | 91% | 82% | 44% |
| oxide Cache, maintenance off | 100% | 91% | 85% | 78% | 48% |
| oxide Cache, WAL with frames | 144% | 127% | 118% | 88% | 43% |
| oxide Cache, threads, WAL with frames | 142% | 131% | 128% | 112% | 78% |
| oxide FanoutCache(8), WAL with frames | 148% | 162% | 150% | 146% | 127% |

Share of the workers' CPU time spent in the kernel:

| Variant | 1 | 2 | 3 | 4 | 8 |
|---|---|---|---|---|---|
| oxide Cache | 59% | 65% | 66% | 68% | 79% |
| oxide Cache, directory per process | 58% | 61% | 62% | 62% | 62% |
| oxide FanoutCache(8) | 55% | 58% | 60% | 63% | 65% |
| diskcache Cache | 43% | 36% | 40% | 40% | 62% |
| diskcache FanoutCache(8) | 40% | 29% | 31% | 31% | 35% |
| raw sqlite3, one WAL file | 56% | 55% | 63% | 65% | 79% |
| raw sqlite3, with a sentinel reader | 45% | 48% | 50% | 50% | 51% |
| raw sqlite3, WAL with frames | 39% | 46% | 55% | 63% | 80% |
| raw sqlite3, sentinel reader, WAL with frames | 0% | 0% | 0% | 0% | 0% |
| oxide Cache, threads in one process | 59% | 47% | 41% | 37% | 36% |
| oxide Cache, 6 B value (fits the page cache) | 60% | 66% | 68% | 70% | 84% |
| oxide Cache, sqlite_cache_size=8192 | 57% | 60% | 64% | 67% | 79% |
| oxide Cache, maintenance off | 57% | 65% | 67% | 69% | 79% |
| oxide Cache, WAL with frames | 46% | 55% | 57% | 67% | 81% |
| oxide Cache, threads, WAL with frames | 44% | 23% | 15% | 11% | 17% |
| oxide FanoutCache(8), WAL with frames | 39% | 42% | 45% | 48% | 53% |

WAL bytes before and after the run: raw sqlite3, WAL with frames: 4,144,752 and 4,144,752; raw sqlite3, sentinel reader, WAL with frames: 4,144,752 and 4,144,752; oxide Cache, WAL with frames: 4,120,032 and 4,120,032; oxide Cache, threads, WAL with frames: 4,120,032 and 4,120,032; oxide FanoutCache(8), WAL with frames: 12,360,256 and 12,360,256.
