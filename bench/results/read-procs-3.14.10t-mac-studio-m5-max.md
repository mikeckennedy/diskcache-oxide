Reads only, 1,000 keys of a 1 KB str unless the row says otherwise, one thread per process (the threaded row: one process), median of 5 rounds of 1.5 s. oxide 0.5.8, Python 3.14.10t, mac-studio-m5-max (18 CPUs), ac power.

Calls a second, by process count:

| Variant | 1 | 2 | 3 | 4 | 8 | 16 |
|---|---|---|---|---|---|---|
| oxide Cache | 540k | 721k | 555k | 435k | 282k | 252k |
| oxide Cache, directory per process | 535k | 954k | 1,330k | 1,787k | 2,903k | 4,217k |
| oxide FanoutCache(8) | 601k | 922k | 1,068k | 1,242k | 1,502k | 1,469k |
| diskcache Cache | 396k | 611k | 642k | 428k | 280k | 245k |
| diskcache FanoutCache(8) | 351k | 726k | 1,046k | 1,114k | 1,522k | 1,554k |
| raw sqlite3, one WAL file | 569k | 754k | 678k | 543k | 281k | 253k |
| raw sqlite3, with a sentinel reader | 907k | 1,327k | 1,566k | 1,577k | 1,062k | 1,051k |
| raw sqlite3, WAL with frames | 739k | 713k | 552k | 377k | 272k | 242k |
| raw sqlite3, sentinel reader, WAL with frames | 1,410k | 2,553k | 3,777k | 5,028k | 8,538k | 11,443k |
| oxide Cache, threads in one process | 519k | 708k | 684k | 680k | 710k | 625k |
| oxide Cache, 6 B value (fits the page cache) | 647k | 814k | 744k | 408k | 281k | 250k |
| oxide Cache, sqlite_cache_size=8192 | 580k | 826k | 748k | 475k | 277k | 250k |
| oxide Cache, maintenance off | 535k | 760k | 693k | 443k | 286k | 251k |
| oxide Cache, WAL with frames | 622k | 734k | 677k | 467k | 270k | 241k |
| oxide Cache, threads, WAL with frames | 671k | 874k | 1,112k | 978k | 765k | 682k |
| oxide FanoutCache(8), WAL with frames | 738k | 1,256k | 1,682k | 1,579k | 1,649k | 1,573k |

As a share of "oxide Cache, directory per process" at the same count:

| Variant | 1 | 2 | 3 | 4 | 8 | 16 |
|---|---|---|---|---|---|---|
| oxide Cache | 101% | 76% | 42% | 24% | 10% | 6% |
| oxide FanoutCache(8) | 112% | 97% | 80% | 69% | 52% | 35% |
| diskcache Cache | 74% | 64% | 48% | 24% | 10% | 6% |
| diskcache FanoutCache(8) | 66% | 76% | 79% | 62% | 52% | 37% |
| raw sqlite3, one WAL file | 106% | 79% | 51% | 30% | 10% | 6% |
| raw sqlite3, with a sentinel reader | 169% | 139% | 118% | 88% | 37% | 25% |
| raw sqlite3, WAL with frames | 138% | 75% | 41% | 21% | 9% | 6% |
| raw sqlite3, sentinel reader, WAL with frames | 263% | 268% | 284% | 281% | 294% | 271% |
| oxide Cache, threads in one process | 97% | 74% | 51% | 38% | 24% | 15% |
| oxide Cache, 6 B value (fits the page cache) | 121% | 85% | 56% | 23% | 10% | 6% |
| oxide Cache, sqlite_cache_size=8192 | 108% | 87% | 56% | 27% | 10% | 6% |
| oxide Cache, maintenance off | 100% | 80% | 52% | 25% | 10% | 6% |
| oxide Cache, WAL with frames | 116% | 77% | 51% | 26% | 9% | 6% |
| oxide Cache, threads, WAL with frames | 125% | 92% | 84% | 55% | 26% | 16% |
| oxide FanoutCache(8), WAL with frames | 138% | 132% | 126% | 88% | 57% | 37% |

Share of the workers' CPU time spent in the kernel:

| Variant | 1 | 2 | 3 | 4 | 8 | 16 |
|---|---|---|---|---|---|---|
| oxide Cache | 39% | 58% | 71% | 83% | 93% | 96% |
| oxide Cache, directory per process | 40% | 44% | 45% | 46% | 52% | 56% |
| oxide FanoutCache(8) | 35% | 45% | 49% | 56% | 69% | 77% |
| diskcache Cache | 24% | 34% | 50% | 71% | 89% | 93% |
| diskcache FanoutCache(8) | 24% | 25% | 28% | 32% | 50% | 66% |
| raw sqlite3, one WAL file | 34% | 52% | 68% | 81% | 92% | 94% |
| raw sqlite3, with a sentinel reader | 17% | 33% | 41% | 50% | 81% | 85% |
| raw sqlite3, WAL with frames | 29% | 51% | 72% | 84% | 93% | 96% |
| raw sqlite3, sentinel reader, WAL with frames | 1% | 1% | 1% | 1% | 1% | 1% |
| oxide Cache, threads in one process | 41% | 39% | 36% | 38% | 54% | 45% |
| oxide Cache, 6 B value (fits the page cache) | 39% | 58% | 72% | 86% | 94% | 96% |
| oxide Cache, sqlite_cache_size=8192 | 34% | 53% | 67% | 83% | 93% | 95% |
| oxide Cache, maintenance off | 40% | 57% | 70% | 82% | 91% | 94% |
| oxide Cache, WAL with frames | 34% | 55% | 72% | 84% | 94% | 95% |
| oxide Cache, threads, WAL with frames | 34% | 28% | 21% | 27% | 48% | 43% |
| oxide FanoutCache(8), WAL with frames | 29% | 40% | 45% | 49% | 71% | 81% |

WAL bytes before and after the run: raw sqlite3, WAL with frames: 4,144,752 and 4,144,752; raw sqlite3, sentinel reader, WAL with frames: 4,144,752 and 4,144,752; oxide Cache, WAL with frames: 4,120,032 and 4,120,032; oxide Cache, threads, WAL with frames: 4,120,032 and 4,120,032; oxide FanoutCache(8), WAL with frames: 12,360,256 and 12,360,256.
