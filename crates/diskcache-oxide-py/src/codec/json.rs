//! Tagged JSON (mode 4) on `serde_json`. The encoder is one `Serialize` impl over Python objects; the
//! decoder is one `DeserializeSeed` that builds Python objects directly.

use std::cell::RefCell;
use std::fmt;
use std::io::Write;

use base64::Engine as _;
use base64::engine::general_purpose::STANDARD as B64;
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyBytes, PyDict, PyFloat, PyFrozenSet, PyInt, PyList, PySet, PyString, PyTuple};
use serde::de::{self, DeserializeSeed, IgnoredAny, MapAccess, SeqAccess, Visitor};
use serde::ser::{self, Serialize, SerializeMap, SerializeSeq, Serializer};

use super::common::{self, Tag, Types};

/// Encodes `obj` as tagged JSON into `out`. Errors keep the original Python exception (a
/// `TypeError` for anything that can't be represented).
pub fn encode<W: Write>(py: Python<'_>, obj: &Bound<'_, PyAny>, out: W) -> PyResult<()> {
    let ctx = Ctx { py, t: common::types(py)?, err: RefCell::new(None) };
    let enc = Enc { obj, ctx: &ctx, depth: 0 };
    enc.serialize(&mut serde_json::Serializer::new(out)).map_err(|e| ctx.take_err(&e))
}

/// Decodes one tagged JSON value. A Python error raised while building the value is returned as
/// itself; anything `serde_json` rejects comes back as its message (the caller makes both corrupt).
pub fn decode<'py>(py: Python<'py>, data: &[u8]) -> Result<Bound<'py, PyAny>, DecodeError> {
    let dctx = DeCtx { py, err: RefCell::new(None) };
    let mut de = serde_json::Deserializer::from_slice(data);
    let result = Any { cx: &dctx }.deserialize(&mut de).and_then(|v| de.end().map(|()| v));
    result.map_err(|e| match dctx.err.take() {
        Some(py_err) => DecodeError::Python(py_err),
        None => DecodeError::Invalid(e.to_string()),
    })
}

/// Why a JSON row couldn't be decoded.
#[derive(Debug)]
pub enum DecodeError {
    /// Python raised while building a value (a bad `$decimal`, an unknown zone key, ...).
    Python(PyErr),
    /// `serde_json`, or our tag rules, rejected the text.
    Invalid(String),
}

struct Ctx<'py> {
    py: Python<'py>,
    t: &'static Types,
    err: RefCell<Option<PyErr>>,
}

impl Ctx<'_> {
    fn fail<E: ser::Error>(&self, e: PyErr) -> E {
        self.err.replace(Some(e));
        E::custom("python error")
    }

    fn take_err(&self, fallback: &serde_json::Error) -> PyErr {
        self.err.take().unwrap_or_else(|| PyValueError::new_err(fallback.to_string()))
    }
}

struct Enc<'a, 'py> {
    obj: &'a Bound<'py, PyAny>,
    ctx: &'a Ctx<'py>,
    depth: u32,
}

impl<'py> Enc<'_, 'py> {
    fn child<'b>(&'b self, obj: &'b Bound<'py, PyAny>, extra: u32) -> Enc<'b, 'py> {
        Enc { obj, ctx: self.ctx, depth: self.depth + extra }
    }
}

/// `{name: value}`, the one-entry tag map.
fn tag<S: Serializer, V: Serialize + ?Sized>(s: S, name: &str, v: &V) -> Result<S::Ok, S::Error> {
    let mut m = s.serialize_map(Some(1))?;
    m.serialize_entry(name, v)?;
    m.end()
}

/// The elements of a list, tuple or set, as a JSON array.
enum Src<'a, 'py> {
    List(&'a Bound<'py, PyList>),
    Tuple(&'a Bound<'py, PyTuple>),
    /// The items of a set or frozenset (`common::set_items`).
    Set(Vec<Bound<'py, PyAny>>),
}

struct Items<'a, 'py> {
    parent: &'a Enc<'a, 'py>,
    src: Src<'a, 'py>,
}

impl Serialize for Items<'_, '_> {
    fn serialize<S: Serializer>(&self, s: S) -> Result<S::Ok, S::Error> {
        match &self.src {
            Src::List(l) => {
                // PyO3's list iterator re-checks the length on every step, so a concurrent resize
                // ends the array early instead of failing; serde_json ignores the length hint.
                let mut seq = s.serialize_seq(None)?;
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
            Src::Set(items) => {
                let mut seq = s.serialize_seq(Some(items.len()))?;
                for item in items {
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
    dict: &'a common::DictEntries<'py>,
}

impl Serialize for StrMap<'_, '_> {
    fn serialize<S: Serializer>(&self, s: S) -> Result<S::Ok, S::Error> {
        let ctx = self.parent.ctx;
        let mut m = s.serialize_map(Some(self.dict.len()))?;
        for (k, v) in self.dict.iter() {
            let k = k.cast_into_exact::<PyString>().map_err(|e| ctx.fail(e.into()))?;
            m.serialize_key(k.to_str().map_err(|e| ctx.fail(e))?)?;
            m.serialize_value(&self.parent.child(&v, 1))?;
        }
        m.end()
    }
}

/// A dict with non-str keys, as `[[k, v], ...]`.
struct Pairs<'a, 'py> {
    parent: &'a Enc<'a, 'py>,
    dict: &'a common::DictEntries<'py>,
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

impl<'py> Enc<'_, 'py> {
    fn serialize_dict<S: Serializer>(&self, s: S, d: &Bound<'py, PyDict>) -> Result<S::Ok, S::Error> {
        let ctx = self.ctx;
        let d = common::DictEntries::of(d);
        let mut all_str = true;
        let mut first_dollar = false;
        for (i, (k, _)) in d.iter().enumerate() {
            if let Ok(ks) = k.cast_exact::<PyString>() {
                if i == 0 {
                    first_dollar = ks.to_str().map_err(|e| ctx.fail(e))?.starts_with('$');
                }
            } else {
                all_str = false;
                break;
            }
        }
        if !all_str {
            return tag(s, common::TAG_DICT, &Pairs { parent: self, dict: &d });
        }
        if first_dollar {
            let inner = self.child(self.obj, 1);
            return tag(s, common::TAG_ESC, &StrMap { parent: &inner, dict: &d });
        }
        StrMap { parent: self, dict: &d }.serialize(s)
    }
}

impl Serialize for Enc<'_, '_> {
    fn serialize<S: Serializer>(&self, s: S) -> Result<S::Ok, S::Error> {
        let o = self.obj;
        let ctx = self.ctx;
        let py = ctx.py;
        if self.depth > common::MAX_DEPTH {
            return Err(ctx.fail(common::too_deep()));
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
            return tag(s, common::TAG_INT, text.to_str().map_err(|e| ctx.fail(e))?);
        }
        if let Ok(f) = o.cast_exact::<PyFloat>() {
            let f = f.value();
            if f.is_finite() {
                return s.serialize_f64(f);
            }
            return tag(s, common::TAG_FLOAT, common::nonfinite_text(f));
        }
        if let Ok(b) = o.cast_exact::<PyBool>() {
            return s.serialize_bool(b.is_true());
        }
        if o.is_none() {
            return s.serialize_unit();
        }
        if let Ok(d) = o.cast_exact::<PyDict>() {
            return self.serialize_dict(s, d);
        }
        if let Ok(l) = o.cast_exact::<PyList>() {
            return Items { parent: self, src: Src::List(l) }.serialize(s);
        }
        if let Ok(t) = o.cast_exact::<PyTuple>() {
            let inner = self.child(o, 1);
            return tag(s, common::TAG_TUPLE, &Items { parent: &inner, src: Src::Tuple(t) });
        }
        if let Ok(b) = o.cast_exact::<PyBytes>() {
            return tag(s, common::TAG_BYTES, &B64.encode(b.as_bytes()));
        }
        let ty = o.get_type();
        let t = ctx.t;
        if ty.is(t.datetime.bind(py)) {
            let text = common::datetime_text(o, t).map_err(|e| ctx.fail(e))?;
            return tag(s, common::TAG_DATETIME, &text);
        }
        if ty.is(t.date.bind(py)) {
            let text = o.call_method0("isoformat").and_then(|x| x.extract::<String>()).map_err(|e| ctx.fail(e))?;
            return tag(s, common::TAG_DATE, &text);
        }
        if ty.is(t.decimal.bind(py)) {
            let text = o.str().map_err(|e| ctx.fail(e))?;
            return tag(s, common::TAG_DECIMAL, text.to_str().map_err(|e| ctx.fail(e))?);
        }
        let set_tag = if o.is_exact_instance_of::<PySet>() {
            common::TAG_SET
        } else if o.is_exact_instance_of::<PyFrozenSet>() {
            common::TAG_FROZENSET
        } else {
            return Err(ctx.fail(common::unsupported(o)));
        };
        let items = common::set_items(o).map_err(|e| ctx.fail(e))?;
        let inner = self.child(o, 1);
        tag(s, set_tag, &Items { parent: &inner, src: Src::Set(items) })
    }
}

// ---------------- decoder ----------------

/// Decoder state shared by every seed: the interpreter token and the first Python error raised.
struct DeCtx<'py> {
    py: Python<'py>,
    err: RefCell<Option<PyErr>>,
}

impl DeCtx<'_> {
    fn fail<E: de::Error>(&self, e: PyErr) -> E {
        let msg = e.to_string();
        self.err.replace(Some(e));
        E::custom(msg)
    }
}

#[derive(Clone, Copy)]
struct Any<'a, 'py> {
    cx: &'a DeCtx<'py>,
}

impl<'de, 'py> DeserializeSeed<'de> for Any<'_, 'py> {
    type Value = Bound<'py, PyAny>;
    fn deserialize<D: de::Deserializer<'de>>(self, d: D) -> Result<Self::Value, D::Error> {
        d.deserialize_any(self)
    }
}

impl<'de, 'py> Visitor<'de> for Any<'_, 'py> {
    type Value = Bound<'py, PyAny>;

    fn expecting(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("a tagged JSON value")
    }
    fn visit_bool<E: de::Error>(self, v: bool) -> Result<Self::Value, E> {
        Ok(PyBool::new(self.cx.py, v).to_owned().into_any())
    }
    fn visit_i64<E: de::Error>(self, v: i64) -> Result<Self::Value, E> {
        let Ok(i) = v.into_pyobject(self.cx.py);
        Ok(i.into_any())
    }
    fn visit_u64<E: de::Error>(self, v: u64) -> Result<Self::Value, E> {
        let Ok(i) = v.into_pyobject(self.cx.py);
        Ok(i.into_any())
    }
    fn visit_f64<E: de::Error>(self, v: f64) -> Result<Self::Value, E> {
        Ok(PyFloat::new(self.cx.py, v).into_any())
    }
    fn visit_str<E: de::Error>(self, v: &str) -> Result<Self::Value, E> {
        Ok(PyString::new(self.cx.py, v).into_any())
    }
    fn visit_unit<E: de::Error>(self) -> Result<Self::Value, E> {
        Ok(self.cx.py.None().into_bound(self.cx.py))
    }
    fn visit_none<E: de::Error>(self) -> Result<Self::Value, E> {
        self.visit_unit()
    }
    fn visit_seq<A: SeqAccess<'de>>(self, seq: A) -> Result<Self::Value, A::Error> {
        let items = collect(self.cx, seq)?;
        Ok(PyList::new(self.cx.py, items).map_err(|e| self.cx.fail(e))?.into_any())
    }
    fn visit_map<A: MapAccess<'de>>(self, mut map: A) -> Result<Self::Value, A::Error> {
        let cx = self.cx;
        match map.next_key_seed(KeySeed { cx, first: true })? {
            None => Ok(PyDict::new(cx.py).into_any()),
            Some(Key::Tag(t)) => {
                let v = map.next_value_seed(TagSeed { cx, tag: t })?;
                if map.next_key::<IgnoredAny>()?.is_some() {
                    return Err(de::Error::custom("a tag map must have exactly one entry"));
                }
                Ok(v)
            }
            Some(Key::Str(k)) => {
                let d = PyDict::new(cx.py);
                let v = map.next_value_seed(self)?;
                d.set_item(k, v).map_err(|e| cx.fail(e))?;
                fill_map(cx, &d, map)?;
                Ok(d.into_any())
            }
        }
    }
}

fn collect<'de, 'py, A: SeqAccess<'de>>(cx: &DeCtx<'py>, mut seq: A) -> Result<Vec<Bound<'py, PyAny>>, A::Error> {
    let mut items = Vec::with_capacity(seq.size_hint().unwrap_or(4).min(4096));
    while let Some(v) = seq.next_element_seed(Any { cx })? {
        items.push(v);
    }
    Ok(items)
}

fn fill_map<'de, 'py, A: MapAccess<'de>>(cx: &DeCtx<'py>, d: &Bound<'py, PyDict>, mut map: A) -> Result<(), A::Error> {
    while let Some(k) = map.next_key_seed(KeySeed { cx, first: false })? {
        // KeySeed reads tags only for the first key, so this is always a plain key.
        let Key::Str(k) = k else { return Err(de::Error::custom("a tag key after the first entry")) };
        let v = map.next_value_seed(Any { cx })?;
        d.set_item(k, v).map_err(|e| cx.fail(e))?;
    }
    Ok(())
}

enum Key<'py> {
    Tag(Tag),
    Str(Bound<'py, PyString>),
}

#[derive(Clone, Copy)]
struct KeySeed<'a, 'py> {
    cx: &'a DeCtx<'py>,
    first: bool,
}

impl<'de, 'py> DeserializeSeed<'de> for KeySeed<'_, 'py> {
    type Value = Key<'py>;
    fn deserialize<D: de::Deserializer<'de>>(self, d: D) -> Result<Self::Value, D::Error> {
        d.deserialize_str(self)
    }
}

impl<'py> Visitor<'_> for KeySeed<'_, 'py> {
    type Value = Key<'py>;
    fn expecting(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("a string key")
    }
    fn visit_str<E: de::Error>(self, v: &str) -> Result<Self::Value, E> {
        if self.first && v.starts_with('$') {
            return Tag::parse(v).map(Key::Tag).ok_or_else(|| E::custom(format!("unknown tag {v:?}")));
        }
        Ok(Key::Str(common::key_str(self.cx.py, v)))
    }
}

struct TagSeed<'a, 'py> {
    cx: &'a DeCtx<'py>,
    tag: Tag,
}

impl<'de, 'py> DeserializeSeed<'de> for TagSeed<'_, 'py> {
    type Value = Bound<'py, PyAny>;
    fn deserialize<D: de::Deserializer<'de>>(self, d: D) -> Result<Self::Value, D::Error> {
        match self.tag {
            Tag::Tuple | Tag::Dict | Tag::Set | Tag::FrozenSet => d.deserialize_seq(self),
            Tag::Esc => d.deserialize_map(self),
            _ => d.deserialize_str(self),
        }
    }
}

impl<'de, 'py> Visitor<'de> for TagSeed<'_, 'py> {
    type Value = Bound<'py, PyAny>;
    fn expecting(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "the payload of tag {:?}", self.tag)
    }
    fn visit_str<E: de::Error>(self, v: &str) -> Result<Self::Value, E> {
        let cx = self.cx;
        let py = cx.py;
        let r = match self.tag {
            Tag::Bytes => {
                let raw = B64.decode(v).map_err(|e| E::custom(format!("bad $bytes payload: {e}")))?;
                Ok(PyBytes::new(py, &raw).into_any())
            }
            Tag::Float => common::float_from_text(py, v),
            Tag::Int => common::int_from_text(py, v),
            Tag::Datetime => common::datetime_from_text(py, v),
            Tag::Date => common::date_from_text(py, v),
            Tag::Decimal => common::decimal_from_text(py, v),
            _ => return Err(E::custom("unexpected string payload")),
        };
        r.map_err(|e| cx.fail(e))
    }
    fn visit_seq<A: SeqAccess<'de>>(self, mut seq: A) -> Result<Self::Value, A::Error> {
        let cx = self.cx;
        let py = cx.py;
        match self.tag {
            Tag::Tuple => common::tuple_from(py, collect(cx, seq)?).map_err(|e| cx.fail(e)),
            Tag::Set => Ok(PySet::new(py, collect(cx, seq)?).map_err(|e| cx.fail(e))?.into_any()),
            Tag::FrozenSet => Ok(PyFrozenSet::new(py, collect(cx, seq)?).map_err(|e| cx.fail(e))?.into_any()),
            Tag::Dict => {
                let d = PyDict::new(py);
                while let Some((k, v)) = seq.next_element_seed(PairSeed { cx })? {
                    d.set_item(k, v).map_err(|e| cx.fail(e))?;
                }
                Ok(d.into_any())
            }
            _ => Err(de::Error::custom("unexpected array payload")),
        }
    }
    fn visit_map<A: MapAccess<'de>>(self, map: A) -> Result<Self::Value, A::Error> {
        if self.tag != Tag::Esc {
            return Err(de::Error::custom("unexpected object payload"));
        }
        // $esc: a plain str-keyed dict whose first key is not a tag.
        let d = PyDict::new(self.cx.py);
        fill_map(self.cx, &d, map)?;
        Ok(d.into_any())
    }
}

struct PairSeed<'a, 'py> {
    cx: &'a DeCtx<'py>,
}

impl<'de, 'py> DeserializeSeed<'de> for PairSeed<'_, 'py> {
    type Value = (Bound<'py, PyAny>, Bound<'py, PyAny>);
    fn deserialize<D: de::Deserializer<'de>>(self, d: D) -> Result<Self::Value, D::Error> {
        d.deserialize_seq(self)
    }
}

impl<'de, 'py> Visitor<'de> for PairSeed<'_, 'py> {
    type Value = (Bound<'py, PyAny>, Bound<'py, PyAny>);
    fn expecting(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("a [key, value] pair")
    }
    fn visit_seq<A: SeqAccess<'de>>(self, mut seq: A) -> Result<Self::Value, A::Error> {
        let cx = self.cx;
        let k = seq.next_element_seed(Any { cx })?.ok_or_else(|| de::Error::custom("short pair"))?;
        let v = seq.next_element_seed(Any { cx })?.ok_or_else(|| de::Error::custom("short pair"))?;
        if seq.next_element::<IgnoredAny>()?.is_some() {
            return Err(de::Error::custom("long pair"));
        }
        Ok((k, v))
    }
}
