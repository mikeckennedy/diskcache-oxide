## Cache.check()


Check the database and the value files against each other, as diskcache's [check()](Cache.check.md#diskcache_oxide.Cache.check).


Usage

``` python
Cache.check(
    fix=False,
    retry=False,
)
```


Reports, as a list of `warnings.WarningMessage` (nothing is issued through `warnings`): lines of SQLite's `integrity_check`; a spilled entry whose value file is missing or the wrong size (`file not found: ...`, `wrong file size: ...`); a file in a value folder, or the cache directory, that no entry names and that is over an hour old (`unknown file: ...`, an `UnknownFileWarning`; a younger one may be a writer's in flight); and stored totals that don't match the rows. Empty value folders aren't reported: they're normal on an open cache. Runs in short steps, so writers and forks aren't held up, apart from `integrity_check` and `VACUUM`.


## Parameters


`fix: bool = ``False`  
Also repair: remove an entry whose file is missing or the wrong size (it reads as a miss anyway), remove unknown files in value folders (never elsewhere), sweep empty value folders, correct the totals, then `VACUUM` and checkpoint. `VACUUM` writes the whole database through the write-ahead log and needs a temporary copy of it in `SQLITE_TMPDIR`, `TMPDIR` or `/tmp`; forks and writers wait for it.

`retry: bool = ``False`  
On a [Timeout](Timeout.md#diskcache_oxide.Timeout), start again until it gets through.


## Raises


`RuntimeError`  
Inside this thread's own [transact()](Cache.transact.md#diskcache_oxide.Cache.transact) block.

`Timeout`  
A step waited longer than [timeout](Cache.timeout.md#diskcache_oxide.Cache.timeout) (and `retry` is False).
