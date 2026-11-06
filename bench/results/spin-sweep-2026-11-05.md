# SQLite mutex spin sweep (2026-11-05)

Plan 12 step 5, build-order step 2: picks `SPIN_HINTS` in `crates/diskcache-oxide-core/src/sqlite_mutex.rs`. Release wheels built with `RUSTFLAGS="--cfg dco_spin_sweep"` (the only build that reads `DCO_SPIN_HINTS` and `DCO_SPIN_ADAPTIVE`), Python 3.14t, 6-byte values, 700 URL-shaped keys, reads only, one shared handle per case. `bench/scaling/spin_sweep.py`: each arm in a fresh process, arms interleaved, median of 5 rounds, thousands of gets/s. `adaptive` is glibc's `PTHREAD_MUTEX_ADAPTIVE_NP` for FAST mutexes with no custom spin (Linux only). FanoutCache's default budget (one connection per shard) is held back by the 50 µs slot poll, which is 5b, so its 4-thread row doesn't move here.

## Mac (M5 Pro, 18 cores), oversubscribed at 36 threads

Load average 3.4-4.1 before the run (desktop apps only; no cargo, pytest or benchmark).

| Case | 0 | 256 | 1024 | 4096 |
|---|---|---|---|---|
| cache 1t | 897 | 905 | 865 | 910 |
| cache 4t | 797 | 1,345 | 1,393 | 1,408 |
| fanout8 1t | 893 | 893 | 880 | 889 |
| fanout8 4t | 803 | 805 | 806 | 807 |
| fanout8 c32 4t | 1,210 | 1,526 | 1,508 | 1,515 |
| cache 36t | 530 | 638 | 681 | 675 |
| fanout8 36t | 676 | 659 | 656 | 674 |

## Linux VM (arm64 Docker, OrbStack, `--cpus 8`), 32 threads

| Case | 0 | 256 | 1024 | 4096 | adaptive |
|---|---|---|---|---|---|
| cache 1t | 1,042 | 1,049 | 1,042 | 1,047 | 1,063 |
| cache 4t | 1,126 | 1,394 | 1,440 | 1,453 | 1,213 |
| fanout8 1t | 1,034 | 1,037 | 1,025 | 1,024 | 1,038 |
| fanout8 4t | 1,024 | 1,020 | 1,007 | 1,019 | 1,030 |
| fanout8 c32 4t | 1,271 | 1,908 | 1,916 | 1,928 | 1,368 |
| cache 32t | 1,654 | 1,667 | 1,649 | 1,580 | 1,658 |
| fanout8 32t | 1,002 | 990 | 995 | 989 | 992 |

## Linux VM at `--cpus 2` (a CFS quota), 4 and 8 threads

Rust's `available_parallelism()` reports 2 in this container (it reads `cpu.max`, `200000 100000`); Python's `os.process_cpu_count()` reports 18.

| Case | 0 | 256 | 1024 | 4096 |
|---|---|---|---|---|
| cache 1t | 1,070 | 987 | 668 | 1,034 |
| cache 4t | 1,114 | 934 | 973 | 942 |
| fanout8 1t | 1,041 | 1,024 | 994 | 1,008 |
| fanout8 4t | 1,033 | 1,045 | 1,029 | 1,005 |
| fanout8 c32 4t | 1,074 | 1,177 | 1,103 | 1,122 |
| cache 8t | 852 | 808 | 819 | 770 |
| fanout8 8t | 1,038 | 1,000 | 1,003 | 1,009 |

The spin arms' single-thread numbers are noisy here (563-1,062 across rounds against 1,042-1,089 at 0). That run comes right after the 4-thread warm-up, so the likely cause is quota the spinners used up, throttling the next period.

## Reading

- **The gain is real and flat across budgets.** Shared `Cache` at 4 threads gets +69-77% on the Mac and +24-29% in the VM; FanoutCache at 32 connections gets +25% and +50%. 256, 1,024 and 4,096 are within a few percent of each other.
- **Single-thread is unchanged** on the Mac and in the 8-CPU VM (within ±3%).
- **glibc's adaptive mutex is not a substitute:** +8% on `Cache` 4t against +28% for the spin.
- **4,096 costs when oversubscribed in the VM** (32 threads on 8 CPUs: -4.5%); 1,024 doesn't (-0.3%). On the Mac at 36 threads every budget beats 0.
- **Under a 2-CPU quota every budget loses:** shared `Cache` -13% to -16% at 4 threads and -4% to -10% at 8. A single-CPU rule wouldn't cover it (Rust sees 2), so the shipped rule is no spin at 2 or fewer CPUs (`MIN_SPIN_CPUS`, `budget_for`): such a host blocks exactly as SQLite's own mutexes do. See the re-run below.
- **Chosen: 1,024**, on 3 or more CPUs. It is within 3% of the best arm on every case that gains, and it avoids 4,096's oversubscribed loss and 256's 3-4% shortfall in the VM.
- **Pending:** the x86-64 run on the htz host (Michael's). A `pause` costs differently from arm64's hint, so 1,024 isn't proven there.

## Re-run with the 2-CPU rule

`default` sets no override, so it runs the shipped budget logic (the sweep build differs from a shipped one only in reading the overrides) and reports the budget it chose.

Linux VM at `--cpus 2`, median of 5 rounds; `default` chose a budget of 0:

| Case | 0 | default | 1024 |
|---|---|---|---|
| cache 1t | 1,066 | 1,050 | 670 |
| cache 4t | 1,140 | 1,180 | 1,056 |
| fanout8 1t | 1,036 | 1,040 | 646 |
| fanout8 4t | 1,044 | 1,011 | 1,045 |
| fanout8 c32 4t | 1,096 | 1,112 | 1,066 |
| cache 8t | 931 | 956 | 704 |
| fanout8 8t | 1,016 | 1,026 | 1,010 |

Linux VM at `--cpus 8`, median of 3 rounds; `default` chose 1,024:

| Case | 0 | default |
|---|---|---|
| cache 1t | 1,044 | 1,045 |
| cache 4t | 1,317 | 1,605 |
| fanout8 1t | 1,004 | 1,028 |
| fanout8 4t | 1,036 | 1,020 |
| fanout8 c32 4t | 1,250 | 1,876 |
| cache 32t | 1,885 | 2,057 |
| fanout8 32t | 999 | 980 |

At 2 CPUs the shipped build matches no spin within noise, and at 8 it keeps the gain.

## Placing the threshold: 3 and 4 CPUs

Linux VM at `--cpus 3` and `--cpus 4`, median of 5 rounds (`default` chose 1,024 at both; the Mac's own background load was about two of its 18 cores):

| Case | 3 CPUs, 0 | 3 CPUs, 1024 | 4 CPUs, 0 | 4 CPUs, 1024 |
|---|---|---|---|---|
| cache 1t | 1,052 | 1,018 | 1,063 | 1,041 |
| cache 4t | 1,411 | 1,440 | 1,399 | 1,709 |
| fanout8 c32 4t | 1,260 | 1,615 | 1,253 | 1,918 |
| cache 8t | 1,319 | 1,130 | 1,761 | 1,334 |
| fanout8 8t | 976 | 960 | 1,025 | 1,034 |

The same oversubscription ratio on a bigger quota, 8 CPUs with 16 threads (median of 5): `cache 16t` 1,845 at 0 against 1,685 at 1,024, but the rounds overlap (1,169-1,974 at 0, 1,306-1,952 at 1,024). Earlier at 8 CPUs with 32 threads the spin was neutral (1,654 against 1,649) or ahead (1,885 against 2,057).

- **4 threads (no more threads than CPUs, or barely more):** spinning wins at 3 CPUs (+2% on shared `Cache`, +28% on FanoutCache at 32 connections) and clearly at 4 (+22%, +53%).
- **8 threads on 3 or 4 CPUs loses:** shared `Cache` -14% and -24%, and every spin round was below every no-spin round. That is a property of oversubscription on a small quota, not of the CPU count: raising `MIN_SPIN_CPUS` to 4 wouldn't remove it (4 CPUs loses too), and it would give up the 4-thread gains there.
- **Decision: `MIN_SPIN_CPUS` stays 3.** The data supports spinning from 3 CPUs when the threads fit the CPUs. A process that runs about twice as many busy reader threads as its CPU quota loses 14-24% on shared `Cache`. A fix would need the spin to back off by itself when it keeps failing (an adaptive budget), not a CPU cutoff. Open for Michael.

## How the CPU count is read

- `std::thread::available_parallelism()` honors a cgroup quota on Linux and rounds a fractional one down: at `--cpus 2.5` (`cpu.max` `250000 100000`) it reads 2, so the spin is off.
- `PYTHON_CPU_COUNT` is ignored: it's Python's setting, and this is read in Rust.
- On macOS it's the online CPU count.
