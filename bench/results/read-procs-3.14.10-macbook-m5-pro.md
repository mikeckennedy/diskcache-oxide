Reads only, 1,000 keys of a 1 KB str, one thread per process, median of 3 rounds. Python 3.14.10, macbook-m5-pro, ac power.

| Processes | oxide Cache | oxide FanoutCache(8) | diskcache FanoutCache(8) |
|---|---|---|---|
| 1 | 596k | 664k | 384k |
| 2 | 836k | 1,067k | 779k |
| 4 | 605k | 1,797k | 1,415k |
| 8 | 296k | 1,741k | 1,684k |
