## TypeRegistry


Types the codecs can store without pickle: per-type `encode` and `decode` functions, or a dataclass's


Usage

``` python
TypeRegistry()
```


fields.

A registered value is stored as a tagged map, `{"$t:<name>": body}`, in both serializers. Reads look the name up in the registry of the reading process: an unregistered name is a [ForeignValueError](ForeignValueError.md#diskcache_oxide.ForeignValueError), and the row is left in place. Nothing is ever imported by name, which is what keeps this safe where pickle isn't.

[diskcache_oxide.registry](registry.md#diskcache_oxide.registry) is the process-wide instance, used by every cache; [register_type()](register_type.md#diskcache_oxide.register_type) and its siblings act on it. A `Cache(types=TypeRegistry())` consults its own registry first, then the global one.


## Examples

``` python
from bson import ObjectId
import diskcache_oxide

diskcache_oxide.register_type(ObjectId, name='bson.ObjectId', encode=str, decode=ObjectId)
diskcache_oxide.register_type(Episode, name='tp.Episode')   # a dataclass: no functions needed

cache.set('session', {'id': ObjectId(), 'episode': Episode(491, 'Rust and Python')})
cache.get('session')['id']   # an ObjectId again
```


## Methods

| Name | Description |
|----|----|
| [register()](#register) | Register `cls` under `name`. |
| [registered()](#registered) | The registrations, as `{name: cls}`. |
| [unregister()](#unregister) | Remove `cls`'s registration. Returns whether there was one. Rows already written with it become |

------------------------------------------------------------------------


#### register()


Register `cls` under `name`.


Usage

``` python
register(cls, *, name, encode=None, decode=None)
```


With `encode` and `decode`, `encode(instance)` must return a value the codec can already store (registered types included, nested) and `decode(value)` must give the instance back. With neither, `cls` must be a dataclass: its `init=True` fields are stored and read back with `getattr`, and it is rebuilt with `cls(**fields)`, so defaults and `__post_init__` run. A field removed or made required between the write and the read makes that row a [CorruptValueError](CorruptValueError.md#diskcache_oxide.CorruptValueError).

Lookup is by exact type: a subclass of a registered class is not registered. Last registration wins, by class and by name, so a reloaded module can register its new class object again.


##### Parameters


`cls: type[T]`  
The class. Not one of the codec's own exact types (`str`, `int`, `dict`, `datetime`, …), whose wire form is fixed; their subclasses are fine.

`name: str`  
The tag written into every row. Yours, so a class rename never orphans stored rows.

`encode: Callable[[T], object] | None = None`  
Instance to a storable value.

`decode: Callable[[Any], T] | None = None`  
That value back to an instance.


##### Raises


`ValueError`  
An empty name, or one of the codec's own types.

`TypeError`  
`cls` isn't a class; only one of `encode`/`decode` was given; neither was given and `cls` isn't a dataclass, or is one with an `InitVar` field (the constructor needs it but no instance holds it: pass explicit functions).


------------------------------------------------------------------------


#### registered()


The registrations, as `{name: cls}`.


Usage

``` python
registered()
```


------------------------------------------------------------------------


#### unregister()


Remove `cls`'s registration. Returns whether there was one. Rows already written with it become


Usage

``` python
unregister(cls)
```


[ForeignValueError](ForeignValueError.md#diskcache_oxide.ForeignValueError) on read until it is registered again.
