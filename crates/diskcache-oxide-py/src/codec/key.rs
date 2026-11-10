//! Python keys to and from the engine's keys (plan 24, release 1).
//!
//! A `str` is a `str` key, borrowed with no copy, as it always was. `bytes`, `int` (64-bit),
//! `float`, `bool`, `None` and tuples of those, nested, are typed keys, encoded by the core's
//! [`Encoder`] (the byte format lives in `diskcache_oxide_core::key`). Only exact types: a subclass
//! (`IntEnum`, `StrEnum`, a `namedtuple`), `bytearray`, `memoryview` and `list` raise `TypeError`,
//! as an int past 64 bits does. A NaN raises `ValueError`. Keys compare by exact type (D-044):
//! `1`, `1.0` and `True` are three keys.

use diskcache_oxide_core::key::{Encoder, Key, KeyError, OwnedKey, Token, TypedKey};
use pyo3::exceptions::{PyTypeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyBytes, PyFloat, PyInt, PyString, PyTuple};

use crate::CorruptKeyError;

/// A key from Python, ready for the engine.
#[derive(Debug)]
pub enum EncodedKey<'a> {
    Str(&'a str),
    Typed(TypedKey),
}

impl EncodedKey<'_> {
    #[must_use]
    pub fn key(&self) -> Key<'_> {
        match self {
            EncodedKey::Str(s) => Key::Str(s),
            EncodedKey::Typed(k) => Key::Typed(k),
        }
    }
}

const KEY_TYPES: &str = "str, bytes, int, float, bool, None, or a tuple of those";

fn wrong_type(obj: &Bound<'_, PyAny>) -> PyErr {
    let name = obj.get_type().name().map_or_else(|_| "?".to_owned(), |n| n.to_string());
    PyTypeError::new_err(format!("cache keys must be {KEY_TYPES}, not {name}"))
}

/// Encodes a Python key. `str` stays borrowed.
///
/// # Errors
///
/// `TypeError` for a type that can't be a key, `ValueError` for a NaN or a tuple nested past
/// `MAX_DEPTH`, `UnicodeEncodeError` for a lone surrogate.
pub fn encode_key<'a>(obj: &'a Bound<'_, PyAny>) -> PyResult<EncodedKey<'a>> {
    if let Ok(s) = obj.cast_exact::<PyString>() {
        return s.to_str().map(EncodedKey::Str);
    }
    let mut encoder = Encoder::new();
    element(&mut encoder, obj, 0)?;
    encoder.finish().map(EncodedKey::Typed).map_err(|e| key_error(&e))
}

fn key_error(e: &KeyError) -> PyErr {
    match e {
        KeyError::NaN | KeyError::TooDeep => PyValueError::new_err(e.to_string()),
        other => PyTypeError::new_err(other.to_string()),
    }
}

fn element(encoder: &mut Encoder, obj: &Bound<'_, PyAny>, depth: usize) -> PyResult<()> {
    if obj.is_none() {
        encoder.none();
    } else if let Ok(b) = obj.cast_exact::<PyBool>() {
        // Before int: a bool is an int to Python, but a key of its own here.
        encoder.bool(b.is_true());
    } else if let Ok(n) = obj.cast_exact::<PyInt>() {
        let n: i64 = n.extract().map_err(|_| PyTypeError::new_err("int keys must fit in 64 bits"))?;
        encoder.int(n);
    } else if let Ok(x) = obj.cast_exact::<PyFloat>() {
        encoder.float(x.value());
    } else if let Ok(b) = obj.cast_exact::<PyBytes>() {
        encoder.bytes(b.as_bytes());
    } else if let Ok(s) = obj.cast_exact::<PyString>() {
        // Only reached inside a tuple: a lone str is a str key.
        encoder.str(s.to_str()?);
    } else if let Ok(t) = obj.cast_exact::<PyTuple>() {
        if depth == diskcache_oxide_core::key::MAX_DEPTH {
            return Err(key_error(&KeyError::TooDeep));
        }
        encoder.begin_tuple();
        for item in t.iter() {
            element(encoder, &item, depth + 1)?;
        }
        encoder.end_tuple();
    } else {
        return Err(wrong_type(obj));
    }
    Ok(())
}

/// A stored key back as the Python object it was made from, with its exact types.
///
/// # Errors
///
/// `CorruptKeyError` if a typed key's bytes were damaged (a `TypedKey` is checked when read, so
/// this can't happen for one that came from the engine).
pub fn decode_key(py: Python<'_>, key: Key<'_>) -> PyResult<Py<PyAny>> {
    let typed = match key {
        Key::Str(s) => return Ok(PyString::new(py, s).into_any().unbind()),
        Key::Typed(k) => k,
    };
    let mut stack: Vec<Vec<Py<PyAny>>> = vec![Vec::new()];
    for token in typed.tokens() {
        let token = token.map_err(|e| CorruptKeyError::new_err(format!("a stored key is damaged: {e}")))?;
        let value: Py<PyAny> = match token {
            Token::None => py.None(),
            Token::Bool(b) => PyBool::new(py, b).to_owned().into_any().unbind(),
            Token::Int(n) => n.into_pyobject(py)?.into_any().unbind(),
            Token::Float(x) => PyFloat::new(py, x).into_any().unbind(),
            Token::Bytes(b) => PyBytes::new(py, &b).into_any().unbind(),
            Token::Str(s) => PyString::new(py, &s).into_any().unbind(),
            Token::BeginTuple => {
                stack.push(Vec::new());
                continue;
            }
            Token::EndTuple => {
                let items = stack.pop().unwrap_or_default();
                PyTuple::new(py, items)?.into_any().unbind()
            }
        };
        if let Some(top) = stack.last_mut() {
            top.push(value);
        }
    }
    stack
        .pop()
        .and_then(|mut top| top.pop())
        .ok_or_else(|| CorruptKeyError::new_err("a stored key is damaged: no value"))
}

/// [`decode_key`] for an owned key from an iteration.
///
/// # Errors
///
/// As [`decode_key`].
pub fn decode_owned(py: Python<'_>, key: &OwnedKey) -> PyResult<Py<PyAny>> {
    decode_key(py, key.as_key())
}
