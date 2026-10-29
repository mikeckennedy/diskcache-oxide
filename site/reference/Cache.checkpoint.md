## Cache.checkpoint()


Checkpoint the write-ahead log and truncate it, retrying until the cache's timeout.


Usage

``` python
Cache.checkpoint()
```


Returns True if the WAL was reset, False if readers or writers in other processes kept it busy. Writes never checkpoint by default (`wal_checkpoint='manual'`): call this when a short pause suits the app, such as at shutdown or after a large [clear()](Cache.clear.md#diskcache_oxide.Cache.clear). [close()](Cache.close.md#diskcache_oxide.Cache.close) also does it for a WAL left large.
