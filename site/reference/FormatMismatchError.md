## FormatMismatchError


A database or manifest in a format this version doesn't support, where opening the cache's


Usage

``` python
FormatMismatchError()
```


versioned subdirectory can't help: inside that subdirectory itself, or (from 0.2.3 and earlier) any cache made by a newer release. A cache directory that merely holds another format opens in its `diskcache-oxide-format-N` subdirectory instead (see the upgrading recipe).
