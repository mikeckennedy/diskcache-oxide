## ENOVAL


diskcache's "no value" sentinel. Pass it as `default=` to tell a miss from a stored `None`


`ENOVAL=_Constant(``"ENOVAL")`


(`cache.get(key, ENOVAL) is ENOVAL` only on a miss); `reset(key)` without a value reads the setting. A singleton that pickles by reference, and can't be stored as a value.
