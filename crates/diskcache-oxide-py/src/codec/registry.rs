//! The type registry (plan 16): how to store a type the codecs don't know, without pickle.
//!
//! A registered type is written as the one-entry tag map the codecs already use, with the key
//! `$t:` followed by the name it was registered under and the body being what its `encode`
//! function returned (or, for a dataclass, a map of its `init=True` fields), encoded by the normal
//! path. On the way out the name is looked up in the registry of the reading process; an
//! unregistered name is a `ForeignValueError`. Nothing is ever imported by name, which is the
//! whole difference from pickle.
//!
//! Lookups are by exact type on the way in and by name on the way out, through a lock-free
//! snapshot ([`Snapshot`]): readers take one atomic load per cache call and never a lock, so a
//! child forked while another thread was encoding can't inherit a held lock, and 3.14t threads
//! never queue on one. Writers go through `ArcSwap::rcu` (load, copy, change, compare-and-swap);
//! registrations are rare and last-wins, so a retried closure is harmless.

use std::cell::Cell;
use std::collections::HashMap;
use std::sync::Arc;

use arc_swap::{ArcSwap, Guard};
use pyo3::exceptions::{PyTypeError, PyValueError};
use pyo3::prelude::*;
use pyo3::sync::PyOnceLock;
use pyo3::types::{
    PyBool, PyBytes, PyDict, PyFloat, PyFrozenSet, PyInt, PyList, PyNone, PySet, PyString, PyTuple, PyType,
};
use pyo3::{PyTraverseError, PyVisit, intern};

use super::common;
use crate::CorruptValueError;

/// The prefix of a registered type's tag key. The `$` namespace is reserved and escaped already,
/// and no built-in tag starts with `$t:` (`$tuple` is `$t` followed by `u`), so the prefix keeps
/// registered names apart from the built-in tags whatever they are called.
pub const TAG_PREFIX: &str = "$t:";

/// A type object's address, as the key it is registered under.
///
/// Safe as a key because the entry it maps to owns a strong `Py<PyType>`, so the address can't be
/// reused while the type is registered, and a reader's [`Snapshot`] keeps the old map's `Arc`
/// alive, so a type can't be freed under an in-flight encode even after `unregister`.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
struct TypeAddr(usize);

impl TypeAddr {
    fn of(ty: &Bound<'_, PyType>) -> Self {
        Self(ty.as_ptr().addr())
    }

    fn of_py(ty: &Py<PyType>) -> Self {
        Self(ty.as_ptr().addr())
    }
}

/// How a registered type is turned into a storable value and back.
#[derive(Debug)]
pub enum Kind {
    /// `encode(instance)` gives the body; `decode(body)` gives the instance back.
    Explicit { encode: Py<PyAny>, decode: Py<PyAny> },
    /// A dataclass: the body is a map of these fields (`init=True`, in order), read with
    /// `getattr`; the instance is rebuilt with `cls(**body)`, so defaults and `__post_init__` run.
    Dataclass { fields: Vec<Py<PyString>> },
}

/// One registration.
#[derive(Debug)]
pub struct Entry {
    pub ty: Py<PyType>,
    pub name: String,
    /// `$t:` + `name`, computed once so an encode allocates nothing for it.
    pub tag: String,
    pub kind: Kind,
}

/// The registrations, indexed both ways over the same entries. Replaced whole on every change.
#[derive(Clone, Debug, Default)]
struct RegistryMap {
    by_type: HashMap<TypeAddr, Arc<Entry>>,
    by_name: HashMap<String, Arc<Entry>>,
}

impl RegistryMap {
    /// Drops the entry registered for `ty` and the one holding `name` (which may be the same, or
    /// two different entries), so a registration replaces both: last wins by class and by name.
    fn without(&mut self, ty: TypeAddr, name: &str) {
        if let Some(old) = self.by_type.remove(&ty) {
            self.by_name.remove(&old.name);
        }
        if let Some(old) = self.by_name.remove(name) {
            self.by_type.remove(&TypeAddr::of_py(&old.ty));
        }
    }
}

/// A registry of types the codecs can store without pickle: per-type `encode` and `decode`
/// functions, or a dataclass's fields. One global instance (`diskcache_oxide.registry`) serves every
/// cache in the process; a cache opened with `types=` consults its own first.
#[pyclass(frozen, module = "diskcache_oxide")]
#[derive(Debug)]
pub struct TypeRegistry {
    map: ArcSwap<RegistryMap>,
}

impl Default for TypeRegistry {
    fn default() -> Self {
        Self { map: ArcSwap::from_pointee(RegistryMap::default()) }
    }
}

#[pymethods]
impl TypeRegistry {
    #[new]
    fn py_new() -> Self {
        Self::default()
    }

    /// Registers `cls` under `name`. With `encode` and `decode`, `encode(instance)` must return a
    /// value the codec can already store (registered types included) and `decode(value)` must give
    /// the instance back. With neither, `cls` must be a dataclass: its `init=True` fields are stored
    /// and it is rebuilt with `cls(**fields)`. Last registration wins, by class and by name.
    #[pyo3(signature = (cls, *, name, encode = None, decode = None))]
    fn register(
        &self,
        py: Python<'_>,
        cls: &Bound<'_, PyType>,
        name: &Bound<'_, PyString>,
        encode: Option<&Bound<'_, PyAny>>,
        decode: Option<&Bound<'_, PyAny>>,
    ) -> PyResult<()> {
        let name = name.to_str()?;
        if name.is_empty() {
            return Err(PyValueError::new_err("name must not be empty"));
        }
        if is_own_type(py, cls)? {
            return Err(PyValueError::new_err(format!(
                "{} is a type the codec stores itself; its wire form can't be changed",
                cls.name()?
            )));
        }
        let kind = match (encode, decode) {
            (Some(encode), Some(decode)) => {
                for (what, f) in [("encode", encode), ("decode", decode)] {
                    if !f.is_callable() {
                        return Err(PyTypeError::new_err(format!(
                            "{what} must be callable, not {}",
                            f.get_type().name()?
                        )));
                    }
                }
                Kind::Explicit { encode: encode.clone().unbind(), decode: decode.clone().unbind() }
            }
            (None, None) => Kind::Dataclass { fields: dataclass_fields(py, cls)? },
            _ => {
                return Err(PyTypeError::new_err(
                    "encode and decode go together: pass both, or neither for a dataclass",
                ));
            }
        };
        let entry = Arc::new(Entry {
            ty: cls.clone().unbind(),
            name: name.to_owned(),
            tag: format!("{TAG_PREFIX}{name}"),
            kind,
        });
        let addr = TypeAddr::of(cls);
        self.map.rcu(|old| {
            let mut map = (**old).clone();
            map.without(addr, name);
            map.by_type.insert(addr, Arc::clone(&entry));
            map.by_name.insert(name.to_owned(), Arc::clone(&entry));
            map
        });
        Ok(())
    }

    /// Removes `cls`'s registration. Returns whether there was one.
    fn unregister(&self, cls: &Bound<'_, PyType>) -> bool {
        let addr = TypeAddr::of(cls);
        let before = self.map.rcu(|old| {
            let mut map = (**old).clone();
            if let Some(old) = map.by_type.remove(&addr) {
                map.by_name.remove(&old.name);
            }
            map
        });
        before.by_type.contains_key(&addr)
    }

    /// The registrations, as `{name: cls}`.
    fn registered<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        let d = PyDict::new(py);
        let map = self.map.load();
        let mut entries: Vec<(&String, &Arc<Entry>)> = map.by_name.iter().collect();
        entries.sort_unstable_by_key(|(name, _)| *name);
        for (name, entry) in entries {
            d.set_item(name, entry.ty.bind(py))?;
        }
        Ok(d)
    }

    fn __len__(&self) -> usize {
        self.map.load().by_type.len()
    }

    fn __repr__(&self) -> String {
        let map = self.map.load();
        let mut names: Vec<&str> = map.by_name.keys().map(String::as_str).collect();
        names.sort_unstable();
        let quoted: Vec<String> = names.iter().map(|n| format!("'{n}'")).collect();
        format!("TypeRegistry([{}])", quoted.join(", "))
    }

    /// Lets the garbage collector see the types and functions held: a closure whose globals refer
    /// back to the registry, or to a cache holding it, would otherwise leak.
    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        let map = self.map.load();
        for entry in map.by_type.values() {
            visit.call(&entry.ty)?;
            if let Kind::Explicit { encode, decode } = &entry.kind {
                visit.call(encode)?;
                visit.call(decode)?;
            }
        }
        Ok(())
    }

    fn __clear__(&self) {
        self.map.store(Arc::new(RegistryMap::default()));
    }
}

static GLOBAL: PyOnceLock<Py<TypeRegistry>> = PyOnceLock::new();

/// The process-wide registry, `diskcache_oxide.registry`.
pub fn global(py: Python<'_>) -> PyResult<&'static Py<TypeRegistry>> {
    GLOBAL.get_or_try_init(py, || Py::new(py, TypeRegistry::default()))
}

/// Whether `cls` is one of the exact types the codec stores itself. Their wire form is a contract
/// across versions and processes, so they can't be registered; their subclasses can.
fn is_own_type(py: Python<'_>, cls: &Bound<'_, PyType>) -> PyResult<bool> {
    let t = common::types(py)?;
    let own = [
        py.get_type::<PyString>(),
        py.get_type::<PyBytes>(),
        py.get_type::<PyInt>(),
        py.get_type::<PyFloat>(),
        py.get_type::<PyBool>(),
        py.get_type::<PyNone>(),
        py.get_type::<PyList>(),
        py.get_type::<PyTuple>(),
        py.get_type::<PyDict>(),
        py.get_type::<PySet>(),
        py.get_type::<PyFrozenSet>(),
        t.datetime.bind(py).clone(),
        t.date.bind(py).clone(),
        t.decimal.bind(py).clone(),
    ];
    Ok(own.iter().any(|o| o.is(cls)))
}

/// The names of a dataclass's `init=True` fields, in order. `TypeError` for a class that isn't a
/// dataclass, or one with an `InitVar`: `fields()` omits those, so it would register fine and then
/// fail every decode.
fn dataclass_fields(py: Python<'_>, cls: &Bound<'_, PyType>) -> PyResult<Vec<Py<PyString>>> {
    let dataclasses = py.import("dataclasses")?;
    if !dataclasses.getattr("is_dataclass")?.call1((cls,))?.is_truthy()? {
        return Err(PyTypeError::new_err(format!(
            "{} is not a dataclass: pass encode and decode functions to register it",
            cls.name()?
        )));
    }
    // `_FIELD_INITVAR` and `Field._field_type` are CPython-private, stable since 3.7 and what
    // `fields()` itself checks; `test_a_dataclass_with_an_initvar_is_refused` is the tripwire.
    let initvar = dataclasses.getattr("_FIELD_INITVAR")?;
    let all = cls.getattr("__dataclass_fields__")?.cast_into::<PyDict>()?;
    for (fname, field) in all.iter() {
        if field.getattr(intern!(py, "_field_type"))?.is(&initvar) {
            return Err(PyTypeError::new_err(format!(
                "{} has an InitVar field {}, which the constructor needs but no instance holds: register it with explicit encode and decode functions",
                cls.name()?,
                fname.str()?
            )));
        }
    }
    let mut names = Vec::new();
    for field in dataclasses.getattr("fields")?.call1((cls,))?.try_iter()? {
        let field = field?;
        if field.getattr(intern!(py, "init"))?.is_truthy()? {
            let name = field.getattr(intern!(py, "name"))?.cast_into::<PyString>()?;
            names.push(PyString::intern(py, name.to_str()?).unbind());
        }
    }
    Ok(names)
}

/// The registrations a cache call sees: its handle's registry (if it has one), then the global
/// one, both as taken at the start of the call, so a registration mid-call can't change a row's
/// format halfway through. Also records whether the call reached a registered type (see
/// [`Snapshot::had_hit`]).
#[derive(Debug)]
pub struct Snapshot {
    local: Option<Guard<Arc<RegistryMap>>>,
    global: Guard<Arc<RegistryMap>>,
    hit: Cell<bool>,
}

impl Snapshot {
    pub fn take(py: Python<'_>, local: Option<&Py<TypeRegistry>>) -> PyResult<Self> {
        Ok(Self {
            local: local.map(|r| r.bind(py).get().map.load()),
            global: global(py)?.bind(py).get().map.load(),
            hit: Cell::new(false),
        })
    }

    /// Whether anything is registered at all: when nothing is, the codecs skip every lookup and
    /// keep their no-user-code paths.
    pub fn is_empty(&self) -> bool {
        self.local.as_ref().is_none_or(|m| m.by_type.is_empty()) && self.global.by_type.is_empty()
    }

    pub fn by_type(&self, ty: &Bound<'_, PyType>) -> Option<&Arc<Entry>> {
        if self.is_empty() {
            return None;
        }
        let addr = TypeAddr::of(ty);
        self.local.as_ref().and_then(|m| m.by_type.get(&addr)).or_else(|| self.global.by_type.get(&addr))
    }

    pub fn by_name(&self, name: &str) -> Option<&Arc<Entry>> {
        self.local.as_ref().and_then(|m| m.by_name.get(name)).or_else(|| self.global.by_name.get(name))
    }

    /// Called by the encoders when they encode through a registration. A value that reached one
    /// is never pickled afterwards, whatever fails later in it: the type was registered to stay off
    /// pickle, and the fallback would hide the bug.
    pub fn note_hit(&self) {
        self.hit.set(true);
    }

    pub fn had_hit(&self) -> bool {
        self.hit.get()
    }
}

/// Rebuilds a registered value from its decoded body: the `decode` function, or `cls(**body)` for
/// a dataclass. An error from either is classified as a pickle hook's would be
/// ([`super::passes_through`]): one that isn't about the row (`OSError`, `Timeout` included,
/// `RecursionError`, `MemoryError`, `UnsafeValueError`, anything that isn't an `Exception`) is
/// raised unchanged with the row kept; a `CorruptValueError` from a nested read is raised unchanged
/// too, and counts as this row's (the outer row is deleted under `'miss'`, as for a pickle hook);
/// everything else (a `TypeError` from an old-shape row, a `ValueError` from the function) becomes
/// `CorruptValueError` naming the tag, with the original as `__cause__`.
pub fn decode_body<'py>(py: Python<'py>, entry: &Entry, body: Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    let result = match &entry.kind {
        Kind::Explicit { decode, .. } => decode.bind(py).call1((body,)),
        Kind::Dataclass { .. } => match body.cast::<PyDict>() {
            Ok(kwargs) => entry.ty.bind(py).call((), Some(kwargs)),
            Err(_) => {
                return Err(CorruptValueError::new_err(format!(
                    "the body of tag {:?} should be a map of fields, not {}",
                    entry.tag,
                    body.get_type().name()?
                )));
            }
        },
    };
    result.map_err(|e| decode_error(py, e, &entry.tag))
}

fn decode_error(py: Python<'_>, e: PyErr, tag: &str) -> PyErr {
    if super::passes_through(py, &e) {
        return e;
    }
    let corrupt = CorruptValueError::new_err(format!("decoding the registered type {tag:?} failed: {e}"));
    corrupt.set_cause(py, Some(e));
    corrupt
}
