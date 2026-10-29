## Cache.expire()


Remove every expired entry, and its value file.


Usage

``` python
Cache.expire()
```


Works in chunks and lets other writers in between them.


## Returns


`int`  
How many entries were removed.
