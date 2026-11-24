# Plan 33: why a plain `Cache` reads slower with more processes, and what a sentinel reader buys (2026-11-24)

Mac Studio (Apple M5 Max, 18 CPUs: the host names itself `mac-studio-m5-max`; the 2026-11-18 file from the same benchmark was labelled `macbook-m5-pro`), macOS 27.2.1, AC power, not niced, oxide 0.5.8 built from source (not the PGO wheel), Python 3.14.10 unless a table says 3.14.10t. The Linux numbers are from htz (x86-64, Linux 6.8, 8 vCPUs of an AMD EPYC Rome at 2.0 GHz), in a Docker container with the 0.5.8 wheel from PyPI (`bench/scaling/htz_read_procs.sh`; section 6). Every table is the median of its rounds; 1,000 keys of a 1 KB str; one thread per spawned process.

Scripts: `bench/scaling/fcntl_probe.py`, `bench/read_procs.py`, `bench/scaling/sentinel_writer_readers.py`. Raw results: `fcntl-probe-*.json`, `read-procs-3.14.10*-mac-studio-m5-max.{md,json}`, and `plan-33/` for the prototype runs. The prototype is `read-sentinel-prototype-2026-11-24.diff`; it was never merged.

## 1. The fall is the kernel's, on one file

A shared `fcntl` lock taken and released on one byte of one file, which is what a WAL read does to the `-shm` file, with no SQLite and no oxide (thousands of pairs a second, 5 rounds of 1.5 s):

| Processes | Mac, one file | Mac, file per process | htz, one file | htz, file per process |
|---|---|---|---|---|
| 1 | 1,810k | 1,808k | 526k | 525k |
| 2 | 1,200k | 3,042k | 769k | 1,073k |
| 3 | 529k | 4,070k | 1,024k | 1,630k |
| 4 | 494k | 5,068k | 916k | 2,142k |
| 6 | | | 817k | 3,121k |
| 8 | 301k | 6,585k | 708k | 4,146k |
| 16 | 269k | 9,138k | | |

On macOS one file collapses to a sixth of its one-process rate, at 99% system time. On Linux it doesn't collapse, but it stops at about a million pairs a second from 3 processes on and drifts down, while a file per process climbs in a straight line: 72% of the control at 2 processes, 63% at 3, 17% at 8. A lock pair also costs more on that host to begin with (1.9 µs against 0.55 µs).

The cache table on the Mac says the same thing (reads a second, 5 rounds):

| Variant | 1 | 2 | 3 | 4 | 8 | 16 |
|---|---|---|---|---|---|---|
| oxide `Cache` | 617k | 863k | 804k | 591k | 285k | 236k |
| oxide `Cache`, directory per process | 630k | 1,133k | 1,622k | 2,144k | 3,265k | 4,749k |
| oxide `FanoutCache(8)` | 662k | 1,087k | 1,457k | 1,761k | 1,661k | 1,550k |
| diskcache `Cache` | 428k | 746k | 932k | 671k | 281k | 243k |
| diskcache `FanoutCache(8)` | 392k | 778k | 1,109k | 1,378k | 1,681k | 1,578k |
| raw `sqlite3`, one WAL file | 608k | 883k | 833k | 552k | 287k | 242k |
| oxide `Cache`, 6 B value (fits the page cache) | 737k | 983k | 845k | 525k | 287k | 246k |
| oxide `Cache`, `sqlite_cache_size=8192` | 655k | 916k | 852k | 547k | 284k | 243k |
| oxide `Cache`, maintenance off | 593k | 824k | 769k | 601k | 282k | 239k |
| oxide `Cache`, WAL with frames | 727k | 925k | 784k | 524k | 282k | 246k |

Python's own `sqlite3` module on one WAL file, and diskcache's plain `Cache`, fall to the same 280k to 290k at 8 processes as ours, with 93 to 97% of their CPU time in the kernel. The value size, the page cache, the maintenance thread and the state of the WAL make no difference there. So the limit belongs to any SQLite database in WAL mode read by several processes, and nothing in oxide's own code is in it.

Threads on one handle (3.14.10t) don't fall the same way, because SQLite counts readers per process and skips the lock calls when they overlap: 609k, 790k, 1,102k and 756k at 1, 2, 4 and 8 threads.

## 2. What the system calls cost, and the two smaller ones behind the lock

A second connection in each process that sits inside a read transaction (a "sentinel") keeps the process's count on the read mark above zero, so every other read in that process takes and releases the mark with no system call. With the raw `sqlite3` loop:

| Variant | 1 | 2 | 3 | 4 | 8 | 16 |
|---|---|---|---|---|---|---|
| raw `sqlite3`, one WAL file | 608k | 883k | 833k | 552k | 287k | 242k |
| raw `sqlite3`, with a sentinel reader | 946k | 1,518k | 2,096k | 2,545k | 1,053k | 927k |
| raw `sqlite3`, WAL with frames | 774k | 1,037k | 884k | 540k | 287k | 239k |
| raw `sqlite3`, sentinel reader, WAL with frames | 1,255k | 2,438k | 3,543k | 4,313k | 8,081k | 14,475k |

With the lock calls gone, two more per-read system calls show, each contended across processes on one file like the lock:

- **`fstat` on the database file, when the WAL index was built from an empty WAL.** `sample` put the sentinel row's remaining time in `sqlite3PagerSharedLock` → `unixFileSize`. The WAL index then holds no page count, so SQLite asks the file system for the size at the start of every read, until the first commit by any connection. A cache that every process has closed, reopened only by readers, stays in that state. With no sentinel at all it is worth 18% at one process for oxide (617k against 727k) and 27% for the raw loop (608k against 774k).
- **`pread`, when the database is larger than the page cache.** 1,000 keys of 1 KB are 351 pages against the default 256. It only shows once the lock is gone: the prototype's plain `Cache` stops near 4M reads a second from 8 processes on, and reaches 16.2M at 16 with `sqlite_cache_size=8192`.

With all three gone a read makes no system call (1% kernel time) and scales with the cores. At one process it is 1,255k against 774k: the lock pair alone is about 40% of a read on this machine.

## 3. The prototype in oxide (candidate S)

One sentinel connection per store, taken in `Store::lease`, behind `DCO_PROTO_SENTINEL=1`. Reads only, 3.14.10 (5 rounds):

| Variant | 1 | 2 | 3 | 4 | 8 | 16 |
|---|---|---|---|---|---|---|
| oxide `Cache`, WAL with frames | 667k | 884k | 742k | 509k | 280k | 246k |
| prototype: oxide `Cache`, WAL with frames | 1,139k | 1,939k | 2,803k | 3,285k | 4,071k | 3,580k |
| oxide `FanoutCache(8)`, WAL with frames | 760k | 1,264k | 1,629k | 1,841k | 1,647k | 1,531k |
| prototype: oxide `FanoutCache(8)`, WAL with frames (3 rounds, earlier run) | 1,314k | 2,545k | 3,814k | 4,573k | 8,108k | 14,976k |
| prototype: oxide `Cache`, `sqlite_cache_size=8192`, WAL with frames (3 rounds, counts 1, 4, 8, 16) | 1,447k | | | 5,496k | 10,294k | 16,237k |

Threads on one handle, 3.14.10t, reads only, WAL with frames (7 rounds):

| Variant | 1 | 2 | 4 | 8 |
|---|---|---|---|---|
| oxide `Cache` | 766k | 1,030k | 1,363k | 988k |
| prototype | 1,253k | 1,148k | 1,399k | 980k |

The gain in threads is at one thread only. From two threads on, the readers already overlap and skip the lock calls, and what limits them is SQLite's per-file mutex, which a sentinel doesn't remove. That part is plan 34's.

## 4. What it costs: a held mark and the WAL

A held read mark caps how far any checkpoint can copy, and stops the log being reset. The first prototype gave its mark up only when its process noticed a change (`SQLITE_FCNTL_DATA_VERSION`, read after each operation) or after 50 ms. With 10 sets in every 1,000 calls it let the WAL grow to 114 MiB in 1.5 s at one process, where it is 4 MiB without, and each write cost about 60 µs more: the checkpoint SQLite runs inside a commit ran while the sentinel still held the mark from before that commit, so it never finished, the log was never restarted, and every later commit tried again.

Two rules were then added: the sentinel is down for the whole of its own process's write, and (optionally) it is taken again only after N operations in a row saw no change ("quiet N").

Calls a second with sets mixed in, and the WAL's size in KiB as the point ended, in brackets (5 rounds):

| 1 set in 1,000 calls | 1 | 2 | 4 | 8 |
|---|---|---|---|---|
| oxide `Cache` | 703k (4,031) | 883k (4,840) | 557k (4,079) | 278k (4,079) |
| prototype | 1,067k (4,031) | 1,540k (30,103) | 2,254k (42,161) | 1,399k (27,037) |
| prototype, quiet 64 | 1,064k (4,031) | 1,564k (6,518) | 1,838k (6,518) | 698k (4,490) |
| prototype, quiet 512 | 854k (4,031) | 1,078k (4,373) | 747k (4,445) | 291k (4,128) |

| 10 sets in 1,000 calls | 1 | 2 | 4 | 8 |
|---|---|---|---|---|
| oxide `Cache` | 588k (4,031) | 667k (4,695) | 577k (4,526) | 255k (5,250) |
| prototype | 942k (4,031) | 775k (129,550) | 894k (169,587) | 623k (127,293) |
| prototype, quiet 64 | 680k (4,031) | 716k (4,236) | 561k (4,490) | 257k (4,695) |
| prototype, quiet 512 | 573k (4,031) | 644k (4,490) | 524k (4,526) | 257k (5,274) |

One process, which knows about every write because it made them, keeps the gain at both mixes with the WAL unchanged: 1,067k against 703k, and 942k against 588k. Several processes don't: without the quiet rule the WAL grows, and with it the gain is there only while the database is quiet for longer than the rule asks.

One writer process beside pure readers (`sentinel_writer_readers.py`), which is the shape the-rust-architect's review predicted would fail. No forced checkpoints, 15 s:

| Mode | Readers | Writes/s | Reads/s | WAL peak KiB |
|---|---|---|---|---|
| off | 4 | 1,000 | 596k | 4,184 |
| sentinel | 4 | 1,000 | 2,733k | 242,669 |
| sentinel, quiet 64 | 4 | 1,000 | 1,896k | 65,268 |
| sentinel, quiet 512 | 4 | 1,000 | 598k | 4,120 |
| off | 4 | 10,000 | 474k | 4,264 |
| sentinel | 4 | 10,000 | 1,097k | 2,431,862 |
| sentinel, quiet 64 | 4 | 10,000 | 509k | 4,297 |
| sentinel, quiet 512 | 4 | 10,000 | 498k | 4,216 |

The writer forcing a checkpoint every 250 ms with a 16 ms budget (one forced round's worth), 8 s:

| Mode | Readers | Writes/s | Reads/s | WAL peak KiB | Forced checkpoints completed |
|---|---|---|---|---|---|
| off | 4 | 1,000 | 595k | 4,023 | 32 of 32 |
| sentinel | 4 | 1,000 | 2,591k | 129,530 | 0 of 31 |
| sentinel, quiet 64 | 4 | 1,000 | 1,915k | 4,103 | 11 of 31 |
| sentinel, quiet 512 | 4 | 1,000 | 564k | 4,023 | 32 of 32 |
| off | 4 | 10,000 | 474k | 4,168 | 32 of 32 |
| sentinel | 4 | 10,000 | 1,078k | 1,296,963 | 0 of 32 |
| sentinel, quiet 64 | 4 | 10,000 | 558k | 4,200 | 2 of 32 |
| sentinel, quiet 512 | 4 | 10,000 | 518k | 4,168 | 32 of 32 |

With a 100 ms budget every mode completed every forced checkpoint, because the budget was then longer than the sentinel's 50 ms timer. Where the sentinel gained across processes, the WAL or the forced checkpoint paid for it; where the WAL was healthy, there was no gain. A rule counted in operations can't separate the two: 64 reads is about 60 µs for a busy reader.

## 5. What this says

- The multi-process fall is the operating system's lock path on one file. It is severe on macOS. On Linux the bare lock levels off, and a plain `Cache` doesn't fall at all: it climbs to 8 processes, at 90% and 86% of the directory-per-process control at 2 and 3 (section 6). `FanoutCache` remains the answer for many processes reading one cache.
- The lock pair is also about 40% of a single read in a single process on this Mac, and a sentinel gives that back whenever its process can know the database hasn't changed. For one process that is always, since it makes every write itself.
- Across processes a sentinel is only safe while the database is quiet for longer than a forced checkpoint round, and it needs a thread to drop it in an idle process. Under steady writes from other processes it has to fall back to today's behaviour.
- Threads on one handle gain nothing past one thread.
- The prototype has no fork handling. A child that inherited an armed sentinel would read with SQLite's in-process count but without the kernel's lock, which is plan 32's class of bug, so a real build puts the sentinel inside the pool where the fork hooks see it.

## 6. Linux (htz), and the decision

The table on htz, 3.14.2 and 3.14.3t, in Docker, reads only (calls a second, 5 rounds; the raw files are in `linux/htz-2026-11-24/`). The bare lock in the container (483k pairs a second at 1 process, 1,130k at 4, 741k at 8) matches the probe on the host, so the container costs nothing measurable here.

| 3.14.2 | 1 | 2 | 3 | 4 | 8 |
|---|---|---|---|---|---|
| oxide `Cache` | 134k | 229k | 324k | 411k | 502k |
| oxide `Cache`, directory per process | 138k | 254k | 377k | 495k | 905k |
| share of the control | 97% | 90% | 86% | 83% | 55% |
| oxide `FanoutCache(8)` | 136k | 239k | 351k | 433k | 751k |
| diskcache `Cache` | 71k | 168k | 233k | 295k | 405k |
| raw `sqlite3`, WAL with frames | 149k | 273k | 347k | 420k | 421k |
| raw `sqlite3`, sentinel reader, WAL with frames | 261k | 494k | 739k | 1,010k | 1,839k |
| oxide `Cache`, WAL with frames | 195k | 317k | 418k | 467k | 422k |

| 3.14.3t | 1 | 2 | 3 | 4 | 8 |
|---|---|---|---|---|---|
| oxide `Cache` | 141k | 228k | 338k | 396k | 438k |
| share of the control | 98% | 88% | 88% | 79% | 47% |
| oxide `FanoutCache(8)` | 136k | 250k | 364k | 458k | 776k |
| oxide `Cache`, threads in one process | 137k | 259k | 379k | 453k | 668k |
| oxide `Cache`, WAL with frames | 206k | 332k | 451k | 442k | 398k |

- **The stop rule is met on Linux.** A plain `Cache` reaches 90% and 86% of the control at 2 and 3 processes (88% and 88% on 3.14t). The collapse is macOS's; on Linux the curve only bends past 4 processes, where `FanoutCache(8)` keeps climbing.
- **Everything is slower on this host in absolute terms:** 7.5 µs a `get` against 1.6 µs on the Mac, and raw `sqlite3` is as slow. A lock pair costs about 2 µs here.
- **A sentinel would gain more on Linux:** +75% at one process in the raw `sqlite3` control (149k to 261k).
- **The `fstat` finding is larger on Linux:** a plain oxide `Cache` does 134k reads a second on a database opened with an empty WAL and 195k once the WAL has frames, +45% at one process (+18% on the Mac). It lasts only from the open until the first commit by any connection, so a cache that takes any write is past it; it is the cost of a cache reopened and then only read.

**Decision (Michael, 2026-11-24): no sentinel.** The gain is real but small in absolute terms (about half a microsecond a `get` on the Mac, about 3 µs on htz), and the price is a connection always inside a transaction beside the fork gate, four release rules, an idle thread, a failure mode that shows only as a slowly growing WAL, and a unix-only code path. The prototype stays here as a diff. The `fstat` fix is measured (above) and its decision is Michael's, since it means a write at open and a reworded D-019.

