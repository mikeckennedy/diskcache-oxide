//! Keys: a `str` key, stored as TEXT under `key_type = 1` as it always was, or a typed key (every
//! other key type) stored as a BLOB under `key_type = 2` (plan 24, release 1).
//!
//! A typed key's bytes are its identity and its `FanoutCache` shard, so the format below is frozen
//! from 0.2.0 on. It is an order-preserving tuple encoding in the style of `FoundationDB`'s: each
//! element is a type code, then its value.
//!
//! | Code | Element | Value |
//! |---|---|---|
//! | `0x00` | terminator, never a type | |
//! | `0x10` | `None` | |
//! | `0x20`, `0x21` | `False`, `True` | |
//! | `0x2F`, `0x31` | reserved: ints below `i64::MIN`, above `i64::MAX` | |
//! | `0x30` | int | 8 bytes big-endian, sign bit flipped |
//! | `0x40` | float | 8 bytes, total-order transform; `-0.0` is `0.0`; never NaN |
//! | `0x50` | bytes | each `0x00` as `0x00 0xFF`, then `0x00` |
//! | `0x60` | str, inside a tuple only | UTF-8, escaped and ended as bytes |
//! | `0x70` | tuple | its elements, then `0x00` |
//! | `0xFF` | reserved (the escape) | |
//!
//! A key is exactly one element; a lone `str` is never typed (it is a `str` key). Every element is
//! self-delimiting and no element starts with `0x00` or `0xFF`, so the encoding is injective and
//! decodes one way, and byte order is key order: by type (`None` < bool < int < float < bytes <
//! str < tuple), then by value, element by element for tuples, a tuple before any longer tuple it
//! begins. Tuples nest at most [`MAX_DEPTH`] deep.
//!
//! [`TypedKey`] can only be made by an [`Encoder`] or by [`TypedKey::parse`], which accepts bytes
//! only if encoding what they decode to gives them back, so a stored key read from a damaged or
//! foreign file can't be mistaken for another.

use std::borrow::Cow;
use std::cmp::Ordering;

use rusqlite::ToSql;
use rusqlite::types::{ToSqlOutput, ValueRef};

use crate::schema::{KEY_TYPE_STR, KEY_TYPE_TYPED};

/// The deepest a typed key's tuples nest. Refused when encoding, so raising it later is compatible.
pub const MAX_DEPTH: usize = 32;

pub(crate) mod code {
    pub const END: u8 = 0x00;
    pub const NONE: u8 = 0x10;
    pub const FALSE: u8 = 0x20;
    pub const TRUE: u8 = 0x21;
    pub const INT: u8 = 0x30;
    pub const FLOAT: u8 = 0x40;
    pub const BYTES: u8 = 0x50;
    pub const STR: u8 = 0x60;
    pub const TUPLE: u8 = 0x70;
    pub const ESCAPE: u8 = 0xFF;
}

pub(crate) const SIGN: u64 = 1 << 63;

/// Why a key can't be encoded, or stored bytes aren't a typed key.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum KeyError {
    #[error("a NaN float can't be a key: it isn't equal to itself")]
    NaN,
    #[error("key tuples nest at most {MAX_DEPTH} deep")]
    TooDeep,
    #[error("a str key is stored as text, not as a typed key")]
    TopLevelStr,
    #[error("a key is exactly one value")]
    NotOneElement,
    #[error("a key tuple was not closed")]
    UnclosedTuple,
    #[error("stored key bytes are malformed: {0}")]
    Malformed(&'static str),
}

/// A key other than a `str`, in its stored bytes. Ordered as SQLite orders the BLOBs.
#[derive(Clone, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct TypedKey(Box<[u8]>);

impl std::fmt::Debug for TypedKey {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "TypedKey(")?;
        for b in &*self.0 {
            write!(f, "{b:02x}")?;
        }
        write!(f, ")")
    }
}

impl TypedKey {
    /// The stored bytes, which are also the shard hash's input.
    #[must_use]
    pub fn as_bytes(&self) -> &[u8] {
        &self.0
    }

    /// Accepts `bytes` only if they are exactly the encoding of one key.
    ///
    /// # Errors
    ///
    /// [`KeyError`] naming what's wrong.
    pub fn parse(bytes: &[u8]) -> Result<Self, KeyError> {
        let mut encoder = Encoder::new();
        for token in Tokens::new(bytes) {
            match token? {
                Token::None => encoder.none(),
                Token::Bool(b) => encoder.bool(b),
                Token::Int(n) => encoder.int(n),
                Token::Float(x) => encoder.float(x),
                Token::Bytes(b) => encoder.bytes(&b),
                Token::Str(s) => encoder.str(&s),
                Token::BeginTuple => encoder.begin_tuple(),
                Token::EndTuple => encoder.end_tuple(),
            }
        }
        let key = encoder.finish()?;
        if *key.0 != *bytes {
            return Err(KeyError::Malformed("not in canonical form"));
        }
        Ok(key)
    }

    /// The key's elements in order, tuples bracketed by [`Token::BeginTuple`] and
    /// [`Token::EndTuple`]. A `TypedKey` is always well formed, so no item is an error unless the
    /// bytes were damaged in memory.
    #[must_use]
    pub fn tokens(&self) -> Tokens<'_> {
        Tokens::new(&self.0)
    }
}

/// One element of a typed key, or a tuple's bracket.
#[derive(Debug, Clone, PartialEq)]
pub enum Token<'a> {
    None,
    Bool(bool),
    Int(i64),
    Float(f64),
    Bytes(Cow<'a, [u8]>),
    Str(Cow<'a, str>),
    BeginTuple,
    EndTuple,
}

/// Decodes a typed key's bytes into [`Token`]s, strictly: never panics, stops at the first error.
#[derive(Debug)]
pub struct Tokens<'a> {
    bytes: &'a [u8],
    at: usize,
    depth: usize,
    started: bool,
    failed: bool,
}

impl<'a> Tokens<'a> {
    fn new(bytes: &'a [u8]) -> Self {
        Self { bytes, at: 0, depth: 0, started: false, failed: false }
    }

    fn take(&mut self, n: usize) -> Result<&'a [u8], KeyError> {
        let end = self.at.checked_add(n).filter(|&end| end <= self.bytes.len());
        let Some(end) = end else { return Err(KeyError::Malformed("truncated")) };
        let taken = &self.bytes[self.at..end];
        self.at = end;
        Ok(taken)
    }

    fn eight(&mut self) -> Result<u64, KeyError> {
        let taken = self.take(8)?;
        let mut word = [0u8; 8];
        word.copy_from_slice(taken);
        Ok(u64::from_be_bytes(word))
    }

    /// Escaped data up to its terminator.
    fn escaped(&mut self) -> Result<Cow<'a, [u8]>, KeyError> {
        let start = self.at;
        let mut owned: Option<Vec<u8>> = None;
        loop {
            let Some(&b) = self.bytes.get(self.at) else { return Err(KeyError::Malformed("unterminated data")) };
            self.at += 1;
            if b == code::END {
                if self.bytes.get(self.at) == Some(&code::ESCAPE) {
                    self.at += 1;
                    owned.get_or_insert_with(|| self.bytes[start..self.at - 2].to_vec()).push(0);
                    continue;
                }
                return Ok(match owned {
                    Some(v) => Cow::Owned(v),
                    None => Cow::Borrowed(&self.bytes[start..self.at - 1]),
                });
            }
            if let Some(v) = owned.as_mut() {
                v.push(b);
            }
        }
    }

    fn next_token(&mut self) -> Result<Option<Token<'a>>, KeyError> {
        if self.started && self.depth == 0 {
            return if self.at == self.bytes.len() { Ok(None) } else { Err(KeyError::Malformed("trailing bytes")) };
        }
        let Some(&c) = self.bytes.get(self.at) else {
            return Err(if self.started { KeyError::UnclosedTuple } else { KeyError::Malformed("empty") });
        };
        self.at += 1;
        let top = !self.started;
        self.started = true;
        Ok(Some(match c {
            code::END if self.depth > 0 => {
                self.depth -= 1;
                Token::EndTuple
            }
            code::NONE => Token::None,
            code::FALSE => Token::Bool(false),
            code::TRUE => Token::Bool(true),
            code::INT => Token::Int(i64::from_be_bytes((self.eight()? ^ SIGN).to_be_bytes())),
            code::FLOAT => {
                let stored = self.eight()?;
                let bits = if stored & SIGN != 0 { stored ^ SIGN } else { !stored };
                Token::Float(f64::from_bits(bits))
            }
            code::BYTES => Token::Bytes(self.escaped()?),
            code::STR if top => return Err(KeyError::TopLevelStr),
            code::STR => match self.escaped()? {
                Cow::Borrowed(b) => {
                    Token::Str(Cow::Borrowed(std::str::from_utf8(b).map_err(|_| KeyError::Malformed("bad UTF-8"))?))
                }
                Cow::Owned(v) => {
                    Token::Str(Cow::Owned(String::from_utf8(v).map_err(|_| KeyError::Malformed("bad UTF-8"))?))
                }
            },
            code::TUPLE => {
                if self.depth == MAX_DEPTH {
                    return Err(KeyError::TooDeep);
                }
                self.depth += 1;
                Token::BeginTuple
            }
            _ => return Err(KeyError::Malformed("unknown type code")),
        }))
    }
}

impl<'a> Iterator for Tokens<'a> {
    type Item = Result<Token<'a>, KeyError>;

    fn next(&mut self) -> Option<Self::Item> {
        if self.failed {
            return None;
        }
        match self.next_token() {
            Ok(token) => token.map(Ok),
            Err(error) => {
                self.failed = true;
                Some(Err(error))
            }
        }
    }
}

/// Builds a [`TypedKey`] one element at a time. The first error sticks and [`Encoder::finish`]
/// reports it.
#[derive(Debug, Default)]
pub struct Encoder {
    out: Vec<u8>,
    depth: usize,
    /// Top-level elements written so far (a key is exactly one).
    top: usize,
    error: Option<KeyError>,
}

impl Encoder {
    #[must_use]
    pub fn new() -> Self {
        Self::default()
    }

    fn element(&mut self, code: u8) {
        if self.depth == 0 {
            self.top += 1;
        }
        self.out.push(code);
    }

    fn fail(&mut self, error: KeyError) {
        self.error.get_or_insert(error);
    }

    fn escaped(&mut self, data: &[u8]) {
        for &b in data {
            self.out.push(b);
            if b == code::END {
                self.out.push(code::ESCAPE);
            }
        }
        self.out.push(code::END);
    }

    pub fn none(&mut self) {
        self.element(code::NONE);
    }

    pub fn bool(&mut self, value: bool) {
        self.element(if value { code::TRUE } else { code::FALSE });
    }

    pub fn int(&mut self, value: i64) {
        self.element(code::INT);
        self.out.extend_from_slice(&(u64::from_be_bytes(value.to_be_bytes()) ^ SIGN).to_be_bytes());
    }

    pub fn float(&mut self, value: f64) {
        if value.is_nan() {
            self.fail(KeyError::NaN);
            return;
        }
        // `-0.0 == 0.0`, so they are one key.
        let bits = if value == 0.0 { 0 } else { value.to_bits() };
        let stored = if bits & SIGN != 0 { !bits } else { bits ^ SIGN };
        self.element(code::FLOAT);
        self.out.extend_from_slice(&stored.to_be_bytes());
    }

    pub fn bytes(&mut self, value: &[u8]) {
        self.element(code::BYTES);
        self.escaped(value);
    }

    /// A `str` element. Only inside a tuple: a lone `str` is a `str` key.
    pub fn str(&mut self, value: &str) {
        if self.depth == 0 {
            self.fail(KeyError::TopLevelStr);
        }
        self.element(code::STR);
        self.escaped(value.as_bytes());
    }

    pub fn begin_tuple(&mut self) {
        if self.depth == MAX_DEPTH {
            self.fail(KeyError::TooDeep);
        }
        self.element(code::TUPLE);
        self.depth += 1;
    }

    pub fn end_tuple(&mut self) {
        if self.depth == 0 {
            self.fail(KeyError::Malformed("end of a tuple that wasn't begun"));
            return;
        }
        self.depth -= 1;
        self.out.push(code::END);
    }

    /// The key.
    ///
    /// # Errors
    ///
    /// The first error any element had, or [`KeyError::UnclosedTuple`] or
    /// [`KeyError::NotOneElement`].
    pub fn finish(self) -> Result<TypedKey, KeyError> {
        if let Some(error) = self.error {
            return Err(error);
        }
        if self.depth != 0 {
            return Err(KeyError::UnclosedTuple);
        }
        if self.top != 1 {
            return Err(KeyError::NotOneElement);
        }
        Ok(TypedKey(self.out.into_boxed_slice()))
    }
}

/// A key as the engine's calls take it.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Key<'a> {
    Str(&'a str),
    Typed(&'a TypedKey),
}

impl<'a> Key<'a> {
    /// The `key_type` column's value.
    #[must_use]
    pub fn key_type(self) -> i64 {
        match self {
            Key::Str(_) => KEY_TYPE_STR,
            Key::Typed(_) => KEY_TYPE_TYPED,
        }
    }

    /// The bytes the shard hash and the `key` column see.
    #[must_use]
    pub fn bytes(self) -> &'a [u8] {
        match self {
            Key::Str(s) => s.as_bytes(),
            Key::Typed(k) => k.as_bytes(),
        }
    }

    #[must_use]
    pub fn to_owned_key(self) -> OwnedKey {
        match self {
            Key::Str(s) => OwnedKey::Str(s.to_owned()),
            Key::Typed(k) => OwnedKey::Typed(k.clone()),
        }
    }
}

impl<'a> From<&'a str> for Key<'a> {
    fn from(s: &'a str) -> Self {
        Key::Str(s)
    }
}

impl<'a> From<&'a String> for Key<'a> {
    fn from(s: &'a String) -> Self {
        Key::Str(s)
    }
}

impl<'a> From<&'a TypedKey> for Key<'a> {
    fn from(k: &'a TypedKey) -> Self {
        Key::Typed(k)
    }
}

impl ToSql for Key<'_> {
    fn to_sql(&self) -> rusqlite::Result<ToSqlOutput<'_>> {
        // The storage class is part of a key's identity: a str key is always TEXT.
        Ok(ToSqlOutput::Borrowed(match self {
            Key::Str(s) => ValueRef::Text(s.as_bytes()),
            Key::Typed(k) => ValueRef::Blob(k.as_bytes()),
        }))
    }
}

/// A key read back from the database. Its order is SQLite's `(key_type, key)` order.
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub enum OwnedKey {
    Str(String),
    Typed(TypedKey),
}

impl OwnedKey {
    #[must_use]
    pub fn as_key(&self) -> Key<'_> {
        match self {
            OwnedKey::Str(s) => Key::Str(s),
            OwnedKey::Typed(k) => Key::Typed(k),
        }
    }

    /// Reads a stored key by its `key_type`.
    ///
    /// # Errors
    ///
    /// [`KeyError::Malformed`] for an unknown `key_type`, a storage class that doesn't match it,
    /// or typed bytes that don't parse.
    pub fn from_row(key_type: i64, key: ValueRef<'_>) -> Result<Self, KeyError> {
        match (key_type, key) {
            (KEY_TYPE_STR, ValueRef::Text(t)) => std::str::from_utf8(t)
                .map(|s| OwnedKey::Str(s.to_owned()))
                .map_err(|_| KeyError::Malformed("bad UTF-8 in a str key")),
            (KEY_TYPE_TYPED, ValueRef::Blob(b)) => TypedKey::parse(b).map(OwnedKey::Typed),
            _ => Err(KeyError::Malformed("unknown key_type or storage class")),
        }
    }
}

impl PartialOrd<Key<'_>> for OwnedKey {
    fn partial_cmp(&self, other: &Key<'_>) -> Option<Ordering> {
        Some((self.as_key().key_type(), self.as_key().bytes()).cmp(&(other.key_type(), other.bytes())))
    }
}

impl PartialEq<str> for OwnedKey {
    fn eq(&self, other: &str) -> bool {
        matches!(self, OwnedKey::Str(s) if s == other)
    }
}

impl PartialEq<&str> for OwnedKey {
    fn eq(&self, other: &&str) -> bool {
        self == *other
    }
}

impl PartialEq<String> for OwnedKey {
    fn eq(&self, other: &String) -> bool {
        self == other.as_str()
    }
}

impl PartialEq<Key<'_>> for OwnedKey {
    fn eq(&self, other: &Key<'_>) -> bool {
        self.as_key() == *other
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// A key as a tree, for the tests.
    #[derive(Debug, Clone, PartialEq)]
    enum V {
        None,
        Bool(bool),
        Int(i64),
        Float(f64),
        Bytes(Vec<u8>),
        Str(String),
        Tuple(Vec<V>),
    }

    fn put(e: &mut Encoder, v: &V) {
        match v {
            V::None => e.none(),
            V::Bool(b) => e.bool(*b),
            V::Int(n) => e.int(*n),
            V::Float(x) => e.float(*x),
            V::Bytes(b) => e.bytes(b),
            V::Str(s) => e.str(s),
            V::Tuple(items) => {
                e.begin_tuple();
                for item in items {
                    put(e, item);
                }
                e.end_tuple();
            }
        }
    }

    fn enc(v: &V) -> Result<TypedKey, KeyError> {
        let mut e = Encoder::new();
        put(&mut e, v);
        e.finish()
    }

    fn hex(v: &V) -> String {
        enc(v).unwrap().as_bytes().iter().fold(String::new(), |mut out, b| {
            use std::fmt::Write;
            let _ = write!(out, "{b:02x}");
            out
        })
    }

    fn dec(k: &TypedKey) -> V {
        let mut stack: Vec<Vec<V>> = vec![Vec::new()];
        for t in k.tokens() {
            let v = match t.unwrap() {
                Token::None => V::None,
                Token::Bool(b) => V::Bool(b),
                Token::Int(n) => V::Int(n),
                Token::Float(x) => V::Float(x),
                Token::Bytes(b) => V::Bytes(b.into_owned()),
                Token::Str(s) => V::Str(s.into_owned()),
                Token::BeginTuple => {
                    stack.push(Vec::new());
                    continue;
                }
                Token::EndTuple => V::Tuple(stack.pop().unwrap()),
            };
            stack.last_mut().unwrap().push(v);
        }
        let mut top = stack.pop().unwrap();
        assert_eq!(top.len(), 1);
        top.pop().unwrap()
    }

    fn t(items: Vec<V>) -> V {
        V::Tuple(items)
    }

    fn s(x: &str) -> V {
        V::Str(x.to_owned())
    }

    /// Frozen from 0.2.0: a change here changes stored keys and their shards.
    #[test]
    fn golden_vectors() {
        let cases: Vec<(V, &str)> = vec![
            (V::None, "10"),
            (V::Bool(false), "20"),
            (V::Bool(true), "21"),
            (V::Int(0), "308000000000000000"),
            (V::Int(1), "308000000000000001"),
            (V::Int(-1), "307fffffffffffffff"),
            (V::Int(i64::MIN), "300000000000000000"),
            (V::Int(i64::MAX), "30ffffffffffffffff"),
            (V::Float(0.0), "408000000000000000"),
            (V::Float(-0.0), "408000000000000000"),
            (V::Float(1.0), "40bff0000000000000"),
            (V::Float(-1.0), "40400fffffffffffff"),
            (V::Float(f64::INFINITY), "40fff0000000000000"),
            (V::Float(f64::NEG_INFINITY), "40000fffffffffffff"),
            (V::Bytes(vec![]), "5000"),
            (V::Bytes(vec![0]), "5000ff00"),
            (V::Bytes(vec![0xff]), "50ff00"),
            (V::Bytes(b"x".to_vec()), "507800"),
            (t(vec![]), "7000"),
            (t(vec![V::None]), "701000"),
            (t(vec![V::Int(1)]), "7030800000000000000100"),
            (t(vec![s("a"), V::None]), "706061001000"),
            (t(vec![t(vec![]), V::None]), "7070001000"),
            (t(vec![t(vec![V::None])]), "7070100000"),
            (t(vec![s("é")]), "7060c3a90000"),
            (t(vec![V::Bytes(vec![])]), "70500000"),
            (t(vec![s("a\0")]), "70606100ff0000"),
            (V::Bytes(vec![0x00, 0xff]), "5000ffff00"),
            (V::Float(f64::MIN_POSITIVE), "408010000000000000"),
            (V::Float(-f64::MIN_POSITIVE), "407fefffffffffffff"),
            (V::Float(f64::MAX), "40ffefffffffffffff"),
        ];
        for (v, want) in cases {
            assert_eq!(hex(&v), want, "{v:?}");
        }
    }

    #[test]
    fn the_keys_that_once_collided_are_distinct() {
        // With None at 0x00 (the terminator), ((), None) and ((None,),) were both 07 07 00 00 00.
        assert_ne!(hex(&t(vec![t(vec![]), V::None])), hex(&t(vec![t(vec![V::None])])));
        assert_ne!(hex(&V::Int(1)), hex(&t(vec![V::Int(1)])));
        assert_ne!(hex(&t(vec![])), hex(&t(vec![V::None])));
    }

    #[test]
    fn the_shard_of_each_golden_key_is_frozen() {
        let shard = |v: &V, shards| crate::fanout::shard_of_key(Key::Typed(&enc(v).unwrap()), shards);
        let keys = [V::None, V::Int(1), V::Bytes(b"x".to_vec()), t(vec![s("m.f"), V::Int(3), V::None])];
        let got: Vec<(usize, usize)> = keys.iter().map(|k| (shard(k, 8), shard(k, 256))).collect();
        assert_eq!(got, FROZEN_SHARDS, "typed keys' shards are part of the on-disk format");
        assert_eq!(crate::fanout::shard_of_key(Key::Str("k"), 256), crate::fanout::shard_of_str("k", 256));
    }

    const FROZEN_SHARDS: [(usize, usize); 4] = [(0, 9), (2, 86), (0, 6), (2, 91)];

    #[test]
    fn what_a_key_cannot_be() {
        assert_eq!(enc(&V::Float(f64::NAN)), Err(KeyError::NaN));
        assert_eq!(enc(&s("a")), Err(KeyError::TopLevelStr));
        assert_eq!(Encoder::new().finish(), Err(KeyError::NotOneElement));
        let mut two = Encoder::new();
        two.none();
        two.none();
        assert_eq!(two.finish(), Err(KeyError::NotOneElement));
        let mut open = Encoder::new();
        open.begin_tuple();
        assert_eq!(open.finish(), Err(KeyError::UnclosedTuple));
        let mut deep = V::None;
        for _ in 0..MAX_DEPTH {
            deep = t(vec![deep]);
        }
        assert!(enc(&deep).is_ok(), "{MAX_DEPTH} deep is allowed");
        assert_eq!(enc(&t(vec![deep])), Err(KeyError::TooDeep));
    }

    #[test]
    fn parse_accepts_only_canonical_encodings() {
        let bad: &[(&[u8], &str)] = &[
            (b"", "empty"),
            (&[0x00], "a terminator"),
            (&[0xff], "the escape"),
            (&[0x11], "unknown code"),
            (&[0x10, 0x10], "two elements"),
            (&[0x10, 0x00], "trailing terminator"),
            (&[0x60, 0x61, 0x00], "top-level str"),
            (&[0x30, 0x80], "short int"),
            (&[0x50, 0x61], "unterminated bytes"),
            (&[0x70, 0x10], "unclosed tuple"),
            (&[0x40, 0x7f, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff], "-0.0"),
            (&[0x40, 0xff, 0xf8, 0, 0, 0, 0, 0, 0], "NaN"),
            (&[0x70, 0x60, 0xc3, 0x00, 0x00], "bad UTF-8"),
        ];
        for (bytes, what) in bad {
            assert!(TypedKey::parse(bytes).is_err(), "{what} must not parse");
        }
        let mut deep = vec![0x70; MAX_DEPTH + 1];
        deep.extend(std::iter::repeat_n(0x00, MAX_DEPTH + 1));
        assert_eq!(TypedKey::parse(&deep), Err(KeyError::TooDeep));
    }

    /// xorshift64*: deterministic, so a failure reproduces.
    struct Rng(u64);
    impl Rng {
        fn next(&mut self) -> u64 {
            self.0 ^= self.0 >> 12;
            self.0 ^= self.0 << 25;
            self.0 ^= self.0 >> 27;
            self.0.wrapping_mul(0x2545_f491_4f6c_dd1d)
        }
        fn below(&mut self, n: u64) -> u64 {
            self.next() % n
        }
    }

    fn small_bytes(r: &mut Rng) -> Vec<u8> {
        // Mostly 0x00, 0x01 and 0xff, where the escaping lives.
        (0..r.below(4)).map(|_| [0x00, 0x01, 0xff, 0x61][usize::try_from(r.below(4)).unwrap()]).collect()
    }

    fn value(r: &mut Rng, depth: usize, top: bool) -> V {
        let kinds = if depth >= 3 { 6 } else { 7 };
        match r.below(kinds) {
            0 => V::None,
            1 => V::Bool(r.below(2) == 1),
            2 => V::Int([0, 1, -1, i64::MIN, i64::MAX, 255, -256][usize::try_from(r.below(7)).unwrap()]),
            3 => V::Float(
                [0.0, -1.5, 1.5, f64::INFINITY, f64::NEG_INFINITY, 1e-300, -1e300]
                    [usize::try_from(r.below(7)).unwrap()],
            ),
            4 => V::Bytes(small_bytes(r)),
            5 if top => V::Bytes(small_bytes(r)),
            5 => V::Str(String::from_utf8_lossy(&small_bytes(r)).into_owned()),
            _ => t((0..r.below(4)).map(|_| value(r, depth + 1, false)).collect()),
        }
    }

    fn rank(v: &V) -> u8 {
        match v {
            V::None => 0,
            V::Bool(_) => 1,
            V::Int(_) => 2,
            V::Float(_) => 3,
            V::Bytes(_) => 4,
            V::Str(_) => 5,
            V::Tuple(_) => 6,
        }
    }

    /// The order keys should sort in: by type, then by value (Python's, within one type).
    fn order(a: &V, b: &V) -> Ordering {
        match (a, b) {
            (V::Bool(x), V::Bool(y)) => x.cmp(y),
            (V::Int(x), V::Int(y)) => x.cmp(y),
            (V::Float(x), V::Float(y)) => x.partial_cmp(y).unwrap(),
            (V::Bytes(x), V::Bytes(y)) => x.cmp(y),
            (V::Str(x), V::Str(y)) => x.cmp(y),
            (V::Tuple(x), V::Tuple(y)) => {
                for (p, q) in x.iter().zip(y) {
                    match order(p, q) {
                        Ordering::Equal => {}
                        other => return other,
                    }
                }
                x.len().cmp(&y.len())
            }
            _ => rank(a).cmp(&rank(b)),
        }
    }

    #[test]
    fn byte_order_is_key_order_and_every_key_round_trips() {
        let mut r = Rng(0x9e37_79b9_7f4a_7c15);
        for _ in 0..20_000 {
            let (a, b) = (value(&mut r, 0, true), value(&mut r, 0, true));
            let (ka, kb) = (enc(&a).unwrap(), enc(&b).unwrap());
            assert_eq!(ka.cmp(&kb), order(&a, &b), "{a:?} vs {b:?}");
            assert_eq!(dec(&ka), a);
            assert_eq!(TypedKey::parse(ka.as_bytes()).as_ref(), Ok(&ka));
        }
    }

    #[test]
    fn a_damaged_key_parses_only_as_its_own_encoding() {
        let mut r = Rng(42);
        for _ in 0..20_000 {
            let key = enc(&value(&mut r, 0, true)).unwrap();
            let mut bytes = key.as_bytes().to_vec();
            match r.below(3) {
                0 => {
                    let at = usize::try_from(r.below(bytes.len() as u64)).unwrap();
                    bytes[at] = u8::try_from(r.below(256)).unwrap();
                }
                1 => {
                    bytes.truncate(usize::try_from(r.below(bytes.len() as u64)).unwrap());
                }
                _ => bytes.push(u8::try_from(r.below(256)).unwrap()),
            }
            if let Ok(parsed) = TypedKey::parse(&bytes) {
                // Independent of `parse`'s own check: through the test's tree and its encoder.
                assert_eq!(
                    enc(&dec(&parsed)).unwrap().as_bytes(),
                    &bytes[..],
                    "a parse must give back the bytes it read"
                );
            }
        }
    }

    #[test]
    fn random_bytes_never_panic_and_parse_only_as_canonical_keys() {
        // Whole byte strings, biased toward the type codes, the terminator and the escape.
        const BIASED: [u8; 12] = [0x00, 0xff, 0x10, 0x20, 0x21, 0x30, 0x40, 0x50, 0x60, 0x70, 0x80, 0x61];
        let mut r = Rng(7);
        let mut parsed = 0;
        for _ in 0..200_000 {
            let len = usize::try_from(r.below(14)).unwrap();
            let bytes: Vec<u8> = (0..len)
                .map(|_| {
                    if r.below(4) == 0 {
                        u8::try_from(r.below(256)).unwrap()
                    } else {
                        BIASED[usize::try_from(r.below(12)).unwrap()]
                    }
                })
                .collect();
            if let Ok(key) = TypedKey::parse(&bytes) {
                parsed += 1;
                assert_eq!(enc(&dec(&key)).unwrap().as_bytes(), &bytes[..]);
            }
        }
        assert!(parsed > 1_000, "the generator should hit valid keys too: {parsed}");
    }

    #[test]
    fn ints_and_floats_of_any_bit_pattern_keep_their_order() {
        let mut rng = Rng(0xdead_beef);
        for _ in 0..50_000 {
            let (lo, hi) = (i64::from_ne_bytes(rng.next().to_ne_bytes()), i64::from_ne_bytes(rng.next().to_ne_bytes()));
            assert_eq!(enc(&V::Int(lo)).unwrap().cmp(&enc(&V::Int(hi)).unwrap()), lo.cmp(&hi), "{lo} vs {hi}");
            let (first, second) = (f64::from_bits(rng.next()), f64::from_bits(rng.next()));
            if first.is_nan() || second.is_nan() {
                continue;
            }
            // -0.0 is stored as 0.0, so compare them as equal.
            let zeroed = |x: f64| if x == 0.0 { 0.0 } else { x };
            let want = zeroed(first).total_cmp(&zeroed(second));
            assert_eq!(
                enc(&V::Float(first)).unwrap().cmp(&enc(&V::Float(second)).unwrap()),
                want,
                "{first} vs {second}"
            );
            assert_eq!(dec(&enc(&V::Float(first)).unwrap()), V::Float(zeroed(first)));
        }
    }

    #[test]
    fn keys_bind_as_text_or_blob_and_order_as_sqlite_does() {
        let typed = enc(&V::Int(1)).unwrap();
        assert!(matches!(Key::from("a").to_sql().unwrap(), ToSqlOutput::Borrowed(ValueRef::Text(b"a"))));
        assert!(matches!(Key::Typed(&typed).to_sql().unwrap(), ToSqlOutput::Borrowed(ValueRef::Blob(_))));
        let mut keys = vec![OwnedKey::Typed(typed.clone()), OwnedKey::Str("b".into()), OwnedKey::Str("a".into())];
        keys.sort();
        assert_eq!(keys, [OwnedKey::Str("a".into()), OwnedKey::Str("b".into()), OwnedKey::Typed(typed.clone())]);
        let row = OwnedKey::from_row(KEY_TYPE_TYPED, ValueRef::Blob(typed.as_bytes())).unwrap();
        assert_eq!(row, OwnedKey::Typed(typed.clone()));
        assert!(OwnedKey::from_row(KEY_TYPE_STR, ValueRef::Blob(b"a")).is_err(), "a str key is always TEXT");
        assert!(OwnedKey::from_row(KEY_TYPE_TYPED, ValueRef::Text(typed.as_bytes())).is_err());
        assert!(OwnedKey::from_row(3, ValueRef::Blob(b"x")).is_err());
    }
}
