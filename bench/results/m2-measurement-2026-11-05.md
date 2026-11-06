# Step 5 measured, M2, and 5c (2026-11-05)

Plan 12 step 5, build-order step 4: the before/after table for the whole of step 5 (5a's SQLite mutex spin and 5b's slot-wait spin), the M2 report on FanoutCache's default connection budget, and the 5c decision. `before` is `721de5a`, the last commit before step 5's first change (`7d1fab1`); `after` is `6bfe945`, which ships the fixed 1,024-hint SQLite mutex spin and 5b's slot-wait spin. Both were built as release wheels (`maturin build --release`, as the earlier pages used, not PGO) from clean `git worktree`s, Python 3.14t, GIL off at runtime.

`bench/scaling/m2_probe.py`: each arm in a fresh process, arms interleaved round by round, 7 rounds, median and min-max, thousands of gets/s. 6-byte values, 700 URL-shaped keys, reads only, one shared handle per case. `fanout8` is `FanoutCache(shards=8)` at its default budget (8 connections, one per shard); `c16` and `c32` are 16 and 32 connections; `dirs4` is the control, four plain `Cache`s on four directories, one per thread. The Mac is an M5 Pro (18 cores); the VM is arm64 Docker under OrbStack (`bench/scaling/linux_m2.sh`) at `--cpus 8` and `--cpus 4`.

## Before and after

| Case | Mac before | Mac after | VM 8 CPUs before | VM 8 CPUs after | VM 4 CPUs before | VM 4 CPUs after |
|---|---|---|---|---|---|---|
| cache 1t | 927 (915-932) | 919 (874-932) | 1,052 (1,008-1,101) | 1,073 (934-1,097) | 1,100 (1,042-1,107) | 1,084 (1,046-1,099) |
| **cache 4t** | **816** (737-841) | **1,416** (1,381-1,474) | **1,110** (1,070-1,417) | **1,690** (1,604-1,728) | **1,099** (1,060-1,418) | **1,715** (1,691-1,733) |
| fanout8 1t | 913 (900-922) | 913 (882-919) | 1,043 (1,036-1,088) | 1,067 (983-1,084) | 1,085 (1,036-1,095) | 1,070 (1,039-1,086) |
| **fanout8 4t** | **773** (770-814) | **1,185** (1,170-1,207) | **1,031** (997-1,082) | **1,176** (1,126-1,242) | **1,075** (980-1,092) | **1,185** (1,122-1,249) |
| fanout8 c16 4t | 973 (966-981) | 1,508 (1,489-1,533) | 1,085 (1,054-1,099) | 1,872 (1,787-1,930) | 1,091 (1,063-1,105) | 1,902 (1,799-1,917) |
| fanout8 c32 1t | 920 (907-927) | 908 (881-918) | 1,039 (1,003-1,087) | 1,056 (1,017-1,091) | 1,090 (1,057-1,105) | 1,076 (1,049-1,089) |
| **fanout8 c32 4t** | **1,215** (1,201-1,231) | **1,535** (1,519-1,558) | **1,231** (1,196-1,279) | **1,923** (1,720-1,954) | **1,265** (1,197-1,311) | **1,938** (1,766-1,949) |
| dirs4 4t | 1,953 (1,636-1,980) | 1,865 (1,833-1,988) | 3,298 (3,064-3,354) | 3,324 (3,218-3,364) | 3,329 (3,254-3,348) | 3,327 (3,164-3,346) |

- **Four threads on one handle:** plain `Cache` +74% on the Mac and +52-56% in the VM; FanoutCache at 32 connections +26% and +53-56%; at 16 connections +55% and +73-74%; at the default budget +53% on the Mac but only +10-14% in the VM.
- **Single-thread is unchanged** (within ±2%, every pair of ranges overlapping), and the four-directory control doesn't move, as expected: nothing there contends.
- **A repeat check:** an earlier 7-round run of the same probe without the `c16` row (Mac, and the VM at 8 and 4 CPUs) agrees within 3% on every 4-thread after median (Mac `cache 4t` 1,454, `fanout8 4t` 1,190, `c32 4t` 1,541; VM 8 CPUs 1,703, 1,173, 1,920; VM 4 CPUs 1,707, 1,160, 1,954).
- The `before` `cache 4t` rows in the VM have one fast round each (1,417 and 1,418): the before build is bimodal there, as the read-ceiling page saw. The medians are representative.

## `bench/fanout.py`

`bench/scaling/fanout_ab.py` runs `bench.fanout --rounds 1` once per build per round, the build order rotating, 5 rounds; each cell is the median over rounds and the min-max, thousands of ops/s. The `threads-*` and `procs-*` cases are the 99:1 get:set mix over 1,000 keys with 1 KB values, 1.5 s each; `small-*` is the probe's read case inside the benchmark.

Mac:

| Case | Cache before | Cache after | fanout8 before | fanout8 after | fanout8 c32 before | fanout8 c32 after |
|---|---|---|---|---|---|---|
| threads-uniform-1 | 637 (632-681) | 666 (638-674) | 692 (687-738) | 732 (704-736) | 693 (686-734) | 734 (728-739) |
| threads-uniform-4 | 741 (722-793) | 953 (945-982) | 658 (648-671) | 904 (897-908) | 1,015 (983-1,021) | 1,155 (1,143-1,179) |
| threads-uniform-8 | 600 (558-634) | 764 (756-820) | 688 (685-703) | 1,019 (1,003-1,024) | **938** (929-949) | **824** (820-837) |
| threads-zipf-4 | 765 (751-797) | 1,079 (1,048-1,108) | 655 (651-670) | 870 (846-879) | 1,046 (1,029-1,066) | 1,268 (1,259-1,286) |
| threads-zipf-8 | 647 (628-661) | 937 (873-946) | 684 (678-687) | 957 (950-968) | **1,051** (1,046-1,060) | **994** (986-1,021) |
| procs-uniform-1 | 647 (628-680) | 665 (626-667) | 724 (679-736) | 722 (689-733) | 710 (699-726) | 717 (681-726) |
| procs-uniform-4 | 612 (594-623) | 598 (558-611) | 1,571 (1,546-1,577) | 1,522 (1,506-1,566) | 1,554 (1,542-1,576) | 1,548 (1,522-1,573) |
| procs-uniform-8 | 257 (254-259) | 259 (257-269) | 1,505 (1,432-1,537) | 1,488 (1,466-1,524) | 1,499 (1,451-1,513) | 1,490 (1,451-1,510) |
| procs-zipf-4 | 576 (534-593) | 565 (551-566) | 1,679 (1,637-1,695) | 1,683 (1,633-1,687) | 1,687 (1,641-1,693) | 1,678 (1,660-1,692) |
| procs-zipf-8 | 264 (258-269) | 262 (255-267) | 1,316 (1,302-1,352) | 1,325 (1,314-1,347) | 1,335 (1,308-1,358) | 1,341 (1,302-1,349) |
| small-1t | 883 (868-894) | 904 (879-910) | 892 (878-894) | 898 (890-908) | 886 (855-905) | 906 (890-913) |
| small-4t-shared | 801 (787-851) | 1,462 (1,343-1,485) | 812 (761-812) | 1,193 (1,190-1,207) | 1,217 (1,188-1,237) | 1,549 (1,536-1,570) |
| small-4t-separate | 1,864 (1,846-1,958) | 1,908 (1,853-1,984) | 1,819 (1,793-1,870) | 1,852 (1,828-1,944) | 1,902 (1,823-1,943) | 1,824 (1,807-1,833) |

VM at `--cpus 8`:

| Case | Cache before | Cache after | fanout8 before | fanout8 after | fanout8 c32 before | fanout8 c32 after |
|---|---|---|---|---|---|---|
| threads-uniform-1 | 686 (663-743) | 673 (664-725) | 756 (734-798) | 742 (718-773) | 784 (727-817) | 728 (703-738) |
| threads-uniform-4 | 672 (640-697) | 903 (849-936) | 748 (714-790) | 752 (745-812) | 998 (982-1,058) | 1,232 (1,221-1,319) |
| threads-uniform-8 | 683 (666-691) | 905 (883-928) | 733 (697-765) | 778 (761-817) | 1,152 (1,125-1,195) | 1,620 (1,513-1,736) |
| threads-zipf-4 | 713 (706-725) | 960 (900-1,010) | 787 (732-795) | 808 (760-824) | 1,030 (994-1,038) | 1,317 (1,312-1,331) |
| threads-zipf-8 | 729 (705-738) | 954 (895-1,039) | 745 (700-753) | 808 (777-823) | 1,100 (1,076-1,148) | 1,719 (1,682-1,743) |
| procs-uniform-1 | 730 (682-744) | 691 (679-729) | 774 (763-814) | 776 (767-804) | 768 (753-806) | 781 (728-799) |
| procs-uniform-4 | 955 (864-961) | 992 (834-1,025) | 1,157 (1,056-1,240) | 1,118 (1,003-1,284) | 1,077 (1,012-1,201) | 1,177 (1,118-1,226) |
| procs-uniform-8 | 1,006 (981-1,093) | 1,045 (832-1,053) | 1,151 (1,082-1,310) | 1,239 (1,134-1,260) | 1,121 (1,014-1,213) | 1,173 (1,087-1,269) |
| procs-zipf-4 | 1,027 (872-1,064) | 913 (642-1,064) | 1,160 (1,127-1,340) | 1,169 (1,015-1,336) | 1,112 (1,053-1,291) | 1,123 (1,033-1,264) |
| procs-zipf-8 | 1,151 (1,077-1,205) | 1,117 (776-1,181) | 1,204 (1,022-1,348) | 1,257 (1,179-1,355) | 1,247 (1,152-1,307) | 1,083 (937-1,304) |
| small-1t | 1,016 (995-1,087) | 1,035 (951-1,068) | 1,037 (1,028-1,100) | 1,018 (964-1,078) | 1,088 (1,012-1,096) | 1,004 (986-1,034) |
| small-4t-shared | 1,449 (1,191-1,491) | 1,719 (1,387-1,862) | 1,019 (1,013-1,065) | 1,154 (1,127-1,224) | 1,290 (1,269-1,316) | 1,882 (1,818-1,929) |
| small-4t-separate | 3,272 (3,208-3,402) | 3,330 (3,306-3,362) | 3,369 (3,341-3,395) | 3,361 (3,241-3,383) | 3,380 (3,304-3,414) | 3,336 (3,325-3,383) |

(2-thread rows and the remaining `procs` rows are in the raw output and say the same; summarized here to keep the tables readable.)

- **Multi-process numbers don't move,** on either machine, within noise: each process has its own connections, so neither spin is contended there. FanoutCache's multi-process lead over a plain `Cache` (13i: 5.7x at 8 processes on the Mac) is intact, 1,488 against 259.
- **Threads on one handle gain** on the 99:1 mix too: plain `Cache` +27-45% at 4 and 8 threads on the Mac and +31-35% in the VM; FanoutCache at 32 connections +14-21% at 4 threads on the Mac and +23-28% in the VM, and +41-56% at 8 threads in the VM.
- **One regression, Mac only: FanoutCache at 32 connections with 8 threads on the 99:1 mix,** -12% uniform (938 to 824) and -5% zipf (1,051 to 994), no overlap in the rounds. The same rows gain 41% and 56% in the VM, and 8 threads isn't oversubscribed on 18 cores. Not explained here; the writes (one in 100) take each shard's write lock, so a spinning waiter there may be what the Mac punishes. Open (below).
- **The default budget in the VM:** 8 threads on the mix gain 6-8%, 4 threads 0-3%. One connection per shard is the limit there, not the locks.
- VM `small-1t` at 32 connections reads 8% lower after (1,088 to 1,004, no overlap), but the probe, with more rounds, shows the same build unchanged at one thread (`c32 1t` 1,039 against 1,056); `bench.fanout`'s small case takes the median of three short in-process repetitions and is the noisier of the two.

## M2: FanoutCache's default budget

FanoutCache(8), 4 threads on one shared handle, after step 5, thousands of gets/s (before step 5 in brackets):

| Budget | fds (D-037) | SQLite page cache, up to | Mac | VM 8 CPUs | VM 4 CPUs | htz (x86-64) |
|---|---|---|---|---|---|---|
| plain `Cache` (8 connections) | 2 × 8 + 1 = 17 | 8 MiB | 1,416 [816] | 1,690 [1,110] | 1,715 [1,099] | pending (Michael) |
| FanoutCache default, 8 (one per shard) | 2 × 8 + 2 × 8 = 32 | 8 MiB | 1,185 [773] | 1,176 [1,031] | 1,185 [1,075] | pending (Michael) |
| FanoutCache, 16 (two per shard) | 2 × 16 + 2 × 8 = 48 | 16 MiB | 1,508 [973] | 1,872 [1,085] | 1,902 [1,091] | pending (Michael) |
| FanoutCache, 32 (four per shard) | 2 × 32 + 2 × 8 = 80 | 32 MiB | 1,535 [1,215] | 1,923 [1,231] | 1,938 [1,265] | pending (Michael) |
| default against plain `Cache` | | | -16% [-5%] | -30% [-7%] | -31% [-2%] | |
| default against 32 connections | | | -23% [-36%] | -39% [-16%] | -39% [-15%] | |
| 16 against 32 connections | | | -2% [-20%] | -3% [-12%] | -2% [-14%] | |

fds are D-037's bound inside the root (`2 × max_connections + 2 × shards`); a plain `Cache`'s is `2 × pool size + 1` (the bounded-pool entry in `tests/DIVERGENCES.md`). Page cache is `max_connections × sqlite_cache_size` at the 1 MiB default (D-023), for connections actually opened: the pool opens lazily, so a handle only pays for what its concurrency used. One connection per thread-shard pair is what diskcache does instead (D-037: 264 fds for 64 threads on 8 shards).

**What this means, Michael.** Step 5 made one shared FanoutCache handle faster at every budget, but it made the default budget's cost relative to the alternatives bigger, not smaller: before step 5 the default was within 2-7% of a plain `Cache`, because both were stuck behind the same locks; now the locks are cheaper and the default's one connection per shard is the limit, so with four threads on one handle it reads 16% slower than a plain `Cache` on the Mac and about 30% slower in the Linux VM, and 23-39% slower than 32 connections. Two connections per shard (`max_connections=16`) gets 97-98% of 32 connections' speed everywhere and beats a plain `Cache` by 6-11%, for 16 more fds inside the root (48 against 32) and up to 8 MiB more SQLite page cache per process. Going to 32 buys another 2-3% for 32 more fds and 16 MiB more. The default doesn't hurt multiple processes (each has its own budget, and the `procs` rows don't move with it), and it doesn't hurt a single thread. It costs only when several threads in one process share a handle and collide on a shard, which is exactly the free-threaded case FanoutCache's docs (13j) say it doesn't yet help. My recommendation: make the default two connections per shard (16 at 8 shards; `2 × shards` in general, at least 8), and document that D-037's fd bound at the defaults becomes 48. That's a change to M2, so it's yours to decide; if you'd rather keep D-023's 8 MiB promise as the headline, keep 8 and say in the FanoutCache guide that threaded readers should pass `max_connections=2 * shards`. The htz numbers should come in first either way, since x86-64 may move the default's gap.

## 5c: the pool mutex

The plan's rule: build 5c only if, after 5a and 5b, `PoolShared::lock` (`pool.rs:92`) is above 10% of wait samples **and** re-applying prototype 3 gives at least 10% on Linux.

**Profiles of the after build**, 4 threads on one shared handle (`m2_probe.py --hold`, the after commit built with symbols kept, `CARGO_PROFILE_RELEASE_STRIP=none`, same code and optimization; `bench/scaling/wait_share.py` classifies the stacks). The main thread's `join` is excluded.

Mac, `/usr/bin/sample` 8 s (every thread sampled; a wait is a sample in a kernel wait trap):

| Case | worker samples | blocked | pool mutex share of blocked | other blocked | on-CPU spin, SQLite mutex | on-CPU spin, slot wait |
|---|---|---|---|---|---|---|
| `Cache` 4t | 19,536 | 3,144 | **99.5%** (3,128) | 16 (SQLite shm mutex) | 3,517 | 0 |
| `FanoutCache(8)` default 4t | 18,784 | 1,129 | **0%** | 1,126 (slot wait) | 208 | 3,213 |

Counting the SQLite mutex's on-CPU spin as waiting too, the pool's share for `Cache` is 3,128 of 6,661, 47%.

Linux VM at `--cpus 8`, `perf record -g` (perf 7.0 added to a throwaway copy of the image as `dco-linux-perf`, run as root in a privileged container): on-CPU samples on the `cpu-clock` event, a wait being a sample whose top three frames are a lock or spin path, and off-CPU `sched:sched_switch` events, every one a block:

| Case | cpu-clock wait samples | pool mutex | SQLite shm mutex | other SQLite mutex | slot wait | sched_switch | pool mutex |
|---|---|---|---|---|---|---|---|
| `Cache` 4t | 18,794 of 90,362 | **28.0%** | 63.0% | 8.7% | 0 | 651 | **34.1%** (shm 58.5%) |
| `FanoutCache(8)` default 4t | 11,566 of 70,119 | **0%** | 4.5% | 6.8% | 88.4% | 4,108 | **0%** (slot 93.4%) |

So the first clause holds for a shared plain `Cache` (28-34% on Linux) and not for FanoutCache's default budget, which waits on its slots. Its Linux wait is std's futex mutex (`lock_contended`) from `Store::lease` (the inlined `Pool::checkout`) and `Pool::checkin`.

**Prototype 3 on Linux**, re-applied on a throwaway worktree of `6bfe945` (`try_lock` spins in `PoolShared::lock` before blocking, the count from `DCO_POOL_SPIN`, read once); the after build as a control, 7 rounds, interleaved:

| Case | VM 8 CPUs after | p3 0 | p3 100 | p3 1,000 | VM 4 CPUs after | p3 0 | p3 100 | p3 1,000 |
|---|---|---|---|---|---|---|---|---|
| cache 1t | 1,072 | 1,068 | 1,080 | 1,063 | 1,047 | 1,064 | 1,067 | 1,054 |
| **cache 4t** | 1,674 (1,649-1,746) | 1,721 (1,617-1,735) | **1,765** (1,717-1,894) | 1,766 (1,664-1,841) | 1,728 (1,617-1,762) | 1,707 (1,652-1,747) | **1,769** (1,638-1,812) | 1,705 (1,627-1,781) |
| fanout8 4t | 1,175 | 1,188 | 1,232 | 1,185 | 1,182 | 1,161 | 1,173 | 1,134 |
| fanout8 c32 4t | 1,929 | 1,951 | 1,923 | 1,925 | 1,960 | 1,911 | 1,926 | 1,911 |
| dirs4 4t | 3,309 | 3,343 | 3,301 | 3,314 | 3,314 | 3,340 | 3,305 | 3,312 |

The best arm, 100 tries, gives the shared `Cache` +2.6% at 8 CPUs and +3.6% at 4 against the same build at 0 (+5% and +2% against the after build), with the rounds overlapping; nothing else moves. That's under the 10% bar: std's Linux mutex already spins before it parks, so a `try_lock` spin in front of it adds little, and what's left of the pool's share is the critical section itself (two `Vec` operations and a generation check, contended by four threads each doing a 1 µs read).

**5c not built.** The pool mutex is a third of a shared `Cache`'s waits on Linux, but spinning on it isn't worth 10%; a gain there would have to come from holding it less often (for instance, a thread-affine connection, the read-ceiling page's open question), not from spinning on it.

## htz (x86-64): pending (Michael)

`bench/scaling/htz_m2.sh` runs everything above on a Linux host through Docker (the repo's `dco-linux-dev` image, built from `docker/linux-dev.Dockerfile` if missing): it checks out the two commits as worktrees, builds the wheels, runs the probe at `--cpus 8` and `--cpus 4` (7 rounds), profiles with perf in a privileged throwaway container, and runs the `bench/fanout.py` A/B (5 rounds), writing everything to one directory. On htz, from a clone at `6bfe945` or later, on a quiet hour:

```bash
bench/scaling/htz_m2.sh ~/m2-htz          # about an hour; M2_SKIP_FANOUT=1 for the probe and profile only
M2_PROFILE=1 bench/scaling/htz_m2.sh ~/m2-htz        # also the perf profile (a privileged container; off by default)
```

Send back the directory: `probe-cpus8.txt` and `probe-cpus4.txt` fill the htz column of the M2 table, `perf-*.txt` the 5c shares, and `fanout.txt` the fanout rows. It leaves no worktrees behind and installs nothing on the host.

## Load and noise

- **Mac:** on AC power. The 1-minute load average was 2.1-4.8 across the runs (the 4-thread cases add about 4 while they run); the steady background was an agent viewer at about 180% of a core and a browser renderer at about 100%, as on the earlier pages. Nothing else heavy ran: no `scripts/check.sh`, cargo or pytest during any measurement, and every run was sequential, never two at once.
- **VM:** the container's own load average was 0.8-2.2 before the rounds.
- The Mac profiles ran under `sample`, which slows the process (1,167k gets/s for `cache 4t` against the probe's 1,416k); the shares, not the rates, are what count there.

## Deviations

- **"Default CPU count" in the VM is `--cpus 8`**, the default of `scripts/linux-check.sh` and the "VM 8 CPUs" of the earlier pages, rather than an unconstrained container (the VM has 18), so the rows line up with those pages.
- **A new probe, `bench/scaling/m2_probe.py`, instead of `scale_probe.py`.** `scale_probe.py` has no FanoutCache case and isn't interleaved, and `slot_wait.py` asserts `_sqlite_mutex_status`, which the before build doesn't have. The new probe is `slot_wait.py`'s shape with min-max, the 4-directory control, a `c16` row, per-arm environment overrides (for prototype 3), and a `--hold` mode for profiling. `bench/scaling/fanout_ab.py` interleaves `bench/fanout.py` by build, and `bench/scaling/wait_share.py` classifies `sample` and `perf script` stacks.
- **Rounds:** 7 for the probe (the plan says at least 5) and 5 for `bench/fanout.py`. `bench/fanout.py` ran three variants (oxide `Cache`, `FanoutCache(8)` and `FanoutCache(8)` at 32 connections), not stock diskcache; before and after don't change stock.
- **`bench/fanout.py` also ran in the VM**, beyond "on the Mac at least", at `--cpus 8` only.
- **Profiles used a build with symbols kept** (`CARGO_PROFILE_RELEASE_STRIP=none`); the shipped release profile strips symbols, which leaves `sample` and perf with addresses only.
- **`perf` came from a throwaway image**, `dco-linux-perf` (`dco-linux-dev` plus Ubuntu's `linux-perf`), made with `docker commit` and removed afterwards; `dco-linux-dev` itself is unchanged. `linux_m2.sh profile` installs `linux-perf` itself when it's missing, which is what htz will do.
- **Prototype 3's spin count** isn't in `read-ceiling-prototypes-2026-11-05.diff` (it came from an environment variable), so 0, 100 and 1,000 tries were swept. It was measured on Linux only, since the rule scores Linux; the Mac's +12% from the read-ceiling page was not re-measured.
- **Not run: htz.** It isn't reachable from here; the column is marked pending and `htz_m2.sh` produces it.

## Open

- **M2** (above): the recommendation is two connections per shard as the default; Michael's call, ideally after the htz rows.
- **FanoutCache at 32 connections, 8 threads on the 99:1 mix, Mac:** -12% uniform and -5% zipf after step 5, while the VM gains 41-56% on the same rows. Not oversubscribed (8 threads on 18 cores); worth a profile of that case before the soak, since 5b's oversubscription notes don't cover it.
- **Build-order step 5, the fresh 24-hour soak,** is still required before production.
