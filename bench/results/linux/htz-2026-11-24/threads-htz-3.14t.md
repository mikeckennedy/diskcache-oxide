Threads in one process, 1,000 keys of a 1 KB str, median of 5 rounds of 1.0 s, thousands of calls a second. htz-3.14t (8 CPUs), unknown power. Arms: 3.14t: Python 3.14.3t, oxide 0.5.8.

**reads**, calls a second:

| Arm, variant | 1 | 2 | 4 | 8 |
|---|---|---|---|---|
| 3.14t, oxide Cache, shared | 202k | 401k | 642k | 826k |
| 3.14t, oxide Cache, handle per thread | 194k | 466k | 629k | 859k |
| 3.14t, oxide FanoutCache(8), shared | 214k | 321k | 591k | 784k |
| 3.14t, oxide FanoutCache(8), handle per thread | 200k | 264k | 515k | 805k |
| 3.14t, oxide Cache, directory per thread | 203k | 384k | 739k | 1,271k |
| 3.14t, oxide getter, shared Cache | 11,332k | 9,724k | 5,102k | 4,441k |
| 3.14t, dict lookup | 14,992k | 20,415k | 18,306k | 32,372k |
| 3.14t, diskcache Cache, shared | 135k | 160k | 209k | 492k |
| 3.14t, diskcache FanoutCache(8), shared | 118k | 201k | 320k | 491k |

**reads**, as a share of "oxide Cache, directory per thread":

| Arm, variant | 1 | 2 | 4 | 8 |
|---|---|---|---|---|
| 3.14t, oxide Cache, shared | 100% | 104% | 87% | 65% |
| 3.14t, oxide Cache, handle per thread | 96% | 121% | 85% | 68% |
| 3.14t, oxide FanoutCache(8), shared | 106% | 84% | 80% | 62% |
| 3.14t, oxide FanoutCache(8), handle per thread | 98% | 69% | 70% | 63% |
| 3.14t, oxide getter, shared Cache | 5585% | 2531% | 691% | 349% |
| 3.14t, dict lookup | 7389% | 5314% | 2479% | 2546% |
| 3.14t, diskcache Cache, shared | 67% | 42% | 28% | 39% |
| 3.14t, diskcache FanoutCache(8), shared | 58% | 52% | 43% | 39% |

**1000:1**, calls a second:

| Arm, variant | 1 | 2 | 4 | 8 |
|---|---|---|---|---|
| 3.14t, oxide Cache, shared | 188k | 320k | 527k | 724k |
| 3.14t, oxide Cache, handle per thread | 192k | 326k | 536k | 666k |
| 3.14t, oxide FanoutCache(8), shared | 210k | 268k | 521k | 741k |
| 3.14t, oxide FanoutCache(8), handle per thread | 215k | 340k | 497k | 753k |
| 3.14t, oxide Cache, directory per thread | 197k | 374k | 683k | 1,208k |
| 3.14t, diskcache Cache, shared | 126k | 175k | 208k | 374k |
| 3.14t, diskcache FanoutCache(8), shared | 110k | 174k | 289k | 445k |

**1000:1**, as a share of "oxide Cache, directory per thread":

| Arm, variant | 1 | 2 | 4 | 8 |
|---|---|---|---|---|
| 3.14t, oxide Cache, shared | 95% | 85% | 77% | 60% |
| 3.14t, oxide Cache, handle per thread | 97% | 87% | 78% | 55% |
| 3.14t, oxide FanoutCache(8), shared | 107% | 72% | 76% | 61% |
| 3.14t, oxide FanoutCache(8), handle per thread | 109% | 91% | 73% | 62% |
| 3.14t, diskcache Cache, shared | 64% | 47% | 31% | 31% |
| 3.14t, diskcache FanoutCache(8), shared | 56% | 47% | 42% | 37% |

**99:1**, calls a second:

| Arm, variant | 1 | 2 | 4 | 8 |
|---|---|---|---|---|
| 3.14t, oxide Cache, shared | 170k | 298k | 379k | 506k |
| 3.14t, oxide Cache, handle per thread | 170k | 249k | 374k | 464k |
| 3.14t, oxide FanoutCache(8), shared | 184k | 295k | 412k | 609k |
| 3.14t, oxide FanoutCache(8), handle per thread | 173k | 216k | 391k | 589k |
| 3.14t, oxide Cache, directory per thread | 163k | 310k | 571k | 967k |
| 3.14t, diskcache Cache, shared | 111k | 94k | 132k | 153k |
| 3.14t, diskcache FanoutCache(8), shared | 98k | 162k | 204k | 190k |

**99:1**, as a share of "oxide Cache, directory per thread":

| Arm, variant | 1 | 2 | 4 | 8 |
|---|---|---|---|---|
| 3.14t, oxide Cache, shared | 104% | 96% | 66% | 52% |
| 3.14t, oxide Cache, handle per thread | 104% | 80% | 65% | 48% |
| 3.14t, oxide FanoutCache(8), shared | 113% | 95% | 72% | 63% |
| 3.14t, oxide FanoutCache(8), handle per thread | 106% | 70% | 68% | 61% |
| 3.14t, diskcache Cache, shared | 68% | 30% | 23% | 16% |
| 3.14t, diskcache FanoutCache(8), shared | 60% | 52% | 36% | 20% |
