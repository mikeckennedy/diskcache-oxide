# Adaptive SQLite mutex spin, 5a-2 (2026-11-05)

Plan 12 step 5, build-order sub-item 3a: the Design's "5a-2, an adaptive spin" (a per-mutex spinner cap, a decaying budget, and a `decay+probe` arm), built as specified and swept on top of 5b. **Outcome: no arm clears the bar, so the fixed 1,024-hint spin stays.** The code isn't in the tree: the full change (the policy, its pure functions, the two per-mutex atomics, the `pthread_atfork` reset, 10 new unit tests, the sweep knobs, and a sweep-only slot-wait counter) is kept as `adaptive-spin-2026-11-05.diff` next to this page, and it applies cleanly to `ca6eb43`.

Release wheels built with `RUSTFLAGS="--cfg dco_spin_sweep"` and that diff applied, Python 3.14t, 6-byte values, 700 URL-shaped keys, reads only, one shared handle per case. `bench/scaling/spin_sweep.py` (extended for this step): each arm in a fresh process, arms interleaved round by round, medians, thousands of gets/s. `cache` is a shared `Cache`; `c32` is `FanoutCache(8)` at 32 connections; `fanout8` is `FanoutCache(8)` at its default budget. `A>Bt` is the phase change: A threads, then B threads on the same handle in the same process, only B scored. The VM is arm64 Docker under OrbStack (`bench/scaling/linux_adaptive_spin.sh`, one wheel for every quota). The Mac is an M5 Pro, 18 cores, load average 3.5-5 before each run (desktop apps; no cargo or pytest).

## The arms

All at the 1,024-hint budget. The cap is per mutex; `half` is `cpus / 2`, at least 1 (1 at 2 and 3 CPUs, 2 at 4, 4 at 8, 9 on the Mac).

| Arm | Cap | Budget |
|---|---|---|
| `0` | none | no spin |
| `fixed` | none | 1,024 (shipped) |
| `cap1`, `cap2`, `caphalf` | 1, 2, half | 1,024 |
| `decay`, `decay64` | none | decaying, floor 32 or 64 |
| `probe`, `probe:FLOOR=64` | none | decaying plus a full-budget spin every 64th contended enter |
| `both`, `bothhalf` | 1, half | decaying, floor 32 |
| `bothhalf:FLOOR=64`, `:FLOOR=128` | half | decaying, floor 64 or 128 |

The decay is as specified: a contended enter spins `clamp(2 × estimate, floor, 1024)`, a success moves the estimate an eighth of the way toward the hints it took, a failure halves it, and the estimate is held to `floor..=1024`. So the smallest budget is twice the floor (see Deviations).

## The bar, per arm

The clause each column checks, from the Design: single-thread within ±3% of `0` at 4 and 8 CPUs and on the Mac; at 4 threads, at least 90% of fixed's gain where fixed gains 10% or more (shared `Cache` here; every arm keeps `c32 4t`'s gain, at 1,856-1,933 against fixed's 1,857-1,929 at 4 CPUs); 8 threads at 3 and 4 CPUs no worse than −3% against `0`; nothing below fixed at 8 CPUs × 32 threads; the phase change's second phase within noise of fixed; and the Mac's 36-thread row, which the 5b review asked to be scored here.

Medians from the largest pass that ran the arm: 15 rounds at 3 and 4 CPUs (25 for `fixed`, `0` and `bothhalf:FLOOR=64/128` at 4 CPUs), 9 at 8 CPUs, 5 on the Mac. `cap1`, `decay`, `probe` and `both` ran only in the first 5-round pass at 3 and 8 CPUs.

| Arm | 1t vs 0 (4 / 8 / Mac) | cache 4t: share of fixed's gain (3 / 4 / 8 / Mac) | 8t vs 0, 3 CPUs (cache / c32) | 8t vs 0, 4 CPUs (cache / c32) | 32t vs fixed, 8 CPUs (cache / c32) | phase 8>4t vs fixed, 4 CPUs (cache / c32) | Mac 36t vs fixed (cache / c32) |
|---|---|---|---|---|---|---|---|
| fixed | -0% / -2% / -2% | 100% / 100% / 100% / 100% | **-6% / -23%** | -3% / **-11%** | 0 / 0 | 0 / 0 | 0 / 0 |
| cap1 | +1% / +0% / -3% | **41% / 43% / 54%** (4 / 8 / Mac) | -1% / -4% | +5% / +13% | +6% / -1% | -9% / +2% | +4% / -5% |
| cap2 | -0% / -1% / +1% | 104% / 97% / **68%** / 98% | -2% / **-22%** | -1% / **-9%** | +3% / +3% | -1% / -0% | -2% / +6% |
| caphalf | -1% / -2% / -0% | 110% / 100% / 98% / 95% | +1% / -4% | -3% / **-10%** | -6% / +4% | +1% / -9% | -5% / -1% |
| decay | +3% / +0% / -1% | 119% / **54% / 49% / 68%** | +5% / -3% | -1% / +18% | +7% / +2% | -5% / -10% | -7% / **-20%** |
| decay64 | -1% / -2% / -1% | 128% / 95% / **85%** / 95% | -1% / **-12%** | -1% / -1% | -7% / +6% | +6% / -3% | -3% / **-17%** |
| probe | +2% / -0% / +1% | 105% / **49% / 88% / 75%** | +3% / -5% | +7% / +20% | +8% / +0% | -3% / -12% | -11% / -20% |
| probe:FLOOR=64 | -1% / -0% / n/a | 120% / 97% / **83%** / n/a | -3% / **-13%** | -2% / -4% | +3% / +3% | +4% / -3% | n/a |
| both | -0% / -1% / -1% | **84% / 38% / 41% / 52%** | +5% / +2% | +2% / +18% | +3% / -2% | -2% / +5% | -11% / -19% |
| bothhalf | -1% / -1% / n/a | **79% / 53% / 46%** / n/a | +4% / +6% | +3% / +16% | +4% / -0% | -1% / +4% | n/a |
| **bothhalf:FLOOR=64** | -1% / -1% / -1% | 107% / **84%** / 90% / 100% | +3% / -2% | +4% / +2% | +2% / +3% | **-7%** / +5% | -8% / **-16%** |
| bothhalf:FLOOR=128 | -0% / n/a / -0% | n/a / **84%** / n/a / 107% | n/a | +2% / -7% | n/a | -7% / +5% | -6% / -12% |

Fixed's gains over `0` (the denominators): shared `Cache` at 4 threads +13% at 3 CPUs, +31% at 4, +25% at 8, +75% on the Mac; `c32 4t` +13%, +48%, +47%, +26%. Fixed's oversubscription loss: shared `Cache` at 8 threads −6% at 3 CPUs and −3% at 4; `c32 8t` −23% and −11%.

The rounds' spread is wide, so a single cell a few points past its line is weak evidence on its own (at 4 CPUs over 25 rounds, `cache 4t` ran 1,439-1,766 for fixed and 1,389-1,671 for `bothhalf:FLOOR=64`). Two cells aren't noise: `c32 8t` at 3 CPUs, where fixed's 15 rounds (971-1,200) all sit below the 15 of `0` (1,275-1,417), and `c32 36t` on the Mac, where all 5 `bothhalf:FLOOR=64` rounds (1,020-1,042) sit below all 5 of fixed's (1,218-1,268).

## Reading

- **What the cap does.** It cures the oversubscription loss only when it's small for the quota: `caphalf` is cap 1 at 3 CPUs (`c32 8t` −4%, against fixed's −23%) but cap 2 at 4 CPUs, where it is no better than fixed (−10%). A cap of 1, in any arm, costs shared `Cache` at 4 threads more than half its gain (38-54% kept): with one spinner the other waiters park, and each unlock pays a futex wake, as the Risks section predicted.
- **What the decay does.** Plain decay fixes the 4-CPU oversubscribed case (`c32 8t` +18%) but costs shared `Cache` at 4 threads half its gain at 4 and 8 CPUs, and 20% on the Mac's `c32 36t` against fixed. The shared-memory mutex is held across an `fcntl` call, so some spins fail even when the threads fit the CPUs, and halving on each failure drives the budget down to the floor. A floor of 64 (a minimum budget of 128) keeps most of the 4-thread gain but no longer cures `c32 8t` at 3 CPUs (−12%).
- **The probe** recovers the 4-thread gain at floor 64 (97% at 4 CPUs) but not the oversubscribed case (−13% at 3 CPUs), and at floor 32 it loses on the Mac like plain decay.
- **The closest arm, `bothhalf:FLOOR=64`** (cap `cpus / 2` plus decay at floor 64), clears the oversubscription clauses everywhere: within −2% of `0` at 3 and 4 CPUs with 8 threads, at or above fixed at 32 threads on 8 CPUs, and single-thread within ±1%. It misses on three counts. It keeps 84% of fixed's `cache 4t` gain at 4 CPUs (25 rounds; floor 128 gives the same 84%), against a 90% bar. Its phase-change second phase is −7% against fixed on shared `Cache` (1,607 against 1,725; the rounds overlap, 1,337-1,679 against 1,517-1,765). And on the Mac it is −16% on `c32 36t` with the rounds apart, and −8% on `cache 36t`.
- **So fixed stays.** The trade on offer is the oversubscribed small-quota case (on `c32 8t`, +28% against fixed at 3 CPUs and +15% at 4) against the 4-thread case at 4 CPUs (−4% absolute on shared `Cache`) and the Mac's heavy-oversubscription rows (−8% and −16%). The bar says no. Raising the floor further moves the arm toward fixed, which brings back fixed's loss.
- **Fixed's oversubscription loss is smaller than 5a's sweep recorded** (on top of 5b, same VM): shared `Cache` at 8 threads −3% to −9% at 4 CPUs over three passes (5a recorded −24%) and −6% to −7% at 3 CPUs (−14% there). The loss now shows up mostly on FanoutCache at 32 connections: −8% to −11% at 4 CPUs and −23% to −26% at 3. This remains open for Michael: a process running twice as many busy reader threads as its CPU quota, on a small quota, loses up to a quarter of its shared-handle read throughput to the spin.

## The 2-CPU rule

At `--cpus 2` with the CPU rule off (`DCO_SPIN_NO_CPU_RULE=1`), `bothhalf:FLOOR=64` (cap 1 there) against `0`, 15 rounds: `cache 1t` 1,042 against 1,041, `cache 4t` 1,153 against 1,132, `c32 4t` 999 against 1,050 (−5%), `c32 8t` 900 against 951 (−5%), `c32 8>4t` 883 against 1,006 (−12%). The first pass's other arms with the rule off were worse (`fixed` −26% on `cache 4t`, −21% on `c32 8t`). `default` (the shipped logic, budget 0) matched `0` within noise. No arm matches no spin at 2 CPUs, so `MIN_SPIN_CPUS` stays.

## The slot wait is reached

The 8-thread `FanoutCache(8)` default-budget runs do reach 5b's slot wait (a sweep-only counter in `genlock::Slots::wait_for_room`, in the diff): about 95,000 detached waits per 480,000-get run at 3 and 4 CPUs, and 1,100 per run at 2 CPUs (5b's spin is off there, so each wait sleeps 50 µs and there are fewer of them); on the Mac about 67,000 at 4 threads, 195,000 at 8 and 1.4M at 36. The count is the same in every arm (within ±4%), so the SQLite mutex arms don't change how often a FanoutCache thread waits for a slot, and `fanout8` throughput moves within noise across arms (VM 4 CPUs at 8 threads: 1,171-1,231).

## Full medians

VM at 4 CPUs, second pass, 15 rounds:

| Case | 0 | fixed | cap2 | caphalf | decay64 | both | bothhalf | bothhalf:F64 | probe:F64 |
|---|---|---|---|---|---|---|---|---|---|
| cache 1t | 1,043 | 1,036 | 1,039 | 1,031 | 1,035 | 1,042 | 1,037 | 1,032 | 1,031 |
| c32 1t | 1,029 | 1,026 | 1,028 | 1,022 | 1,021 | 1,025 | 1,028 | 1,026 | 1,020 |
| cache 4t | 1,146 | 1,486 | 1,475 | 1,485 | 1,468 | 1,274 | 1,327 | 1,446 | 1,475 |
| c32 4t | 1,299 | 1,929 | 1,919 | 1,933 | 1,916 | 1,912 | 1,911 | 1,927 | 1,925 |
| cache 8t | 1,631 | 1,489 | 1,617 | 1,578 | 1,622 | 1,658 | 1,676 | 1,663 | 1,592 |
| c32 8t | 1,510 | 1,350 | 1,370 | 1,355 | 1,501 | 1,776 | 1,747 | 1,504 | 1,443 |
| fanout8 8t | 1,215 | 1,171 | 1,209 | 1,189 | 1,214 | 1,231 | 1,220 | 1,220 | 1,221 |
| cache 8>4t | 1,424 | 1,612 | 1,604 | 1,625 | 1,702 | 1,583 | 1,592 | 1,613 | 1,670 |
| c32 8>4t | 1,333 | 1,799 | 1,792 | 1,640 | 1,752 | 1,886 | 1,869 | 1,896 | 1,742 |

VM at 4 CPUs, third pass, 25 rounds:

| Case | 0 | fixed | bothhalf:F64 | bothhalf:F128 |
|---|---|---|---|---|
| cache 1t | 1,037 | 1,035 | 1,026 | 1,037 |
| cache 4t | 1,152 | 1,511 | 1,453 | 1,452 |
| c32 4t | 1,293 | 1,913 | 1,911 | 1,917 |
| cache 8t | 1,589 | 1,543 | 1,655 | 1,622 |
| c32 8t | 1,495 | 1,326 | 1,524 | 1,385 |
| cache 8>4t | 1,424 | 1,725 | 1,607 | 1,604 |
| c32 8>4t | 1,329 | 1,755 | 1,838 | 1,836 |

VM at 3 CPUs, second pass, 15 rounds:

| Case | 0 | fixed | cap2 | caphalf | decay64 | both | bothhalf | bothhalf:F64 | probe:F64 |
|---|---|---|---|---|---|---|---|---|---|
| cache 1t | 1,033 | 1,033 | 1,042 | 1,035 | 1,020 | 1,033 | 1,033 | 1,032 | 1,035 |
| cache 4t | 1,156 | 1,309 | 1,314 | 1,324 | 1,351 | 1,285 | 1,278 | 1,319 | 1,340 |
| c32 4t | 1,281 | 1,449 | 1,470 | 1,584 | 1,537 | 1,492 | 1,502 | 1,562 | 1,478 |
| cache 8t | 1,294 | 1,210 | 1,274 | 1,309 | 1,278 | 1,354 | 1,342 | 1,327 | 1,258 |
| c32 8t | 1,344 | 1,032 | 1,045 | 1,292 | 1,186 | 1,367 | 1,429 | 1,324 | 1,164 |
| cache 8>4t | 1,385 | 1,341 | 1,343 | 1,496 | 1,393 | 1,569 | 1,565 | 1,529 | 1,382 |
| c32 8>4t | 1,317 | 1,252 | 1,326 | 1,382 | 1,656 | 1,393 | 1,359 | 1,363 | 1,643 |

(`both` and `bothhalf` are the same arm at 3 CPUs, cap 1; so are `cap1` and `caphalf`.)

VM at 8 CPUs, second pass, 9 rounds:

| Case | 0 | fixed | cap2 | caphalf | decay64 | both | bothhalf | bothhalf:F64 | probe:F64 |
|---|---|---|---|---|---|---|---|---|---|
| cache 1t | 1,046 | 1,025 | 1,039 | 1,029 | 1,027 | 1,032 | 1,033 | 1,036 | 1,044 |
| cache 4t | 1,368 | 1,705 | 1,597 | 1,699 | 1,656 | 1,505 | 1,524 | 1,673 | 1,649 |
| c32 4t | 1,308 | 1,926 | 1,923 | 1,916 | 1,916 | 1,902 | 1,900 | 1,919 | 1,931 |
| cache 8t | 1,809 | 1,942 | 2,116 | 1,905 | 1,840 | 2,093 | 2,073 | 2,119 | 2,100 |
| c32 8t | 1,490 | 2,376 | 2,291 | 2,284 | 2,321 | 2,184 | 2,298 | 2,319 | 2,276 |
| cache 16t | 1,780 | 1,853 | 2,048 | 1,825 | 1,833 | 2,012 | 2,028 | 1,999 | 1,996 |
| c32 16t | 1,595 | 1,780 | 2,192 | 2,022 | 2,261 | 2,099 | 2,161 | 2,205 | 2,119 |
| cache 32t | 1,725 | 1,860 | 1,920 | 1,743 | 1,732 | 1,916 | 1,929 | 1,903 | 1,915 |
| c32 32t | 1,654 | 2,067 | 2,123 | 2,160 | 2,190 | 2,019 | 2,067 | 2,119 | 2,128 |
| fanout8 8t | 1,295 | 1,315 | 1,269 | 1,237 | 1,320 | 1,309 | 1,282 | 1,345 | 1,284 |
| fanout8 32t | 1,261 | 1,260 | 1,240 | 1,239 | 1,268 | 1,251 | 1,234 | 1,260 | 1,260 |
| cache 8>4t | 1,538 | 1,835 | 1,686 | 1,724 | 1,699 | 1,656 | 1,721 | 1,815 | 1,771 |
| c32 8>4t | 1,316 | 1,924 | 1,928 | 1,917 | 1,929 | 1,893 | 1,922 | 1,930 | 1,940 |

Mac, first pass, 5 rounds (36 threads oversubscribed; phase is 36 threads, then 4):

| Case | 0 | fixed | cap1 | cap2 | caphalf | decay | decay16 | decay64 | both | probe |
|---|---|---|---|---|---|---|---|---|---|---|
| cache 1t | 901 | 907 | 877 | 906 | 898 | 891 | 898 | 894 | 892 | 906 |
| c32 1t | 895 | 889 | 875 | 888 | 878 | 889 | 882 | 878 | 886 | 888 |
| cache 4t | 776 | 1,335 | 1,075 | 1,326 | 1,308 | 1,157 | 984 | 1,305 | 1,066 | 1,197 |
| c32 4t | 1,221 | 1,521 | 1,490 | 1,532 | 1,519 | 1,514 | 1,395 | 1,476 | 1,428 | 1,498 |
| cache 8t | 897 | 1,000 | 969 | 1,012 | 962 | 940 | 918 | 977 | 947 | 943 |
| c32 8t | 1,152 | 1,214 | 1,226 | 1,239 | 1,215 | 1,274 | 1,222 | 1,285 | 1,258 | 1,223 |
| cache 36t | 496 | 594 | 615 | 584 | 566 | 551 | 520 | 574 | 527 | 529 |
| c32 36t | 1,008 | 1,226 | 1,166 | 1,297 | 1,216 | 977 | 961 | 1,013 | 994 | 976 |
| fanout8 4t | 1,191 | 1,177 | 1,187 | 1,184 | 1,175 | 1,167 | 1,169 | 1,163 | 1,176 | 1,158 |
| fanout8 8t | 1,192 | 1,159 | 1,185 | 1,196 | 1,191 | 1,180 | 1,200 | 1,136 | 1,170 | 1,129 |
| fanout8 36t | 674 | 665 | 655 | 673 | 672 | 668 | 665 | 663 | 665 | 656 |
| cache 36>4t | 767 | 1,342 | 1,083 | 1,310 | 1,320 | 1,169 | 996 | 1,324 | 1,077 | 1,209 |
| c32 36>4t | 1,228 | 1,520 | 1,471 | 1,521 | 1,530 | 1,499 | 1,332 | 1,517 | 1,496 | 1,507 |

Mac, third pass, 5 rounds:

| Case | 0 | fixed | bothhalf:F64 | bothhalf:F128 |
|---|---|---|---|---|
| cache 1t | 919 | 901 | 908 | 916 |
| cache 4t | 780 | 1,366 | 1,366 | 1,404 |
| c32 4t | 1,209 | 1,528 | 1,515 | 1,535 |
| cache 36t | 506 | 634 | 582 | 598 |
| c32 36t | 1,000 | 1,228 | 1,036 | 1,078 |
| cache 36>4t | 772 | 1,352 | 1,379 | 1,403 |
| c32 36>4t | 1,192 | 1,542 | 1,540 | 1,537 |

The first VM pass (5 rounds, all arms at 2, 3, 4 and 8 CPUs) points the same way where the arms overlap and is not repeated here.

## Deviations from the Design

- **Not shipped, and not in the tree.** The bar says keep fixed, so `sqlite_mutex.rs` is unchanged, which keeps the shipped code exactly what 5a measured. The 5a-2 code (with its tests and sweep knobs) is in `adaptive-spin-2026-11-05.diff`, ready to re-apply, for instance for the pending x86-64 (htz) run, where `pause` costs differ.
- **The floor applies twice.** As written, the estimate is held to `floor..=budget` and the budget is `clamp(2 × estimate, floor, budget)`, so a decayed mutex spins `2 × floor` hints, not `floor`. The floor sweep (16, 32, 64, 128) spans minimum budgets of 32 to 256, so this changes no conclusion.
- **`DCO_SPIN_CAP=half`** was added to the sweep knobs, alongside the numbers: half the CPUs, at least 1, as for 5b's slot spinners.
- **A sweep-only slot-wait counter** (`genlock::sweep_slot_waits`, `_core._sweep_slot_waits()`, compiled only under `--cfg dco_spin_sweep`) was added to confirm that the 8-thread runs reach 5b's slot wait. It's in the diff too.
- **Not run:** the htz (x86-64) arm and floor sweep. With no arm chosen, there's nothing to confirm there.
