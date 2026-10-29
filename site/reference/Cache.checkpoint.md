## Cache.checkpoint()


Copy the write-ahead log into the database and truncate it to zero.


Usage

``` python
Cache.checkpoint(timeout=None)
```


With readers in other processes, a burst of writes can leave the log much larger than the database. Call this when a short pause suits the app: at shutdown, in a maintenance job, or after a large [clear()](Cache.clear.md#diskcache_oxide.Cache.clear). [close()](Cache.close.md#diskcache_oxide.Cache.close) also does it for a log left large.


## Parameters


`timeout: float | None = None`  
Seconds to keep retrying while other processes are busy (default: the cache's [timeout](Cache.timeout.md#diskcache_oxide.Cache.timeout)).


## Returns


`bool`  
True if the log was reset, False if readers or writers in other processes kept it

busy. It never raises for a busy database.
