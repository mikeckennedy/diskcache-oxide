Django 6.1.3, Python 3.14.10, macbook-m5-pro, ac power.

`redis` is Django's RedisCache at redis://127.0.0.1:6379/0. Run in a container on a Mac, it answers through the VM's port forwarding: its numbers measure that as much as Redis.

## get and set latency, one thread (µs: get p50 / get p99 / set p50)

| Value | oxide | oxide-8 | diskcache | file | locmem | db | redis |
|---|---|---|---|---|---|---|---|
| small dict | 1.3 / 1.8 / 14.8 | 1.4 / 1.8 / 21.2 | 2.7 / 3.8 / 27.4 | 12.9 / 16.6 / 200.0 | 1.0 / 1.2 / 1.1 | 10.7 / 13.6 / 46.7 | 109.9 / 128.5 / 129.4 |
| 4 KiB str | 1.6 / 2.2 / 15.7 | 1.6 / 2.2 / 16.3 | 2.6 / 3.5 / 22.0 | 15.3 / 21.2 / 207.5 | 1.0 / 1.3 / 1.1 | 15.0 / 18.8 / 48.6 | 129.6 / 156.2 / 120.2 |
| 50 KiB response | 6.0 / 7.8 / 35.7 | 6.0 / 7.5 / 28.0 | 21.7 / 27.2 / 322.8 | 49.6 / 63.2 / 600.8 | 3.1 / 4.0 / 3.6 | 62.5 / 75.5 / 94.6 | 144.4 / 171.4 / 153.0 |
| 100 KiB response | 22.2 / 27.8 / 177.2 | 21.8 / 27.2 / 201.1 | 23.1 / 30.7 / 337.2 | 59.4 / 71.0 / 1193.4 | 3.8 / 4.8 / 4.2 | 110.4 / 133.8 / 140.8 | 171.9 / 196.0 / 169.3 |

Backend overhead: DjangoCache.get 1.46 µs against Cache.get 1.19 µs, 0.27 µs for the framework.

## threads, 99to1 (thousand ops/s at 1 / 2 / 4 / 8)

| Backend | 1 | 2 | 4 | 8 |
|---|---|---|---|---|
| oxide | 520 | 513 | 516 | 510 |
| oxide-8 | 575 | 572 | 559 | 572 |
| diskcache | 356 | 144 | 59 | 18 |
| file | 47 | 54 | 27 | 13 |
| locmem | 1067 | 1092 | 1073 | 1073 |
| db | 82 | 66 | 50 | 20 |
| redis | 8 | 17 | 15 | 14 |

## threads, 1000to1 (thousand ops/s at 1 / 2 / 4 / 8)

| Backend | 1 | 2 | 4 | 8 |
|---|---|---|---|---|
| oxide | 573 | 551 | 571 | 569 |
| oxide-8 | 653 | 653 | 658 | 653 |
| diskcache | 400 | 153 | 60 | 18 |
| file | 62 | 84 | 37 | 25 |
| locmem | 1091 | 1083 | 1093 | 1086 |
| db | 85 | 68 | 50 | 21 |
| redis | 9 | 15 | 15 | 14 |

## processes, 99to1 (thousand ops/s at 1 / 2 / 4 / 8)

| Backend | 1 | 2 | 4 | 8 |
|---|---|---|---|---|
| oxide | 514 | 610 | 607 | 269 |
| oxide-8 | 547 | 894 | 1397 | 1431 |
| diskcache | 341 | 610 | 1013 | 1270 |
| file | 43 | 65 | 115 | 129 |
| locmem | 1088 | 2177 | 4189 | 7249 |
| db | 79 | 144 | 262 | 300 |
| redis | 9 | 14 | 25 | 36 |

## processes, 1000to1 (thousand ops/s at 1 / 2 / 4 / 8)

| Backend | 1 | 2 | 4 | 8 |
|---|---|---|---|---|
| oxide | 586 | 821 | 597 | 295 |
| oxide-8 | 654 | 1098 | 1847 | 1754 |
| diskcache | 396 | 713 | 1294 | 1567 |
| file | 66 | 100 | 158 | 159 |
| locmem | 1095 | 2188 | 4176 | 7138 |
| db | 84 | 155 | 280 | 325 |
| redis | 9 | 17 | 29 | 37 |

## async reads on oxide (µs: p50 / p99 / p99.9 / max)

| Case | aget (thread hop) | get inline |
|---|---|---|
| quiet, small dict | 58.1 / 74.5 / 190.8 / 1403 | 2.5 / 4.6 / 12.1 / 131 |
| quiet, 100 KiB response | 85.6 / 104.1 / 137.7 / 564 | 23.6 / 33.4 / 64.8 / 230 |
| beside a writer process, small dict | 58.2 / 70.6 / 81.8 / 295 | 7.2 / 10.8 / 19.6 / 99 |
