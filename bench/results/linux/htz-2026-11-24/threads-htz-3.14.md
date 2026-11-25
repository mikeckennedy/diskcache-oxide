Threads in one process, 1,000 keys of a 1 KB str, median of 5 rounds of 1.0 s, thousands of calls a second. htz-3.14 (8 CPUs), unknown power. Arms: 3.14: Python 3.14.2, oxide 0.5.8.

**reads**, calls a second:

| Arm, variant | 1 | 2 | 4 | 8 |
|---|---|---|---|---|
| 3.14, oxide Cache, shared | 195k | 170k | 156k | 153k |
| 3.14, oxide Cache, handle per thread | 203k | 176k | 148k | 151k |
| 3.14, oxide FanoutCache(8), shared | 213k | 182k | 161k | 158k |
| 3.14, oxide FanoutCache(8), handle per thread | 212k | 174k | 163k | 155k |
| 3.14, oxide Cache, directory per thread | 204k | 175k | 154k | 146k |
| 3.14, oxide getter, shared Cache | 9,801k | 8,835k | 8,711k | 8,403k |
| 3.14, dict lookup | 17,613k | 16,321k | 14,958k | 15,704k |
| 3.14, diskcache Cache, shared | 96k | 38k | 11k | 6k |
| 3.14, diskcache FanoutCache(8), shared | 86k | 31k | 10k | 5k |

**reads**, as a share of "oxide Cache, directory per thread":

| Arm, variant | 1 | 2 | 4 | 8 |
|---|---|---|---|---|
| 3.14, oxide Cache, shared | 96% | 98% | 101% | 105% |
| 3.14, oxide Cache, handle per thread | 99% | 101% | 96% | 103% |
| 3.14, oxide FanoutCache(8), shared | 105% | 104% | 104% | 108% |
| 3.14, oxide FanoutCache(8), handle per thread | 104% | 100% | 105% | 106% |
| 3.14, oxide getter, shared Cache | 4810% | 5057% | 5646% | 5751% |
| 3.14, dict lookup | 8644% | 9341% | 9695% | 10748% |
| 3.14, diskcache Cache, shared | 47% | 21% | 7% | 4% |
| 3.14, diskcache FanoutCache(8), shared | 42% | 18% | 7% | 3% |

**1000:1**, calls a second:

| Arm, variant | 1 | 2 | 4 | 8 |
|---|---|---|---|---|
| 3.14, oxide Cache, shared | 189k | 162k | 149k | 145k |
| 3.14, oxide Cache, handle per thread | 196k | 165k | 152k | 147k |
| 3.14, oxide FanoutCache(8), shared | 206k | 174k | 152k | 154k |
| 3.14, oxide FanoutCache(8), handle per thread | 201k | 179k | 160k | 153k |
| 3.14, oxide Cache, directory per thread | 199k | 174k | 141k | 148k |
| 3.14, diskcache Cache, shared | 94k | 38k | 11k | 5k |
| 3.14, diskcache FanoutCache(8), shared | 83k | 30k | 10k | 5k |

**1000:1**, as a share of "oxide Cache, directory per thread":

| Arm, variant | 1 | 2 | 4 | 8 |
|---|---|---|---|---|
| 3.14, oxide Cache, shared | 95% | 93% | 105% | 98% |
| 3.14, oxide Cache, handle per thread | 98% | 95% | 108% | 99% |
| 3.14, oxide FanoutCache(8), shared | 103% | 100% | 108% | 104% |
| 3.14, oxide FanoutCache(8), handle per thread | 101% | 103% | 113% | 104% |
| 3.14, diskcache Cache, shared | 47% | 22% | 8% | 4% |
| 3.14, diskcache FanoutCache(8), shared | 41% | 17% | 7% | 3% |

**99:1**, calls a second:

| Arm, variant | 1 | 2 | 4 | 8 |
|---|---|---|---|---|
| 3.14, oxide Cache, shared | 173k | 145k | 130k | 127k |
| 3.14, oxide Cache, handle per thread | 167k | 147k | 132k | 127k |
| 3.14, oxide FanoutCache(8), shared | 176k | 157k | 136k | 138k |
| 3.14, oxide FanoutCache(8), handle per thread | 174k | 143k | 135k | 131k |
| 3.14, oxide Cache, directory per thread | 173k | 149k | 134k | 132k |
| 3.14, diskcache Cache, shared | 83k | 35k | 11k | 5k |
| 3.14, diskcache FanoutCache(8), shared | 74k | 28k | 10k | 5k |

**99:1**, as a share of "oxide Cache, directory per thread":

| Arm, variant | 1 | 2 | 4 | 8 |
|---|---|---|---|---|
| 3.14, oxide Cache, shared | 100% | 97% | 97% | 96% |
| 3.14, oxide Cache, handle per thread | 97% | 99% | 99% | 96% |
| 3.14, oxide FanoutCache(8), shared | 102% | 105% | 101% | 105% |
| 3.14, oxide FanoutCache(8), handle per thread | 101% | 96% | 101% | 99% |
| 3.14, diskcache Cache, shared | 48% | 24% | 8% | 4% |
| 3.14, diskcache FanoutCache(8), shared | 43% | 19% | 7% | 4% |
