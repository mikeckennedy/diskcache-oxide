## Cache.close()


Close this handle and return its connections.


Usage

``` python
Cache.close(
    *,
    checkpoint=True,
)
```


When the last handle on a directory in the process closes, every connection to it closes, whichever threads used them. Using the cache again afterwards reopens it.


## Parameters


`checkpoint: bool = ``True`  
If this is the process's last handle and the write-ahead log is over half of `wal_size_limit`, checkpoint it first, waiting at most 1 second. A small log costs nothing extra.
