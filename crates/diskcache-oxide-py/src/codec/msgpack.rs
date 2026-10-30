//! Tagged msgpack (mode 3), written with `rmp`'s encode functions and read by a hand-written slice
//! decoder. bytes, non-str keys and NaN/inf are native. A UTC datetime uses the standard timestamp
//! extension (-1); everything else that needs a tag uses the same one-entry tag maps as JSON.

use std::io::Write;

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{
    PyBool, PyBytes, PyDateAccess, PyDateTime, PyDict, PyFloat, PyFrozenSet, PyInt, PyList, PySet, PyString,
    PyTimeAccess, PyTuple, PyTzInfo, PyTzInfoAccess,
};
use rmp::encode as w;

use super::common::{self, Tag, Types};
use crate::{CorruptValueError, ForeignValueError};

fn io(e: impl std::fmt::Display) -> PyErr {
    PyValueError::new_err(e.to_string())
}

fn len32(n: usize) -> PyResult<u32> {
    u32::try_from(n).map_err(|_| common::unsupported_len())
}

fn tag_head<W: Write>(out: &mut W, name: &str) -> PyResult<()> {
    w::write_map_len(out, 1).map_err(io)?;
    w::write_str(out, name).map_err(io)
}

/// Encodes `o` as tagged msgpack into `out`. Errors keep the original Python exception (a
/// `TypeError` for anything that can't be represented).
pub fn encode<W: Write>(o: &Bound<'_, PyAny>, t: &Types, depth: u32, out: &mut W) -> PyResult<()> {
    let py = o.py();
    if depth > common::MAX_DEPTH {
        return Err(common::too_deep());
    }
    if let Ok(s) = o.cast_exact::<PyString>() {
        return w::write_str(out, s.to_str()?).map_err(io);
    }
    if o.is_exact_instance_of::<PyInt>() {
        if let Ok(i) = o.extract::<i64>() {
            return w::write_sint(out, i).map(|_| ()).map_err(io);
        }
        if let Ok(u) = o.extract::<u64>() {
            return w::write_uint(out, u).map(|_| ()).map_err(io);
        }
        tag_head(out, common::TAG_INT)?;
        return w::write_str(out, o.str()?.to_str()?).map_err(io);
    }
    if let Ok(f) = o.cast_exact::<PyFloat>() {
        return w::write_f64(out, f.value()).map_err(io);
    }
    if let Ok(b) = o.cast_exact::<PyBool>() {
        return w::write_bool(out, b.is_true()).map_err(io);
    }
    if o.is_none() {
        return w::write_nil(out).map_err(io);
    }
    if let Ok(d) = o.cast_exact::<PyDict>() {
        // A snapshot on the free-threaded build, so the length written first matches the entries.
        let d = common::DictEntries::of(d);
        let first_dollar = d.iter().next().is_some_and(|(k, _)| {
            k.cast_exact::<PyString>().is_ok_and(|s| s.to_str().is_ok_and(|s| s.starts_with('$')))
        });
        let mut depth = depth + 1;
        if first_dollar {
            tag_head(out, common::TAG_ESC)?;
            depth += 1;
        }
        w::write_map_len(out, len32(d.len())?).map_err(io)?;
        for (k, v) in d.iter() {
            encode(&k, t, depth, out)?;
            encode(&v, t, depth, out)?;
        }
        return Ok(());
    }
    if let Ok(l) = o.cast_exact::<PyList>() {
        // By index: if another thread shrinks the list after the length is written, fail rather
        // than write fewer items than the header promises. Growth just isn't seen.
        let n = l.len();
        w::write_array_len(out, len32(n)?).map_err(io)?;
        for i in 0..n {
            let item = l.get_item(i).map_err(|_| common::changed_during_encode("list"))?;
            encode(&item, t, depth + 1, out)?;
        }
        return Ok(());
    }
    if let Ok(tp) = o.cast_exact::<PyTuple>() {
        tag_head(out, common::TAG_TUPLE)?;
        w::write_array_len(out, len32(tp.len())?).map_err(io)?;
        for item in tp.iter() {
            encode(&item, t, depth + 2, out)?;
        }
        return Ok(());
    }
    if let Ok(b) = o.cast_exact::<PyBytes>() {
        return w::write_bin(out, b.as_bytes()).map_err(io);
    }
    let ty = o.get_type();
    if ty.is(t.datetime.bind(py)) {
        let dt = o.cast::<PyDateTime>()?;
        if !dt.get_fold() && dt.get_tzinfo().is_some_and(|tz| tz.is(t.utc.bind(py))) {
            return write_timestamp(out, dt);
        }
        let text = common::datetime_text(o, t)?;
        tag_head(out, common::TAG_DATETIME)?;
        return w::write_str(out, &text).map_err(io);
    }
    if ty.is(t.date.bind(py)) {
        tag_head(out, common::TAG_DATE)?;
        return w::write_str(out, &o.call_method0("isoformat")?.extract::<String>()?).map_err(io);
    }
    if ty.is(t.decimal.bind(py)) {
        tag_head(out, common::TAG_DECIMAL)?;
        return w::write_str(out, o.str()?.to_str()?).map_err(io);
    }
    let name = if o.is_exact_instance_of::<PySet>() {
        common::TAG_SET
    } else if o.is_exact_instance_of::<PyFrozenSet>() {
        common::TAG_FROZENSET
    } else {
        return Err(common::unsupported(o));
    };
    // Collected first because the length goes before the items.
    let items = common::set_items(o)?;
    tag_head(out, name)?;
    w::write_array_len(out, len32(items.len())?).map_err(io)?;
    for item in &items {
        encode(item, t, depth + 2, out)?;
    }
    Ok(())
}

// Howard Hinnant's days_from_civil / civil_from_days.
fn days_from_civil(y: i64, m: i64, d: i64) -> i64 {
    let y = if m <= 2 { y - 1 } else { y };
    let era = y.div_euclid(400);
    let yoe = y - era * 400;
    let mp = (m + 9) % 12;
    let doy = (153 * mp + 2) / 5 + d - 1;
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    era * 146_097 + doe - 719_468
}

fn civil_from_days(z: i64) -> (i64, i64, i64) {
    let z = z + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z - era * 146_097;
    let yoe = (doe - doe / 1460 + doe / 36524 - doe / 146_096) / 365;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = doy - (153 * mp + 2) / 5 + 1;
    let m = if mp < 10 { mp + 3 } else { mp - 9 };
    (yoe + era * 400 + i64::from(m <= 2), m, d)
}

fn write_timestamp<W: Write>(out: &mut W, dt: &Bound<'_, PyDateTime>) -> PyResult<()> {
    let days = days_from_civil(i64::from(dt.get_year()), i64::from(dt.get_month()), i64::from(dt.get_day()));
    let secs =
        days * 86_400 + i64::from(dt.get_hour()) * 3600 + i64::from(dt.get_minute()) * 60 + i64::from(dt.get_second());
    let nanos = dt.get_microsecond() * 1000;
    if (0..1 << 34).contains(&secs) {
        #[allow(clippy::cast_sign_loss)]
        let v = (u64::from(nanos) << 34) | secs as u64;
        if nanos == 0 && secs <= i64::from(u32::MAX) {
            w::write_ext_meta(out, 4, -1).map_err(io)?;
            #[allow(clippy::cast_possible_truncation)]
            out.write_all(&(v as u32).to_be_bytes()).map_err(io)?;
        } else {
            w::write_ext_meta(out, 8, -1).map_err(io)?;
            out.write_all(&v.to_be_bytes()).map_err(io)?;
        }
    } else {
        w::write_ext_meta(out, 12, -1).map_err(io)?;
        out.write_all(&nanos.to_be_bytes()).map_err(io)?;
        out.write_all(&secs.to_be_bytes()).map_err(io)?;
    }
    Ok(())
}

// ---------------- decoder ----------------

/// Decodes one msgpack value that must fill `data` exactly. Malformed input raises
/// `CorruptValueError`; a Python error raised while building a value is returned as itself.
pub fn decode<'py>(py: Python<'py>, data: &[u8]) -> PyResult<Bound<'py, PyAny>> {
    let mut r = Reader { data, pos: 0, depth: 0 };
    let v = r.value(py)?;
    if r.pos != r.data.len() {
        return Err(CorruptValueError::new_err("trailing bytes after the msgpack value"));
    }
    Ok(v)
}

struct Reader<'a> {
    data: &'a [u8],
    pos: usize,
    depth: u32,
}

fn bad() -> PyErr {
    CorruptValueError::new_err("truncated or invalid msgpack")
}

fn corrupt(msg: impl Into<String>) -> PyErr {
    CorruptValueError::new_err(msg.into())
}

/// Well-formed, but this build doesn't know it: a newer version may have written it.
fn foreign(msg: impl Into<String>) -> PyErr {
    ForeignValueError::new_err(msg.into())
}

impl<'a> Reader<'a> {
    fn take(&mut self, n: usize) -> PyResult<&'a [u8]> {
        let end = self.pos.checked_add(n).filter(|&e| e <= self.data.len()).ok_or_else(bad)?;
        let s = &self.data[self.pos..end];
        self.pos = end;
        Ok(s)
    }
    fn u8(&mut self) -> PyResult<u8> {
        Ok(self.take(1)?[0])
    }
    fn be<const N: usize>(&mut self) -> PyResult<[u8; N]> {
        self.take(N)?.try_into().map_err(|_| bad())
    }
    fn len(&mut self, bytes: usize) -> PyResult<usize> {
        Ok(match bytes {
            1 => usize::from(self.u8()?),
            2 => usize::from(u16::from_be_bytes(self.be()?)),
            _ => u32::from_be_bytes(self.be()?) as usize,
        })
    }
    fn str_at(&mut self, n: usize) -> PyResult<&'a str> {
        std::str::from_utf8(self.take(n)?).map_err(|e| corrupt(format!("invalid UTF-8 in a msgpack str: {e}")))
    }

    /// If the next item is a str, returns it without converting it to Python.
    fn peek_str(&mut self) -> PyResult<Option<&'a str>> {
        let m = *self.data.get(self.pos).ok_or_else(bad)?;
        let n = match m {
            0xa0..=0xbf => {
                self.pos += 1;
                usize::from(m & 0x1f)
            }
            0xd9 => {
                self.pos += 1;
                self.len(1)?
            }
            0xda => {
                self.pos += 1;
                self.len(2)?
            }
            0xdb => {
                self.pos += 1;
                self.len(4)?
            }
            _ => return Ok(None),
        };
        self.str_at(n).map(Some)
    }

    fn key<'py>(&mut self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        match self.peek_str()? {
            Some(s) => Ok(common::key_str(py, s).into_any()),
            None => self.value(py),
        }
    }

    fn map_body<'py>(&mut self, py: Python<'py>, n: usize, d: &Bound<'py, PyDict>) -> PyResult<()> {
        for _ in 0..n {
            let k = self.key(py)?;
            let v = self.value(py)?;
            d.set_item(k, v)?;
        }
        Ok(())
    }

    fn map<'py>(&mut self, py: Python<'py>, n: usize) -> PyResult<Bound<'py, PyAny>> {
        let d = PyDict::new(py);
        if n == 0 {
            return Ok(d.into_any());
        }
        let first = match self.peek_str()? {
            Some(s) if s.starts_with('$') => {
                let tag = Tag::parse(s).ok_or_else(|| foreign(format!("unknown tag {s:?}")))?;
                if n != 1 {
                    return Err(corrupt("a tag map must have exactly one entry"));
                }
                return self.tag(py, tag);
            }
            Some(s) => common::key_str(py, s).into_any(),
            None => self.value(py)?,
        };
        let v = self.value(py)?;
        d.set_item(first, v)?;
        self.map_body(py, n - 1, &d)?;
        Ok(d.into_any())
    }

    fn array_len(&mut self) -> PyResult<usize> {
        let m = self.u8()?;
        match m {
            0x90..=0x9f => Ok(usize::from(m & 0x0f)),
            0xdc => self.len(2),
            0xdd => self.len(4),
            _ => Err(bad()),
        }
    }

    fn items<'py>(&mut self, py: Python<'py>) -> PyResult<Vec<Bound<'py, PyAny>>> {
        let n = self.array_len()?;
        // Every item takes at least one byte, so a crafted length can't make us over-allocate.
        let mut v = Vec::with_capacity(n.min(self.data.len() - self.pos).min(4096));
        for _ in 0..n {
            v.push(self.value(py)?);
        }
        Ok(v)
    }

    fn tag<'py>(&mut self, py: Python<'py>, tag: Tag) -> PyResult<Bound<'py, PyAny>> {
        match tag {
            Tag::Tuple => common::tuple_from(py, self.items(py)?),
            Tag::Set => Ok(PySet::new(py, self.items(py)?)?.into_any()),
            Tag::FrozenSet => Ok(PyFrozenSet::new(py, self.items(py)?)?.into_any()),
            Tag::Esc => {
                let m = self.u8()?;
                let n = match m {
                    0x80..=0x8f => usize::from(m & 0x0f),
                    0xde => self.len(2)?,
                    0xdf => self.len(4)?,
                    _ => return Err(bad()),
                };
                let d = PyDict::new(py);
                self.map_body(py, n, &d)?;
                Ok(d.into_any())
            }
            Tag::Int | Tag::Datetime | Tag::Date | Tag::Decimal => {
                let s = self.peek_str()?.ok_or_else(bad)?;
                match tag {
                    Tag::Int => common::int_from_text(py, s),
                    Tag::Datetime => common::datetime_from_text(py, s),
                    Tag::Date => common::date_from_text(py, s),
                    _ => common::decimal_from_text(py, s),
                }
            }
            // Not written by this version; a later one might use it.
            Tag::Bytes | Tag::Dict | Tag::Float => Err(foreign("tag not used in msgpack")),
        }
    }

    fn ext<'py>(&mut self, py: Python<'py>, n: usize) -> PyResult<Bound<'py, PyAny>> {
        #[allow(clippy::cast_possible_wrap)]
        let kind = self.u8()? as i8;
        let body = self.take(n)?;
        if kind != -1 {
            return Err(foreign(format!("unknown msgpack extension {kind}")));
        }
        let (secs, nanos): (i64, u32) = match n {
            4 => (i64::from(u32::from_be_bytes(body.try_into().map_err(|_| bad())?)), 0),
            8 => {
                let v = u64::from_be_bytes(body.try_into().map_err(|_| bad())?);
                #[allow(clippy::cast_possible_truncation, clippy::cast_possible_wrap)]
                ((v & ((1 << 34) - 1)) as i64, (v >> 34) as u32)
            }
            12 => (
                i64::from_be_bytes(body[4..].try_into().map_err(|_| bad())?),
                u32::from_be_bytes(body[..4].try_into().map_err(|_| bad())?),
            ),
            _ => return Err(bad()),
        };
        if nanos % 1000 != 0 || nanos >= 1_000_000_000 {
            return Err(corrupt("timestamp has sub-microsecond precision or an invalid nanosecond field"));
        }
        let (y, m, d) = civil_from_days(secs.div_euclid(86_400));
        let rem = secs.rem_euclid(86_400);
        let utc = PyTzInfo::utc(py)?;
        let year = i32::try_from(y).map_err(|_| corrupt("timestamp out of range"))?;
        #[allow(clippy::cast_possible_truncation, clippy::cast_sign_loss)]
        let dt = PyDateTime::new(
            py,
            year,
            m as u8,
            d as u8,
            (rem / 3600) as u8,
            (rem / 60 % 60) as u8,
            (rem % 60) as u8,
            nanos / 1000,
            Some(&utc),
        )?;
        Ok(dt.into_any())
    }

    fn value<'py>(&mut self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        // A crafted row must not overflow the stack; the encoder never writes deeper than this.
        if self.depth > 2 * common::MAX_DEPTH {
            return Err(corrupt("msgpack nested too deeply"));
        }
        self.depth += 1;
        let r = self.value_inner(py);
        self.depth -= 1;
        r
    }

    fn value_inner<'py>(&mut self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let m = self.u8()?;
        Ok(match m {
            0x00..=0x7f => i64::from(m).into_pyobject(py)?.into_any(),
            0xe0..=0xff =>
            {
                #[allow(clippy::cast_possible_wrap)]
                i64::from(m as i8).into_pyobject(py)?.into_any()
            }
            0x80..=0x8f => return self.map(py, usize::from(m & 0x0f)),
            0xde => {
                let n = self.len(2)?;
                return self.map(py, n);
            }
            0xdf => {
                let n = self.len(4)?;
                return self.map(py, n);
            }
            0x90..=0x9f | 0xdc | 0xdd => {
                self.pos -= 1;
                return Ok(PyList::new(py, self.items(py)?)?.into_any());
            }
            0xa0..=0xbf | 0xd9..=0xdb => {
                self.pos -= 1;
                let s = self.peek_str()?.ok_or_else(bad)?;
                PyString::new(py, s).into_any()
            }
            0xc0 => py.None().into_bound(py),
            0xc2 => PyBool::new(py, false).to_owned().into_any(),
            0xc3 => PyBool::new(py, true).to_owned().into_any(),
            0xc4..=0xc6 => {
                let n = self.len(1 << (m - 0xc4))?;
                PyBytes::new(py, self.take(n)?).into_any()
            }
            0xc7..=0xc9 => {
                let n = self.len(1 << (m - 0xc7))?;
                return self.ext(py, n);
            }
            0xd4..=0xd8 => return self.ext(py, 1 << (m - 0xd4)),
            0xca => PyFloat::new(py, f64::from(f32::from_be_bytes(self.be()?))).into_any(),
            0xcb => PyFloat::new(py, f64::from_be_bytes(self.be()?)).into_any(),
            0xcc => u8::from_be_bytes(self.be()?).into_pyobject(py)?.into_any(),
            0xcd => u16::from_be_bytes(self.be()?).into_pyobject(py)?.into_any(),
            0xce => u32::from_be_bytes(self.be()?).into_pyobject(py)?.into_any(),
            0xcf => u64::from_be_bytes(self.be()?).into_pyobject(py)?.into_any(),
            0xd0 => i8::from_be_bytes(self.be()?).into_pyobject(py)?.into_any(),
            0xd1 => i16::from_be_bytes(self.be()?).into_pyobject(py)?.into_any(),
            0xd2 => i32::from_be_bytes(self.be()?).into_pyobject(py)?.into_any(),
            0xd3 => i64::from_be_bytes(self.be()?).into_pyobject(py)?.into_any(),
            _ => return Err(bad()),
        })
    }
}
