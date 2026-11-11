//! The queue calls (plan 24, release 4; D-054 to D-057): a queue's key range, finding its head and
//! its end, and the reads `peek` and `peekitem`. `push` is a write and lives beside the other writes
//! in `ops`. `pull` is `peek` and then [`Store::delete_version`] of exactly the version read, retried
//! by the binding, which decodes in between (decision 25: never a remove-then-decode).
//!
//! A queue is a range of keys, as diskcache's: with no prefix the `int` keys `0 < n < END`, with one
//! the `str` keys `'{prefix}-'` and 15 digits, numbered from [`START`]. Unlike diskcache's, a key
//! belongs to the queue only if it has exactly that shape (D-054). Both ranges are a seek on
//! `cache_key (key_type, key)`: an int key is `0x30` and 8 bytes in numeric order, so its range is a
//! byte range no other type code can fall in; every `str` key strictly between the prefixed bounds
//! starts with `'{prefix}-'`, so with the exact length and an all-digit tail, `'q'` and `'q-1'` can't
//! cross. The shape is checked here in Rust, not in SQL, whose `length()` stops at a NUL and counts
//! characters, not bytes. Other keys in the range cost a scan per call: a queue's prefix should be
//! one no other key starts with.
//!
//! The reads never write (D-055): the head is the first live row, and expired ones are left to
//! `expire()` and the write path's sweep. They go through [`Store::read_at`], so the version, the
//! tag and the key come from one statement on every path, and a row whose value file is truly gone
//! is passed over rather than found again: they never spin.

use std::time::Duration;

use rusqlite::Connection;
use rusqlite::types::ValueRef;

use crate::key::{Encoder, OwnedKey};
use crate::ops::{Found, Hit, Locate, Read, found_from_row};
use crate::schema::{KEY_TYPE_STR, KEY_TYPE_TYPED};
use crate::store::Store;
use crate::{Error, Result};

/// The first item's number in an empty queue (diskcache's).
pub const START: i64 = 500_000_000_000_000;

/// One past the last number a queue may hold: its range is `0 < n < END`, both bounds excluded.
pub const END: i64 = 999_999_999_999_999;

/// Digits in a prefixed queue key's number.
const DIGITS: usize = 15;

/// Which end of a queue.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Side {
    Front,
    Back,
}

/// The keys of one queue: `int` keys with no prefix, or `str` keys `'{prefix}-'` and 15 digits.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum QueueRange {
    Int,
    Prefixed(String),
}

impl QueueRange {
    #[must_use]
    pub(crate) fn key_type(&self) -> i64 {
        match self {
            Self::Int => KEY_TYPE_TYPED,
            Self::Prefixed(_) => KEY_TYPE_STR,
        }
    }

    /// The range's exclusive bounds, as keys (bound as a key is: TEXT or BLOB).
    pub(crate) fn bounds(&self) -> (OwnedKey, OwnedKey) {
        match self {
            Self::Int => (int_key(0), int_key(END)),
            Self::Prefixed(prefix) => {
                (OwnedKey::Str(format!("{prefix}-{:0DIGITS$}", 0)), OwnedKey::Str(format!("{prefix}-{END:0DIGITS$}")))
            }
        }
    }

    /// The number of a stored key in the range, if the key is exactly one of this queue's; `None`
    /// for any other key the range holds.
    pub(crate) fn number(&self, key: ValueRef<'_>) -> Option<i64> {
        match (self, key) {
            (Self::Int, ValueRef::Blob(bytes)) => {
                let body: [u8; 8] = bytes.strip_prefix(&[crate::key::code::INT])?.try_into().ok()?;
                let n = i64::from_be_bytes((u64::from_be_bytes(body) ^ crate::key::SIGN).to_be_bytes());
                (0 < n && n < END).then_some(n)
            }
            (Self::Prefixed(prefix), ValueRef::Text(bytes)) => {
                let tail = bytes.strip_prefix(prefix.as_bytes())?.strip_prefix(b"-")?;
                if tail.len() != DIGITS || !tail.iter().all(u8::is_ascii_digit) {
                    return None;
                }
                let n = std::str::from_utf8(tail).ok()?.parse::<i64>().ok()?;
                (0 < n && n < END).then_some(n)
            }
            _ => None,
        }
    }

    /// The key of item number `n`.
    pub(crate) fn key_of(&self, n: i64) -> OwnedKey {
        match self {
            Self::Int => int_key(n),
            Self::Prefixed(prefix) => OwnedKey::Str(format!("{prefix}-{n:0DIGITS$}")),
        }
    }
}

fn int_key(n: i64) -> OwnedKey {
    let mut encoder = Encoder::new();
    encoder.int(n);
    #[allow(clippy::expect_used)] // One int is always a complete key.
    OwnedKey::Typed(encoder.finish().expect("a lone int is a valid key"))
}

/// The range's rows in order, from the front or the back. Every statement here walks the
/// `cache_key` index between the two bounds with no sort (pinned by the plan tests below).
const HEAD_FRONT_SQL: &str =
    "SELECT mode, value, expire_time, filename, size, id, store_seq, tag_type, tag, key FROM cache
     WHERE key_type = ?1 AND key > ?2 AND key < ?3 AND (expire_time IS NULL OR expire_time > ?4) ORDER BY key";
const HEAD_BACK_SQL: &str =
    "SELECT mode, value, expire_time, filename, size, id, store_seq, tag_type, tag, key FROM cache
     WHERE key_type = ?1 AND key > ?2 AND key < ?3 AND (expire_time IS NULL OR expire_time > ?4) ORDER BY key DESC";
/// Every row of the range, live or not (an expired item still holds its number), for `push`.
pub(crate) const END_FRONT_SQL: &str =
    "SELECT key FROM cache WHERE key_type = ?1 AND key > ?2 AND key < ?3 ORDER BY key";
pub(crate) const END_BACK_SQL: &str =
    "SELECT key FROM cache WHERE key_type = ?1 AND key > ?2 AND key < ?3 ORDER BY key DESC";
/// Live rows in insertion order (as `__iter__`), for `peekitem`. Rows of an unknown key type are
/// passed over, as iteration passes them. The unary `+` keeps `cache_key` out of the plan: with it the
/// planner seeks `key_type` and then sorts every row by `id`.
const OLDEST_SQL: &str =
    "SELECT mode, value, expire_time, filename, size, id, store_seq, tag_type, tag, key_type, key FROM cache
     WHERE +key_type IN (1, 2) AND (expire_time IS NULL OR expire_time > ?1) ORDER BY id";
const NEWEST_SQL: &str =
    "SELECT mode, value, expire_time, filename, size, id, store_seq, tag_type, tag, key_type, key FROM cache
     WHERE +key_type IN (1, 2) AND (expire_time IS NULL OR expire_time > ?1) ORDER BY id DESC";

/// The queue's live head from `side`, passing over the rows in `skip` and any key not of the queue's
/// shape, as a [`Found`] carrying its key.
#[allow(clippy::too_many_arguments)]
pub(crate) fn look_up_head<T>(
    conn: &Connection,
    range: &QueueRange,
    side: Side,
    skip: &[i64],
    now: f64,
    with_tag: bool,
    timeout: Duration,
    decode: &mut impl FnMut(i64, ValueRef<'_>) -> Result<T>,
) -> Result<Found<T>> {
    let sqlite = |e| Error::from_sqlite(e, timeout);
    let (low, high) = range.bounds();
    let sql = match side {
        Side::Front => HEAD_FRONT_SQL,
        Side::Back => HEAD_BACK_SQL,
    };
    let mut stmt = conn.prepare_cached(sql).map_err(sqlite)?;
    let mut rows = stmt.query((range.key_type(), low.as_key(), high.as_key(), now)).map_err(sqlite)?;
    while let Some(row) = rows.next().map_err(sqlite)? {
        if skip.contains(&row.get::<_, i64>(5).map_err(sqlite)?) {
            continue;
        }
        let stored = row.get_ref(9).map_err(sqlite)?;
        if range.number(stored).is_none() {
            continue;
        }
        let key = OwnedKey::from_row(range.key_type(), stored).map_err(Error::CorruptKey)?;
        return found_from_row(row, with_tag, Some(key), timeout, decode);
    }
    Ok(Found::Miss)
}

/// The oldest (or with `last`, the newest) live row of any key, passing over the rows in `skip`.
pub(crate) fn look_up_inserted<T>(
    conn: &Connection,
    last: bool,
    skip: &[i64],
    now: f64,
    with_tag: bool,
    timeout: Duration,
    decode: &mut impl FnMut(i64, ValueRef<'_>) -> Result<T>,
) -> Result<Found<T>> {
    let sqlite = |e| Error::from_sqlite(e, timeout);
    let mut stmt = conn.prepare_cached(if last { NEWEST_SQL } else { OLDEST_SQL }).map_err(sqlite)?;
    let mut rows = stmt.query([now]).map_err(sqlite)?;
    while let Some(row) = rows.next().map_err(sqlite)? {
        if skip.contains(&row.get::<_, i64>(5).map_err(sqlite)?) {
            continue;
        }
        // A damaged key is an error, as in iteration: never skipped.
        let key = OwnedKey::from_row(row.get(9).map_err(sqlite)?, row.get_ref(10).map_err(sqlite)?)
            .map_err(Error::CorruptKey)?;
        return found_from_row(row, with_tag, Some(key), timeout, decode);
    }
    Ok(Found::Miss)
}

/// The next free number from `side`, or [`START`] for an empty queue: one past the end, counting
/// expired items, which still hold their numbers. [`Error::QueueFull`] past either bound.
pub(crate) fn next_key(conn: &Connection, range: &QueueRange, side: Side, timeout: Duration) -> Result<OwnedKey> {
    let sqlite = |e| Error::from_sqlite(e, timeout);
    let (low, high) = range.bounds();
    // From the side being pushed to: the end is the largest number for the back, the smallest for
    // the front.
    let sql = match side {
        Side::Back => END_BACK_SQL,
        Side::Front => END_FRONT_SQL,
    };
    let mut stmt = conn.prepare_cached(sql).map_err(sqlite)?;
    let mut rows = stmt.query((range.key_type(), low.as_key(), high.as_key())).map_err(sqlite)?;
    let mut end = None;
    while let Some(row) = rows.next().map_err(sqlite)? {
        if let Some(n) = range.number(row.get_ref(0).map_err(sqlite)?) {
            end = Some(n);
            break;
        }
    }
    let next = match (end, side) {
        (None, _) => START,
        (Some(n), Side::Back) => n + 1,
        (Some(n), Side::Front) => n - 1,
    };
    if next <= 0 || next >= END {
        return Err(Error::QueueFull);
    }
    Ok(range.key_of(next))
}

impl Store {
    /// The live item at `side` of the queue `range`, read as [`Store::get_tagged`] reads, with its key
    /// in [`Hit::key`]; `None` for an empty queue. Never writes, but removes without waiting an item
    /// whose value file is truly gone (D-013), and goes on to the next.
    #[allow(clippy::too_many_arguments)]
    pub fn peek<T>(
        &self,
        timeout: Duration,
        range: &QueueRange,
        side: Side,
        now: f64,
        with_tag: bool,
        refuse: impl Fn(i64) -> bool,
        mut decode: impl FnMut(i64, ValueRef<'_>) -> Result<T>,
    ) -> Result<Option<Hit<T>>> {
        self.read_passing_lost(timeout, Locate::Head { range, side }, now, with_tag, &refuse, &mut decode)
    }

    /// The newest live entry (or with `last` false, the oldest), of any key, as [`Store::peek`].
    pub fn peekitem<T>(
        &self,
        timeout: Duration,
        last: bool,
        now: f64,
        with_tag: bool,
        refuse: impl Fn(i64) -> bool,
        mut decode: impl FnMut(i64, ValueRef<'_>) -> Result<T>,
    ) -> Result<Option<Hit<T>>> {
        self.read_passing_lost(timeout, Locate::Inserted { last }, now, with_tag, &refuse, &mut decode)
    }

    /// [`Store::read_at`] at a position, passing over rows whose value files are gone: each one is
    /// added to the skip list, so it can't be found again even if removing it had to be skipped.
    fn read_passing_lost<T>(
        &self,
        timeout: Duration,
        locate: Locate<'_>,
        now: f64,
        with_tag: bool,
        refuse: &impl Fn(i64) -> bool,
        decode: &mut impl FnMut(i64, ValueRef<'_>) -> Result<T>,
    ) -> Result<Option<Hit<T>>> {
        let mut lost = Vec::new();
        loop {
            match self.read_at(timeout, locate, &lost, now, with_tag, refuse, &mut *decode)? {
                Read::Hit(hit) => return Ok(Some(hit)),
                Read::Miss => return Ok(None),
                Read::Lost(version) => lost.push(version.parts().0),
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use std::path::Path;
    use std::sync::Arc;

    use super::*;
    use crate::key::Key;
    use crate::open::Requested;
    use crate::ops::{Pop, Value};
    use crate::rows::Tag;
    use crate::schema::{MODE_BYTES, MODE_STR};
    use crate::store::{DEFAULT_CACHE_PAGES, DEFAULT_MAX_CONNECTIONS, DEFAULT_WAL_SIZE_LIMIT, OpenOptions};
    use crate::testutil::TempDir;

    const T: Duration = Duration::from_secs(5);
    const NOW: f64 = 1_000_000.0;

    fn store(dir: &Path) -> Arc<Store> {
        Store::open(&OpenOptions {
            directory: dir.to_path_buf(),
            timeout: T,
            requested: Requested::default(),
            max_connections: DEFAULT_MAX_CONNECTIONS,
            cache_pages: DEFAULT_CACHE_PAGES,
            wal_size_limit: DEFAULT_WAL_SIZE_LIMIT,
            auto_checkpoint: false,
            role: crate::open::Role::Plain,
            default_size_limit: crate::schema::DEFAULT_SIZE_LIMIT,
        })
        .unwrap()
    }

    fn v(s: &str) -> Value<'_> {
        Value::Bytes { mode: MODE_STR, data: s.as_bytes() }
    }

    fn push(s: &Store, range: &QueueRange, side: Side, value: &str) -> OwnedKey {
        s.push(T, range, side, v(value), None, None, NOW).unwrap()
    }

    #[allow(clippy::unnecessary_wraps)] // The shape `decode` callbacks have.
    fn bytes_of(_: i64, value: ValueRef<'_>) -> Result<Vec<u8>> {
        Ok(match value {
            ValueRef::Text(b) | ValueRef::Blob(b) => b.to_vec(),
            _ => Vec::new(),
        })
    }

    fn peek(s: &Store, range: &QueueRange, side: Side, now: f64) -> Option<(OwnedKey, Vec<u8>)> {
        s.peek(T, range, side, now, false, |_| false, bytes_of).unwrap().map(|hit| (hit.key.unwrap(), hit.value))
    }

    /// A pull as the binding does it: the head read, then exactly that version deleted.
    fn pull(s: &Store, range: &QueueRange, side: Side) -> Option<(OwnedKey, Vec<u8>)> {
        let hit = s.peek(T, range, side, NOW, false, |_| false, bytes_of).unwrap()?;
        assert!(s.delete_version(T, hit.version).unwrap(), "no other puller here");
        Some((hit.key.unwrap(), hit.value))
    }

    fn rows(s: &Store) -> i64 {
        s.read(T, |c| c.query_row("SELECT COUNT(*) FROM cache", [], |r| r.get(0)).map_err(|e| Error::from_sqlite(e, T)))
            .unwrap()
    }

    fn set(s: &Store, key: Key<'_>, value: &str) {
        s.set(T, key, v(value), None, NOW).unwrap();
    }

    #[test]
    fn push_numbers_from_the_middle_on_both_sides_and_pull_takes_each_end() {
        let tmp = TempDir::new("queue-basics");
        let s = store(tmp.path());
        let q = QueueRange::Int;
        assert_eq!(push(&s, &q, Side::Back, "a"), int_key(START));
        assert_eq!(push(&s, &q, Side::Back, "b"), int_key(START + 1));
        assert_eq!(push(&s, &q, Side::Front, "z"), int_key(START - 1));
        assert_eq!(peek(&s, &q, Side::Front, NOW), Some((int_key(START - 1), b"z".to_vec())));
        assert_eq!(peek(&s, &q, Side::Back, NOW), Some((int_key(START + 1), b"b".to_vec())));
        assert_eq!(rows(&s), 3, "peek removes nothing");
        assert_eq!(pull(&s, &q, Side::Front), Some((int_key(START - 1), b"z".to_vec())));
        assert_eq!(pull(&s, &q, Side::Back), Some((int_key(START + 1), b"b".to_vec())));
        assert_eq!(pull(&s, &q, Side::Back), Some((int_key(START), b"a".to_vec())));
        assert_eq!(pull(&s, &q, Side::Back), None);
        let p = QueueRange::Prefixed("jobs".into());
        assert_eq!(push(&s, &p, Side::Back, "j"), OwnedKey::Str("jobs-500000000000000".into()));
        assert_eq!(peek(&s, &q, Side::Front, NOW), None, "the int queue is another queue");
    }

    #[test]
    fn a_queue_ignores_keys_not_of_its_shape() {
        let tmp = TempDir::new("queue-shape");
        let s = store(tmp.path());
        let mut float = Encoder::new();
        float.float(5.5e14);
        let float = float.finish().unwrap();
        set(&s, Key::Typed(&float), "a float in the int range");
        let mut yes = Encoder::new();
        yes.bool(true);
        let yes = yes.finish().unwrap();
        set(&s, Key::Typed(&yes), "True");
        set(&s, Key::Str("user-9"), "an ordinary key");
        set(&s, Key::Str("user-1abc"), "another");
        let q = QueueRange::Int;
        assert_eq!(peek(&s, &q, Side::Back, NOW), None);
        assert_eq!(push(&s, &q, Side::Back, "a"), int_key(START));
        let user = QueueRange::Prefixed("user".into());
        assert_eq!(peek(&s, &user, Side::Back, NOW), None);
        assert_eq!(push(&s, &user, Side::Back, "u"), OwnedKey::Str("user-500000000000000".into()));
        assert_eq!(push(&s, &user, Side::Back, "u"), OwnedKey::Str("user-500000000000001".into()));
        // Nested prefixes never cross.
        let inner = QueueRange::Prefixed("q-1".into());
        push(&s, &inner, Side::Back, "inner");
        let outer = QueueRange::Prefixed("q".into());
        assert_eq!(peek(&s, &outer, Side::Front, NOW), None);
        assert_eq!(pull(&s, &outer, Side::Front), None);
        assert_eq!(push(&s, &outer, Side::Back, "outer"), OwnedKey::Str("q-500000000000000".into()));
        assert_eq!(pull(&s, &inner, Side::Front).unwrap().1, b"inner");
    }

    #[test]
    fn odd_prefixes_work() {
        let tmp = TempDir::new("queue-prefixes");
        let s = store(tmp.path());
        for prefix in ["", "a\0b", "é", "*?[", "a%_b", "-", "9"] {
            let q = QueueRange::Prefixed(prefix.into());
            let first = push(&s, &q, Side::Back, "1");
            assert_eq!(first, OwnedKey::Str(format!("{prefix}-500000000000000")), "{prefix:?}");
            push(&s, &q, Side::Back, "2");
            assert_eq!(pull(&s, &q, Side::Front).map(|(k, _)| k), Some(first), "{prefix:?}");
            assert_eq!(pull(&s, &q, Side::Front).unwrap().1, b"2", "{prefix:?}");
            assert_eq!(pull(&s, &q, Side::Front), None, "{prefix:?}");
        }
    }

    #[test]
    fn push_raises_past_either_end_and_never_overwrites() {
        let tmp = TempDir::new("queue-full");
        let s = store(tmp.path());
        let q = QueueRange::Int;
        set(&s, int_key(1).as_key(), "the lowest number in range");
        set(&s, int_key(0).as_key(), "a user's key 0");
        assert!(matches!(s.push(T, &q, Side::Front, v("x"), None, None, NOW), Err(Error::QueueFull)));
        s.delete(T, int_key(1).as_key(), NOW).unwrap();
        set(&s, int_key(END - 1).as_key(), "the highest");
        set(&s, int_key(END).as_key(), "a user's key END");
        assert!(matches!(s.push(T, &q, Side::Back, v("x"), None, None, NOW), Err(Error::QueueFull)));
        let p = QueueRange::Prefixed("p".into());
        set(&s, Key::Str("p-000000000000001"), "lowest");
        assert!(matches!(s.push(T, &p, Side::Front, v("x"), None, None, NOW), Err(Error::QueueFull)));
        assert_eq!(rows(&s), 4, "nothing written, nothing overwritten");
        assert_eq!(
            s.get_with(T, int_key(0).as_key(), NOW, |_| false, bytes_of).unwrap().unwrap().value,
            b"a user's key 0"
        );
    }

    #[test]
    fn reads_take_the_first_live_item_and_never_write() {
        let tmp = TempDir::new("queue-expired");
        let s = store(tmp.path());
        let q = QueueRange::Int;
        s.push(T, &q, Side::Back, v("old"), Some(1.0), None, NOW).unwrap();
        push(&s, &q, Side::Back, "new");
        assert_eq!(peek(&s, &q, Side::Front, NOW + 10.0).unwrap().1, b"new");
        assert_eq!(rows(&s), 2, "the expired head is left for expire()");
        let newest = s.peekitem(T, true, NOW + 10.0, false, |_| false, bytes_of).unwrap().unwrap();
        assert_eq!(newest.value, b"new");
        let oldest = s.peekitem(T, false, NOW + 10.0, false, |_| false, bytes_of).unwrap().unwrap();
        assert_eq!((oldest.key, oldest.value), (Some(int_key(START + 1)), b"new".to_vec()), "skips the expired");
        let live = s.peek(T, &q, Side::Front, NOW + 10.0, false, |_| false, bytes_of).unwrap().unwrap();
        assert!(s.delete_version(T, live.version).unwrap());
        assert!(s.peekitem(T, false, NOW + 10.0, false, |_| false, bytes_of).unwrap().is_none(), "only expired left");
    }

    #[test]
    fn a_head_whose_file_is_gone_is_passed_over_by_every_read() {
        let _forks = crate::testutil::one_global_fork_at_a_time();
        let tmp = TempDir::new("queue-lost");
        let s = store(tmp.path());
        let q = QueueRange::Int;
        let big = vec![b'x'; crate::spill::THRESHOLD];
        s.push(T, &q, Side::Back, Value::Bytes { mode: MODE_BYTES, data: &big }, None, None, NOW).unwrap();
        push(&s, &q, Side::Back, "next");
        for entry in walk(tmp.path()) {
            std::fs::remove_file(entry).unwrap();
        }
        assert_eq!(peek(&s, &q, Side::Front, NOW).unwrap().1, b"next", "never spins on it");
        assert_eq!(rows(&s), 1, "and removed it, as get would (D-013)");
        let big2 = vec![b'y'; crate::spill::THRESHOLD];
        s.push(T, &q, Side::Front, Value::Bytes { mode: MODE_BYTES, data: &big2 }, None, None, NOW).unwrap();
        for entry in walk(tmp.path()) {
            std::fs::remove_file(entry).unwrap();
        }
        assert_eq!(pull(&s, &q, Side::Front).unwrap().1, b"next");
        assert_eq!(rows(&s), 0);
    }

    fn walk(dir: &Path) -> Vec<std::path::PathBuf> {
        let mut out = Vec::new();
        for entry in std::fs::read_dir(dir).unwrap() {
            let path = entry.unwrap().path();
            if path.is_dir() {
                out.extend(walk(&path));
            } else if path.extension().is_some_and(|x| x == "val") {
                out.push(path);
            }
        }
        out
    }

    #[test]
    fn a_refused_spilled_head_is_never_read_and_a_tagged_one_comes_with_its_tag() {
        let tmp = TempDir::new("queue-refused");
        let s = store(tmp.path());
        let q = QueueRange::Prefixed("q".into());
        let big = vec![b'z'; crate::spill::THRESHOLD * 2];
        s.push(T, &q, Side::Back, Value::Bytes { mode: MODE_BYTES, data: &big }, None, Some(Tag(Key::Str("t"))), NOW)
            .unwrap();
        push(&s, &q, Side::Back, "second");
        let refused = s
            .peek(T, &q, Side::Front, NOW, true, |mode| mode == MODE_BYTES, |_, v| Ok(matches!(v, ValueRef::Null)))
            .unwrap()
            .unwrap();
        assert!(refused.value, "decode saw Null: the file was never read");
        assert_eq!(rows(&s), 2, "and the head stays, with the queue behind it");
        let hit = s.peek(T, &q, Side::Front, NOW, true, |_| false, bytes_of).unwrap().unwrap();
        assert_eq!((hit.value, hit.tag), (big, Some(OwnedKey::Str("t".into()))));
        assert!(s.delete_version(T, hit.version).unwrap());
        assert!(walk(tmp.path()).is_empty(), "its file went with its row");
        let (count, size): (i64, i64) = s
            .read(T, |c| {
                c.query_row("SELECT count, size FROM state", [], |r| Ok((r.get(0)?, r.get(1)?)))
                    .map_err(|e| Error::from_sqlite(e, T))
            })
            .unwrap();
        assert_eq!((count, size), (1, 0), "the totals follow");
        assert!(matches!(s.pop(T, Key::Str("q-500000000000001"), NOW, |_| false).unwrap(), Pop::Removed(_)));
    }

    #[test]
    fn every_queue_statement_is_an_ordered_index_range_with_no_sort() {
        let tmp = TempDir::new("queue-plans");
        let s = store(tmp.path());
        s.read(T, |conn| {
            for range in [QueueRange::Int, QueueRange::Prefixed("q".into())] {
                let (low, high) = range.bounds();
                let (key_type, low, high) = (range.key_type(), low.as_key(), high.as_key());
                for sql in [HEAD_FRONT_SQL, HEAD_BACK_SQL, END_FRONT_SQL, END_BACK_SQL] {
                    let mut stmt = conn.prepare(&format!("EXPLAIN QUERY PLAN {sql}")).unwrap();
                    let params: Vec<&dyn rusqlite::ToSql> = if sql.contains("?4") {
                        vec![&key_type, &low, &high, &NOW]
                    } else {
                        vec![&key_type, &low, &high]
                    };
                    let plan: Vec<String> = stmt
                        .query_map(params.as_slice(), |r| r.get(3))
                        .unwrap()
                        .collect::<rusqlite::Result<_>>()
                        .unwrap();
                    let plan = plan.join(" | ");
                    assert!(plan.contains("cache_key (key_type=? AND key>? AND key<?)"), "{sql}: {plan}");
                    assert!(!plan.contains("TEMP B-TREE"), "{sql}: {plan}");
                }
            }
            for sql in [OLDEST_SQL, NEWEST_SQL] {
                let mut stmt = conn.prepare(&format!("EXPLAIN QUERY PLAN {sql}")).unwrap();
                let plan: Vec<String> =
                    stmt.query_map([NOW], |r| r.get(3)).unwrap().collect::<rusqlite::Result<_>>().unwrap();
                let plan = plan.join(" | ");
                assert!(!plan.contains("TEMP B-TREE"), "{sql}: {plan}");
            }
            Ok(())
        })
        .unwrap();
    }

    #[test]
    fn finding_the_end_costs_the_same_however_long_the_queue() {
        let tmp = TempDir::new("queue-cost");
        let s = store(tmp.path());
        let q = QueueRange::Int;
        let steps = |s: &Store| -> i32 {
            s.read(T, |conn| {
                let (low, high) = q.bounds();
                let mut stmt = conn.prepare(END_BACK_SQL).unwrap();
                let mut rows = stmt.query((q.key_type(), low.as_key(), high.as_key())).unwrap();
                let _ = rows.next().unwrap();
                drop(rows);
                Ok(stmt.get_status(rusqlite::StatementStatus::VmStep))
            })
            .unwrap()
        };
        push(&s, &q, Side::Back, "x");
        let short = steps(&s);
        for _ in 0..5_000 {
            push(&s, &q, Side::Back, "x");
        }
        assert!(steps(&s) <= short + 10, "{} steps against {short}", steps(&s));
    }

    fn text(s: &str) -> Vec<u8> {
        s.as_bytes().to_vec()
    }

    #[test]
    fn a_key_belongs_to_a_queue_only_with_its_exact_shape() {
        let q = QueueRange::Prefixed("q".into());
        assert_eq!(q.number(ValueRef::Text(&text("q-500000000000000"))), Some(START));
        for other in ["q-1-500000000000000", "q-1abc", "q-50000000000000", "q-5000000000000000", "q-000000000000000"] {
            assert_eq!(q.number(ValueRef::Text(&text(other))), None, "{other}");
        }
        let nul = QueueRange::Prefixed("a\0b".into());
        assert_eq!(nul.number(ValueRef::Text(&text("a\0b-000000000000007"))), Some(7));
        let wide = QueueRange::Prefixed("é".into());
        assert_eq!(wide.key_of(START), OwnedKey::Str("é-500000000000000".into()));
        assert_eq!(wide.number(ValueRef::Text(&text("é-500000000000000"))), Some(START));
        let OwnedKey::Typed(k) = int_key(START) else { panic!() };
        assert_eq!(QueueRange::Int.number(ValueRef::Blob(k.as_bytes())), Some(START));
        assert_eq!(QueueRange::Int.number(ValueRef::Blob(&[0x30, 0x80])), None, "a damaged int is passed over");
        assert_eq!(QueueRange::Int.number(ValueRef::Text(b"1")), None);
    }
}
