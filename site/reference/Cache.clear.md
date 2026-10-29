## Cache.clear()


Remove every entry that existed when the call started.


Usage

``` python
Cache.clear()
```


Works in chunks and lets other writers in between them. Writes that land while it runs, including overwrites of existing keys, are kept.


## Returns


`int`  
How many entries were removed.
