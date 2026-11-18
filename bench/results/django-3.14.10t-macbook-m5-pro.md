Django 6.1.3, Python 3.14.10t, macbook-m5-pro, ac power.

`redis` is Django's RedisCache at redis://127.0.0.1:6379/0. Run in a container on a Mac, it answers through the VM's port forwarding: its numbers measure that as much as Redis.

## get and set latency, one thread (µs: get p50 / get p99 / set p50)

| Value | oxide | oxide-8 | diskcache | file | locmem | db | redis |
|---|---|---|---|---|---|---|---|
| small dict | 1.4 / 1.9 / 15.9 | 1.4 / 1.8 / 22.7 | 2.6 / 3.6 / 28.3 | 13.7 / 16.2 / 193.0 | 1.0 / 1.3 / 1.1 | 11.2 / 13.7 / 48.0 | 105.4 / 122.7 / 111.1 |
| 4 KiB str | 1.6 / 2.2 / 16.2 | 1.6 / 2.1 / 16.6 | 2.5 / 3.4 / 22.1 | 15.7 / 19.4 / 200.8 | 1.0 / 1.2 / 1.1 | 15.4 / 18.1 / 51.2 | 108.4 / 123.9 / 131.9 |
| 50 KiB response | 7.2 / 9.9 / 37.1 | 7.6 / 9.1 / 29.5 | 19.7 / 23.0 / 306.8 | 49.0 / 54.4 / 587.1 | 3.1 / 3.8 / 3.7 | 62.9 / 73.4 / 98.8 | 149.4 / 162.5 / 156.8 |
| 100 KiB response | 20.4 / 23.9 / 174.5 | 20.7 / 24.1 / 199.4 | 23.9 / 29.6 / 327.5 | 57.5 / 64.9 / 1174.5 | 4.0 / 5.1 / 4.4 | 113.7 / 133.3 / 153.3 | 170.2 / 213.3 / 164.1 |

Backend overhead: DjangoCache.get 1.43 µs against Cache.get 1.18 µs, 0.25 µs for the framework.

## threads, 99to1 (thousand ops/s at 1 / 2 / 4 / 8)

| Backend | 1 | 2 | 4 | 8 |
|---|---|---|---|---|
| oxide | 496 | 600 | 765 | 676 |
| oxide-8 | 558 | 679 | 1071 | 871 |
| diskcache | 369 | 465 | 527 | 430 |
| file | 47 | 70 | 113 | 121 |
| locmem | 991 | 879 | 769 | 487 |
| db | 70 | 97 | 134 | 79 |
| redis | 9 | 16 | 22 | 22 |

## threads, 1000to1 (thousand ops/s at 1 / 2 / 4 / 8)

| Backend | 1 | 2 | 4 | 8 |
|---|---|---|---|---|
| oxide | 579 | 781 | 978 | 823 |
| oxide-8 | 652 | 784 | 1254 | 993 |
| diskcache | 427 | 603 | 1008 | 986 |
| file | 59 | 92 | 144 | 141 |
| locmem | 996 | 840 | 753 | 450 |
| db | 78 | 102 | 134 | 81 |
| redis | 9 | 17 | 22 | 21 |

## processes, 99to1 (thousand ops/s at 1 / 2 / 4 / 8)

| Backend | 1 | 2 | 4 | 8 |
|---|---|---|---|---|
| oxide | 508 | 581 | 591 | 254 |
| oxide-8 | 536 | 875 | 1220 | 1255 |
| diskcache | 347 | 615 | 934 | 1193 |
| file | 45 | 70 | 119 | 132 |
| locmem | 1094 | 2217 | 3735 | 6602 |
| db | 74 | 136 | 237 | 281 |
| redis | 8 | 14 | 24 | 35 |

## processes, 1000to1 (thousand ops/s at 1 / 2 / 4 / 8)

| Backend | 1 | 2 | 4 | 8 |
|---|---|---|---|---|
| oxide | 581 | 804 | 599 | 278 |
| oxide-8 | 637 | 1049 | 1749 | 1722 |
| diskcache | 402 | 764 | 1327 | 1602 |
| file | 68 | 104 | 161 | 159 |
| locmem | 1109 | 2175 | 3890 | 6756 |
| db | 78 | 145 | 266 | 319 |
| redis | 9 | 17 | 27 | 36 |

## async reads on oxide (µs: p50 / p99 / p99.9 / max)

| Case | aget (thread hop) | get inline |
|---|---|---|
| quiet, small dict | 65.8 / 108.7 / 261.9 / 541 | 3.0 / 6.2 / 29.2 / 441 |
| quiet, 100 KiB response | 91.2 / 164.5 / 356.5 / 1260 | 23.8 / 48.8 / 141.9 / 318 |
| beside a writer process, small dict | 61.2 / 116.4 / 266.0 / 6742 | 7.0 / 13.6 / 53.5 / 998 |
