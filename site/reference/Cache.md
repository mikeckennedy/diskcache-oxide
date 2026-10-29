## Cache


A disk cache backed by SQLite, shared safely across threads and processes.


Usage

``` python
Cache(
    directory,
    timeout=None,
    *,
    size_limit=None,
    cull_limit=None,
    serializer="msgpack",
    pickle=False,
    clock=None,
    max_connections=None,
    sqlite_cache_size=None,
    wal_size_limit=None,
    wal_checkpoint="manual"
)
```


Every thread and process that opens the same directory shares one cache. The API follows `diskcache.Cache`; the deliberate differences are listed in the Divergences guide.


## Parameters


`directory: str | os.PathLike[str]`  
The cache directory. It's created (owner-only, `0700`) if it doesn't exist.

`timeout: float | None = None`  
Seconds a write waits for another process's lock before raising [Timeout](Timeout.md#diskcache_oxide.Timeout) (default 60).

`size_limit: int | None = None`  
The disk budget in bytes, database plus value files (default 1 GiB, minimum 1 MiB). Stored in the cache, so it applies to every process.

`cull_limit: int | None = None`  
How many least-recently-stored entries a [set()](Cache.set.md#diskcache_oxide.Cache.set) evicts when the cache is over [size_limit](Cache.size_limit.md#diskcache_oxide.Cache.size_limit) (default 10).

`serializer: str = ``"msgpack"`  
`'msgpack'` (the default) or `'json'`, for values that aren't plain `str` or `bytes`.

`pickle: bool = ``False`  
Allow pickle for values the serializer can't represent exactly. Without it, such values raise `TypeError` and pickled values are never read.

`clock: Callable[[], float] | None = None`  
A function returning the current time in seconds, used instead of the native clock. Tests pass `clock=lambda: time.time()` to follow a patched `time.time`.

`max_connections: int | None = None`  
The most SQLite connections this process opens for the directory, shared by every handle on it (default 8).

`sqlite_cache_size: int | None = None`  
SQLite's page cache per connection, in 4 KiB pages (default 256, 1 MiB).

`wal_size_limit: int | None = None`  
The write-ahead log is trimmed back to half this many bytes whenever it resets (default 64 MiB, minimum 1 MiB).

`wal_checkpoint: Literal[``"manual", `<span class="st">`"auto"``]`</span>` = ``"manual"`  
`'manual'` (the default) leaves checkpoints to [checkpoint()](Cache.checkpoint.md#diskcache_oxide.Cache.checkpoint) and [close()](Cache.close.md#diskcache_oxide.Cache.close). `'auto'` also checkpoints after writes once the log is over `wal_size_limit`.


## Raises


`ValueError`  
A setting is out of range, such as a [size_limit](Cache.size_limit.md#diskcache_oxide.Cache.size_limit) under 1 MiB.

`TypeError`  
An argument has the wrong type, including a `bool` for [timeout](Cache.timeout.md#diskcache_oxide.Cache.timeout).

`FormatMismatchError`  
The directory holds a database in a format this version can't read.

`Timeout`  
Another process held the database locked for longer than [timeout](Cache.timeout.md#diskcache_oxide.Cache.timeout).
