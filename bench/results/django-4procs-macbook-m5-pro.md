4 worker processes through `django.core.cache`, one thread each, 1,000 keys of a 1 KB str, median of 3 rounds of 3 s. Django 6.1.3, MacBook Pro (M5 Pro), AC power, not under nice, 2026-11-18. The same code as `bench/django_procs.py`, run before it was moved there. `oxide` is a plain `Cache`, `oxide-8` is `OPTIONS = {'shards': 8}`, `diskcache` is diskcache 5.6.3's backend (a FanoutCache of 8 shards, its default), `db` is `DatabaseCache` on SQLite (WAL), `file` is `FileBasedCache`, `locmem` is `LocMemCache` (each process's own dict, not shared).

| Mix | Backend | 3.14 | 3.14 rounds | 3.14t | 3.14t rounds |
|---|---|---|---|---|---|
| 99:1 | oxide | 597k | 586, 616, 597 | 573k | 582, 521, 573 |
| 99:1 | oxide-8 | 1,320k | 1313, 1320, 1355 | 1,279k | 1278, 1279, 1279 |
| 99:1 | diskcache | 970k | 970, 958, 986 | 939k | 939, 894, 961 |
| 99:1 | db | 258k | 260, 257, 258 | 241k | 239, 241, 245 |
| 99:1 | file | 117k | 117, 117, 116 | 117k | 117, 116, 118 |
| 99:1 | locmem | 4,096k | 4118, 4096, 4051 | 4,195k | 4202, 4183, 4195 |
| 1,000:1 | oxide | 622k | 622, 622, 620 | 584k | 588, 584, 555 |
| 1,000:1 | oxide-8 | 1,750k | 1761, 1684, 1750 | 1,643k | 1641, 1643, 1660 |
| 1,000:1 | diskcache | 1,165k | 1246, 1160, 1165 | 1,237k | 1147, 1237, 1288 |
| 1,000:1 | db | 280k | 267, 280, 281 | 269k | 253, 269, 271 |
| 1,000:1 | file | 156k | 153, 156, 156 | 158k | 155, 158, 158 |
| 1,000:1 | locmem | 4,105k | 4105, 4035, 4106 | 4,217k | 4217, 4213, 4248 |
