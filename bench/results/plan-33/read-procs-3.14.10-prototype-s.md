Reads only, 1,000 keys of a 1 KB str unless the row says otherwise, one thread per process (the threaded row: one process), median of 5 rounds of 1.5 s. oxide 0.5.8, Python 3.14.10, prototype-s (18 CPUs), ac power.

Calls a second, by process count:

| Variant | 1 | 2 | 3 | 4 | 8 | 16 |
|---|---|---|---|---|---|---|
| oxide Cache | 555k | 785k | 705k | 540k | 273k | 246k |
| oxide Cache, directory per process | 562k | 1,034k | 1,432k | 1,856k | 2,847k | 4,425k |
| oxide FanoutCache(8) | 591k | 924k | 1,263k | 1,548k | 1,631k | 1,538k |
| raw sqlite3, sentinel reader, WAL with frames | 1,241k | 2,370k | 3,645k | 4,567k | 7,807k | 13,736k |
| oxide Cache, WAL with frames | 667k | 884k | 742k | 509k | 280k | 246k |
| oxide FanoutCache(8), WAL with frames | 760k | 1,264k | 1,629k | 1,841k | 1,647k | 1,531k |
| prototype S: oxide Cache | 849k | 1,274k | 1,620k | 1,630k | 780k | 728k |
| prototype S: oxide Cache, sqlite_cache_size=8192 | 1,036k | 1,614k | 2,243k | 2,500k | 1,038k | 997k |
| prototype S: oxide FanoutCache(8) | 961k | 1,620k | 2,282k | 2,863k | 4,350k | 5,568k |
| prototype S, quiet 64: oxide Cache | 894k | 1,292k | 1,615k | 1,686k | 784k | 735k |
| prototype S, quiet 512: oxide Cache | 870k | 1,290k | 1,581k | 1,392k | 780k | 735k |
| prototype S, quiet 64: oxide FanoutCache(8) | 921k | 1,606k | 2,268k | 3,043k | 4,371k | 5,554k |
| prototype S, quiet 512: oxide FanoutCache(8) | 1,009k | 1,613k | 2,289k | 3,041k | 4,292k | 5,625k |
| prototype S: oxide Cache, WAL with frames | 1,139k | 1,939k | 2,803k | 3,285k | 4,071k | 3,580k |
| prototype S: oxide Cache, sqlite_cache_size=8192, WAL with frames | 1,324k | 2,556k | 3,689k | 4,952k | 9,223k | 15,776k |
| prototype S: oxide FanoutCache(8), WAL with frames | 1,260k | 2,620k | 4,016k | 5,073k | 9,139k | 15,020k |

As a share of "oxide Cache, directory per process" at the same count:

| Variant | 1 | 2 | 3 | 4 | 8 | 16 |
|---|---|---|---|---|---|---|
| oxide Cache | 99% | 76% | 49% | 29% | 10% | 6% |
| oxide FanoutCache(8) | 105% | 89% | 88% | 83% | 57% | 35% |
| raw sqlite3, sentinel reader, WAL with frames | 221% | 229% | 255% | 246% | 274% | 310% |
| oxide Cache, WAL with frames | 119% | 85% | 52% | 27% | 10% | 6% |
| oxide FanoutCache(8), WAL with frames | 135% | 122% | 114% | 99% | 58% | 35% |
| prototype S: oxide Cache | 151% | 123% | 113% | 88% | 27% | 16% |
| prototype S: oxide Cache, sqlite_cache_size=8192 | 184% | 156% | 157% | 135% | 36% | 23% |
| prototype S: oxide FanoutCache(8) | 171% | 157% | 159% | 154% | 153% | 126% |
| prototype S, quiet 64: oxide Cache | 159% | 125% | 113% | 91% | 28% | 17% |
| prototype S, quiet 512: oxide Cache | 155% | 125% | 110% | 75% | 27% | 17% |
| prototype S, quiet 64: oxide FanoutCache(8) | 164% | 155% | 158% | 164% | 154% | 126% |
| prototype S, quiet 512: oxide FanoutCache(8) | 180% | 156% | 160% | 164% | 151% | 127% |
| prototype S: oxide Cache, WAL with frames | 203% | 187% | 196% | 177% | 143% | 81% |
| prototype S: oxide Cache, sqlite_cache_size=8192, WAL with frames | 236% | 247% | 258% | 267% | 324% | 357% |
| prototype S: oxide FanoutCache(8), WAL with frames | 224% | 253% | 280% | 273% | 321% | 339% |

Share of the workers' CPU time spent in the kernel:

| Variant | 1 | 2 | 3 | 4 | 8 | 16 |
|---|---|---|---|---|---|---|
| oxide Cache | 39% | 57% | 69% | 81% | 94% | 97% |
| oxide Cache, directory per process | 39% | 43% | 44% | 46% | 51% | 55% |
| oxide FanoutCache(8) | 35% | 44% | 49% | 52% | 71% | 80% |
| raw sqlite3, sentinel reader, WAL with frames | 1% | 1% | 0% | 1% | 1% | 1% |
| oxide Cache, WAL with frames | 34% | 54% | 72% | 83% | 94% | 96% |
| oxide FanoutCache(8), WAL with frames | 29% | 39% | 44% | 48% | 68% | 79% |
| prototype S: oxide Cache | 25% | 43% | 50% | 60% | 87% | 93% |
| prototype S: oxide Cache, sqlite_cache_size=8192 | 17% | 33% | 37% | 45% | 85% | 91% |
| prototype S: oxide FanoutCache(8) | 18% | 26% | 28% | 30% | 41% | 54% |
| prototype S, quiet 64: oxide Cache | 24% | 43% | 50% | 59% | 86% | 92% |
| prototype S, quiet 512: oxide Cache | 25% | 43% | 50% | 60% | 87% | 93% |
| prototype S, quiet 64: oxide FanoutCache(8) | 17% | 26% | 28% | 30% | 42% | 50% |
| prototype S, quiet 512: oxide FanoutCache(8) | 17% | 26% | 28% | 30% | 40% | 55% |
| prototype S: oxide Cache, WAL with frames | 13% | 22% | 26% | 29% | 51% | 71% |
| prototype S: oxide Cache, sqlite_cache_size=8192, WAL with frames | 1% | 1% | 1% | 1% | 1% | 1% |
| prototype S: oxide FanoutCache(8), WAL with frames | 1% | 1% | 0% | 0% | 1% | 1% |

WAL bytes before and after the run: raw sqlite3, sentinel reader, WAL with frames: 4,144,752 and 4,144,752; oxide Cache, WAL with frames: 4,120,032 and 4,120,032; oxide FanoutCache(8), WAL with frames: 12,360,256 and 12,360,256; prototype S: oxide Cache, WAL with frames: 4,120,032 and 4,120,032; prototype S: oxide Cache, sqlite_cache_size=8192, WAL with frames: 4,120,032 and 4,120,032; prototype S: oxide FanoutCache(8), WAL with frames: 12,360,256 and 12,360,256.
