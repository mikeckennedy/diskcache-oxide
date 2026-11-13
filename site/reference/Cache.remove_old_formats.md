## Cache.remove_old_formats()


Remove the caches an older diskcache-oxide format left in this cache's directory: the


Usage

``` python
Cache.remove_old_formats()
```


directory's own cache, when it's in an older format than this release's, and any `diskcache-oxide-format-M` subdirectory holding one. Returns the database files, manifests and directories removed (value files inside a removed value folder aren't listed one by one); `[]` when there's nothing old.

## Warnings

it doesn't check whether a process running an older diskcache-oxide still uses those files. Call it once every old worker has stopped, after draining any work queue they share. If an old worker is still running, on Linux and macOS its cache can be corrupted until it restarts (errors such as "database disk image is malformed", or wrong results, not just misses), as with `rm -rf` under it, and everything it kept there is lost. On Windows, files an old worker has open are left, with a warning, for a later call.

Never removed: a cache in this release's format or a newer one, anything diskcache-oxide didn't write, anything behind a symbolic link, a database this process has open, a cache whose format can't be read, and an empty database that may still be being created (one in a subdirectory or a shard, or at the root under 10 minutes old). What was removed is logged at `INFO`, and what was left, with why, at `WARNING`, on the `diskcache_oxide` logger.


## Raises


`OSError`  
The directory can't be listed.
