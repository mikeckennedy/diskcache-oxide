//! Shared pieces: cached Python types, the dict-key string cache, tag names, the snapshot helpers
//! that keep free-threaded encoding consistent, and the helpers both formats use for the types that
//! are encoded through their Python string form.

use std::cell::RefCell;

use pyo3::exceptions::{PyRuntimeError, PyTypeError, PyValueError};
use pyo3::intern;
use pyo3::prelude::*;
use pyo3::sync::PyOnceLock;
use pyo3::types::{PyDict, PyFloat, PyInt, PyString, PyTuple, PyType};

/// Nesting limit, counted in output containers (a tag wrapper counts as one). `serde_json`'s decoder
/// stops at 128, so the encoder refuses anything deeper rather than write a row it can't read back.
/// This bounds what a row can hold, not the stack it takes: that's [`check_stack`].
pub const MAX_DEPTH: u32 = 120;

/// `RecursionError` if this thread's C stack is nearly used up. The codecs call it once per container,
/// because a value within [`MAX_DEPTH`] can still overflow a small thread stack, which would kill the
/// process. Since 3.14 `CPython` compares the stack pointer with this thread's real limits here,
/// leaving a margin that a frame or two of ours fit in. That also makes the call its own check, with
/// nothing to keep entered: `Py_LeaveRecursiveCall` only pairs it up.
#[allow(unsafe_code)]
pub fn check_stack(py: Python<'_>) -> PyResult<()> {
    // SAFETY: `py` says this thread is attached, and the message is a static C string.
    let overflow = unsafe { pyo3::ffi::Py_EnterRecursiveCall(c" in a cached value".as_ptr()) } != 0;
    if overflow {
        return Err(PyErr::fetch(py));
    }
    // SAFETY: as above, and it matches the enter that just succeeded.
    unsafe { pyo3::ffi::Py_LeaveRecursiveCall() };
    Ok(())
}

// Tag names. A tag is a map with exactly one entry whose key is one of these. Any user dict whose
// first key starts with '$' is wrapped in TAG_ESC, so the whole '$' namespace is reserved.
pub const TAG_BYTES: &str = "$bytes";
pub const TAG_TUPLE: &str = "$tuple";
pub const TAG_DICT: &str = "$dict";
pub const TAG_FLOAT: &str = "$float";
pub const TAG_INT: &str = "$int";
pub const TAG_DATETIME: &str = "$datetime";
pub const TAG_DATE: &str = "$date";
pub const TAG_DECIMAL: &str = "$decimal";
pub const TAG_SET: &str = "$set";
pub const TAG_FROZENSET: &str = "$frozenset";
pub const TAG_ESC: &str = "$esc";

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Tag {
    Bytes,
    Tuple,
    Dict,
    Float,
    Int,
    Datetime,
    Date,
    Decimal,
    Set,
    FrozenSet,
    Esc,
}

impl Tag {
    pub fn parse(key: &str) -> Option<Self> {
        Some(match key {
            TAG_BYTES => Self::Bytes,
            TAG_TUPLE => Self::Tuple,
            TAG_DICT => Self::Dict,
            TAG_FLOAT => Self::Float,
            TAG_INT => Self::Int,
            TAG_DATETIME => Self::Datetime,
            TAG_DATE => Self::Date,
            TAG_DECIMAL => Self::Decimal,
            TAG_SET => Self::Set,
            TAG_FROZENSET => Self::FrozenSet,
            TAG_ESC => Self::Esc,
            _ => return None,
        })
    }
}

/// Python objects looked up once per interpreter.
#[derive(Debug)]
pub struct Types {
    pub datetime: Py<PyType>,
    pub date: Py<PyType>,
    pub timezone: Py<PyType>,
    pub utc: Py<PyAny>,
    pub zoneinfo: Py<PyType>,
    pub decimal: Py<PyType>,
    pub dt_fromisoformat: Py<PyAny>,
    pub date_fromisoformat: Py<PyAny>,
    pub pickle_dumps: Py<PyAny>,
    pub pickle_loads: Py<PyAny>,
}

static TYPES: PyOnceLock<Types> = PyOnceLock::new();

pub fn types(py: Python<'_>) -> PyResult<&'static Types> {
    TYPES.get_or_try_init(py, || {
        let dtm = py.import("datetime")?;
        let datetime = dtm.getattr("datetime")?.cast_into::<PyType>()?;
        let date = dtm.getattr("date")?.cast_into::<PyType>()?;
        let pickle = py.import("pickle")?;
        Ok(Types {
            dt_fromisoformat: datetime.getattr("fromisoformat")?.unbind(),
            date_fromisoformat: date.getattr("fromisoformat")?.unbind(),
            datetime: datetime.unbind(),
            date: date.unbind(),
            timezone: dtm.getattr("timezone")?.cast_into::<PyType>()?.unbind(),
            utc: dtm.getattr("timezone")?.getattr("utc")?.unbind(),
            zoneinfo: py.import("zoneinfo")?.getattr("ZoneInfo")?.cast_into::<PyType>()?.unbind(),
            decimal: py.import("decimal")?.getattr("Decimal")?.cast_into::<PyType>()?.unbind(),
            pickle_dumps: pickle.getattr("dumps")?.unbind(),
            pickle_loads: pickle.getattr("loads")?.unbind(),
        })
    })
}

// ---- free-threaded consistency ----

/// The entries of a dict, as the encoder iterates them.
///
/// PyO3's dict iterator panics if the dict changes size mid-iteration, and on the free-threaded
/// build another thread can do that between any two steps. There the entries are copied into a
/// Vec under one critical section (the iterator is created inside it, and nothing in the section
/// takes another lock, so it's never suspended): an atomic snapshot whose length matches what we
/// iterate, which msgpack needs because it writes the length first. With the GIL nothing else runs
/// while the encoder does, since it calls only C methods that never release it, so the dict is
/// iterated in place.
#[derive(Debug)]
pub struct DictEntries<'py> {
    #[cfg(Py_GIL_DISABLED)]
    items: Vec<(Bound<'py, PyAny>, Bound<'py, PyAny>)>,
    #[cfg(not(Py_GIL_DISABLED))]
    dict: Bound<'py, PyDict>,
}

impl<'py> DictEntries<'py> {
    #[cfg(Py_GIL_DISABLED)]
    pub fn of(d: &Bound<'py, PyDict>) -> Self {
        let items = pyo3::sync::critical_section::with_critical_section(d, || {
            d.iter().fold(Vec::with_capacity(d.len()), |mut v, kv| {
                v.push(kv);
                v
            })
        });
        Self { items }
    }

    #[cfg(not(Py_GIL_DISABLED))]
    pub fn of(d: &Bound<'py, PyDict>) -> Self {
        Self { dict: d.clone() }
    }

    pub fn len(&self) -> usize {
        #[cfg(Py_GIL_DISABLED)]
        return self.items.len();
        #[cfg(not(Py_GIL_DISABLED))]
        return self.dict.len();
    }

    #[cfg(Py_GIL_DISABLED)]
    pub fn iter(&self) -> impl Iterator<Item = (Bound<'py, PyAny>, Bound<'py, PyAny>)> + '_ {
        self.items.iter().cloned()
    }

    #[cfg(not(Py_GIL_DISABLED))]
    pub fn iter(&self) -> impl Iterator<Item = (Bound<'py, PyAny>, Bound<'py, PyAny>)> + '_ {
        self.dict.iter()
    }
}

/// The items of an exact set or frozenset, in iteration order.
///
/// On the free-threaded build a set is first copied with its own (C) `copy()`, which holds the
/// set's lock, so the items are an atomic snapshot. Iterating a shared set directly could see a
/// mix of states, and its iterator's `__length_hint__` can go negative under concurrent mutation,
/// so this loops without asking for a size hint.
pub fn set_items<'py>(o: &Bound<'py, PyAny>) -> PyResult<Vec<Bound<'py, PyAny>>> {
    #[cfg(Py_GIL_DISABLED)]
    let snapshot = if o.is_exact_instance_of::<pyo3::types::PySet>() {
        o.call_method0(intern!(o.py(), "copy"))?
    } else {
        o.clone()
    };
    #[cfg(Py_GIL_DISABLED)]
    let o = &snapshot;
    let mut items = Vec::with_capacity(o.len()?);
    for item in o.try_iter()? {
        items.push(item?);
    }
    Ok(items)
}

/// A list or set changed size while it was being encoded (another thread mutated it).
pub fn changed_during_encode(what: &str) -> PyErr {
    PyRuntimeError::new_err(format!("{what} changed size while it was being encoded"))
}

// ---- dict-key cache ----

const KEY_CACHE_SLOTS: usize = 1024;
const KEY_CACHE_MAX_LEN: usize = 64;

thread_local! {
    // Direct-mapped, per thread, so there's no lock on the free-threaded build.
    static KEYS: RefCell<Vec<Option<Py<PyString>>>> = RefCell::new((0..KEY_CACHE_SLOTS).map(|_| None).collect());
}

fn fnv1a(bytes: &[u8]) -> u64 {
    let mut h: u64 = 0xcbf2_9ce4_8422_2325;
    for &b in bytes {
        h ^= u64::from(b);
        h = h.wrapping_mul(0x0100_0000_01b3);
    }
    h
}

/// A Python str for a dict key, from the per-thread cache when the key is short. If the cache is
/// already borrowed (a re-entrant decode on this thread), it builds a fresh str instead.
pub fn key_str<'py>(py: Python<'py>, s: &str) -> Bound<'py, PyString> {
    if s.len() > KEY_CACHE_MAX_LEN {
        return PyString::new(py, s);
    }
    #[allow(clippy::cast_possible_truncation)]
    let slot = (fnv1a(s.as_bytes()) as usize) & (KEY_CACHE_SLOTS - 1);
    KEYS.with(|cell| {
        let Ok(mut slots) = cell.try_borrow_mut() else {
            return PyString::new(py, s);
        };
        if let Some(cached) = &slots[slot] {
            let b = cached.bind(py);
            if b.to_str().is_ok_and(|c| c == s) {
                return b.clone();
            }
        }
        let fresh = PyString::new(py, s);
        slots[slot] = Some(fresh.clone().unbind());
        fresh
    })
}

// ---- datetime / date / decimal through their string forms ----

/// The tag text for an exact `datetime`, or `TypeError` for one whose tzinfo can't round-trip.
///
/// Calls only C-implemented methods: the tzinfo's type is checked before any method on it runs, so
/// a custom tzinfo's Python code never executes.
pub fn datetime_text(o: &Bound<'_, PyAny>, t: &Types) -> PyResult<String> {
    let py = o.py();
    let tz = o.getattr(intern!(py, "tzinfo"))?;
    let fold: u8 = o.getattr(intern!(py, "fold"))?.extract()?;
    let fold_suffix = if fold == 0 { "" } else { FOLD_SUFFIX };
    let tz_type = tz.get_type();
    let utc_or_naive = tz.is_none() || tz.is(t.utc.bind(py));
    if utc_or_naive || tz_type.is(t.timezone.bind(py)) {
        if !utc_or_naive {
            // A timezone built with a custom name would come back unnamed.
            let offset = tz.call_method1(intern!(py, "utcoffset"), (py.None(),))?;
            let plain = t.timezone.bind(py).call1((offset,))?;
            let name = tz.call_method1(intern!(py, "tzname"), (py.None(),))?;
            if !name.eq(plain.call_method1(intern!(py, "tzname"), (py.None(),))?)? {
                return Err(PyTypeError::new_err("a datetime.timezone with a custom name can't round-trip"));
            }
        }
        let mut iso: String = o.call_method0(intern!(py, "isoformat"))?.extract()?;
        iso.push_str(fold_suffix);
        return Ok(iso);
    }
    if tz_type.is(t.zoneinfo.bind(py)) {
        let key = tz.getattr(intern!(py, "key"))?;
        if key.is_none() {
            return Err(PyTypeError::new_err("a ZoneInfo without a key (from_file) can't round-trip"));
        }
        let key: String = key.extract()?;
        let iso: String = o.call_method0(intern!(py, "isoformat"))?.extract()?;
        // RFC 9557 (IXDTF) zone suffix; the offset stays in the text as a check against tzdata drift.
        return Ok(format!("{iso}[{key}]{fold_suffix}"));
    }
    Err(PyTypeError::new_err(format!(
        "tzinfo of type {} can't round-trip; use datetime.timezone or zoneinfo.ZoneInfo",
        tz_type.name()?
    )))
}

/// An RFC 9557 experimental (underscore) suffix key: present only when `fold == 1`.
const FOLD_SUFFIX: &str = "[_fold=1]";

pub fn datetime_from_text<'py>(py: Python<'py>, s: &str) -> PyResult<Bound<'py, PyAny>> {
    let t = types(py)?;
    let (s, fold) = match s.strip_suffix(FOLD_SUFFIX) {
        Some(rest) => (rest, true),
        None => (s, false),
    };
    let zone_split = s.strip_suffix(']').and_then(|r| r.rfind('[').map(|i| (&r[..i], &r[i + 1..])));
    let (iso, zone) = match zone_split {
        Some((iso, key)) => (iso, Some(key)),
        None => (s, None),
    };
    let parsed = t.dt_fromisoformat.bind(py).call1((iso,))?;
    if !parsed.get_type().is(t.datetime.bind(py)) {
        return Err(PyValueError::new_err("$datetime payload is not a datetime"));
    }
    if zone.is_none() && !fold {
        return Ok(parsed);
    }
    let kwargs = PyDict::new(py);
    if fold {
        kwargs.set_item("fold", 1)?;
    }
    if let Some(key) = zone {
        // The wall time, zone and fold are authoritative. If tzdata changed since the write, the
        // offset differs from the stored one; the zone's current rules win.
        let zone = t.zoneinfo.bind(py).call1((key,)).map_err(|e| foreign_zone(py, e))?;
        kwargs.set_item("tzinfo", zone)?;
    }
    parsed.call_method("replace", (), Some(&kwargs))
}

/// A zone key this host's tzdata lacks is another host's data, not corruption: `ForeignValueError`,
/// with the original error as its cause. Any other error (a malformed key) is returned as itself.
fn foreign_zone(py: Python<'_>, e: PyErr) -> PyErr {
    let missing = py
        .import("zoneinfo")
        .and_then(|m| m.getattr("ZoneInfoNotFoundError"))
        .is_ok_and(|missing| e.matches(py, missing).unwrap_or(false));
    if !missing {
        return e;
    }
    let foreign = crate::ForeignValueError::new_err(format!("time zone not available on this host: {e}"));
    foreign.set_cause(py, Some(e));
    foreign
}

pub fn date_from_text<'py>(py: Python<'py>, s: &str) -> PyResult<Bound<'py, PyAny>> {
    types(py)?.date_fromisoformat.bind(py).call1((s,))
}

pub fn decimal_from_text<'py>(py: Python<'py>, s: &str) -> PyResult<Bound<'py, PyAny>> {
    types(py)?.decimal.bind(py).call1((s,))
}

pub fn float_from_text<'py>(py: Python<'py>, s: &str) -> PyResult<Bound<'py, PyAny>> {
    let f = match s {
        "nan" => f64::NAN,
        "inf" => f64::INFINITY,
        "-inf" => f64::NEG_INFINITY,
        _ => return Err(PyValueError::new_err(format!("bad $float payload {s:?}"))),
    };
    Ok(PyFloat::new(py, f).into_any())
}

pub fn nonfinite_text(f: f64) -> &'static str {
    if f.is_nan() {
        "nan"
    } else if f > 0.0 {
        "inf"
    } else {
        "-inf"
    }
}

/// A `$int` payload: decimal digits with an optional leading `-`, nothing else (no `_`, spaces or
/// `+`, which `int()` would accept).
pub fn int_from_text<'py>(py: Python<'py>, s: &str) -> PyResult<Bound<'py, PyAny>> {
    let digits = s.strip_prefix('-').unwrap_or(s);
    if digits.is_empty() || !digits.bytes().all(|b| b.is_ascii_digit()) {
        return Err(PyValueError::new_err(format!("bad $int payload {s:?}")));
    }
    // The digits are valid, so a ValueError here is this host's `sys.set_int_max_str_digits`
    // limit: another host may read it, so it's foreign, not corrupt.
    py.get_type::<PyInt>().call1((s,)).map_err(|e| {
        if !e.is_instance_of::<PyValueError>(py) {
            return e;
        }
        let foreign = crate::ForeignValueError::new_err(format!("integer too long for this host: {e}"));
        foreign.set_cause(py, Some(e));
        foreign
    })
}

pub fn unsupported(o: &Bound<'_, PyAny>) -> PyErr {
    let name = o.get_type().fully_qualified_name().map_or_else(|_| "?".to_owned(), |n| n.to_string());
    PyTypeError::new_err(format!(
        "values of type {name} can't be stored without pickle (only exact built-in types round-trip)"
    ))
}

pub fn unsupported_len() -> PyErr {
    PyTypeError::new_err("containers with 2**32 or more items can't be stored without pickle")
}

pub fn too_deep() -> PyErr {
    PyTypeError::new_err("value nested too deeply (or recursive) to store")
}

pub fn tuple_from<'py>(py: Python<'py>, items: Vec<Bound<'py, PyAny>>) -> PyResult<Bound<'py, PyAny>> {
    Ok(PyTuple::new(py, items)?.into_any())
}
