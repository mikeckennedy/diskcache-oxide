## register_type()


Register `cls` in the process-wide [registry](registry.md#diskcache_oxide.registry), so every cache can store it without pickle.


Usage

``` python
register_type(
    cls,
    *,
    name,
    encode=None,
    decode=None,
)
```


`encode(instance)` must return a value the codec can already store and `decode(value)` must give the instance back. A dataclass needs neither: its `init=True` fields are stored and it is rebuilt with `cls(**fields)`. See [TypeRegistry.register](TypeRegistry.md#diskcache_oxide.TypeRegistry.register) for the rules. Call it at import time, in every process that opens the cache.


## Examples

``` python
from bson import ObjectId
import diskcache_oxide

diskcache_oxide.register_type(ObjectId, name='bson.ObjectId', encode=str, decode=ObjectId)
diskcache_oxide.register_type(Episode, name='tp.Episode')  # a dataclass
```
