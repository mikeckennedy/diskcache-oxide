Threads in one process, 1,000 keys of a 1 KB str, median of 7 rounds of 1.0 s, thousands of calls a second. htz-kernel-3.14t (8 CPUs), unknown power. Arms: 3.14t: Python 3.14.3t, oxide 0.5.8.

**reads**, calls a second:

| Arm, variant | 1 | 2 | 3 | 4 | 8 |
|---|---|---|---|---|---|
| 3.14t, oxide Cache, shared | 203k | 411k | 487k | 560k | 765k |
| 3.14t, oxide FanoutCache(8), shared | 220k | 277k | 412k | 508k | 791k |
| 3.14t, oxide Cache, directory per thread | 201k | 369k | 525k | 705k | 1,232k |

**reads**, as a share of "oxide Cache, directory per thread":

| Arm, variant | 1 | 2 | 3 | 4 | 8 |
|---|---|---|---|---|---|
| 3.14t, oxide Cache, shared | 101% | 111% | 93% | 79% | 62% |
| 3.14t, oxide FanoutCache(8), shared | 109% | 75% | 78% | 72% | 64% |

**reads**, share of CPU time in the kernel:

| Arm, variant | 1 | 2 | 3 | 4 | 8 |
|---|---|---|---|---|---|
| 3.14t, oxide Cache, shared | 45% | 24% | 14% | 11% | 18% |
| 3.14t, oxide FanoutCache(8), shared | 39% | 29% | 27% | 25% | 20% |
| 3.14t, oxide Cache, directory per thread | 45% | 44% | 42% | 42% | 39% |

**1000:1**, calls a second:

| Arm, variant | 1 | 2 | 3 | 4 | 8 |
|---|---|---|---|---|---|
| 3.14t, oxide Cache, shared | 195k | 326k | 464k | 523k | 596k |
| 3.14t, oxide FanoutCache(8), shared | 198k | 271k | 423k | 601k | 766k |
| 3.14t, oxide Cache, directory per thread | 185k | 360k | 527k | 661k | 1,213k |

**1000:1**, as a share of "oxide Cache, directory per thread":

| Arm, variant | 1 | 2 | 3 | 4 | 8 |
|---|---|---|---|---|---|
| 3.14t, oxide Cache, shared | 105% | 90% | 88% | 79% | 49% |
| 3.14t, oxide FanoutCache(8), shared | 107% | 75% | 80% | 91% | 63% |

**1000:1**, share of CPU time in the kernel:

| Arm, variant | 1 | 2 | 3 | 4 | 8 |
|---|---|---|---|---|---|
| 3.14t, oxide Cache, shared | 45% | 30% | 20% | 20% | 20% |
| 3.14t, oxide FanoutCache(8), shared | 41% | 31% | 30% | 28% | 21% |
| 3.14t, oxide Cache, directory per thread | 45% | 43% | 42% | 41% | 39% |

**99:1**, calls a second:

| Arm, variant | 1 | 2 | 3 | 4 | 8 |
|---|---|---|---|---|---|
| 3.14t, oxide Cache, shared | 170k | 253k | 313k | 354k | 454k |
| 3.14t, oxide FanoutCache(8), shared | 174k | 243k | 330k | 423k | 593k |
| 3.14t, oxide Cache, directory per thread | 172k | 310k | 439k | 565k | 999k |

**99:1**, as a share of "oxide Cache, directory per thread":

| Arm, variant | 1 | 2 | 3 | 4 | 8 |
|---|---|---|---|---|---|
| 3.14t, oxide Cache, shared | 99% | 82% | 71% | 63% | 45% |
| 3.14t, oxide FanoutCache(8), shared | 101% | 78% | 75% | 75% | 59% |

**99:1**, share of CPU time in the kernel:

| Arm, variant | 1 | 2 | 3 | 4 | 8 |
|---|---|---|---|---|---|
| 3.14t, oxide Cache, shared | 45% | 34% | 28% | 28% | 26% |
| 3.14t, oxide FanoutCache(8), shared | 40% | 38% | 33% | 32% | 25% |
| 3.14t, oxide Cache, directory per thread | 44% | 43% | 44% | 43% | 42% |
