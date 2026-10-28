//! Tagged JSON. The encoder is one `Serialize` impl over Python objects, so serde_json and sonic-rs
//! both drive it; the decoder is one `DeserializeSeed` that builds Python objects directly.

use std::cell::{Cell, RefCell};
use std::fmt;

use base64::Engine as _;
use base64::engine::general_purpose::STANDARD as B64;
use pyo3::exceptions::{PyTypeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyBytes, PyDict, PyFloat, PyFrozenSet, PyInt, PyList, PySet, PyString, PyTuple};
use serde::de::{self, DeserializeSeed, IgnoredAny, MapAccess, SeqAccess, Visitor};
use serde::ser::{self, Serialize, SerializeMap, SerializeSeq, Serializer};

use crate::common::{self, Tag, Types};

pub struct Ctx<'py> {
    py: Python<'py>,
    t: &'static Types,
    err: RefCell<Option<PyErr>>,
    pub tagged: Cell<bool>,
}

impl<'py> Ctx<'py> {
    pub fn new(py: Python<'py>) -> PyResult<Self> {
        Ok(Self { py, t: common::types(py)?, err: RefCell::new(None), tagged: Cell::new(false) })
    }

    fn fail<E: ser::Error>(&self, e: PyErr) -> E {
        self.err.replace(Some(e));
        E::custom("python error")
    }

    pub fn take_err(&self, fallback: impl fmt::Display) -> PyErr {
        self.err.take().unwrap_or_else(|| PyValueError::new_err(fallback.to_string()))
    }
}

pub struct Enc<'a, 'py> {
    pub obj: &'a Bound<'py, PyAny>,
    pub ctx: &'a Ctx<'py>,
    pub depth: u32,
}

impl<'a, 'py> Enc<'a, 'py> {
    fn child<'b>(&'b self, obj: &'b Bound<'py, PyAny>, extra: u32) -> Enc<'b, 'py> {
        Enc { obj, ctx: self.ctx, depth: self.depth + extra }
    }
}

/// `{name: value}`, the one-entry tag map.
fn tag<S: Serializer, V: Serialize + ?Sized>(s: S, ctx: &Ctx<'_>, name: &str, v: &V) -> Result<S::Ok, S::Error> {
    ctx.tagged.set(true);
    let mut m = s.serialize_map(Some(1))?;
    m.serialize_entry(name, v)?;
    m.end()
}

/// Serializes the elements of any Python iterable as a JSON array.
enum Src<'a, 'py> {
    List(&'a Bound<'py, PyList>),
    Tuple(&'a Bound<'py, PyTuple>),
    Vec(Vec<Bound<'py, PyAny>>),
}

struct Items<'a, 'py> {
    parent: &'a Enc<'a, 'py>,
    src: Src<'a, 'py>,
}

impl Serialize for Items<'_, '_> {
    fn serialize<S: Serializer>(&self, s: S) -> Result<S::Ok, S::Error> {
        match &self.src {
            Src::List(l) => {
                let mut seq = s.serialize_seq(Some(l.len()))?;
                for item in l.iter() {
                    seq.serialize_element(&self.parent.child(&item, 1))?;
                }
                seq.end()
            }
            Src::Tuple(t) => {
                let mut seq = s.serialize_seq(Some(t.len()))?;
                for item in t.iter() {
                    seq.serialize_element(&self.parent.child(&item, 1))?;
                }
                seq.end()
            }
            Src::Vec(v) => {
                let mut seq = s.serialize_seq(Some(v.len()))?;
                for item in v {
                    seq.serialize_element(&self.parent.child(item, 1))?;
                }
                seq.end()
            }
        }
    }
}

/// A str-keyed dict as a JSON object (the caller has checked every key is an exact str).
struct StrMap<'a, 'py> {
    parent: &'a Enc<'a, 'py>,
    dict: &'a Bound<'py, PyDict>,
}

impl Serialize for StrMap<'_, '_> {
    fn serialize<S: Serializer>(&self, s: S) -> Result<S::Ok, S::Error> {
        let ctx = self.parent.ctx;
        let mut m = s.serialize_map(Some(self.dict.len()))?;
        for (k, v) in self.dict.iter() {
            let k = k.cast_into::<PyString>().map_err(|e| ctx.fail(e.into()))?;
            m.serialize_key(k.to_str().map_err(|e| ctx.fail(e))?)?;
            m.serialize_value(&self.parent.child(&v, 1))?;
        }
        m.end()
    }
}

/// A dict with non-str keys, as `[[k, v], ...]`.
struct Pairs<'a, 'py> {
    parent: &'a Enc<'a, 'py>,
    dict: &'a Bound<'py, PyDict>,
}

impl Serialize for Pairs<'_, '_> {
    fn serialize<S: Serializer>(&self, s: S) -> Result<S::Ok, S::Error> {
        let mut seq = s.serialize_seq(Some(self.dict.len()))?;
        for (k, v) in self.dict.iter() {
            seq.serialize_element(&(self.parent.child(&k, 2), self.parent.child(&v, 2)))?;
        }
        seq.end()
    }
}

impl Serialize for Enc<'_, '_> {
    #[allow(clippy::too_many_lines)]
    fn serialize<S: Serializer>(&self, s: S) -> Result<S::Ok, S::Error> {
        let o = self.obj;
        let ctx = self.ctx;
        let py = ctx.py;
        if self.depth > common::MAX_DEPTH {
            return Err(ctx.fail(PyTypeError::new_err("value nested too deeply (or recursive) to store")));
        }
        if let Ok(st) = o.cast_exact::<PyString>() {
            return s.serialize_str(st.to_str().map_err(|e| ctx.fail(e))?);
        }
        if o.is_exact_instance_of::<PyInt>() {
            if let Ok(i) = o.extract::<i64>() {
                return s.serialize_i64(i);
            }
            if let Ok(u) = o.extract::<u64>() {
                return s.serialize_u64(u);
            }
            let text = o.str().map_err(|e| ctx.fail(e))?;
            return tag(s, ctx, common::TAG_INT, text.to_str().map_err(|e| ctx.fail(e))?);
        }
        if let Ok(f) = o.cast_exact::<PyFloat>() {
            let f = f.value();
            if f.is_finite() {
                return s.serialize_f64(f);
            }
            return tag(s, ctx, common::TAG_FLOAT, common::nonfinite_text(f));
        }
        if let Ok(b) = o.cast_exact::<PyBool>() {
            return s.serialize_bool(b.is_true());
        }
        if o.is_none() {
            return s.serialize_unit();
        }
        if let Ok(d) = o.cast_exact::<PyDict>() {
            let mut all_str = true;
            let mut first_dollar = false;
            for (i, k) in d.keys().iter().enumerate() {
                match k.cast_exact::<PyString>() {
                    Ok(ks) => {
                        if i == 0 {
                            first_dollar = ks.to_str().map_err(|e| ctx.fail(e))?.starts_with('$');
                        }
                    }
                    Err(_) => {
                        all_str = false;
                        break;
                    }
                }
            }
            if !all_str {
                return tag(s, ctx, common::TAG_DICT, &Pairs { parent: self, dict: d });
            }
            if first_dollar {
                let inner = Enc { obj: o, ctx, depth: self.depth + 1 };
                return tag(s, ctx, common::TAG_ESC, &StrMap { parent: &inner, dict: d });
            }
            return StrMap { parent: self, dict: d }.serialize(s);
        }
        if let Ok(l) = o.cast_exact::<PyList>() {
            return Items { parent: self, src: Src::List(l) }.serialize(s);
        }
        if let Ok(t) = o.cast_exact::<PyTuple>() {
            let inner = Enc { obj: o, ctx, depth: self.depth + 1 };
            return tag(s, ctx, common::TAG_TUPLE, &Items { parent: &inner, src: Src::Tuple(t) });
        }
        if let Ok(b) = o.cast_exact::<PyBytes>() {
            return tag(s, ctx, common::TAG_BYTES, &B64.encode(b.as_bytes()));
        }
        let ty = o.get_type();
        let t = ctx.t;
        if ty.is(t.datetime.bind(py)) {
            let text = common::datetime_text(o, t).map_err(|e| ctx.fail(e))?;
            return tag(s, ctx, common::TAG_DATETIME, &text);
        }
        if ty.is(t.date.bind(py)) {
            let text = o.call_method0("isoformat").and_then(|x| x.extract::<String>()).map_err(|e| ctx.fail(e))?;
            return tag(s, ctx, common::TAG_DATE, &text);
        }
        if ty.is(t.decimal.bind(py)) {
            let text = o.str().map_err(|e| ctx.fail(e))?;
            return tag(s, ctx, common::TAG_DECIMAL, text.to_str().map_err(|e| ctx.fail(e))?);
        }
        if let Ok(st) = o.cast_exact::<PySet>() {
            let inner = Enc { obj: o, ctx, depth: self.depth + 1 };
            let items: Vec<_> = st.iter().collect();
            return tag(s, ctx, common::TAG_SET, &Items { parent: &inner, src: Src::Vec(items) });
        }
        if let Ok(st) = o.cast_exact::<PyFrozenSet>() {
            let inner = Enc { obj: o, ctx, depth: self.depth + 1 };
            let items: Vec<_> = st.iter().collect();
            return tag(s, ctx, common::TAG_FROZENSET, &Items { parent: &inner, src: Src::Vec(items) });
        }
        Err(ctx.fail(common::unsupported(o)))
    }
}

// ---------------- decoder ----------------

fn de_err<E: de::Error>(e: PyErr) -> E {
    E::custom(e)
}

#[derive(Clone, Copy)]
pub struct Any<'py> {
    pub py: Python<'py>,
}

impl<'de, 'py> DeserializeSeed<'de> for Any<'py> {
    type Value = Bound<'py, PyAny>;
    fn deserialize<D: de::Deserializer<'de>>(self, d: D) -> Result<Self::Value, D::Error> {
        d.deserialize_any(self)
    }
}

impl<'de, 'py> Visitor<'de> for Any<'py> {
    type Value = Bound<'py, PyAny>;

    fn expecting(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("a tagged JSON value")
    }
    fn visit_bool<E: de::Error>(self, v: bool) -> Result<Self::Value, E> {
        Ok(PyBool::new(self.py, v).to_owned().into_any())
    }
    fn visit_i64<E: de::Error>(self, v: i64) -> Result<Self::Value, E> {
        let Ok(i) = v.into_pyobject(self.py);
        Ok(i.into_any())
    }
    fn visit_u64<E: de::Error>(self, v: u64) -> Result<Self::Value, E> {
        let Ok(i) = v.into_pyobject(self.py);
        Ok(i.into_any())
    }
    fn visit_f64<E: de::Error>(self, v: f64) -> Result<Self::Value, E> {
        Ok(PyFloat::new(self.py, v).into_any())
    }
    fn visit_str<E: de::Error>(self, v: &str) -> Result<Self::Value, E> {
        Ok(PyString::new(self.py, v).into_any())
    }
    fn visit_unit<E: de::Error>(self) -> Result<Self::Value, E> {
        Ok(self.py.None().into_bound(self.py))
    }
    fn visit_none<E: de::Error>(self) -> Result<Self::Value, E> {
        self.visit_unit()
    }
    fn visit_seq<A: SeqAccess<'de>>(self, seq: A) -> Result<Self::Value, A::Error> {
        let items = collect(self.py, seq)?;
        Ok(PyList::new(self.py, items).map_err(de_err)?.into_any())
    }
    fn visit_map<A: MapAccess<'de>>(self, mut map: A) -> Result<Self::Value, A::Error> {
        let py = self.py;
        match map.next_key_seed(KeySeed { py, first: true })? {
            None => Ok(PyDict::new(py).into_any()),
            Some(Key::Tag(t)) => {
                let v = map.next_value_seed(TagSeed { py, tag: t })?;
                if map.next_key::<IgnoredAny>()?.is_some() {
                    return Err(de::Error::custom("a tag map must have exactly one entry"));
                }
                Ok(v)
            }
            Some(Key::Str(k)) => {
                let d = PyDict::new(py);
                let v = map.next_value_seed(self)?;
                d.set_item(k, v).map_err(de_err)?;
                fill_map(py, &d, map)?;
                Ok(d.into_any())
            }
        }
    }
}

fn collect<'de, 'py, A: SeqAccess<'de>>(py: Python<'py>, mut seq: A) -> Result<Vec<Bound<'py, PyAny>>, A::Error> {
    let mut items = Vec::with_capacity(seq.size_hint().unwrap_or(4).min(4096));
    while let Some(v) = seq.next_element_seed(Any { py })? {
        items.push(v);
    }
    Ok(items)
}

fn fill_map<'de, 'py, A: MapAccess<'de>>(py: Python<'py>, d: &Bound<'py, PyDict>, mut map: A) -> Result<(), A::Error> {
    while let Some(Key::Str(k)) = map.next_key_seed(KeySeed { py, first: false })? {
        let v = map.next_value_seed(Any { py })?;
        d.set_item(k, v).map_err(de_err)?;
    }
    Ok(())
}

enum Key<'py> {
    Tag(Tag),
    Str(Bound<'py, PyString>),
}

#[derive(Clone, Copy)]
struct KeySeed<'py> {
    py: Python<'py>,
    first: bool,
}

impl<'de, 'py> DeserializeSeed<'de> for KeySeed<'py> {
    type Value = Key<'py>;
    fn deserialize<D: de::Deserializer<'de>>(self, d: D) -> Result<Self::Value, D::Error> {
        d.deserialize_str(self)
    }
}

impl<'de, 'py> Visitor<'de> for KeySeed<'py> {
    type Value = Key<'py>;
    fn expecting(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("a string key")
    }
    fn visit_str<E: de::Error>(self, v: &str) -> Result<Self::Value, E> {
        if self.first && v.starts_with('$') {
            return Tag::parse(v).map(Key::Tag).ok_or_else(|| E::custom(format!("unknown tag {v:?}")));
        }
        Ok(Key::Str(common::key_str(self.py, v)))
    }
}

struct TagSeed<'py> {
    py: Python<'py>,
    tag: Tag,
}

impl<'de, 'py> DeserializeSeed<'de> for TagSeed<'py> {
    type Value = Bound<'py, PyAny>;
    fn deserialize<D: de::Deserializer<'de>>(self, d: D) -> Result<Self::Value, D::Error> {
        match self.tag {
            Tag::Tuple | Tag::Dict | Tag::Set | Tag::FrozenSet => d.deserialize_seq(self),
            Tag::Esc => d.deserialize_map(self),
            _ => d.deserialize_str(self),
        }
    }
}

impl<'de, 'py> Visitor<'de> for TagSeed<'py> {
    type Value = Bound<'py, PyAny>;
    fn expecting(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "the payload of tag {:?}", self.tag)
    }
    fn visit_str<E: de::Error>(self, v: &str) -> Result<Self::Value, E> {
        let py = self.py;
        match self.tag {
            Tag::Bytes => {
                let s = v.as_bytes();
                let pad = s.iter().rev().take(2).filter(|&&c| c == b'=').count();
                let n = (s.len() / 4 * 3).checked_sub(pad).ok_or_else(|| E::custom("bad base64"))?;
                let out = PyBytes::new_with(py, n, |buf| {
                    B64.decode_slice_unchecked(s, buf).map(|_| ()).map_err(|e| PyValueError::new_err(e.to_string()))
                })
                .map_err(de_err)?;
                Ok(out.into_any())
            }
            Tag::Float => common::float_from_text(py, v).map_err(de_err),
            Tag::Int => common::int_from_text(py, v).map_err(de_err),
            Tag::Datetime => common::datetime_from_text(py, v).map_err(de_err),
            Tag::Date => common::date_from_text(py, v).map_err(de_err),
            Tag::Decimal => common::decimal_from_text(py, v).map_err(de_err),
            _ => Err(E::custom("unexpected string payload")),
        }
    }
    fn visit_seq<A: SeqAccess<'de>>(self, mut seq: A) -> Result<Self::Value, A::Error> {
        let py = self.py;
        match self.tag {
            Tag::Tuple => common::tuple_from(py, collect(py, seq)?).map_err(de_err),
            Tag::Set => Ok(PySet::new(py, collect(py, seq)?).map_err(de_err)?.into_any()),
            Tag::FrozenSet => Ok(PyFrozenSet::new(py, collect(py, seq)?).map_err(de_err)?.into_any()),
            Tag::Dict => {
                let d = PyDict::new(py);
                while let Some((k, v)) = seq.next_element_seed(PairSeed { py })? {
                    d.set_item(k, v).map_err(de_err)?;
                }
                Ok(d.into_any())
            }
            _ => Err(de::Error::custom("unexpected array payload")),
        }
    }
    fn visit_map<A: MapAccess<'de>>(self, map: A) -> Result<Self::Value, A::Error> {
        // $esc: a plain str-keyed dict whose first key is not a tag.
        let d = PyDict::new(self.py);
        let mut map = map;
        if let Some(Key::Str(k)) = map.next_key_seed(KeySeed { py: self.py, first: false })? {
            let v = map.next_value_seed(Any { py: self.py })?;
            d.set_item(k, v).map_err(de_err)?;
            fill_map(self.py, &d, map)?;
        }
        Ok(d.into_any())
    }
}

struct PairSeed<'py> {
    py: Python<'py>,
}

impl<'de, 'py> DeserializeSeed<'de> for PairSeed<'py> {
    type Value = (Bound<'py, PyAny>, Bound<'py, PyAny>);
    fn deserialize<D: de::Deserializer<'de>>(self, d: D) -> Result<Self::Value, D::Error> {
        d.deserialize_seq(self)
    }
}

impl<'de, 'py> Visitor<'de> for PairSeed<'py> {
    type Value = (Bound<'py, PyAny>, Bound<'py, PyAny>);
    fn expecting(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("a [key, value] pair")
    }
    fn visit_seq<A: SeqAccess<'de>>(self, mut seq: A) -> Result<Self::Value, A::Error> {
        let k = seq.next_element_seed(Any { py: self.py })?.ok_or_else(|| de::Error::custom("short pair"))?;
        let v = seq.next_element_seed(Any { py: self.py })?.ok_or_else(|| de::Error::custom("short pair"))?;
        if seq.next_element::<IgnoredAny>()?.is_some() {
            return Err(de::Error::custom("long pair"));
        }
        Ok((k, v))
    }
}
