## Cache.directory


The directory the cache is in: the one given to the constructor, or its


`Cache.directory: str`


`diskcache-oxide-format-N` subdirectory when the given one holds another format. To open the same cache again, pass the original directory, not this one: it picks the right subdirectory for whichever release opens it.
