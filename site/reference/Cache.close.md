## Cache.close()


Close this handle and return its connections.


Usage

``` python
Cache.close(
    *,
    checkpoint=True,
)
```


When the last handle on a directory in the process closes, every connection to it closes, whichever threads used them, and the folders that large values use are swept (D-042): a temp file no writer has touched for an hour, left by a process killed mid-write, is removed, and a folder left empty goes (those not touched in the last 30 seconds), for at most 100 ms in all, unless this thread is inside a transaction. A handle dropped without [close()](Cache.close.md#diskcache_oxide.Cache.close) does the same. Using the cache again afterwards reopens it.

It never raises. Behind a fork that stays stuck it returns after about 2 seconds at most, whatever [timeout](Cache.timeout.md#diskcache_oxide.Cache.timeout) is, and those connections close when the fork ends.


## Parameters


`checkpoint: bool = ``True`  
If this is the process's last handle and the write-ahead log is over half of `wal_size_limit`, checkpoint it first, waiting at most 1 second. A small log costs nothing extra.


## Examples

``` python
with Cache('/var/cache/my-app') as cache:   # closed at the end of the block
    cache.set('k', 'v')
cache.close(checkpoint=False)                # skip the checkpoint, for a quick exit
```
